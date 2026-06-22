from __future__ import annotations

from agora.config import DEFAULT_PRESET, Settings, load_preset


def test_default_preset_loads_agents_and_rooms():
    agents, rooms = load_preset(DEFAULT_PRESET, default_model="claude-sonnet-4-6")
    assert {a.name for a in agents} >= {"Ada", "Theo", "Mira", "Kai"}
    assert rooms, "expected at least one preset room"
    room = rooms[0]
    # Every room agent reference should resolve to a real agent id.
    agent_ids = {a.id for a in agents}
    assert set(room.agent_ids) <= agent_ids
    # Agents inherit the default model when the preset omits one.
    assert all(a.model == "claude-sonnet-4-6" for a in agents)


def test_resolved_provider_auto_without_key():
    assert Settings(provider="auto", anthropic_api_key=None).resolved_provider() == "mock"


def test_resolved_provider_auto_with_key():
    assert Settings(provider="auto", anthropic_api_key="sk-test").resolved_provider() == "anthropic"


def test_missing_preset_returns_empty():
    from pathlib import Path

    assert load_preset(Path("nope.toml"), "m") == ([], [])
