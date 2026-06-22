# Agora — Design Notes

This document records the design and the self-iteration behind Agora, so the choices
are reviewable.

## Goal

Build an agent group-chat system that is:

- **Cross-platform** (Windows + Linux/macOS),
- **Easy to start**,
- **GUI-first** — you can *see* what the agents are saying, as it happens,
- inspired by `claude-peers-mcp` (agents talking to agents).

## How it differs from the reference

`claude-peers-mcp` is peer-to-peer: each Claude Code session runs an MCP server, a
broker daemon relays direct messages, and there is no shared room or UI. Agora keeps
the spirit (independent agents conversing) but reshapes it for *observability*:

| | claude-peers-mcp | Agora |
| --- | --- | --- |
| Topology | P2P direct messages | Shared rooms + orchestrator |
| Participants | Real Claude Code sessions | AI agents *and* real peers (via MCP) |
| Driver | Each human prompts their Claude | Autonomous turn-taking loop |
| Observability | CLI / inside each session | Real-time web GUI |
| Stack | Bun / TypeScript | Python + uv, FastAPI, vanilla-JS GUI |

Agora intentionally re-implements the peer bridge (`agora-mcp`) so the original use
case — a real Claude Code instance joining the conversation — still works, now
visible in the GUI.

## Architecture

```
            Browser GUI (vanilla JS)
                  │  REST (commands)        ▲ WebSocket (events)
                  ▼                         │
        ┌──────────────────────────────────────────┐
        │ FastAPI server (server.py)                │
        │   REST control plane + /ws event stream   │
        └───────────────┬───────────────────────────┘
                        │
                 ┌──────▼──────┐      emits events to all WS clients
                 │    Hub      │──────────────────────────────────►
                 │ state + bus │
                 └──┬───────┬──┘
            store   │       │   providers (anthropic | mock)
        (in-mem +   │       │
         JSONL)     │       ▼
                    │   Orchestrator (per-room async loop)
                    │     pick speaker → stream tokens → repeat
                    ▼
          External Claude Code ──(agora-mcp, HTTP)──► /api/peers, /messages
```

### Key decisions

- **uv for everything.** `uv run agora` bootstraps Python + deps; no system Python
  required. This is the single biggest "easy to start" lever and is identical across
  Windows and Linux.
- **Zero-build GUI.** The frontend is plain HTML/CSS/ES-modules served by FastAPI —
  no Node toolchain, no bundler, nothing to compile. The whole app is one
  `uv run` away. A WebSocket carries a `snapshot` on connect and incremental events
  after; REST handles mutations. This keeps the GUI honest (state always mirrors the
  server) and trivially cross-platform.
- **Mock provider by default.** With no API key the system is fully functional —
  streaming, the director, status indicators — so anyone can try it in seconds and
  tests never hit the network.
- **Token streaming over the bus.** Each agent turn emits `message_start` →
  `message_delta*` → `message_end`. This is what makes "watch the agents think" feel
  alive, and it is the core requirement of the brief.
- **Director vs round-robin.** A natural group chat needs someone to decide who
  talks. A cheap moderator model (Haiku) chooses the next speaker and can end the
  discussion; round-robin is the deterministic fallback (and what mock mode uses).
- **Append-only JSONL transcripts.** Durable, human-readable, no DB engine to
  install — the right amount of persistence for a local tool.
- **Safety rails.** A `max_turns` cap, Stop/Pause, and bounded prompt windows keep
  autonomous loops from running away or getting expensive.

## Iteration log

1. **P2P vs. room.** Mirroring the reference's direct-message broker would not
   satisfy "see what agents are saying" well — there's no shared surface to render.
   Chose a **shared room + orchestrator** so there is one transcript to watch.
2. **Desktop GUI vs. web GUI.** Electron/Tauri add build complexity and per-OS
   packaging. A **browser GUI served by the backend** is the most portable and the
   easiest to launch, so it won.
3. **Framework frontend vs. zero-build.** A React/Vite app is "best practice" but
   needs Node at build time, fighting the "easy to start" goal. A well-structured
   vanilla ES-module app keeps the project to a single `uv run` while staying clean.
4. **Honoring the reference.** Added the `agora-mcp` bridge so a real Claude Code
   peer can still join — closing the loop back to `claude-peers`.

## Possible next steps

- Per-room export ("save transcript as Markdown").
- Tool-use agents (let agents run tools and show tool calls in the GUI).
- Multiple concurrent rooms visible at once (split view).
- Auth + remote hosting profile (currently localhost-first by design).
