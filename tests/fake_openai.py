"""A scripted chat-completions endpoint behind httpx.MockTransport, for the real openai SDK."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
import openai

from ai_newsdesk.llm.openai_client import make_sdk

# Built at run time, so there is nothing for gitleaks to flag.
FAKE_KEY = "sk-test-" + "k" * 40


def completion(
    content: str | None,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    prompt_tokens: int = 900,
    completion_tokens: int = 150,
    cached: int = 0,
    cache_write: int | None = None,
    cost: float | None = None,
    usage: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_791_000_000,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
    }
    if usage:
        details: dict[str, Any] = {"cached_tokens": cached}
        if cache_write is not None:
            details["cache_write_tokens"] = cache_write
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": details,
        }
        if cost is not None:
            body["usage"]["cost"] = cost
    return body


class FakeOpenAI:
    """Replays one scripted reply (a JSON body, an httpx.Response or an exception) per request."""

    def __init__(self, replies: Sequence[dict[str, Any] | httpx.Response | Exception]) -> None:
        self._replies = list(replies)
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self._replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, httpx.Response):
            return nxt
        return httpx.Response(200, json=nxt)

    def body(self, n: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[n].content)

    def sdk(self, provider: str) -> openai.AsyncOpenAI:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        return make_sdk(provider, FAKE_KEY, http_client=http, max_retries=0)
