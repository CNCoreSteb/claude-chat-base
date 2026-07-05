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

from pydantic import BaseModel, Field, field_validator


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _now() -> float:
    return time.time()


class AgentKind(StrEnum):
    """智能体类型。每个 CCB 参与者都是一个外部 Claude Code 实例（peer）。"""

    PEER = "peer"  # 外部 Claude Code 实例通过 MCP 桥接 / Skill 发言。


# 一组精选的配色，让智能体在 GUI 中区分明显，且不必使用随机色。
AGENT_COLORS = [
    "#6366f1", "#ec4899", "#10b981", "#f59e0b", "#06b6d4",
    "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#3b82f6",
]


class Agent(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("agent"))
    name: str
    persona: str = ""
    kind: AgentKind = AgentKind.PEER
    # 多仓库协同：该智能体代表的仓库角色与本地路径（peer 槽位的核心信息）。
    role: str = ""  # 例如：依赖库 / 手机端 / web端 / 后端
    repo_path: str = ""  # 该仓库在本机的路径，便于在对应目录启动 Claude Code
    color: str = AGENT_COLORS[0]
    enabled: bool = True
    # peer 在线状态：预定义的仓库槽位在真实 Claude Code 接入前为离线（仅运行期，加载时重置）。
    online: bool = False
    last_seen: float = 0.0

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, _v: object) -> str:
        # AI 功能已删除：一切参与者皆为 peer。把历史数据库里的旧值（如 kind="ai"）一律兼容为
        # peer，避免旧 .ccb 在启动加载时因非法枚举值而校验失败。
        return "peer"


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
    # 主持人：默认是建群者；可由人工在 GUI 改派。主持人对本主题有直接的踢/邀/关/改 todo 权，
    # 非主持人须经 request_action 投递请求由主持人审批。空=暂无主持人（仅人工管理）。
    host_id: str = ""


TodoScope = Literal["agent", "room", "global"]


class Todo(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("todo"))
    scope: TodoScope = "agent"
    # scope=agent -> agent_id；scope=room -> room_id；scope=global -> ""。
    scope_id: str = ""
    text: str = ""
    done: bool = False
    created_by: str = ""        # agent_id 或 "human"
    assignee: str = ""          # 可选：指派给某 agent_id
    ts: float = Field(default_factory=_now)


class TodoCreate(BaseModel):
    scope: TodoScope = "agent"
    scope_id: str = ""
    text: str
    assignee: str = ""


class TodoUpdate(BaseModel):
    text: str | None = None
    done: bool | None = None
    assignee: str | None = None


# 非主持人请求主持人执行的受控动作。
RequestAction = Literal[
    "kick", "invite", "close",          # 房间管理
    "todo_add", "todo_update", "todo_remove",  # 主题 todo 改动
]


class ActionRequest(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("req"))
    room_id: str
    action: RequestAction
    requested_by: str = ""              # 发起的 agent_id
    requested_by_name: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)  # 例：{"target_id":..} / {"text":..}
    reason: str = ""
    status: Literal["pending", "approved", "rejected"] = "pending"
    ts: float = Field(default_factory=_now)
    resolved_by: str = ""               # 审批者 agent_id 或 "human"
    resolved_ts: float = 0.0
    result: str = ""                    # 执行结果/拒绝说明


class AgentCreate(BaseModel):
    name: str
    persona: str = ""
    kind: AgentKind = AgentKind.PEER
    role: str = ""
    repo_path: str = ""
    color: str | None = None
    enabled: bool = True

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, _v: object) -> str:
        return "peer"  # 一切参与者皆为 peer；容忍旧客户端传入的 kind="ai" 等。


class AgentUpdate(BaseModel):
    name: str | None = None
    persona: str | None = None
    role: str | None = None
    repo_path: str | None = None
    color: str | None = None
    enabled: bool | None = None


class RoomCreate(BaseModel):
    name: str
    topic: str = ""
    agent_ids: list[str] = Field(default_factory=list)


class RoomUpdate(BaseModel):
    name: str | None = None
    topic: str | None = None
    agent_ids: list[str] | None = None


class HumanMessage(BaseModel):
    content: str
    sender_name: str = "你"
