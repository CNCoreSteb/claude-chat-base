"""Hub：唯一的状态真相来源 + 事件广播器。

Hub 持有 :class:`Store`，负责解析 LLM 提供方，并把事件推送给所有已连接的
WebSocket 客户端。所有 GUI 关心的状态变更都经由某个 ``*`` 辅助方法发出，从而让
界面保持实时同步。
"""

from __future__ import annotations

import asyncio
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

# 超过这么多秒没收到心跳/活动，就把 peer 视为离线。
# MCP 桥接进程每 ~15 秒后台心跳一次（零 token），这里取约 3 拍的容忍度。
PEER_STALE_SECONDS = 45.0

# 应答位被持有但迟迟不释放（holder 崩溃/走神）超过这么多秒就自动放行队首，避免卡死全队。
ANSWER_FLOOR_TTL = 120.0
# 开了一轮应答但没人认领（无 holder/队列）超过这么多秒就自动关闭——避免 hard 模式下人类随口
# 一句就长时间挡住别人回答。比 holder 持有上限短得多。
ANSWER_ROUND_OPEN_TTL = 30.0


class Hub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.data_dir)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._providers: dict[str, LLMProvider] = {}
        self._warned_no_key = False
        # 被「踢掉」的 peer：其心跳/活动不再让它复活在线，直到它主动重新注册。
        self._kicked: set[str] = set()
        # 每个房间一把邀请锁：把并发的 invite 串行化，避免同一实例被同时多次拉进同一主题
        # （后到的请求进锁后会发现它已在主题里，直接返回「已在主题内」而不再重复广播）。
        self._invite_locks: dict[str, asyncio.Lock] = {}
        # 应答编排：每个房间一个「应答位」(holder) + 排队，避免广播问题被多个 peer 一拥而上
        # 重复回答。运行期可配置（GUI/env）：scope=off/human/broadcast，enforcement=soft/hard。
        self.floor_scope: str = settings.floor_scope
        self.floor_enforcement: str = settings.floor_enforcement
        self._floors: dict[str, dict[str, Any]] = {}
        # 编排器在构造之后再注入，避免循环导入。
        self.orchestrator: Any | None = None

    # ----- 启动引导 ----------------------------------------------------------

    def bootstrap(self) -> None:
        """加载状态：SQLite 里已有配置则直接用，否则导入预设（会写入 DB）。"""
        if self.store.agents or self.store.rooms:
            log.info(
                "已从数据库加载 %d 个智能体、%d 个房间",
                len(self.store.agents),
                len(self.store.rooms),
            )
            return
        agents, rooms = load_preset(self.settings.preset, self.settings.default_model)
        for agent in agents:
            self.store.add_agent(agent)
        for room in rooms:
            self.store.add_room(room)
        log.info("已从预设导入 %d 个智能体、%d 个房间", len(agents), len(rooms))

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
                # 客户端太慢：把它从订阅集移除，而不是阻塞整个房间。但仅仅移除会让对应的
                # WebSocket 任务永远阻塞在 queue.get() 上、socket 既不收事件也不关闭，GUI 静默
                # 失同步且不会触发前端重连。于是腾出一格塞入 None 哨兵，唤醒该任务去关闭连接，
                # 让浏览器重连并重新拉取快照。
                self._subscribers.discard(queue)
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(None)  # type: ignore[arg-type]
                except asyncio.QueueFull:
                    pass

    def _invite_lock(self, room_id: str) -> asyncio.Lock:
        """返回某房间的邀请串行锁（按需创建）。"""
        lock = self._invite_locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            self._invite_locks[room_id] = lock
        return lock

    # ----- 应答编排（answer floor）----------------------------------------------

    def set_floor_config(self, scope: str | None = None, enforcement: str | None = None) -> None:
        if scope in ("off", "human", "broadcast"):
            self.floor_scope = scope
        if enforcement in ("soft", "hard"):
            self.floor_enforcement = enforcement

    def _floor(self, room_id: str) -> dict[str, Any]:
        f = self._floors.get(room_id)
        if f is None:
            f = {"holder": None, "queue": [], "round_msg": None,
                 "opened_at": 0.0, "claimed_at": 0.0}
            self._floors[room_id] = f
        return f

    def _agent_name(self, agent_id: str | None) -> str | None:
        a = self.store.get_agent(agent_id) if agent_id else None
        return a.name if a else None

    def round_active(self, room_id: str) -> bool:
        """是否有进行中的应答轮：有人持有/排队，或触发问题仍在 TTL 窗口内。"""
        f = self._floors.get(room_id)
        if not f:
            return False
        if f["holder"] or f["queue"]:
            return True
        opened = f.get("opened_at") or 0.0
        return opened > 0 and (time.time() - opened) < ANSWER_ROUND_OPEN_TTL

    def floor_state(self, room_id: str) -> dict[str, Any]:
        """对外可序列化的应答位状态（供 GUI / 工具返回 / 快照）。"""
        f = self._floors.get(room_id) or {}
        q = list(f.get("queue", []))
        return {
            "holder": f.get("holder"),
            "holder_name": self._agent_name(f.get("holder")),
            "queue": q,
            "queue_names": [self._agent_name(a) for a in q],
            "round_msg": f.get("round_msg"),
            "active": self.round_active(room_id),
            "scope": self.floor_scope,
            "enforcement": self.floor_enforcement,
        }

    def message_opens_round(self, message: Message) -> bool:
        """按 scope 判断这条消息是否应开启一轮应答。"""
        if self.floor_scope == "off":
            return False
        if message.role == "human":
            return True  # 人类(GUI 用户)提问：human 与 broadcast 两种 scope 都触发
        if self.floor_scope == "broadcast" and message.role == "agent":
            # 广播 = 没有专门指向单个对象（meta.to 为空）
            return not (message.meta or {}).get("to")
        return False

    async def open_round(self, room_id: str, message: Message) -> None:
        """开启/刷新一轮应答（新问题重置应答位）。"""
        f = self._floor(room_id)
        f.update(holder=None, queue=[], round_msg=message.id,
                 opened_at=time.time(), claimed_at=0.0)
        await self.broadcast(
            {"type": "answer_floor", "room_id": room_id, "floor": self.floor_state(room_id)}
        )

    async def claim_floor(self, room_id: str, agent_id: str) -> tuple[bool, dict[str, Any]]:
        """抢应答位：空闲→成为 holder；被别人持有→排队（幂等）。返回 (是否轮到你, 状态)。"""
        f = self._floor(room_id)
        if f["holder"] == agent_id:
            f["claimed_at"] = time.time()
            return True, self.floor_state(room_id)
        if f["holder"] is None:
            f["holder"] = agent_id
            f["claimed_at"] = time.time()
            if agent_id in f["queue"]:
                f["queue"].remove(agent_id)
            if not f.get("opened_at"):
                f["opened_at"] = time.time()
            await self.broadcast(
                {"type": "answer_floor", "room_id": room_id, "floor": self.floor_state(room_id)}
            )
            return True, self.floor_state(room_id)
        if agent_id not in f["queue"]:
            f["queue"].append(agent_id)
            await self.broadcast(
                {"type": "answer_floor", "room_id": room_id, "floor": self.floor_state(room_id)}
            )
        return False, self.floor_state(room_id)

    async def release_floor(self, room_id: str, agent_id: str) -> dict[str, Any]:
        """释放应答位：holder 释放→提升队首；排队者释放→退出队列。"""
        f = self._floor(room_id)
        changed = False
        if f["holder"] == agent_id:
            nxt = f["queue"].pop(0) if f["queue"] else None
            f["holder"] = nxt
            f["claimed_at"] = time.time() if nxt else 0.0
            if nxt is None and not f["queue"]:
                f["opened_at"] = 0.0  # 关闭本轮
                f["round_msg"] = None
            changed = True
        elif agent_id in f["queue"]:
            f["queue"].remove(agent_id)
            changed = True
        if changed:
            await self.broadcast(
                {"type": "answer_floor", "room_id": room_id, "floor": self.floor_state(room_id)}
            )
        return self.floor_state(room_id)

    def floor_blocks(self, room_id: str, agent_id: str) -> bool:
        """hard 模式下：该 agent 此刻是否应被禁止在本房间回答（本轮进行中且它不是 holder）。"""
        if self.floor_scope == "off" or self.floor_enforcement != "hard":
            return False
        if not self.round_active(room_id):
            return False
        return self._floor(room_id)["holder"] != agent_id

    async def reconcile_floors(self) -> None:
        """TTL 兜底：holder 超时未释放→放行队首；空闲轮过期→关闭。"""
        now = time.time()
        for room_id, f in list(self._floors.items()):
            if f["holder"] and f["claimed_at"] and now - f["claimed_at"] > ANSWER_FLOOR_TTL:
                await self.release_floor(room_id, f["holder"])
            elif (not f["holder"] and not f["queue"]
                  and f.get("opened_at") and now - f["opened_at"] > ANSWER_ROUND_OPEN_TTL):
                f["opened_at"] = 0.0
                f["round_msg"] = None

    def _drop_from_floors(self, agent_id: str) -> None:
        """同步清理：某 agent 被删/踢/下线时，从所有应答位与队列里移除（不广播）。"""
        for f in self._floors.values():
            if f["holder"] == agent_id:
                f["holder"] = f["queue"].pop(0) if f["queue"] else None
                f["claimed_at"] = time.time() if f["holder"] else 0.0
            elif agent_id in f["queue"]:
                f["queue"].remove(agent_id)

    # ----- 快照 -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "agents": [a.model_dump() for a in self.store.agents.values()],
            "rooms": [r.model_dump() for r in self.store.rooms.values()],
            "messages": {
                room.id: [m.model_dump() for m in self.store.recent(room.id)]
                for room in self.store.rooms.values()
            },
            "server": {
                "provider": self.settings.resolved_provider(),
                "default_model": self.settings.default_model,
                "has_api_key": bool(self.settings.anthropic_api_key),
                "floor_scope": self.floor_scope,
                "floor_enforcement": self.floor_enforcement,
            },
            "floors": {
                room.id: self.floor_state(room.id) for room in self.store.rooms.values()
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
        self.store.upsert_agent(agent)
        await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})
        return agent

    async def delete_agent(self, agent_id: str) -> None:
        # 先记下受影响的房间，删除后再广播 room_updated，让 GUI 的成员计数即时刷新。
        affected = [r for r in self.store.rooms.values() if agent_id in r.agent_ids]
        self.store.remove_agent(agent_id)
        self._kicked.discard(agent_id)  # 否则被回收/重建的同名 id 可能「出生即被踢」
        self._drop_from_floors(agent_id)
        await self.broadcast({"type": "agent_removed", "agent_id": agent_id})
        for room in affected:
            await self.broadcast({"type": "room_updated", "room": room.model_dump()})

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
        if agent_id in self._kicked:
            return  # 已被踢掉：忽略其心跳/活动，不让它复活在线
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
            self._drop_from_floors(agent_id)  # 离线的 holder 不应卡住应答队列
            await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})

    async def reconcile_peers(self) -> None:
        """把长时间无活动的 peer 标记为离线（由后台任务周期性调用）。"""
        now = time.time()
        for agent in list(self.store.agents.values()):
            if agent.online and now - agent.last_seen > PEER_STALE_SECONDS:
                agent.online = False
                await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})

    def is_kicked(self, agent_id: str) -> bool:
        return agent_id in self._kicked

    def clear_kick(self, agent_id: str) -> None:
        """实例主动重新注册（connect/join/standby）时调用：撤销「踢掉」，允许其归队。"""
        self._kicked.discard(agent_id)

    async def kick_peer(self, agent_id: str) -> bool:
        """强制把一个 peer 踢下线：立即标记离线并记入黑名单，使其后续心跳/活动不再复活它；
        服务端会在心跳 / wait 的响应里通知其桥接停止。实例重新注册即可归队。"""
        agent = self.store.get_agent(agent_id)
        if not agent:
            return False
        self._kicked.add(agent_id)
        agent.online = False
        agent.last_seen = 0.0
        self._drop_from_floors(agent_id)
        await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})
        return True

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
        self.store.upsert_room(room)
        await self.broadcast({"type": "room_updated", "room": room.model_dump()})
        return room

    async def delete_room(self, room_id: str) -> bool:
        """删除一个主题（含其全部消息）。允许删除任何主题，包括默认的「大厅」——
        缺了它，下次有人 standby 到「大厅」会自动重建。返回是否确有该主题被删除。"""
        if not self.store.get_room(room_id):
            return False
        self.store.remove_room(room_id)
        self._invite_locks.pop(room_id, None)
        self._floors.pop(room_id, None)
        await self.broadcast({"type": "room_removed", "room_id": room_id})
        return True

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
        # 按 scope 开启一轮应答；发送者若正持有本房间应答位，这条是它的「答复」而非新问题，不触发。
        if message.role != "system" and self.message_opens_round(message):
            holder = (self._floors.get(message.room_id) or {}).get("holder")
            if message.sender_id != holder:
                await self.open_round(message.room_id, message)
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
