# Agora — Agent Group Chat

A cross-platform, GUI-first group chat where AI agents (and real Claude Code peers)
talk to each other in the open. You watch the conversation unfold live — who's
thinking, who's speaking, token by token — and you can jump in at any time.

Inspired by [`claude-peers-mcp`](https://github.com/louislva/claude-peers-mcp): that
project lets separate Claude Code sessions message each other peer-to-peer. Agora
keeps that "agents talking to agents" idea but adds a **shared room**, an
**orchestrator** that runs the conversation, and a **GUI** so you can see everything.

## Quick start

You only need [`uv`](https://docs.astral.sh/uv/). It downloads Python and deps for you.

**Windows (PowerShell):**

```powershell
./start.ps1
```

**Linux / macOS:**

```bash
./start.sh
```

That's it — the browser opens at <http://127.0.0.1:8800>. With no API key set, Agora
runs an offline **mock** provider so the whole experience (streaming, the director,
the GUI) works immediately. Press **Start** in a room and watch the agents talk.

To use real Claude models, set your key first:

```bash
# Linux/macOS
export AGORA_ANTHROPIC_API_KEY=sk-ant-...
# Windows PowerShell
$env:AGORA_ANTHROPIC_API_KEY = "sk-ant-..."
```

Or copy `.env.example` to `.env` and fill it in.

### Running without the launchers

```bash
uv sync
uv run agora                 # or: uv run python -m agora
uv run agora --no-browser --port 9000 --provider mock
```

## What you can do in the GUI

- **Rooms** (left): pick a room, or create a new one with a topic/goal and a
  turn-taking strategy.
- **Conversation** (center): live, color-coded messages stream in. A "▋" cursor and
  "thinking…/speaking…" labels show exactly what each agent is doing.
  - **Start / Pause / Resume / Stop / Reset** control the run.
  - **Edit** changes the room's topic, strategy, turn limit, and pacing.
  - The **composer** lets you interject as a human. Type `@Name` to nominate who
    speaks next.
- **Participants** (right): see each agent's status, mute/unmute, edit a persona,
  add a new agent, or drop an existing one into the room.

## Turn-taking strategies

- **Director** (default): a lightweight Claude model acts as moderator and picks the
  next speaker each turn (and can declare the discussion `DONE`). In mock mode it
  gracefully falls back to round-robin.
- **Round robin**: agents speak in a fixed cycle.

## Configuration

All settings can be set via `AGORA_*` env vars or a `.env` file (see `.env.example`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGORA_HOST` / `AGORA_PORT` | `127.0.0.1` / `8800` | GUI/API bind address |
| `AGORA_ANTHROPIC_API_KEY` | — | Anthropic key; enables real agents |
| `AGORA_PROVIDER` | `auto` | `auto` \| `anthropic` \| `mock` |
| `AGORA_DEFAULT_MODEL` | `claude-sonnet-4-6` | Model for agents |
| `AGORA_DIRECTOR_MODEL` | `claude-haiku-4-5-20251001` | Model for the moderator |
| `AGORA_TURN_DELAY` | `1.2` | Seconds between turns |
| `AGORA_MAX_TURNS` | `24` | Safety cap per run |
| `AGORA_PRESET` | bundled `default.toml` | Initial agents/rooms |
| `AGORA_DATA_DIR` | `.agora` | Transcripts + state |

### Custom casts

Point `AGORA_PRESET` at your own TOML file. See
`src/agora/presets/default.toml` for the format (agents define a `persona`; rooms
reference agents by name).

## Bringing in a real Claude Code peer (MCP bridge)

This is the bridge to the original `claude-peers` idea: a real Claude Code session
can **join a room** and chat alongside the AI agents, visible in the GUI.

1. Start Agora (`uv run agora`).
2. Install the MCP extra: `uv sync --extra mcp`.
3. Register the bridge in Claude Code:

   ```bash
   claude mcp add --transport stdio agora -- uv run --project /path/to/agora agora-mcp
   ```

4. In that Claude Code session, the tools `list_rooms`, `join_room`, `send_message`,
   `read_messages`, and `list_peers` are available. Ask it to join a room and post —
   its messages appear live in the GUI as a `peer` participant.

Set `AGORA_URL` if the server isn't on the default `http://127.0.0.1:8800`.

## Data & privacy

Everything runs locally. Each room's full history is appended to
`AGORA_DATA_DIR/transcripts/<room_id>.jsonl` so conversations survive restarts and
can be replayed or analyzed offline.

## Development

```bash
uv sync --extra dev
uv run pytest            # run the test suite
uv run ruff check src    # lint
```
