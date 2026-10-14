import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
import respx

from ai_newsdesk.models import SearchHit
from ai_newsdesk.research_engine import HttpResearchEngine, ResearchEngineError

BASE = "http://re.test"
KEY = "test-key-" + "0123456789abcdef"  # built at runtime: nothing for gitleaks to flag


def envelope(data: Any, errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {"request_id": "r1", "schema_version": "1.0.0", "took_ms": 5, "cache_hit": False},
        "errors": errors or [],
    }


SEARCH_DATA: dict[str, Any] = {
    "query": "site:x.com karpathy",
    "results": [
        {
            "rank": 1,
            "url": "https://x.com/karpathy/status/1",
            "canonical_url": "https://x.com/karpathy/status/1",
            "title": 'Andrej Karpathy on X: "nanochat is out"',
            "snippet": "A new repo for training a small chat model end to end.",
            "domain": "x.com",
            "engines": ["brave"],
            "score": 1.0,
            "published_at": None,
        }
    ],
    "suggestions": [],
    "infoboxes": [],
    "unresponsive_engines": [],
}
EXPECTED_HIT = SearchHit(
    url="https://x.com/karpathy/status/1",
    title='Andrej Karpathy on X: "nanochat is out"',
    snippet="A new repo for training a small chat model end to end.",
)
FETCH_FAILED = [{"code": "fetch_failed", "message": "upstream HTTP 500", "retryable": True}]


async def key_provider() -> str:
    return KEY


def make_engine() -> HttpResearchEngine:
    return HttpResearchEngine(BASE, key_provider, client=httpx.AsyncClient(), backoff=(0.0, 0.0))


async def test_search_parses_results_and_sends_key(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/search").mock(
        return_value=httpx.Response(200, json=envelope(SEARCH_DATA))
    )
    hits = await make_engine().search("site:x.com karpathy", max_results=6, time_range="week")
    assert hits == [EXPECTED_HIT]
    request = route.calls.last.request
    assert request.headers["X-API-Key"] == KEY
    assert json.loads(request.content) == {
        "query": "site:x.com karpathy",
        "max_results": 6,
        "time_range": "week",
    }


async def test_key_provider_is_awaited_for_every_request(respx_mock: respx.MockRouter) -> None:
    first, second = "first-key-" + "0123456789", "second-key-" + "0123456789"
    keys = iter([first, second])
    calls = 0

    async def rotating_key() -> str:
        nonlocal calls
        calls += 1
        return next(keys)

    route = respx_mock.post(f"{BASE}/v1/search").mock(
        return_value=httpx.Response(200, json=envelope(SEARCH_DATA))
    )
    engine = HttpResearchEngine(BASE, rotating_key, client=httpx.AsyncClient(), backoff=(0.0, 0.0))
    await engine.search("q")
    await engine.search("q")
    assert [c.request.headers["X-API-Key"] for c in route.calls] == [
        first,
        second,
    ]
    assert calls == 2


async def test_429_then_200_is_retried(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/search").mock(
        side_effect=[
            httpx.Response(429, text="slow down"),
            httpx.Response(200, json=envelope(SEARCH_DATA)),
        ]
    )
    assert await make_engine().search("q") == [EXPECTED_HIT]
    assert route.call_count == 2


async def test_non_json_502_is_retried(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/search").mock(
        side_effect=[
            httpx.Response(502, text="<html>Bad Gateway</html>"),
            httpx.Response(200, json=envelope(SEARCH_DATA)),
        ]
    )
    assert await make_engine().search("q") == [EXPECTED_HIT]
    assert route.call_count == 2


async def test_transport_error_is_retried(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/search").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json=envelope(SEARCH_DATA))]
    )
    assert await make_engine().search("q") == [EXPECTED_HIT]
    assert route.call_count == 2


async def test_gives_up_after_three_attempts(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(ResearchEngineError, match="gave up after 3 attempts"):
        await make_engine().search("q")
    assert route.call_count == 3


async def test_backoff_sleeps_five_then_ten_seconds(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("ai_newsdesk.research_engine.asyncio.sleep", fake_sleep)
    respx_mock.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(429, text="slow down"))
    engine = HttpResearchEngine(BASE, key_provider, client=httpx.AsyncClient())
    with pytest.raises(ResearchEngineError):
        await engine.search("q")
    assert slept == [5.0, 10.0]


async def test_search_error_envelope_raises_without_retry(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/search").mock(
        return_value=httpx.Response(
            401,
            json=envelope(
                None, [{"code": "unauthorized", "message": "bad key", "retryable": False}]
            ),
        )
    )
    with pytest.raises(ResearchEngineError, match="unauthorized"):
        await make_engine().search("q")
    assert route.call_count == 1


async def test_fetch_returns_markdown(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/fetch").mock(
        return_value=httpx.Response(
            200, json=envelope({"url": "https://example.com/a", "markdown": "# Title\n\nBody text"})
        )
    )
    assert await make_engine().fetch_markdown("https://example.com/a") == "# Title\n\nBody text"
    assert json.loads(route.calls.last.request.content) == {
        "url": "https://example.com/a",
        "formats": ["markdown"],
    }


async def test_json_502_envelope_is_not_retried(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/fetch").mock(
        return_value=httpx.Response(502, json=envelope(None, FETCH_FAILED))
    )
    assert await make_engine().fetch_markdown("https://example.com/a") is None
    assert route.call_count == 1


async def test_fetch_robots_disallowed_returns_none(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/fetch").mock(
        return_value=httpx.Response(
            403,
            json=envelope(
                None, [{"code": "robots_disallowed", "message": "no", "retryable": False}]
            ),
        )
    )
    assert await make_engine().fetch_markdown("https://example.com/a") is None
    assert route.call_count == 1


async def test_fetch_returns_none_after_retries_exhausted(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/fetch").mock(side_effect=httpx.ReadTimeout("slow"))
    assert await make_engine().fetch_markdown("https://example.com/a") is None
    assert route.call_count == 3


async def test_fetch_never_requests_x_urls(respx_mock: respx.MockRouter) -> None:
    engine = make_engine()
    assert await engine.fetch_markdown("https://x.com/karpathy/status/1") is None
    assert await engine.fetch_markdown("https://twitter.com/karpathy/status/1") is None
    assert respx_mock.calls.call_count == 0


async def test_key_never_logged_or_in_errors(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx_mock.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(429, text="slow down"))
    engine = make_engine()
    with pytest.raises(ResearchEngineError) as excinfo:
        await engine.search("q")
    assert caplog.records  # the retries were logged
    assert KEY not in caplog.text
    assert KEY not in str(excinfo.value)
    assert KEY not in repr(engine)


class SlowTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return httpx.Response(200, json=envelope({"url": str(request.url), "markdown": "text"}))


async def test_at_most_three_requests_in_flight() -> None:
    transport = SlowTransport()
    engine = HttpResearchEngine(BASE, key_provider, client=httpx.AsyncClient(transport=transport))
    results = await asyncio.gather(
        *(engine.fetch_markdown(f"https://example.com/{i}") for i in range(10))
    )
    assert results == ["text"] * 10
    assert transport.peak == 3


async def test_health(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{BASE}/health")
    route.mock(
        return_value=httpx.Response(200, json=envelope({"status": "up", "version": "1.0.0"}))
    )
    assert await make_engine().health() is True
    route.mock(
        return_value=httpx.Response(503, json=envelope({"status": "down", "version": "1.0.0"}))
    )
    assert await make_engine().health() is False
    route.mock(side_effect=httpx.ConnectError("refused"))
    assert await make_engine().health() is False
