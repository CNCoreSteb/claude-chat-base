from __future__ import annotations

from ccb.config import DEFAULT_PRESET, Settings, load_preset


def test_default_preset_has_no_preset_slots():
    # 不再预置任何"仓库槽位"——实例由 Claude Code 连接时自注册。
    agents, rooms = load_preset(DEFAULT_PRESET, default_model="claude-sonnet-4-6")
    assert agents == [], "默认预设不应包含任何预置智能体/槽位"
    # 仅保留一个默认的落脚主题。
    assert len(rooms) == 1
    assert rooms[0].agent_ids == []


def test_resolved_provider_auto_without_key():
    assert Settings(provider="auto", anthropic_api_key=None).resolved_provider() == "mock"


def test_resolved_provider_auto_with_key():
    assert Settings(provider="auto", anthropic_api_key="sk-test").resolved_provider() == "anthropic"


def test_missing_preset_returns_empty():
    from pathlib import Path

    assert load_preset(Path("nope.toml"), "m") == ([], [])
