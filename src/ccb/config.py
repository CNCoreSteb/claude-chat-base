"""运行期配置与预设加载。

配置来自环境变量（前缀 ``CCB_``）以及可选的 ``.env`` 文件。预设——即服务端
全新启动时的初始智能体与房间——从一个 TOML 文件加载，这样用户无需改动代码就能
定制自己的"演员阵容"。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import Agent, AgentKind, Room, Strategy

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PRESET = PACKAGE_DIR / "presets" / "default.toml"


class Settings(BaseSettings):
    """服务端配置，可通过环境变量（``CCB_*``）或 ``.env`` 覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="CCB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8800

    anthropic_api_key: str | None = None
    default_model: str = "claude-sonnet-4-6"
    director_model: str = "claude-haiku-4-5-20251001"
    # "auto" 在有密钥时选 anthropic，否则选 mock 提供方。
    provider: str = "auto"

    turn_delay: float = 1.2
    max_turns: int = 24
    max_tokens: int = 600  # 每条智能体消息的上限；让对话保持简短、省钱。

    data_dir: Path = Path(".ccb")
    preset: Path = DEFAULT_PRESET
    open_browser: bool = True

    def resolved_provider(self) -> str:
        """返回默认实际使用的提供方名称。"""
        if self.provider != "auto":
            return self.provider
        return "anthropic" if self.anthropic_api_key else "mock"


def load_preset(path: Path, default_model: str) -> tuple[list[Agent], list[Room]]:
    """从 TOML 预设文件加载智能体与房间。

    若文件不存在则返回两个空列表，以保证服务端仍可正常启动。
    """
    if not path.exists():
        return [], []

    data = tomllib.loads(path.read_text(encoding="utf-8"))

    agents: list[Agent] = []
    name_to_id: dict[str, str] = {}
    for raw in data.get("agents", []):
        agent = Agent(
            name=raw["name"],
            persona=raw.get("persona", ""),
            kind=AgentKind(raw.get("kind", "ai")),
            model=raw.get("model") or default_model,
            provider=raw.get("provider"),
            temperature=raw.get("temperature", 0.8),
            color=raw.get("color", Agent.model_fields["color"].default),
            enabled=raw.get("enabled", True),
        )
        agents.append(agent)
        name_to_id[agent.name] = agent.id

    rooms: list[Room] = []
    for raw in data.get("rooms", []):
        # 预设里房间通过名字引用智能体，便于阅读。
        ids = [name_to_id[n] for n in raw.get("agents", []) if n in name_to_id]
        rooms.append(
            Room(
                name=raw["name"],
                topic=raw.get("topic", ""),
                agent_ids=ids,
                strategy=Strategy(raw.get("strategy", "director")),
                max_turns=raw.get("max_turns", 24),
                turn_delay=raw.get("turn_delay", 1.2),
            )
        )

    return agents, rooms
