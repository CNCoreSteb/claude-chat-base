from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ccb.config import Settings
from ccb.server import create_app


def _client(tmp_path):
    s = Settings(data_dir=tmp_path / "d",
                 preset=Path("nope.toml"), floor_scope="off")
    return TestClient(create_app(s))


def _setup(cl):
    room = cl.post("/api/rooms", json={"name": "大厅"}).json()["id"]
    srv = cl.post("/api/instances/connect",
                  json={"name": "server", "role": "服务器"}).json()["agent_id"]
    # 极端歧义：另一实例的职责恰好等于 @ 词 "server"
    ota = cl.post("/api/instances/connect",
                  json={"name": "tqiu_ota_server", "role": "server"}).json()["agent_id"]
    cl.post(f"/api/rooms/{room}/agents/{srv}")
    cl.post(f"/api/rooms/{room}/agents/{ota}")
    return room, srv, ota


def test_explicit_agentid_is_authoritative_no_fuzzy_leak(tmp_path):
    """显式 to/mentions(agentid) 指定时以它为准；正文 @server 不得把 role=server 的另一实例点名。"""
    with _client(tmp_path) as cl:
        room, srv, ota = _setup(cl)
        meta = cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "@server 请由你主持", "to": srv,
                             "mentions": [srv]}).json()["meta"]
        assert meta["mentions"] == [srv]          # 只有 server，ota 不泄漏
        assert ota not in meta["mentions"]
        assert meta["to"] == srv
        assert meta["to_name"] == "server"        # 桥接/GUI 可直接显示「主要发给谁」


def test_plain_text_mention_still_resolves_when_no_explicit(tmp_path):
    """没有显式 to/mentions 时，回退到正文 @ 文本解析（按名字/职责精确匹配）。"""
    with _client(tmp_path) as cl:
        room, srv, ota = _setup(cl)
        meta = cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "@server 请由你主持"}).json()["meta"]
        # @server 精确命中 name=server 与 role=server 两者（纯文本本就歧义，应让用户用自动补全消歧）
        assert srv in meta["mentions"] and ota in meta["mentions"]
        assert "to" not in meta                   # 纯文本不设主要接收者


def test_reply_to_agent_is_directed_to_that_agent(tmp_path):
    """引用回复某 agent = 定向发给它：meta.to/mentions=该 agent（只它该回答，不开应答轮）。"""
    with _client(tmp_path) as cl:
        room, srv, _ota = _setup(cl)
        sm = cl.post(f"/api/rooms/{room}/messages",
                     json={"content": "方案已定稿", "agent_id": srv}).json()
        meta = cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "你们的方案能实现什么",
                             "reply_to": sm["id"]}).json()["meta"]
        assert meta.get("to") == srv and meta.get("mentions") == [srv]
        assert meta.get("to_name") == "server"
        assert meta.get("reply_to") == sm["id"]


def test_reply_to_human_is_not_directed(tmp_path):
    """回复人类/系统消息不设 meta.to（它们不是 peer），仅保留引用块。"""
    with _client(tmp_path) as cl:
        room, _srv, _ota = _setup(cl)
        hm = cl.post(f"/api/rooms/{room}/messages", json={"content": "大家好"}).json()  # human
        meta = cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "我是说……", "reply_to": hm["id"]}).json()["meta"]
        assert "to" not in meta
        assert meta.get("reply_to") == hm["id"]


def test_explicit_to_overrides_reply_target(tmp_path):
    """同时有显式 to 和 reply_to 时，显式 to 优先。"""
    with _client(tmp_path) as cl:
        room, srv, ota = _setup(cl)
        sm = cl.post(f"/api/rooms/{room}/messages",
                     json={"content": "x", "agent_id": srv}).json()
        meta = cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "看下", "reply_to": sm["id"], "to": ota}).json()["meta"]
        assert meta.get("to") == ota and meta.get("mentions") == [ota]


def test_explicit_to_by_role_resolves_to_id(tmp_path):
    """to 也可传名字/职责，解析成 agentid；非成员/查无此人则丢弃。"""
    with _client(tmp_path) as cl:
        room, srv, ota = _setup(cl)
        meta = cl.post(f"/api/rooms/{room}/messages",
                       json={"content": "看一下", "to": "服务器"}).json()["meta"]  # 按职责
        assert meta["to"] == srv and meta["mentions"] == [srv]
        meta2 = cl.post(f"/api/rooms/{room}/messages",
                        json={"content": "hi", "to": "查无此人"}).json()["meta"]
        assert "to" not in meta2 and "mentions" not in meta2
