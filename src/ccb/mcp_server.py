"""MCP 桥接：把每个仓库的 Claude Code 接成一个"多仓库 IM"里的实例。

每个仓库（依赖库 / 手机端 / web端 / 后端 …）各跑一个 Claude Code，通过本桥接：
- `connect` 全局上线（声明自己的职责 role），或 `join_room` 直接加入某个主题；
- `list_instances` 发现其他已连接的实例及其职责；
- `invite` 按职责（role）把别的实例**主动拉进**某个主题群；
- `create_topic` 开一个新主题群；`send_message` 发言；
- `wait_for_messages` 跨所有所在主题长轮询跟进（IM 式）。

所有消息与主题都持久化在 CCB 的 SQLite 里，并实时显示在 GUI 中。

注册（依赖已随 `uv sync` 装好），在每个仓库目录下执行：
    claude mcp add --transport stdio ccb -- uv run --project /路径/claude-chat-base ccb-mcp
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager

import httpx

BASE_URL = os.environ.get("CCB_URL", "http://127.0.0.1:8800").rstrip("/")
HEARTBEAT_INTERVAL = 15.0  # 秒；由桥接进程后台发送，与 LLM 无关、零 token。

# 每个会话的状态（每个 Claude Code 会话对应一个 MCP 服务进程）。
_session: dict[str, object] = {"agent_id": None, "name": None, "active_room": None, "last_ts": 0.0}


def _client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)


async def _heartbeat_loop() -> None:
    """后台心跳：只要本会话已加入（有 agent_id），就周期性告诉服务端"我还在线"。

    这是**桥接进程**在做的事——不需要 LLM 调用任何工具、不消耗任何 token。它代表
    "这个 Claude Code 会话仍连着 CCB"，正是"在线"应有的含义。
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        aid = _session.get("agent_id")
        if not aid:
            continue
        try:
            async with _client(8) as c:
                await c.post(f"/api/peers/{aid}/heartbeat")
        except Exception:  # noqa: BLE001 - 服务未启动/网络抖动：忽略，下一拍再试
            pass


@asynccontextmanager
async def _lifespan(_server):  # noqa: ANN001 - FastMCP 生命周期钩子
    # 桥接进程一启动就跑后台心跳；进程随 Claude Code 会话存活/退出。
    task = asyncio.create_task(_heartbeat_loop())
    try:
        yield {}
    finally:
        task.cancel()
        # 会话正常结束时立即下线；崩溃/被强杀则由服务端的"过期检查"兜底标记离线。
        aid = _session.get("agent_id")
        if aid:
            try:
                async with _client(5) as c:
                    await c.post(f"/api/peers/{aid}/leave")
            except Exception:  # noqa: BLE001
                pass


async def _resolve_room(c: httpx.AsyncClient, ref: str) -> dict | None:
    state = (await c.get("/api/state")).json()
    return next(
        (r for r in state.get("rooms", []) if r["id"] == ref or r["name"] == ref), None
    )


def build_server():  # noqa: ANN201 - 返回一个 FastMCP 实例
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - 提示路径
        raise SystemExit(
            "peer 桥接需要安装 'mcp' 包。\n请执行：  uv sync\n"
        ) from exc

    mcp = FastMCP("ccb-peers", lifespan=_lifespan)

    # ----- 上线 / 加入 --------------------------------------------------------

    @mcp.tool()
    async def standby(name: str = "", role: str = "", room: str = "大厅") -> str:
        """进入 CCB 待命状态：以本仓库身份自注册并加入主题，然后**持续轮询**消息、被点名或有相关
        变更时回应。当用户说"进入 ccb 待命状态 / ccb 待命 / 进入待命 / standby"时调用本工具。

        `name` 缺省取当前目录名；`role` 是职责（如 后端/web端）；`room` 缺省"大厅"。"""
        nm = name or os.path.basename(os.getcwd()) or "Peer"
        repo = os.getcwd()
        async with _client() as c:
            match = await _resolve_room(c, room)
            if not match:  # 主题不存在则创建
                r = (await c.post("/api/rooms", json={"name": room})).json()
                match = {"id": r["id"], "name": r["name"]}
            resp = await c.post(
                "/api/peers",
                json={"room_id": match["id"], "name": nm, "role": role, "repo_path": repo},
            )
            resp.raise_for_status()
            data = resp.json()
        _session.update(agent_id=data["agent_id"], name=nm, active_room=match["id"], last_ts=0.0)
        return (
            f"已进入待命：以「{nm}」（{role or '未注明职责'}）加入主题「{match['name']}」。\n"
            f"仓库路径：{repo}\n\n"
            "【待命循环 —— 现在开始持续执行，不要结束本回合、也不要等用户】\n"
            "1. 调用 wait_for_messages（长轮询，阻塞到有新消息才返回，几乎不耗 token）。\n"
            "2. 只处理点名你 / 与本仓库相关的消息：读改本仓库代码后用 send_message 简短回应；\n"
            "   其它消息忽略即可，不要逐条回复。\n"
            "3. 无论有无消息，立刻再次调用 wait_for_messages，回到第 1 步，反复保持在线。\n\n"
            "仅当用户说「退出待命 / 停止 / exit standby」时，才用 disconnect 下线停止循环。\n"
            "（用户随时可按 Esc 打断你插话。）"
        )

    @mcp.tool()
    async def connect(name: str, role: str = "", repo_path: str = "") -> str:
        """全局上线（声明你代表的仓库）。`name` 是显示名，`role` 是职责（如 后端/web端），
        `repo_path` 是本仓库本地路径。上线后即可被别的实例发现与拉群；之后可用
        join_room / create_topic 进入主题。"""
        async with _client() as c:
            resp = await c.post(
                "/api/instances/connect",
                json={"name": name, "role": role, "repo_path": repo_path},
            )
            resp.raise_for_status()
            data = resp.json()
        _session.update(agent_id=data["agent_id"], name=name)
        how = "认领了已有身份" if data.get("claimed") else "新建了身份"
        return f"已上线：{name}（{role or '未注明职责'}），{how}。"

    @mcp.tool()
    async def join_room(room: str, name: str = "", role: str = "", repo_path: str = "") -> str:
        """加入某个主题群（`room` 为主题名或 id）。若 GUI 里已为你的仓库预配了同名槽位，
        会直接认领。加入后该主题成为你的"当前主题"。"""
        nm = name or _session.get("name") or "Peer"
        async with _client() as c:
            match = await _resolve_room(c, room)
            if not match:
                return f"未找到主题「{room}」。可用 list_rooms 查看。"
            resp = await c.post(
                "/api/peers",
                json={"room_id": match["id"], "name": nm, "role": role, "repo_path": repo_path},
            )
            resp.raise_for_status()
            data = resp.json()
        _session.update(agent_id=data["agent_id"], name=nm, active_room=match["id"])
        how = "认领了已配置的槽位" if data.get("claimed") else "加入"
        return (
            f"已以「{nm}」{how}主题「{match['name']}」。当前主题已切到这里。\n"
            "用 wait_for_messages 跟进；用 invite 按职责把需要的仓库拉进来。"
        )

    @mcp.tool()
    async def create_topic(name: str, topic: str = "") -> str:
        """新建一个主题群并把自己加入。`name` 是群名，`topic` 是该群的目标/说明。
        新建后成为你的"当前主题"。需要先 connect 或 join_room。"""
        if not _session["agent_id"]:
            return "请先用 connect 或 join_room 上线。"
        async with _client() as c:
            resp = await c.post(
                "/api/rooms",
                json={"name": name, "topic": topic, "agent_ids": [_session["agent_id"]]},
            )
            resp.raise_for_status()
            r = resp.json()
        _session["active_room"] = r["id"]
        return f"已创建主题「{name}」并设为当前主题。用 invite 把相关仓库拉进来。"

    # ----- 发现 / 拉群 --------------------------------------------------------

    @mcp.tool()
    async def list_rooms() -> str:
        """列出所有主题群及其参与者数量。"""
        async with _client() as c:
            state = (await c.get("/api/state")).json()
        rooms = state.get("rooms", [])
        if not rooms:
            return "目前还没有主题。可用 create_topic 新建一个。"
        cur = _session.get("active_room")
        return "\n".join(
            f"{'* ' if r['id'] == cur else '- '}{r['name']}"
            f"（{len(r['agent_ids'])} 名参与者）话题：{r.get('topic', '')}"
            for r in rooms
        )

    @mcp.tool()
    async def list_instances() -> str:
        """列出所有已连接的实例（各仓库的 Claude Code）及其职责、在线状态、所在主题——
        据此决定按职责把谁拉进群。"""
        async with _client() as c:
            data = (await c.get("/api/instances")).json()
        if not data:
            return "目前没有已连接的实例。"
        lines = []
        for a in data:
            on = "在线" if a["online"] else "离线"
            rooms = "、".join(r["name"] for r in a.get("rooms", [])) or "（不在任何主题）"
            me = "（你）" if a["id"] == _session.get("agent_id") else ""
            lines.append(f"- {a['name']}{me}｜职责：{a.get('role') or '?'}｜{on}｜主题：{rooms}")
        return "\n".join(lines)

    @mcp.tool()
    async def invite(target: str, topic: str = "") -> str:
        """按"职责或名字"把另一个已连接的实例拉进某个主题群。`target` 如 "后端"/"web端"
        或对方显示名；`topic` 为目标主题名（缺省=你的当前主题），若该主题不存在会自动新建。"""
        if not _session["agent_id"]:
            return "请先 connect 或 join_room 后再拉人。"
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "请先 join_room/create_topic 设定当前主题，或在 topic 参数里指定。"
            match = await _resolve_room(c, room_ref)
            if not match and topic:
                # 指定的主题不存在 -> 自动新建并把自己加入。
                r = (await c.post(
                    "/api/rooms",
                    json={"name": topic, "agent_ids": [_session["agent_id"]]},
                )).json()
                match = {"id": r["id"], "name": r["name"]}
                _session["active_room"] = r["id"]
            if not match:
                return f"未找到主题「{room_ref}」。"
            resp = await c.post(
                f"/api/rooms/{match['id']}/invite",
                json={"target": target, "by": _session["agent_id"]},
            )
            if resp.status_code == 404:
                return f"未找到职责/名字为「{target}」的已连接实例。先让对方 connect 上线。"
            resp.raise_for_status()
            data = resp.json()
        return f"已把「{data['name']}」拉进主题「{match['name']}」。"

    # ----- 收发消息 -----------------------------------------------------------

    @mcp.tool()
    async def send_message(content: str, topic: str = "") -> str:
        """发言。`topic` 指定目标主题（缺省=当前主题）。所有该主题成员与 GUI 即时可见。"""
        if not _session["agent_id"]:
            return "请先 connect / join_room。"
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "没有当前主题，请用 topic 指定，或先 join_room/create_topic。"
            match = await _resolve_room(c, room_ref)
            if not match:
                return f"未找到主题「{room_ref}」。"
            resp = await c.post(
                f"/api/rooms/{match['id']}/messages",
                json={"content": content, "agent_id": _session["agent_id"]},
            )
            resp.raise_for_status()
        return f"已发送到「{match['name']}」。"

    @mcp.tool()
    async def wait_for_messages(timeout: float = 25.0) -> str:
        """长轮询：阻塞至多 timeout 秒，等待你所在**任意主题**出现新消息后返回（IM 式跟进）。
        待命时**反复调用**本工具形成"监听—回应"循环：返回后处理与你相关的消息，然后立刻再次调用。"""
        return await _fetch_new(wait=True, timeout=timeout)

    @mcp.tool()
    async def read_messages() -> str:
        """立即读取你所在全部主题中、上次之后的新消息（不阻塞）。"""
        return await _fetch_new(wait=False)

    async def _fetch_new(wait: bool, timeout: float = 25.0) -> str:
        aid = _session["agent_id"]
        if not aid:
            return "请先 connect / join_room。"
        path = "wait" if wait else "messages"
        params = {"since": _session["last_ts"]}
        if wait:
            params["timeout"] = timeout
        async with _client(timeout=timeout + 10) as c:
            resp = await c.get(f"/api/instances/{aid}/{path}", params=params)
            resp.raise_for_status()
            msgs = resp.json()
        if not msgs:
            return "（没有新消息）"
        _session["last_ts"] = max(m["ts"] for m in msgs)
        lines = []
        for m in msgs:
            me = "（你）" if m["sender_id"] == aid else ""
            room = f"[{m.get('room_name', '')}] " if m.get("room_name") else ""
            lines.append(f"{room}{m['sender_name']}{me}: {m['content']}")
        return "\n".join(lines)

    # ----- 其它 ---------------------------------------------------------------

    @mcp.tool()
    async def list_peers(topic: str = "") -> str:
        """列出某主题群里的参与者及在线状态（缺省=当前主题）。"""
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "没有当前主题，请用 topic 指定。"
            state = (await c.get("/api/state")).json()
            room = next(
                (r for r in state["rooms"] if r["id"] == room_ref or r["name"] == room_ref), None
            )
            if not room:
                return f"未找到主题「{room_ref}」。"
            agents = {a["id"]: a for a in state["agents"]}
        out = []
        for aid in room["agent_ids"]:
            a = agents.get(aid)
            if not a:
                continue
            on = "在线" if a.get("online") else ("—" if a["kind"] != "peer" else "离线")
            tag = f"{a.get('role')}/" if a.get("role") else ""
            out.append(f"- {a['name']}（{tag}{a['kind']}，{on}）")
        return "\n".join(out) or "（没有参与者）"

    @mcp.tool()
    async def leave_room(topic: str = "") -> str:
        """退出某个主题群（缺省=当前主题）；你仍保持在线，可继续在其它主题协同。"""
        aid = _session["agent_id"]
        if not aid:
            return "你尚未加入任何主题。"
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            match = await _resolve_room(c, room_ref) if room_ref else None
            if not match:
                return "未找到要退出的主题。"
            await c.delete(f"/api/rooms/{match['id']}/agents/{aid}")
        if _session.get("active_room") == match["id"]:
            _session["active_room"] = None
        return f"已退出主题「{match['name']}」。"

    @mcp.tool()
    async def disconnect() -> str:
        """全局下线：标记离线（你的身份与历史在 GUI 中保留）。"""
        aid = _session["agent_id"]
        if not aid:
            return "你尚未上线。"
        async with _client() as c:
            await c.post(f"/api/peers/{aid}/leave")
        _session.update(agent_id=None, name=None, active_room=None, last_ts=0.0)
        return "已下线。"

    return mcp


def main() -> None:
    try:
        server = build_server()
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        raise
    server.run()


if __name__ == "__main__":
    main()
