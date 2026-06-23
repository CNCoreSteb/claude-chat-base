"""CCB 的核心领域模型。

这些 pydantic 模型描述了系统中可持久化 / 可序列化的状态：智能体（参与者）、
房间（群聊）以及消息。它们刻意保持精简、对 JSON 友好，因此可以原样通过
WebSocket 和 REST 接口传输。
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
    """智能体产生消息的方式。"""

    AI = "ai"  # 由编排器通过 LLM 提供方在本地生成。
    PEER = "peer"  # 外部 Claude Code 实例通过 MCP 桥接发言。


class Strategy(StrEnum):
    """编排器决定下一个发言者的策略。"""

    DIRECTOR = "director"  # 由 LLM 主持人提名下一个发言者。
    ROUND_ROBIN = "round_robin"  # 在启用的 AI 智能体之间按顺序轮流。


class RoomStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"


# 一组精选的配色，让智能体在 GUI 中区分明显，且不必使用随机色。
AGENT_COLORS = [
    "#6366f1", "#ec4899", "#10b981", "#f59e0b", "#06b6d4",
    "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#3b82f6",
]


class Agent(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("agent"))
    name: str
    persona: str = ""
    kind: AgentKind = AgentKind.AI
    # 多仓库协同：该智能体代表的仓库角色与本地路径（peer 槽位的核心信息）。
    role: str = ""  # 例如：依赖库 / 手机端 / web端 / 后端
    repo_path: str = ""  # 该仓库在本机的路径，便于在对应目录启动 Claude Code
    model: str | None = None  # 为 None 时回退到服务端默认模型。
    provider: str | None = None  # "anthropic" | "mock" | None（用服务端默认）。
    temperature: float = 0.8
    color: str = AGENT_COLORS[0]
    enabled: bool = True
    # 仅运行期使用的字段（不是编排输入）；加载时会重置。
    status: Literal["idle", "thinking", "speaking"] = "idle"
    # peer 在线状态：预定义的仓库槽位在真实 Claude Code 接入前为离线。
    online: bool = False
    last_seen: float = 0.0


class Message(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("msg"))
    room_id: str
    sender_id: str  # 智能体 id、"human" 或 "system"
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
    turn: int = 0  # 当前这一轮运行中已经进行的发言轮数。


class AgentCreate(BaseModel):
    name: str
    persona: str = ""
    kind: AgentKind = AgentKind.AI
    role: str = ""
    repo_path: str = ""
    model: str | None = None
    provider: str | None = None
    temperature: float = 0.8
    color: str | None = None
    enabled: bool = True


class AgentUpdate(BaseModel):
    name: str | None = None
    persona: str | None = None
    role: str | None = None
    repo_path: str | None = None
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
    sender_name: str = "你"
