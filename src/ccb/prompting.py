"""群聊的提示词构造。

群聊并不能干净地映射到"两方 user/assistant"的对话结构，所以这里采用更简单、
稳健的做法：把完整对话渲染成带说话人标签的"对话记录"，放进一条 user 消息里；
而智能体的人设和群聊规则放进 system 提示词。模型只需写出它的下一句发言。
"""

from __future__ import annotations

from .models import Agent, Message, Room

MAX_TRANSCRIPT_MESSAGES = 60  # 限制提示词规模，避免长时间运行的房间无限膨胀。


def participants_blurb(agents: list[Agent], me: Agent) -> str:
    others = [a for a in agents if a.id != me.id]
    if not others:
        return "目前只有你一个参与者。"
    lines = []
    for a in others:
        summary = a.persona.strip().splitlines()
        first = next((s.strip() for s in summary if s.strip()), "")
        lines.append(f"- {a.name}：{first}" if first else f"- {a.name}")
    return "本房间的其他参与者：\n" + "\n".join(lines)


def build_system_prompt(agent: Agent, room: Room, agents: list[Agent]) -> str:
    persona = agent.persona.strip() or f"你是 {agent.name}，一位深思熟虑的参与者。"
    goal = room.topic.strip() or "一场开放式的讨论"
    return (
        f"你是 {agent.name}，正在一个名为「{room.name}」的实时群聊里参与讨论。\n\n"
        f"{persona}\n\n"
        f"{participants_blurb(agents, agent)}\n\n"
        f"这场对话的目标：{goal}\n\n"
        "群聊规则：\n"
        f"- 始终以 {agent.name} 的身份说话，绝不替别人发言。\n"
        "- 保持口语化、简洁——几句话即可，像真实聊天那样。\n"
        "- 在别人说的基础上推进；需要时直接点名。不要重复已经说过的内容。\n"
        "- 用具体的内容把讨论向目标推进。\n"
        "- 只回复你这一句发言本身，不要在前面加自己的名字。\n"
        "- 如果你确实没有可补充的，或目标已达成，用一句话简短说明即可。"
    )


def render_transcript(history: list[Message]) -> str:
    recent = history[-MAX_TRANSCRIPT_MESSAGES:]
    if not recent:
        return "[对话还是空的，你可以来开个头。]"
    lines = ["[对话记录]"]
    for m in recent:
        if m.role == "system":
            lines.append(f"（系统）{m.content}")
        else:
            lines.append(f"{m.sender_name}: {m.content}")
    return "\n".join(lines)


def build_agent_prompt(agent: Agent, history: list[Message]) -> str:
    return f"{render_transcript(history)}\n\n现在，请以 {agent.name} 的身份写出你的下一句发言。"


def build_director_prompt(room: Room, agents: list[Agent], history: list[Message]) -> str:
    roster = "、".join(a.name for a in agents) or "（无）"
    goal = room.topic.strip() or "一场开放式的讨论"
    return (
        f"你是名为「{room.name}」的群聊的主持人。\n"
        f"目标：{goal}\n"
        f"可供你选择的参与者：{roster}\n\n"
        f"{render_transcript(history)}\n\n"
        "请决定下一个应该由谁发言，以让讨论保持高效、生动。"
        "优先选择最近没怎么发言、且能带来新角度的人。"
        "只回复名单中的一个参与者名字；如果目标已明显达成、或讨论已陷入停滞，"
        "就只回复一个词 DONE。"
    )


DIRECTOR_SYSTEM = "你是一位简洁、公正的群聊主持人。你每次只会回复一个参与者名字，或者回复 DONE。"
