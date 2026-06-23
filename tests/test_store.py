from __future__ import annotations

from ccb.models import Agent, Message, Room
from ccb.store import Store


def _msg(room_id, name, content, ts=0.0):
    return Message(room_id=room_id, sender_id=name, sender_name=name, content=content, ts=ts)


def test_add_and_history_in_order(tmp_path):
    store = Store(tmp_path)
    room = store.add_room(Room(name="房间"))
    store.add_message(_msg(room.id, "你", "你好", ts=1.0))
    store.add_message(_msg(room.id, "阿工", "嗨", ts=2.0))
    assert [m.content for m in store.history(room.id)] == ["你好", "嗨"]


def test_state_and_messages_persist_across_reopen(tmp_path):
    store = Store(tmp_path)
    agent = store.add_agent(Agent(name="后端", role="后端", repo_path="/repos/api"))
    room = store.add_room(Room(name="协同", agent_ids=[agent.id]))
    store.add_message(_msg(room.id, "后端", "一行"))
    store.close()

    # 用同一目录重新打开：智能体、房间、消息都应恢复。
    reopened = Store(tmp_path)
    assert any(a.name == "后端" and a.repo_path == "/repos/api" for a in reopened.agents.values())
    assert any(r.name == "协同" for r in reopened.rooms.values())
    assert [m.content for m in reopened.history(room.id)] == ["一行"]


def test_messages_since_and_cross_room(tmp_path):
    store = Store(tmp_path)
    r1 = store.add_room(Room(name="r1"))
    r2 = store.add_room(Room(name="r2"))
    store.add_message(Message(room_id=r1.id, sender_id="a", sender_name="A", content="m1", ts=1.0))
    store.add_message(Message(room_id=r2.id, sender_id="b", sender_name="B", content="m2", ts=2.0))
    assert [m.content for m in store.messages_since(r1.id, 0.5)] == ["m1"]
    assert [m.content for m in store.messages_since(r1.id, 1.5)] == []
    both = store.messages_for_rooms_since([r1.id, r2.id], 0.0)
    assert [m.content for m in both] == ["m1", "m2"]


def test_remove_agent_detaches_from_rooms(tmp_path):
    store = Store(tmp_path)
    agent = store.add_agent(Agent(name="阿工"))
    room = store.add_room(Room(name="房间", agent_ids=[agent.id]))
    store.remove_agent(agent.id)
    assert agent.id not in room.agent_ids
    assert store.get_agent(agent.id) is None
    # 重新打开后也应保持已移除。
    store.close()
    reopened = Store(tmp_path)
    assert reopened.get_agent(agent.id) is None
    assert all(agent.id not in r.agent_ids for r in reopened.rooms.values())


def test_clear_messages_keeps_room(tmp_path):
    store = Store(tmp_path)
    room = store.add_room(Room(name="房间"))
    store.add_message(Message(room_id=room.id, sender_id="a", sender_name="阿工", content="x"))
    store.clear_messages(room.id)
    assert store.history(room.id) == []
    assert store.get_room(room.id) is not None
