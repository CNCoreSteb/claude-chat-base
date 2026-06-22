"""Prompt construction for group chat.

Group chat doesn't map cleanly onto a two-party user/assistant transcript, so we
take a simpler, robust approach: the full conversation is rendered as a labeled
transcript inside a single user message, and the agent's persona plus the chat
rules live in the system prompt. The model then writes only its next line.
"""

from __future__ import annotations

from .models import Agent, Message, Room

MAX_TRANSCRIPT_MESSAGES = 60  # Keep prompts bounded for long-running rooms.


def participants_blurb(agents: list[Agent], me: Agent) -> str:
    others = [a for a in agents if a.id != me.id]
    if not others:
        return "You are the only participant so far."
    lines = []
    for a in others:
        summary = a.persona.strip().splitlines()
        first = next((s.strip() for s in summary if s.strip()), "")
        lines.append(f"- {a.name}: {first}" if first else f"- {a.name}")
    return "Other participants in this room:\n" + "\n".join(lines)


def build_system_prompt(agent: Agent, room: Room, agents: list[Agent]) -> str:
    persona = agent.persona.strip() or f"You are {agent.name}, a thoughtful participant."
    goal = room.topic.strip() or "an open-ended discussion"
    return (
        f"You are {agent.name}, a participant in a live group chat called "
        f'"{room.name}".\n\n'
        f"{persona}\n\n"
        f"{participants_blurb(agents, agent)}\n\n"
        f"The goal of this conversation: {goal}\n\n"
        "Group chat rules:\n"
        f"- Always stay in character as {agent.name}. Never speak for anyone else.\n"
        "- Keep it conversational and concise — a few sentences, like a real chat.\n"
        "- Build on what others said; address people by name when it helps. Don't repeat points.\n"
        "- Move the discussion forward toward the goal with something concrete.\n"
        "- Reply with ONLY your message text. Do not prefix it with your name.\n"
        "- If you genuinely have nothing to add or the goal is met, say so in one short line."
    )


def render_transcript(history: list[Message]) -> str:
    recent = history[-MAX_TRANSCRIPT_MESSAGES:]
    if not recent:
        return "[The chat is empty. You may open the conversation.]"
    lines = ["[Conversation so far]"]
    for m in recent:
        if m.role == "system":
            lines.append(f"(system) {m.content}")
        else:
            lines.append(f"{m.sender_name}: {m.content}")
    return "\n".join(lines)


def build_agent_prompt(agent: Agent, history: list[Message]) -> str:
    return (
        f"{render_transcript(history)}\n\n"
        f"Now write your next message as {agent.name}."
    )


def build_director_prompt(room: Room, agents: list[Agent], history: list[Message]) -> str:
    roster = ", ".join(a.name for a in agents) or "(none)"
    goal = room.topic.strip() or "an open-ended discussion"
    return (
        f"You are the moderator of a group chat called \"{room.name}\".\n"
        f"Goal: {goal}\n"
        f"Participants you may choose from: {roster}\n\n"
        f"{render_transcript(history)}\n\n"
        "Decide who should speak next to keep the conversation productive and lively. "
        "Prefer someone who hasn't spoken recently and can add a fresh angle. "
        "Reply with ONLY one participant name from the list, or the single word DONE "
        "if the goal is clearly met or the discussion has stalled."
    )


DIRECTOR_SYSTEM = (
    "You are a concise, fair group-chat moderator. You only ever respond with a "
    "single participant name or the word DONE."
)
