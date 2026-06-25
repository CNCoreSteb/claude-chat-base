from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ccb.config import Settings
from ccb.server import create_app


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
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
        assert state["server"]["floor_scope"] == "human"
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


def test_wait_returns_existing_messages_quickly(tmp_path):
    with _client(tmp_path) as client:
        room = client.post("/api/rooms", json={"name": "房间"}).json()
        client.post(f"/api/rooms/{room['id']}/messages", json={"content": "先发一条"})
        # since=0 -> 已有的消息应立即返回，不必等满 timeout。
        msgs = client.get(f"/api/rooms/{room['id']}/wait", params={"since": 0, "timeout": 5}).json()
        assert [m["content"] for m in msgs] == ["先发一条"]


def test_peer_join_claims_existing_slot(tmp_path):
    with _client(tmp_path) as client:
        # 在 GUI 里预先建好一个仓库 peer 槽位。
        slot = client.post(
            "/api/agents", json={"name": "后端", "kind": "peer", "role": "后端"}
        ).json()
        room = client.post("/api/rooms", json={"name": "协同", "agent_ids": [slot["id"]]}).json()
        assert slot["online"] is False

        # 真实 peer 以同名加入 -> 应认领该槽位（而不是新建）并标记在线。
        res = client.post(
            "/api/peers", json={"room_id": room["id"], "name": "后端", "repo_path": "/repos/api"}
        ).json()
        assert res["claimed"] is True
        assert res["agent_id"] == slot["id"]

        state = client.get("/api/state").json()
        peers = [a for a in state["agents"] if a["kind"] == "peer"]
        assert len(peers) == 1  # 没有产生重复槽位
        assert peers[0]["online"] is True
        assert peers[0]["repo_path"] == "/repos/api"


def test_instance_connect_and_discovery(tmp_path):
    with _client(tmp_path) as client:
        a = client.post(
            "/api/instances/connect", json={"name": "后端", "role": "后端"}
        ).json()
        assert a["claimed"] is False
        # 同名再次连接 -> 认领，不产生重复实例。
        again = client.post(
            "/api/instances/connect", json={"name": "后端", "role": "后端"}
        ).json()
        assert again["claimed"] is True and again["agent_id"] == a["agent_id"]

        online = client.get("/api/instances", params={"online": True}).json()
        assert [i["name"] for i in online] == ["后端"]
        assert online[0]["online"] is True


def test_invite_pulls_instance_by_role(tmp_path):
    with _client(tmp_path) as client:
        # 两个实例上线，"后端" 建一个主题。
        backend = client.post(
            "/api/instances/connect", json={"name": "后端", "role": "后端"}
        ).json()
        client.post("/api/instances/connect", json={"name": "web端", "role": "web端"})
        room = client.post(
            "/api/rooms", json={"name": "发布协调", "agent_ids": [backend["agent_id"]]}
        ).json()

        # 后端按职责把 web端 拉进来。
        res = client.post(
            f"/api/rooms/{room['id']}/invite",
            json={"target": "web端", "by": backend["agent_id"]},
        ).json()

        state = client.get("/api/state").json()
        room_state = next(r for r in state["rooms"] if r["id"] == room["id"])
        assert res["agent_id"] in room_state["agent_ids"]
        # 应产生一条系统提示消息。
        msgs = client.get(f"/api/rooms/{room['id']}/messages").json()
        assert any(m["role"] == "system" and "web端" in m["content"] for m in msgs)


def test_heartbeat_marks_peer_online(tmp_path):
    with _client(tmp_path) as client:
        # 心跳由桥接进程后台发送（零 token），用于维持在线状态。
        peer = client.post("/api/agents", json={"name": "x", "kind": "peer"}).json()
        assert peer["online"] is False
        client.post(f"/api/peers/{peer['id']}/heartbeat")
        state = client.get("/api/state").json()
        a = next(x for x in state["agents"] if x["id"] == peer["id"])
        assert a["online"] is True


def test_kick_forces_offline_and_blocks_revival(tmp_path):
    with _client(tmp_path) as client:
        peer = client.post("/api/agents", json={"name": "后端", "kind": "peer"}).json()
        # 心跳 -> 在线
        assert client.post(f"/api/peers/{peer['id']}/heartbeat").json() == {"ok": True}
        state = client.get("/api/state").json()
        assert next(a for a in state["agents"] if a["id"] == peer["id"])["online"] is True

        # 踢掉 -> 立即离线
        assert client.post(f"/api/peers/{peer['id']}/kick").json()["ok"] is True
        state = client.get("/api/state").json()
        assert next(a for a in state["agents"] if a["id"] == peer["id"])["online"] is False

        # 被踢后心跳不再复活，并返回 kicked 信号通知桥接停止
        assert client.post(f"/api/peers/{peer['id']}/heartbeat").json() == {
            "ok": False, "kicked": True
        }
        state = client.get("/api/state").json()
        assert next(a for a in state["agents"] if a["id"] == peer["id"])["online"] is False

        # wait 也立即返回 kicked 通知，让待命的实例据此停止
        waited = client.get(
            f"/api/instances/{peer['id']}/wait", params={"since": 0, "timeout": 1}
        ).json()
        assert waited and waited[0]["meta"]["kicked"] is True

        # 主动重连（同名 connect）-> 撤销踢出，可再次在线
        again = client.post("/api/instances/connect", json={"name": "后端"}).json()
        assert again["agent_id"] == peer["id"]
        assert client.post(f"/api/peers/{peer['id']}/heartbeat").json() == {"ok": True}
        state = client.get("/api/state").json()
        assert next(a for a in state["agents"] if a["id"] == peer["id"])["online"] is True


def test_connect_then_join_reuses_same_instance(tmp_path):
    with _client(tmp_path) as client:
        # 实例先 connect 全局上线，再 join_room 加入房间——应复用同一实例，不产生重复。
        c = client.post(
            "/api/instances/connect", json={"name": "tqiu", "role": "后端"}
        ).json()
        room = client.post("/api/rooms", json={"name": "大厅"}).json()
        j = client.post(
            "/api/peers",
            json={"room_id": room["id"], "name": "tqiu", "repo_path": "/x"},
        ).json()
        assert j["claimed"] is True
        assert j["agent_id"] == c["agent_id"]

        state = client.get("/api/state").json()
        peers = [a for a in state["agents"] if a["kind"] == "peer"]
        assert len(peers) == 1, "同名实例 connect+join 不应产生重复"
        room_state = next(r for r in state["rooms"] if r["id"] == room["id"])
        assert c["agent_id"] in room_state["agent_ids"]


def test_instance_wait_is_cross_room(tmp_path):
    with _client(tmp_path) as client:
        inst = client.post(
            "/api/instances/connect", json={"name": "后端", "role": "后端"}
        ).json()
        r1 = client.post("/api/rooms", json={"name": "群1", "agent_ids": [inst["agent_id"]]}).json()
        r2 = client.post("/api/rooms", json={"name": "群2", "agent_ids": [inst["agent_id"]]}).json()
        client.post(f"/api/rooms/{r1['id']}/messages", json={"content": "来自群1"})
        client.post(f"/api/rooms/{r2['id']}/messages", json={"content": "来自群2"})

        got = client.get(
            f"/api/instances/{inst['agent_id']}/messages", params={"since": 0}
        ).json()
        contents = {m["content"] for m in got}
        assert {"来自群1", "来自群2"} <= contents
        # 每条消息都带上所属主题名。
        assert all("room_name" in m for m in got)


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


def test_ai_orchestration_endpoints_removed(tmp_path):
    # AI 自动对话已删除：编排端点应不存在（404/405）；清空(reset)仍保留（F7）。
    with _client(tmp_path) as client:
        room = client.post("/api/rooms", json={"name": "x"}).json()["id"]
        for action in ("start", "pause", "stop"):
            assert client.post(f"/api/rooms/{room}/{action}").status_code in (404, 405)
        assert client.post(f"/api/rooms/{room}/reset").status_code == 200


def test_snapshot_server_has_no_ai_fields(tmp_path):
    with _client(tmp_path) as client:
        srv = client.get("/api/state").json()["server"]
        for gone in ("provider", "default_model", "director_model", "has_api_key"):
            assert gone not in srv, f"snapshot.server 不该再有 {gone}"
        assert "floor_scope" in srv and "floor_enforcement" in srv


def test_settings_has_no_ai_fields():
    fields = set(Settings.model_fields)
    for gone in ("provider", "anthropic_api_key", "default_model", "director_model",
                 "turn_delay", "max_turns", "max_tokens"):
        assert gone not in fields, f"Settings 不该再有 AI 字段 {gone}"
