from __future__ import annotations

import socket
import time

import httpx
from fastapi.testclient import TestClient

from ccb.config import Settings
from ccb.debug_app import create_debug_app
from ccb.hub import Hub
from ccb.models import Agent, AgentKind, Message, Room
from ccb.server import create_app


def _hub(tmp_path, **kw) -> Hub:
    return Hub(Settings(data_dir=tmp_path / "d", **kw))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_debug_state_full(tmp_path):
    h = _hub(tmp_path)
    a = h.store.add_agent(Agent(name="后端", kind=AgentKind.PEER, role="后端"))
    r = h.store.add_room(Room(name="大厅", agent_ids=[a.id]))
    h.store.add_message(Message(room_id=r.id, sender_id=a.id, sender_name="后端", content="hi"))
    with TestClient(create_debug_app(h)) as cl:
        st = cl.get("/debug/state").json()
        for k in ["now", "server", "constants", "reconcile", "websocket", "kicked",
                  "invite_locks", "agents", "rooms", "db", "messages_tail"]:
            assert k in st, f"missing section: {k}"
        # agent 派生字段
        ag = next(x for x in st["agents"] if x["name"] == "后端")
        assert "staleness" in ag and "kicked" in ag and "holds_floor_in" in ag
        assert ag["rooms"][0]["name"] == "大厅"
        # room + floor 原始计时
        rm = next(x for x in st["rooms"] if x["name"] == "大厅")
        assert rm["message_count"] == 1
        assert "opened_at" in rm["floor"] and "blocked_agents" in rm["floor"]
        # db
        assert st["db"]["total_messages"] == 1
        assert "pragma" in st["db"] and "file_sizes" in st["db"]
        assert st["db"]["pragma"]["journal_mode"].lower() == "wal"
        assert st["messages_tail"][0]["content"] == "hi"


def test_debug_ws_subscribes_and_unsubscribes(tmp_path):
    h = _hub(tmp_path)
    with TestClient(create_debug_app(h)) as cl:
        before = len(h._subscribers)
        with cl.websocket_connect("/debug/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "_debug_hello"
            assert len(h._subscribers) == before + 1  # 订阅了同一条事件总线
        # 断开后读侧探测应及时退订，不泄漏订阅（F12）
        for _ in range(50):
            if len(h._subscribers) == before:
                break
            time.sleep(0.02)
        assert len(h._subscribers) == before


def test_debug_actions(tmp_path):
    h = _hub(tmp_path)
    a = h.store.add_agent(Agent(name="x", kind=AgentKind.PEER))
    room = h.store.add_room(Room(name="大厅", agent_ids=[a.id]))
    with TestClient(create_debug_app(h)) as cl:
        r = cl.post("/debug/actions/set-floor-config",
                    json={"scope": "broadcast", "enforcement": "hard"}).json()
        assert r == {"scope": "broadcast", "enforcement": "hard"}
        assert h.floor_scope == "broadcast" and h.floor_enforcement == "hard"
        # 非法值应被拒（F13），且不改动当前配置
        assert cl.post("/debug/actions/set-floor-config",
                       json={"scope": "nonsense"}).status_code == 400
        assert cl.post("/debug/actions/set-floor-config",
                       json={"enforcement": "nope"}).status_code == 400
        assert h.floor_scope == "broadcast"

        assert cl.post("/debug/actions/kick", json={"agent_id": a.id}).json()["ok"] is True
        assert a.id in h._kicked
        cl.post("/debug/actions/clear-kick", json={"agent_id": a.id})
        assert a.id not in h._kicked

        rr = cl.post("/debug/actions/force-release-floor", json={"room_id": room.id}).json()
        assert rr["ok"] is True
        assert cl.post("/debug/actions/force-release-floor",
                       json={"room_id": "nope"}).status_code == 404


def test_debug_online_offline_and_reconcile_actions(tmp_path):
    h = _hub(tmp_path)
    a = h.store.add_agent(Agent(name="后端", kind=AgentKind.PEER))
    with TestClient(create_debug_app(h)) as cl:
        assert a.online is False
        on = cl.post("/debug/actions/set-online", json={"agent_id": a.id}).json()
        assert on["online"] is True and a.online is True
        off = cl.post("/debug/actions/set-offline", json={"agent_id": a.id}).json()
        assert off["online"] is False and a.online is False
        # 未知实例 -> 404
        assert cl.post("/debug/actions/set-online", json={"agent_id": "nope"}).status_code == 404
        # 手动 reconcile 可用并推进 tick
        before = h.reconcile_count
        assert cl.post("/debug/actions/reconcile", json={}).json()["ok"] is True
        assert h.reconcile_count == before + 1


def test_second_port_debug_server_serves_on_localhost(tmp_path):
    # 端到端：CCB_DEBUG_PORT>0 时主应用 lifespan 在独立端口（127.0.0.1）起一个调试服务。
    port = _free_port()
    settings = Settings(data_dir=tmp_path / "d", debug_port=port)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json() == {"ok": True}  # 主应用正常
        up = False
        health = f"http://127.0.0.1:{port}/debug/health"
        for _ in range(100):
            try:
                if httpx.get(health, timeout=0.5).status_code == 200:
                    up = True
                    break
            except Exception:  # noqa: BLE001 - 调试服务尚在启动，重试
                pass
            time.sleep(0.05)
        assert up, "调试服务未在独立端口起来"
        st = httpx.get(f"http://127.0.0.1:{port}/debug/state", timeout=2).json()
        assert st["server"]["debug_port"] == port
        assert st["server"]["uptime_seconds"] is not None  # 走了 main_app 注入分支
