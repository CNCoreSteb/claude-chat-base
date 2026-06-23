"""轻量校验独立 skill 客户端 ccb_peer.py：可加载、状态读写、参数解析正常。

完整的收发/拉群行为由针对真实服务的端到端冒烟测试覆盖（脚本走 HTTP，不经服务进程内）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skill" / "ccb-peer" / "ccb_peer.py"


def _load(monkeypatch, tmp_path):
    # 把会话状态文件指到临时目录，避免污染工作目录。
    monkeypatch.setenv("CCB_PEER_STATE", str(tmp_path / "state.json"))
    spec = importlib.util.spec_from_file_location("ccb_peer", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_state_roundtrip(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    assert mod.load_state() == {}
    mod.save_state({"agent_id": "agent_x", "name": "后端", "last_ts": 1.5})
    assert mod.load_state()["agent_id"] == "agent_x"
    assert mod.base_url({"base_url": "http://h:1"}) == "http://h:1"


def test_parser_has_all_subcommands(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    parser = mod.build_parser()
    for cmd in ["standby", "join", "connect", "create-topic", "rooms", "instances",
                "invite", "send", "wait", "read", "peers", "leave", "disconnect", "whoami"]:
        ns = parser.parse_args([cmd] + (["--room", "x"] if cmd == "join" else [])
                               + (["--target", "y"] if cmd == "invite" else [])
                               + (["--text", "z"] if cmd == "send" else [])
                               + (["--name", "n"] if cmd == "create-topic" else []))
        assert hasattr(ns, "func")


def test_require_agent_exits_without_session(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        mod.require_agent({})
