from __future__ import annotations

from ccb.llm import MockProvider
from ccb.models import Agent, Message, Room
from ccb.prompting import (
    build_agent_prompt,
    build_director_prompt,
    build_system_prompt,
    render_transcript,
)


async def test_mock_stream_yields_text():
    provider = MockProvider(seed=1)
    chunks = [
        c async for c in provider.stream(
            system="你是阿工，一名工程师。", prompt="[对话记录]\n小设: 你好",
            model="m", temperature=0.5, max_tokens=50,
        )
    ]
    assert "".join(chunks).strip()


async def test_mock_complete_returns_text():
    provider = MockProvider(seed=2)
    out = await provider.complete(
        system="你是阿工。", prompt="x", model="m", temperature=0.2, max_tokens=10
    )
    assert isinstance(out, str) and out


def test_system_prompt_mentions_name_topic_participants():
    gong = Agent(name="阿工", persona="你是阿工，一名工程师。")
    she = Agent(name="小设", persona="你是小设，一名设计师。")
    room = Room(name="工作室", topic="挑一个图标", agent_ids=[gong.id, she.id])
    prompt = build_system_prompt(gong, room, [gong, she])
    assert "阿工" in prompt
    assert "挑一个图标" in prompt
    assert "小设" in prompt


def test_render_transcript_formats_speakers():
    msgs = [
        Message(room_id="r", sender_id="a", sender_name="阿工", content="嗨"),
        Message(room_id="r", sender_id="sys", sender_name="system", role="system", content="备注"),
    ]
    text = render_transcript(msgs)
    assert "阿工: 嗨" in text
    assert "（系统）备注" in text


def test_agent_and_director_prompts():
    gong = Agent(name="阿工")
    room = Room(name="房间", topic="目标", agent_ids=[gong.id])
    assert "阿工" in build_agent_prompt(gong, [])
    director = build_director_prompt(room, [gong], [])
    assert "阿工" in director
    assert "DONE" in director
