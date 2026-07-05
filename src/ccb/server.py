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
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .hub import Hub
from .models import (
    ActionRequest,
    Agent,
    AgentCreate,
    AgentKind,
    AgentUpdate,
    Message,
    RoomCreate,
    RoomUpdate,
)

log = logging.getLogger("ccb.server")


def _install_proactor_noise_filter() -> None:
    """静默 Windows ProactorEventLoop 的已知噪声。

    当客户端（浏览器）突然断开连接时，transport 的 connection-lost 回调会对已失效的
    socket 调用 shutdown() 抛出 ConnectionResetError/OSError，被 asyncio 默认处理器记成
    ERROR。连接此时早已断开，属无害噪声——这里把它过滤掉，其余异常仍交给默认处理器。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    previous = loop.get_exception_handler()

    def handler(loop, context):  # noqa: ANN001
        exc = context.get("exception")
        message = context.get("message", "")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)) or (
            isinstance(exc, OSError) and "_call_connection_lost" in message
        ):
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


STATIC_DIR = Path(__file__).resolve().parent / "static"
# Python 的 \w 默认是 Unicode 感知的，已能匹配中文等字符。
MENTION_RE = re.compile(r"@([\w-]+)")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _install_proactor_noise_filter()
        hub = Hub(settings)
        hub.bootstrap()
        app.state.hub = hub
        app.state.started_at = time.time()

        async def _reconcile_loop() -> None:
            # 周期性地把长时间无活动的 peer 标记为离线。单拍异常不能让整条循环退出，
            # 否则离线判定会永久停摆。
            while True:
                await asyncio.sleep(10)
                try:
                    await hub.reconcile_peers()
                    await hub.reconcile_floors()
                    await hub.pua.tick()
                    hub.last_reconcile_at = time.time()
                    hub.reconcile_count += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 记录后继续，保证离线判定不中断
                    hub.last_reconcile_error = repr(exc)
                    log.exception("reconcile 失败，将在下一拍重试")

        reconcile_task = asyncio.create_task(_reconcile_loop())

        # 可选：独立端口的调试页（CCB_DEBUG_PORT>0 时）。作为本 lifespan 的子任务启动，与主服务
        # 共享同一个活的 Hub；强制绑 127.0.0.1（无鉴权、含内部状态，不外露）；no-op 信号让主服务
        # 独占 Ctrl+C；关停时随主应用收尾。
        debug_server = None
        debug_task = None
        if settings.debug_port and settings.debug_port > 0:
            import uvicorn

            from .debug_app import create_debug_app
            dbg_app = create_debug_app(hub)
            dbg_cfg = uvicorn.Config(
                dbg_app, host="127.0.0.1", port=settings.debug_port,
                log_level="warning", timeout_graceful_shutdown=3,
            )
            debug_server = uvicorn.Server(dbg_cfg)
            debug_server.install_signal_handlers = lambda: None  # 主服务独占信号
            dbg_app.state.uvicorn_server = debug_server
            dbg_app.state.main_app = app
            debug_task = asyncio.create_task(debug_server.serve())
            log.info("CCB 调试页：http://127.0.0.1:%d/", settings.debug_port)

        log.info("Claude Chat Base 就绪")
        try:
            yield
        finally:
            reconcile_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconcile_task
            if debug_server is not None:
                debug_server.should_exit = True
                with suppress(Exception):
                    await asyncio.wait_for(debug_task, timeout=5)
            hub.store.close()

    app = FastAPI(title="Claude Chat Base", version="0.1.0", lifespan=lifespan)

    def hub() -> Hub:
        return app.state.hub

    def should_exit() -> bool:
        """uvicorn 是否已被请求关闭（Ctrl+C 后立即置位）。

        长轮询 / WebSocket 等长驻处理器据此主动收尾，避免 uvicorn 优雅关闭时
        因这些任务迟迟不结束而卡在 "Waiting for background tasks to complete"。
        """
        server = getattr(app.state, "uvicorn_server", None)
        return bool(server is not None and server.should_exit)

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

    @app.delete("/api/rooms/{room_id}")
    async def delete_room(room_id: str, actor: str = "human") -> dict:
        """关闭/删除一个主题（含其全部消息）。人工/主持人可直接关；非主持人 agent 须走
        request_action(close) 由主持人审批。允许关任何主题，包括「大厅」。"""
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        if not hub().is_host(room_id, actor):
            raise HTTPException(403, "你不是本主题主持人，请用 request_action 投递关闭请求")
        await hub().delete_room(room_id)
        return {"ok": True, "room_id": room_id}

    @app.post("/api/rooms/{room_id}/agents/{agent_id}")
    async def add_room_agent(room_id: str, agent_id: str) -> dict:
        room = hub().store.get_room(room_id)
        if not room:
            raise HTTPException(404, "房间不存在")
        if not hub().store.get_agent(agent_id):
            raise HTTPException(404, "智能体不存在")  # 否则会静默返回成功却没真正加入
        if agent_id not in room.agent_ids:
            room.agent_ids.append(agent_id)
        await hub().update_room(room_id, RoomUpdate(agent_ids=room.agent_ids))
        await hub().pua.on_join(room_id, agent_id)
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

    @app.post("/api/rooms/{room_id}/reset")
    async def reset_room(room_id: str) -> dict:
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
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
        reply_to = (body.get("reply_to") or "").strip()

        agent_id = body.get("agent_id")
        if agent_id:
            agent = hub().store.get_agent(agent_id)
            if not agent:
                raise HTTPException(404, "智能体不存在")
            # 应答编排(hard)：本轮进行中且你不是 holder 时，挡回这条回答，提示先 claim 排队。
            if hub().floor_blocks(room_id, agent.id):
                who = hub().floor_state(room_id).get("holder_name") or "其他实例"
                raise HTTPException(
                    409,
                    f"应答编排(hard)：{who} 正在回答本轮问题；请先 claim_answer 取得应答位或排队，"
                    "轮到你再回答（避免一拥而上重复回答）。",
                )
            # PUA 模式硬拦截：不符合当前阶段/轮次的发送挡回（附原因指引）。
            pua_block = hub().pua.blocks(room_id, agent.id, reply_to)
            if pua_block:
                raise HTTPException(409, pua_block)
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

        # 被引用回复的原消息（既用于渲染引用块，也用于「回复某 agent = 定向发给它」的推断）。
        original = hub().store.get_message(reply_to) if reply_to else None
        if original and original.room_id != room_id:
            original = None

        # 接收者优先级：显式 to（agent_id/名字/职责）> 回复对象（被引用消息的发送者，若是本房间
        # 的 agent）。显式/回复指定时以它为准——不再并入正文 @ 文本的模糊匹配（否则名字/职责与
        # @词撞车的其它实例会被误打"被点名"一起回答，如 @server 误命中 role=server 的另一实例）。
        # 两者都没有时才回退到正文 @ 文本解析。meta.to 标出主要发给谁（按 id 规范、随消息持久化）。
        to_id = _resolve_member(hub(), room_id, body.get("to"))
        if not to_id and original:
            to_id = _resolve_member(hub(), room_id, original.sender_id)  # 回复某 agent -> 定向给它
        explicit_ids: list[str] = []
        raw_mentions = body.get("mentions")
        if isinstance(raw_mentions, list):
            for tok in raw_mentions:
                rid = _resolve_member(hub(), room_id, tok)
                if rid and rid not in explicit_ids:
                    explicit_ids.append(rid)
        if to_id or explicit_ids:  # 显式/回复指定 -> 以它为准
            source = ([to_id] if to_id else []) + explicit_ids
        else:                      # 无任何指定 -> 回退正文 @ 文本解析
            source = _resolve_mentions(hub(), room_id, content)
        ordered: list[str] = []
        for rid in source:
            if rid and rid not in ordered:
                ordered.append(rid)
        if ordered:
            msg.meta["mentions"] = ordered
        if to_id:
            msg.meta["to"] = to_id  # 主要接收者（按 id 规范，不依赖文本）
            to_agent = hub().store.get_agent(to_id)
            if to_agent:
                msg.meta["to_name"] = to_agent.name  # 给桥接/GUI 直接显示「主要发给谁」
        if original:
            msg.meta["reply_to"] = original.id
            msg.meta["reply_to_sender"] = original.sender_name
            msg.meta["reply_to_preview"] = _reply_preview(original.content)
        # 待命实例用 ask 发问时标记，GUI 据此高亮"等你回答"。
        if body.get("is_question"):
            msg.meta["is_question"] = True

        await hub().post_message(msg)
        return msg.model_dump()

    @app.get("/api/rooms/{room_id}/messages")
    async def list_messages(room_id: str, since: float = 0.0, limit: int = 0) -> list[dict]:
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        # 默认用无界的 messages_since（而非截到最近 200 条的 history）——否则积压超过 200 条时
        # 会静默丢掉 since 之后较早的消息。limit>0 时只取最近 limit 条（供"按需回看历史"用）。
        if limit and limit > 0:
            msgs = [m for m in hub().store.history(room_id, limit) if m.ts > since]
        else:
            msgs = hub().store.messages_since(room_id, since)
        return [m.model_dump() for m in msgs]

    @app.get("/api/rooms/{room_id}/wait")
    async def wait_messages(
        room_id: str, since: float = 0.0, timeout: float = 25.0, agent_id: str = ""
    ) -> list[dict]:
        """长轮询：等待 since 之后出现的新消息，最多等 timeout 秒；用于 peer 高效跟进。"""
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        if agent_id and hub().is_kicked(agent_id):
            return _kicked_payload()
        if agent_id:
            await hub().mark_peer_seen(agent_id)
        deadline = time.monotonic() + min(max(timeout, 0.0), 50.0)
        while True:
            # 用无界 messages_since，避免房间积压 >200 条时漏发较早的新消息。
            fresh = [m.model_dump() for m in hub().store.messages_since(room_id, since)]
            # 等待期间被踢：立即返回「已被踢出」通知，别让 in-flight 长轮询拖到下一拍才生效。
            if agent_id and hub().is_kicked(agent_id):
                return _kicked_payload()
            if fresh or time.monotonic() >= deadline or should_exit():
                return fresh
            await asyncio.sleep(0.4)

    # ----- peer（MCP 桥接） -------------------------------------------------

    @app.post("/api/peers")
    async def register_peer(body: dict) -> dict:
        """让一个外部 Claude Code 实例以 peer 身份加入房间。

        若已存在同名的 peer 实例（全局，不限于本房间——例如它先调过 connect 全局上线，
        或已在别的主题里），就认领它并确保它在本房间，避免产生重复；否则新建一个 peer。
        """
        room_id = body.get("room_id")
        room = hub().store.get_room(room_id) if room_id else None
        if not room:
            raise HTTPException(404, "房间不存在")
        # 与 /api/instances/connect 一致地归一化名字（strip）——否则 "后端 " 与 "后端" 会被
        # 当成不同实例，导致同一仓库产生重复身份、在线状态与消息路由被劈成两份。
        name = (body.get("name") or "").strip() or "Peer"

        # 全局按名字认领已有的同名 peer（与 /api/instances/connect 保持一致）。
        existing = next(
            (a for a in hub().store.agents.values()
             if a.kind == AgentKind.PEER and a.name == name),
            None,
        )
        if existing:
            patch = AgentUpdate(
                persona=body.get("persona") or None,
                role=body.get("role") or None,
                repo_path=body.get("repo_path") or None,
            )
            await hub().update_agent(existing.id, patch)
            hub().clear_kick(existing.id)  # 主动重连（join/standby）即撤销「踢掉」
            await hub().mark_peer_seen(existing.id)
            if existing.id not in room.agent_ids:
                room.agent_ids.append(existing.id)
                await hub().update_room(room_id, RoomUpdate(agent_ids=room.agent_ids))
            await hub().pua.on_join(room_id, existing.id)
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
        await hub().pua.on_join(room_id, agent.id)
        return {"agent_id": agent.id, "room_id": room_id, "color": agent.color,
                "claimed": False}

    @app.post("/api/peers/{agent_id}/heartbeat")
    async def peer_heartbeat(agent_id: str) -> dict:
        """心跳：由 MCP 桥接进程后台周期性调用，维持"在线"状态（与 LLM 无关、零 token）。

        若该实例已被删除或被踢掉，返回 ``{"ok": False, "kicked": True}``，桥接据此停止心跳。
        """
        if not hub().store.get_agent(agent_id) or hub().is_kicked(agent_id):
            return {"ok": False, "kicked": True}
        await hub().mark_peer_seen(agent_id)
        return {"ok": True}

    @app.post("/api/peers/{agent_id}/leave")
    async def peer_leave(agent_id: str) -> dict:
        """把一个 peer 标记为离线（其在 GUI 中的槽位保留）。"""
        await hub().set_peer_offline(agent_id)
        return {"ok": True}

    @app.post("/api/peers/{agent_id}/kick")
    async def peer_kick(agent_id: str) -> dict:
        """强制踢掉一个 peer 实例：标记离线并记入黑名单，使其后续心跳/活动不再复活它；
        桥接会在下一次心跳 / wait 的响应里收到通知而停止。实例重新注册即可归队。"""
        agent = hub().store.get_agent(agent_id)
        if not agent:
            raise HTTPException(404, "实例不存在")
        await hub().kick_peer(agent_id)
        for room in hub().store.rooms_for_agent(agent_id):
            await hub().post_message(
                Message(
                    room_id=room.id, sender_id="system", sender_name="system",
                    role="system", content=f"{agent.name} 已被踢出（强制下线）。",
                )
            )
        return {"ok": True, "agent_id": agent_id}

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
            hub().clear_kick(existing.id)  # 主动重连即撤销「踢掉」
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
        if not hub().store.get_agent(agent_id):
            raise HTTPException(404, "实例不存在")
        return _instance_new_messages(hub(), agent_id, since)

    @app.get("/api/instances/{agent_id}/wait")
    async def instance_wait(
        agent_id: str, since: float = 0.0, timeout: float = 25.0
    ) -> list[dict]:
        """长轮询：等待该实例所在任一房间出现新消息（跨房间，IM 式跟进）。"""
        if hub().is_kicked(agent_id):
            return _kicked_payload()
        if not hub().store.get_agent(agent_id):
            raise HTTPException(404, "实例不存在")
        await hub().mark_peer_seen(agent_id)
        deadline = time.monotonic() + min(max(timeout, 0.0), 50.0)
        while True:
            fresh = _instance_new_messages(hub(), agent_id, since)
            # 等待期间被踢：立即返回通知，别拖到下一拍。
            if hub().is_kicked(agent_id):
                return _kicked_payload()
            if fresh or time.monotonic() >= deadline or should_exit():
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
        # 主持人/人工可直接邀请；非主持人 agent 须改走 request_action 投递请求由主持人审批。
        by = (body.get("by") or "").strip()
        if by and not hub().is_host(room_id, by):
            raise HTTPException(403, "你不是本主题主持人，请用 request_action 投递邀请请求")
        target = (body.get("target") or "").strip()
        if not target:
            raise HTTPException(400, "target 不能为空")

        match = _find_instance(hub(), target)
        if not match:
            raise HTTPException(404, f"未找到匹配「{target}」的已连接实例")

        # 队列式：同一房间的并发邀请串行执行。后到的请求进锁后会发现目标已在房间里，
        # 直接返回「已在主题内」而不再重复加入、也不再重复广播「拉进了本房间」系统消息。
        async with hub()._invite_lock(room_id):
            room = hub().store.get_room(room_id)
            if not room:
                raise HTTPException(404, "房间不存在")
            if match.id in room.agent_ids:
                return {"agent_id": match.id, "room_id": room_id, "name": match.name,
                        "already_member": True}
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
        await hub().pua.on_join(room_id, match.id)
        return {"agent_id": match.id, "room_id": room_id, "name": match.name,
                "already_member": False}

    # ----- 应答编排（answer floor）-------------------------------------------

    @app.post("/api/rooms/{room_id}/answer/claim")
    async def claim_answer(room_id: str, body: dict) -> dict:
        """抢「应答位」：空闲则成为 holder（你来答）；被别人持有则排队。幂等，可反复调用复查。"""
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        agent_id = (body.get("agent_id") or "").strip()
        if not agent_id or not hub().store.get_agent(agent_id):
            raise HTTPException(404, "实例不存在")
        granted, state = await hub().claim_floor(room_id, agent_id)
        return {"granted": granted, "floor": state}

    @app.post("/api/rooms/{room_id}/answer/release")
    async def release_answer(room_id: str, body: dict) -> dict:
        """释放应答位：holder 释放→自动提升队首；排队者释放→退出队列。"""
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        agent_id = (body.get("agent_id") or "").strip()
        state = await hub().release_floor(room_id, agent_id)
        return {"ok": True, "floor": state}

    @app.get("/api/rooms/{room_id}/answer")
    async def get_answer_floor(room_id: str) -> dict:
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        return hub().floor_state(room_id)

    @app.patch("/api/answer-floor")
    async def set_answer_floor(body: dict) -> dict:
        """运行期切换协同配置（GUI 用）：scope=off/human/broadcast，enforcement=soft/hard，
        directed_visibility=all/recipient/until_reply。"""
        hub().set_floor_config(
            scope=body.get("scope"), enforcement=body.get("enforcement"),
            directed=body.get("directed_visibility"),
        )
        await hub().broadcast(
            {"type": "floor_config", "scope": hub().floor_scope,
             "enforcement": hub().floor_enforcement,
             "directed_visibility": hub().directed_visibility}
        )
        return {"scope": hub().floor_scope, "enforcement": hub().floor_enforcement,
                "directed_visibility": hub().directed_visibility}

    # ----- TODO（各 agent / 主题 / 全局三级）---------------------------------

    @app.get("/api/todos")
    async def list_todos(scope: str, scope_id: str = "") -> list[dict]:
        return [t.model_dump() for t in hub().store.list_todos(scope, scope_id)]

    @app.post("/api/todos")
    async def create_todo(body: dict) -> dict:
        """新增 todo。actor=发起者（agent_id 或缺省 "human"）。按 scope 鉴权：
        agent=本人 / room=主持人 / global=人工或被授权 agent。"""
        actor = (body.get("actor") or "human").strip()
        scope = body.get("scope") or "agent"
        scope_id = (body.get("scope_id") or "").strip()
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text 不能为空")
        if scope not in ("agent", "room", "global"):
            raise HTTPException(400, "scope 非法")
        if not hub().can_edit_todo(actor, scope, scope_id):
            raise HTTPException(403, "无权改这级 todo（主题 todo 需主持人、全局 todo 需授权）")
        assignee = (body.get("assignee") or "").strip()
        todo = await hub().add_todo(actor, scope, scope_id, text, assignee)
        return todo.model_dump()

    @app.patch("/api/todos/{todo_id}")
    async def patch_todo(todo_id: str, body: dict) -> dict:
        todo = hub().store.get_todo(todo_id)
        if not todo:
            raise HTTPException(404, "todo 不存在")
        actor = (body.get("actor") or "human").strip()
        if not hub().can_edit_todo(actor, todo.scope, todo.scope_id):
            raise HTTPException(403, "无权改这条 todo")
        updated = await hub().update_todo(
            todo_id, text=body.get("text"), done=body.get("done"), assignee=body.get("assignee"))
        return updated.model_dump()

    @app.delete("/api/todos/{todo_id}")
    async def delete_todo(todo_id: str, actor: str = "human") -> dict:
        todo = hub().store.get_todo(todo_id)
        if not todo:
            raise HTTPException(404, "todo 不存在")
        if not hub().can_edit_todo(actor, todo.scope, todo.scope_id):
            raise HTTPException(403, "无权删这条 todo")
        await hub().remove_todo(todo_id)
        return {"ok": True}

    @app.get("/api/global-todo-editors")
    async def get_global_editors() -> dict:
        return {"editors": hub().global_todo_editors()}

    @app.patch("/api/global-todo-editors")
    async def set_global_editors(body: dict) -> dict:
        """人工给全局 todo 授权：editors=被授权的 agent_id 列表。"""
        editors = await hub().set_global_todo_editors(list(body.get("editors") or []))
        return {"editors": editors}

    # ----- 主持人 + 受控请求 -------------------------------------------------

    @app.patch("/api/rooms/{room_id}/host")
    async def set_room_host(room_id: str, body: dict) -> dict:
        """改派主持人（人工）。host_id 传成员的 agent_id，传空=取消主持人。"""
        host_id = (body.get("host_id") or "").strip()
        room = hub().store.get_room(room_id)
        if not room:
            raise HTTPException(404, "房间不存在")
        if host_id and host_id not in room.agent_ids:
            raise HTTPException(400, "主持人必须是本主题成员")
        room = await hub().set_host(room_id, host_id)
        return room.model_dump()

    @app.get("/api/requests")
    async def list_requests(room_id: str = "", status: str = "pending") -> list[dict]:
        return [r.model_dump() for r in hub().store.list_requests(room_id, status)]

    @app.post("/api/requests")
    async def create_request(body: dict) -> dict:
        """非主持人投递受控动作请求：action=kick/invite/close/todo_add/todo_update/todo_remove。
        kick/invite 的 target（职责/名字/id）在此解析为 target_id 存入 payload。"""
        room_id = (body.get("room_id") or "").strip()
        action = (body.get("action") or "").strip()
        requester = (body.get("requested_by") or "").strip()
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        if action not in ("kick", "invite", "close", "todo_add", "todo_update", "todo_remove"):
            raise HTTPException(400, "action 非法")
        payload = {k: body[k] for k in ("text", "assignee", "todo_id", "done") if k in body}
        if action in ("kick", "invite"):
            match = _find_instance(hub(), (body.get("target") or "").strip())
            if not match:
                raise HTTPException(404, "未找到目标实例")
            payload["target_id"] = match.id
            payload["target_name"] = match.name
        agent = hub().store.get_agent(requester)
        req = ActionRequest(
            room_id=room_id, action=action, requested_by=requester,
            requested_by_name=agent.name if agent else requester,
            payload=payload, reason=(body.get("reason") or "").strip())
        await hub().submit_request(req)
        return req.model_dump()

    @app.post("/api/requests/{req_id}/resolve")
    async def resolve_request(req_id: str, body: dict) -> dict:
        """主持人/人工审批请求：approve=true 通过并执行、false 拒绝。"""
        req = hub().store.get_request(req_id)
        if not req:
            raise HTTPException(404, "请求不存在")
        approver = (body.get("approver") or "human").strip()
        if not hub().is_host(req.room_id, approver):
            raise HTTPException(403, "只有该主题的主持人（或人工）能审批")
        resolved = await hub().resolve_request(
            req_id, approver, bool(body.get("approve")), (body.get("note") or "").strip())
        return resolved.model_dump()

    # ----- PUA 模式（强制多阶段协同）------------------------------------------

    @app.post("/api/rooms/{room_id}/pua")
    async def set_pua(room_id: str, body: dict) -> dict:
        """开/关某主题的 PUA 模式（人工）。enabled=true 进入上报阶段；window=安静窗口秒数。"""
        if not hub().store.get_room(room_id):
            raise HTTPException(404, "房间不存在")
        if body.get("enabled"):
            window = float(body.get("window") or 60.0)
            await hub().pua.enable(room_id, window)
        else:
            await hub().pua.disable(room_id)
        return {"ok": True, "pua": hub().pua.snapshot(room_id)}

    @app.get("/api/rooms/{room_id}/pua")
    async def get_pua(room_id: str, agent_id: str = "") -> dict:
        """读 PUA 状态；带 agent_id 时附带给该实例的"现在该你做什么"提示。"""
        return {"pua": hub().pua.snapshot(room_id),
                "hint": hub().pua.agent_hint(room_id, agent_id) if agent_id else None}

    @app.post("/api/rooms/{room_id}/pua/pass")
    async def pua_pass(room_id: str, body: dict) -> dict:
        """质疑阶段：某实例对某条 todo 本轮无意见、跳过（也推动流程）。"""
        agent_id = (body.get("agent_id") or "").strip()
        todo_id = (body.get("todo_id") or "").strip()
        msg = await hub().pua.pass_todo(room_id, agent_id, todo_id)
        return {"ok": True, "message": msg, "pua": hub().pua.snapshot(room_id)}

    # ----- WebSocket ----------------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        h = hub()
        queue = h.subscribe()

        async def _pump() -> None:
            await websocket.send_json(h.snapshot())
            while not should_exit():
                # 用超时轮询代替无限 await，使本任务能感知关闭并主动退出。
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                # None 是「该订阅者因积压被丢弃」的关闭哨兵：跳出关闭本连接，让浏览器重连重拉快照。
                if event is None:
                    return
                await websocket.send_json(event)

        async def _watch() -> None:
            # 读侧探测断开：标签页悄悄断开（睡眠/断网）时及时收尾，不必等下一个广播事件
            # 才发现——否则安静房间里死连接会一直占着订阅。
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
            h.unsubscribe(queue)

    # ----- 静态 GUI（最后挂载，保证 API 路由优先匹配） ------------------------

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


def _kicked_payload() -> list[dict]:
    """被踢掉的实例在 wait / instance_wait 上立即收到的「停止待命」系统通知。"""
    return [
        {
            "id": "kicked",
            "room_id": "",
            "sender_id": "system",
            "sender_name": "CCB",
            "role": "system",
            "content": "你已被踢出 CCB（kicked）。请调用 disconnect 结束待命，不要再 wait；"
            "如需归队请重新 standby。",
            "ts": time.time(),
            "color": None,
            "meta": {"kicked": True},
            "room_name": "",
        }
    ]


def _instance_new_messages(hub: Hub, agent_id: str, since: float) -> list[dict]:
    """该实例所在全部房间中、since 之后的新消息，附带房间名以便区分。

    按 directed_visibility 过滤掉「不该让本实例看到的定向消息」（仅 agent 侧；GUI 不经此路径）。
    过滤后为空时长轮询会继续等待——所以被隐藏的定向消息不会让 wait 空转返回。
    """
    rooms = hub.store.rooms_for_agent(agent_id)
    room_names = {r.id: r.name for r in rooms}
    msgs = hub.store.messages_for_rooms_since([r.id for r in rooms], since)
    out = []
    for m in msgs:
        d = m.model_dump()
        d["room_name"] = room_names.get(m.room_id, "")
        out.append(d)
    return hub.filter_visible(out, agent_id, time.time())


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


def _mentioned_tokens(content: str) -> set[str]:
    """从消息正文里取出全部 @token（小写）。"""
    return {m.lower() for m in MENTION_RE.findall(content)}


def _resolve_member(hub: Hub, room_id: str, token: object) -> str | None:
    """把 token（agent_id/名字/职责）解析成本房间内某成员的 agent_id，解析不到返回 None。

    用于消息的显式接收者约束（``to`` / ``mentions``）：GUI 传 agent_id，agent 也可传名字/职责；
    只在房间成员范围内匹配——只能指定在场的人，与文本 @ 的解析口径一致。
    """
    tok = str(token or "").strip()
    if not tok:
        return None
    room = hub.store.get_room(room_id)
    if not room:
        return None
    if tok in room.agent_ids and hub.store.get_agent(tok):
        return tok  # 直接给的就是在场成员的 agent_id
    t = tok.lower()
    for aid in room.agent_ids:
        a = hub.store.get_agent(aid)
        if a and (a.name.lower() == t or (a.role and a.role.lower() == t)):
            return a.id
    return None


def _resolve_mentions(hub: Hub, room_id: str, content: str) -> list[str]:
    """房间内被 @ 点名（按名字或职责匹配）的成员 agent_id。"""
    tokens = _mentioned_tokens(content)
    if not tokens:
        return []
    room = hub.store.get_room(room_id)
    if not room:
        return []
    matched = []
    for aid in room.agent_ids:
        a = hub.store.get_agent(aid)
        if a and (a.name.lower() in tokens or (a.role and a.role.lower() in tokens)):
            matched.append(a.id)
    return matched


def _reply_preview(text: str, limit: int = 80) -> str:
    """把被引用消息压成单行短摘要，便于在引用块里展示。"""
    s = " ".join(text.split())
    return s if len(s) <= limit else s[:limit] + "…"
