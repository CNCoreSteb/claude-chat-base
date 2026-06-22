from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ccb.config import Settings
from ccb.server import create_app


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        provider="mock",
        anthropic_api_key=None,
        data_dir=tmp_path / "data",
        preset=Path("does-not-exist.toml"),  # 以空状态启动，便于干净地测试
        open_browser=False,
    )
    return TestClient(create_app(settings))


def test_health_and_state(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/api/health").json() == {"ok": True}
        state = client.get("/api/state").json()
        assert state["type"] == "snapshot"
        assert state["server"]["provider"] == "mock"
        assert state["agents"] == []


def test_agent_room_message_flow(tmp_path):
    with _client(tmp_path) as client:
        agent = client.post("/api/agents", json={"name": "阿工", "persona": "工程师"}).json()
        room = client.post("/api/rooms", json={"name": "房间", "topic": "话题"}).json()
        client.post(f"/api/rooms/{room['id']}/agents/{agent['id']}")

        posted = client.post(
            f"/api/rooms/{room['id']}/messages", json={"content": "大家好"}
        ).json()
        assert posted["role"] == "human"

        msgs = client.get(f"/api/rooms/{room['id']}/messages").json()
        assert [m["content"] for m in msgs] == ["大家好"]

        state = client.get("/api/state").json()
        room_state = next(r for r in state["rooms"] if r["id"] == room["id"])
        assert agent["id"] in room_state["agent_ids"]


def test_peer_registration(tmp_path):
    with _client(tmp_path) as client:
        room = client.post("/api/rooms", json={"name": "房间"}).json()
        peer = client.post(
            "/api/peers", json={"room_id": room["id"], "name": "ClaudeX", "persona": "peer"}
        ).json()
        assert peer["room_id"] == room["id"]
        state = client.get("/api/state").json()
        agent = next(a for a in state["agents"] if a["id"] == peer["agent_id"])
        assert agent["kind"] == "peer"


def test_websocket_receives_snapshot_and_events(tmp_path):
    with _client(tmp_path) as client:
        with client.websocket_connect("/ws") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            client.post("/api/rooms", json={"name": "实时房间"})
            # 创建房间应被广播到该 socket。
            event = ws.receive_json()
            assert event["type"] == "room_added"
            assert event["room"]["name"] == "实时房间"
