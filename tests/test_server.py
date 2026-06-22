from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agora.config import Settings
from agora.server import create_app


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        provider="mock",
        anthropic_api_key=None,
        data_dir=tmp_path / "data",
        preset=Path("does-not-exist.toml"),  # start empty for a clean test
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
        agent = client.post("/api/agents", json={"name": "Ada", "persona": "engineer"}).json()
        room = client.post("/api/rooms", json={"name": "R", "topic": "t"}).json()
        client.post(f"/api/rooms/{room['id']}/agents/{agent['id']}")

        posted = client.post(
            f"/api/rooms/{room['id']}/messages", json={"content": "hello team"}
        ).json()
        assert posted["role"] == "human"

        msgs = client.get(f"/api/rooms/{room['id']}/messages").json()
        assert [m["content"] for m in msgs] == ["hello team"]

        state = client.get("/api/state").json()
        room_state = next(r for r in state["rooms"] if r["id"] == room["id"])
        assert agent["id"] in room_state["agent_ids"]


def test_peer_registration(tmp_path):
    with _client(tmp_path) as client:
        room = client.post("/api/rooms", json={"name": "R"}).json()
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
            client.post("/api/rooms", json={"name": "Live"})
            # The room creation should be broadcast to the socket.
            event = ws.receive_json()
            assert event["type"] == "room_added"
            assert event["room"]["name"] == "Live"
