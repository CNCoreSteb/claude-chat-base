from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ccb.config import Settings
from ccb.server import create_app


def _client(tmp_path) -> TestClient:
    s = Settings(data_dir=tmp_path / "d", preset=Path("nope.toml"), open_browser=False)
    return TestClient(create_app(s))


def _connect(cl, name, role="") -> str:
    return cl.post("/api/instances/connect", json={"name": name, "role": role}).json()["agent_id"]


def test_creator_is_host_and_todo_permissions(tmp_path):
    with _client(tmp_path) as cl:
        a = _connect(cl, "A", "后端")
        b = _connect(cl, "B", "web")
        room = cl.post("/api/rooms", json={"name": "群", "agent_ids": [a, b]}).json()
        assert room["host_id"] == a  # 首个成员（建群者）默认主持人

        # 各 agent 自己的 todo：本人可加，他人不可
        assert cl.post("/api/todos", json={"scope": "agent", "scope_id": a,
                                           "text": "我的活", "actor": a}).status_code == 200
        assert cl.post("/api/todos", json={"scope": "agent", "scope_id": a,
                                           "text": "x", "actor": b}).status_code == 403
        # 主题 todo：主持人可，非主持人不可
        assert cl.post("/api/todos", json={"scope": "room", "scope_id": room["id"],
                                           "text": "主题活", "actor": a}).status_code == 200
        assert cl.post("/api/todos", json={"scope": "room", "scope_id": room["id"],
                                           "text": "x", "actor": b}).status_code == 403
        # 全局 todo：人工可，未授权 agent 不可；授权后可
        assert cl.post("/api/todos", json={"scope": "global", "text": "全局活",
                                           "actor": "human"}).status_code == 200
        assert cl.post("/api/todos", json={"scope": "global", "text": "x",
                                           "actor": a}).status_code == 403
        cl.patch("/api/global-todo-editors", json={"editors": [a]})
        assert cl.post("/api/todos", json={"scope": "global", "text": "A的全局活",
                                           "actor": a}).status_code == 200


def test_non_host_invite_blocked_then_requested_and_approved(tmp_path):
    with _client(tmp_path) as cl:
        a = _connect(cl, "A", "后端")  # 主持人
        b = _connect(cl, "B", "web")
        c = _connect(cl, "C", "ota")
        room = cl.post("/api/rooms", json={"name": "群", "agent_ids": [a, b]}).json()["id"]
        # 非主持人直接邀请 -> 403
        assert cl.post(f"/api/rooms/{room}/invite",
                       json={"target": "ota", "by": b}).status_code == 403
        # B 投递邀请请求；server 解析 target=ota -> C
        req = cl.post("/api/requests", json={"room_id": room, "action": "invite",
                                             "requested_by": b, "target": "ota"}).json()
        assert req["status"] == "pending" and req["payload"]["target_id"] == c
        # 非主持人审批 -> 403
        assert cl.post(f"/api/requests/{req['id']}/resolve",
                       json={"approver": b, "approve": True}).status_code == 403
        # 主持人 A 批准 -> 执行邀请，C 进群
        res = cl.post(f"/api/requests/{req['id']}/resolve",
                      json={"approver": a, "approve": True}).json()
        assert res["status"] == "approved"
        rm = next(r for r in cl.get("/api/state").json()["rooms"] if r["id"] == room)
        assert c in rm["agent_ids"]


def test_request_todo_add_and_reject_close(tmp_path):
    with _client(tmp_path) as cl:
        a = _connect(cl, "A")
        b = _connect(cl, "B")
        room = cl.post("/api/rooms", json={"name": "群", "agent_ids": [a, b]}).json()["id"]
        # B 请求加主题 todo -> A 批准 -> 主题 todo 出现
        req = cl.post("/api/requests", json={"room_id": room, "action": "todo_add",
                                             "requested_by": b, "text": "B提议的活"}).json()
        cl.post(f"/api/requests/{req['id']}/resolve", json={"approver": a, "approve": True})
        todos = cl.get("/api/todos", params={"scope": "room", "scope_id": room}).json()
        assert any(t["text"] == "B提议的活" for t in todos)
        # B 请求关主题 -> A 拒绝 -> 主题仍在
        req2 = cl.post("/api/requests", json={"room_id": room, "action": "close",
                                              "requested_by": b}).json()
        res = cl.post(f"/api/requests/{req2['id']}/resolve",
                      json={"approver": a, "approve": False, "note": "先不关"}).json()
        assert res["status"] == "rejected"
        assert any(r["id"] == room for r in cl.get("/api/state").json()["rooms"])


def test_snapshot_exposes_todos_requests_editors(tmp_path):
    with _client(tmp_path) as cl:
        a = _connect(cl, "A")
        room = cl.post("/api/rooms", json={"name": "群", "agent_ids": [a]}).json()["id"]
        cl.post("/api/todos", json={"scope": "global", "text": "G", "actor": "human"})
        cl.patch("/api/global-todo-editors", json={"editors": [a]})
        snap = cl.get("/api/state").json()
        assert any(t["text"] == "G" for t in snap["todos"])
        assert snap["global_todo_editors"] == [a]
        assert "requests" in snap
        rm = next(r for r in snap["rooms"] if r["id"] == room)
        assert rm["host_id"] == a
