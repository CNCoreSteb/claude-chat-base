from __future__ import annotations

import asyncio

from agora.hub import Hub
from agora.models import Agent, AgentKind, Room, RoomStatus, Strategy


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_round_robin_runs_and_stops_at_max_turns(populated_hub: Hub):
    hub = populated_hub
    room = next(iter(hub.store.rooms.values()))
    await hub.orchestrator.start(room.id)

    ok = await _wait_for(lambda: room.status == RoomStatus.IDLE and room.turn >= room.max_turns)
    assert ok, f"room did not finish: status={room.status} turn={room.turn}"

    agent_msgs = [m for m in hub.store.history(room.id) if m.role == "agent"]
    assert len(agent_msgs) == room.max_turns
    # Round-robin should alternate between the two agents.
    senders = [m.sender_name for m in agent_msgs]
    assert senders[0] != senders[1]


async def test_round_robin_pick_cycles(populated_hub: Hub):
    hub = populated_hub
    room = next(iter(hub.store.rooms.values()))
    eligible = hub.orchestrator._eligible_ai_agents(room)
    picks = [hub.orchestrator._round_robin_pick(room, eligible).name for _ in range(4)]
    assert picks == [eligible[0].name, eligible[1].name, eligible[0].name, eligible[1].name]


async def test_stop_halts_the_loop(populated_hub: Hub):
    hub = populated_hub
    room = next(iter(hub.store.rooms.values()))
    room.max_turns = 1000
    room.turn_delay = 0.05
    await hub.orchestrator.start(room.id)
    await _wait_for(lambda: room.turn >= 1)
    await hub.orchestrator.stop(room.id)
    assert await _wait_for(lambda: room.status == RoomStatus.IDLE)
    turn_after_stop = room.turn
    await asyncio.sleep(0.2)
    assert room.turn == turn_after_stop  # no further turns after stop


async def test_peer_agents_are_not_auto_generated(hub: Hub):
    peer = hub.store.add_agent(Agent(name="ClaudeX", kind=AgentKind.PEER))
    room = hub.store.add_room(
        Room(name="R", agent_ids=[peer.id], strategy=Strategy.ROUND_ROBIN, max_turns=3)
    )
    await hub.orchestrator.start(room.id)
    # No eligible AI agents -> the loop should end immediately without generating.
    assert await _wait_for(lambda: room.status == RoomStatus.IDLE)
    assert [m for m in hub.store.history(room.id) if m.role == "agent"] == []


async def test_mention_hint_directs_next_speaker(populated_hub: Hub):
    hub = populated_hub
    room = next(iter(hub.store.rooms.values()))
    ada = next(a for a in hub.store.agents.values() if a.name == "Ada")
    hub.orchestrator.hint_next(room.id, ada.id)
    chosen = await hub.orchestrator._pick_speaker(room)
    assert chosen.id == ada.id
    # hint is consumed after use
    assert room.id not in hub.orchestrator._next_hint
