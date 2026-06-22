from __future__ import annotations

from pathlib import Path

import pytest

from agora.config import Settings
from agora.hub import Hub
from agora.models import Agent, Room, Strategy
from agora.orchestrator import Orchestrator


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Force the offline provider so tests never touch the network.
    return Settings(provider="mock", anthropic_api_key=None, data_dir=tmp_path / "data")


@pytest.fixture
def hub(settings: Settings) -> Hub:
    h = Hub(settings)
    h.orchestrator = Orchestrator(h)
    return h


@pytest.fixture
def populated_hub(hub: Hub) -> Hub:
    a = hub.store.add_agent(Agent(name="Ada", persona="You are Ada, an engineer."))
    b = hub.store.add_agent(Agent(name="Theo", persona="You are Theo, a designer."))
    room = Room(
        name="Test Room",
        topic="Decide on a logo.",
        agent_ids=[a.id, b.id],
        strategy=Strategy.ROUND_ROBIN,
        max_turns=4,
        turn_delay=0.02,
    )
    hub.store.add_room(room)
    return hub
