# Step 2: Sources (tasks 9–14)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans. Steps use checkbox (`- [ ]`) syntax. Read the master plan `docs/plans/2026-10-14-ai-newsdesk-v1.md` (Global Constraints and Shared contract) before starting.

**Goal:** Everything that turns the outside world into `Candidate`s: the research-engine HTTP client (ADR-0001 politeness), URL and text normalisation, all six source adapters (`feed`, `x_person`, `search`, `hn`, `arxiv`, `github`), the adapter registry, the seed `config/sources.yaml`, and the body-excerpt enrichment helper.

**Branch:** `step-2-sources` (from `main`), merged through one PR.

**Depends on:** steps 0–1 (`models.py`, `protocols.py`, `clock.py` with `FixedClock(at)`, `tests/fakes.py` with `FakeResearchEngine`, pytest/respx/ruff/pyright tooling; `Store` from step 1, which provides `items_for_enrichment` and `seed_sources_if_empty`, master plan R1). Tests use `from ai_newsdesk.clock import FixedClock`; there is no `FakeClock`.

**Architecture notes:**
- Every adapter subclasses `BaseSource`. It implements `fetch_candidates()` (which may raise); `BaseSource.collect()` times it and turns any exception into `SourceResult.error`, so `collect()` never raises. The pipeline (step 3) adds its own 60 s timeout per source; `safe_collect(..., timeout_s=...)` is the same guard for any other caller.
- Adapters that filter by date use `cutoff = since - lookback_hours` (per-kind default, overridable per source). The look-back catches late-indexed items; the store's canonical-URL dedupe drops repeats, so re-collecting is harmless.
- research-engine is reached only through `HttpResearchEngine`. Exact field names it parses, from `research_engine_client` (`models/common.py`, `search.py`, `document.py`, `health.py`): `Envelope{data, meta, errors[{code, message, retryable, source}]}`; search `data` = `SearchResponse{query, results[{rank, url, canonical_url, title, snippet, domain, engines, score, published_at}], suggestions, infoboxes, unresponsive_engines}`; fetch `data` = `Document{url, final_url, status, title, ..., markdown, word_count, ...}`; `/health` `data` = `HealthReport{status: "up"|"degraded"|"down", version, git_sha, dependencies}`. Error codes used: `robots_disallowed`, `fetch_failed`, `unauthorized`. Requests: `POST /v1/search {query, max_results, time_range}`, `POST /v1/fetch {url, formats: ["markdown"]}`, `GET /health` (no key). Header `X-API-Key`.
- The `ResearchEngine` protocol has no `intent` parameter, so V1 searches use research-engine's default intent (`general`). `site:` queries were validated with it in the 2026-10-14 probe.
- Test code relies on `asyncio_mode = "auto"` (see Contract additions), so async tests carry no marker.
- Run `uv run ruff format` before every commit; the code below is already formatted at step 0's line length (100).

## Contract additions

New cross-step names introduced by this step. Later steps use them exactly; the items marked **(step 0)** / **(step 1)** are provided by those steps.

**`src/ai_newsdesk/research_engine.py`**
- `class ResearchEngineError(Exception)`: raised by `HttpResearchEngine.search()` when research-engine cannot answer (after retries, or on an error envelope). Source adapters let it propagate into `SourceResult.error`. The message never contains the API key.
- `class HttpResearchEngine(base_url: str, api_key_provider: Callable[[], Awaitable[str]], client: httpx.AsyncClient | None = None, max_concurrency: int = 3, backoff: Sequence[float] = (5.0, 10.0))`: implements `ResearchEngine`, plus `async aclose()` (closes the client only if it created it). The provider is async and is awaited once per request (master plan R3): `app.py` builds it as `HttpResearchEngine(settings.research_engine_url, lambda: api_keys.resolve_api_key("research_engine"), client=http)` with step 1's `ApiKeys`, so a key changed in the UI applies to the next request.
- `NO_FETCH_HOSTS`, `is_no_fetch_url(url) -> bool`.

**`src/ai_newsdesk/normalise.py`**
- `canonical_url(url: str) -> str`: the single canonicaliser. `app.py` passes it to step 1's `Store(db_path, clock=clock, canonicalise=normalise.canonical_url)` (master plan R8), so `Store.add_candidates` computes `items.canonical_url` with it.
- `x_handle(url: str) -> str | None`, `is_x_url(url: str) -> bool`, `clean_text(text: str | None, limit: int | None = None) -> str`, `excerpt_from_markdown(markdown: str | None, limit: int = 1500) -> str`.
- Constants `TITLE_MAX = 300`, `SNIPPET_MAX = 1500`, `EXCERPT_MAX = 1500` (characters).

**`src/ai_newsdesk/sources/base.py`**
- `class BaseSource(ABC)`: `kind: SourceKind`; `async collect(src, since) -> SourceResult` (never raises); abstract `async fetch_candidates(src, since) -> list[Candidate]`.
- `async safe_collect(source: Source, src: SourceConfig, since: datetime, *, timeout_s: float = 60.0) -> SourceResult`: what the pipeline (step 3) calls for every enabled source.
- `class SourceConfigError(ValueError)`; `cfg_str(src, key, default=None)`, `cfg_int(src, key, default)`, `cfg_list(src, key, default)`; `cutoff(src, since, default_lookback_hours)`, `keep_after(published, cut)`; `time_range_for(since, now) -> "day" | "week" | "month" | "year"`; `USER_AGENT`.

**Adapters and their `SourceConfig.config` keys** (all accept `max`; date-filtered ones accept `lookback_hours`)

| Class (module) | `kind` | Constructor | Config keys (default) |
|---|---|---|---|
| `FeedSource` (`sources/feed.py`) | `feed` | `(http: httpx.AsyncClient)` | `url` (required), `max` (10), `lookback_hours` (24) |
| `XPersonSource` (`sources/x_person.py`) | `x_person` | `(research_engine: ResearchEngine, clock: Clock \| None = None)` | `handle` (required, `@` optional), `max` (10) |
| `SearchSource` (`sources/search.py`) | `search` | `(research_engine: ResearchEngine, clock: Clock \| None = None)` | `query` and/or `site` (one required), `max` (10) |
| `HNSource` (`sources/hn.py`) | `hn` | `(http: httpx.AsyncClient)` | `queries` (["AI"]), `min_points` (100), `max` per query (30), `lookback_hours` (24) |
| `ArxivSource` (`sources/arxiv.py`) | `arxiv` | `(http: httpx.AsyncClient)` | `categories` (["cs.CL","cs.AI","cs.LG"]), `max` (50), `lookback_hours` (48) |
| `GitHubReleasesSource` (`sources/github.py`) | `github` | `(http: httpx.AsyncClient)` | `repo` ("owner/name", required), `max` (5), `lookback_hours` (24) |

- `sources/registry.py`: `build_sources(http: httpx.AsyncClient, research_engine: ResearchEngine, clock: Clock | None = None) -> dict[SourceKind, Source]`. `app.py` passes one shared `httpx.AsyncClient` (timeout about 30 s) and the `SystemClock`.
- **(step 3)** Before collecting an `hn` source whose config has no `min_points`, the pipeline passes `src.model_copy(update={"config": {**src.config, "min_points": runtime_settings.hn_min_points}})`.
- **(step 3)** `since` passed to `collect` is `now − max(6 h, 48 h / runs_per_day)` (step 3's `pipeline.collection_window`). Adapters add their own look-back on top; dedupe absorbs the overlap.

**`config/sources.yaml` shape** (read by **(step 1)** `Store.seed_sources_if_empty(path)`):

```yaml
sources:
  - {kind: feed, name: OpenAI, trust: primary, config: {url: "https://openai.com/news/rss.xml", max: 10}}
```

A top-level `sources:` list; each entry has `kind`, `name` (unique), `trust`, `config`, and optional `enabled` (default true) and `weight` (default 1.0). That is, `SourceConfig` fields without `id`. Every `arxiv` entry must carry `config.keywords` (a non-empty list of lab and author names): step 3's pre-filter drops any arXiv paper whose title or authors match none of them, so an arXiv source without keywords yields nothing.

**`src/ai_newsdesk/enrich.py`**
- `async enrich_bodies(store: EnrichStore, research_engine: ResearchEngine, item_ids: list[int], *, thin_chars: int = 300) -> int`: step 3 calls it on pre-filter survivors before review.
- `class EnrichStore(Protocol)`: `items_for_enrichment` and `set_item_fields` below.

**Store method this step relies on (provided by step 1, R1):** `async items_for_enrichment(item_ids: list[int]) -> list[tuple[int, str, SourceKind, str]]`, which returns `(item_id, url, kind of the item's source, snippet)` for the given ids that exist. `body_excerpt` is written with step 1's `set_item_fields(item_id, body_excerpt=...)`.

**(step 0) Test tooling:** `[tool.pytest] asyncio_mode = "auto"`; `tests/` is a package (tests import `tests.fakes`); `respx` in the dev group; `feedparser`, `httpx` and `PyYAML` as runtime dependencies. `FakeResearchEngine.search(query, *, max_results=10, time_range="day")` returns `search_results.get(query, [])[:max_results]`, and `fetch_markdown(url)` returns `pages.get(url)`. `ai_newsdesk.clock.FixedClock(at)` is the test clock.

---

### Task 9: research-engine client

**Goal:** `HttpResearchEngine` implements the `ResearchEngine` protocol against research-engine's REST API, with ADR-0001 politeness (≤3 in flight; retry 429, non-JSON 5xx and transport errors with 5 s then 10 s backoff, 3 attempts in all), and never leaks the API key.

**Files:**
- Create: `src/ai_newsdesk/research_engine.py`
- Create: `tests/test_research_engine.py`

**Acceptance Criteria:**
- [ ] `search()` POSTs `{query, max_results, time_range}` to `/v1/search` with `X-API-Key` and returns `SearchHit`s built from `data.results[].url/title/snippet`.
- [ ] A 429, a 5xx without a JSON envelope, or a transport error is retried; at most 3 attempts; sleeps are exactly 5.0 s then 10.0 s by default.
- [ ] A 5xx **with** a JSON envelope (e.g. `fetch_failed`) is not retried; nor is any 4xx.
- [ ] `search()` raises `ResearchEngineError` on final failure; `fetch_markdown()` returns `None` on `robots_disallowed`, `fetch_failed`, exhausted retries or any other error.
- [ ] `fetch_markdown()` never sends a request for an x.com/twitter.com URL.
- [ ] At most `max_concurrency` (default 3) requests are in flight at once.
- [ ] `api_key_provider` is an async callable, awaited once per request (retries reuse that key), so a key changed in the UI is used by the next request.
- [ ] The key never appears in logs, error messages or `repr()`.
- [ ] `health()` is True only for HTTP 200 with `data.status` `up` or `degraded`; False on any error.

**Verify:** `uv run pytest tests/test_research_engine.py -q` → `16 passed`

**Steps:**

- [ ] **Step 1: Create the branch**

```bash
git switch main && git pull --ff-only && git switch -c step-2-sources
```

- [ ] **Step 2: Write the failing test** — `tests/test_research_engine.py`:

```python
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
KEY = "test-key-0123456789abcdef"


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
    keys = iter(["first-key-0123456789", "second-key-0123456789"])
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
        "first-key-0123456789",
        "second-key-0123456789",
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
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_research_engine.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_newsdesk.research_engine'` (collection error).

- [ ] **Step 4: Write the implementation** — `src/ai_newsdesk/research_engine.py`:

```python
"""HttpResearchEngine: the only client of research-engine's REST API.

Politeness (ADR-0001): at most `max_concurrency` (3) requests in flight. A request is retried after
a 429, a 5xx without a JSON envelope, or a transport error, sleeping `backoff` (5 s, then 10 s)
between attempts, so at most 3 attempts. A 5xx WITH a JSON envelope is research-engine's definitive
answer (for example `fetch_failed`) and is not retried. X pages are never fetched (robots.txt
disallows them). The API key is awaited from `api_key_provider` once per request (so a key changed
in the UI applies to the next request) and is never logged or put in an error message.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_newsdesk.models import SearchHit

log = logging.getLogger(__name__)

NO_FETCH_HOSTS = frozenset(
    {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
)


class ResearchEngineError(Exception):
    """research-engine could not answer. The message never contains the API key."""


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ErrorDetail(_Lenient):
    code: str
    message: str = ""
    retryable: bool = False


class _Envelope(_Lenient):
    data: Any = None
    errors: list[_ErrorDetail] = Field(default_factory=list[_ErrorDetail])


class _SearchResult(_Lenient):
    url: str
    title: str = ""
    snippet: str = ""


class _SearchResponse(_Lenient):
    results: list[_SearchResult] = Field(default_factory=list[_SearchResult])


class _Document(_Lenient):
    markdown: str = ""


class _Health(_Lenient):
    status: str = "down"


def _parse_envelope(resp: httpx.Response) -> _Envelope | None:
    """The research-engine envelope, or None when the body is not one (HTML error page, proxy
    reply)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or ("data" not in body and "errors" not in body):
        return None
    try:
        return _Envelope.model_validate(body)
    except ValidationError:
        return None


def is_no_fetch_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in NO_FETCH_HOSTS


class HttpResearchEngine:
    def __init__(
        self,
        base_url: str,
        api_key_provider: Callable[[], Awaitable[str]],
        client: httpx.AsyncClient | None = None,
        max_concurrency: int = 3,
        backoff: Sequence[float] = (5.0, 10.0),
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key_provider
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))
        self._sem = asyncio.Semaphore(max_concurrency)
        self._backoff = tuple(backoff)

    def __repr__(self) -> str:
        return f"HttpResearchEngine(base_url={self._base_url!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> _Envelope:
        attempts = len(self._backoff) + 1
        last = "no attempt"
        headers = {"X-API-Key": await self._api_key()}
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self._backoff[attempt - 1])
            try:
                async with self._sem:
                    resp = await self._client.post(
                        f"{self._base_url}{path}", json=body, headers=headers
                    )
            except httpx.TransportError as exc:
                last = f"transport error {type(exc).__name__}"
                log.warning(
                    "research-engine %s attempt %d/%d failed: %s", path, attempt + 1, attempts, last
                )
                continue
            env = _parse_envelope(resp)
            if resp.status_code == 429 or (resp.status_code >= 500 and env is None):
                last = f"HTTP {resp.status_code}"
                log.warning(
                    "research-engine %s attempt %d/%d failed: %s", path, attempt + 1, attempts, last
                )
                continue
            if env is None:
                raise ResearchEngineError(
                    f"{path}: HTTP {resp.status_code} without a JSON envelope"
                )
            if resp.status_code >= 400 or env.data is None:
                codes = ",".join(e.code for e in env.errors) or "no data"
                raise ResearchEngineError(f"{path}: HTTP {resp.status_code} ({codes})")
            return env
        raise ResearchEngineError(f"{path}: gave up after {attempts} attempts ({last})")

    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]:
        """Raises ResearchEngineError when research-engine cannot answer; source adapters record
        it."""
        env = await self._post(
            "/v1/search", {"query": query, "max_results": max_results, "time_range": time_range}
        )
        try:
            data = _SearchResponse.model_validate(env.data)
        except ValidationError as exc:
            raise ResearchEngineError("/v1/search: unexpected data shape") from exc
        return [SearchHit(url=r.url, title=r.title, snippet=r.snippet) for r in data.results]

    async def fetch_markdown(self, url: str) -> str | None:
        """The page as Markdown, or None on robots_disallowed, fetch_failed or any other failure."""
        if is_no_fetch_url(url):
            return None
        try:
            env = await self._post("/v1/fetch", {"url": url, "formats": ["markdown"]})
            doc = _Document.model_validate(env.data)
        except Exception:
            log.info(
                "research-engine fetch of %s failed; the snippet is used instead",
                url,
                exc_info=True,
            )
            return None
        return doc.markdown or None

    async def health(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base_url}/health", timeout=10.0)
        except httpx.HTTPError:
            return False
        env = _parse_envelope(resp)
        if resp.status_code != 200 or env is None:
            return False
        try:
            return _Health.model_validate(env.data).status in ("up", "degraded")
        except ValidationError:
            return False
```

- [ ] **Step 5: Run it and watch it pass**

Run: `uv run ruff format && uv run ruff check && uv run pyright src/ai_newsdesk/research_engine.py tests/test_research_engine.py && uv run pytest tests/test_research_engine.py -q`
Expected: no lint or type errors; `16 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/ai_newsdesk/research_engine.py tests/test_research_engine.py
git commit -m "feat(research-engine): HTTP client with ADR-0001 retries and concurrency cap (V1-S2)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Normalisation helpers

**Goal:** One canonical spelling per URL (for dedupe), X handle extraction, and the text cleaning and Markdown excerpting proven in the bake-off.

**Files:**
- Create: `src/ai_newsdesk/normalise.py`
- Create: `tests/test_normalise.py`

**Acceptance Criteria:**
- [ ] `canonical_url` lowercases scheme and host, strips `www.`, removes `utm_*` (any case), `fbclid`, `gclid` and `ref` params, drops the fragment, drops default ports, strips the trailing slash, and maps twitter.com/mobile/www variants to `https://x.com` with a lower-cased path and no query.
- [ ] `canonical_url` is idempotent and returns non-http(s) input stripped but otherwise unchanged.
- [ ] `x_handle` returns the handle of an X/Twitter status URL, else None; `is_x_url` recognises every X/Twitter host.
- [ ] `clean_text` strips tags, unescapes entities, collapses whitespace and caps length.
- [ ] `excerpt_from_markdown` drops images and lines ≤ 60 chars, turns links into their text and caps at 1,500 chars.

**Verify:** `uv run pytest tests/test_normalise.py -q` → `36 passed`

**Steps:**

- [ ] **Step 1: Write the failing test** — `tests/test_normalise.py`:

```python
import pytest

from ai_newsdesk.normalise import (
    canonical_url,
    clean_text,
    excerpt_from_markdown,
    is_x_url,
    x_handle,
)

CANONICAL_CASES = [
    (
        "HTTPS://WWW.Example.COM/Path/?utm_source=x&utm_medium=y&id=5#frag",
        "https://example.com/Path?id=5",
    ),
    ("https://example.com/a/?fbclid=1&gclid=2&ref=hn", "https://example.com/a"),
    ("https://example.com/a?UTM_Campaign=z&page=2", "https://example.com/a?page=2"),
    ("https://example.com/a?ref_id=7", "https://example.com/a?ref_id=7"),
    ("https://twitter.com/Karpathy/status/123?s=20", "https://x.com/karpathy/status/123"),
    ("https://mobile.x.com/simonw/status/9/", "https://x.com/simonw/status/9"),
    ("https://www.x.com/simonw/status/9#m", "https://x.com/simonw/status/9"),
    ("http://example.com:80/x", "http://example.com/x"),
    ("https://example.com:8443/x", "https://example.com:8443/x"),
    ("https://example.com/", "https://example.com"),
    ("  https://example.com/a  ", "https://example.com/a"),
    ("not a url", "not a url"),
    ("mailto:someone@example.com", "mailto:someone@example.com"),
]


@pytest.mark.parametrize(("url", "expected"), CANONICAL_CASES)
def test_canonical_url(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


@pytest.mark.parametrize(("url", "expected"), CANONICAL_CASES)
def test_canonical_url_is_idempotent(url: str, expected: str) -> None:
    assert canonical_url(canonical_url(url)) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.com/karpathy/status/123", "karpathy"),
        ("https://twitter.com/SimonW/status/9?s=20", "SimonW"),
        ("https://mobile.x.com/emollick/status/5", "emollick"),
        ("https://x.com/karpathy", None),
        ("https://example.com/karpathy/status/1", None),
    ],
)
def test_x_handle(url: str, expected: str | None) -> None:
    assert x_handle(url) == expected


def test_is_x_url() -> None:
    assert is_x_url("https://x.com/a/status/1")
    assert is_x_url("https://www.twitter.com/a")
    assert not is_x_url("https://notx.com/a")
    assert not is_x_url("https://example.com/x.com")


def test_clean_text_strips_tags_entities_and_whitespace() -> None:
    raw = "<p>Hello&nbsp;<b>world</b></p>\n\n  &amp; more"
    assert clean_text(raw) == "Hello world & more"


def test_clean_text_limit_and_empty() -> None:
    assert clean_text("abc def ghi", 7) == "abc def"
    assert clean_text("abc def ghi", 8) == "abc def"
    assert clean_text(None) == ""
    assert clean_text("") == ""


ANNOUNCEMENT = (
    "Anthropic today released a new model that is faster and cheaper than before, says the"
)
PRICING = (
    "The model is available in the API from today, with pricing at one dollar per million"
    " input tokens."
)
ARTICLE_MARKDOWN = f"""[Home](/) | [Blog](/blog)
![hero](https://img.example/x.png)
# Short title
{ANNOUNCEMENT} [announcement](https://a.example/c).
Subscribe
{PRICING}
"""


def test_excerpt_from_markdown_keeps_prose_only() -> None:
    assert excerpt_from_markdown(ARTICLE_MARKDOWN) == f"{ANNOUNCEMENT} announcement. {PRICING}"


def test_excerpt_from_markdown_is_capped() -> None:
    excerpt = excerpt_from_markdown(("x" * 70 + "\n") * 50)
    assert 1400 < len(excerpt) <= 1500
    assert excerpt_from_markdown(None) == ""
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_normalise.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_newsdesk.normalise'`.

- [ ] **Step 3: Write the implementation** — `src/ai_newsdesk/normalise.py`:

```python
"""URL canonicalisation and text cleaning shared by sources, dedupe and enrichment."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TITLE_MAX = 300
SNIPPET_MAX = 1500
EXCERPT_MAX = 1500

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "ref"})
_X_HOSTS = frozenset({"x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"})
_X_STATUS = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([^/?#]+)/status/", re.IGNORECASE
)
_DEFAULT_PORTS = {("http", 80), ("https", 443)}


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMS


def canonical_url(url: str) -> str:
    """One spelling per page, used for dedupe. Non-http(s) or unparseable input comes back
    stripped."""
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return raw
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return raw
    host = host.removeprefix("www.")
    path = parts.path.rstrip("/")
    if host in _X_HOSTS:
        # X: one host, handles are case-insensitive, and the query (?s=20, ?t=...) is share
        # tracking.
        return urlunsplit(("https", "x.com", path.lower(), "", ""))
    netloc = host if port is None or (scheme, port) in _DEFAULT_PORTS else f"{host}:{port}"
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
    ]
    return urlunsplit((scheme, netloc, path, urlencode(kept), ""))


def x_handle(url: str) -> str | None:
    """The author handle of an X/Twitter status URL, as written in the URL; None for anything
    else."""
    match = _X_STATUS.match(url.strip())
    return match.group(1) if match else None


def is_x_url(url: str) -> bool:
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host.removeprefix("www.") in _X_HOSTS


def clean_text(text: str | None, limit: int | None = None) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace, then cap at `limit` characters."""
    if not text:
        return ""
    out = _TAG.sub(" ", text)
    out = html.unescape(out)
    out = _WS.sub(" ", out).strip()
    if limit is not None and len(out) > limit:
        out = out[:limit].rstrip()
    return out


def excerpt_from_markdown(markdown: str | None, limit: int = EXCERPT_MAX) -> str:
    """Article prose from research-engine Markdown: no images, links as text, short (menu) lines
    dropped."""
    text = _MD_IMAGE.sub(" ", markdown or "")
    text = _MD_LINK.sub(r"\1", text)
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 60]
    return clean_text(" ".join(lines), limit)
```

- [ ] **Step 4: Run it and watch it pass**

Run: `uv run ruff format && uv run ruff check && uv run pyright src/ai_newsdesk/normalise.py tests/test_normalise.py && uv run pytest tests/test_normalise.py -q`
Expected: no lint or type errors; `36 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ai_newsdesk/normalise.py tests/test_normalise.py
git commit -m "feat(normalise): canonical URLs, X handles, text cleaning and excerpts (V1-S2)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Source base and RSS/Atom feeds

**Goal:** The shared adapter base (typed config access, look-back cut-off, `time_range_for`, timing, never-raise `collect` and `safe_collect`) and `FeedSource` with ETag/Last-Modified, `max` per source and a published-date filter.

**Files:**
- Create (if step 0 did not): `src/ai_newsdesk/sources/__init__.py` containing only `"""Source adapters: one per SourceKind."""`
- Create: `src/ai_newsdesk/sources/base.py`
- Create: `src/ai_newsdesk/sources/feed.py`
- Create: `tests/fixtures/feeds/rss_basic.xml`
- Create: `tests/fixtures/feeds/atom_basic.xml`
- Create: `tests/test_sources_base.py`
- Create: `tests/test_sources_feed.py`

**Acceptance Criteria:**
- [ ] `BaseSource.collect` returns `SourceResult(error="<ExcType>: <message>")` (≤300 chars) instead of raising, and always sets `duration_s`.
- [ ] `safe_collect` turns a timeout into `error="timed out after <n>s"` and any exception into an error result.
- [ ] `time_range_for` maps ≤1 day → `day`, ≤7 days → `week`, ≤31 days → `month`, else `year`.
- [ ] `FeedSource` parses RSS and Atom, keeps title/link/author/published/snippet, skips entries without link or title, keeps undated entries, drops entries older than `since − lookback_hours` (default 24) and stops at `max` (default 10).
- [ ] After a 200 with `ETag`/`Last-Modified`, the next request for the same source id sends `If-None-Match`/`If-Modified-Since`; a 304 gives an empty, error-free result. Validators are per source id and in memory only (V1: lost on restart, which costs one full fetch per feed).
- [ ] HTTP errors, unparseable bodies and missing `url` config come back as `SourceResult.error`.

**Verify:** `uv run pytest tests/test_sources_base.py tests/test_sources_feed.py -q` → `22 passed`

**Steps:**

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/feeds/rss_basic.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Example Lab News</title>
    <link>https://lab.example/news</link>
    <description>News from an example AI lab</description>
    <item>
      <title>Introducing Model &amp; Tools 2</title>
      <link>https://lab.example/news/model-2</link>
      <dc:creator>Jane Doe</dc:creator>
      <pubDate>Wed, 14 Oct 2026 05:30:00 +0000</pubDate>
      <description><![CDATA[<p>Claude gets <b>faster</b> &amp; cheaper.</p>]]></description>
    </item>
    <item>
      <title>Research update: sparse attention</title>
      <link>https://lab.example/news/sparse-attention</link>
      <pubDate>Tue, 13 Oct 2026 07:00:00 GMT</pubDate>
      <description>A short research note.</description>
    </item>
    <item>
      <title>Old post from Monday</title>
      <link>https://lab.example/news/old</link>
      <pubDate>Mon, 12 Oct 2026 09:00:00 GMT</pubDate>
      <description>Too old for this run.</description>
    </item>
    <item>
      <title>Undated post</title>
      <link>https://lab.example/news/undated</link>
      <description>No date on this one.</description>
    </item>
    <item>
      <title>Post without a link</title>
      <pubDate>Wed, 14 Oct 2026 04:00:00 GMT</pubDate>
      <description>Skipped: nothing to link to.</description>
    </item>
  </channel>
</rss>
```

`tests/fixtures/feeds/atom_basic.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Blog</title>
  <link href="https://blog.example/" rel="alternate"/>
  <id>https://blog.example/</id>
  <updated>2026-10-14T04:00:00Z</updated>
  <entry>
    <title>Notes on a new open-weights model</title>
    <link href="https://blog.example/2026/oct/14/open-weights/" rel="alternate"/>
    <id>https://blog.example/2026/oct/14/open-weights/</id>
    <updated>2026-10-14T04:00:00Z</updated>
    <author><name>Sam Writer</name></author>
    <content type="html">&lt;p&gt;I tried the new model on my laptop.&lt;/p&gt;&lt;p&gt;It runs at 40 tokens/second.&lt;/p&gt;</content>
  </entry>
</feed>
```

- [ ] **Step 2: Write the failing tests**

`tests/test_sources_base.py`:

```python
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind, SourceResult, Trust
from ai_newsdesk.sources.base import (
    BaseSource,
    SourceConfigError,
    cfg_int,
    cfg_list,
    cfg_str,
    cutoff,
    keep_after,
    safe_collect,
    time_range_for,
)

SINCE = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)


def src(**config: Any) -> SourceConfig:
    return SourceConfig(
        id=7, kind=SourceKind.FEED, name="Example", config=config, trust=Trust.PRIMARY
    )


class Exploding(BaseSource):
    kind = SourceKind.FEED

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        raise RuntimeError("boom")


class OneItem(BaseSource):
    kind = SourceKind.FEED

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        return [Candidate(source_id=src.id, url="https://example.com/a", title="A")]


class RawSlow:
    kind = SourceKind.FEED

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult:
        await asyncio.sleep(1)
        return SourceResult(source_id=src.id)


class RawBroken:
    kind = SourceKind.FEED

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult:
        raise ValueError("bad adapter")


async def test_collect_catches_errors() -> None:
    result = await Exploding().collect(src(), SINCE)
    assert result.source_id == 7
    assert result.candidates == []
    assert result.error == "RuntimeError: boom"
    assert result.duration_s >= 0


async def test_collect_returns_candidates() -> None:
    result = await OneItem().collect(src(), SINCE)
    assert result.error is None
    assert [c.url for c in result.candidates] == ["https://example.com/a"]


async def test_safe_collect_times_out() -> None:
    result = await safe_collect(RawSlow(), src(), SINCE, timeout_s=0.01)
    assert result.error == "timed out after 0.01s"
    assert result.candidates == []


async def test_safe_collect_catches_raising_sources() -> None:
    result = await safe_collect(RawBroken(), src(), SINCE)
    assert result.error == "ValueError: bad adapter"


async def test_safe_collect_passes_results_through() -> None:
    result = await safe_collect(OneItem(), src(), SINCE)
    assert result.error is None
    assert len(result.candidates) == 1


def test_config_helpers() -> None:
    s = src(url="https://example.com/feed", max=5, flag=True, queries="AI")
    assert cfg_str(s, "url") == "https://example.com/feed"
    assert cfg_str(s, "query", "") == ""
    assert cfg_int(s, "max", 10) == 5
    assert cfg_int(s, "missing", 10) == 10
    assert cfg_list(s, "queries", []) == ["AI"]
    assert cfg_list(s, "missing", ["LLM"]) == ["LLM"]
    with pytest.raises(SourceConfigError):
        cfg_str(s, "handle")
    with pytest.raises(SourceConfigError):
        cfg_int(s, "flag", 1)
    with pytest.raises(SourceConfigError):
        cfg_int(s, "url", 1)


def test_cutoff_and_keep_after() -> None:
    assert cutoff(src(), SINCE, 24) == SINCE - timedelta(hours=24)
    assert cutoff(src(lookback_hours=2), SINCE, 24) == SINCE - timedelta(hours=2)
    assert keep_after(None, SINCE)
    assert keep_after(SINCE, SINCE)
    assert not keep_after(SINCE - timedelta(seconds=1), SINCE)


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (timedelta(hours=6), "day"),
        (timedelta(hours=24), "day"),
        (timedelta(hours=25), "week"),
        (timedelta(days=7), "week"),
        (timedelta(days=8), "month"),
        (timedelta(days=40), "year"),
    ],
)
def test_time_range_for(gap: timedelta, expected: str) -> None:
    assert time_range_for(SINCE - gap, SINCE) == expected
```

`tests/test_sources_feed.py`:

```python
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import respx

from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.sources.feed import FeedSource

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
RSS = (FIXTURES / "rss_basic.xml").read_bytes()
ATOM = (FIXTURES / "atom_basic.xml").read_bytes()
RSS_URL = "https://lab.example/rss.xml"
ATOM_URL = "https://blog.example/atom.xml"
SINCE = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
VALIDATORS = {"ETag": '"v1"', "Last-Modified": "Wed, 14 Oct 2026 05:00:00 GMT"}


def feed_src(url: str = RSS_URL, source_id: int = 1, **extra: Any) -> SourceConfig:
    return SourceConfig(
        id=source_id,
        kind=SourceKind.FEED,
        name="Example Lab",
        config={"url": url, **extra},
        trust=Trust.PRIMARY,
    )


async def test_rss_entries_become_candidates(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, content=RSS))
    result = await FeedSource(httpx.AsyncClient()).collect(feed_src(), SINCE)
    assert result.error is None
    assert [c.title for c in result.candidates] == [
        "Introducing Model & Tools 2",
        "Research update: sparse attention",
        "Undated post",
    ]
    first = result.candidates[0]
    assert first.source_id == 1
    assert first.url == "https://lab.example/news/model-2"
    assert first.author == "Jane Doe"
    assert first.published_at == datetime(2026, 10, 14, 5, 30, tzinfo=UTC)
    assert first.snippet == "Claude gets faster & cheaper."
    assert result.candidates[2].published_at is None


async def test_max_entries_per_source(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, content=RSS))
    result = await FeedSource(httpx.AsyncClient()).collect(feed_src(max=2), SINCE)
    assert len(result.candidates) == 2


async def test_lookback_hours_from_config(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, content=RSS))
    result = await FeedSource(httpx.AsyncClient()).collect(feed_src(lookback_hours=1), SINCE)
    assert [c.title for c in result.candidates] == ["Introducing Model & Tools 2", "Undated post"]


async def test_atom_content_and_author(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ATOM_URL).mock(return_value=httpx.Response(200, content=ATOM))
    result = await FeedSource(httpx.AsyncClient()).collect(feed_src(ATOM_URL), SINCE)
    assert result.error is None
    [only] = result.candidates
    assert only.title == "Notes on a new open-weights model"
    assert only.author == "Sam Writer"
    assert only.snippet == "I tried the new model on my laptop. It runs at 40 tokens/second."
    assert only.published_at == datetime(2026, 10, 14, 4, 0, tzinfo=UTC)


class Recorder:
    """respx side effect that records each request and replies from a list (the last reply
    repeats)."""

    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.replies[min(len(self.requests), len(self.replies)) - 1]


async def test_etag_and_last_modified_are_honoured(respx_mock: respx.MockRouter) -> None:
    recorder = Recorder(httpx.Response(200, content=RSS, headers=VALIDATORS), httpx.Response(304))
    respx_mock.get(RSS_URL).mock(side_effect=recorder)
    source = FeedSource(httpx.AsyncClient())
    first = await source.collect(feed_src(), SINCE)
    second = await source.collect(feed_src(), SINCE)
    assert len(first.candidates) == 3
    assert second.error is None
    assert second.candidates == []
    sent_first, sent_second = recorder.requests
    assert "If-None-Match" not in sent_first.headers
    assert sent_second.headers["If-None-Match"] == '"v1"'
    assert sent_second.headers["If-Modified-Since"] == "Wed, 14 Oct 2026 05:00:00 GMT"


async def test_validators_are_per_source(respx_mock: respx.MockRouter) -> None:
    recorder = Recorder(httpx.Response(200, content=RSS, headers=VALIDATORS))
    respx_mock.get(RSS_URL).mock(side_effect=recorder)
    source = FeedSource(httpx.AsyncClient())
    await source.collect(feed_src(source_id=1), SINCE)
    await source.collect(feed_src(source_id=2), SINCE)
    assert "If-None-Match" not in recorder.requests[1].headers


async def test_http_error_is_recorded(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(RSS_URL).mock(return_value=httpx.Response(500, text="oops"))
    result = await FeedSource(httpx.AsyncClient()).collect(feed_src(), SINCE)
    assert result.candidates == []
    assert result.error is not None and result.error.startswith("HTTPStatusError")


async def test_garbage_body_is_recorded(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, text="this is not xml <<<"))
    result = await FeedSource(httpx.AsyncClient()).collect(feed_src(), SINCE)
    assert result.error is not None and "unparseable feed" in result.error


async def test_missing_url_is_a_config_error() -> None:
    bad = SourceConfig(id=3, kind=SourceKind.FEED, name="Broken", config={}, trust=Trust.DISCOVERY)
    result = await FeedSource(httpx.AsyncClient()).collect(bad, SINCE)
    assert result.error is not None and result.error.startswith("SourceConfigError")
```

- [ ] **Step 3: Run them and watch them fail**

Run: `uv run pytest tests/test_sources_base.py tests/test_sources_feed.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_newsdesk.sources.base'` and `... 'ai_newsdesk.sources.feed'` (2 collection errors). If `ai_newsdesk.sources` itself is missing, create `src/ai_newsdesk/sources/__init__.py` first (see Files).

- [ ] **Step 4: Write the implementation**

`src/ai_newsdesk/sources/base.py`:

```python
"""Shared pieces for source adapters: typed config access, look-back cut-offs and never-raise
wrappers."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind, SourceResult
from ai_newsdesk.protocols import Source

log = logging.getLogger(__name__)

USER_AGENT = "ai-newsdesk/1 (self-hosted news reader)"
ERROR_MAX_CHARS = 300


class SourceConfigError(ValueError):
    """A source's config is missing a key or has the wrong type."""


def cfg_str(src: SourceConfig, key: str, default: str | None = None) -> str:
    """A string config value. With no default, the key is required and must be non-empty."""
    value = src.config.get(key, default)
    if isinstance(value, str) and (value or default is not None):
        return value
    raise SourceConfigError(
        f"source {src.id} ({src.name}): config '{key}' must be a non-empty string"
    )


def cfg_int(src: SourceConfig, key: str, default: int) -> int:
    value = src.config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceConfigError(f"source {src.id} ({src.name}): config '{key}' must be an integer")
    return value


def cfg_list(src: SourceConfig, key: str, default: list[str]) -> list[str]:
    value = src.config.get(key, default)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    raise SourceConfigError(
        f"source {src.id} ({src.name}): config '{key}' must be a list of strings"
    )


def cutoff(src: SourceConfig, since: datetime, default_lookback_hours: int) -> datetime:
    """Oldest publish time to keep. The look-back catches late-indexed items; dedupe drops
    repeats."""
    return since - timedelta(hours=cfg_int(src, "lookback_hours", default_lookback_hours))


def keep_after(published: datetime | None, cut: datetime) -> bool:
    """Undated entries are kept: the store's canonical-URL dedupe stops them repeating."""
    return published is None or published >= cut


def time_range_for(since: datetime, now: datetime) -> str:
    """research-engine `time_range` covering the interval since the last run."""
    gap = now - since
    if gap <= timedelta(days=1):
        return "day"
    if gap <= timedelta(days=7):
        return "week"
    if gap <= timedelta(days=31):
        return "month"
    return "year"


def error_text(exc: BaseException) -> str:
    message = str(exc).strip()
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text[:ERROR_MAX_CHARS]


class BaseSource(ABC):
    """Adapters implement `fetch_candidates`, which may raise; `collect` times it and never
    raises."""

    kind: SourceKind

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult:
        start = time.perf_counter()
        try:
            candidates = await self.fetch_candidates(src, since)
        except Exception as exc:
            log.warning("source %s (%s) failed", src.id, src.name, exc_info=True)
            return SourceResult(
                source_id=src.id, error=error_text(exc), duration_s=time.perf_counter() - start
            )
        return SourceResult(
            source_id=src.id, candidates=candidates, duration_s=time.perf_counter() - start
        )

    @abstractmethod
    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]: ...


async def safe_collect(
    source: Source, src: SourceConfig, since: datetime, *, timeout_s: float = 60.0
) -> SourceResult:
    """Run any Source with a timeout. Never raises: failures come back in `SourceResult.error`."""
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            result = await source.collect(src, since)
    except TimeoutError:
        return SourceResult(
            source_id=src.id,
            error=f"timed out after {timeout_s:g}s",
            duration_s=time.perf_counter() - start,
        )
    except Exception as exc:
        log.warning("source %s (%s) raised from collect", src.id, src.name, exc_info=True)
        return SourceResult(
            source_id=src.id, error=error_text(exc), duration_s=time.perf_counter() - start
        )
    if result.duration_s == 0.0:
        result = result.model_copy(update={"duration_s": time.perf_counter() - start})
    return result
```

`src/ai_newsdesk/sources/feed.py`:

```python
"""RSS/Atom feeds (kind `feed`). GitHubReleasesSource reuses this with a different URL and title."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any, cast

import feedparser  # pyright: ignore[reportMissingTypeStubs]
import httpx

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, clean_text
from ai_newsdesk.sources.base import USER_AGENT, BaseSource, cfg_int, cfg_str, cutoff, keep_after


def entry_datetime(entry: Any) -> datetime | None:
    """feedparser gives UTC struct_times; published wins over updated."""
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    return None


def entry_text(entry: Any) -> str:
    text: str = entry.get("summary") or ""
    if not text:
        content: list[Any] = entry.get("content") or []
        if content:
            text = content[0].get("value") or ""
    return clean_text(text, SNIPPET_MAX)


def entry_author(entry: Any) -> str | None:
    return clean_text(entry.get("author") or "", 200) or None


def parse_feed(payload: bytes) -> list[Any]:
    parsed = cast(Any, feedparser.parse(payload))  # pyright: ignore[reportUnknownMemberType]
    entries: list[Any] = list(parsed.entries)
    if not entries and parsed.get("bozo"):
        raise ValueError(f"unparseable feed ({type(parsed.get('bozo_exception')).__name__})")
    return entries


class FeedSource(BaseSource):
    """Config: `url` (required), `max` (default 10), `lookback_hours` (default 24).

    ETag/Last-Modified validators are kept in memory per source id (V1). After a restart each feed
    is fetched in full once; the store's canonical-URL dedupe drops the repeats.
    """

    kind = SourceKind.FEED
    default_max = 10
    lookback_hours = 24

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._validators: dict[int, tuple[str | None, str | None]] = {}

    def feed_url(self, src: SourceConfig) -> str:
        return cfg_str(src, "url")

    def entry_title(self, src: SourceConfig, entry: Any) -> str:
        return clean_text(entry.get("title") or "", TITLE_MAX)

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        url = self.feed_url(src)
        limit = cfg_int(src, "max", self.default_max)
        cut = cutoff(src, since, self.lookback_hours)
        headers = {"User-Agent": USER_AGENT}
        etag, last_modified = self._validators.get(src.id, (None, None))
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        resp = await self._http.get(url, headers=headers, follow_redirects=True)
        if resp.status_code == 304:
            return []
        resp.raise_for_status()
        entries = parse_feed(resp.content)
        self._validators[src.id] = (resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
        out: list[Candidate] = []
        for entry in entries:
            link: str = entry.get("link") or ""
            title = self.entry_title(src, entry)
            published = entry_datetime(entry)
            if not link or not title or not keep_after(published, cut):
                continue
            out.append(
                Candidate(
                    source_id=src.id,
                    url=link,
                    title=title,
                    author=entry_author(entry),
                    published_at=published,
                    snippet=entry_text(entry),
                )
            )
            if len(out) >= limit:
                break
        return out
```

- [ ] **Step 5: Run them and watch them pass**

Run: `uv run ruff format && uv run ruff check && uv run pyright src/ai_newsdesk/sources tests/test_sources_base.py tests/test_sources_feed.py && uv run pytest tests/test_sources_base.py tests/test_sources_feed.py -q`
Expected: no lint or type errors; `22 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/ai_newsdesk/sources/__init__.py src/ai_newsdesk/sources/base.py src/ai_newsdesk/sources/feed.py \
  tests/fixtures/feeds/rss_basic.xml tests/fixtures/feeds/atom_basic.xml \
  tests/test_sources_base.py tests/test_sources_feed.py
git commit -m "feat(sources): adapter base and RSS/Atom feed source with ETag support (V1-S2)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: X people and research-engine searches

**Goal:** `XPersonSource` (snippet-only X posts via `site:x.com <handle>`, own posts only) and `SearchSource` (topic and `site:`-scoped searches), both through the `ResearchEngine` protocol.

**Files:**
- Create: `src/ai_newsdesk/sources/x_person.py`
- Create: `src/ai_newsdesk/sources/search.py`
- Create: `tests/test_sources_x_search.py`

**Acceptance Criteria:**
- [ ] `XPersonSource` searches `site:x.com <handle>` (leading `@` stripped) with `max_results` = config `max` (default 10) and `time_range` from `time_range_for(since, clock.now())`.
- [ ] Only status URLs whose handle equals the source handle (case-insensitive) are kept; profile pages, other accounts and non-X URLs are dropped. `author` = the configured handle; titles like `Name on X: "..."` are kept as they are; an empty title falls back to the snippet.
- [ ] `SearchSource` builds `site:<site> <query>` / `<query>` / `site:<site>`; with `site`, off-site results (including look-alike paths such as `/newsletter` for `/news`) are dropped; X posts found by a topic search get their handle as `author`.
- [ ] research-engine failures and missing config come back as `SourceResult.error`; neither source ever calls `fetch_markdown`.

**Verify:** `uv run pytest tests/test_sources_x_search.py -q` → `9 passed`

**Steps:**

- [ ] **Step 1: Write the failing test** — `tests/test_sources_x_search.py`:

```python
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import SearchHit, SourceConfig, SourceKind, Trust
from ai_newsdesk.research_engine import ResearchEngineError
from ai_newsdesk.sources.search import SearchSource, matches_site
from ai_newsdesk.sources.x_person import XPersonSource
from tests.fakes import FakeResearchEngine

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class RecordingEngine:
    """Records search calls; returns `hits` for every query, or raises when `fail` is set."""

    def __init__(self, hits: list[SearchHit] | None = None, fail: bool = False) -> None:
        self.hits = hits or []
        self.fail = fail
        self.calls: list[tuple[str, int, str | None]] = []

    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]:
        self.calls.append((query, max_results, time_range))
        if self.fail:
            raise ResearchEngineError("/v1/search: gave up after 3 attempts (HTTP 503)")
        return self.hits

    async def fetch_markdown(self, url: str) -> str | None:
        raise AssertionError("sources must not fetch pages")

    async def health(self) -> bool:
        return True


def x_src(handle: str = "karpathy", **extra: Any) -> SourceConfig:
    return SourceConfig(
        id=11,
        kind=SourceKind.X_PERSON,
        name=f"@{handle}",
        config={"handle": handle, **extra},
        trust=Trust.TRUSTED,
    )


def search_src(**config: Any) -> SourceConfig:
    return SourceConfig(
        id=21, kind=SourceKind.SEARCH, name="search", config=config, trust=Trust.DISCOVERY
    )


X_HITS = [
    SearchHit(
        url="https://x.com/karpathy/status/1",
        title='Andrej Karpathy on X: "nanochat is out"',
        snippet="A new repo for training a small chat model &amp; serving it.",
    ),
    SearchHit(
        url="https://x.com/someRepostBot/status/2", title="Repost bot on X", snippet="RT karpathy"
    ),
    SearchHit(
        url="https://x.com/Karpathy/status/3",
        title="",
        snippet="Second post, no title from the engine.",
    ),
    SearchHit(
        url="https://x.com/karpathy",
        title="Andrej Karpathy (@karpathy) / X",
        snippet="Profile page",
    ),
    SearchHit(url="https://example.com/karpathy-news", title="News about Karpathy", snippet="Blog"),
]


async def test_x_person_keeps_only_the_sources_own_posts() -> None:
    engine = FakeResearchEngine(search_results={"site:x.com karpathy": X_HITS})
    result = await XPersonSource(engine, FixedClock(NOW)).collect(x_src(), NOW - timedelta(hours=6))
    assert result.error is None
    assert [c.url for c in result.candidates] == [
        "https://x.com/karpathy/status/1",
        "https://x.com/Karpathy/status/3",
    ]
    first, second = result.candidates
    assert first.title == 'Andrej Karpathy on X: "nanochat is out"'
    assert first.snippet == "A new repo for training a small chat model & serving it."
    assert first.author == "karpathy"
    assert first.published_at is None
    assert second.title == "Second post, no title from the engine."


async def test_x_person_query_time_range_and_max() -> None:
    engine = RecordingEngine()
    source = XPersonSource(engine, FixedClock(NOW))
    await source.collect(x_src("@simonw", max=6), NOW - timedelta(hours=6))
    await source.collect(x_src("simonw"), NOW - timedelta(days=3))
    assert engine.calls == [("site:x.com simonw", 6, "day"), ("site:x.com simonw", 10, "week")]


async def test_x_person_records_research_engine_failure() -> None:
    result = await XPersonSource(RecordingEngine(fail=True), FixedClock(NOW)).collect(x_src(), NOW)
    assert result.candidates == []
    assert result.error is not None and result.error.startswith("ResearchEngineError")


async def test_x_person_needs_a_handle() -> None:
    bad = SourceConfig(id=12, kind=SourceKind.X_PERSON, name="?", config={}, trust=Trust.TRUSTED)
    result = await XPersonSource(RecordingEngine(), FixedClock(NOW)).collect(bad, NOW)
    assert result.error is not None and result.error.startswith("SourceConfigError")


async def test_topic_search() -> None:
    hits = [
        SearchHit(
            url="https://news.example/new-model",
            title="Lab ships <b>new</b> model",
            snippet="Details",
        ),
        SearchHit(
            url="https://x.com/emollick/status/5", title="Ethan Mollick on X", snippet="Thoughts"
        ),
    ]
    engine = FakeResearchEngine(search_results={"new AI model release": hits})
    result = await SearchSource(engine, FixedClock(NOW)).collect(
        search_src(query="new AI model release"), NOW
    )
    assert result.error is None
    assert [(c.title, c.author) for c in result.candidates] == [
        ("Lab ships new model", None),
        ("Ethan Mollick on X", "emollick"),
    ]


async def test_site_search_drops_off_site_results() -> None:
    hits = [
        SearchHit(
            url="https://www.anthropic.com/news/claude-haiku-5-5",
            title="Claude Haiku 5.5",
            snippet="a",
        ),
        SearchHit(url="https://anthropic.com/news/policy", title="Policy update", snippet="b"),
        SearchHit(url="https://example.com/about-anthropic", title="About Anthropic", snippet="c"),
        SearchHit(url="https://anthropic.com/newsletter", title="Newsletter", snippet="d"),
    ]
    engine = RecordingEngine(hits)
    result = await SearchSource(engine, FixedClock(NOW)).collect(
        search_src(site="anthropic.com/news"), NOW
    )
    assert [c.title for c in result.candidates] == ["Claude Haiku 5.5", "Policy update"]
    assert engine.calls == [("site:anthropic.com/news", 10, "day")]


async def test_site_plus_query() -> None:
    engine = RecordingEngine()
    await SearchSource(engine, FixedClock(NOW)).collect(
        search_src(site="openai.com/index", query="model", max=5), NOW
    )
    assert engine.calls == [("site:openai.com/index model", 5, "day")]


async def test_search_needs_query_or_site() -> None:
    result = await SearchSource(RecordingEngine(), FixedClock(NOW)).collect(search_src(), NOW)
    assert result.error is not None and result.error.startswith("SourceConfigError")


def test_matches_site() -> None:
    assert matches_site("https://www.openai.com/index/gpt/", "openai.com/index")
    assert matches_site("https://openai.com/index", "openai.com/index")
    assert not matches_site("https://openai.com/indexes", "openai.com/index")
    assert matches_site("https://simonwillison.net/2026/Oct/14/x/", "simonwillison.net")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_sources_x_search.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_newsdesk.sources.search'`.

- [ ] **Step 3: Write the implementation**

`src/ai_newsdesk/sources/x_person.py`:

```python
"""One X account (kind `x_person`), via research-engine search. Snippets only: X pages are never
fetched."""

from __future__ import annotations

from datetime import UTC, datetime

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, clean_text, x_handle
from ai_newsdesk.protocols import Clock, ResearchEngine
from ai_newsdesk.sources.base import BaseSource, cfg_int, cfg_str, time_range_for


class XPersonSource(BaseSource):
    """Config: `handle` (required, with or without @), `max` (default 10).

    Query `site:x.com <handle>` (validated by the 2026-10-14 probe). Only status URLs whose handle
    equals the source handle (case-insensitive) are kept, so reposts and replies by other accounts
    are dropped. Titles such as `Name on X: "..."` are kept as they are.
    """

    kind = SourceKind.X_PERSON

    def __init__(self, research_engine: ResearchEngine, clock: Clock | None = None) -> None:
        self._re = research_engine
        self._clock = clock

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        handle = cfg_str(src, "handle").strip().lstrip("@")
        now = self._clock.now() if self._clock else datetime.now(UTC)
        hits = await self._re.search(
            f"site:x.com {handle}",
            max_results=cfg_int(src, "max", 10),
            time_range=time_range_for(since, now),
        )
        out: list[Candidate] = []
        for hit in hits:
            author = x_handle(hit.url)
            if author is None or author.lower() != handle.lower():
                continue
            snippet = clean_text(hit.snippet, SNIPPET_MAX)
            title = clean_text(hit.title, TITLE_MAX) or snippet[:TITLE_MAX]
            if not title:
                continue
            out.append(
                Candidate(
                    source_id=src.id, url=hit.url, title=title, author=handle, snippet=snippet
                )
            )
        return out
```

`src/ai_newsdesk/sources/search.py`:

```python
"""research-engine searches (kind `search`): topic queries and domain-scoped (`site:`) queries."""

from __future__ import annotations

from datetime import UTC, datetime

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, canonical_url, clean_text, x_handle
from ai_newsdesk.protocols import Clock, ResearchEngine
from ai_newsdesk.sources.base import BaseSource, SourceConfigError, cfg_int, cfg_str, time_range_for


def build_query(src: SourceConfig) -> str:
    query = cfg_str(src, "query", "").strip()
    site = cfg_str(src, "site", "").strip().strip("/")
    built = f"site:{site} {query}".strip() if site else query
    if not built:
        raise SourceConfigError(f"source {src.id} ({src.name}): config needs 'query' or 'site'")
    return built


def matches_site(url: str, site: str) -> bool:
    """True when the URL is on `site` (host plus optional path prefix, `www.` ignored)."""
    rest = canonical_url(url).split("://", 1)[-1].lower()
    prefix = site.strip().strip("/").lower().removeprefix("www.")
    return rest == prefix or rest.startswith((prefix + "/", prefix + "?"))


class SearchSource(BaseSource):
    """Config: `query` and/or `site` (at least one), `max` (default 10).

    With `site`, results off that site are dropped: not every engine honours `site:`, and a
    site-scoped source may carry `primary` trust. X posts found by a topic search keep their handle
    as author.
    """

    kind = SourceKind.SEARCH

    def __init__(self, research_engine: ResearchEngine, clock: Clock | None = None) -> None:
        self._re = research_engine
        self._clock = clock

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        query = build_query(src)
        site = cfg_str(src, "site", "")
        now = self._clock.now() if self._clock else datetime.now(UTC)
        hits = await self._re.search(
            query, max_results=cfg_int(src, "max", 10), time_range=time_range_for(since, now)
        )
        out: list[Candidate] = []
        for hit in hits:
            if site and not matches_site(hit.url, site):
                continue
            snippet = clean_text(hit.snippet, SNIPPET_MAX)
            title = clean_text(hit.title, TITLE_MAX) or snippet[:TITLE_MAX]
            if not title:
                continue
            out.append(
                Candidate(
                    source_id=src.id,
                    url=hit.url,
                    title=title,
                    author=x_handle(hit.url),
                    snippet=snippet,
                )
            )
        return out
```

- [ ] **Step 4: Run it and watch it pass**

Run: `uv run ruff format && uv run ruff check && uv run pyright src/ai_newsdesk/sources tests/test_sources_x_search.py && uv run pytest tests/test_sources_x_search.py -q`
Expected: no lint or type errors; `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ai_newsdesk/sources/x_person.py src/ai_newsdesk/sources/search.py tests/test_sources_x_search.py
git commit -m "feat(sources): X person and research-engine search sources (V1-S2)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: Hacker News, arXiv and GitHub releases

**Goal:** `HNSource` (Algolia, points threshold), `ArxivSource` (export API, cs.CL/cs.AI/cs.LG, trust `discovery`; the hard pre-filter is step 3's job) and `GitHubReleasesSource` (`releases.atom`, reusing `FeedSource`).

**Files:**
- Create: `src/ai_newsdesk/sources/hn.py`
- Create: `src/ai_newsdesk/sources/arxiv.py`
- Create: `src/ai_newsdesk/sources/github.py`
- Create: `tests/fixtures/hn/search_by_date.json`
- Create: `tests/fixtures/feeds/arxiv_query.xml`
- Create: `tests/fixtures/feeds/github_releases.atom`
- Create: `tests/test_sources_hn_arxiv_github.py`

**Acceptance Criteria:**
- [ ] `HNSource` GETs `https://hn.algolia.com/api/v1/search_by_date` once per configured query with `tags=story`, `numericFilters=points>=<min_points>,created_at_i><cutoff epoch>` and `hitsPerPage=<max>`; `min_points` comes from source config (the pipeline injects `RuntimeSettings.hn_min_points`), default 100; look-back default 24 h.
- [ ] HN stories without a URL link to `https://news.ycombinator.com/item?id=<objectID>`; `hn_points`, `author`, `published_at` and the cleaned `story_text` are set; a story found by two queries appears once; untitled hits are skipped.
- [ ] `ArxivSource` requests exactly `https://export.arxiv.org/api/query?search_query=cat:cs.CL+OR+cat:cs.AI+OR+cat:cs.LG&sortBy=submittedDate&sortOrder=descending&max_results=<max>` (categories and `max` configurable, default `max` 50), cleans multi-line titles, formats authors as up to 3 names plus "et al.", and drops papers older than `since − 48 h`.
- [ ] `GitHubReleasesSource` reads `https://github.com/<owner>/<repo>/releases.atom`, titles items `<owner>/<repo> <release title>`, defaults `max` to 5, and rejects a malformed `repo` as a config error.
- [ ] HTTP failures come back as `SourceResult.error`.

**Verify:** `uv run pytest tests/test_sources_hn_arxiv_github.py -q` → `8 passed`

**Steps:**

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/hn/search_by_date.json` (`created_at_i` 1791954000 = 2026-10-14T05:00:00Z, 1791930600 = 2026-10-13T22:30:00Z):

```json
{
  "hits": [
    {
      "objectID": "41000001",
      "title": "Claude Haiku 5.5 released",
      "url": "https://www.anthropic.com/news/claude-haiku-5-5",
      "author": "hnreader",
      "points": 512,
      "num_comments": 230,
      "created_at": "2026-10-14T05:00:00Z",
      "created_at_i": 1791954000,
      "story_text": null,
      "_tags": ["story", "author_hnreader", "story_41000001"]
    },
    {
      "objectID": "41000002",
      "title": "Ask HN: How do you evaluate LLM agents?",
      "url": null,
      "author": "builder",
      "points": 150,
      "num_comments": 88,
      "created_at": "2026-10-13T22:30:00Z",
      "created_at_i": 1791930600,
      "story_text": "<p>We are building &amp; testing agents.</p>",
      "_tags": ["story", "author_builder", "story_41000002", "ask_hn"]
    },
    {
      "objectID": "41000003",
      "title": "",
      "url": "https://example.com/untitled",
      "author": "someone",
      "points": 120,
      "created_at_i": 1791930600,
      "_tags": ["story"]
    }
  ],
  "nbHits": 3,
  "page": 0,
  "nbPages": 1,
  "hitsPerPage": 30,
  "query": "AI",
  "params": "query=AI&tags=story"
}
```

`tests/fixtures/feeds/arxiv_query.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>https://arxiv.org/api/example</id>
  <title>arXiv Query: search_query=cat:cs.CL OR cat:cs.AI OR cat:cs.LG</title>
  <updated>2026-10-14T00:00:00Z</updated>
  <opensearch:totalResults>2</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>2</opensearch:itemsPerPage>
  <entry>
    <id>http://arxiv.org/abs/2610.01234v1</id>
    <title>Sparse Mixture of
  Experts at Scale</title>
    <updated>2026-10-13T17:59:59Z</updated>
    <published>2026-10-13T17:59:59Z</published>
    <summary>  We study sparse mixture-of-experts language models
  at scale and report new results.</summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <author><name>Grace Hopper</name></author>
    <author><name>Edsger Dijkstra</name></author>
    <link href="http://arxiv.org/abs/2610.01234v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2610.01234v1" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2610.00001v1</id>
    <title>An Older Paper</title>
    <updated>2026-10-10T12:00:00Z</updated>
    <published>2026-10-10T12:00:00Z</published>
    <summary>Too old for this run.</summary>
    <author><name>Solo Author</name></author>
    <link href="http://arxiv.org/abs/2610.00001v1" rel="alternate" type="text/html"/>
    <arxiv:primary_category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
```

`tests/fixtures/feeds/github_releases.atom`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/" xml:lang="en-US">
  <id>tag:github.com,2008:https://github.com/vllm-project/vllm/releases</id>
  <link type="text/html" rel="alternate" href="https://github.com/vllm-project/vllm/releases"/>
  <title>Release notes from vllm</title>
  <updated>2026-10-13T20:00:00Z</updated>
  <entry>
    <id>tag:github.com,2008:Repository/599547518/v0.12.0</id>
    <updated>2026-10-13T20:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/vllm-project/vllm/releases/tag/v0.12.0"/>
    <title>v0.12.0</title>
    <content type="html">&lt;h2&gt;Highlights&lt;/h2&gt;&lt;p&gt;Faster MoE kernels.&lt;/p&gt;</content>
    <author><name>release-bot</name></author>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/599547518/v0.11.0</id>
    <updated>2026-09-01T10:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/vllm-project/vllm/releases/tag/v0.11.0"/>
    <title>v0.11.0</title>
    <content type="html">&lt;p&gt;Older release.&lt;/p&gt;</content>
    <author><name>release-bot</name></author>
  </entry>
</feed>
```

- [ ] **Step 2: Write the failing test** — `tests/test_sources_hn_arxiv_github.py`:

```python
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import respx

from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.sources.arxiv import ARXIV_API, ArxivSource, format_authors
from ai_newsdesk.sources.github import GitHubReleasesSource
from ai_newsdesk.sources.hn import HN_SEARCH_URL, HNSource

FIXTURES = Path(__file__).parent / "fixtures"
HN_JSON = json.loads((FIXTURES / "hn" / "search_by_date.json").read_text())
ARXIV_XML = (FIXTURES / "feeds" / "arxiv_query.xml").read_bytes()
GITHUB_ATOM = (FIXTURES / "feeds" / "github_releases.atom").read_bytes()
SINCE = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)


def make_src(kind: SourceKind, **config: Any) -> SourceConfig:
    return SourceConfig(id=31, kind=kind, name=str(kind), config=config, trust=Trust.DISCOVERY)


async def test_hn_stories(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(HN_SEARCH_URL).mock(return_value=httpx.Response(200, json=HN_JSON))
    result = await HNSource(httpx.AsyncClient()).collect(make_src(SourceKind.HN), SINCE)
    assert result.error is None
    assert [(c.url, c.hn_points) for c in result.candidates] == [
        ("https://www.anthropic.com/news/claude-haiku-5-5", 512),
        ("https://news.ycombinator.com/item?id=41000002", 150),
    ]
    ask = result.candidates[1]
    assert ask.author == "builder"
    assert ask.snippet == "We are building & testing agents."
    assert ask.published_at == datetime(2026, 10, 13, 22, 30, tzinfo=UTC)
    params = route.calls.last.request.url.params
    created_after = int((SINCE - timedelta(hours=24)).timestamp())
    assert params["query"] == "AI"
    assert params["tags"] == "story"
    assert params["numericFilters"] == f"points>=100,created_at_i>{created_after}"
    assert params["hitsPerPage"] == "30"


async def test_hn_min_points_and_queries_from_config(respx_mock: respx.MockRouter) -> None:
    sent: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=HN_JSON)

    respx_mock.get(HN_SEARCH_URL).mock(side_effect=reply)
    source = HNSource(httpx.AsyncClient())
    result = await source.collect(
        make_src(SourceKind.HN, queries=["AI", "LLM"], min_points=250), SINCE
    )
    assert [request.url.params["query"] for request in sent] == ["AI", "LLM"]
    assert sent[0].url.params["numericFilters"].startswith("points>=250,")
    assert len(result.candidates) == 2  # the same stories from both queries are kept once


async def test_hn_http_error_is_recorded(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(HN_SEARCH_URL).mock(return_value=httpx.Response(503))
    result = await HNSource(httpx.AsyncClient()).collect(make_src(SourceKind.HN), SINCE)
    assert result.candidates == []
    assert result.error is not None and result.error.startswith("HTTPStatusError")


async def test_arxiv_papers(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(ARXIV_API).mock(return_value=httpx.Response(200, content=ARXIV_XML))
    result = await ArxivSource(httpx.AsyncClient()).collect(
        make_src(SourceKind.ARXIV, max=25), SINCE
    )
    assert result.error is None
    [paper] = result.candidates
    assert paper.url == "http://arxiv.org/abs/2610.01234v1"
    assert paper.title == "Sparse Mixture of Experts at Scale"
    assert paper.author == "Ada Lovelace, Alan Turing, Grace Hopper et al."
    assert (
        paper.snippet
        == "We study sparse mixture-of-experts language models at scale and report new results."
    )
    assert paper.published_at == datetime(2026, 10, 13, 17, 59, 59, tzinfo=UTC)
    assert str(route.calls.last.request.url) == (
        "https://export.arxiv.org/api/query?search_query=cat:cs.CL+OR+cat:cs.AI+OR+cat:cs.LG"
        "&sortBy=submittedDate&sortOrder=descending&max_results=25"
    )


async def test_arxiv_categories_from_config(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(ARXIV_API).mock(return_value=httpx.Response(200, content=ARXIV_XML))
    await ArxivSource(httpx.AsyncClient()).collect(
        make_src(SourceKind.ARXIV, categories=["cs.CV"]), SINCE
    )
    assert "search_query=cat:cs.CV&" in str(route.calls.last.request.url)


def test_format_authors() -> None:
    assert format_authors([]) is None
    assert format_authors(["A"]) == "A"
    assert format_authors(["A", "B", "C"]) == "A, B, C"
    assert format_authors(["A", "B", "C", "D"]) == "A, B, C et al."


async def test_github_releases(respx_mock: respx.MockRouter) -> None:
    url = "https://github.com/vllm-project/vllm/releases.atom"
    respx_mock.get(url).mock(return_value=httpx.Response(200, content=GITHUB_ATOM))
    result = await GitHubReleasesSource(httpx.AsyncClient()).collect(
        make_src(SourceKind.GITHUB, repo="vllm-project/vllm"), SINCE
    )
    assert result.error is None
    [release] = result.candidates
    assert release.title == "vllm-project/vllm v0.12.0"
    assert release.url == "https://github.com/vllm-project/vllm/releases/tag/v0.12.0"
    assert release.author == "release-bot"
    assert release.snippet == "Highlights Faster MoE kernels."


async def test_github_bad_repo_is_a_config_error() -> None:
    result = await GitHubReleasesSource(httpx.AsyncClient()).collect(
        make_src(SourceKind.GITHUB, repo="not a repo"), SINCE
    )
    assert result.error is not None and result.error.startswith("SourceConfigError")
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_sources_hn_arxiv_github.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_newsdesk.sources.arxiv'`.

- [ ] **Step 4: Write the implementation**

`src/ai_newsdesk/sources/hn.py`:

```python
"""Hacker News stories above a points threshold, via the Algolia API (kind `hn`)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, clean_text
from ai_newsdesk.sources.base import USER_AGENT, BaseSource, cfg_int, cfg_list, cutoff

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={}"
DEFAULT_MIN_POINTS = (
    100  # RuntimeSettings.hn_min_points default; the pipeline injects the live value
)


class _Hit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    object_id: str = Field(alias="objectID")
    title: str | None = None
    url: str | None = None
    author: str | None = None
    points: int | None = None
    created_at_i: int | None = None
    story_text: str | None = None


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hits: list[_Hit] = Field(default_factory=list[_Hit])


class HNSource(BaseSource):
    """Config: `queries` (default ["AI"]), `min_points` (default 100), `max` hits per query (default
    30), `lookback_hours` (default 24). Stories are found by creation time, so the look-back lets a
    story that crossed the threshold after the last run still be caught.
    """

    kind = SourceKind.HN
    lookback_hours = 24

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        queries = cfg_list(src, "queries", ["AI"])
        min_points = cfg_int(src, "min_points", DEFAULT_MIN_POINTS)
        per_query = cfg_int(src, "max", 30)
        created_after = int(cutoff(src, since, self.lookback_hours).timestamp())
        seen: set[str] = set()
        out: list[Candidate] = []
        for query in queries:
            resp = await self._http.get(
                HN_SEARCH_URL,
                params={
                    "query": query,
                    "tags": "story",
                    "numericFilters": f"points>={min_points},created_at_i>{created_after}",
                    "hitsPerPage": per_query,
                },
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            for hit in _Response.model_validate(resp.json()).hits:
                title = clean_text(hit.title, TITLE_MAX)
                if hit.object_id in seen or not title:
                    continue
                seen.add(hit.object_id)
                out.append(
                    Candidate(
                        source_id=src.id,
                        url=hit.url or HN_ITEM_URL.format(hit.object_id),
                        title=title,
                        author=hit.author,
                        published_at=datetime.fromtimestamp(hit.created_at_i, tz=UTC)
                        if hit.created_at_i
                        else None,
                        snippet=clean_text(hit.story_text, SNIPPET_MAX),
                        hn_points=hit.points,
                    )
                )
        return out
```

`src/ai_newsdesk/sources/arxiv.py`:

```python
"""New arXiv papers in AI categories via the export API (kind `arxiv`, trust `discovery`).

Volume is high, so every paper is a candidate here and the step-3 pre-filter drops most of them
(authors or affiliations linked to trusted sources, plus HN/X mentions).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, clean_text
from ai_newsdesk.sources.base import USER_AGENT, BaseSource, cfg_int, cfg_list, cutoff, keep_after
from ai_newsdesk.sources.feed import entry_datetime, parse_feed

ARXIV_API = "https://export.arxiv.org/api/query"
DEFAULT_CATEGORIES = ["cs.CL", "cs.AI", "cs.LG"]


def format_authors(names: list[str]) -> str | None:
    if not names:
        return None
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + " et al."


def query_url(src: SourceConfig) -> str:
    categories = cfg_list(src, "categories", DEFAULT_CATEGORIES)
    search = "+OR+".join(f"cat:{category}" for category in categories)
    count = cfg_int(src, "max", 50)
    return (
        f"{ARXIV_API}?search_query={search}"
        f"&sortBy=submittedDate&sortOrder=descending&max_results={count}"
    )


class ArxivSource(BaseSource):
    """Config: `categories` (default cs.CL, cs.AI, cs.LG), `max` (default 50), `lookback_hours`
    (default 48; papers reach the API a day or more after submission)."""

    kind = SourceKind.ARXIV
    lookback_hours = 48

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        resp = await self._http.get(
            query_url(src), headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        resp.raise_for_status()
        cut = cutoff(src, since, self.lookback_hours)
        out: list[Candidate] = []
        for entry in parse_feed(resp.content):
            link: str = entry.get("link") or ""
            title = clean_text(entry.get("title") or "", TITLE_MAX)
            published = entry_datetime(entry)
            if not link or not title or not keep_after(published, cut):
                continue
            authors: list[Any] = entry.get("authors") or []
            names = [clean_text(a.get("name") or "") for a in authors]
            out.append(
                Candidate(
                    source_id=src.id,
                    url=link,
                    title=title,
                    author=format_authors([n for n in names if n]),
                    published_at=published,
                    snippet=clean_text(entry.get("summary") or "", SNIPPET_MAX),
                )
            )
        return out
```

`src/ai_newsdesk/sources/github.py`:

```python
"""GitHub releases of one repository via its Atom feed (kind `github`)."""

from __future__ import annotations

import re
from typing import Any

from ai_newsdesk.models import SourceConfig, SourceKind
from ai_newsdesk.normalise import TITLE_MAX, clean_text
from ai_newsdesk.sources.base import SourceConfigError, cfg_str
from ai_newsdesk.sources.feed import FeedSource

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def repo_of(src: SourceConfig) -> str:
    repo = cfg_str(src, "repo").strip().strip("/")
    if not _REPO.match(repo):
        raise SourceConfigError(
            f"source {src.id} ({src.name}): config 'repo' must look like owner/name"
        )
    return repo


class GitHubReleasesSource(FeedSource):
    """Config: `repo` ("owner/name", required), `max` (default 5), `lookback_hours` (default 24).
    Same ETag/Last-Modified handling as FeedSource. Titles are prefixed with the repo name."""

    kind = SourceKind.GITHUB
    default_max = 5

    def feed_url(self, src: SourceConfig) -> str:
        return f"https://github.com/{repo_of(src)}/releases.atom"

    def entry_title(self, src: SourceConfig, entry: Any) -> str:
        release = clean_text(entry.get("title") or "", TITLE_MAX)
        return clean_text(f"{repo_of(src)} {release}", TITLE_MAX) if release else ""
```

- [ ] **Step 5: Run it and watch it pass**

Run: `uv run ruff format && uv run ruff check && uv run pyright src/ai_newsdesk/sources tests/test_sources_hn_arxiv_github.py && uv run pytest tests/test_sources_hn_arxiv_github.py -q`
Expected: no lint or type errors; `8 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/ai_newsdesk/sources/hn.py src/ai_newsdesk/sources/arxiv.py src/ai_newsdesk/sources/github.py \
  tests/fixtures/hn/search_by_date.json tests/fixtures/feeds/arxiv_query.xml \
  tests/fixtures/feeds/github_releases.atom tests/test_sources_hn_arxiv_github.py
git commit -m "feat(sources): Hacker News, arXiv and GitHub releases sources (V1-S2)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 14: Registry, seed sources and body enrichment

**Goal:** `build_sources()` wires one adapter per `SourceKind`; `config/sources.yaml` holds the seed list (feed URLs checked working on 2026-10-14); `enrich_bodies()` fills thin, non-X items with a research-engine Markdown excerpt.

**Files:**
- Create: `src/ai_newsdesk/sources/registry.py`
- Create: `src/ai_newsdesk/enrich.py`
- Create: `config/sources.yaml`
- Create: `tests/test_sources_registry.py`
- Create: `tests/test_enrich.py`

**Acceptance Criteria:**
- [ ] `build_sources(http, research_engine, clock=None)` returns a dict whose keys are exactly `set(SourceKind)` and whose values have `.kind == key`.
- [ ] Every `config/sources.yaml` entry validates as `SourceConfig(id=n, **entry)`; names are unique; every kind appears; labs' own feeds and site-scoped lab searches are `primary`, named people and newsletters `trusted`, media/arXiv/HN/topic searches `discovery`; no SEO-bait searches (`best AI tools 2026`, `AI news today`); X handles are unique and valid; every `arxiv` entry has a non-empty, duplicate-free `keywords` list (step 3 requirement).
- [ ] `enrich_bodies` fetches only items whose snippet is shorter than `thin_chars` (default 300) and that are neither `x_person` items nor X/Twitter URLs; it stores `body_excerpt` via `Store.set_item_fields` only when the excerpt is longer than the snippet; it returns the number of items enriched; failures leave the snippet in place; no ids → no store calls.

**Verify:** `uv run pytest tests/test_sources_registry.py tests/test_enrich.py -q` → `12 passed`

**Steps:**

- [ ] **Step 1: Write the failing tests**

`tests/test_sources_registry.py`:

```python
import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.normalise import is_x_url
from ai_newsdesk.sources.registry import build_sources
from tests.fakes import FakeResearchEngine

SOURCES_YAML = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"


def seed_entries() -> list[dict[str, Any]]:
    loaded: dict[str, list[dict[str, Any]]] = yaml.safe_load(SOURCES_YAML.read_text())
    entries = loaded["sources"]
    assert isinstance(entries, list)
    return entries


def seed_configs() -> list[SourceConfig]:
    return [SourceConfig(id=i, **entry) for i, entry in enumerate(seed_entries(), start=1)]


def test_every_kind_has_a_matching_adapter() -> None:
    sources = build_sources(httpx.AsyncClient(), FakeResearchEngine())
    assert set(sources) == set(SourceKind)
    for kind, source in sources.items():
        assert source.kind == kind


def test_seed_file_loads_into_source_configs() -> None:
    configs = seed_configs()
    assert len(configs) >= 60
    assert len({c.name for c in configs}) == len(configs), "names must be unique"
    assert {c.kind for c in configs} == set(SourceKind)
    assert all(c.enabled for c in configs)


def test_seed_configs_are_well_formed() -> None:
    for c in seed_configs():
        if c.kind in (SourceKind.FEED,):
            url = c.config["url"]
            assert isinstance(url, str) and url.startswith("https://") and not is_x_url(url), c.name
        if c.kind == SourceKind.X_PERSON:
            handle = c.config["handle"]
            assert isinstance(handle, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle), c.name
        if c.kind == SourceKind.SEARCH:
            assert c.config.get("query") or c.config.get("site"), c.name
        if c.kind == SourceKind.GITHUB:
            repo = c.config["repo"]
            assert isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo), c.name


def test_seed_arxiv_sources_carry_keywords() -> None:
    arxiv = [c for c in seed_configs() if c.kind == SourceKind.ARXIV]
    assert arxiv
    for c in arxiv:
        keywords = c.config.get("keywords")
        assert isinstance(keywords, list) and keywords, c.name
        assert all(isinstance(k, str) and k.strip() for k in keywords), c.name
        assert len({k.lower() for k in keywords}) == len(keywords), c.name


def test_seed_trust_levels() -> None:
    by_name = {c.name: c for c in seed_configs()}
    assert by_name["OpenAI"].trust == Trust.PRIMARY
    assert by_name["Simon Willison"].trust == Trust.TRUSTED
    assert by_name["X: @karpathy"].trust == Trust.TRUSTED
    for c in by_name.values():
        if c.kind in (SourceKind.ARXIV, SourceKind.HN) or c.name.startswith("Search: "):
            assert c.trust == Trust.DISCOVERY, c.name


def test_seed_x_handles_are_unique() -> None:
    handles = [
        str(c.config["handle"]).lower() for c in seed_configs() if c.kind == SourceKind.X_PERSON
    ]
    assert len(handles) == len(set(handles))


def test_seed_has_no_seo_bait_searches() -> None:
    queries = [
        str(c.config.get("query", "")).lower()
        for c in seed_configs()
        if c.kind == SourceKind.SEARCH
    ]
    assert "best ai tools 2026" not in queries
    assert "ai news today" not in queries
```

`tests/test_enrich.py`:

```python
from typing import Any

from ai_newsdesk.enrich import enrich_bodies
from ai_newsdesk.models import SearchHit, SourceKind
from tests.fakes import FakeResearchEngine

LONG_LINE = (
    "The new model is available in the API today, priced at one dollar per million input tokens."
)
ARTICLE = f"# Launch\n\n{LONG_LINE}\n\nShare\n\n{LONG_LINE}"
EXCERPT = f"{LONG_LINE} {LONG_LINE}"


class TinyStore:
    def __init__(self, rows: list[tuple[int, str, SourceKind, str]]) -> None:
        self.rows = rows
        self.updates: list[tuple[int, dict[str, Any]]] = []
        self.lookups: list[list[int]] = []

    async def items_for_enrichment(
        self, item_ids: list[int]
    ) -> list[tuple[int, str, SourceKind, str]]:
        self.lookups.append(item_ids)
        return [row for row in self.rows if row[0] in item_ids]

    async def set_item_fields(self, item_id: int, **fields: Any) -> None:
        self.updates.append((item_id, fields))


class RecordingEngine:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]:
        return []

    async def fetch_markdown(self, url: str) -> str | None:
        self.fetched.append(url)
        return self.pages.get(url)

    async def health(self) -> bool:
        return True


async def test_thin_items_get_an_excerpt() -> None:
    store = TinyStore([(1, "https://lab.example/a", SourceKind.FEED, "Short.")])
    engine = FakeResearchEngine(pages={"https://lab.example/a": ARTICLE})
    assert await enrich_bodies(store, engine, [1]) == 1
    assert store.updates == [(1, {"body_excerpt": EXCERPT})]


async def test_thick_and_x_items_are_not_fetched() -> None:
    store = TinyStore(
        [
            (1, "https://lab.example/long", SourceKind.FEED, "x" * 300),
            (2, "https://x.com/karpathy/status/1", SourceKind.X_PERSON, "Short."),
            (3, "https://twitter.com/emollick/status/2", SourceKind.SEARCH, "Short."),
            (4, "https://news.example/b", SourceKind.HN, ""),
        ]
    )
    engine = RecordingEngine({"https://news.example/b": ARTICLE})
    assert await enrich_bodies(store, engine, [1, 2, 3, 4]) == 1
    assert engine.fetched == ["https://news.example/b"]
    assert store.updates == [(4, {"body_excerpt": EXCERPT})]


async def test_failed_or_useless_fetch_keeps_the_snippet() -> None:
    snippet = "A snippet that is longer than the fetched page's prose, so it stays."
    store = TinyStore(
        [
            (1, "https://lab.example/missing", SourceKind.FEED, "Short."),
            (2, "https://lab.example/menu-only", SourceKind.SEARCH, snippet),
        ]
    )
    engine = FakeResearchEngine(pages={"https://lab.example/menu-only": "Home\nAbout\nSubscribe"})
    assert await enrich_bodies(store, engine, [1, 2]) == 0
    assert store.updates == []


async def test_custom_thin_threshold() -> None:
    store = TinyStore([(1, "https://lab.example/a", SourceKind.FEED, "y" * 50)])
    engine = RecordingEngine({"https://lab.example/a": ARTICLE})
    assert await enrich_bodies(store, engine, [1], thin_chars=40) == 0
    assert engine.fetched == []


async def test_no_ids_means_no_work() -> None:
    store = TinyStore([])
    assert await enrich_bodies(store, FakeResearchEngine(), []) == 0
    assert store.lookups == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_sources_registry.py tests/test_enrich.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_newsdesk.sources.registry'` and `... 'ai_newsdesk.enrich'`.

- [ ] **Step 3: Write the seed list** — `config/sources.yaml`:

```yaml
# Seed sources, loaded into the database on first run (Store.seed_sources_if_empty), then edited in the UI.
# Each entry is a SourceConfig without `id`: kind, name (unique), trust, config, optional enabled/weight.
# Trust: primary = a lab's own channel; trusted = a respected person or newsletter;
#        discovery = media, aggregators, arXiv, HN and searches.
# Feed URLs were checked working on 2026-10-14. X is read through research-engine search, snippets only.
sources:
  # --- Labs' own feeds (primary) ---
  - {kind: feed, name: OpenAI, trust: primary, config: {url: "https://openai.com/news/rss.xml", max: 10}}
  - {kind: feed, name: Google DeepMind, trust: primary, config: {url: "https://deepmind.google/blog/rss.xml", max: 10}}
  - {kind: feed, name: Google AI blog, trust: primary, config: {url: "https://blog.google/technology/ai/rss/", max: 10}}
  - {kind: feed, name: Hugging Face, trust: primary, config: {url: "https://huggingface.co/blog/feed.xml", max: 10}}
  - {kind: feed, name: Mistral AI, trust: primary, config: {url: "https://mistral.ai/rss.xml", max: 10}}
  - {kind: feed, name: Meta AI, trust: primary, config: {url: "https://ai.meta.com/blog/rss/", max: 10}}
  - {kind: feed, name: NVIDIA AI blog, trust: primary, config: {url: "https://blogs.nvidia.com/blog/category/deep-learning/feed/", max: 10}}
  - {kind: feed, name: Microsoft Research, trust: primary, config: {url: "https://www.microsoft.com/en-us/research/feed/", max: 10}}
  - {kind: feed, name: AWS ML blog, trust: primary, config: {url: "https://aws.amazon.com/blogs/machine-learning/feed/", max: 10}}
  - {kind: feed, name: BAIR, trust: primary, config: {url: "https://bair.berkeley.edu/blog/feed.xml", max: 5}}
  # Anthropic has no RSS feed: its news pages come in through a site-scoped search (below).
  - {kind: search, name: Anthropic news, trust: primary, config: {site: "anthropic.com/news", max: 10}}
  - {kind: search, name: OpenAI index, trust: primary, config: {site: "openai.com/index", max: 10}}

  # --- Respected people and newsletters (trusted) ---
  - {kind: feed, name: Simon Willison, trust: trusted, config: {url: "https://simonwillison.net/atom/everything/", max: 15}}
  - {kind: feed, name: Interconnects, trust: trusted, config: {url: "https://www.interconnects.ai/feed", max: 8}}
  - {kind: feed, name: Import AI, trust: trusted, config: {url: "https://importai.substack.com/feed", max: 6}}
  - {kind: feed, name: Latent Space, trust: trusted, config: {url: "https://www.latent.space/feed", max: 10}}
  - {kind: feed, name: Lilian Weng, trust: trusted, config: {url: "https://lilianweng.github.io/index.xml", max: 5}}
  - {kind: feed, name: Sebastian Raschka, trust: trusted, config: {url: "https://magazine.sebastianraschka.com/feed", max: 8}}
  - {kind: feed, name: AI Snake Oil, trust: trusted, config: {url: "https://www.aisnakeoil.com/feed", max: 6}}
  - {kind: feed, name: The Gradient, trust: trusted, config: {url: "https://thegradient.pub/rss/", max: 5}}
  - {kind: feed, name: One Useful Thing, trust: trusted, config: {url: "https://www.oneusefulthing.org/feed", max: 6}}
  - {kind: feed, name: Gary Marcus, trust: trusted, config: {url: "https://garymarcus.substack.com/feed", max: 8}}
  - {kind: feed, name: Chip Huyen, trust: trusted, config: {url: "https://huyenchip.com/feed.xml", max: 4}}
  - {kind: feed, name: Eugene Yan, trust: trusted, config: {url: "https://eugeneyan.com/rss/", max: 4}}
  - {kind: feed, name: SemiAnalysis, trust: trusted, config: {url: "https://newsletter.semianalysis.com/feed", max: 6}}

  # --- X accounts: labs (primary) ---
  - {kind: x_person, name: "X: @AnthropicAI", trust: primary, config: {handle: AnthropicAI}}
  - {kind: x_person, name: "X: @OpenAI", trust: primary, config: {handle: OpenAI}}
  - {kind: x_person, name: "X: @GoogleDeepMind", trust: primary, config: {handle: GoogleDeepMind}}
  - {kind: x_person, name: "X: @MistralAI", trust: primary, config: {handle: MistralAI}}
  - {kind: x_person, name: "X: @huggingface", trust: primary, config: {handle: huggingface}}
  - {kind: x_person, name: "X: @MetaAI", trust: primary, config: {handle: MetaAI}}
  - {kind: x_person, name: "X: @nvidia", trust: primary, config: {handle: nvidia}}

  # --- X accounts: people and research groups (trusted) ---
  - {kind: x_person, name: "X: @karpathy", trust: trusted, config: {handle: karpathy}}
  - {kind: x_person, name: "X: @simonw", trust: trusted, config: {handle: simonw}}
  - {kind: x_person, name: "X: @sama", trust: trusted, config: {handle: sama}}
  - {kind: x_person, name: "X: @ylecun", trust: trusted, config: {handle: ylecun}}
  - {kind: x_person, name: "X: @demishassabis", trust: trusted, config: {handle: demishassabis}}
  - {kind: x_person, name: "X: @JeffDean", trust: trusted, config: {handle: JeffDean}}
  - {kind: x_person, name: "X: @swyx", trust: trusted, config: {handle: swyx}}
  - {kind: x_person, name: "X: @emollick", trust: trusted, config: {handle: emollick}}
  - {kind: x_person, name: "X: @AndrewYNg", trust: trusted, config: {handle: AndrewYNg}}
  - {kind: x_person, name: "X: @DrJimFan", trust: trusted, config: {handle: DrJimFan}}
  - {kind: x_person, name: "X: @hardmaru", trust: trusted, config: {handle: hardmaru}}
  - {kind: x_person, name: "X: @_akhaliq", trust: trusted, config: {handle: _akhaliq}}
  - {kind: x_person, name: "X: @natolambert", trust: trusted, config: {handle: natolambert}}
  - {kind: x_person, name: "X: @jackclarkSF", trust: trusted, config: {handle: jackclarkSF}}
  - {kind: x_person, name: "X: @ESYudkowsky", trust: trusted, config: {handle: ESYudkowsky}}
  - {kind: x_person, name: "X: @GaryMarcus", trust: trusted, config: {handle: GaryMarcus}}
  - {kind: x_person, name: "X: @fchollet", trust: trusted, config: {handle: fchollet}}
  - {kind: x_person, name: "X: @rasbt", trust: trusted, config: {handle: rasbt}}
  - {kind: x_person, name: "X: @lilianweng", trust: trusted, config: {handle: lilianweng}}
  - {kind: x_person, name: "X: @OfficialLoganK", trust: trusted, config: {handle: OfficialLoganK}}
  - {kind: x_person, name: "X: @ClementDelangue", trust: trusted, config: {handle: ClementDelangue}}
  - {kind: x_person, name: "X: @SebastienBubeck", trust: trusted, config: {handle: SebastienBubeck}}
  - {kind: x_person, name: "X: @polynoamial", trust: trusted, config: {handle: polynoamial}}
  - {kind: x_person, name: "X: @gdb", trust: trusted, config: {handle: gdb}}
  - {kind: x_person, name: "X: @mustafasuleyman", trust: trusted, config: {handle: mustafasuleyman}}
  - {kind: x_person, name: "X: @AravSrinivas", trust: trusted, config: {handle: AravSrinivas}}
  - {kind: x_person, name: "X: @EpochAIResearch", trust: trusted, config: {handle: EpochAIResearch}}
  - {kind: x_person, name: "X: @tszzl", trust: trusted, config: {handle: tszzl}}

  # --- Media (discovery) ---
  - {kind: feed, name: The Decoder, trust: discovery, config: {url: "https://the-decoder.com/feed/", max: 15}}
  - {kind: feed, name: TechCrunch AI, trust: discovery, config: {url: "https://techcrunch.com/category/artificial-intelligence/feed/", max: 15}}
  - {kind: feed, name: VentureBeat AI, trust: discovery, config: {url: "https://venturebeat.com/category/ai/feed/", max: 15}}
  - {kind: feed, name: MIT Tech Review AI, trust: discovery, config: {url: "https://www.technologyreview.com/topic/artificial-intelligence/feed", max: 10}}
  - {kind: feed, name: The Verge AI, trust: discovery, config: {url: "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", max: 12}}
  - {kind: feed, name: Ars Technica AI, trust: discovery, config: {url: "https://arstechnica.com/ai/feed/", max: 10}}

  # --- Topic searches (discovery); SEO bait such as "best AI tools" or "AI news today" is left out ---
  - {kind: search, name: "Search: new AI model release", trust: discovery, config: {query: "new AI model release", max: 6}}
  - {kind: search, name: "Search: open weights model release", trust: discovery, config: {query: "open weights model release", max: 6}}
  - {kind: search, name: "Search: AI research paper breakthrough", trust: discovery, config: {query: "AI research paper breakthrough", max: 6}}
  - {kind: search, name: "Search: AI lab announcement", trust: discovery, config: {query: "AI lab announcement", max: 6}}
  - {kind: search, name: "Search: AI startup funding round", trust: discovery, config: {query: "AI startup funding round", max: 6}}
  - {kind: search, name: "Search: AI acquisition", trust: discovery, config: {query: "AI acquisition", max: 6}}
  - {kind: search, name: "Search: AI regulation news", trust: discovery, config: {query: "AI regulation news", max: 6}}
  - {kind: search, name: "Search: AI safety news", trust: discovery, config: {query: "AI safety news", max: 6}}
  - {kind: search, name: "Search: AI chip announcement", trust: discovery, config: {query: "AI chip announcement", max: 6}}
  - {kind: search, name: "Search: LLM benchmark results", trust: discovery, config: {query: "LLM benchmark results", max: 6}}
  - {kind: search, name: "Search: AI agent launch", trust: discovery, config: {query: "AI agent launch", max: 6}}
  - {kind: search, name: "Search: AI lawsuit copyright", trust: discovery, config: {query: "AI lawsuit copyright", max: 6}}

  # --- Research and aggregators (discovery) ---
  # The step 3 pre-filter drops an arXiv paper unless its title or author list contains one of `keywords`
  # (whole word or phrase, case-insensitive); an arXiv source without keywords drops everything.
  - kind: arxiv
    name: arXiv cs.CL/cs.AI/cs.LG
    trust: discovery
    config:
      categories: [cs.CL, cs.AI, cs.LG]
      max: 50
      keywords: [OpenAI, Anthropic, DeepMind, Google, Meta, FAIR, Mistral, Hugging Face, NVIDIA, Microsoft,
                 Claude, Gemini, GPT, Llama, Qwen, DeepSeek, Andrej Karpathy, Yann LeCun, Yoshua Bengio,
                 Geoffrey Hinton, Ilya Sutskever, Jeff Dean, Percy Liang, Christopher Manning, Chelsea Finn,
                 Sergey Levine, Noam Brown, Jason Wei, Sebastian Raschka, Lilian Weng]
  # min_points is not set here: the pipeline injects RuntimeSettings.hn_min_points (default 100).
  - {kind: hn, name: Hacker News, trust: discovery, config: {queries: [AI, LLM, GPT, Claude, OpenAI, Gemini, agents]}}

  # --- Key open-model and tooling releases (discovery: patch releases are noisy, the pre-filter decides) ---
  - {kind: github, name: "GitHub: huggingface/transformers", trust: discovery, config: {repo: huggingface/transformers, max: 3}}
  - {kind: github, name: "GitHub: vllm-project/vllm", trust: discovery, config: {repo: vllm-project/vllm, max: 3}}
  - {kind: github, name: "GitHub: ollama/ollama", trust: discovery, config: {repo: ollama/ollama, max: 3}}
  - {kind: github, name: "GitHub: ggml-org/llama.cpp", trust: discovery, config: {repo: ggml-org/llama.cpp, max: 3}}
  - {kind: github, name: "GitHub: pytorch/pytorch", trust: discovery, config: {repo: pytorch/pytorch, max: 3}}
```

- [ ] **Step 4: Write the implementation**

`src/ai_newsdesk/sources/registry.py`:

```python
"""One adapter per SourceKind. Only app.py calls this; everything else receives the dict."""

from __future__ import annotations

import httpx

from ai_newsdesk.models import SourceKind
from ai_newsdesk.protocols import Clock, ResearchEngine, Source
from ai_newsdesk.sources.arxiv import ArxivSource
from ai_newsdesk.sources.feed import FeedSource
from ai_newsdesk.sources.github import GitHubReleasesSource
from ai_newsdesk.sources.hn import HNSource
from ai_newsdesk.sources.search import SearchSource
from ai_newsdesk.sources.x_person import XPersonSource


def build_sources(
    http: httpx.AsyncClient, research_engine: ResearchEngine, clock: Clock | None = None
) -> dict[SourceKind, Source]:
    return {
        SourceKind.FEED: FeedSource(http),
        SourceKind.X_PERSON: XPersonSource(research_engine, clock),
        SourceKind.SEARCH: SearchSource(research_engine, clock),
        SourceKind.ARXIV: ArxivSource(http),
        SourceKind.GITHUB: GitHubReleasesSource(http),
        SourceKind.HN: HNSource(http),
    }
```

`src/ai_newsdesk/enrich.py`:

```python
"""Fill thin item text with an article excerpt read through research-engine.

Spec section 3, "Full text".

The bake-off showed judging is much worse on headline-only items, so items whose snippet is shorter
than `thin_chars` get a Markdown excerpt. X items are never fetched (robots.txt), and a failed fetch
simply leaves the snippet in place.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from ai_newsdesk.models import SourceKind
from ai_newsdesk.normalise import excerpt_from_markdown, is_x_url
from ai_newsdesk.protocols import ResearchEngine


class EnrichStore(Protocol):
    async def items_for_enrichment(
        self, item_ids: list[int]
    ) -> list[tuple[int, str, SourceKind, str]]: ...
    async def set_item_fields(self, item_id: int, **fields: Any) -> None: ...


async def enrich_bodies(
    store: EnrichStore,
    research_engine: ResearchEngine,
    item_ids: list[int],
    *,
    thin_chars: int = 300,
) -> int:
    """Fetch excerpts for thin, non-X items. Returns how many items got a `body_excerpt`. Never
    raises for a single item's failure."""
    if not item_ids:
        return 0
    rows = await store.items_for_enrichment(item_ids)
    targets = [
        (item_id, url, snippet)
        for item_id, url, kind, snippet in rows
        if kind != SourceKind.X_PERSON and not is_x_url(url) and len(snippet) < thin_chars
    ]

    async def one(item_id: int, url: str, snippet: str) -> bool:
        markdown = await research_engine.fetch_markdown(url)
        if not markdown:
            return False
        excerpt = excerpt_from_markdown(markdown)
        if len(excerpt) <= len(snippet):
            return False
        await store.set_item_fields(item_id, body_excerpt=excerpt)
        return True

    results = await asyncio.gather(*(one(*target) for target in targets), return_exceptions=True)
    return sum(1 for result in results if result is True)
```

- [ ] **Step 5: Run them and watch them pass, then the whole suite**

Run: `uv run pytest tests/test_sources_registry.py tests/test_enrich.py -q`
Expected: `12 passed`.

Run: `make test`
Expected: ruff, ruff format check and pyright clean; pytest all green (step 0–1 tests plus the 103 tests of this step: 16 + 36 + 13 + 9 + 9 + 8 + 7 + 5).

- [ ] **Step 6: Commit**

```bash
git add src/ai_newsdesk/sources/registry.py src/ai_newsdesk/enrich.py config/sources.yaml \
  tests/test_sources_registry.py tests/test_enrich.py
git commit -m "feat(sources): adapter registry, seed sources and body enrichment (V1-S2)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Step done when

- [ ] Tasks 9–14 are committed on `step-2-sources`, one commit each, Conventional Commits with `(V1-S2)` and the `Co-Authored-By` trailer.
- [ ] `make test` passes (ruff, ruff format check, pyright, pytest), including all 103 tests added in this step.
- [ ] `grep -rn "X-API-Key" src/` matches only `src/ai_newsdesk/research_engine.py`, and no log line or error message contains a key value (`test_key_never_logged_or_in_errors`).
- [ ] No adapter ever calls `fetch_markdown` for an X URL (`tests/test_research_engine.py::test_fetch_never_requests_x_urls`, `tests/test_enrich.py::test_thick_and_x_items_are_not_fetched`).
- [ ] `config/sources.yaml` contains no lab IPs, internal hostnames or usernames (public repo).
- [ ] The Contract additions above match the master plan's reconciliation decisions (R1 `items_for_enrichment` in step 1, R3 async key provider, R9 `hn_min_points` injection in step 3) and the arXiv seed carries `keywords`.
- [ ] Branch pushed (`git push -u origin step-2-sources`) and a PR opened with `GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr create --base main --head step-2-sources --title "Step 2: sources (V1-S2)"`, CI green.
