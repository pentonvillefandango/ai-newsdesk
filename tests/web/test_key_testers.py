from __future__ import annotations

import sys
from typing import Any

import httpx
import httpx2  # installed with anthropic >= 1.13, which sends through it
import pytest
import respx

from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.web.keys import check_anthropic_key, check_typesafe_key, research_engine_checker

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
