from __future__ import annotations

from ccb.config import DEFAULT_PRESET, Settings, load_preset


def test_default_preset_loads_agents_and_rooms():
    agents, rooms = load_preset(DEFAULT_PRESET, default_model="claude-sonnet-4-6")
    assert {a.name for a in agents} >= {"阿工", "小创", "阿慎", "小和"}
    assert rooms, "预设里应至少有一个房间"
    room = rooms[0]
    # 房间引用的每个智能体都应能解析为真实的 agent id。
    agent_ids = {a.id for a in agents}
    assert set(room.agent_ids) <= agent_ids
    # 预设未指定模型时，智能体应继承默认模型。
    assert all(a.model == "claude-sonnet-4-6" for a in agents)


def test_resolved_provider_auto_without_key():
    assert Settings(provider="auto", anthropic_api_key=None).resolved_provider() == "mock"


def test_resolved_provider_auto_with_key():
    assert Settings(provider="auto", anthropic_api_key="sk-test").resolved_provider() == "anthropic"


def test_missing_preset_returns_empty():
    from pathlib import Path

    assert load_preset(Path("nope.toml"), "m") == ([], [])
