from __future__ import annotations

from pathlib import Path

import pytest

from ccb.config import Settings
from ccb.hub import Hub
from ccb.models import Agent, Room, Strategy
from ccb.orchestrator import Orchestrator


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # 强制使用离线提供方，确保测试永不触网。
    return Settings(provider="mock", anthropic_api_key=None, data_dir=tmp_path / "data")


@pytest.fixture
def hub(settings: Settings) -> Hub:
    h = Hub(settings)
    h.orchestrator = Orchestrator(h)
    return h


@pytest.fixture
def populated_hub(hub: Hub) -> Hub:
    a = hub.store.add_agent(Agent(name="阿工", persona="你是阿工，一名工程师。"))
    b = hub.store.add_agent(Agent(name="小设", persona="你是小设，一名设计师。"))
    room = Room(
        name="测试房间",
        topic="确定一个图标方案。",
        agent_ids=[a.id, b.id],
        strategy=Strategy.ROUND_ROBIN,
        max_turns=4,
        turn_delay=0.02,
    )
    hub.store.add_room(room)
    return hub
