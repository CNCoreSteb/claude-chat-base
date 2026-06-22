from __future__ import annotations

from agora.llm import MockProvider
from agora.models import Agent, Message, Room
from agora.prompting import (
    build_agent_prompt,
    build_director_prompt,
    build_system_prompt,
    render_transcript,
)


async def test_mock_stream_yields_text():
    provider = MockProvider(seed=1)
    chunks = [
        c async for c in provider.stream(
            system="You are Ada, an engineer.", prompt="[Conversation so far]\nTheo: hi",
            model="m", temperature=0.5, max_tokens=50,
        )
    ]
    assert "".join(chunks).strip()


async def test_mock_complete_returns_text():
    provider = MockProvider(seed=2)
    out = await provider.complete(
        system="You are Ada.", prompt="x", model="m", temperature=0.2, max_tokens=10
    )
    assert isinstance(out, str) and out


def test_system_prompt_mentions_name_topic_participants():
    ada = Agent(name="Ada", persona="You are Ada, an engineer.")
    theo = Agent(name="Theo", persona="You are Theo, a designer.")
    room = Room(name="Studio", topic="Pick a logo", agent_ids=[ada.id, theo.id])
    prompt = build_system_prompt(ada, room, [ada, theo])
    assert "Ada" in prompt
    assert "Pick a logo" in prompt
    assert "Theo" in prompt


def test_render_transcript_formats_speakers():
    msgs = [
        Message(room_id="r", sender_id="a", sender_name="Ada", content="hi"),
        Message(room_id="r", sender_id="sys", sender_name="system", role="system", content="note"),
    ]
    text = render_transcript(msgs)
    assert "Ada: hi" in text
    assert "(system) note" in text


def test_agent_and_director_prompts():
    ada = Agent(name="Ada")
    room = Room(name="R", topic="goal", agent_ids=[ada.id])
    assert "Ada" in build_agent_prompt(ada, [])
    director = build_director_prompt(room, [ada], [])
    assert "Ada" in director
    assert "DONE" in director
