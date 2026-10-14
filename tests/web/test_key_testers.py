from __future__ import annotations

import json
import sys
from typing import Any

import httpx
import httpx2  # installed with anthropic >= 1.13, which sends through it
import pytest
import respx

from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.web.keys import (
    check_anthropic_key,
    check_typesafe_key,
    default_key_testers,
    openai_key_checker,
    openrouter_key_checker,
    research_engine_checker,
)

RE_URL = "http://research-engine.invalid:8000"
COUNT_TOKENS = "https://api.anthropic.com/v1/messages/count_tokens"


def _anthropic_answering(status: int, body: dict[str, Any], seen: list[str]) -> Any:
    """The anthropic SDK uses httpx2 (respx cannot see it), so answer through a MockTransport."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url} key={request.headers.get('x-api-key')}")
        return httpx2.Response(status, json=body)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_anthropic_key_rejected() -> None:
    seen: list[str] = []
    error = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    }
    async with _anthropic_answering(401, error, seen) as http:
        message = await check_anthropic_key("sk-bad", RuntimeSettings(), http_client=http)
    assert message == "Anthropic rejected the key (401)."
    assert seen == [f"POST {COUNT_TOKENS} key=sk-bad"]


async def test_anthropic_key_accepted() -> None:
    seen: list[str] = []
    async with _anthropic_answering(200, {"input_tokens": 5}, seen) as http:
        assert await check_anthropic_key("sk-good", RuntimeSettings(), http_client=http) is None
    assert len(seen) == 1


@respx.mock
async def test_research_engine_key_checked_on_an_authenticated_endpoint() -> None:
    route = respx.get(f"{RE_URL}/v1/engines").mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as http:
        check = research_engine_checker(RE_URL, http)
        assert await check("bad", RuntimeSettings()) == "research-engine rejected the key (401)."
        assert route.calls.last.request.headers["X-API-Key"] == "bad"
        route.mock(return_value=httpx.Response(200, json={"data": {}}))
        assert await check("good", RuntimeSettings()) is None
        route.mock(side_effect=httpx.ConnectError("refused"))
        message = await check("good", RuntimeSettings())
        assert message is not None and message.startswith("Could not reach research-engine")


async def test_typesafe_without_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    message = await check_typesafe_key("ts-key", RuntimeSettings())
    assert message is not None and "isn't installed" in message


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "claude-haiku-5-5"),
        ({"writer_model": "claude-sonnet-5-5"}, "claude-sonnet-5-5"),
        # The writer is elsewhere: the next Anthropic job's model, else the default.
        (
            {
                "writer_provider": "openrouter",
                "writer_model": "google/gemini-3.8-flash",
                "judge_model": "claude-opus-5-5",
            },
            "claude-opus-5-5",
        ),
        (
            {
                "judge_provider": "openai",
                "judge_model": "gpt-5.4-nano",
                "writer_provider": "openrouter",
                "writer_model": "google/gemini-3.8-flash",
                "briefing_provider": "openai",
                "briefing_model": "gpt-5.4-nano",
                "consolidator_provider": "openrouter",
                "consolidator_model": "openai/gpt-5.4-nano",
            },
            "claude-haiku-5-5",
        ),
    ],
)
async def test_anthropic_key_test_always_names_a_claude_model(
    changes: dict[str, str], expected: str
) -> None:
    models: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        models.append(json.loads(request.content)["model"])
        return httpx2.Response(200, json={"input_tokens": 5})

    runtime = RuntimeSettings.model_validate(changes)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        assert await check_anthropic_key("sk-good", runtime, http_client=http) is None
    assert models == [expected]


# ---- OpenAI and OpenRouter: a free, authenticated GET with the candidate key ------------------

BEARER_CASES = [
    ("OpenAI", "https://api.openai.com/v1/models", openai_key_checker),
    ("OpenRouter", "https://openrouter.ai/api/v1/key", openrouter_key_checker),
]


@pytest.mark.parametrize(("label", "url", "make"), BEARER_CASES)
@respx.mock
async def test_bearer_key_testers(label: str, url: str, make: Any) -> None:
    key = "sk-test-" + "z" * 40
    route = respx.get(url).mock(return_value=httpx.Response(200, json={"data": []}))
    async with httpx.AsyncClient() as http:
        check = make(http)
        assert await check(key, RuntimeSettings()) is None
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {key}"
        assert route.calls.last.request.method == "GET"
        route.mock(return_value=httpx.Response(401, json={"error": {"message": f"bad key {key}"}}))
        assert await check(key, RuntimeSettings()) == f"{label} rejected the key (401)."
        route.mock(return_value=httpx.Response(403))
        assert await check(key, RuntimeSettings()) == (
            f"{label} accepted the key but refused access (403)."
        )
        route.mock(return_value=httpx.Response(500, text=f"oops {key}"))
        message = await check(key, RuntimeSettings())
        assert message == f"{label} answered 500." and key not in message
        route.mock(side_effect=httpx.ConnectError(f"refused {key}"))
        message = await check(key, RuntimeSettings())
        assert message == f"Could not reach {label} (ConnectError)."


def test_default_testers_cover_every_key() -> None:
    import tempfile
    from pathlib import Path

    from ai_newsdesk.runtime_settings import API_KEY_NAMES
    from tests.web.support import make_settings

    with tempfile.TemporaryDirectory() as tmp:
        testers = default_key_testers(make_settings(Path(tmp)), httpx.AsyncClient())
    assert set(testers) == set(API_KEY_NAMES)
