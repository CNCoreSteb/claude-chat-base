from __future__ import annotations

from pathlib import Path

import pytest

from ccb.config import Settings
from ccb.hub import Hub
from ccb.models import Agent, Room


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """测试必须 hermetic：不读开发者本地 .env，否则其中的 CCB_DEBUG_PORT 等会污染测试、
    甚至让每个 create_app 都去抢占同一个调试端口而互相绑定失败。"""
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def hub(settings: Settings) -> Hub:
    return Hub(settings)


@pytest.fixture
def populated_hub(hub: Hub) -> Hub:
    a = hub.store.add_agent(Agent(name="阿工", role="后端"))
    b = hub.store.add_agent(Agent(name="小设", role="设计"))
    hub.store.add_room(Room(name="测试房间", topic="确定一个图标方案。", agent_ids=[a.id, b.id]))
    return hub
