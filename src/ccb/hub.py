"""Hub：唯一的状态真相来源 + 事件广播器。

Hub 持有 :class:`Store`，负责解析 LLM 提供方，并把事件推送给所有已连接的
WebSocket 客户端。所有 GUI 关心的状态变更都经由某个 ``*`` 辅助方法发出，从而让
界面保持实时同步。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import Settings, load_preset
from .llm import AnthropicProvider, LLMProvider, MockProvider
from .models import (
    Agent,
    AgentCreate,
    AgentUpdate,
    Message,
    Room,
    RoomCreate,
    RoomStatus,
    RoomUpdate,
)
from .store import Store

log = logging.getLogger("ccb.hub")


class Hub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.data_dir)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._providers: dict[str, LLMProvider] = {}
        self._warned_no_key = False
        # 编排器在构造之后再注入，避免循环导入。
        self.orchestrator: Any | None = None

    # ----- 启动引导 ----------------------------------------------------------

    def load_presets(self) -> None:
        agents, rooms = load_preset(self.settings.preset, self.settings.default_model)
        for agent in agents:
            self.store.add_agent(agent)
        for room in rooms:
            self.store.add_room(room)
        log.info("已从预设加载 %d 个智能体、%d 个房间", len(agents), len(rooms))

    # ----- 提供方 ----------------------------------------------------------

    def resolve_provider_name(self, agent: Agent) -> str:
        return agent.provider or self.settings.resolved_provider()

    def get_provider(self, name: str) -> LLMProvider:
        if name in self._providers:
            return self._providers[name]

        if name == "anthropic":
            key = self.settings.anthropic_api_key
            if not key:
                if not self._warned_no_key:
                    log.warning("未设置 ANTHROPIC API 密钥，回退到 mock 提供方。")
                    self._warned_no_key = True
                return self.get_provider("mock")
            provider: LLMProvider = AnthropicProvider(key)
        elif name == "mock":
            provider = MockProvider()
        else:
            raise ValueError(f"未知的提供方：{name}")

        self._providers[name] = provider
        return provider

    # ----- 发布 / 订阅 --------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def broadcast(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 客户端太慢：直接丢弃它，而不是阻塞整个房间。
                self._subscribers.discard(queue)

    # ----- 快照 -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "agents": [a.model_dump() for a in self.store.agents.values()],
            "rooms": [r.model_dump() for r in self.store.rooms.values()],
            "messages": {
                rid: [m.model_dump() for m in msgs]
                for rid, msgs in self.store.messages.items()
            },
            "server": {
                "provider": self.settings.resolved_provider(),
                "default_model": self.settings.default_model,
                "has_api_key": bool(self.settings.anthropic_api_key),
            },
        }

    # ----- 智能体操作 ---------------------------------------------------------

    async def create_agent(self, data: AgentCreate) -> Agent:
        from .models import AGENT_COLORS

        payload = data.model_dump()
        # 输入里的 color 是可选的；省略时让模型默认值来填。
        color = payload.pop("color", None)
        agent = Agent(**payload)
        # 按当前智能体数量在配色表里轮流取色。
        agent.color = color or AGENT_COLORS[len(self.store.agents) % len(AGENT_COLORS)]
        if agent.model is None:
            agent.model = self.settings.default_model
        self.store.add_agent(agent)
        await self.broadcast({"type": "agent_added", "agent": agent.model_dump()})
        return agent

    async def update_agent(self, agent_id: str, data: AgentUpdate) -> Agent | None:
        agent = self.store.get_agent(agent_id)
        if not agent:
            return None
        for field, value in data.model_dump(exclude_none=True).items():
            setattr(agent, field, value)
        await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})
        return agent

    async def delete_agent(self, agent_id: str) -> None:
        self.store.remove_agent(agent_id)
        await self.broadcast({"type": "agent_removed", "agent_id": agent_id})

    async def set_agent_status(self, agent_id: str, status: str) -> None:
        agent = self.store.get_agent(agent_id)
        if not agent:
            return
        agent.status = status  # type: ignore[assignment]
        await self.broadcast(
            {"type": "agent_status", "agent_id": agent_id, "status": status}
        )

    # ----- 房间操作 ----------------------------------------------------------

    async def create_room(self, data: RoomCreate) -> Room:
        room = Room(**data.model_dump())
        room.turn_delay = room.turn_delay or self.settings.turn_delay
        self.store.add_room(room)
        await self.broadcast({"type": "room_added", "room": room.model_dump()})
        return room

    async def update_room(self, room_id: str, data: RoomUpdate) -> Room | None:
        room = self.store.get_room(room_id)
        if not room:
            return None
        for field, value in data.model_dump(exclude_none=True).items():
            setattr(room, field, value)
        await self.broadcast({"type": "room_updated", "room": room.model_dump()})
        return room

    async def set_room_status(self, room_id: str, status: RoomStatus) -> None:
        room = self.store.get_room(room_id)
        if not room:
            return
        room.status = status
        await self.broadcast(
            {
                "type": "room_status",
                "room_id": room_id,
                "status": status.value,
                "turn": room.turn,
            }
        )

    # ----- 消息 -----------------------------------------------------------

    async def post_message(self, message: Message) -> Message:
        self.store.add_message(message)
        await self.broadcast({"type": "message", "message": message.model_dump()})
        return message

    async def reset_room(self, room_id: str) -> None:
        room = self.store.get_room(room_id)
        if not room:
            return
        self.store.clear_messages(room_id)
        room.turn = 0
        room.status = RoomStatus.IDLE
        await self.broadcast({"type": "room_reset", "room_id": room_id})
        await self.set_room_status(room_id, RoomStatus.IDLE)
