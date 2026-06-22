"""Runtime configuration and preset loading.

Settings come from environment variables (prefix ``AGORA_``) and an optional
``.env`` file. Presets — the initial agents and rooms a fresh server starts with —
are loaded from a TOML file so users can curate their own casts without touching code.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import Agent, AgentKind, Room, Strategy

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PRESET = PACKAGE_DIR / "presets" / "default.toml"


class Settings(BaseSettings):
    """Server configuration, overridable via env (``AGORA_*``) or ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="AGORA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8800

    anthropic_api_key: str | None = None
    default_model: str = "claude-sonnet-4-6"
    director_model: str = "claude-haiku-4-5-20251001"
    # "auto" picks anthropic when a key is present, otherwise the mock provider.
    provider: str = "auto"

    turn_delay: float = 1.2
    max_turns: int = 24
    max_tokens: int = 600  # Per agent message; keeps chat snappy and cheap.

    data_dir: Path = Path(".agora")
    preset: Path = DEFAULT_PRESET
    open_browser: bool = True

    def resolved_provider(self) -> str:
        """Return the concrete provider name to use by default."""
        if self.provider != "auto":
            return self.provider
        return "anthropic" if self.anthropic_api_key else "mock"


def load_preset(path: Path, default_model: str) -> tuple[list[Agent], list[Room]]:
    """Load agents and rooms from a TOML preset file.

    Returns empty lists if the file does not exist so the server can still boot.
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
        # Rooms reference agents by name in the preset for readability.
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
