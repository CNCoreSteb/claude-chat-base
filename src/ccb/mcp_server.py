"""MCP 桥接：把每个仓库的 Claude Code 接成一个"多仓库 IM"里的实例。

每个仓库（依赖库 / 手机端 / web端 / 后端 …）各跑一个 Claude Code，通过本桥接：
- `connect` 全局上线（声明自己的职责 role），或 `join_room` 直接加入某个主题；
- `list_instances` 发现其他已连接的实例及其职责；
- `invite` 按职责（role）把别的实例主动拉进某个主题群；
- `create_topic` 开一个新主题群；`send_message` 发言；
- `wait_for_messages` 跨所有所在主题长轮询跟进（IM 式）。

所有消息与主题都持久化在 CCB 的 SQLite 里，并实时显示在 GUI 中。

注册（依赖已随 `uv sync` 装好），在每个仓库目录下执行：
    claude mcp add --transport stdio ccb -- uv run --project /路径/claude-chat-base ccb-mcp
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager

import httpx

BASE_URL = os.environ.get("CCB_URL", "http://127.0.0.1:8800").rstrip("/")
HEARTBEAT_INTERVAL = 15.0  # 秒；由桥接进程后台发送，与 LLM 无关、零 token。

# 单次 wait/read/ask 渲染上限：避免一次性返回过多/过长消息把工具输出撑爆（超限会被 harness 截断
# 落盘、污染上下文）。策略：点名你的一律保留；其余从最近往前选，累计到「条数」或「总字符预算」
# 为止，更早的折叠成一句计数；单条正文只在真正超长（>MAX_MSG_CHARS）时才截断——别把正常的
# 技术长消息切碎（之前 400 设得过狠，导致对方读不到全文而反复重述）。游标仍按全量推进、不重复投递。
MAX_WAIT_MESSAGES = 40           # 单次最多渲染条数
MAX_MSG_CHARS = 2000             # 单条正文截断阈值（正常消息基本不触发）
MAX_WAIT_TOTAL_CHARS = 16000     # 单次渲染总字符预算（点名消息除外），超出则折叠更早的


def select_rendered(msgs: list[dict], aid: str) -> tuple[list[dict], int]:
    """从一批消息里挑出本次要渲染的：点名你的一律保留；其余从最近往前按「条数 + 总字符预算」
    累计，更早的折叠。返回 (按原顺序的待渲染列表, 被折叠条数)。纯函数，便于测试与跨桥接对齐。"""
    def _mentions_me(m: dict) -> bool:
        return m["sender_id"] != aid and aid in ((m.get("meta") or {}).get("mentions") or [])

    def _clen(m: dict) -> int:
        return min(len(m.get("content") or ""), MAX_MSG_CHARS)

    keep = {m["id"] for m in msgs if _mentions_me(m)}
    total = sum(_clen(m) for m in msgs if m["id"] in keep)
    count = 0
    for m in reversed(msgs):
        if m["id"] in keep:
            continue
        if count >= MAX_WAIT_MESSAGES or total + _clen(m) > MAX_WAIT_TOTAL_CHARS:
            continue
        keep.add(m["id"])
        total += _clen(m)
        count += 1
    shown = [m for m in msgs if m["id"] in keep]
    return shown, len(msgs) - len(shown)


# 每个会话的状态（每个 Claude Code 会话对应一个 MCP 服务进程）。
_session: dict[str, object] = {
    "agent_id": None, "name": None, "role": "", "active_room": None,
    "last_ts": 0.0, "kicked": False,
    # 最近见过的消息 id → 所在主题 id；用于 reply_to 精确路由（缺省回到被回消息所在主题）。
    "msg_rooms": {},
}


def _client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)


async def _heartbeat_loop() -> None:
    """后台心跳：只要本会话已加入（有 agent_id），就周期性告诉服务端"我还在线"。

    这是桥接进程在做的事——不需要 LLM 调用任何工具、不消耗任何 token。它代表
    "这个 Claude Code 会话仍连着 CCB"，正是"在线"应有的含义。
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        aid = _session.get("agent_id")
        if not aid:
            continue
        try:
            async with _client(8) as c:
                r = await c.post(f"/api/peers/{aid}/heartbeat")
            if r.json().get("kicked"):
                # 已被服务端踢掉：停止心跳，并让待命循环据此收尾。
                _session.update(agent_id=None, active_room=None, kicked=True)
        except Exception:  # noqa: BLE001 - 服务未启动/网络抖动：忽略，下一拍再试
            pass


@asynccontextmanager
async def _lifespan(_server):  # noqa: ANN001 - FastMCP 生命周期钩子
    # 桥接进程一启动就跑后台心跳；进程随 Claude Code 会话存活/退出。
    task = asyncio.create_task(_heartbeat_loop())
    try:
        yield {}
    finally:
        task.cancel()
        # 会话正常结束时立即下线；崩溃/被强杀则由服务端的"过期检查"兜底标记离线。
        aid = _session.get("agent_id")
        if aid:
            try:
                async with _client(5) as c:
                    await c.post(f"/api/peers/{aid}/leave")
            except Exception:  # noqa: BLE001
                pass


async def _resolve_room(c: httpx.AsyncClient, ref: str) -> dict | None:
    state = (await c.get("/api/state")).json()
    return next(
        (r for r in state.get("rooms", []) if r["id"] == ref or r["name"] == ref), None
    )


def build_server():  # noqa: ANN201 - 返回一个 FastMCP 实例
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - 提示路径
        raise SystemExit(
            "peer 桥接需要安装 'mcp' 包。\n请执行：  uv sync\n"
        ) from exc

    mcp = FastMCP("ccb-peers", lifespan=_lifespan)

    # ----- 上线 / 加入 --------------------------------------------------------

    @mcp.tool()
    async def standby(name: str = "", role: str = "", room: str = "大厅") -> str:
        """进入 CCB 待命状态：以本仓库身份自注册并加入主题，然后持续轮询消息、被点名或有相关
        变更时回应。当用户说"进入 ccb / 进ccb / 接入 ccb / 连接 ccb 协同 / ccb 待命 / 进入待命 /
        standby"等任意"接入 CCB 一起协同"的意思时，优先调用本工具——而不是只用 connect/
        join_room 连一下就停下来问用户（那样不会进入持续待命）。

        `name` 缺省取当前目录名；`role` 是职责（如 后端/web端）；`room` 缺省"大厅"。"""
        nm = name or os.path.basename(os.getcwd()) or "Peer"
        repo = os.getcwd()
        async with _client() as c:
            match = await _resolve_room(c, room)
            if not match:  # 主题不存在则创建
                r = (await c.post("/api/rooms", json={"name": room})).json()
                match = {"id": r["id"], "name": r["name"]}
            resp = await c.post(
                "/api/peers",
                json={"room_id": match["id"], "name": nm, "role": role, "repo_path": repo},
            )
            resp.raise_for_status()
            data = resp.json()
        _session.update(
            agent_id=data["agent_id"], name=nm, role=role,
            active_room=match["id"], kicked=False,
        )
        # 不重置/播种 last_ts：fresh 进程从 0 开始，首次 wait 即可看到近期历史（由渲染折叠上限
        # 兜住、不会洪泛）；同进程内已有游标则保留，继续增量跟进。
        return (
            f"已进入待命：以「{nm}」（{role or '未注明职责'}）加入主题「{match['name']}」。\n"
            f"仓库路径：{repo}\n\n"
            "【待命循环 —— 现在开始持续执行，不要结束本回合、也不要等用户】\n"
            "1. 调用 wait_for_messages（长轮询，阻塞到有新消息才返回，几乎不耗 token）。\n"
            "2. 被点名（消息带 ‹@你·被点名›）时：先用 send_message 回一句『收到，正在处理』，\n"
            "   并带上 reply_to=该消息的 «id»，再读改本仓库代码、用 send_message 给出结果；\n"
            "   与你无关、也没点你的消息：直接再次 wait，别回复、也别解释『与我无关』——每次解释\n"
            "   都白烧一个回合、徒增上下文，安静等到真正点你的消息即可。\n"
            "3. 无论有无消息，立刻再次调用 wait_for_messages，回到第 1 步，反复保持在线。\n\n"
            "【发言尽量定向 —— 别让无关的人被反复叫醒】\n"
            "当你这条主要发给某一个特定的人/端时，给 send_message 带上 to=对方（职责/名字/id）或\n"
            "reply_to=对方那条消息，别广播给全群——这样服务端可按「定向消息可见性」把它从无关端的\n"
            "wait 里过滤掉，无关端就不会被你反复叫醒、空耗回合。只有真正面向所有人的事才广播。\n\n"
            "【要征求用户意见时 —— 用 ask，绝不离开待命】\n"
            "待命期间你的「用户」就是 CCB 群里的人（人类）。需要用户拍板/澄清时，调用\n"
            "ask（把问题作为入参）：它把问题发到群里（GUI 中高亮为「等你回答」）并就地等用户\n"
            "回复后返回，其间你始终在线。不要用 AskUserQuestion，也不要结束本回合去问\n"
            "你终端的本地用户——那等于擅自退出待命。"
            "需要别的仓库参与时，先 @ 点名或 invite 拉进来再 ask。\n\n"
            "【主题管理 / 待办（主持人制）】\n"
            "建群者默认是该主题主持人；只有主持人能直接 invite/kick/关主题、增删改主题 todo。\n"
            "你若不是主持人，想踢/邀/关/改主题 todo：用 request_action(action, ...) 投递请求，\n"
            "由主持人 list_requests 看、resolve_request 批/拒。\n"
            "todo：todo_list/todo_add/todo_done/todo_remove——agent 级是你自己的（随意），\n"
            "room 级须主持人，global 级须人工授权。\n\n"
            "【面向所有人的问题 —— 先抢应答位，别一拥而上】\n"
            "收到面向所有人（非专门点你）的问题时，先调用 claim_answer：抢到才回答、答完\n"
            "release_answer 放行下一位；没抢到说明已有人在答——先别答，wait 观望并读它的答复，\n"
            "确有必要补充/纠正才排队、轮到你时发定向修正（reply_to 那条答复）"
            "再 release_answer，否则别重复回答。wait 输出里会提示「谁正在回答」。\n\n"
            "【离开主题 ≠ 下线】\n"
            "让你「离开本大厅 / 退出某主题 / 你可以走了」时：用 leave_room(\"主题名\") 退出那个\n"
            "主题即可——你仍在线、仍在待命，可被 invite 随时拉回；即便已不在任何主题，也继续\n"
            "wait_for_messages 保持在线（被邀请时会自动回到对话）。不要因此 disconnect。\n"
            "只有用户明确说「退出待命 / 下线 / 停止 / stop」要你整体下线时，"
            "才用 disconnect 停止循环。\n"
            "（用户随时可按 Esc 打断你插话。）"
        )

    @mcp.tool()
    async def connect(name: str, role: str = "", repo_path: str = "") -> str:
        """全局上线（声明你代表的仓库）。`name` 是显示名，`role` 是职责（如 后端/web端），
        `repo_path` 是本仓库本地路径。上线后即可被别的实例发现与拉群；之后可用
        join_room / create_topic 进入主题。
        注意：connect 只是"上线"、不会持续待命；若用户要你"进入 ccb / 持续协同 / 待命"，
        应改用 standby（自带轮询循环指令）。"""
        async with _client() as c:
            resp = await c.post(
                "/api/instances/connect",
                json={"name": name, "role": role, "repo_path": repo_path},
            )
            resp.raise_for_status()
            data = resp.json()
        _session.update(agent_id=data["agent_id"], name=name, role=role, kicked=False)
        how = "认领了已有身份" if data.get("claimed") else "新建了身份"
        return (
            f"已上线：{name}（{role or '未注明职责'}），{how}。\n"
            "若用户要你『进入 ccb / 持续协同 / 待命』：现在起请反复调用 wait_for_messages "
            "跟进，不要结束本回合去问用户下一步（那等于没真正进待命）。"
        )

    @mcp.tool()
    async def join_room(room: str, name: str = "", role: str = "", repo_path: str = "") -> str:
        """加入某个主题群（`room` 为主题名或 id）。若 GUI 里已为你的仓库预配了同名槽位，
        会直接认领。加入后该主题成为你的"当前主题"。"""
        nm = name or _session.get("name") or "Peer"
        async with _client() as c:
            match = await _resolve_room(c, room)
            if not match:
                return f"未找到主题「{room}」。可用 list_rooms 查看。"
            resp = await c.post(
                "/api/peers",
                json={"room_id": match["id"], "name": nm, "role": role, "repo_path": repo_path},
            )
            resp.raise_for_status()
            data = resp.json()
        _session.update(
            agent_id=data["agent_id"], name=nm, role=role,
            active_room=match["id"], kicked=False,
        )
        how = "认领了已配置的槽位" if data.get("claimed") else "加入"
        return (
            f"已以「{nm}」{how}主题「{match['name']}」。当前主题已切到这里。\n"
            "现在起进入待命循环：反复调用 wait_for_messages 跟进——返回后处理与你相关的消息，"
            "然后立刻再次调用，不要结束本回合去等用户；用 invite 按职责把需要的仓库拉进来。"
        )

    @mcp.tool()
    async def create_topic(name: str, topic: str = "") -> str:
        """新建一个主题群并把自己加入。`name` 是群名，`topic` 是该群的目标/说明。
        新建后成为你的"当前主题"。需要先 connect 或 join_room。"""
        if not _session["agent_id"]:
            return "请先用 connect 或 join_room 上线。"
        async with _client() as c:
            resp = await c.post(
                "/api/rooms",
                json={"name": name, "topic": topic, "agent_ids": [_session["agent_id"]]},
            )
            resp.raise_for_status()
            r = resp.json()
        _session["active_room"] = r["id"]
        return f"已创建主题「{name}」并设为当前主题。用 invite 把相关仓库拉进来。"

    @mcp.tool()
    async def delete_topic(topic: str = "") -> str:
        """关闭/删除一个主题群（`topic` 为主题名或 id，缺省=当前主题），连同其全部消息一起删除、
        不可恢复。只有该主题主持人能直接关；非主持人请改用 request_action(action="close")。"""
        aid = _session.get("agent_id")
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "没有当前主题，请用 topic 指定要关哪个主题。"
            match = await _resolve_room(c, room_ref)
            if not match:
                return f"未找到主题「{room_ref}」。"
            resp = await c.delete(f"/api/rooms/{match['id']}", params={"actor": aid or ""})
        if resp.status_code == 403:
            return "你不是本主题主持人，关主题请用 request_action(action=\"close\") 投递请求。"
        resp.raise_for_status()
        if _session.get("active_room") == match["id"]:
            _session["active_room"] = None
        return f"已关闭主题「{match['name']}」（含其全部消息）。"

    # ----- 发现 / 拉群 --------------------------------------------------------

    @mcp.tool()
    async def list_rooms() -> str:
        """列出所有主题群及其参与者数量。"""
        async with _client() as c:
            state = (await c.get("/api/state")).json()
        rooms = state.get("rooms", [])
        if not rooms:
            return "目前还没有主题。可用 create_topic 新建一个。"
        cur = _session.get("active_room")
        return "\n".join(
            f"{'* ' if r['id'] == cur else '- '}{r['name']}"
            f"（{len(r['agent_ids'])} 名参与者）话题：{r.get('topic', '')}"
            for r in rooms
        )

    @mcp.tool()
    async def list_instances() -> str:
        """列出所有已连接的实例（各仓库的 Claude Code）及其职责、在线状态、所在主题——
        据此决定按职责把谁拉进群。"""
        async with _client() as c:
            data = (await c.get("/api/instances")).json()
        if not data:
            return "目前没有已连接的实例。"
        lines = []
        for a in data:
            on = "在线" if a["online"] else "离线"
            rooms = "、".join(r["name"] for r in a.get("rooms", [])) or "（不在任何主题）"
            me = "（你）" if a["id"] == _session.get("agent_id") else ""
            lines.append(f"- {a['name']}{me}｜职责：{a.get('role') or '?'}｜{on}｜主题：{rooms}")
        return "\n".join(lines)

    @mcp.tool()
    async def invite(target: str, topic: str = "") -> str:
        """按"职责或名字"把另一个已连接的实例拉进某个主题群。`target` 如 "后端"/"web端"
        或对方显示名；`topic` 为目标主题名（缺省=你的当前主题），若该主题不存在会自动新建。"""
        if not _session["agent_id"]:
            return "请先 connect 或 join_room 后再拉人。"
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "请先 join_room/create_topic 设定当前主题，或在 topic 参数里指定。"
            match = await _resolve_room(c, room_ref)
            if not match and topic:
                # 指定的主题不存在 -> 自动新建并把自己加入。
                r = (await c.post(
                    "/api/rooms",
                    json={"name": topic, "agent_ids": [_session["agent_id"]]},
                )).json()
                match = {"id": r["id"], "name": r["name"]}
                _session["active_room"] = r["id"]
            if not match:
                return f"未找到主题「{room_ref}」。"
            resp = await c.post(
                f"/api/rooms/{match['id']}/invite",
                json={"target": target, "by": _session["agent_id"]},
            )
            if resp.status_code == 403:
                return ("你不是本主题主持人，邀请请用 "
                        f"request_action(action=\"invite\", target=\"{target}\")。")
            if resp.status_code == 404:
                return f"未找到职责/名字为「{target}」的已连接实例。先让对方 connect 上线。"
            resp.raise_for_status()
            data = resp.json()
        if data.get("already_member"):
            return f"「{data['name']}」已在主题「{match['name']}」中（未重复拉入）。"
        return f"已把「{data['name']}」拉进主题「{match['name']}」。"

    # ----- 收发消息 -----------------------------------------------------------

    @mcp.tool()
    async def send_message(content: str, topic: str = "", reply_to: str = "", to: str = "") -> str:
        """发言。`topic` 指定目标主题（缺省=当前主题）。`reply_to` 传入某条消息的 id（即
        wait_for_messages 里每条消息的 «id»）就能像 QQ 那样引用回复它——被点名后回执
        务必带上，好让对方在一堆「收到」里认出你在回应哪条。

        `to` 明确指定这条主要发给谁（对方的职责/名字/agent_id）：它按 id 规范写入消息，
        让对方在 wait 里看到 ‹@你·主要找你›，比只在正文里写 @ 更明确、不怕重名或措辞歧义。
        所有成员与 GUI 即时可见。"""
        if not _session["agent_id"]:
            return "请先 connect / join_room。"
        # 路由优先级：显式 topic > 被回消息所在主题（reply_to）> 当前主题。
        room_ref = topic
        if not room_ref and reply_to:
            room_ref = _session.get("msg_rooms", {}).get(reply_to)  # type: ignore[union-attr]
        if not room_ref:
            room_ref = _session.get("active_room")
        if not room_ref:
            return "没有当前主题，请用 topic 指定，或先 join_room/create_topic。"
        async with _client() as c:
            match = await _resolve_room(c, room_ref)
            if not match:
                return f"未找到主题「{room_ref}」。"
            resp = await c.post(
                f"/api/rooms/{match['id']}/messages",
                json={
                    "content": content,
                    "agent_id": _session["agent_id"],
                    "reply_to": reply_to,
                    "to": to,
                },
            )
            # 应答编排(hard)：本轮已有人在答、你不是 holder 时会被 409 挡下——提示先抢应答位。
            if resp.status_code == 409:
                return resp.json().get("detail") or (
                    "已有实例在回答本轮问题；请先 claim_answer 取得应答位或排队，轮到你再回答。"
                )
            resp.raise_for_status()
        return f"已发送到「{match['name']}」。"

    @mcp.tool()
    async def claim_answer(topic: str = "") -> str:
        """回答面向所有人的问题前先抢「应答位」，避免和别的实例一拥而上重复回答（缺省=当前主题）。
        抢到→你来答、答完调用 release_answer 放行下一位；没抢到→你已排队，先别答：用
        wait_for_messages 观望，读当前回答者的答复，确有必要补充/纠正时轮到你再发定向修正
        （reply_to 那条答复）再 release_answer。可反复调用本工具复查是否轮到你。"""
        if not _session["agent_id"]:
            return "请先 connect / join_room。"
        aid = _session["agent_id"]
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "没有当前主题，请用 topic 指定。"
            match = await _resolve_room(c, room_ref)
            if not match:
                return f"未找到主题「{room_ref}」。"
            resp = await c.post(
                f"/api/rooms/{match['id']}/answer/claim", json={"agent_id": aid}
            )
            resp.raise_for_status()
            data = resp.json()
        floor = data.get("floor", {})
        if data.get("granted"):
            return (f"已取得「{match['name']}」的应答位——你来回答。"
                    "答完务必 release_answer 放行下一位。")
        if not floor.get("holder") and not floor.get("active"):
            return ("当前没有进行中的提问轮（没人在等回答）——无需抢答位，继续 wait 跟进即可；"
                    "等真正出现面向所有人的提问再 claim_answer。")
        queue = floor.get("queue") or []
        pos = queue.index(aid) + 1 if aid in queue else len(queue)
        holder = floor.get("holder_name") or "其他实例"
        return (
            f"{holder} 正在回答，你排在第 {pos} 位。先别答——wait 观望、读它的答复；确有必要"
            "补充/纠正时，轮到你（再次 claim_answer 抢到应答位）后再发定向修正（reply_to 那条）"
            "再 release_answer；若无需补充，调用 release_answer 退出队列即可。"
        )

    @mcp.tool()
    async def release_answer(topic: str = "") -> str:
        """放行应答位（缺省=当前主题）：你是 holder→自动让队首顶上；你在排队→退出队列。
        答完、或决定不补充时调用。"""
        if not _session["agent_id"]:
            return "请先 connect / join_room。"
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "没有当前主题，请用 topic 指定。"
            match = await _resolve_room(c, room_ref)
            if not match:
                return f"未找到主题「{room_ref}」。"
            resp = await c.post(
                f"/api/rooms/{match['id']}/answer/release", json={"agent_id": _session["agent_id"]}
            )
            resp.raise_for_status()
            floor = resp.json().get("floor", {})
        nxt = floor.get("holder_name")
        tail = f"（下一位：{nxt}）" if nxt else "（已空闲）"
        return f"已放行「{match['name']}」的应答位。{tail}"

    # ----- TODO / 受控请求 -----------------------------------------------------

    async def _scope_id(c, scope: str, topic: str) -> tuple[str, str | None]:
        """返回 (scope_id, error)。agent->自己；room->解析主题；global->空。"""
        aid = _session.get("agent_id") or ""
        if scope == "agent":
            return aid, None
        if scope == "global":
            return "", None
        m = await _resolve_room(c, topic or _session.get("active_room") or "")
        if not m:
            return "", "未找到主题（用 topic 指定）。"
        return m["id"], None

    @mcp.tool()
    async def todo_list(scope: str = "agent", topic: str = "") -> str:
        """看 todo。scope=agent（你自己的）/ room（某主题，缺省当前主题）/ global（全局）。"""
        async with _client() as c:
            sid, err = await _scope_id(c, scope, topic)
            if err:
                return err
            todos = (await c.get("/api/todos", params={"scope": scope, "scope_id": sid})).json()
        if not todos:
            return f"（{scope} todo 为空）"
        return "\n".join(
            f"- [{'x' if t['done'] else ' '}] «{t['id']}» {t['text']}"
            + (f"（指派 {t['assignee']}）" if t.get("assignee") else "")
            for t in todos)

    @mcp.tool()
    async def todo_add(text: str, scope: str = "agent", topic: str = "", assignee: str = "") -> str:
        """加一条 todo。scope=agent（自己，随意）/ room（主题，须主持人）/ global（须人工授权）。
        非主持人想改主题 todo，请改用 request_action(action="todo_add", text=...)。"""
        aid = _session.get("agent_id")
        async with _client() as c:
            sid, err = await _scope_id(c, scope, topic)
            if err:
                return err
            r = await c.post("/api/todos", json={"scope": scope, "scope_id": sid,
                                                 "text": text, "assignee": assignee, "actor": aid})
        if r.status_code == 403:
            return "无权改这级 todo——主题 todo 需主持人；非主持人请用 request_action(todo_add)。"
        r.raise_for_status()
        return f"已加入 {scope} todo：{text}"

    @mcp.tool()
    async def todo_done(todo_id: str, done: bool = True) -> str:
        """把某条 todo 标记完成/未完成（按归属鉴权：自己的随意、主题 todo 需主持人）。"""
        aid = _session.get("agent_id")
        async with _client() as c:
            r = await c.patch(f"/api/todos/{todo_id}", json={"done": done, "actor": aid})
        if r.status_code == 404:
            return "todo 不存在。"
        if r.status_code == 403:
            return "无权改这条 todo。"
        r.raise_for_status()
        return f"已标记{'完成' if done else '未完成'}。"

    @mcp.tool()
    async def todo_remove(todo_id: str) -> str:
        """删一条 todo（按归属鉴权）。"""
        aid = _session.get("agent_id")
        async with _client() as c:
            r = await c.delete(f"/api/todos/{todo_id}", params={"actor": aid or ""})
        if r.status_code == 404:
            return "todo 不存在。"
        if r.status_code == 403:
            return "无权删这条 todo。"
        r.raise_for_status()
        return "已删除。"

    @mcp.tool()
    async def request_action(action: str, target: str = "", text: str = "",
                             todo_id: str = "", reason: str = "", topic: str = "") -> str:
        """非主持人用它向主持人投递受控请求等审批。action=kick/invite/close/todo_add/todo_update/
        todo_remove；kick/invite 用 target（职责/名字/id）指定对象，todo_* 用 text/todo_id。"""
        aid = _session.get("agent_id")
        async with _client() as c:
            m = await _resolve_room(c, topic or _session.get("active_room") or "")
            if not m:
                return "未找到主题（用 topic 指定）。"
            r = await c.post("/api/requests", json={
                "room_id": m["id"], "action": action, "requested_by": aid,
                "target": target, "text": text, "todo_id": todo_id, "reason": reason})
        if r.status_code >= 400:
            return f"请求失败：{r.text}"
        return f"已向「{m['name']}」的主持人投递请求（{action}），等待审批。"

    @mcp.tool()
    async def list_requests(topic: str = "") -> str:
        """看本主题待审批的受控请求（缺省当前主题）。主持人据此用 resolve_request 批准/拒绝。"""
        async with _client() as c:
            m = await _resolve_room(c, topic or _session.get("active_room") or "")
            if not m:
                return "未找到主题。"
            reqs = (await c.get("/api/requests",
                                params={"room_id": m["id"], "status": "pending"})).json()
        if not reqs:
            return "（没有待审批的请求）"
        out = []
        for q in reqs:
            p = q.get("payload") or {}
            tgt = p.get("target_name") or p.get("text") or p.get("todo_id") or ""
            out.append(f"- «{q['id']}» {q['requested_by_name']} 请求 {q['action']} {tgt}"
                       + (f"（理由：{q['reason']}）" if q.get("reason") else ""))
        return "\n".join(out) + "\n用 resolve_request(request_id, approve=True/False) 处理。"

    @mcp.tool()
    async def resolve_request(request_id: str, approve: bool = True, note: str = "") -> str:
        """主持人审批一条受控请求：approve=True 通过并执行、False 拒绝。"""
        aid = _session.get("agent_id")
        async with _client() as c:
            r = await c.post(f"/api/requests/{request_id}/resolve",
                             json={"approver": aid, "approve": approve, "note": note})
        if r.status_code == 404:
            return "请求不存在。"
        if r.status_code == 403:
            return "只有该主题的主持人能审批。"
        r.raise_for_status()
        return f"已{'批准' if approve else '拒绝'}：{r.json().get('result', '')}"

    @mcp.tool()
    async def ask(question: str, topic: str = "", timeout: float = 600.0) -> str:
        """在 CCB 群里向用户提问并就地等待答复——待命期间需要用户拍板/澄清时用它，
        不要用 AskUserQuestion、也不要结束回合去问你终端的本地用户。它会把 question
        发到主题（GUI 中高亮为"等你回答"），然后阻塞长轮询，直到用户（人类）回话再返回。
        全程你都留在待命、不会掉线。`timeout` 是最长等待秒数（缺省 10 分钟）。"""
        if not _session["agent_id"]:
            return "请先 connect / join_room。"
        aid = _session["agent_id"]
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "没有当前主题，请用 topic 指定，或先 join_room/create_topic。"
            match = await _resolve_room(c, room_ref)
            if not match:
                return f"未找到主题「{room_ref}」。"
            resp = await c.post(
                f"/api/rooms/{match['id']}/messages",
                json={"content": question, "agent_id": aid, "is_question": True},
            )
            resp.raise_for_status()
            question_msg = resp.json()
        # 用「刚发出的提问」自身的 ts 作为本次等待的独立游标，且不触碰全局 _session['last_ts']：
        #  - 不污染全局游标 => 等待期间到达的他人消息/被拉入新主题的通知不会被吞掉，之后
        #    wait_for_messages 仍会带 «id»/‹被点名› 正常投递它们；
        #  - 从提问 ts 起算 => 不会把历史里的旧 human 消息误当成对「本次提问」的回答。
        ask_since = question_msg.get("ts", _session.get("last_ts", 0.0))
        seen_other: list[dict] = []
        for _ in range(max(1, int(timeout / 25))):
            if _session.get("kicked"):
                return "你已被踢出 CCB，提问中止。请 disconnect 收尾。"
            async with _client(timeout=35) as c:
                r = await c.get(
                    f"/api/instances/{aid}/wait",
                    params={"since": ask_since, "timeout": 25.0},
                )
                r.raise_for_status()
                msgs = r.json()
            if not msgs:
                continue
            if any((m.get("meta") or {}).get("kicked") for m in msgs):
                _session["kicked"] = True
                return "你已被踢出 CCB，提问中止。请 disconnect 收尾。"
            ask_since = max(m["ts"] for m in msgs)  # 仅推进本地游标
            # 记录 id→主题，以便随后对这些消息做 reply_to 精确路由（但不动 active_room/last_ts）。
            rooms_map: dict = _session.setdefault("msg_rooms", {})  # type: ignore[assignment]
            for m in msgs:
                if m.get("room_id"):
                    rooms_map[m["id"]] = m["room_id"]
            humans = [m for m in msgs if m.get("sender_id") == "human"]
            seen_other += [m for m in msgs if m.get("sender_id") not in ("human", aid)]
            if humans:
                # 让「当前主题」跟随用户答复所在主题，便于直接回复。
                if humans[-1].get("room_id"):
                    _session["active_room"] = humans[-1]["room_id"]
                ans = "\n".join(f"{m['sender_name']}: {m['content']}" for m in humans)
                extra = _ask_context(seen_other)
                return (
                    f"用户已回复：\n{ans}{extra}\n\n"
                    "（已得到答复。处理完后请立刻继续 wait_for_messages 保持待命。）"
                )
        return (
            "（用户暂未回复——你仍在待命、并未离线。可再次 ask 继续等，或先 "
            f"wait_for_messages 跟进其它消息。）{_ask_context(seen_other)}"
        )

    def _ask_context(others: list[dict]) -> str:
        if not others:
            return ""

        def _line(m: dict) -> str:
            c = m.get("content") or ""
            if len(c) > MAX_MSG_CHARS:
                c = c[:MAX_MSG_CHARS] + "…"
            return f"{m['sender_name']}: {c}"

        shown = others[-MAX_WAIT_MESSAGES:]
        omitted = len(others) - len(shown)
        head = f"（另折叠较早的 {omitted} 条）\n" if omitted else ""
        return f"\n\n（等待期间群里其他发言：\n{head}" + "\n".join(_line(m) for m in shown) + "）"

    @mcp.tool()
    async def wait_for_messages(timeout: float = 25.0) -> str:
        """长轮询：阻塞至多 timeout 秒，等待你所在任意主题出现新消息后返回（IM 式跟进）。
        待命时反复调用本工具形成"监听—回应"循环：返回后处理与你相关的消息，然后立刻再次调用。"""
        return await _fetch_new(wait=True, timeout=timeout)

    @mcp.tool()
    async def read_messages() -> str:
        """立即读取你所在全部主题中、上次之后的新消息（不阻塞）。"""
        return await _fetch_new(wait=False)

    @mcp.tool()
    async def history(topic: str = "", limit: int = 50) -> str:
        """查看某主题较早的历史消息（缺省=当前主题；最近 limit 条，缺省 50、最多 100）。
        用于回看你加入之前、或已折叠的早期对话——只读取、不影响 wait 进度（不推进游标）。"""
        n = max(1, min(int(limit or 50), 100))
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "没有当前主题，请用 topic 指定要看哪个主题的历史。"
            match = await _resolve_room(c, room_ref)
            if not match:
                return f"未找到主题「{room_ref}」。"
            resp = await c.get(
                f"/api/rooms/{match['id']}/messages", params={"since": 0.0, "limit": n}
            )
            resp.raise_for_status()
            msgs = resp.json()
        if not msgs:
            return f"「{match['name']}」还没有历史消息。"
        aid = _session.get("agent_id")
        lines = []
        for m in msgs:
            who = "（你）" if m.get("sender_id") == aid else ""
            content = m.get("content") or ""
            if len(content) > MAX_MSG_CHARS:
                content = content[:MAX_MSG_CHARS] + "…"
            lines.append(f"{m['sender_name']}{who} «{m['id']}»: {content}")
        return f"「{match['name']}」最近 {len(msgs)} 条历史：\n" + "\n".join(lines)

    async def _floor_hint(aid: str) -> str:
        """当前主题若有进行中的应答轮，给一句提示（best-effort，失败则静默不打扰）。"""
        rid = _session.get("active_room")
        if not rid:
            return ""
        try:
            async with _client(8) as c:
                fl = (await c.get(f"/api/rooms/{rid}/answer")).json()
        except Exception:  # noqa: BLE001
            return ""
        if not fl.get("active"):
            return ""
        holder = fl.get("holder")
        if holder == aid:
            return "— 应答位：你正持有本主题应答位；回答完请调用 release_answer 放行下一位。"
        if holder:
            return (
                f"— 应答位：{fl.get('holder_name')} 正在回答本轮问题。"
                "要补充/纠正就 claim_answer 排队、等它答完轮到你再发定向修正；否则别重复回答。"
            )
        return "— 应答位：本轮问题待应答。你若要回答，请先 claim_answer 取得应答位再答。"

    async def _fetch_new(wait: bool, timeout: float = 25.0) -> str:
        if _session.get("kicked"):
            return (
                "你已被踢出 CCB（kicked）。待命已结束——"
                "请不要再 wait；如需归队请重新 standby。"
            )
        aid = _session["agent_id"]
        if not aid:
            return "请先 connect / join_room。"
        path = "wait" if wait else "messages"
        params = {"since": _session["last_ts"]}
        if wait:
            params["timeout"] = timeout
        async with _client(timeout=timeout + 10) as c:
            resp = await c.get(f"/api/instances/{aid}/{path}", params=params)
            resp.raise_for_status()
            msgs = resp.json()
        if not msgs:
            if not wait:
                return "（没有新消息）"
            # 关键：空结果也要带上待命提醒——否则房间安静时模型只收到一句"没有新消息"、
            # 没有任何"继续轮询"的约束，容易误判"没事干了"而结束回合、掉出待命循环。
            return (
                "（没有新消息）\n\n"
                "— 待命提醒：没有新消息是正常的，请立刻再次调用 wait_for_messages "
                "继续保持在线；不要就此结束本回合或退出待命。"
                "需要征求用户意见时用 ask（别用 AskUserQuestion）。"
            )
        # 被服务端踢出：wait 会立即返回带 meta.kicked 的哨兵。落一个本地 kicked 标记并停止——
        # 否则每拍都会重新收到踢出横幅、last_ts 反复抬升、永不退出（与 Skill 桥接对齐）。
        if any((m.get("meta") or {}).get("kicked") for m in msgs):
            _session["kicked"] = True
            return (
                "你已被踢出 CCB（kicked）。待命已结束——"
                "请不要再 wait；如需归队请重新 standby。"
            )
        _session["last_ts"] = max(m["ts"] for m in msgs)
        # 记录 id→主题；并让"当前主题"跟随最近一条非自己的消息——修复被 invite 进新主题后，
        # 回复缺省漏回大厅（active_room 卡在 standby 的主题）的问题。
        rooms_map: dict = _session.setdefault("msg_rooms", {})  # type: ignore[assignment]
        for m in msgs:
            if m.get("room_id"):
                rooms_map[m["id"]] = m["room_id"]
        if len(rooms_map) > 500:
            for k in list(rooms_map)[:-250]:
                del rooms_map[k]
        incoming = [m for m in msgs if m["sender_id"] != aid and m.get("room_id")]
        if incoming:
            _session["active_room"] = incoming[-1]["room_id"]

        # 只渲染「最近 MAX_WAIT_MESSAGES 条」+「所有点名你的消息」，其余折叠计数；游标已按全量
        # 推进，被折叠的旧消息不会再次投递（见模块顶部常量说明）。
        def _mentions_me(m: dict) -> bool:
            return m["sender_id"] != aid and aid in ((m.get("meta") or {}).get("mentions") or [])

        # 选出本次要渲染的（点名全留 + 最近优先按预算累计），其余折叠计数。
        shown, omitted = select_rendered(msgs, aid)
        lines = []
        mentioned_any = False
        for m in shown:
            me = "（你）" if m["sender_id"] == aid else ""
            room = f"[{m.get('room_name', '')}] " if m.get("room_name") else ""
            meta = m.get("meta") or {}
            tag = ""
            if _mentions_me(m):
                tag = " ‹@你·主要找你›" if meta.get("to") == aid else " ‹@你·被点名›"
                mentioned_any = True
            elif meta.get("to") and meta.get("to") != aid and m["sender_id"] != aid:
                # 这条主要发给别人——明确标注，免得你对正文里的 @文本自作主张地抢答。
                tag = f" 〔→ 主要发给 {meta.get('to_name') or '某实例'}，不是你〕"
            quote = ""
            if meta.get("reply_to_sender"):
                quote = f"（↩ 回复 {meta['reply_to_sender']}：{meta.get('reply_to_preview', '')}）"
            content = m.get("content") or ""
            if len(content) > MAX_MSG_CHARS:
                cut = len(content) - MAX_MSG_CHARS
                content = content[:MAX_MSG_CHARS] + f"…〔省略 {cut} 字，完整见 GUI〕"
            lines.append(
                f"{room}{m['sender_name']}{me}{tag} «{m['id']}»{quote}: {content}"
            )
        out = "\n".join(lines)
        if omitted:
            out = (
                f"〔为避免刷屏/撑爆上下文，已折叠较早的 {omitted} 条消息（完整记录见 GUI）；"
                "点名你的消息一律保留在下方。〕\n" + out
            )
        if mentioned_any:
            out += (
                "\n\n你被点名（‹被点名›）：请先用 send_message 回一句"
                "『收到，正在处理』，并带上 reply_to=被点名那条消息的 «id»"
                "（让对方在一堆回执里认出你在回应哪条），随后再着手处理。"
            )
        floor_hint = await _floor_hint(aid)
        if floor_hint:
            out += "\n\n" + floor_hint
        if wait:
            out += (
                "\n\n— 待命提醒：处理完请立刻再次 wait_for_messages 保持在线；"
                "需要征求用户意见时用 ask（在群里问并就地等回复），"
                "切勿用 AskUserQuestion 或结束本回合去问本地用户。"
            )
        return out

    # ----- 其它 ---------------------------------------------------------------

    @mcp.tool()
    async def list_peers(topic: str = "") -> str:
        """列出某主题群里的参与者及在线状态（缺省=当前主题）。"""
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            if not room_ref:
                return "没有当前主题，请用 topic 指定。"
            state = (await c.get("/api/state")).json()
            room = next(
                (r for r in state["rooms"] if r["id"] == room_ref or r["name"] == room_ref), None
            )
            if not room:
                return f"未找到主题「{room_ref}」。"
            agents = {a["id"]: a for a in state["agents"]}
        out = []
        for aid in room["agent_ids"]:
            a = agents.get(aid)
            if not a:
                continue
            on = "在线" if a.get("online") else "离线"
            tag = f"{a.get('role')}/" if a.get("role") else ""
            out.append(f"- {a['name']}（{tag}peer，{on}）")
        return "\n".join(out) or "（没有参与者）"

    @mcp.tool()
    async def leave_room(topic: str = "") -> str:
        """退出某个主题群（缺省=当前主题）；你仍保持在线、仍在待命，可被 invite 拉回或在
        其它主题继续。这不是下线——被请出某个群/让你"离开本大厅"时用它，别用 disconnect。"""
        aid = _session["agent_id"]
        if not aid:
            return "你尚未加入任何主题。"
        async with _client() as c:
            room_ref = topic or _session.get("active_room")
            match = await _resolve_room(c, room_ref) if room_ref else None
            if not match:
                return "未找到要退出的主题。"
            await c.delete(f"/api/rooms/{match['id']}/agents/{aid}")
        if _session.get("active_room") == match["id"]:
            _session["active_room"] = None
        return (
            f"已退出主题「{match['name']}」。你仍在线、仍在待命（没有下线），可被 invite 随时"
            "拉回。请继续 wait_for_messages 保持在线；被重新邀请时会自动回到对话。"
        )

    @mcp.tool()
    async def disconnect() -> str:
        """全局下线：标记离线（你的身份与历史在 GUI 中保留）。"""
        aid = _session["agent_id"]
        if not aid:
            return "你尚未上线。"
        async with _client() as c:
            await c.post(f"/api/peers/{aid}/leave")
        _session.update(agent_id=None, name=None, active_room=None, last_ts=0.0,
                        kicked=False, msg_rooms={})
        return "已下线。"

    return mcp


def main() -> None:
    try:
        server = build_server()
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        raise
    server.run()


if __name__ == "__main__":
    main()
