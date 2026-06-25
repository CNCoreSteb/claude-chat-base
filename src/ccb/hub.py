"""Hub：唯一的状态真相来源 + 事件广播器。

Hub 持有 :class:`Store`，并把状态变更事件推送给所有已连接的 WebSocket 客户端。所有
GUI 关心的状态变更都经由某个 ``*`` 辅助方法发出，从而让界面保持实时同步。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import Settings, load_preset
from .models import (
    Agent,
    AgentCreate,
    AgentUpdate,
    Message,
    Room,
    RoomCreate,
    RoomUpdate,
)
from .store import HISTORY_LIMIT, RECENT_LIMIT, Store

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
        # reconcile 循环存活度（供调试页判断离线/应答位 TTL 判定是否还在跑）。
        self.last_reconcile_at: float | None = None
        self.reconcile_count: int = 0
        self.last_reconcile_error: str | None = None

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
        agents, rooms = load_preset(self.settings.preset)
        for agent in agents:
            self.store.add_agent(agent)
        for room in rooms:
            self.store.add_room(room)
        log.info("已从预设导入 %d 个智能体、%d 个房间", len(agents), len(rooms))

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
        # 已指定主要接收者(meta.to)的「定向提问」只该由那一个回答，没有一拥而上的风险——不开应答轮
        # （否则你 @ 某个具体的人也会弹出"待应答"）。应答编排只针对**未指定接收者的广播提问**。
        if (message.meta or {}).get("to"):
            return False
        if message.role == "human":
            return True  # 人类(GUI 用户)的广播提问
        if self.floor_scope == "broadcast" and message.role == "agent":
            # 仅「广播问题」触发（与配置语义一致）：普通 agent 广播（状态同步等）不应重置一轮
            # 正在进行的应答，否则别人随口一句就把在答的那轮冲掉。
            return bool((message.meta or {}).get("is_question"))
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
            # 不凭空开一轮：必须已有进行中的提问轮（某条广播提问已 open_round）才允许抢。否则
            # 一个 agent 误调 claim 会把自己设成 holder、在 hard 模式下无故挡住所有人发言。
            if not self.round_active(room_id):
                return False, self.floor_state(room_id)
            f["holder"] = agent_id
            f["claimed_at"] = time.time()
            if agent_id in f["queue"]:
                f["queue"].remove(agent_id)
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
                # 必须广播，否则 GUI 收不到"本轮已关闭"，横幅会一直卡在"待应答"。
                await self.broadcast(
                    {"type": "answer_floor", "room_id": room_id, "floor": self.floor_state(room_id)}
                )

    def _drop_from_floors(self, agent_id: str) -> list[str]:
        """某 agent 被删/踢/下线时，从所有应答位与队列里移除。返回状态有变化的房间 id
        （供调用方广播 answer_floor，让 GUI 与被提升者即时同步、不必等下一拍轮询/兜底）。"""
        changed: list[str] = []
        for rid, f in self._floors.items():
            if f["holder"] == agent_id:
                f["holder"] = f["queue"].pop(0) if f["queue"] else None
                f["claimed_at"] = time.time() if f["holder"] else 0.0
                if f["holder"] is None:
                    # 没人接替：关闭这一轮，否则 round_active 仍为真而在 hard 模式下卡住所有人。
                    f["opened_at"] = 0.0
                    f["round_msg"] = None
                changed.append(rid)
            elif agent_id in f["queue"]:
                f["queue"].remove(agent_id)
                changed.append(rid)
        return changed

    async def _broadcast_floors(self, room_ids: list[str]) -> None:
        for rid in room_ids:
            await self.broadcast(
                {"type": "answer_floor", "room_id": rid, "floor": self.floor_state(rid)}
            )

    # ----- 调试快照（供独立端口调试页；只读，把全部可观测状态摊平成一个 JSON）----------

    def _floor_debug(self, room_id: str, now: float) -> dict[str, Any]:
        st = self.floor_state(room_id)
        f = self._floors.get(room_id) or {}
        opened = f.get("opened_at") or None
        claimed = f.get("claimed_at") or None
        st["opened_at"] = opened
        st["claimed_at"] = claimed
        st["floor_age"] = (now - claimed) if claimed else None
        st["seconds_to_floor_release"] = (
            max(0.0, ANSWER_FLOOR_TTL - (now - claimed)) if (st["holder"] and claimed) else None
        )
        st["round_age"] = (now - opened) if opened else None
        st["seconds_to_round_close"] = (
            max(0.0, ANSWER_ROUND_OPEN_TTL - (now - opened))
            if (opened and not st["holder"] and not st["queue"]) else None
        )
        st["blocked_agents"] = (
            [a.id for a in self.store.agents.values() if self.floor_blocks(room_id, a.id)]
            if (self.floor_enforcement == "hard" and st["active"]) else []
        )
        return st

    def debug_state(self, now: float, started_at: float | None = None,
                    main_should_exit: bool = False,
                    debug_should_exit: bool = False) -> dict[str, Any]:
        s = self.settings
        holds: dict[str, list[str]] = {}
        queued: dict[str, list[str]] = {}
        for rid, f in self._floors.items():
            if f.get("holder"):
                holds.setdefault(f["holder"], []).append(rid)
            for q in f.get("queue", []):
                queued.setdefault(q, []).append(rid)

        db = self.store.db_stats()
        per_room = {r["room_id"]: r for r in db["per_room_counts"]}

        agents = []
        for a in self.store.agents.values():
            d = a.model_dump()
            seen = a.last_seen or 0.0
            d["staleness"] = (now - seen) if seen else None
            d["stale"] = ((now - seen) > PEER_STALE_SECONDS) if seen else None
            d["seconds_to_offline"] = (
                max(0.0, PEER_STALE_SECONDS - (now - seen)) if (a.online and seen) else None
            )
            d["kicked"] = a.id in self._kicked
            d["rooms"] = [{"id": r.id, "name": r.name} for r in self.store.rooms_for_agent(a.id)]
            d["holds_floor_in"] = holds.get(a.id, [])
            d["queued_in"] = queued.get(a.id, [])
            agents.append(d)

        rooms = []
        for r in self.store.rooms.values():
            d = r.model_dump()
            d["agent_ids_resolved"] = [
                {"id": aid, "name": (self.store.get_agent(aid).name
                                     if self.store.get_agent(aid) else None)}
                for aid in r.agent_ids
            ]
            rc = per_room.get(r.id)
            d["message_count"] = rc["count"] if rc else 0
            d["first_ts"] = rc["min_ts"] if rc else None
            d["last_ts"] = rc["max_ts"] if rc else None
            d["floor"] = self._floor_debug(r.id, now)
            rooms.append(d)

        return {
            "now": now,
            "server": {
                "started_at": started_at,
                "uptime_seconds": (now - started_at) if started_at else None,
                "host": s.host, "port": s.port, "debug_port": s.debug_port,
                "data_dir": str(self.store.data_dir), "preset": str(s.preset),
                "open_browser": s.open_browser,
                "floor_scope": self.floor_scope, "floor_enforcement": self.floor_enforcement,
                "floor_scope_initial": s.floor_scope,
                "floor_enforcement_initial": s.floor_enforcement,
                "main_should_exit": main_should_exit, "debug_should_exit": debug_should_exit,
            },
            "constants": {
                "PEER_STALE_SECONDS": PEER_STALE_SECONDS,
                "ANSWER_FLOOR_TTL": ANSWER_FLOOR_TTL,
                "ANSWER_ROUND_OPEN_TTL": ANSWER_ROUND_OPEN_TTL,
                "RECENT_LIMIT": RECENT_LIMIT, "HISTORY_LIMIT": HISTORY_LIMIT,
            },
            "reconcile": {
                "last_run_at": self.last_reconcile_at,
                "seconds_since_last_run": (
                    (now - self.last_reconcile_at) if self.last_reconcile_at else None),
                "tick_count": self.reconcile_count,
                "last_error": self.last_reconcile_error,
            },
            "websocket": {
                "subscriber_count": len(self._subscribers),
                "queues": [{"qsize": q.qsize(), "maxsize": q.maxsize}
                           for q in self._subscribers],
            },
            "kicked": [{"id": k, "name": (self.store.get_agent(k).name
                                          if self.store.get_agent(k) else None)}
                       for k in self._kicked],
            "invite_locks": [{"room_id": rid, "locked": lock.locked()}
                             for rid, lock in self._invite_locks.items()],
            "agents": agents,
            "rooms": rooms,
            "db": {
                **db, "db_path": str(self.store.db_path),
                "file_sizes": self.store.file_sizes(),
                "pragma": self.store.pragma_stats(),
                "lock_locked": self.store.lock_locked(),
                "cursor_vs_max_delta": (
                    (self.store.last_ts - db["db_max_ts"])
                    if db["db_max_ts"] is not None else None),
                "cursor_vs_wallclock": self.store.last_ts - now,
            },
            "messages_tail": [m.model_dump() for m in self.store.messages_tail(50)],
        }

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
        await self._broadcast_floors(self._drop_from_floors(agent_id))
        await self.broadcast({"type": "agent_removed", "agent_id": agent_id})
        for room in affected:
            await self.broadcast({"type": "room_updated", "room": room.model_dump()})

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
            # 离线的 holder 不应卡住应答队列：移除并广播交接，让 GUI/被提升者即时同步。
            await self._broadcast_floors(self._drop_from_floors(agent_id))
            await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})

    async def reconcile_peers(self) -> None:
        """把长时间无活动的 peer 标记为离线（由后台任务周期性调用）。"""
        now = time.time()
        for agent in list(self.store.agents.values()):
            if agent.online and now - agent.last_seen > PEER_STALE_SECONDS:
                agent.online = False
                await self._broadcast_floors(self._drop_from_floors(agent.id))
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
        await self._broadcast_floors(self._drop_from_floors(agent_id))
        await self.broadcast({"type": "agent_updated", "agent": agent.model_dump()})
        return True

    # ----- 房间操作 ----------------------------------------------------------

    async def create_room(self, data: RoomCreate) -> Room:
        room = Room(**data.model_dump())
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
        """清空某主题的全部消息（保留主题与成员）。"""
        room = self.store.get_room(room_id)
        if not room:
            return
        self.store.clear_messages(room_id)
        self._floors.pop(room_id, None)  # 清空也重置该房间的应答位
        await self.broadcast({"type": "room_reset", "room_id": room_id})
