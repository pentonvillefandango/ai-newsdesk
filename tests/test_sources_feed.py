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
