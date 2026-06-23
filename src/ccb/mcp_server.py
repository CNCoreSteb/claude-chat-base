"""MCP 桥接：让每个仓库的 Claude Code 以 peer 身份加入同一个 CCB 房间协同。

典型用法：在仓库 A（依赖库）/ B（手机端）/ C（web端）/ D（后端）各自的目录里启动
Claude Code，各自把本服务注册为 MCP server，然后 `join_room` 认领自己仓库对应的
槽位。之后用 `wait_for_messages` 长轮询跟进，被点名时用 `send_message` 回应——所有
人的协同过程都会实时显示在 CCB 的 GUI 中。

注册方式（先 `uv sync --extra mcp`），在每个仓库的目录下执行：
    claude mcp add --transport stdio ccb -- uv run --project /路径/claude-chat-base ccb-mcp
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("CCB_URL", "http://127.0.0.1:8800").rstrip("/")

# 每个会话的状态（每个 Claude Code 会话对应一个 MCP 服务进程）。
_session: dict[str, object] = {"room_id": None, "agent_id": None, "name": None, "last_ts": 0.0}


def _client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)


def build_server():  # noqa: ANN201 - 返回一个 FastMCP 实例
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - 提示路径
        raise SystemExit(
            "peer 桥接需要安装 'mcp' 包。\n请执行：  uv sync --extra mcp\n"
        ) from exc

    mcp = FastMCP("ccb-peers")

    @mcp.tool()
    async def list_rooms() -> str:
        """列出 CCB 服务上可用的协同房间，以及每个房间里已配置的仓库槽位。"""
        async with _client() as c:
            state = (await c.get("/api/state")).json()
        rooms = state.get("rooms", [])
        agents = {a["id"]: a for a in state.get("agents", [])}
        if not rooms:
            return "目前还没有房间。请在 GUI 里创建一个。"
        out = []
        for r in rooms:
            slots = []
            for aid in r["agent_ids"]:
                a = agents.get(aid)
                if a and a["kind"] == "peer":
                    on = "在线" if a.get("online") else "离线"
                    slots.append(f"{a['name']}（{a.get('role') or '?'}/{on}）")
            out.append(
                f"- {r['name']}（id={r['id']}）话题：{r.get('topic', '')}\n"
                f"    仓库槽位：{'、'.join(slots) or '（无）'}"
            )
        return "\n".join(out)

    @mcp.tool()
    async def join_room(room: str, name: str, role: str = "", repo_path: str = "") -> str:
        """以 peer 身份加入房间。`room` 是房间 id 或名字，`name` 是你的显示名——
        若 GUI 里已为你的仓库预配了同名槽位，会直接认领它。`role`（如 后端/web端）和
        `repo_path` 可选，用于补充你代表的仓库信息。发言前请先调用本工具。"""
        async with _client() as c:
            state = (await c.get("/api/state")).json()
            match = next(
                (r for r in state.get("rooms", []) if r["id"] == room or r["name"] == room),
                None,
            )
            if not match:
                return f"未找到房间「{room}」。可用 list_rooms 查看可选项。"
            resp = await c.post(
                "/api/peers",
                json={
                    "room_id": match["id"],
                    "name": name,
                    "role": role,
                    "repo_path": repo_path,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        _session.update(
            room_id=data["room_id"], agent_id=data["agent_id"], name=name, last_ts=0.0
        )
        how = "认领了已配置的槽位" if data.get("claimed") else "新建了一个槽位"
        return (
            f"已以「{name}」身份加入「{match['name']}」（{how}）。你现在会出现在 GUI 中。\n"
            "接下来：用 wait_for_messages 跟进对话，被点名或有相关变更时用 send_message 回应。"
        )

    @mcp.tool()
    async def send_message(content: str) -> str:
        """向你加入的房间发一条消息。所有仓库 peer（以及 GUI）会即时看到。"""
        if not _session["room_id"]:
            return "请先用 join_room 加入一个房间。"
        async with _client() as c:
            resp = await c.post(
                f"/api/rooms/{_session['room_id']}/messages",
                json={"content": content, "agent_id": _session["agent_id"]},
            )
            resp.raise_for_status()
        return "消息已发送。"

    @mcp.tool()
    async def read_messages() -> str:
        """读取你上次读取之后的新消息（立即返回，不阻塞）。"""
        return await _fetch_new(wait=False)

    @mcp.tool()
    async def wait_for_messages(timeout: float = 25.0) -> str:
        """长轮询：阻塞至多 timeout 秒，等待新消息出现后立即返回（高效跟进对话）。
        建议在协同时反复调用本工具形成"监听—回应"循环。"""
        return await _fetch_new(wait=True, timeout=timeout)

    async def _fetch_new(wait: bool, timeout: float = 25.0) -> str:
        if not _session["room_id"]:
            return "请先用 join_room 加入一个房间。"
        path = "wait" if wait else "messages"
        params = {"since": _session["last_ts"], "agent_id": _session["agent_id"]}
        if wait:
            params["timeout"] = timeout
        async with _client(timeout=timeout + 10) as c:
            resp = await c.get(f"/api/rooms/{_session['room_id']}/{path}", params=params)
            resp.raise_for_status()
            msgs = resp.json()
        if not msgs:
            return "（没有新消息）"
        _session["last_ts"] = max(m["ts"] for m in msgs)
        lines = []
        for m in msgs:
            me = "（你）" if m["sender_id"] == _session["agent_id"] else ""
            lines.append(f"{m['sender_name']}{me}: {m['content']}")
        return "\n".join(lines)

    @mcp.tool()
    async def list_peers() -> str:
        """列出当前房间里的所有仓库参与者及其在线状态。"""
        if not _session["room_id"]:
            return "请先用 join_room 加入一个房间。"
        async with _client() as c:
            state = (await c.get("/api/state")).json()
        room = next((r for r in state["rooms"] if r["id"] == _session["room_id"]), None)
        if not room:
            return "你所在的房间已不存在。"
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
    async def leave_room() -> str:
        """离开房间：把你标记为离线（GUI 中的仓库槽位会保留）。"""
        if not _session["agent_id"]:
            return "你尚未加入任何房间。"
        async with _client() as c:
            await c.post(f"/api/peers/{_session['agent_id']}/leave")
        _session.update(room_id=None, agent_id=None, name=None, last_ts=0.0)
        return "已离开房间。"

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
