"""Hub：唯一的状态真相来源 + 事件广播器。

Hub 持有 :class:`Store`，负责解析 LLM 提供方，并把事件推送给所有已连接的
WebSocket 客户端。所有 GUI 关心的状态变更都经由某个 ``*`` 辅助方法发出，从而让
界面保持实时同步。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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

# 超过这么多秒没有任何活动，就把 peer 视为离线。
PEER_STALE_SECONDS = 40.0
# 配置中不应持久化的运行期字段。
_AGENT_RUNTIME_FIELDS = {"status", "online", "last_seen"}
_ROOM_RUNTIME_FIELDS = {"status", "turn"}


class Hub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.data_dir)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._providers: dict[str, LLMProvider] = {}
        self._warned_no_key = False
        # 编排器在构造之后再注入，避免循环导入。
        self.orchestrator: Any | None = None

    # ----- 启动引导与持久化 --------------------------------------------------

    @property
    def config_path(self):  # noqa: ANN201
        return self.settings.data_dir / "config.json"

    def bootstrap(self) -> None:
        """加载状态：优先读用户保存的 config.json，否则用预设并立即保存。"""
        if self.load_config():
            log.info(
                "已从 %s 加载 %d 个智能体、%d 个房间",
                self.config_path,
                len(self.store.agents),
                len(self.store.rooms),
            )
            return
        agents, rooms = load_preset(self.settings.preset, self.settings.default_model)
        for agent in agents:
            self.store.add_agent(agent)
        for room in rooms:
            self.store.add_room(room)
        self.save_config()
        log.info("已从预设加载 %d 个智能体、%d 个房间", len(agents), len(rooms))

    def load_config(self) -> bool:
        if not self.config_path.exists():
            return False
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("读取 %s 失败，回退到预设", self.config_path)
            return False
        for ad in data.get("agents", []):
            self.store.add_agent(Agent(**ad))  # 运行期字段用默认值
        for rd in data.get("rooms", []):
            self.store.add_room(Room(**rd))
        return True

    def save_config(self) -> None:
        """把智能体与房间配置（不含运行期字段）写盘，使 GUI 中的设置持久化。"""
        data = {
            "agents": [
                {k: v for k, v in a.model_dump().items() if k not in _AGENT_RUNTIME_FIELDS}
                for a in self.store.agents.values()
            ],
            "rooms": [
                {k: v for k, v in r.model_dump().items() if k not in _ROOM_RUNTIME_FIELDS}
                for r in self.store.rooms.values()
            ],
        }
        try:
            self.config_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            log.exception("写入 %s 失败", self.config_path)

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
        self.save_config()
        await self.broadcast({"type": "agent_added", "agent": agent.model_dump()})
        return agent

    async def update_agent(self, agent_id: str, data: AgentUpdate) -> Agent | None:
        agent = self.store.get_agent(agent_id)
        if not agent:
            return None
        for field, value in data.model_dump(exclude_none=True).items():
            setattr(agent, field, value)
        self.save_config()
        await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})
        return agent

    async def delete_agent(self, agent_id: str) -> None:
        self.store.remove_agent(agent_id)
        self.save_config()
        await self.broadcast({"type": "agent_removed", "agent_id": agent_id})

    async def set_agent_status(self, agent_id: str, status: str) -> None:
        agent = self.store.get_agent(agent_id)
        if not agent:
            return
        agent.status = status  # type: ignore[assignment]
        await self.broadcast(
            {"type": "agent_status", "agent_id": agent_id, "status": status}
        )

    # ----- peer 在线状态 ------------------------------------------------------

    async def mark_peer_seen(self, agent_id: str) -> None:
        """记录某个 peer 刚有过活动；必要时把它标记为在线并广播。"""
        agent = self.store.get_agent(agent_id)
        if not agent:
            return
        agent.last_seen = time.time()
        if not agent.online:
            agent.online = True
            await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})

    async def set_peer_offline(self, agent_id: str) -> None:
        agent = self.store.get_agent(agent_id)
        if agent and agent.online:
            agent.online = False
            await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})

    async def reconcile_peers(self) -> None:
        """把长时间无活动的 peer 标记为离线（由后台任务周期性调用）。"""
        now = time.time()
        for agent in list(self.store.agents.values()):
            if agent.online and now - agent.last_seen > PEER_STALE_SECONDS:
                agent.online = False
                await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})

    # ----- 房间操作 ----------------------------------------------------------

    async def create_room(self, data: RoomCreate) -> Room:
        room = Room(**data.model_dump())
        room.turn_delay = room.turn_delay or self.settings.turn_delay
        self.store.add_room(room)
        self.save_config()
        await self.broadcast({"type": "room_added", "room": room.model_dump()})
        return room

    async def update_room(self, room_id: str, data: RoomUpdate) -> Room | None:
        room = self.store.get_room(room_id)
        if not room:
            return None
        for field, value in data.model_dump(exclude_none=True).items():
            setattr(room, field, value)
        self.save_config()
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
