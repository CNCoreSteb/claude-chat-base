from __future__ import annotations

import time

from ccb.config import Settings
from ccb.hub import PEER_STALE_SECONDS, Hub
from ccb.models import Agent, AgentCreate, AgentKind, RoomCreate
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
    h1.store.close()

    # 用同一数据目录新建 Hub 并 bootstrap，应从 SQLite 恢复刚才的配置。
    h2 = _hub(tmp_path)
    h2.bootstrap()
    restored = next((a for a in h2.store.agents.values() if a.name == "后端"), None)
    assert restored is not None
    assert restored.kind == AgentKind.PEER
    assert restored.repo_path == "/repos/api"
    assert any(r.name == "协同" for r in h2.store.rooms.values())


async def test_bootstrap_uses_preset_when_db_empty(tmp_path):
    h = _hub(tmp_path)
    h.bootstrap()  # 空库 -> 导入预设（仅一个默认主题，无预置槽位）
    assert h.store.rooms, "应从预设导入默认主题"
    assert not h.store.agents, "默认预设不应预置任何实例"
    # 重新打开应直接命中数据库，而不是再次导入。
    h.store.close()
    h2 = _hub(tmp_path)
    n_before = len(h2.store.rooms)
    h2.bootstrap()
    assert len(h2.store.rooms) == n_before


async def test_runtime_fields_reset_after_reopen(tmp_path):
    h = _hub(tmp_path)
    a = h.store.add_agent(Agent(name="后端", kind=AgentKind.PEER))
    await h.mark_peer_seen(a.id)
    assert a.online is True
    h.store.close()
    # online/last_seen 是运行期字段，不持久化；重开后应回到离线。
    h2 = _hub(tmp_path)
    restored = next(x for x in h2.store.agents.values() if x.name == "后端")
    assert restored.online is False


async def test_peer_online_marking_and_reconcile(tmp_path):
    h = _hub(tmp_path)
    peer = h.store.add_agent(Agent(name="后端", kind=AgentKind.PEER))
    assert peer.online is False
    await h.mark_peer_seen(peer.id)
    assert peer.online is True

    peer.last_seen = time.time() - PEER_STALE_SECONDS - 5
    await h.reconcile_peers()
    assert peer.online is False
