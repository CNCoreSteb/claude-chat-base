"""In-memory state store with append-only transcript persistence.

State (agents, rooms, messages) lives in memory for speed and simplicity. Every
message is also appended to a per-room JSONL transcript on disk, so conversations
survive restarts and can be replayed or analyzed offline. This append-only design
is robust and cross-platform (no database engine required).
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import Agent, Message, Room


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.transcripts_dir = self.data_dir / "transcripts"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)

        self.agents: dict[str, Agent] = {}
        self.rooms: dict[str, Room] = {}
        self.messages: dict[str, list[Message]] = {}  # room_id -> ordered messages

    # ----- agents -------------------------------------------------------------

    def add_agent(self, agent: Agent) -> Agent:
        self.agents[agent.id] = agent
        return agent

    def remove_agent(self, agent_id: str) -> None:
        self.agents.pop(agent_id, None)
        for room in self.rooms.values():
            if agent_id in room.agent_ids:
                room.agent_ids.remove(agent_id)

    def get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    # ----- rooms --------------------------------------------------------------

    def add_room(self, room: Room) -> Room:
        self.rooms[room.id] = room
        self.messages.setdefault(room.id, [])
        return room

    def get_room(self, room_id: str) -> Room | None:
        return self.rooms.get(room_id)

    # ----- messages -----------------------------------------------------------

    def add_message(self, message: Message) -> Message:
        self.messages.setdefault(message.room_id, []).append(message)
        self._append_transcript(message)
        return message

    def history(self, room_id: str) -> list[Message]:
        return self.messages.get(room_id, [])

    def clear_messages(self, room_id: str) -> None:
        """Reset a room's in-memory history (transcript file is kept)."""
        self.messages[room_id] = []

    def _transcript_path(self, room_id: str) -> Path:
        return self.transcripts_dir / f"{room_id}.jsonl"

    def _append_transcript(self, message: Message) -> None:
        path = self._transcript_path(message.room_id)
        line = json.dumps(message.model_dump(), ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
