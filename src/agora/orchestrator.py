"""The Orchestrator: runs a live conversation loop per room.

For each running room an asyncio task repeatedly: (1) picks the next speaker via the
configured strategy, (2) streams that agent's message token-by-token to the GUI, and
(3) waits a beat before the next turn. Humans can interject at any time and "@mention"
an agent to nominate who speaks next.
"""

from __future__ import annotations

import asyncio
import logging

from . import prompting
from .models import Agent, AgentKind, Message, Room, RoomStatus, Strategy

log = logging.getLogger("agora.orchestrator")


class Orchestrator:
    def __init__(self, hub) -> None:  # noqa: ANN001 - avoid circular import typing
        self.hub = hub
        self._tasks: dict[str, asyncio.Task] = {}
        self._rr_index: dict[str, int] = {}
        self._next_hint: dict[str, str] = {}

    # ----- lifecycle ----------------------------------------------------------

    async def start(self, room_id: str) -> None:
        room = self.hub.store.get_room(room_id)
        if not room:
            return
        existing = self._tasks.get(room_id)
        if room.status == RoomStatus.PAUSED:
            await self.hub.set_room_status(room_id, RoomStatus.RUNNING)
            return
        if existing and not existing.done():
            return
        await self.hub.set_room_status(room_id, RoomStatus.RUNNING)
        self._tasks[room_id] = asyncio.create_task(self._run(room_id))

    async def pause(self, room_id: str) -> None:
        room = self.hub.store.get_room(room_id)
        if room and room.status == RoomStatus.RUNNING:
            await self.hub.set_room_status(room_id, RoomStatus.PAUSED)

    async def stop(self, room_id: str) -> None:
        await self.hub.set_room_status(room_id, RoomStatus.IDLE)
        task = self._tasks.pop(room_id, None)
        if task and not task.done():
            task.cancel()

    def hint_next(self, room_id: str, agent_id: str) -> None:
        """Nominate the next speaker (used by @mentions in human messages)."""
        self._next_hint[room_id] = agent_id

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    # ----- main loop ----------------------------------------------------------

    async def _run(self, room_id: str) -> None:
        try:
            while True:
                room = self.hub.store.get_room(room_id)
                if not room or room.status == RoomStatus.IDLE:
                    break
                if room.status == RoomStatus.PAUSED:
                    await asyncio.sleep(0.2)
                    continue
                if room.turn >= room.max_turns:
                    await self._system(room, "Reached the turn limit for this run.")
                    await self.hub.set_room_status(room_id, RoomStatus.IDLE)
                    break

                speaker = await self._pick_speaker(room)
                if speaker is None:
                    await self._system(room, "The moderator wrapped up the discussion.")
                    await self.hub.set_room_status(room_id, RoomStatus.IDLE)
                    break

                await self._agent_turn(room, speaker)
                room.turn += 1
                await self.hub.set_room_status(room_id, RoomStatus.RUNNING)
                await self._interruptible_sleep(room.turn_delay, room_id)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - surface unexpected loop failures to the GUI
            log.exception("Conversation loop crashed for room %s", room_id)
            room = self.hub.store.get_room(room_id)
            if room:
                await self._system(room, "The conversation loop hit an error and stopped.")
                await self.hub.set_room_status(room_id, RoomStatus.IDLE)
        finally:
            self._tasks.pop(room_id, None)

    # ----- speaker selection --------------------------------------------------

    def _eligible_ai_agents(self, room: Room) -> list[Agent]:
        agents = []
        for aid in room.agent_ids:
            agent = self.hub.store.get_agent(aid)
            if agent and agent.enabled and agent.kind == AgentKind.AI:
                agents.append(agent)
        return agents

    def _room_agents(self, room: Room) -> list[Agent]:
        return [a for a in (self.hub.store.get_agent(i) for i in room.agent_ids) if a]

    async def _pick_speaker(self, room: Room) -> Agent | None:
        eligible = self._eligible_ai_agents(room)
        if not eligible:
            return None

        hint = self._next_hint.pop(room.id, None)
        if hint:
            for agent in eligible:
                if agent.id == hint:
                    return agent

        if room.strategy == Strategy.DIRECTOR:
            chosen = await self._director_pick(room, eligible)
            if chosen is not None:
                return chosen
            # Fall through to round-robin when the director is unavailable/unclear.

        return self._round_robin_pick(room, eligible)

    def _round_robin_pick(self, room: Room, eligible: list[Agent]) -> Agent:
        idx = self._rr_index.get(room.id, -1) + 1
        idx %= len(eligible)
        self._rr_index[room.id] = idx
        return eligible[idx]

    async def _director_pick(self, room: Room, eligible: list[Agent]) -> Agent | None:
        provider = self.hub.get_provider(self.hub.settings.resolved_provider())
        history = self.hub.store.history(room.id)
        prompt = prompting.build_director_prompt(room, eligible, history)
        try:
            raw = await provider.complete(
                system=prompting.DIRECTOR_SYSTEM,
                prompt=prompt,
                model=self.hub.settings.director_model,
                temperature=0.2,
                max_tokens=12,
            )
        except Exception:  # noqa: BLE001 - director is best-effort; degrade gracefully
            log.exception("Director call failed; using round-robin")
            return None

        text = raw.strip().lower()
        if "done" in text.split() or text == "done":
            return None
        for agent in eligible:
            if agent.name.lower() in text:
                return agent
        return None  # Unparseable -> caller falls back to round-robin.

    # ----- a single agent turn ------------------------------------------------

    async def _agent_turn(self, room: Room, agent: Agent) -> None:
        provider_name = self.hub.resolve_provider_name(agent)
        provider = self.hub.get_provider(provider_name)
        history = self.hub.store.history(room.id)
        system = prompting.build_system_prompt(agent, room, self._room_agents(room))
        prompt = prompting.build_agent_prompt(agent, history)

        message = Message(
            room_id=room.id,
            sender_id=agent.id,
            sender_name=agent.name,
            role="agent",
            color=agent.color,
            content="",
            meta={"model": agent.model or self.hub.settings.default_model},
        )

        await self.hub.set_agent_status(agent.id, "thinking")
        await self.hub.broadcast({"type": "message_start", "message": message.model_dump()})

        chunks: list[str] = []
        try:
            await self.hub.set_agent_status(agent.id, "speaking")
            async for delta in provider.stream(
                system=system,
                prompt=prompt,
                model=agent.model or self.hub.settings.default_model,
                temperature=agent.temperature,
                max_tokens=self.hub.settings.max_tokens,
            ):
                chunks.append(delta)
                await self.hub.broadcast(
                    {"type": "message_delta", "message_id": message.id, "delta": delta}
                )
        except Exception:  # noqa: BLE001
            log.exception("Agent %s failed to generate", agent.name)
            chunks.append("…(failed to respond)")

        message.content = _clean(("".join(chunks)).strip(), agent.name)
        self.hub.store.add_message(message)
        await self.hub.broadcast({"type": "message_end", "message": message.model_dump()})
        await self.hub.set_agent_status(agent.id, "idle")

    async def _system(self, room: Room, text: str) -> None:
        msg = Message(
            room_id=room.id, sender_id="system", sender_name="system", role="system", content=text
        )
        await self.hub.post_message(msg)

    async def _interruptible_sleep(self, seconds: float, room_id: str) -> None:
        steps = max(1, int(seconds / 0.1))
        for _ in range(steps):
            room = self.hub.store.get_room(room_id)
            if not room or room.status != RoomStatus.RUNNING:
                return
            await asyncio.sleep(0.1)


def _clean(text: str, name: str) -> str:
    """Strip an accidental leading 'Name:' prefix the model may add."""
    prefix = f"{name}:"
    if text.lower().startswith(prefix.lower()):
        text = text[len(prefix):].lstrip()
    return text or "…"
