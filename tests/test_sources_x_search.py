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


async def test_x_person_always_searches_the_past_week() -> None:
    """A "day" search found too little (35 handles gave 12 posts); repeats are absorbed by the
    canonical-URL dedupe, so X always searches a week, max 10 per handle unless configured."""
    engine = RecordingEngine()
    source = XPersonSource(engine, FixedClock(NOW))
    await source.collect(x_src("@simonw", max=6), NOW - timedelta(hours=6))
    await source.collect(x_src("simonw"), NOW - timedelta(days=3))
    await source.collect(x_src("simonw"), NOW - timedelta(hours=1))
    assert engine.calls == [
        ("site:x.com simonw", 6, "week"),
        ("site:x.com simonw", 10, "week"),
        ("site:x.com simonw", 10, "week"),
    ]


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
