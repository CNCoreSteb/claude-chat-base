from __future__ import annotations

import time

from ccb.config import Settings
from ccb.hub import PEER_STALE_SECONDS, Hub
from ccb.models import Agent, AgentCreate, AgentKind, Room, RoomCreate
from ccb.orchestrator import Orchestrator


def _hub(tmp_path) -> Hub:
    settings = Settings(provider="mock", anthropic_api_key=None, data_dir=tmp_path / "data")
    h = Hub(settings)
    h.orchestrator = Orchestrator(h)
    return h


async def test_gui_config_persists_across_restart(tmp_path):
    h1 = _hub(tmp_path)
    agent = await h1.create_agent(
        AgentCreate(name="后端", kind=AgentKind.PEER, role="后端", repo_path="/repos/api")
    )
    await h1.create_room(RoomCreate(name="协同", agent_ids=[agent.id]))
    assert h1.config_path.exists()

    # 用同一数据目录新建 Hub 并 bootstrap，应恢复刚才的配置。
    h2 = _hub(tmp_path)
    h2.bootstrap()
    names = {a.name for a in h2.store.agents.values()}
    assert "后端" in names
    restored = next(a for a in h2.store.agents.values() if a.name == "后端")
    assert restored.kind == AgentKind.PEER
    assert restored.repo_path == "/repos/api"
    assert any(r.name == "协同" for r in h2.store.rooms.values())


async def test_bootstrap_uses_preset_when_no_config(tmp_path):
    h = _hub(tmp_path)
    h.bootstrap()  # 没有 config.json -> 用预设并写盘
    assert h.config_path.exists()
    assert h.store.agents, "应从预设加载到参与者"


async def test_saved_config_excludes_runtime_fields(tmp_path):
    h = _hub(tmp_path)
    a = h.store.add_agent(Agent(name="后端", kind=AgentKind.PEER, online=True, last_seen=123.0))
    h.store.add_room(Room(name="r", agent_ids=[a.id]))
    h.save_config()
    import json

    data = json.loads(h.config_path.read_text(encoding="utf-8"))
    assert "online" not in data["agents"][0]
    assert "last_seen" not in data["agents"][0]
    assert "status" not in data["agents"][0]
    assert "turn" not in data["rooms"][0]


async def test_peer_online_marking_and_reconcile(tmp_path):
    h = _hub(tmp_path)
    peer = h.store.add_agent(Agent(name="后端", kind=AgentKind.PEER))
    assert peer.online is False
    await h.mark_peer_seen(peer.id)
    assert peer.online is True

    # 把 last_seen 设为很久以前，reconcile 应将其标记为离线。
    peer.last_seen = time.time() - PEER_STALE_SECONDS - 5
    await h.reconcile_peers()
    assert peer.online is False
