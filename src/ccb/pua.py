"""PUA 模式：对某主题（默认大厅）强制的多阶段协同工作流。

状态机（硬拦截，不符合当前阶段/轮次的发送被 server 以 409 挡回）：

    off → onboarding（每个在线 peer 各发一条上报：项目/状态/三个假设需求问题）
        → 自动给每人的整条上报建一条全局 TODO
        → critique（每人对每条 todo 按角色质疑，或 pua_pass 跳过）
        → review（该 todo 的发送人逐条回应所有质疑）
        → 循环 critique↔review，直到某轮无人质疑（无可迭代）或人工停止。

onboarding 计时：每人上报后开一个"安静窗口"（默认 60s，可调），已上报者窗口内不能再发；
有新 peer 加入则重置并暂停窗口、并进入"只让新人补报"的刷新态，新人上报后重启窗口；全员已报
且窗口走完无新人 → 进入下一阶段。新 peer 在任意阶段加入都会再次触发该刷新态（只让新人发/建）。

PUA 状态是按房间的内存状态（与应答位一样，重启即清；产出的全局 TODO 已持久化）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .models import Message

if TYPE_CHECKING:
    from .hub import Hub

PUA_SILENT_WINDOW = 60.0  # onboarding 安静窗口默认秒数（enable 时可调）


class Pua:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.rooms: dict[str, dict[str, Any]] = {}

    # ----- 查询 -------------------------------------------------------------

    def active(self, room_id: str) -> bool:
        return bool(self.rooms.get(room_id))

    def _members(self, room_id: str) -> list[str]:
        """该房间当前在线的 peer 成员 id（onboarding/质疑的参与集合）。"""
        room = self.hub.store.get_room(room_id)
        if not room:
            return []
        out = []
        for aid in room.agent_ids:
            a = self.hub.store.get_agent(aid)
            if a and a.online:
                out.append(aid)
        return out

    def snapshot(self, room_id: str) -> dict[str, Any] | None:
        st = self.rooms.get(room_id)
        if not st:
            return None
        now = time.time()
        secs = None
        if st["deadline"]:
            secs = max(0.0, st["deadline"] - now)
        return {
            "active": True,
            "phase": st["phase"],
            "round": st["round"],
            "window": st["window"],
            "reported": list(st["reported"]),
            "expected": self._members(room_id) if st["phase"] == "onboarding" else [],
            "refresh": st["refresh"],
            "seconds_to_advance": secs,
            "todos": [{"id": t["id"], "owner": t["owner"], "owner_name": t["owner_name"]}
                      for t in st["todos"]],
            "progress": self._progress(room_id, st),
        }

    def _progress(self, room_id: str, st: dict) -> str:
        members = self._members(room_id)
        if st["phase"] == "onboarding":
            return f"已上报 {len(st['reported'])}/{len(members)}"
        if st["phase"] == "critique":
            done = sum(len(st["acted"].get(t["id"], set()) & set(members))
                       for t in st["todos"])
            need = sum(max(0, len(set(members) - {t["owner"]})) for t in st["todos"])
            return f"质疑（第 {st['round']} 轮）{done}/{need}"
        if st["phase"] == "review":
            done = sum(len(st["reviewed"].get(t["id"], set())) for t in st["todos"])
            need = sum(len(st["critiques"].get(t["id"], [])) for t in st["todos"])
            return f"审查（第 {st['round']} 轮）{done}/{need}"
        if st["phase"] == "done":
            return "已完成（无可迭代）"
        return ""

    async def _broadcast(self, room_id: str) -> None:
        await self.hub.broadcast(
            {"type": "pua", "room_id": room_id, "pua": self.snapshot(room_id)})

    def agent_hint(self, room_id: str, agent_id: str) -> str | None:
        """给某实例的"现在该你做什么"提示（用于 wait 输出）。"""
        st = self.rooms.get(room_id)
        if not st:
            return None
        phase = st["phase"]
        if phase == "onboarding":
            if agent_id not in self._members(room_id):
                return None
            if agent_id in st["reported"]:
                left = [m for m in self._members(room_id) if m not in st["reported"]]
                tail = f"（还差 {len(left)} 人）" if left else "（窗口走完即进入质疑）"
                return f"〔PUA·上报〕你已上报，请静默 wait 等其他实例上报完{tail}。"
            return ("〔PUA·上报阶段〕请发一条消息：你的项目、当前状态、基于你项目提出的"
                    "三个假设需求问题。发完后静默 wait，等其他实例上报。")
        if phase == "critique":
            lines = [f"- «{t['anchor']}»（{t['owner_name']} 的 todo）"
                     for t in st["todos"]
                     if t["owner"] != agent_id
                     and agent_id not in st["acted"].get(t["id"], set())]
            if not lines:
                return "〔PUA·质疑〕你本轮已对该质疑的 todo 全部表态，静默 wait 等本轮结束。"
            return ("〔PUA·质疑阶段〕请对下列 todo 按你的角色 reply_to 其报告消息提质疑或问进度，"
                    "本轮无意见就 pua_pass 跳过：\n" + "\n".join(lines))
        if phase == "review":
            mine = [t for t in st["todos"] if t["owner"] == agent_id]
            if not mine:
                return "〔PUA·审查〕等各 todo 主人逐条回应质疑，你静默 wait。"
            pending = sum(
                len(set(st["critiques"].get(t["id"], [])) - st["reviewed"].get(t["id"], set()))
                for t in mine)
            if not pending:
                return "〔PUA·审查〕你已回应完针对你 todo 的质疑，静默 wait 等其他人。"
            return (f"〔PUA·审查阶段〕你是 {len(mine)} 条 todo 的主人，请逐条 reply_to 回应针对它的"
                    f"每条质疑（群里能看到它们 reply 了你的上报），共 {pending} 条待回应。")
        if phase == "done":
            return "〔PUA·完成〕本主题协同已结束，请静默 wait，等人工关闭 PUA。"
        return None

    # ----- 开关 -------------------------------------------------------------

    async def enable(self, room_id: str, window: float = PUA_SILENT_WINDOW) -> bool:
        if not self.hub.store.get_room(room_id):
            return False
        self.rooms[room_id] = {
            "phase": "onboarding", "round": 1, "window": max(1.0, window),
            "reported": set(), "deadline": None, "refresh": False,
            "todos": [], "acted": {}, "critiques": {}, "reviewed": {},
        }
        await self.hub.post_message(Message(
            room_id=room_id, sender_id="system", sender_name="system", role="system",
            content="〔PUA 模式·上报阶段〕请每个在线实例各发一条消息：你的项目、当前状态、"
                    "以及基于你项目提出的三个假设需求问题。发完后静默等其他人。"))
        await self._broadcast(room_id)
        return True

    async def disable(self, room_id: str) -> None:
        if self.rooms.pop(room_id, None) is not None:
            await self.hub.post_message(Message(
                room_id=room_id, sender_id="system", sender_name="system", role="system",
                content="〔PUA 模式已关闭〕恢复自由发言。"))
            await self.hub.broadcast({"type": "pua", "room_id": room_id, "pua": None})

    # ----- 硬拦截：post_message 前调用，返回拒绝原因则该消息应被 409 挡回 -----------

    def blocks(self, room_id: str, sender_id: str, reply_to: str) -> str | None:
        st = self.rooms.get(room_id)
        if not st or sender_id in ("human", "system"):
            return None
        phase = st["phase"]
        if phase == "done":
            return "〔PUA〕本主题协同已完成，请等人工关闭 PUA 或开新主题。"
        if phase == "onboarding":
            if sender_id in st["reported"]:
                return "〔PUA·上报阶段〕你已上报，请静默等其他实例上报完。"
            if sender_id not in self._members(room_id):
                return "〔PUA·上报阶段〕请稍候。"
            return None  # 允许它上报
        if phase == "critique":
            todo = self._todo_by_anchor(st, reply_to)
            if not todo:
                return ("〔PUA·质疑阶段〕请用 reply_to 引用某条 todo 的报告消息来按你的角色质疑/"
                        "问进度；本轮对它无意见就用 pua_pass 跳过。")
            if todo["owner"] == sender_id:
                return "〔PUA·质疑阶段〕不质疑你自己的 todo，请质疑别人的或 pua_pass。"
            if sender_id in st["acted"].get(todo["id"], set()):
                return "〔PUA·质疑阶段〕你本轮已对这条 todo 表过态了，去处理别的 todo。"
            return None
        if phase == "review":
            crit = self._critique_by_msg(st, reply_to)
            if not crit:
                return "〔PUA·审查阶段〕请 reply_to 引用一条针对你 todo 的质疑来逐条回应。"
            if crit["owner"] != sender_id:
                return "〔PUA·审查阶段〕现在只由各 todo 的发送人回应针对自己 todo 的质疑。"
            if reply_to in st["reviewed"].get(crit["todo_id"], set()):
                return "〔PUA·审查阶段〕这条质疑你已回应过。"
            return None
        return None

    def _todo_by_anchor(self, st: dict, anchor_msg: str) -> dict | None:
        for t in st["todos"]:
            if t["anchor"] == anchor_msg:
                return t
        return None

    def _critique_by_msg(self, st: dict, crit_msg: str) -> dict | None:
        for t in st["todos"]:
            if crit_msg in st["critiques"].get(t["id"], []):
                return {"todo_id": t["id"], "owner": t["owner"]}
        return None

    # ----- 消息入库后推进状态机 ----------------------------------------------

    async def on_message(self, message: Message) -> None:
        st = self.rooms.get(message.room_id)
        if not st or message.role != "agent":
            return
        sender = message.sender_id
        phase = st["phase"]
        if phase == "onboarding":
            if sender in self._members(message.room_id) and sender not in st["reported"]:
                st["reported"].add(sender)
                # 该实例的整条上报 → 一条全局 todo（归它名下），锚点=这条上报消息。
                a = self.hub.store.get_agent(sender)
                todo = await self.hub.add_todo(sender, "global", "", message.content)
                st["todos"].append({"id": todo.id, "owner": sender,
                                    "owner_name": a.name if a else sender,
                                    "anchor": message.id})
                # 上报即（重）启安静窗口；若还在等新人补报则不开窗。
                if not st["refresh"]:
                    st["deadline"] = time.time() + st["window"]
                await self._maybe_advance(message.room_id)
                await self._broadcast(message.room_id)
        elif phase == "critique":
            todo = self._todo_by_anchor(st, message.meta.get("reply_to", ""))
            acted = st["acted"].get(todo["id"], set()) if todo else set()
            if todo and todo["owner"] != sender and sender not in acted:
                st["acted"].setdefault(todo["id"], set()).add(sender)
                st["critiques"].setdefault(todo["id"], []).append(message.id)
                await self._maybe_advance(message.room_id)
                await self._broadcast(message.room_id)
        elif phase == "review":
            rt = message.meta.get("reply_to", "")
            crit = self._critique_by_msg(st, rt)
            if crit and crit["owner"] == sender:
                st["reviewed"].setdefault(crit["todo_id"], set()).add(rt)
                await self._maybe_advance(message.room_id)
                await self._broadcast(message.room_id)

    async def pass_todo(self, room_id: str, agent_id: str, ref: str) -> str:
        """质疑阶段：本轮对某 todo 无意见，跳过（也算"已行动"，推动流程）。
        ref 可以是 todo id，也可以是它的报告消息 «id»（即质疑时 reply_to 的那个）。"""
        st = self.rooms.get(room_id)
        if not st or st["phase"] != "critique":
            return "现在不是质疑阶段。"
        todo = next((t for t in st["todos"] if ref in (t["id"], t["anchor"])), None)
        if not todo:
            return "未找到该 todo（用 wait 提示里列出的 «id»）。"
        if todo["owner"] == agent_id:
            return "这是你自己的 todo，无需质疑/跳过。"
        st["acted"].setdefault(todo["id"], set()).add(agent_id)
        await self._maybe_advance(room_id)
        await self._broadcast(room_id)
        return "已跳过这条 todo。"

    async def on_join(self, room_id: str, agent_id: str) -> None:
        """新 peer 加入：再次进入静默/补报态，只让新人发与建（其余人继续被锁）。"""
        st = self.rooms.get(room_id)
        if not st or agent_id in st["reported"]:
            return  # 没开 PUA / 老成员已上报过：不重置
        if agent_id not in self._members(room_id):
            return  # 还不是在线成员
        already_refresh = st["phase"] == "onboarding" and st["refresh"]
        st["phase"] = "onboarding"
        st["refresh"] = True
        st["deadline"] = None  # 暂停窗口，等新人上报
        if not already_refresh:
            await self.hub.post_message(Message(
                room_id=room_id, sender_id="system", sender_name="system", role="system",
                content="〔PUA·新成员加入〕请新加入的实例补发一条上报（项目/状态/三个假设需求问题）；"
                        "其余实例继续静默。"))
        await self._broadcast(room_id)

    async def tick(self) -> None:
        """reconcile 周期调用：onboarding 安静窗口到点则推进。"""
        for room_id in list(self.rooms):
            await self._maybe_advance(room_id)

    # ----- 阶段推进 ----------------------------------------------------------

    async def _maybe_advance(self, room_id: str) -> None:
        st = self.rooms.get(room_id)
        if not st:
            return
        members = self._members(room_id)
        phase = st["phase"]
        if phase == "onboarding":
            if not members or not all(m in st["reported"] for m in members):
                return  # 还有人没报
            if st["refresh"]:
                # 新人补报完成：重启窗口，回到正常 onboarding 收尾。
                st["refresh"] = False
                st["deadline"] = time.time() + st["window"]
                return
            if st["deadline"] and time.time() >= st["deadline"]:
                await self._start_critique(room_id, fresh=True)
        elif phase == "critique":
            for t in st["todos"]:
                need = set(members) - {t["owner"]}
                if not need <= st["acted"].get(t["id"], set()):
                    return  # 还有人没对某 todo 表态
            # 全员对所有 todo 都行动了：有质疑→审查；零质疑→完成。
            if any(st["critiques"].get(t["id"]) for t in st["todos"]):
                st["phase"] = "review"
                await self.hub.post_message(Message(
                    room_id=room_id, sender_id="system", sender_name="system", role="system",
                    content=f"〔PUA·审查阶段（第 {st['round']} 轮）〕各 todo 的发送人请逐条 "
                            "reply_to 回应针对自己 todo 的每条质疑。"))
                await self._broadcast(room_id)
            else:
                await self._finish(room_id)
        elif phase == "review":
            for t in st["todos"]:
                crits = set(st["critiques"].get(t["id"], []))
                if not crits <= st["reviewed"].get(t["id"], set()):
                    return  # 还有质疑没回应
            # 本轮审查完毕 → 开下一轮质疑。
            st["round"] += 1
            await self._start_critique(room_id, fresh=False)

    async def _start_critique(self, room_id: str, fresh: bool) -> None:
        st = self.rooms[room_id]
        st["phase"] = "critique"
        st["acted"] = {}
        st["critiques"] = {}
        st["reviewed"] = {}
        word = "进入质疑阶段" if fresh else f"进入第 {st['round']} 轮质疑"
        await self.hub.post_message(Message(
            room_id=room_id, sender_id="system", sender_name="system", role="system",
            content=f"〔PUA·{word}〕请每个实例对每条 todo（除你自己的）按你的角色 reply_to 引用其"
                    "报告消息提出质疑或问进度；本轮对某条无意见就 pua_pass 跳过。"))
        await self._broadcast(room_id)
        # 全员对所有 todo 都行动后会自动进审查；可能刚开轮就已满足（无成员可质疑）。
        await self._maybe_advance(room_id)

    async def _finish(self, room_id: str) -> None:
        st = self.rooms.get(room_id)
        if not st:
            return
        st["phase"] = "done"
        st["deadline"] = None
        await self.hub.post_message(Message(
            room_id=room_id, sender_id="system", sender_name="system", role="system",
            content="〔PUA·完成〕本轮无人再质疑，协同迭代结束。人工可关闭 PUA 恢复自由发言。"))
        await self._broadcast(room_id)
