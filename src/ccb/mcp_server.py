"""MCP 桥接：让一个外部的 Claude Code 实例以 peer 身份加入 CCB 房间。

把它作为一个 stdio MCP 服务从任意 Claude Code 会话中运行（见文档）。它会与正在运行
的 CCB HTTP 服务通信，因此该 peer 的发言会和 AI 智能体一起实时出现在 GUI 中——把
"claude-peers"的点子接入到群聊 GUI 里。

注册方式（先执行 `uv sync --extra mcp`），例如：
    claude mcp add --transport stdio ccb -- uv run ccb-mcp
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("CCB_URL", "http://127.0.0.1:8800").rstrip("/")

# 每个会话的状态（每个 Claude Code 会话对应一个 MCP 服务进程）。
_session: dict[str, object] = {"room_id": None, "agent_id": None, "name": None, "last_ts": 0.0}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=15.0)


def build_server():  # noqa: ANN201 - 返回一个 FastMCP 实例
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - 提示路径
        raise SystemExit(
            "peer 桥接需要安装 'mcp' 包。\n"
            "请执行：  uv sync --extra mcp\n"
        ) from exc

    mcp = FastMCP("ccb-peers")

    @mcp.tool()
    async def list_rooms() -> str:
        """列出 CCB 服务上可用的聊天房间。"""
        async with _client() as c:
            state = (await c.get("/api/state")).json()
        rooms = state.get("rooms", [])
        if not rooms:
            return "目前还没有房间。请在 GUI 里创建一个。"
        return "\n".join(
            f"- {r['name']}（id={r['id']}，{len(r['agent_ids'])} 名参与者，{r['status']}）"
            f"\n    话题：{r.get('topic', '')}"
            for r in rooms
        )

    @mcp.tool()
    async def join_room(room: str, name: str, persona: str = "") -> str:
        """以 peer 身份加入房间。`room` 可以是房间 id 或名字，`name` 是你在聊天中的
        显示名。返回确认信息。发言前请先调用本工具。"""
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
                json={"room_id": match["id"], "name": name, "persona": persona},
            )
            resp.raise_for_status()
            data = resp.json()
        _session.update(
            room_id=data["room_id"], agent_id=data["agent_id"], name=name, last_ts=0.0
        )
        return f"已以「{name}」身份加入「{match['name']}」。你现在会出现在 GUI 中。"

    @mcp.tool()
    async def send_message(content: str) -> str:
        """向你加入的房间发一条消息。所有人（以及 GUI）会即时看到。"""
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
        """读取你上次读取之后的新消息（轮询本工具即可跟上聊天进度）。"""
        if not _session["room_id"]:
            return "请先用 join_room 加入一个房间。"
        async with _client() as c:
            resp = await c.get(
                f"/api/rooms/{_session['room_id']}/messages",
                params={"since": _session["last_ts"]},
            )
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
        """列出当前房间里的参与者。"""
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
            if a:
                out.append(f"- {a['name']}（{a['kind']}，{a['status']}）")
        return "\n".join(out) or "（没有参与者）"

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
