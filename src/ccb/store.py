"""内存状态存储 + 仅追加的对话记录持久化。

状态（智能体、房间、消息）保存在内存中以追求速度与简洁。每条消息同时会被
追加写入按房间划分的 JSONL 文件，因此对话可在重启后保留，也能离线回放或分析。
这种"仅追加"的设计稳健且跨平台（无需任何数据库引擎）。
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
        self.messages: dict[str, list[Message]] = {}  # room_id -> 有序消息列表

    # ----- 智能体 -------------------------------------------------------------

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

    # ----- 房间 --------------------------------------------------------------

    def add_room(self, room: Room) -> Room:
        self.rooms[room.id] = room
        self.messages.setdefault(room.id, [])
        return room

    def get_room(self, room_id: str) -> Room | None:
        return self.rooms.get(room_id)

    # ----- 消息 -----------------------------------------------------------

    def add_message(self, message: Message) -> Message:
        self.messages.setdefault(message.room_id, []).append(message)
        self._append_transcript(message)
        return message

    def history(self, room_id: str) -> list[Message]:
        return self.messages.get(room_id, [])

    def clear_messages(self, room_id: str) -> None:
        """清空某个房间的内存历史（磁盘上的记录文件保留）。"""
        self.messages[room_id] = []

    def _transcript_path(self, room_id: str) -> Path:
        return self.transcripts_dir / f"{room_id}.jsonl"

    def _append_transcript(self, message: Message) -> None:
        path = self._transcript_path(message.room_id)
        line = json.dumps(message.model_dump(), ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
