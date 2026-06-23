#!/usr/bin/env python3
"""CCB 仓库 peer 客户端（独立、仅用标准库）。

让一个 Claude Code 实例**无需 MCP**、仅凭 HTTP 就能加入 CCB 多仓库群聊：自注册、
收发消息、按职责拉别的实例进群。把本目录（含 SKILL.md）丢进仓库的 .claude/skills/
即可，Agent 会照 SKILL.md 运行这个脚本。

会话状态（agent_id / 当前主题 / 上次读取时间）保存在一个本地 JSON 文件里，所以多次
独立调用之间能延续。默认服务地址 http://127.0.0.1:8800，可用 CCB_URL 覆盖。

用法示例：
    python ccb_peer.py join --room 大厅 --name 后端 --role 后端
    python ccb_peer.py wait
    python ccb_peer.py send --text "v2 接口下周改造，请各端评估"
    python ccb_peer.py invite --target web端
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("CCB_URL", "http://127.0.0.1:8800").rstrip("/")
STATE_PATH = os.environ.get("CCB_PEER_STATE", os.path.join(os.getcwd(), ".ccb-peer.json"))


# ----- 会话状态 --------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def base_url(state: dict) -> str:
    return state.get("base_url") or DEFAULT_URL


# ----- HTTP ------------------------------------------------------------------

def request(method: str, url: str, body: dict | None = None, timeout: float = 35.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        die(f"请求失败 {e.code}：{detail or e.reason}")
    except urllib.error.URLError as e:
        die(f"无法连接 CCB（{url}）：{e.reason}。请确认服务已启动（uv run ccb）。")


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


def resolve_room(state: dict, ref: str) -> dict | None:
    snap = request("GET", base_url(state) + "/api/state")
    return next(
        (r for r in snap.get("rooms", []) if r["id"] == ref or r["name"] == ref), None
    )


def require_agent(state: dict) -> str:
    aid = state.get("agent_id")
    if not aid:
        die("尚未上线。请先运行：connect 或 join。")
    return aid


# ----- 子命令 ----------------------------------------------------------------

def cmd_connect(args) -> None:
    state = load_state()
    state["base_url"] = args.url or base_url(state)
    name = args.name or os.path.basename(os.getcwd())
    res = request(
        "POST",
        base_url(state) + "/api/instances/connect",
        {"name": name, "role": args.role, "repo_path": args.repo or os.getcwd()},
    )
    state.update(agent_id=res["agent_id"], name=name, role=args.role, last_ts=0.0)
    save_state(state)
    how = "认领了已有身份" if res.get("claimed") else "新建了身份"
    print(f"已上线：{name}（{args.role or '未注明职责'}），{how}。")


def cmd_join(args) -> None:
    state = load_state()
    state["base_url"] = args.url or base_url(state)
    name = args.name or state.get("name") or os.path.basename(os.getcwd())
    room = resolve_room(state, args.room)
    if not room:
        die(f"未找到主题「{args.room}」。可用 rooms 查看，或用 create-topic 新建。")
    res = request(
        "POST",
        base_url(state) + "/api/peers",
        {
            "room_id": room["id"],
            "name": name,
            "role": args.role,
            "repo_path": args.repo or os.getcwd(),
        },
    )
    state.update(agent_id=res["agent_id"], name=name, role=args.role,
                 active_room=room["id"], last_ts=state.get("last_ts", 0.0))
    save_state(state)
    how = "认领了已配置的槽位" if res.get("claimed") else "加入"
    print(f"已以「{name}」{how}主题「{room['name']}」，并设为当前主题。")


def cmd_standby(args) -> None:
    """进入待命：自注册到主题（缺省"大厅"），并提示进入 wait 轮询循环。"""
    state = load_state()
    state["base_url"] = args.url or base_url(state)
    name = args.name or os.path.basename(os.getcwd()) or "Peer"
    room = resolve_room(state, args.room)
    if not room:
        r = request("POST", base_url(state) + "/api/rooms", {"name": args.room})
        room = {"id": r["id"], "name": r["name"]}
    res = request(
        "POST",
        base_url(state) + "/api/peers",
        {"room_id": room["id"], "name": name, "role": args.role, "repo_path": os.getcwd()},
    )
    state.update(agent_id=res["agent_id"], name=name, role=args.role,
                 active_room=room["id"], last_ts=state.get("last_ts", 0.0))
    save_state(state)
    print(
        f"已进入待命：以「{name}」加入主题「{room['name']}」（仓库 {os.getcwd()}）。\n"
        "现在进入待命循环：反复执行 `wait`（长轮询，期间几乎不耗 token）。返回后——被点名"
        "（消息带 ‹@你·被点名›）时先 `send --text \"收到，正在处理\" --reply-to <该消息id>` 回执，"
        "再读改本仓库代码用 `send --text` 给结果；与你无关的忽略，继续 `wait`。\n"
        "要征求用户意见时用 `ask --text \"问题\"`：它把问题发到群里（GUI 高亮\"等你回答\"）并就地"
        "等用户回复，期间你始终在线——**不要**用 AskUserQuestion、也**不要**结束回合去问本地用户"
        "（那等于擅自退出待命）。待命期间你的「用户」就是 CCB 群里(GUI 旁)的人。\n"
        "用户说「退出待命」时执行 `disconnect`。"
    )


def cmd_create_topic(args) -> None:
    state = load_state()
    aid = require_agent(state)
    res = request(
        "POST",
        base_url(state) + "/api/rooms",
        {"name": args.name, "topic": args.topic, "agent_ids": [aid]},
    )
    state["active_room"] = res["id"]
    save_state(state)
    print(f"已创建主题「{args.name}」并设为当前主题。")


def cmd_rooms(args) -> None:
    state = load_state()
    snap = request("GET", base_url(state) + "/api/state")
    cur = state.get("active_room")
    rooms = snap.get("rooms", [])
    if not rooms:
        print("（暂无主题）")
        return
    for r in rooms:
        mark = "* " if r["id"] == cur else "- "
        print(f"{mark}{r['name']}（{len(r['agent_ids'])} 名参与者）话题：{r.get('topic', '')}")


def cmd_instances(args) -> None:
    state = load_state()
    data = request("GET", base_url(state) + "/api/instances")
    if not data:
        print("（暂无已连接实例）")
        return
    me = state.get("agent_id")
    for a in data:
        on = "在线" if a["online"] else "离线"
        rooms = "、".join(r["name"] for r in a.get("rooms", [])) or "（不在任何主题）"
        tag = "（你）" if a["id"] == me else ""
        print(f"- {a['name']}{tag}｜职责：{a.get('role') or '?'}｜{on}｜主题：{rooms}")


def cmd_invite(args) -> None:
    state = load_state()
    aid = require_agent(state)
    ref = args.topic or state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定，或先 join/create-topic。")
    room = resolve_room(state, ref)
    if not room and args.topic:
        res = request("POST", base_url(state) + "/api/rooms",
                      {"name": args.topic, "agent_ids": [aid]})
        room = {"id": res["id"], "name": res["name"]}
        state["active_room"] = res["id"]
        save_state(state)
    if not room:
        die(f"未找到主题「{ref}」。")
    res = request("POST", base_url(state) + f"/api/rooms/{room['id']}/invite",
                  {"target": args.target, "by": aid})
    print(f"已把「{res['name']}」拉进主题「{room['name']}」。")


def cmd_send(args) -> None:
    state = load_state()
    aid = require_agent(state)
    ref = args.topic or state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定，或先 join/create-topic。")
    room = resolve_room(state, ref)
    if not room:
        die(f"未找到主题「{ref}」。")
    request("POST", base_url(state) + f"/api/rooms/{room['id']}/messages",
            {"content": args.text, "agent_id": aid, "reply_to": args.reply_to})
    print(f"已发送到「{room['name']}」。")


def cmd_ask(args) -> None:
    """在群里向用户提问并就地等待答复——待命期间想征求用户意见时用它，别退出循环去问本地用户。"""
    state = load_state()
    aid = require_agent(state)
    ref = args.topic or state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定，或先 join/create-topic。")
    room = resolve_room(state, ref)
    if not room:
        die(f"未找到主题「{ref}」。")
    request("POST", base_url(state) + f"/api/rooms/{room['id']}/messages",
            {"content": args.text, "agent_id": aid, "is_question": True})
    seen_other: list = []
    for _ in range(max(1, int(args.timeout / 25))):
        since = state.get("last_ts", 0.0)
        url = base_url(state) + f"/api/instances/{aid}/wait?since={since}&timeout=25"
        msgs = request("GET", url, timeout=35)
        if not msgs:
            continue
        state["last_ts"] = max(m["ts"] for m in msgs)
        save_state(state)
        humans = [m for m in msgs if m.get("sender_id") == "human"]
        seen_other += [m for m in msgs if m.get("sender_id") not in ("human", aid)]
        if humans:
            ans = "\n".join(f"{m['sender_name']}: {m['content']}" for m in humans)
            print(f"用户已回复：\n{ans}{_ask_context(seen_other)}\n"
                  "（已得到答复。处理完后请立刻继续 `wait` 保持待命。）")
            return
    print("（用户暂未回复——你仍在待命、并未离线。可再次 ask 继续等，或先 `wait` 跟进其它消息。）"
          f"{_ask_context(seen_other)}")


def _ask_context(others: list) -> str:
    if not others:
        return ""
    ctx = "\n".join(f"{m['sender_name']}: {m['content']}" for m in others)
    return f"\n（等待期间群里其他发言：\n{ctx}）"


def _print_messages(state: dict, msgs: list, is_wait: bool = False) -> None:
    if not msgs:
        print("（没有新消息）")
        return
    state["last_ts"] = max(m["ts"] for m in msgs)
    save_state(state)
    me = state.get("agent_id")
    mentioned_any = False
    for m in msgs:
        you = "（你）" if m["sender_id"] == me else ""
        room = f"[{m.get('room_name', '')}] " if m.get("room_name") else ""
        meta = m.get("meta") or {}
        tag = ""
        if me in (meta.get("mentions") or []) and m["sender_id"] != me:
            tag = " ‹@你·被点名›"
            mentioned_any = True
        quote = ""
        if meta.get("reply_to_sender"):
            quote = f"（↩ 回复 {meta['reply_to_sender']}：{meta.get('reply_to_preview', '')}）"
        print(f"{room}{m['sender_name']}{you}{tag} «{m['id']}»{quote}: {m['content']}")
    if mentioned_any:
        print(
            '\n⚠️ 你被点名（‹被点名›）：请先 `send --text "收到，正在处理" '
            "--reply-to <被点名那条的 «id»>` 回执（让对方认出你在回应哪条），再着手处理。"
        )
    if is_wait:
        print(
            '\n— 待命提醒：处理完请立刻再次 `wait` 保持在线；需要征求用户意见时用 '
            '`ask --text "问题"`（在群里问并就地等回复），切勿用 AskUserQuestion 或结束回合。'
        )


def cmd_wait(args) -> None:
    state = load_state()
    aid = require_agent(state)
    since = state.get("last_ts", 0.0)
    url = (base_url(state) + f"/api/instances/{aid}/wait"
           f"?since={since}&timeout={args.timeout}")
    msgs = request("GET", url, timeout=args.timeout + 10)
    _print_messages(state, msgs, is_wait=True)


def cmd_read(args) -> None:
    state = load_state()
    aid = require_agent(state)
    since = state.get("last_ts", 0.0)
    msgs = request("GET", base_url(state) + f"/api/instances/{aid}/messages?since={since}")
    _print_messages(state, msgs)


def cmd_peers(args) -> None:
    state = load_state()
    ref = args.topic or state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定。")
    snap = request("GET", base_url(state) + "/api/state")
    room = next((r for r in snap["rooms"] if r["id"] == ref or r["name"] == ref), None)
    if not room:
        die(f"未找到主题「{ref}」。")
    agents = {a["id"]: a for a in snap["agents"]}
    for aid in room["agent_ids"]:
        a = agents.get(aid)
        if not a:
            continue
        on = "在线" if a.get("online") else ("—" if a["kind"] != "peer" else "离线")
        tag = f"{a.get('role')}/" if a.get("role") else ""
        print(f"- {a['name']}（{tag}{a['kind']}，{on}）")


def cmd_leave(args) -> None:
    state = load_state()
    aid = require_agent(state)
    ref = args.topic or state.get("active_room")
    room = resolve_room(state, ref) if ref else None
    if not room:
        die("未找到要退出的主题。")
    request("DELETE", base_url(state) + f"/api/rooms/{room['id']}/agents/{aid}")
    if state.get("active_room") == room["id"]:
        state["active_room"] = None
        save_state(state)
    print(f"已退出主题「{room['name']}」。")


def cmd_disconnect(args) -> None:
    state = load_state()
    aid = state.get("agent_id")
    if aid:
        request("POST", base_url(state) + f"/api/peers/{aid}/leave")
    save_state({"base_url": base_url(state)})
    print("已下线。")


def cmd_whoami(args) -> None:
    state = load_state()
    print(json.dumps(
        {k: state.get(k) for k in ("base_url", "agent_id", "name", "role", "active_room")},
        ensure_ascii=False,
    ))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ccb_peer", description="CCB 仓库 peer 客户端（无需 MCP）")
    p.add_argument("--url", help=f"CCB 服务地址（默认 {DEFAULT_URL}）")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("standby", help="进入待命：自注册到主题（缺省大厅），随后进入 wait 轮询循环")
    c.add_argument("--room", default="大厅"); c.add_argument("--name"); c.add_argument("--role", default="")
    c.set_defaults(func=cmd_standby)

    c = sub.add_parser("connect", help="全局上线（自注册，不进任何主题）")
    c.add_argument("--name"); c.add_argument("--role", default=""); c.add_argument("--repo")
    c.set_defaults(func=cmd_connect)

    c = sub.add_parser("join", help="自注册并加入某个主题（缺省 name=当前目录名）")
    c.add_argument("--room", required=True); c.add_argument("--name")
    c.add_argument("--role", default=""); c.add_argument("--repo")
    c.set_defaults(func=cmd_join)

    c = sub.add_parser("create-topic", help="新建主题并把自己加入")
    c.add_argument("--name", required=True); c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_create_topic)

    sub.add_parser("rooms", help="列出所有主题").set_defaults(func=cmd_rooms)
    sub.add_parser("instances", help="列出所有已连接实例及职责/在线").set_defaults(func=cmd_instances)

    c = sub.add_parser("invite", help="按职责或名字把另一实例拉进主题")
    c.add_argument("--target", required=True); c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_invite)

    c = sub.add_parser("send", help="发言（缺省发到当前主题）")
    c.add_argument("--text", required=True); c.add_argument("--topic", default="")
    c.add_argument("--reply-to", dest="reply_to", default="",
                   help="引用回复某条消息的 id（wait 里每条消息的 «id»；被点名后回执务必带上）")
    c.set_defaults(func=cmd_send)

    c = sub.add_parser("ask", help="在群里向用户提问并就地等其回复（待命期间征求用户意见用它，别退出循环）")
    c.add_argument("--text", required=True); c.add_argument("--topic", default="")
    c.add_argument("--timeout", type=float, default=600.0, help="最长等待秒数（缺省 600）")
    c.set_defaults(func=cmd_ask)

    c = sub.add_parser("wait", help="跨主题长轮询，等到新消息再返回")
    c.add_argument("--timeout", type=float, default=25.0)
    c.set_defaults(func=cmd_wait)

    sub.add_parser("read", help="立即读取新消息（不阻塞）").set_defaults(func=cmd_read)

    c = sub.add_parser("peers", help="列出某主题的参与者")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_peers)

    c = sub.add_parser("leave", help="退出某主题")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_leave)

    sub.add_parser("disconnect", help="全局下线").set_defaults(func=cmd_disconnect)
    sub.add_parser("whoami", help="打印当前会话状态").set_defaults(func=cmd_whoami)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
