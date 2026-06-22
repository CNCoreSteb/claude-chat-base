from __future__ import annotations

import json

from agora.models import Agent, Message, Room
from agora.store import Store


def test_add_and_history(tmp_path):
    store = Store(tmp_path)
    room = store.add_room(Room(name="R"))
    store.add_message(Message(room_id=room.id, sender_id="human", sender_name="You", content="hi"))
    store.add_message(Message(room_id=room.id, sender_id="a", sender_name="Ada", content="hello"))
    history = store.history(room.id)
    assert [m.content for m in history] == ["hi", "hello"]


def test_transcript_is_persisted(tmp_path):
    store = Store(tmp_path)
    room = store.add_room(Room(name="R"))
    store.add_message(Message(room_id=room.id, sender_id="a", sender_name="Ada", content="line"))
    path = tmp_path / "transcripts" / f"{room.id}.jsonl"
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["content"] == "line"
    assert record["sender_name"] == "Ada"


def test_remove_agent_detaches_from_rooms(tmp_path):
    store = Store(tmp_path)
    agent = store.add_agent(Agent(name="Ada"))
    room = store.add_room(Room(name="R", agent_ids=[agent.id]))
    store.remove_agent(agent.id)
    assert agent.id not in room.agent_ids
    assert store.get_agent(agent.id) is None


def test_clear_messages_keeps_room(tmp_path):
    store = Store(tmp_path)
    room = store.add_room(Room(name="R"))
    store.add_message(Message(room_id=room.id, sender_id="a", sender_name="Ada", content="x"))
    store.clear_messages(room.id)
    assert store.history(room.id) == []
