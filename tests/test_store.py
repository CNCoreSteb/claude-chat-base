from __future__ import annotations

import json

from ccb.models import Agent, Message, Room
from ccb.store import Store


def test_add_and_history(tmp_path):
    store = Store(tmp_path)
    room = store.add_room(Room(name="房间"))
    store.add_message(Message(room_id=room.id, sender_id="human", sender_name="你", content="你好"))
    store.add_message(Message(room_id=room.id, sender_id="a", sender_name="阿工", content="嗨"))
    history = store.history(room.id)
    assert [m.content for m in history] == ["你好", "嗨"]


def test_transcript_is_persisted(tmp_path):
    store = Store(tmp_path)
    room = store.add_room(Room(name="房间"))
    store.add_message(Message(room_id=room.id, sender_id="a", sender_name="阿工", content="一行"))
    path = tmp_path / "transcripts" / f"{room.id}.jsonl"
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["content"] == "一行"
    assert record["sender_name"] == "阿工"


def test_remove_agent_detaches_from_rooms(tmp_path):
    store = Store(tmp_path)
    agent = store.add_agent(Agent(name="阿工"))
    room = store.add_room(Room(name="房间", agent_ids=[agent.id]))
    store.remove_agent(agent.id)
    assert agent.id not in room.agent_ids
    assert store.get_agent(agent.id) is None


def test_clear_messages_keeps_room(tmp_path):
    store = Store(tmp_path)
    room = store.add_room(Room(name="房间"))
    store.add_message(Message(room_id=room.id, sender_id="a", sender_name="阿工", content="x"))
    store.clear_messages(room.id)
    assert store.history(room.id) == []
