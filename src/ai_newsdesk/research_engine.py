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
