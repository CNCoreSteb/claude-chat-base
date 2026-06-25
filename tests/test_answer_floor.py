from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from ccb.config import Settings
from ccb.hub import ANSWER_ROUND_OPEN_TTL, Hub
from ccb.models import Agent, AgentKind, Message, Room
from ccb.server import create_app


def _hub(tmp_path, scope="human", enforcement="soft") -> Hub:
    s = Settings(data_dir=tmp_path / "d",
                 floor_scope=scope, floor_enforcement=enforcement)
    return Hub(s)


async def test_claim_queue_release_promote(tmp_path):
    h = _hub(tmp_path)
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    b = h.store.add_agent(Agent(name="B", kind=AgentKind.PEER))
    # 必须先有一轮进行中的提问才能抢（F2：claim 不凭空起一轮）
    await h.open_round(room.id, Message(room_id=room.id, sender_id="human",
                                        sender_name="你", role="human", content="各位？"))
    granted, st = await h.claim_floor(room.id, a.id)
    assert granted and st["holder"] == a.id
    granted2, st2 = await h.claim_floor(room.id, b.id)
    assert not granted2 and st2["holder"] == a.id and b.id in st2["queue"]
    # B 反复 claim 复查仍未轮到（幂等、不重复入队）
    g, st3 = await h.claim_floor(room.id, b.id)
    assert not g and st3["queue"] == [b.id]
    # A 释放 -> B 自动提升为 holder
    st4 = await h.release_floor(room.id, a.id)
    assert st4["holder"] == b.id and st4["queue"] == []
    # B 释放 -> 空闲、本轮关闭
    st5 = await h.release_floor(room.id, b.id)
    assert st5["holder"] is None and st5["active"] is False


async def test_human_opens_round_agent_does_not_in_human_scope(tmp_path):
    h = _hub(tmp_path, scope="human")
    room = h.store.add_room(Room(name="大厅"))
    human = Message(room_id=room.id, sender_id="human", sender_name="你",
                    role="human", content="各位刚都做了什么？")
    assert h.message_opens_round(human)
    await h.post_message(human)
    assert h.round_active(room.id)
    agent_msg = Message(room_id=room.id, sender_id="x", sender_name="X", role="agent", content="嗨")
    assert not h.message_opens_round(agent_msg)


async def test_directed_message_does_not_open_round(tmp_path):
    """带 meta.to 的定向提问只该那一个回答，不应开应答轮（否则 @ 单个人也弹"待应答"且卡住）。"""
    h = _hub(tmp_path, scope="human")
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    directed = Message(room_id=room.id, sender_id="human", sender_name="你",
                       role="human", content="@A 请你处理", meta={"to": a.id})
    assert not h.message_opens_round(directed)
    await h.post_message(directed)
    assert not h.round_active(room.id)


async def test_empty_round_ttl_closes_and_broadcasts(tmp_path):
    """无人认领的空轮过期后 reconcile 关闭并广播 answer_floor，让横幅消失（不再卡"待应答"）。"""
    h = _hub(tmp_path)
    room = h.store.add_room(Room(name="大厅"))
    human = Message(room_id=room.id, sender_id="human", sender_name="你",
                    role="human", content="各位在吗？")
    await h.post_message(human)  # 开轮（无人认领）
    assert h.round_active(room.id)
    q = h.subscribe()
    h._floors[room.id]["opened_at"] = time.time() - ANSWER_ROUND_OPEN_TTL - 1  # 推到过期
    await h.reconcile_floors()
    assert not h.round_active(room.id)
    evs = []
    while not q.empty():
        evs.append(q.get_nowait())
    assert any(e.get("type") == "answer_floor" and not e["floor"]["active"] for e in evs)


async def test_broadcast_scope_round_detection(tmp_path):
    h = _hub(tmp_path, scope="broadcast")
    room = h.store.add_room(Room(name="大厅"))
    question = Message(room_id=room.id, sender_id="x", sender_name="X",
                       role="agent", content="谁能看下？", meta={"is_question": True})
    plain = Message(room_id=room.id, sender_id="x", sender_name="X",
                    role="agent", content="大家看下", meta={})
    directed = Message(room_id=room.id, sender_id="x", sender_name="X",
                       role="agent", content="@后端", meta={"to": "be"})
    assert h.message_opens_round(question)       # 广播问题开轮
    assert not h.message_opens_round(plain)       # 普通广播不开轮（F1）
    assert not h.message_opens_round(directed)    # 定向不开轮


async def test_floor_blocks_only_in_hard(tmp_path):
    h = _hub(tmp_path, scope="human", enforcement="soft")
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    b = h.store.add_agent(Agent(name="B", kind=AgentKind.PEER))
    await h.open_round(room.id, Message(room_id=room.id, sender_id="human",
                                        sender_name="你", role="human", content="各位？"))
    await h.claim_floor(room.id, a.id)
    assert h.floor_blocks(room.id, b.id) is False  # soft 从不挡
    h.floor_enforcement = "hard"
    assert h.floor_blocks(room.id, b.id) is True   # hard 挡非 holder
    assert h.floor_blocks(room.id, a.id) is False  # holder 不挡


async def test_holder_removed_promotes_next(tmp_path):
    h = _hub(tmp_path)
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    b = h.store.add_agent(Agent(name="B", kind=AgentKind.PEER))
    await h.open_round(room.id, Message(room_id=room.id, sender_id="human",
                                        sender_name="你", role="human", content="各位？"))
    await h.claim_floor(room.id, a.id)
    await h.claim_floor(room.id, b.id)
    await h.delete_agent(a.id)  # holder 被删 -> 队首 B 顶上
    assert h.floor_state(room.id)["holder"] == b.id


def test_endpoints_and_hard_enforcement(tmp_path):
    s = Settings(data_dir=tmp_path / "d",
                 preset=Path("nope.toml"), floor_scope="human", floor_enforcement="hard")
    with TestClient(create_app(s)) as cl:
        room = cl.post("/api/rooms", json={"name": "大厅"}).json()["id"]
        a = cl.post("/api/instances/connect", json={"name": "A"}).json()["agent_id"]
        b = cl.post("/api/instances/connect", json={"name": "B"}).json()["agent_id"]
        cl.post(f"/api/rooms/{room}/agents/{a}")
        cl.post(f"/api/rooms/{room}/agents/{b}")

        # 人类提问 -> 开一轮应答
        cl.post(f"/api/rooms/{room}/messages", json={"content": "各位刚都做了什么？"})
        # hard：A 未持有应答位就直接答 -> 被 409 挡
        assert cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "我做了X", "agent_id": a}).status_code == 409
        # A claim -> 成 holder -> 可答
        assert cl.post(f"/api/rooms/{room}/answer/claim",
                       json={"agent_id": a}).json()["granted"] is True
        assert cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "我做了X", "agent_id": a}).status_code == 200
        # B claim -> 排队（granted False），且答复仍被挡
        jb = cl.post(f"/api/rooms/{room}/answer/claim", json={"agent_id": b}).json()
        assert jb["granted"] is False and b in jb["floor"]["queue"]
        assert cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "我补充", "agent_id": b}).status_code == 409
        # A 释放 -> B 自动提升 -> B 可答
        cl.post(f"/api/rooms/{room}/answer/release", json={"agent_id": a})
        assert cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "我补充", "agent_id": b}).status_code == 200

        # 运行期切到 off -> 不再开轮、不再拦截
        assert cl.patch("/api/answer-floor", json={"scope": "off"}).json()["scope"] == "off"
        c = cl.post("/api/instances/connect", json={"name": "C"}).json()["agent_id"]
        cl.post(f"/api/rooms/{room}/agents/{c}")
        cl.post(f"/api/rooms/{room}/messages", json={"content": "问题2"})
        assert cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "随便答", "agent_id": c}).status_code == 200

        # 快照暴露应答位与配置
        snap = cl.get("/api/state").json()
        assert snap["server"]["floor_scope"] == "off"
        assert room in snap["floors"]


async def test_kicked_holder_promotes_queue_and_broadcasts(tmp_path):
    # holder 被踢/离线 -> 队首自动提升，且必须广播 answer_floor（GUI/被提升者即时同步）。
    h = _hub(tmp_path)
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    b = h.store.add_agent(Agent(name="B", kind=AgentKind.PEER))
    await h.open_round(room.id, Message(room_id=room.id, sender_id="human",
                                        sender_name="你", role="human", content="各位？"))
    await h.claim_floor(room.id, a.id)   # A 持有
    await h.claim_floor(room.id, b.id)   # B 排队
    q = h.subscribe()
    await h.kick_peer(a.id)
    assert h.floor_state(room.id)["holder"] == b.id
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert any(e.get("type") == "answer_floor" and e["floor"]["holder"] == b.id for e in events)


async def test_lone_holder_offline_closes_round(tmp_path):
    # 唯一 holder 离线、无人排队 -> 关闭本轮，否则 hard 模式下会卡住所有人直到 TTL 兜底。
    h = _hub(tmp_path, enforcement="hard")
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    b = h.store.add_agent(Agent(name="B", kind=AgentKind.PEER))
    await h.post_message(Message(room_id=room.id, sender_id="human", sender_name="你",
                                 role="human", content="各位？"))
    await h.claim_floor(room.id, a.id)
    assert h.round_active(room.id) is True
    await h.mark_peer_seen(a.id)
    await h.set_peer_offline(a.id)
    assert h.floor_state(room.id)["holder"] is None
    assert h.round_active(room.id) is False       # 轮已关闭
    assert h.floor_blocks(room.id, b.id) is False  # 不再拦截其他人


async def test_claim_without_active_round_is_refused(tmp_path):
    # 没有进行中的提问轮时 claim 被拒，不凭空起一轮在 hard 模式挡住别人（F2）。
    h = _hub(tmp_path, enforcement="hard")
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    b = h.store.add_agent(Agent(name="B", kind=AgentKind.PEER))
    granted, st = await h.claim_floor(room.id, a.id)
    assert granted is False and st["holder"] is None and st["active"] is False
    assert h.floor_blocks(room.id, b.id) is False  # 没有凭空挡住别人
    # 一旦有真正的提问开轮，claim 即可生效
    await h.open_round(room.id, Message(room_id=room.id, sender_id="human",
                                        sender_name="你", role="human", content="各位？"))
    granted2, st2 = await h.claim_floor(room.id, a.id)
    assert granted2 is True and st2["holder"] == a.id


async def test_broadcast_scope_only_questions_open_round(tmp_path):
    # broadcast scope 下：普通 agent 广播不开/不冲轮，只有标记为 is_question 的才开轮（F1）。
    h = _hub(tmp_path, scope="broadcast")
    room = h.store.add_room(Room(name="大厅"))
    plain = Message(room_id=room.id, sender_id="x", sender_name="X",
                    role="agent", content="我这边好了")
    question = Message(room_id=room.id, sender_id="x", sender_name="X",
                       role="agent", content="谁能评估？", meta={"is_question": True})
    assert h.message_opens_round(plain) is False
    assert h.message_opens_round(question) is True
    # 进行中的一轮不应被普通广播冲掉
    await h.open_round(room.id, question)
    assert h.round_active(room.id) is True
    await h.post_message(plain)
    assert h.round_active(room.id) is True


async def test_reset_room_clears_floor(tmp_path):
    # 清空主题应一并重置应答位（F6：reset_room 里 _floors.pop）。
    h = _hub(tmp_path)
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    await h.open_round(room.id, Message(room_id=room.id, sender_id="human",
                                        sender_name="你", role="human", content="各位？"))
    await h.claim_floor(room.id, a.id)
    assert h.round_active(room.id) is True
    await h.reset_room(room.id)
    assert h.round_active(room.id) is False
    assert h.floor_state(room.id)["holder"] is None
