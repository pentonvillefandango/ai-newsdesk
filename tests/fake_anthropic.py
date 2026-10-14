"""A stand-in for anthropic.AsyncAnthropic: records messages.parse calls, replays scripted
replies."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx

# The SDK types these as its own HTTP client's classes; httpx objects behave the same at run time.
REQUEST: Any = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _response(status: int) -> Any:
    return httpx.Response(status, request=REQUEST)


def reply(
    parsed: object | None,
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 900,
    output_tokens: int = 150,
    cache_read: int = 0,
    cache_write: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


def rate_limited() -> anthropic.RateLimitError:
    return anthropic.RateLimitError("rate limited", response=_response(429), body=None)


def server_error() -> anthropic.InternalServerError:
    return anthropic.InternalServerError("overloaded", response=_response(500), body=None)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=REQUEST)


class _Messages:
    def __init__(self, replies: Sequence[object]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        nxt = self._replies.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class FakeAnthropic:
    def __init__(self, replies: Sequence[object]) -> None:
        self.messages = _Messages(replies)


def as_client(fake: FakeAnthropic) -> anthropic.AsyncAnthropic:
    return cast(anthropic.AsyncAnthropic, fake)
