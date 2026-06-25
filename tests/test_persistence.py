from __future__ import annotations

import json
import sqlite3
import time

from ccb.config import Settings
from ccb.hub import PEER_STALE_SECONDS, Hub
from ccb.models import Agent, AgentCreate, AgentKind, RoomCreate
from ccb.store import Store


def _hub(tmp_path) -> Hub:
    settings = Settings(data_dir=tmp_path / "data")
    return Hub(settings)


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


def test_legacy_ai_kind_coerced_to_peer():
    # AI 功能删除后，历史里 kind="ai" 等旧值必须兼容为 peer（否则旧 .ccb 加载会 ValidationError）。
    a = Agent.model_validate({"name": "旧AI", "kind": "ai", "model": "claude-sonnet-4-6",
                              "provider": "anthropic", "temperature": 0.5, "status": "thinking"})
    assert a.kind == AgentKind.PEER
    dumped = a.model_dump()
    for gone in ("model", "provider", "temperature", "status"):
        assert gone not in dumped, f"已删字段 {gone} 不应再出现在 Agent 上"
    assert AgentCreate.model_validate({"name": "x", "kind": "ai"}).kind == AgentKind.PEER
    assert not hasattr(AgentKind, "AI")


def test_legacy_ai_era_db_rows_load_and_coerce(tmp_path):
    # 模拟一个「AI 时代」的旧数据库：agents/rooms 的 data 列里带有现已删除的字段。
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "ccb.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.execute("CREATE TABLE rooms (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, room_id TEXT, sender_id TEXT,"
        " sender_name TEXT, role TEXT, content TEXT, ts REAL, color TEXT, meta TEXT)"
    )
    conn.execute("INSERT INTO agents VALUES(?,?)", ("agent_old", json.dumps({
        "id": "agent_old", "name": "旧AI", "kind": "ai", "role": "后端",
        "model": "claude-sonnet-4-6", "provider": "anthropic", "temperature": 0.7,
        "status": "speaking", "color": "#fff", "enabled": True,
    })))
    conn.execute("INSERT INTO rooms VALUES(?,?)", ("room_old", json.dumps({
        "id": "room_old", "name": "旧厅", "topic": "t", "agent_ids": ["agent_old"],
        "strategy": "director", "status": "running", "max_turns": 18, "turn_delay": 1.2, "turn": 3,
    })))
    conn.commit()
    conn.close()

    store = Store(data_dir)  # _load_state 必须能加载这些旧行而不崩
    a = store.get_agent("agent_old")
    assert a is not None and a.kind == AgentKind.PEER and a.role == "后端"
    r = store.get_room("room_old")
    assert r is not None and r.name == "旧厅" and r.agent_ids == ["agent_old"]
    store.close()
