"""MCP bridge: let an external Claude Code instance join an Agora room as a peer.

Run this as a stdio MCP server from any Claude Code session (see docs). It talks to
a running Agora HTTP server, so the peer's messages appear live in the GUI alongside
the AI agents — bridging the "claude-peers" idea into the group-chat GUI.

Register it (after `uv sync --extra mcp`) with, e.g.:
    claude mcp add --transport stdio agora -- uv run agora-mcp
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("AGORA_URL", "http://127.0.0.1:8800").rstrip("/")

# Per-session state (one MCP server process per Claude Code session).
_session: dict[str, object] = {"room_id": None, "agent_id": None, "name": None, "last_ts": 0.0}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=15.0)


def build_server():  # noqa: ANN201 - returns a FastMCP instance
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - guidance path
        raise SystemExit(
            "The 'mcp' package is required for the peer bridge.\n"
            "Install it with:  uv sync --extra mcp\n"
        ) from exc

    mcp = FastMCP("agora-peers")

    @mcp.tool()
    async def list_rooms() -> str:
        """List the chat rooms available on the Agora server."""
        async with _client() as c:
            state = (await c.get("/api/state")).json()
        rooms = state.get("rooms", [])
        if not rooms:
            return "No rooms exist yet. Ask the host to create one in the GUI."
        return "\n".join(
            f"- {r['name']} (id={r['id']}, {len(r['agent_ids'])} participants, {r['status']})"
            f"\n    topic: {r.get('topic', '')}"
            for r in rooms
        )

    @mcp.tool()
    async def join_room(room: str, name: str, persona: str = "") -> str:
        """Join a room as a peer. `room` may be a room id or its name. `name` is your
        display name in the chat. Returns confirmation. Call this before sending."""
        async with _client() as c:
            state = (await c.get("/api/state")).json()
            match = next(
                (r for r in state.get("rooms", []) if r["id"] == room or r["name"] == room),
                None,
            )
            if not match:
                return f"Room '{room}' not found. Use list_rooms to see options."
            resp = await c.post(
                "/api/peers",
                json={"room_id": match["id"], "name": name, "persona": persona},
            )
            resp.raise_for_status()
            data = resp.json()
        _session.update(
            room_id=data["room_id"], agent_id=data["agent_id"], name=name, last_ts=0.0
        )
        return f"Joined '{match['name']}' as '{name}'. You now appear in the GUI."

    @mcp.tool()
    async def send_message(content: str) -> str:
        """Post a message to the room you joined. Everyone (and the GUI) sees it instantly."""
        if not _session["room_id"]:
            return "Join a room first with join_room."
        async with _client() as c:
            resp = await c.post(
                f"/api/rooms/{_session['room_id']}/messages",
                json={"content": content, "agent_id": _session["agent_id"]},
            )
            resp.raise_for_status()
        return "Message sent."

    @mcp.tool()
    async def read_messages() -> str:
        """Read messages posted since your last read (poll this to follow the chat)."""
        if not _session["room_id"]:
            return "Join a room first with join_room."
        async with _client() as c:
            resp = await c.get(
                f"/api/rooms/{_session['room_id']}/messages",
                params={"since": _session["last_ts"]},
            )
            resp.raise_for_status()
            msgs = resp.json()
        if not msgs:
            return "(no new messages)"
        _session["last_ts"] = max(m["ts"] for m in msgs)
        lines = []
        for m in msgs:
            me = " (you)" if m["sender_id"] == _session["agent_id"] else ""
            lines.append(f"{m['sender_name']}{me}: {m['content']}")
        return "\n".join(lines)

    @mcp.tool()
    async def list_peers() -> str:
        """List the participants currently in your room."""
        if not _session["room_id"]:
            return "Join a room first with join_room."
        async with _client() as c:
            state = (await c.get("/api/state")).json()
        room = next((r for r in state["rooms"] if r["id"] == _session["room_id"]), None)
        if not room:
            return "Your room no longer exists."
        agents = {a["id"]: a for a in state["agents"]}
        out = []
        for aid in room["agent_ids"]:
            a = agents.get(aid)
            if a:
                out.append(f"- {a['name']} ({a['kind']}, {a['status']})")
        return "\n".join(out) or "(no participants)"

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
