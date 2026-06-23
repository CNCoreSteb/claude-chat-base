"""LLM 提供方抽象层。

⚠️ 休眠模块：仅供 API 驱动的 AI 智能体自动对话使用，而该功能当前已停用
（见 orchestrator.py），本项目暂时只专注于多 Claude Code 协作。保留以便日后恢复。


内置两种提供方：

* ``AnthropicProvider``——通过官方 SDK 流式获取真实的 Claude 回复。
* ``MockProvider``——确定性的、离线的、带人设风格的文本，让整个系统（GUI、流式、
  编排）在零配置、无 API 密钥的情况下也能跑起来。

两者暴露相同的异步接口：``stream`` 逐块产出文本，``complete`` 返回完整字符串
（供主持人 / 导演使用）。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Protocol


class LLMProvider(Protocol):
    name: str

    async def stream(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]: ...

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str: ...


class AnthropicProvider:
    """通过 Anthropic Messages API 流式获取回复。"""

    name = "anthropic"

    def __init__(self, api_key: str) -> None:
        # 延迟导入，使得在未配置密钥 / SDK 时本包仍可正常工作。
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)

    async def stream(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        async with self._client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        msg = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in msg.content if block.type == "text")


# 用于离线演示的、与具体人设无关的简短对话片段。
_MOCK_OPENERS = [
    "接着刚才的话题，",
    "我的看法是：",
    "我想追问一点——",
    "在这个基础上，",
    "换个角度看，",
    "我有一个顾虑：",
    "具体来说，",
    "我大体同意，不过",
]
_MOCK_BODIES = [
    "我们应该锚定真实用户，而不是抽象概念。",
    "最棘手的是跨平台的状态同步，别一笔带过。",
    "不如先发布一个最小可用版本，再从中学习？",
    "这里是速度和正确性的权衡，我更倾向于正确性。",
    "把命名理清楚，能省掉以后好几场争论。",
    "把决定写下来，免得下周又重新讨论一遍。",
    "离线场景往往是设计悄悄崩溃的地方。",
    "现在我宁愿在数据模型上多投入，而不是界面装饰。",
]


class MockProvider:
    """离线提供方，编造出貌似合理、带人设色彩的发言。"""

    name = "mock"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def _line(self, prompt: str) -> str:
        opener = self._rng.choice(_MOCK_OPENERS)
        body = self._rng.choice(_MOCK_BODIES)
        # 偶尔点名上一个发言者，让它更像真实群聊。
        last_speaker = _last_speaker_from_prompt(prompt)
        if last_speaker and self._rng.random() < 0.5:
            return f"{opener}{last_speaker}，{body}"
        return f"{opener}{body}"

    async def stream(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        for piece in _pieces(self._line(prompt)):
            await asyncio.sleep(0.04)
            yield piece

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        await asyncio.sleep(0.02)
        return self._line(prompt)


def _pieces(text: str):
    """把文本切成适合流式输出的小片：英文按词、中文逐字，兼顾流式观感。"""
    buf = ""
    for ch in text:
        if ch == " ":
            if buf:
                yield buf + " "
                buf = ""
            else:
                yield " "
        elif ord(ch) > 0x2E80:  # CJK 及全角符号区间，逐字输出
            if buf:
                yield buf
                buf = ""
            yield ch
        else:
            buf += ch
    if buf:
        yield buf


def _last_speaker_from_prompt(prompt: str) -> str | None:
    """尽力从对话记录里提取最近一条 "名字: 内容" 的发言者名字。"""
    for line in reversed(prompt.splitlines()):
        line = line.strip()
        if ":" in line or "：" in line:
            sep = ":" if ":" in line else "："
            candidate = line.split(sep, 1)[0].strip()
            if candidate.startswith("[") or candidate.startswith("("):
                continue
            if 0 < len(candidate) <= 24 and " " not in candidate:
                return candidate
    return None
