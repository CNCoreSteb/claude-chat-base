"""LLM provider abstraction.

Two providers are bundled:

* ``AnthropicProvider`` — streams real Claude responses via the official SDK.
* ``MockProvider`` — deterministic, offline persona-flavored text so the whole
  system (GUI, streaming, orchestration) works with zero setup and no API key.

Both expose the same async interface: ``stream`` yields text chunks, and
``complete`` returns a full string (used by the moderator/director).
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
    """Streams responses from the Anthropic Messages API."""

    name = "anthropic"

    def __init__(self, api_key: str) -> None:
        # Imported lazily so the package works without the key/SDK configured.
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


# Short, persona-agnostic conversational fragments for the offline demo.
_MOCK_OPENERS = [
    "Picking up on that —",
    "Here's my angle:",
    "I'd push on one thing.",
    "Building on what was said,",
    "Let me reframe this.",
    "One concern I have:",
    "Concretely, then:",
    "I mostly agree, but",
]
_MOCK_BODIES = [
    "we should anchor this on the actual user, not the abstraction.",
    "the risky part is state sync across platforms — let's not hand-wave it.",
    "what if we ship the smallest useful slice first and learn from it?",
    "the trade-off is speed versus correctness, and I'd bias to correctness here.",
    "naming this clearly will save us three arguments later.",
    "let's write down the decision so we don't relitigate it next week.",
    "the offline case is where most designs quietly fall apart.",
    "I'd rather over-invest in the data model than the UI chrome right now.",
]


class MockProvider:
    """Offline provider that fabricates plausible, persona-tinted chatter."""

    name = "mock"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def _line(self, system: str, prompt: str) -> str:
        # Derive a stable-ish name from the system prompt's "You are X" preamble.
        name = "Someone"
        marker = "You are "
        if marker in system:
            after = system.split(marker, 1)[1]
            name = after.split(",", 1)[0].split(".", 1)[0].split(" participating")[0].strip()
        opener = self._rng.choice(_MOCK_OPENERS)
        body = self._rng.choice(_MOCK_BODIES)
        # Occasionally address the previous speaker to make it feel like a chat.
        last_speaker = _last_speaker_from_prompt(prompt)
        if last_speaker and self._rng.random() < 0.5:
            return f"{opener} {last_speaker}, {body}"
        return f"{opener} {body}" if name else body

    async def stream(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        text = self._line(system, prompt)
        for word in text.split(" "):
            await asyncio.sleep(0.04)
            yield word + " "

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
        return self._line(system, prompt)


def _last_speaker_from_prompt(prompt: str) -> str | None:
    """Best-effort extraction of the most recent 'Name: ...' line in a transcript."""
    for line in reversed(prompt.splitlines()):
        line = line.strip()
        if ":" in line and not line.startswith("["):
            candidate = line.split(":", 1)[0].strip()
            if 0 < len(candidate) <= 24 and " " not in candidate:
                return candidate
    return None
