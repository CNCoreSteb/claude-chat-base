from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ccb.config import Settings
from ccb.hub import Hub
from ccb.models import Agent, AgentKind, Message, Room
from ccb.orchestrator import Orchestrator
from ccb.server import create_app


def _hub(tmp_path, scope="human", enforcement="soft") -> Hub:
    s = Settings(provider="mock", anthropic_api_key=None, data_dir=tmp_path / "d",
                 floor_scope=scope, floor_enforcement=enforcement)
    h = Hub(s)
    h.orchestrator = Orchestrator(h)
    return h


async def test_claim_queue_release_promote(tmp_path):
    h = _hub(tmp_path)
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    b = h.store.add_agent(Agent(name="B", kind=AgentKind.PEER))

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


async def test_broadcast_scope_round_detection(tmp_path):
    h = _hub(tmp_path, scope="broadcast")
    room = h.store.add_room(Room(name="大厅"))
    broadcast = Message(room_id=room.id, sender_id="x", sender_name="X",
                        role="agent", content="大家看下", meta={})
    directed = Message(room_id=room.id, sender_id="x", sender_name="X",
                       role="agent", content="@后端", meta={"to": "be"})
    assert h.message_opens_round(broadcast)
    assert not h.message_opens_round(directed)


async def test_floor_blocks_only_in_hard(tmp_path):
    h = _hub(tmp_path, scope="human", enforcement="soft")
    room = h.store.add_room(Room(name="大厅"))
    a = h.store.add_agent(Agent(name="A", kind=AgentKind.PEER))
    b = h.store.add_agent(Agent(name="B", kind=AgentKind.PEER))
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
    await h.claim_floor(room.id, a.id)
    await h.claim_floor(room.id, b.id)
    await h.delete_agent(a.id)  # holder 被删 -> 队首 B 顶上
    assert h.floor_state(room.id)["holder"] == b.id


def test_endpoints_and_hard_enforcement(tmp_path):
    s = Settings(provider="mock", anthropic_api_key=None, data_dir=tmp_path / "d",
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
