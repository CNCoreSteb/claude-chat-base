"""FastAPI 应用：REST 控制面 + WebSocket 事件流 + 静态 GUI。

GUI 通过 REST 接口改变状态；Hub 再把由此产生的事件经 ``/ws`` 广播出去，从而让每个
已连接的浏览器（包括发起操作的那个）实时更新。MCP peer 桥接使用的也是这同一套
REST 接口。
"""

from __future__ import annotations

import logging
import re
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
        hub.load_presets()
        app.state.hub = hub
        log.info(
            "Claude Chat Base 就绪 —— provider=%s model=%s",
            settings.resolved_provider(),
            settings.default_model,
        )
        try:
            yield
        finally:
            await hub.orchestrator.shutdown()

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

    # ----- peer（MCP 桥接） -------------------------------------------------

    @app.post("/api/peers")
    async def register_peer(body: dict) -> dict:
        """把一个外部 Claude Code 实例注册为 peer 参与者。"""
        room_id = body.get("room_id")
        room = hub().store.get_room(room_id) if room_id else None
        if not room:
            raise HTTPException(404, "房间不存在")
        from .models import AGENT_COLORS

        agent = Agent(
            name=body.get("name") or "Peer",
            persona=body.get("persona") or "一个外部的 Claude Code peer。",
            kind=AgentKind.PEER,
            color=AGENT_COLORS[len(hub().store.agents) % len(AGENT_COLORS)],
        )
        hub().store.add_agent(agent)
        if agent.id not in room.agent_ids:
            room.agent_ids.append(agent.id)
        await hub().broadcast({"type": "agent_added", "agent": agent.model_dump()})
        await hub().update_room(room_id, RoomUpdate(agent_ids=room.agent_ids))
        return {"agent_id": agent.id, "room_id": room_id, "color": agent.color}

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
