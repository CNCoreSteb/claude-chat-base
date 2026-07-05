from __future__ import annotations

import asyncio

from ccb.config import Settings
from ccb.hub import Hub
from ccb.models import Agent, AgentKind, Message, Room


def _setup(tmp_path, n=2):
    h = Hub(Settings(data_dir=tmp_path / "d"))
    agents = []
    for i in range(n):
        a = Agent(name=f"P{i}", role=f"r{i}", kind=AgentKind.PEER, online=True)
        h.store.add_agent(a)
        agents.append(a)
    room = Room(name="大厅", agent_ids=[a.id for a in agents])
    h.store.add_room(room)
    return h, room, agents


def _send(h, room, agent, reply_to="", text="x"):
    meta = {"reply_to": reply_to} if reply_to else {}
    return asyncio.run(h.post_message(Message(
        room_id=room.id, sender_id=agent.id, sender_name=agent.name,
        role="agent", content=text, meta=meta)))


def test_onboarding_gates_and_builds_todos(tmp_path):
    h, room, (a, b) = _setup(tmp_path, 2)
    asyncio.run(h.pua.enable(room.id, window=60))
    st = h.pua.rooms[room.id]
    assert st["phase"] == "onboarding"
    assert h.pua.blocks(room.id, a.id, "") is None     # 未上报：允许
    _send(h, room, a, text="项目A 状态 三个问题")
    assert h.pua.blocks(room.id, a.id, "") is not None  # 已上报：挡回
    assert h.pua.blocks(room.id, b.id, "") is None      # b 仍可上报
    todos = h.store.list_todos("global")
    assert len(todos) == 1 and todos[0].created_by == a.id  # 整条上报→1 条 todo
    _send(h, room, b, text="项目B 状态 三个问题")
    assert len(h.store.list_todos("global")) == 2
    # 窗口未到不进阶段；窗口到点→质疑
    assert st["phase"] == "onboarding"
    st["deadline"] = 1.0
    asyncio.run(h.pua.tick())
    assert st["phase"] == "critique"


def test_critique_then_review_then_next_round(tmp_path):
    h, room, agents = _setup(tmp_path, 3)
    for ag in agents:
        _send(h, room, ag, text=f"{ag.name} 上报")
    asyncio.run(h.pua.enable(room.id, window=60))  # enable 后重新上报
    # 重新走 onboarding（enable 清空状态）
    st = h.pua.rooms[room.id]
    for ag in agents:
        _send(h, room, ag, text=f"{ag.name} 上报")
    st["deadline"] = 1.0
    asyncio.run(h.pua.tick())
    assert st["phase"] == "critique"

    crit_msgs = {}
    for t in list(st["todos"]):
        for critic in agents:
            if critic.id == t["owner"]:
                assert h.pua.blocks(room.id, critic.id, t["anchor"]) is not None  # 不质疑自己
                continue
            assert h.pua.blocks(room.id, critic.id, t["anchor"]) is None
            m = _send(h, room, critic, reply_to=t["anchor"], text="按我角色质疑")
            crit_msgs.setdefault(t["id"], []).append((critic, m))
    assert st["phase"] == "review"  # 全员质疑完→审查

    for t in list(st["todos"]):
        owner = next(ag for ag in agents if ag.id == t["owner"])
        for _critic, cm in crit_msgs[t["id"]]:
            assert h.pua.blocks(room.id, owner.id, cm.id) is None
            _send(h, room, owner, reply_to=cm.id, text="回应")
    assert st["phase"] == "critique" and st["round"] == 2  # 审查完→下一轮质疑


def test_all_pass_finishes(tmp_path):
    h, room, agents = _setup(tmp_path, 2)
    asyncio.run(h.pua.enable(room.id, window=60))
    for ag in agents:
        _send(h, room, ag, text=f"{ag.name} 上报")
    st = h.pua.rooms[room.id]
    st["deadline"] = 1.0
    asyncio.run(h.pua.tick())
    assert st["phase"] == "critique"
    for t in list(st["todos"]):
        for ag in agents:
            if ag.id != t["owner"]:
                asyncio.run(h.pua.pass_todo(room.id, ag.id, t["id"]))
    assert st["phase"] == "done"  # 零质疑→无可迭代
    assert h.pua.blocks(room.id, agents[0].id, "") is not None  # done 后发言被挡


def test_new_join_triggers_refresh(tmp_path):
    h, room, (a, b) = _setup(tmp_path, 2)
    asyncio.run(h.pua.enable(room.id, window=60))
    _send(h, room, a, text="A 上报")
    _send(h, room, b, text="B 上报")
    st = h.pua.rooms[room.id]
    # 新 peer 加入 → 再次静默、只让新人补报
    c = Agent(name="P2", kind=AgentKind.PEER, online=True)
    h.store.add_agent(c)
    room.agent_ids.append(c.id)
    h.store.upsert_room(room)
    asyncio.run(h.pua.on_join(room.id, c.id))
    assert st["phase"] == "onboarding" and st["refresh"] is True
    assert h.pua.blocks(room.id, c.id, "") is None      # 新人可补报
    assert h.pua.blocks(room.id, a.id, "") is not None   # 老成员仍被锁
    _send(h, room, c, text="C 上报")
    assert st["refresh"] is False                        # 补报完，刷新结束
    assert len(h.store.list_todos("global")) == 3        # 新人也建了 todo
