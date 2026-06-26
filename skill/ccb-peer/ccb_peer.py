#!/usr/bin/env python3
"""CCB 仓库 peer 客户端（独立、仅用标准库）。

让一个 Claude Code 实例无需 MCP、仅凭 HTTP 就能加入 CCB 多仓库群聊：自注册、
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

# 单次 wait/read/ask 渲染上限：避免一次性输出撑爆工具结果。点名你的一律保留；其余从最近往前累计
# 到「条数」或「总字符预算」为止，更早的折叠；单条正文只在真正超长(>MAX_MSG_CHARS)时才截断——别把
# 正常技术长消息切碎。游标仍按全量推进、不重复投递。与 MCP 桥接保持一致。
MAX_WAIT_MESSAGES = 40
MAX_MSG_CHARS = 2000
MAX_WAIT_TOTAL_CHARS = 16000


def select_rendered(msgs, aid):
    """挑出本次要渲染的：点名你的一律保留；其余从最近往前按「条数 + 总字符预算」累计，更早的
    折叠。返回 (按原顺序的待渲染列表, 被折叠条数)。须与 MCP 桥接的同名函数行为一致。"""
    def _mentions_me(m):
        return m["sender_id"] != aid and aid in ((m.get("meta") or {}).get("mentions") or [])

    def _clen(m):
        return min(len(m.get("content") or ""), MAX_MSG_CHARS)

    keep = {m["id"] for m in msgs if _mentions_me(m)}
    total = sum(_clen(m) for m in msgs if m["id"] in keep)
    count = 0
    for m in reversed(msgs):
        if m["id"] in keep:
            continue
        if count >= MAX_WAIT_MESSAGES or total + _clen(m) > MAX_WAIT_TOTAL_CHARS:
            continue
        keep.add(m["id"]); total += _clen(m); count += 1
    shown = [m for m in msgs if m["id"] in keep]
    return shown, len(msgs) - len(shown)


# ----- 会话状态 --------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    # 原子写：先写临时文件再 os.replace，避免写到一半崩溃/中断把 .ccb-peer.json 写坏，
    # 导致 load_state 读不出来而静默丢掉整个身份（agent_id/当前主题/游标）。
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def base_url(state: dict) -> str:
    return state.get("base_url") or DEFAULT_URL


# ----- HTTP ------------------------------------------------------------------

def request_status(method: str, url: str, body: dict | None = None, timeout: float = 35.0):
    """发请求并返回 (status_code, parsed_body_or_raw_text)。

    仅在连接层失败（服务未启动 / 网络不可达 / 超时）时直接退出；HTTP 4xx/5xx 会把状态码
    与响应体原样返回，交由调用方决定是友好处理（如 invite 的 404）还是 die。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        return e.code, (detail or e.reason)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        die(f"无法连接 CCB（{url}）：{reason}。请确认服务已启动（uv run ccb）。")


def request(method: str, url: str, body: dict | None = None, timeout: float = 35.0):
    status, payload = request_status(method, url, body, timeout)
    if status >= 400:
        die(f"请求失败 {status}：{payload or ''}")
    return payload


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
    # 不重置 last_ts：fresh 仓库从 0 开始，首次 wait 即可看到近期历史（由渲染折叠上限兜住）；
    # 已有游标则保留，继续增量跟进。清掉 kicked。
    state.update(agent_id=res["agent_id"], name=name, role=args.role,
                 last_ts=state.get("last_ts", 0.0), kicked=False)
    save_state(state)
    how = "认领了已有身份" if res.get("claimed") else "新建了身份"
    print(f"已上线：{name}（{args.role or '未注明职责'}），{how}。\n"
          "若用户要你『进入 ccb / 持续协同 / 待命』：现在起请反复执行 `wait` 跟进，"
          "不要结束本回合去问用户下一步（那等于没真正进待命）。")


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
                 active_room=room["id"],
                 last_ts=state.get("last_ts", 0.0), kicked=False)
    save_state(state)
    how = "认领了已配置的槽位" if res.get("claimed") else "加入"
    print(f"已以「{name}」{how}主题「{room['name']}」，并设为当前主题。\n"
          "现在起进入待命循环：反复执行 `wait` 跟进——返回后处理与你相关的消息再立刻再次 `wait`，"
          "不要结束本回合去等用户。")


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
                 active_room=room["id"],
                 last_ts=state.get("last_ts", 0.0), kicked=False)
    save_state(state)
    print(
        f"已进入待命：以「{name}」加入主题「{room['name']}」（仓库 {os.getcwd()}）。\n"
        "现在进入待命循环：反复执行 `wait`（长轮询，期间几乎不耗 token）。返回后——被点名"
        "（消息带 ‹@你·被点名›）时先 `send --text \"收到，正在处理\" --reply-to <该消息id>` 回执，"
        "再读改本仓库代码用 `send --text` 给结果；与你无关、也没点你的：直接再 `wait`，别回复也别"
        "解释『与我无关』——每次解释都白烧一个回合，安静等到真正点你的消息即可。\n"
        "发言尽量定向：这条主要发给某一个特定的人/端时，给 `send` 带 `--to <对方>` 或 "
        "`--reply-to <对方消息id>`，别广播给全群——服务端可按「定向消息可见性」把它从无关端的 "
        "`wait` 里过滤掉，无关端就不会被反复叫醒。只有真正面向所有人的事才广播。\n"
        "要征求用户意见时用 `ask --text \"问题\"`：它把问题发到群里（GUI 高亮\"等你回答\"）并就地"
        "等用户回复，期间你始终在线——不要用 AskUserQuestion、也不要结束回合去问本地用户"
        "（那等于擅自退出待命）。待命期间你的「用户」就是 CCB 群里的人（人类）。\n"
        "让你「离开本大厅/退出某主题/你可以走了」时：用 `leave --topic <主题>` 退出那个主题即可，"
        "你仍在线、仍待命、可被 invite 拉回（即便不在任何主题也继续 `wait`），别 disconnect。\n"
        "只有用户明确说「退出待命/下线/停止」要你整体下线时，才执行 `disconnect`。"
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


def cmd_delete_topic(args) -> None:
    """删除一个主题（含其全部消息），缺省=当前主题。允许删除「大厅」。"""
    state = load_state()
    ref = args.topic or state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定要删除哪个主题。")
    room = resolve_room(state, ref)
    if not room:
        die(f"未找到主题「{ref}」。")
    request("DELETE", base_url(state) + f"/api/rooms/{room['id']}")
    if state.get("active_room") == room["id"]:
        state["active_room"] = None
        save_state(state)
    print(f"已删除主题「{room['name']}」（含其全部消息）。")


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
    # 用 request_status 软处理 404：邀请一个尚未上线的角色/名字是正常情形，应给出友好提示
    # 而不是以 exit 1 崩掉（与 MCP 桥接对齐）。
    status, res = request_status(
        "POST", base_url(state) + f"/api/rooms/{room['id']}/invite",
        {"target": args.target, "by": aid})
    if status == 404:
        die(f"未找到职责/名字为「{args.target}」的已连接实例。先让对方 connect 上线。")
    if status >= 400:
        die(f"请求失败 {status}：{res or ''}")
    if isinstance(res, dict) and res.get("already_member"):
        print(f"「{res['name']}」已在主题「{room['name']}」中（未重复拉入）。")
    else:
        print(f"已把「{res['name']}」拉进主题「{room['name']}」。")


def cmd_send(args) -> None:
    state = load_state()
    aid = require_agent(state)
    # 路由优先级：显式 --topic > 被回消息所在主题（--reply-to）> 当前主题。
    ref = args.topic
    if not ref and args.reply_to:
        ref = state.get("msg_rooms", {}).get(args.reply_to)
    if not ref:
        ref = state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定，或先 join/create-topic。")
    room = resolve_room(state, ref)
    if not room:
        die(f"未找到主题「{ref}」。")
    # 用 request_status 软处理 409：应答编排(hard)挡下时给提示，而非崩掉。
    status, body = request_status(
        "POST", base_url(state) + f"/api/rooms/{room['id']}/messages",
        {"content": args.text, "agent_id": aid, "reply_to": args.reply_to, "to": args.to})
    if status == 409:
        detail = body.get("detail") if isinstance(body, dict) else body
        print(detail or "已有实例在回答本轮问题；请先 `claim` 取得应答位或排队，轮到你再答。")
        return
    if status >= 400:
        die(f"请求失败 {status}：{body or ''}")
    print(f"已发送到「{room['name']}」。")


def cmd_claim(args) -> None:
    """回答面向所有人的问题前先抢应答位（缺省=当前主题），避免一拥而上重复回答。"""
    state = load_state()
    aid = require_agent(state)
    ref = args.topic or state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定。")
    room = resolve_room(state, ref)
    if not room:
        die(f"未找到主题「{ref}」。")
    data = request("POST", base_url(state) + f"/api/rooms/{room['id']}/answer/claim",
                   {"agent_id": aid})
    floor = data.get("floor", {})
    if data.get("granted"):
        print(f"已取得「{room['name']}」的应答位——你来回答。答完务必 `release` 放行下一位。")
        return
    if not floor.get("holder") and not floor.get("active"):
        print("当前没有进行中的提问轮——无需抢答位，继续 `wait` 即可；等出现面向所有人的提问再 `claim`。")
        return
    queue = floor.get("queue") or []
    pos = queue.index(aid) + 1 if aid in queue else len(queue)
    holder = floor.get("holder_name") or "其他实例"
    print(f"{holder} 正在回答，你排第 {pos} 位。先别答——`wait` 观望、读它的答复；确有必要补充才"
          "排队，轮到你（再 `claim` 抢到应答位）后发定向回复（`send --reply-to`）再 `release`；"
          "无需补充就 `release`。")


def cmd_release(args) -> None:
    """放行应答位（缺省=当前主题）：你是 holder→让队首顶上；在排队→退出队列。"""
    state = load_state()
    aid = require_agent(state)
    ref = args.topic or state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定。")
    room = resolve_room(state, ref)
    if not room:
        die(f"未找到主题「{ref}」。")
    data = request("POST", base_url(state) + f"/api/rooms/{room['id']}/answer/release",
                   {"agent_id": aid})
    nxt = (data.get("floor") or {}).get("holder_name")
    print(f"已放行「{room['name']}」的应答位。" + (f"（下一位：{nxt}）" if nxt else "（已空闲）"))


def _floor_hint(state: dict) -> None:
    """当前主题若有进行中的应答轮，打印一句提示（best-effort）。"""
    rid = state.get("active_room")
    if not rid:
        return
    try:
        _, fl = request_status("GET", base_url(state) + f"/api/rooms/{rid}/answer", timeout=8)
    except SystemExit:
        return
    if not isinstance(fl, dict) or not fl.get("active"):
        return
    me = state.get("agent_id")
    holder = fl.get("holder")
    if holder == me:
        print("— 应答位：你正持有本主题应答位；回答完请 `release` 放行下一位。")
    elif holder:
        print(f"— 应答位：{fl.get('holder_name')} 正在回答本轮问题。要补充就 `claim` 排队、"
              "等它答完轮到你再发定向回复；否则别重复回答。")
    else:
        print("— 应答位：本轮问题待应答。你若要回答，先 `claim` 取得应答位再答。")


def _pua_hint(state: dict) -> None:
    """当前主题若开了 PUA 模式，打印"现在该你做什么"（best-effort）。"""
    rid = state.get("active_room")
    if not rid:
        return
    try:
        _, data = request_status(
            "GET", base_url(state) + f"/api/rooms/{rid}/pua?agent_id={state.get('agent_id', '')}",
            timeout=8)
    except SystemExit:
        return
    if isinstance(data, dict) and data.get("hint"):
        print("\n" + data["hint"])


def cmd_ask(args) -> None:
    """在群里向用户提问并就地等待答复——待命期间想征求用户意见时用它，别退出循环去问本地用户。"""
    state = load_state()
    if state.get("kicked"):
        print("你已被踢出 CCB（kicked）。请重新 standby 归队。")
        return
    aid = require_agent(state)
    ref = args.topic or state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定，或先 join/create-topic。")
    room = resolve_room(state, ref)
    if not room:
        die(f"未找到主题「{ref}」。")
    q = request("POST", base_url(state) + f"/api/rooms/{room['id']}/messages",
                {"content": args.text, "agent_id": aid, "is_question": True})
    # 用「刚发出的提问」自身的 ts 作为本次等待的独立游标，且不持久化到 state['last_ts']：
    # 既不会把历史旧 human 消息误当本次答复（B2），也不会吞掉期间到达的他人消息（B1，
    # 它们留给 wait 带 «id»/‹被点名› 正常投递）。
    ask_since = (q or {}).get("ts", state.get("last_ts", 0.0))
    seen_other: list = []
    for _ in range(max(1, int(args.timeout / 25))):
        url = base_url(state) + f"/api/instances/{aid}/wait?since={ask_since}&timeout=25"
        msgs = request("GET", url, timeout=35)
        if not msgs:
            continue
        if any((m.get("meta") or {}).get("kicked") for m in msgs):
            state["kicked"] = True
            save_state(state)
            print("你已被踢出 CCB（kicked），提问中止。请重新 standby 归队。")
            return
        ask_since = max(m["ts"] for m in msgs)  # 仅推进本地游标
        rooms_map = state.setdefault("msg_rooms", {})
        for m in msgs:
            if m.get("room_id"):
                rooms_map[m["id"]] = m["room_id"]
        humans = [m for m in msgs if m.get("sender_id") == "human"]
        seen_other += [m for m in msgs if m.get("sender_id") not in ("human", aid)]
        if humans:
            if humans[-1].get("room_id"):
                state["active_room"] = humans[-1]["room_id"]
            save_state(state)
            ans = "\n".join(f"{m['sender_name']}: {m['content']}" for m in humans)
            print(f"用户已回复：\n{ans}{_ask_context(seen_other)}\n"
                  "（已得到答复。处理完后请立刻继续 `wait` 保持待命。）")
            return
        save_state(state)  # 持久化 msg_rooms 更新（用于后续 reply_to 路由）
    print("（用户暂未回复——你仍在待命、并未离线。可再次 ask 继续等，或先 `wait` 跟进其它消息。）"
          f"{_ask_context(seen_other)}")


def _ask_context(others: list) -> str:
    if not others:
        return ""

    def _line(m):
        c = m.get("content") or ""
        if len(c) > MAX_MSG_CHARS:
            c = c[:MAX_MSG_CHARS] + "…"
        return f"{m['sender_name']}: {c}"

    shown = others[-MAX_WAIT_MESSAGES:]
    omitted = len(others) - len(shown)
    head = f"（另折叠较早的 {omitted} 条）\n" if omitted else ""
    return f"\n（等待期间群里其他发言：\n{head}" + "\n".join(_line(m) for m in shown) + "）"


def _print_messages(state: dict, msgs: list, is_wait: bool = False) -> None:
    if not msgs:
        # 空结果也要带上待命提醒（仅 wait 路径）——否则房间安静时只有一句"没有新消息"、
        # 没有"继续轮询"的约束，模型容易误判"没事干了"而结束、掉出待命循环。
        if is_wait:
            print("（没有新消息）\n"
                  "— 待命提醒：没有新消息是正常的，请立刻再次 `wait` 继续保持在线，"
                  "不要就此结束；需要征求用户意见时用 `ask`（别用 AskUserQuestion）。")
        else:
            print("（没有新消息）")
        return
    # 被服务端踢出：wait/instance_wait 会立即返回带 meta.kicked 的哨兵。落一个本地 kicked 标记
    # 并停止——否则每拍都会重新收到踢出横幅、last_ts 反复抬升、永不退出（与 MCP 桥接对齐）。
    if any((m.get("meta") or {}).get("kicked") for m in msgs):
        state["kicked"] = True
        save_state(state)
        print("你已被踢出 CCB（kicked）。待命已结束——请不要再 wait；如需归队请重新 standby。")
        return
    state["last_ts"] = max(m["ts"] for m in msgs)
    me = state.get("agent_id")
    # 记录 id→主题；并让"当前主题"跟随最近一条非自己的消息——修复被 invite 进新主题后回复漏回大厅。
    rooms_map = state.setdefault("msg_rooms", {})
    for m in msgs:
        if m.get("room_id"):
            rooms_map[m["id"]] = m["room_id"]
    if len(rooms_map) > 500:
        for k in list(rooms_map)[:-250]:
            del rooms_map[k]
    incoming = [m for m in msgs if m["sender_id"] != me and m.get("room_id")]
    if incoming:
        state["active_room"] = incoming[-1]["room_id"]
    save_state(state)

    def _mentions_me(m):
        return m["sender_id"] != me and me in ((m.get("meta") or {}).get("mentions") or [])

    # 选出本次要渲染的（点名全留 + 最近优先按预算累计），其余折叠计数。
    shown, omitted = select_rendered(msgs, me)
    if omitted:
        print(f"〔为避免刷屏/撑爆上下文，已折叠较早的 {omitted} 条消息（完整记录见 GUI）；"
              "点名你的一律保留在下方。〕")
    mentioned_any = False
    for m in shown:
        you = "（你）" if m["sender_id"] == me else ""
        room = f"[{m.get('room_name', '')}] " if m.get("room_name") else ""
        meta = m.get("meta") or {}
        tag = ""
        if _mentions_me(m):
            tag = " ‹@你·主要找你›" if meta.get("to") == me else " ‹@你·被点名›"
            mentioned_any = True
        elif meta.get("to") and meta.get("to") != me and m["sender_id"] != me:
            # 主要发给别人——明确标注，免得对正文里的 @文本自作主张地抢答。
            tag = f" 〔→ 主要发给 {meta.get('to_name') or '某实例'}，不是你〕"
        quote = ""
        if meta.get("reply_to_sender"):
            quote = f"（↩ 回复 {meta['reply_to_sender']}：{meta.get('reply_to_preview', '')}）"
        content = m.get("content") or ""
        if len(content) > MAX_MSG_CHARS:
            content = content[:MAX_MSG_CHARS] + f"…〔省略 {len(m['content']) - MAX_MSG_CHARS} 字，完整见 GUI〕"
        print(f"{room}{m['sender_name']}{you}{tag} «{m['id']}»{quote}: {content}")
    if mentioned_any:
        print(
            '\n你被点名（‹被点名›）：请先 `send --text "收到，正在处理" '
            "--reply-to <被点名那条的 «id»>` 回执（让对方认出你在回应哪条），再着手处理。"
        )
    if is_wait:
        print(
            '\n— 待命提醒：处理完请立刻再次 `wait` 保持在线；需要征求用户意见时用 '
            '`ask --text "问题"`（在群里问并就地等回复），切勿用 AskUserQuestion 或结束回合。'
        )


def cmd_wait(args) -> None:
    state = load_state()
    if state.get("kicked"):
        print("你已被踢出 CCB（kicked）。请不要再 wait；如需归队请重新 standby。")
        return
    aid = require_agent(state)
    since = state.get("last_ts", 0.0)
    url = (base_url(state) + f"/api/instances/{aid}/wait"
           f"?since={since}&timeout={args.timeout}")
    msgs = request("GET", url, timeout=args.timeout + 10)
    _print_messages(state, msgs, is_wait=True)
    # _print_messages 可能从 kicked 哨兵里置位 kicked；非 kicked 时再附应答位/PUA 提示。
    if not load_state().get("kicked"):
        _floor_hint(state)
        _pua_hint(state)


def cmd_history(args) -> None:
    """查看某主题较早的历史消息（缺省=当前主题，最近 N 条），只读取、不影响 wait 进度。"""
    state = load_state()
    ref = args.topic or state.get("active_room")
    if not ref:
        die("没有当前主题。请用 --topic 指定要看哪个主题的历史。")
    room = resolve_room(state, ref)
    if not room:
        die(f"未找到主题「{ref}」。")
    n = max(1, min(int(args.limit or 50), 100))
    msgs = request("GET", base_url(state) + f"/api/rooms/{room['id']}/messages?since=0&limit={n}")
    if not msgs:
        print(f"「{room['name']}」还没有历史消息。")
        return
    me = state.get("agent_id")
    print(f"「{room['name']}」最近 {len(msgs)} 条历史：")
    for m in msgs:
        who = "（你）" if m.get("sender_id") == me else ""
        content = m.get("content") or ""
        if len(content) > MAX_MSG_CHARS:
            content = content[:MAX_MSG_CHARS] + "…"
        print(f"{m['sender_name']}{who} «{m['id']}»: {content}")


def cmd_read(args) -> None:
    state = load_state()
    if state.get("kicked"):
        print("你已被踢出 CCB（kicked）。请不要再 read/wait；如需归队请重新 standby。")
        return
    aid = require_agent(state)
    since = state.get("last_ts", 0.0)
    msgs = request("GET", base_url(state) + f"/api/instances/{aid}/messages?since={since}")
    _print_messages(state, msgs)
    if not load_state().get("kicked"):
        _pua_hint(state)


def cmd_pua_pass(args) -> None:
    state = load_state()
    aid = require_agent(state)
    ref = args.topic or state.get("active_room")
    room = resolve_room(state, ref) if ref else None
    if not room:
        die("未找到主题。")
    status, body = request_status(
        "POST", base_url(state) + f"/api/rooms/{room['id']}/pua/pass",
        {"agent_id": aid, "todo_id": args.todo})
    if status >= 400:
        die(f"失败 {status}：{body}")
    print(body.get("message", "已处理。") if isinstance(body, dict) else "已处理。")


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
        on = "在线" if a.get("online") else "离线"
        tag = f"{a.get('role')}/" if a.get("role") else ""
        print(f"- {a['name']}（{tag}peer，{on}）")


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
    print(f"已退出主题「{room['name']}」。你仍在线、仍在待命（没有下线），可被 invite 随时拉回——"
          "请继续 `wait` 保持在线；被重新邀请时会自动回到对话。")


def cmd_disconnect(args) -> None:
    state = load_state()
    aid = state.get("agent_id")
    if aid:
        # 即便服务端不可达也要把本地身份清掉——否则状态文件会卡着旧 agent_id 无法下线。
        try:
            request_status("POST", base_url(state) + f"/api/peers/{aid}/leave")
        except SystemExit:
            pass
    save_state({"base_url": base_url(state)})  # 清空身份/游标/msg_rooms/kicked，仅留服务地址
    print("已下线。")


def cmd_whoami(args) -> None:
    state = load_state()
    print(json.dumps(
        {k: state.get(k) for k in ("base_url", "agent_id", "name", "role", "active_room")},
        ensure_ascii=False,
    ))


def _todo_scope_id(state, scope, topic):
    if scope == "agent":
        return require_agent(state), None
    if scope == "global":
        return "", None
    ref = topic or state.get("active_room")
    room = resolve_room(state, ref) if ref else None
    if not room:
        return "", "未找到主题（用 --topic 指定）。"
    return room["id"], None


def cmd_todo_list(args) -> None:
    state = load_state()
    sid, err = _todo_scope_id(state, args.scope, args.topic)
    if err:
        die(err)
    todos = request("GET", base_url(state) + f"/api/todos?scope={args.scope}&scope_id={sid}")
    if not todos:
        print(f"（{args.scope} todo 为空）")
        return
    for t in todos:
        extra = f"（指派 {t['assignee']}）" if t.get("assignee") else ""
        print(f"- [{'x' if t['done'] else ' '}] «{t['id']}» {t['text']}{extra}")


def cmd_todo_add(args) -> None:
    state = load_state()
    aid = require_agent(state)
    sid, err = _todo_scope_id(state, args.scope, args.topic)
    if err:
        die(err)
    status, body = request_status(
        "POST", base_url(state) + "/api/todos",
        {"scope": args.scope, "scope_id": sid, "text": args.text,
         "assignee": args.assignee, "actor": aid})
    if status == 403:
        print("无权改这级 todo——主题 todo 需主持人；"
              "非主持人请用 `request --action todo_add --text ...`。")
        return
    if status >= 400:
        die(f"失败 {status}：{body}")
    print(f"已加入 {args.scope} todo：{args.text}")


def cmd_todo_done(args) -> None:
    state = load_state()
    aid = require_agent(state)
    status, body = request_status(
        "PATCH", base_url(state) + f"/api/todos/{args.id}",
        {"done": not args.undone, "actor": aid})
    if status == 404:
        die("todo 不存在。")
    if status == 403:
        print("无权改这条 todo。")
        return
    if status >= 400:
        die(f"失败 {status}：{body}")
    print(f"已标记{'未完成' if args.undone else '完成'}。")


def cmd_todo_remove(args) -> None:
    state = load_state()
    aid = require_agent(state)
    status, body = request_status(
        "DELETE", base_url(state) + f"/api/todos/{args.id}?actor={aid}")
    if status == 403:
        print("无权删这条 todo。")
        return
    if status >= 400:
        die(f"失败 {status}：{body}")
    print("已删除。")


def cmd_request(args) -> None:
    state = load_state()
    aid = require_agent(state)
    ref = args.topic or state.get("active_room")
    room = resolve_room(state, ref) if ref else None
    if not room:
        die("未找到主题（用 --topic 指定）。")
    status, body = request_status(
        "POST", base_url(state) + "/api/requests",
        {"room_id": room["id"], "action": args.action, "requested_by": aid,
         "target": args.target, "text": args.text, "todo_id": args.todo_id,
         "reason": args.reason})
    if status >= 400:
        die(f"请求失败 {status}：{body}")
    print(f"已向「{room['name']}」的主持人投递请求（{args.action}），等待审批。")


def cmd_requests(args) -> None:
    state = load_state()
    ref = args.topic or state.get("active_room")
    room = resolve_room(state, ref) if ref else None
    if not room:
        die("未找到主题。")
    reqs = request("GET", base_url(state) + f"/api/requests?room_id={room['id']}&status=pending")
    if not reqs:
        print("（没有待审批的请求）")
        return
    for q in reqs:
        p = q.get("payload") or {}
        tgt = p.get("target_name") or p.get("text") or p.get("todo_id") or ""
        reason = f"（理由：{q['reason']}）" if q.get("reason") else ""
        print(f"- «{q['id']}» {q['requested_by_name']} 请求 {q['action']} {tgt}{reason}")
    print("用 `resolve --id <id>`（加 --reject 拒绝）处理。")


def cmd_resolve(args) -> None:
    state = load_state()
    aid = require_agent(state)
    status, body = request_status(
        "POST", base_url(state) + f"/api/requests/{args.id}/resolve",
        {"approver": aid, "approve": not args.reject, "note": args.note})
    if status == 404:
        die("请求不存在。")
    if status == 403:
        print("只有该主题的主持人能审批。")
        return
    if status >= 400:
        die(f"失败 {status}：{body}")
    result = body.get("result", "") if isinstance(body, dict) else ""
    print(f"已{'拒绝' if args.reject else '批准'}：{result}")


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

    c = sub.add_parser("delete-topic", help="删除主题（含其全部消息），缺省=当前主题")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_delete_topic)

    sub.add_parser("rooms", help="列出所有主题").set_defaults(func=cmd_rooms)
    sub.add_parser("instances", help="列出所有已连接实例及职责/在线").set_defaults(func=cmd_instances)

    c = sub.add_parser("invite", help="按职责或名字把另一实例拉进主题")
    c.add_argument("--target", required=True); c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_invite)

    c = sub.add_parser("send", help="发言（缺省发到当前主题）")
    c.add_argument("--text", required=True); c.add_argument("--topic", default="")
    c.add_argument("--reply-to", dest="reply_to", default="",
                   help="引用回复某条消息的 id（wait 里每条消息的 «id»；被点名后回执务必带上）")
    c.add_argument("--to", default="",
                   help="明确指定这条主要发给谁（对方的职责/名字/agent_id），对方会看到 ‹主要找你›")
    c.set_defaults(func=cmd_send)

    c = sub.add_parser("claim", help="抢应答位（回答面向所有人的问题前先抢，避免一拥而上重复回答）")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_claim)

    c = sub.add_parser("release", help="放行应答位（答完、或决定不补充时）")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_release)

    c = sub.add_parser("ask", help="在群里向用户提问并就地等其回复（待命期间征求用户意见用它，别退出循环）")
    c.add_argument("--text", required=True); c.add_argument("--topic", default="")
    c.add_argument("--timeout", type=float, default=600.0, help="最长等待秒数（缺省 600）")
    c.set_defaults(func=cmd_ask)

    c = sub.add_parser("wait", help="跨主题长轮询，等到新消息再返回")
    c.add_argument("--timeout", type=float, default=25.0)
    c.set_defaults(func=cmd_wait)

    sub.add_parser("read", help="立即读取新消息（不阻塞）").set_defaults(func=cmd_read)

    c = sub.add_parser("history", help="查看某主题较早的历史消息（缺省=当前主题，最近 N 条）")
    c.add_argument("--topic", default=""); c.add_argument("--limit", type=int, default=50)
    c.set_defaults(func=cmd_history)

    c = sub.add_parser("peers", help="列出某主题的参与者")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_peers)

    c = sub.add_parser("leave", help="退出某主题")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_leave)

    c = sub.add_parser("todo-list", help="看 todo（scope=agent/room/global）")
    c.add_argument("--scope", default="agent", choices=["agent", "room", "global"])
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_todo_list)

    c = sub.add_parser("todo-add", help="加 todo（agent=自己/room=须主持人/global=须授权）")
    c.add_argument("--text", required=True)
    c.add_argument("--scope", default="agent", choices=["agent", "room", "global"])
    c.add_argument("--topic", default="")
    c.add_argument("--assignee", default="")
    c.set_defaults(func=cmd_todo_add)

    c = sub.add_parser("todo-done", help="标记某 todo 完成/未完成")
    c.add_argument("--id", required=True)
    c.add_argument("--undone", action="store_true", help="改为未完成")
    c.set_defaults(func=cmd_todo_done)

    c = sub.add_parser("todo-remove", help="删一条 todo")
    c.add_argument("--id", required=True)
    c.set_defaults(func=cmd_todo_remove)

    c = sub.add_parser("request", help="非主持人投递受控请求（kick/invite/close/todo_*）给主持人审批")
    c.add_argument("--action", required=True)
    c.add_argument("--target", default="")
    c.add_argument("--text", default="")
    c.add_argument("--todo-id", dest="todo_id", default="")
    c.add_argument("--reason", default="")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_request)

    c = sub.add_parser("requests", help="看本主题待审批请求（主持人据此 resolve）")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_requests)

    c = sub.add_parser("resolve", help="主持人审批一条请求（缺省批准，--reject 拒绝）")
    c.add_argument("--id", required=True)
    c.add_argument("--reject", action="store_true")
    c.add_argument("--note", default="")
    c.set_defaults(func=cmd_resolve)

    c = sub.add_parser("pua-pass", help="PUA 质疑阶段：对某条 todo 本轮无质疑、跳过")
    c.add_argument("--todo", required=True, help="该 todo 的报告消息 «id»（或 todo 自身 id）")
    c.add_argument("--topic", default="")
    c.set_defaults(func=cmd_pua_pass)

    sub.add_parser("disconnect", help="全局下线").set_defaults(func=cmd_disconnect)
    sub.add_parser("whoami", help="打印当前会话状态").set_defaults(func=cmd_whoami)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
