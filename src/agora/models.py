"""Core domain models for Agora.

These pydantic models describe the persistent/serializable state of the system:
agents (participants), rooms (group chats), and messages. They are intentionally
small and JSON-friendly so they can flow over the WebSocket and REST API verbatim.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _now() -> float:
    return time.time()


class AgentKind(StrEnum):
    """How an agent produces its messages."""

    AI = "ai"  # Generated locally by the orchestrator via an LLM provider.
    PEER = "peer"  # An external Claude Code instance posting through the MCP bridge.


class Strategy(StrEnum):
    """How the orchestrator decides who speaks next."""

    DIRECTOR = "director"  # An LLM moderator nominates the next speaker.
    ROUND_ROBIN = "round_robin"  # Cycle through enabled AI agents in order.


class RoomStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"


# A curated palette so agents are visually distinct in the GUI without random colors.
AGENT_COLORS = [
    "#6366f1", "#ec4899", "#10b981", "#f59e0b", "#06b6d4",
    "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#3b82f6",
]


class Agent(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("agent"))
    name: str
    persona: str = ""
    kind: AgentKind = AgentKind.AI
    model: str | None = None  # Falls back to the server default when None.
    provider: str | None = None  # "anthropic" | "mock" | None (use server default).
    temperature: float = 0.8
    color: str = AGENT_COLORS[0]
    enabled: bool = True
    # Runtime-only fields (not orchestration inputs); reset on load.
    status: Literal["idle", "thinking", "speaking"] = "idle"


class Message(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("msg"))
    room_id: str
    sender_id: str  # agent id, "human", or "system"
    sender_name: str
    role: Literal["agent", "human", "system"] = "agent"
    content: str = ""
    ts: float = Field(default_factory=_now)
    color: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class Room(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("room"))
    name: str
    topic: str = ""
    agent_ids: list[str] = Field(default_factory=list)
    strategy: Strategy = Strategy.DIRECTOR
    status: RoomStatus = RoomStatus.IDLE
    max_turns: int = 24
    turn_delay: float = 1.2
    turn: int = 0  # Number of agent turns taken in the current run.


class AgentCreate(BaseModel):
    name: str
    persona: str = ""
    kind: AgentKind = AgentKind.AI
    model: str | None = None
    provider: str | None = None
    temperature: float = 0.8
    color: str | None = None
    enabled: bool = True


class AgentUpdate(BaseModel):
    name: str | None = None
    persona: str | None = None
    model: str | None = None
    provider: str | None = None
    temperature: float | None = None
    color: str | None = None
    enabled: bool | None = None


class RoomCreate(BaseModel):
    name: str
    topic: str = ""
    agent_ids: list[str] = Field(default_factory=list)
    strategy: Strategy = Strategy.DIRECTOR
    max_turns: int = 24
    turn_delay: float = 1.2


class RoomUpdate(BaseModel):
    name: str | None = None
    topic: str | None = None
    agent_ids: list[str] | None = None
    strategy: Strategy | None = None
    max_turns: int | None = None
    turn_delay: float | None = None


class HumanMessage(BaseModel):
    content: str
    sender_name: str = "You"
