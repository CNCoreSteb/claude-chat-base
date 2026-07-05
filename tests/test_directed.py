from __future__ import annotations

from ccb.config import Settings
from ccb.hub import DIRECTED_HOLD_TTL, Hub


def _hub(tmp_path, directed="all") -> Hub:
    return Hub(Settings(data_dir=tmp_path / "d", directed_visibility=directed))


def _msg(mid, sender, ts, to=None, mentions=None, room="r1"):
    meta = {}
    if to:
        meta["to"] = to
    if mentions:
        meta["mentions"] = mentions
    return {"id": mid, "room_id": room, "sender_id": sender, "ts": ts, "meta": meta}


def _ids(msgs):
    return [m["id"] for m in msgs]


def test_directed_all_visible_to_everyone(tmp_path):
    h = _hub(tmp_path, "all")
    msgs = [_msg("m1", "you", 1.0, to="X")]
    assert _ids(h.filter_visible(msgs, "Y", 100.0)) == ["m1"]  # 非接收者也能看到


def test_directed_recipient_only(tmp_path):
    h = _hub(tmp_path, "recipient")
    msgs = [_msg("m1", "you", 1.0, to="X", mentions=["Z"])]
    assert _ids(h.filter_visible(msgs, "X", 100.0)) == ["m1"]     # 接收者可见
    assert _ids(h.filter_visible(msgs, "Z", 100.0)) == ["m1"]     # 被 @ 者可见
    assert _ids(h.filter_visible(msgs, "you", 100.0)) == ["m1"]   # 发送者可见
    assert h.filter_visible(msgs, "Y", 100.0) == []               # 其它实例看不到
    # 非定向消息不受影响
    assert _ids(h.filter_visible([_msg("p1", "you", 1.0)], "Y", 100.0)) == ["p1"]


def test_directed_until_reply(tmp_path):
    h = _hub(tmp_path, "until_reply")
    # 接收者未回复 -> 其它实例看不到；接收者本人可见
    held = [_msg("m1", "you", 10.0, to="X")]
    assert h.filter_visible(held, "Y", 11.0) == []
    assert _ids(h.filter_visible(held, "X", 11.0)) == ["m1"]
    # 接收者在本批里回复了 -> 解禁，其它实例也看到（含回复本身）
    answered = [_msg("m1", "you", 10.0, to="X"), _msg("r1", "X", 12.0)]
    assert set(_ids(h.filter_visible(answered, "Y", 13.0))) == {"m1", "r1"}
    # 接收者一直不回，但超过 TTL -> 兜底解禁
    assert _ids(h.filter_visible(held, "Y", 10.0 + DIRECTED_HOLD_TTL + 1)) == ["m1"]


def test_set_directed_visibility(tmp_path):
    h = _hub(tmp_path)
    assert h.directed_visibility == "all"
    h.set_floor_config(directed="recipient")
    assert h.directed_visibility == "recipient"
    h.set_floor_config(directed="不合法")  # 非法值忽略
    assert h.directed_visibility == "recipient"
    # snapshot 暴露该配置
    assert h.snapshot()["server"]["directed_visibility"] == "recipient"
