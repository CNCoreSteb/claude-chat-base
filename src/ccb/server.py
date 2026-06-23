"""FastAPI 应用：REST 控制面 + WebSocket 事件流 + 静态 GUI。

GUI 通过 REST 接口改变状态；Hub 再把由此产生的事件经 ``/ws`` 广播出去，从而让每个
已连接的浏览器（包括发起操作的那个）实时更新。MCP peer 桥接使用的也是这同一套
REST 接口。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .hub import Hub
from .models import (
    Agent,
    AgentCreate,
    AgentKind,
    AgentUpdate,
    Message,
    RoomCreate,
    RoomUpdate,
)
from .orchestrator import Orchestrator

log = logging.getLogger("ccb.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Python 的 \w 默认是 Unicode 感知的，已能匹配中文等字符。
MENTION_RE = re.compile(r"@([\w-]+)")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub = Hub(settings)
        hub.orchestrator = Orchestrator(hub)
        hub.bootstrap()
        app.state.hub = hub

        async def _reconcile_loop() -> None:
            # 周期性地把长时间无活动的 peer 标记为离线。
            while True:
                await asyncio.sleep(10)
                await hub.reconcile_peers()

        reconcile_task = asyncio.create_task(_reconcile_loop())
        log.info(
            "Claude Chat Base 就绪 —— provider=%s model=%s",
            settings.resolved_provider(),
            settings.default_model,
        )
        try:
            yield
        finally:
            reconcile_task.cancel()
            await hub.orchestrator.shutdown()
            hub.store.close()

    app = FastAPI(title="Claude Chat Base", version="0.1.0", lifespan=lifespan)

    def hub() -> Hub:
        return app.state.hub

    # ----- 状态 --------------------------------------------------------------

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        return JSONResponse(hub().snapshot())

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    # ----- 智能体 -------------------------------------------------------------

    @app.post("/api/agents")
    async def create_agent(data: AgentCreate) -> dict:
        agent = await hub().create_agent(data)
        return agent.model_dump()

    @app.patch("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, data: AgentUpdate) -> dict:
        agent = await hub().update_agent(agent_id, data)
        if not agent:
            raise HTTPException(404, "智能体不存在")
        return agent.model_dump()

    @app.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str) -> dict:
        await hub().delete_agent(agent_id)
        return {"ok": True}

    # ----- 房间 --------------------------------------------------------------

    @app.post("/api/rooms")
    async def create_room(data: RoomCreate) -> dict:
        room = await hub().create_room(data)
        return room.model_dump()

    @app.patch("/api/rooms/{room_id}")
    async def update_room(room_id: str, data: RoomUpdate) -> dict:
        room = await hub().update_room(room_id, data)
        if not room:
            raise HTTPException(404, "房间不存在")
        return room.model_dump()

    @app.post("/api/rooms/{room_id}/agents/{agent_id}")
    async def add_room_agent(room_id: str, agent_id: str) -> dict:
        room = hub().store.get_room(room_id)
        if not room:
            raise HTTPException(404, "房间不存在")
        if agent_id not in room.agent_ids and hub().store.get_agent(agent_id):
            room.agent_ids.append(agent_id)
        await hub().update_room(room_id, RoomUpdate(agent_ids=room.agent_ids))
        return room.model_dump()

    @app.delete("/api/rooms/{room_id}/agents/{agent_id}")
    async def remove_room_agent(room_id: str, agent_id: str) -> dict:
        room = hub().store.get_room(room_id)
        if not room:
            raise HTTPException(404, "房间不存在")
        if agent_id in room.agent_ids:
            room.agent_ids.remove(agent_id)
        await hub().update_room(room_id, RoomUpdate(agent_ids=room.agent_ids))
        return room.model_dump()

    # ----- 房间控制 ------------------------------------------------------

    @app.post("/api/rooms/{room_id}/start")
    async def start_room(room_id: str) -> dict:
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        await hub().orchestrator.start(room_id)
        return {"ok": True}

    @app.post("/api/rooms/{room_id}/pause")
    async def pause_room(room_id: str) -> dict:
        await hub().orchestrator.pause(room_id)
        return {"ok": True}

    @app.post("/api/rooms/{room_id}/stop")
    async def stop_room(room_id: str) -> dict:
        await hub().orchestrator.stop(room_id)
        return {"ok": True}

    @app.post("/api/rooms/{room_id}/reset")
    async def reset_room(room_id: str) -> dict:
        await hub().orchestrator.stop(room_id)
        await hub().reset_room(room_id)
        return {"ok": True}

    # ----- 消息 -----------------------------------------------------------

    @app.post("/api/rooms/{room_id}/messages")
    async def post_message(room_id: str, body: dict) -> dict:
        room = hub().store.get_room(room_id)
        if not room:
            raise HTTPException(404, "房间不存在")
        content = (body.get("content") or "").strip()
        if not content:
            raise HTTPException(400, "内容不能为空")

        agent_id = body.get("agent_id")
        if agent_id:
            agent = hub().store.get_agent(agent_id)
            if not agent:
                raise HTTPException(404, "智能体不存在")
            msg = Message(
                room_id=room_id,
                sender_id=agent.id,
                sender_name=agent.name,
                role="agent",
                color=agent.color,
                content=content,
            )
            if agent.kind == AgentKind.PEER:
                await hub().mark_peer_seen(agent.id)
        else:
            msg = Message(
                room_id=room_id,
                sender_id="human",
                sender_name=body.get("sender_name") or "你",
                role="human",
                color="#94a3b8",
                content=content,
            )
            _apply_mentions(hub(), room_id, content)

        await hub().post_message(msg)
        return msg.model_dump()

    @app.get("/api/rooms/{room_id}/messages")
    async def list_messages(room_id: str, since: float = 0.0) -> list[dict]:
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        return [
            m.model_dump() for m in hub().store.history(room_id) if m.ts > since
        ]

    @app.get("/api/rooms/{room_id}/wait")
    async def wait_messages(
        room_id: str, since: float = 0.0, timeout: float = 25.0, agent_id: str = ""
    ) -> list[dict]:
        """长轮询：等待 since 之后出现的新消息，最多等 timeout 秒；用于 peer 高效跟进。"""
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        if agent_id:
            await hub().mark_peer_seen(agent_id)
        deadline = time.monotonic() + min(max(timeout, 0.0), 50.0)
        while True:
            fresh = [m.model_dump() for m in hub().store.history(room_id) if m.ts > since]
            if fresh or time.monotonic() >= deadline:
                return fresh
            await asyncio.sleep(0.4)

    # ----- peer（MCP 桥接） -------------------------------------------------

    @app.post("/api/peers")
    async def register_peer(body: dict) -> dict:
        """让一个外部 Claude Code 实例以 peer 身份加入房间。

        若房间里已存在同名的 peer 槽位（通常是在 GUI 里预先配置的仓库），就认领它并
        标记为在线；否则即时新建一个 peer 参与者。
        """
        room_id = body.get("room_id")
        room = hub().store.get_room(room_id) if room_id else None
        if not room:
            raise HTTPException(404, "房间不存在")
        name = body.get("name") or "Peer"

        # 尝试认领已有的同名 peer 槽位。
        existing = next(
            (
                hub().store.get_agent(aid)
                for aid in room.agent_ids
                if (a := hub().store.get_agent(aid)) and a.kind == AgentKind.PEER and a.name == name
            ),
            None,
        )
        if existing:
            patch = AgentUpdate(
                persona=body.get("persona") or None,
                role=body.get("role") or None,
                repo_path=body.get("repo_path") or None,
            )
            await hub().update_agent(existing.id, patch)
            await hub().mark_peer_seen(existing.id)
            return {"agent_id": existing.id, "room_id": room_id, "color": existing.color,
                    "claimed": True}

        from .models import AGENT_COLORS

        agent = Agent(
            name=name,
            persona=body.get("persona") or "一个外部的 Claude Code peer。",
            role=body.get("role") or "",
            repo_path=body.get("repo_path") or "",
            kind=AgentKind.PEER,
            color=AGENT_COLORS[len(hub().store.agents) % len(AGENT_COLORS)],
        )
        hub().store.add_agent(agent)
        if agent.id not in room.agent_ids:
            room.agent_ids.append(agent.id)
        await hub().mark_peer_seen(agent.id)
        await hub().broadcast({"type": "agent_added", "agent": agent.model_dump()})
        await hub().update_room(room_id, RoomUpdate(agent_ids=room.agent_ids))
        return {"agent_id": agent.id, "room_id": room_id, "color": agent.color, "claimed": False}

    @app.post("/api/peers/{agent_id}/leave")
    async def peer_leave(agent_id: str) -> dict:
        """把一个 peer 标记为离线（其在 GUI 中的槽位保留）。"""
        await hub().set_peer_offline(agent_id)
        return {"ok": True}

    # ----- 实例（跨房间的连接与拉群） -----------------------------------------

    def _instance_dump(agent: Agent) -> dict:
        d = agent.model_dump()
        d["rooms"] = [
            {"id": r.id, "name": r.name}
            for r in hub().store.rooms_for_agent(agent.id)
        ]
        return d

    @app.get("/api/instances")
    async def list_instances(online: bool = False) -> list[dict]:
        """列出所有 peer 实例（即各仓库的 Claude Code）。online=true 时只列在线的。"""
        out = []
        for a in hub().store.agents.values():
            if a.kind != AgentKind.PEER:
                continue
            if online and not a.online:
                continue
            out.append(_instance_dump(a))
        return out

    @app.post("/api/instances/connect")
    async def connect_instance(body: dict) -> dict:
        """让一个 Claude Code 实例全局上线（不必先加入任何房间）。

        若已存在同名 peer 则认领它（标记在线并补充角色/路径），否则新建。
        """
        name = (body.get("name") or "").strip() or "Peer"
        existing = next(
            (a for a in hub().store.agents.values()
             if a.kind == AgentKind.PEER and a.name == name),
            None,
        )
        if existing:
            await hub().update_agent(
                existing.id,
                AgentUpdate(
                    persona=body.get("persona") or None,
                    role=body.get("role") or None,
                    repo_path=body.get("repo_path") or None,
                ),
            )
            await hub().mark_peer_seen(existing.id)
            return {"agent_id": existing.id, "claimed": True}

        from .models import AGENT_COLORS

        agent = Agent(
            name=name,
            persona=body.get("persona") or "一个外部的 Claude Code 实例。",
            role=body.get("role") or "",
            repo_path=body.get("repo_path") or "",
            kind=AgentKind.PEER,
            color=AGENT_COLORS[len(hub().store.agents) % len(AGENT_COLORS)],
        )
        hub().store.add_agent(agent)
        await hub().mark_peer_seen(agent.id)
        await hub().broadcast({"type": "agent_added", "agent": agent.model_dump()})
        return {"agent_id": agent.id, "claimed": False}

    @app.get("/api/instances/{agent_id}/messages")
    async def instance_messages(agent_id: str, since: float = 0.0) -> list[dict]:
        """读取该实例所在的全部房间中、since 之后的新消息（立即返回）。"""
        return _instance_new_messages(hub(), agent_id, since)

    @app.get("/api/instances/{agent_id}/wait")
    async def instance_wait(
        agent_id: str, since: float = 0.0, timeout: float = 25.0
    ) -> list[dict]:
        """长轮询：等待该实例所在任一房间出现新消息（跨房间，IM 式跟进）。"""
        await hub().mark_peer_seen(agent_id)
        deadline = time.monotonic() + min(max(timeout, 0.0), 50.0)
        while True:
            fresh = _instance_new_messages(hub(), agent_id, since)
            if fresh or time.monotonic() >= deadline:
                return fresh
            await asyncio.sleep(0.4)

    @app.post("/api/rooms/{room_id}/invite")
    async def invite_to_room(room_id: str, body: dict) -> dict:
        """把另一个已连接的实例按"职责(role)或名字"拉进本房间。

        body: {target: 角色或名字或 agent_id, by?: 邀请者 agent_id}
        """
        room = hub().store.get_room(room_id)
        if not room:
            raise HTTPException(404, "房间不存在")
        target = (body.get("target") or "").strip()
        if not target:
            raise HTTPException(400, "target 不能为空")

        match = _find_instance(hub(), target)
        if not match:
            raise HTTPException(404, f"未找到匹配「{target}」的已连接实例")

        if match.id not in room.agent_ids:
            room.agent_ids.append(match.id)
            await hub().update_room(room_id, RoomUpdate(agent_ids=room.agent_ids))

        inviter = hub().store.get_agent(body.get("by") or "")
        inviter_name = inviter.name if inviter else "某实例"
        tag = f"（{match.role}）" if match.role else ""
        await hub().post_message(
            Message(
                room_id=room_id,
                sender_id="system",
                sender_name="system",
                role="system",
                content=f"{inviter_name} 把 {match.name}{tag} 拉进了本房间。",
            )
        )
        return {"agent_id": match.id, "room_id": room_id, "name": match.name}

    # ----- WebSocket ----------------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        h = hub()
        queue = h.subscribe()
        try:
            await websocket.send_json(h.snapshot())
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            h.unsubscribe(queue)

    # ----- 静态 GUI（最后挂载，保证 API 路由优先匹配） ------------------------

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


def _instance_new_messages(hub: Hub, agent_id: str, since: float) -> list[dict]:
    """该实例所在全部房间中、since 之后的新消息，附带房间名以便区分。"""
    rooms = hub.store.rooms_for_agent(agent_id)
    room_names = {r.id: r.name for r in rooms}
    msgs = hub.store.messages_for_rooms_since([r.id for r in rooms], since)
    out = []
    for m in msgs:
        d = m.model_dump()
        d["room_name"] = room_names.get(m.room_id, "")
        out.append(d)
    return out


def _find_instance(hub: Hub, target: str) -> Agent | None:
    """按 agent_id / 职责(role) / 名字 查找一个 peer 实例，优先在线的。"""
    by_id = hub.store.get_agent(target)
    if by_id and by_id.kind == AgentKind.PEER:
        return by_id
    t = target.lower()
    peers = [a for a in hub.store.agents.values() if a.kind == AgentKind.PEER]
    candidates = [a for a in peers if a.role.lower() == t or a.name.lower() == t]
    if not candidates:
        return None
    # 优先返回在线的实例。
    return next((a for a in candidates if a.online), candidates[0])


def _apply_mentions(hub: Hub, room_id: str, content: str) -> None:
    room = hub.store.get_room(room_id)
    if not room:
        return
    mentioned = {m.lower() for m in MENTION_RE.findall(content)}
    if not mentioned:
        return
    for aid in room.agent_ids:
        agent = hub.store.get_agent(aid)
        if agent and agent.name.lower() in mentioned and agent.kind == AgentKind.AI:
            hub.orchestrator.hint_next(room_id, agent.id)
            return
