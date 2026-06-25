"""独立端口的调试 / 可观测页应用（与主服务共享同一个活的 Hub）。

由主应用的 lifespan 在 ``CCB_DEBUG_PORT>0`` 时作为子任务启动（强制绑 127.0.0.1）。它本身
不创建/不销毁任何状态（Hub、Store、reconcile 循环都归主应用所有），只读地把整个 CCB + 各
agent 的可观测状态摊平给调试页：

* ``GET /``            —— 自包含调试页（纯前端，免构建）。
* ``GET /debug/state`` —— 一次性全量状态 JSON（见 :meth:`Hub.debug_state`）。
* ``GET /debug/ws``    —— 实时事件流：订阅 Hub 事件总线、原样转发（与 GUI 同一条总线）。
* ``POST /debug/actions/*`` —— 少量明确标注的安全调试动作（复用现有 Hub 方法，无破坏性清库）。

安全：无鉴权，含内部状态/DB 信息，故仅绑 127.0.0.1、默认关闭、绝不回显原始 API key。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, JSONResponse

from .hub import Hub

DEBUG_HTML = Path(__file__).resolve().parent / "static" / "debug.html"


def create_debug_app(hub: Hub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.hub = hub  # 不拥有任何东西：不 bootstrap、不 reconcile、不 close store
        yield

    app = FastAPI(title="CCB Debug", version="0.1.0", lifespan=lifespan)

    def _should_exit() -> bool:
        srv = getattr(app.state, "uvicorn_server", None)
        return bool(srv is not None and srv.should_exit)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(DEBUG_HTML))

    @app.get("/debug/state")
    async def debug_state() -> JSONResponse:
        now = time.time()
        main_app = getattr(app.state, "main_app", None)
        started_at = getattr(main_app.state, "started_at", None) if main_app else None
        main_srv = getattr(main_app.state, "uvicorn_server", None) if main_app else None
        dbg_srv = getattr(app.state, "uvicorn_server", None)
        return JSONResponse(hub.debug_state(
            now,
            started_at=started_at,
            main_should_exit=bool(main_srv is not None and main_srv.should_exit),
            debug_should_exit=bool(dbg_srv is not None and dbg_srv.should_exit),
        ))

    @app.get("/debug/health")
    async def health() -> dict:
        return {"ok": True}

    @app.websocket("/debug/ws")
    async def debug_ws(websocket: WebSocket) -> None:
        """实时事件流：订阅与 GUI 同一条 Hub 事件总线，原样转发每个事件。

        两条并行任务：``_pump`` 推事件、``_watch`` 读侧探测断开。任一结束即收尾——这样即便
        总线安静、客户端悄悄断开，也能立刻 unsubscribe，不会泄漏订阅、虚增 subscriber_count。
        """
        await websocket.accept()
        queue = hub.subscribe()

        async def _pump() -> None:
            await websocket.send_json({"type": "_debug_hello", "now": time.time()})
            while not _should_exit():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if event is None:  # 背压丢弃哨兵
                    return
                await websocket.send_json(event)

        async def _watch() -> None:
            # 客户端断开时 receive() 抛 WebSocketDisconnect；用它及时结束，不必等下一个事件。
            try:
                while True:
                    await websocket.receive()
            except Exception:  # noqa: BLE001
                pass

        pump = asyncio.create_task(_pump())
        watch = asyncio.create_task(_watch())
        try:
            await asyncio.wait({pump, watch}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (pump, watch):
                t.cancel()
            await asyncio.gather(pump, watch, return_exceptions=True)
            hub.unsubscribe(queue)  # 关键：否则会泄漏订阅、虚增 subscriber_count

    # ----- 安全调试动作（复用现有 Hub 方法；无破坏性清库/删主题）----------------------

    @app.post("/debug/actions/kick")
    async def act_kick(body: dict) -> dict:
        ok = await hub.kick_peer((body.get("agent_id") or "").strip())
        return {"ok": ok}

    @app.post("/debug/actions/clear-kick")
    async def act_clear_kick(body: dict) -> dict:
        hub.clear_kick((body.get("agent_id") or "").strip())
        return {"ok": True}

    @app.post("/debug/actions/set-online")
    async def act_set_online(body: dict) -> dict:
        """强制把某实例标记为在线（复用心跳路径）。被 kick 的需先 clear-kick 才会复活。"""
        aid = (body.get("agent_id") or "").strip()
        agent = hub.store.get_agent(aid)
        if not agent:
            raise HTTPException(404, "实例不存在")
        await hub.mark_peer_seen(aid)
        return {"ok": True, "online": agent.online}

    @app.post("/debug/actions/set-offline")
    async def act_set_offline(body: dict) -> dict:
        """强制把某实例标记为离线（会一并移出应答位并广播交接），便于测试离线/接管逻辑。"""
        aid = (body.get("agent_id") or "").strip()
        agent = hub.store.get_agent(aid)
        if not agent:
            raise HTTPException(404, "实例不存在")
        await hub.set_peer_offline(aid)
        return {"ok": True, "online": agent.online}

    @app.post("/debug/actions/reconcile")
    async def act_reconcile() -> dict:
        """立即跑一次 reconcile（离线判定 + 应答位 TTL），免去等后台循环的 10~45s。"""
        await hub.reconcile_peers()
        await hub.reconcile_floors()
        hub.last_reconcile_at = time.time()
        hub.reconcile_count += 1
        return {"ok": True, "tick_count": hub.reconcile_count}

    @app.post("/debug/actions/force-release-floor")
    async def act_force_release(body: dict) -> dict:
        room_id = (body.get("room_id") or "").strip()
        if not hub.store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        holder = hub.floor_state(room_id).get("holder")
        if holder:
            await hub.release_floor(room_id, holder)
        return {"ok": True, "floor": hub.floor_state(room_id)}

    @app.post("/debug/actions/set-floor-config")
    async def act_set_floor(body: dict) -> dict:
        scope, enf = body.get("scope"), body.get("enforcement")
        if scope is not None and scope not in ("off", "human", "broadcast"):
            raise HTTPException(400, f"无效 scope：{scope}")
        if enf is not None and enf not in ("soft", "hard"):
            raise HTTPException(400, f"无效 enforcement：{enf}")
        hub.set_floor_config(scope=scope, enforcement=enf)
        await hub.broadcast(
            {"type": "floor_config", "scope": hub.floor_scope,
             "enforcement": hub.floor_enforcement}
        )
        return {"scope": hub.floor_scope, "enforcement": hub.floor_enforcement}

    return app
