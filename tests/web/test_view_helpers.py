from __future__ import annotations

from datetime import date, timedelta

from ai_newsdesk.models import Category, SourceConfig, SourceKind, StorySource, Trust
from ai_newsdesk.web.deps import (
    ago,
    category_label,
    day_label,
    icon_for,
    kind_label,
    safe_url,
    short_date,
    signal_of,
    source_detail,
    trust_label,
    unique_sources,
    usd,
)
from tests.web.support import NOW


def test_ago() -> None:
    assert ago(NOW - timedelta(seconds=20), NOW) == "just now"
    assert ago(NOW - timedelta(minutes=7), NOW) == "7 min ago"
    assert ago(NOW - timedelta(hours=3, minutes=5), NOW) == "3 h ago"
    assert ago(NOW - timedelta(days=1, hours=2), NOW) == "yesterday"
    assert ago(NOW - timedelta(days=3), NOW) == "3 days ago"
    assert ago(NOW - timedelta(days=40), NOW) == "4 Sep 2026"
    assert ago(NOW.replace(tzinfo=None) - timedelta(hours=1), NOW) == "1 h ago"
    assert ago(None, NOW) == "never"


def test_dates() -> None:
    assert day_label(date(2026, 10, 14)) == "Wednesday 14 October"
    assert short_date(date(2026, 10, 3)) == "Sat 3 Oct"


def test_usd() -> None:
    assert usd(0) == "$0.00"
    assert usd(0.004) == "under $0.01"
    assert usd(2.07) == "$2.07"
    assert usd(1234.5) == "$1,234.50"


def test_safe_url_allows_only_web_links() -> None:
    assert safe_url("https://lab.example/a?b=1") == "https://lab.example/a?b=1"
    assert safe_url(" http://lab.example/ ") == "http://lab.example/"
    for bad in (
        "javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "data:text/html,x",
        "//evil.example",
        "/local",
        "",
        "https://",
    ):
        assert safe_url(bad) == "#"


def test_signal_of_clamps_and_rounds() -> None:
    assert signal_of(7.6) == 8
    assert signal_of(12.4) == 10
    assert signal_of(-1) == 0


def test_unique_sources_keeps_first_per_name() -> None:
    a = StorySource(
        item_id=1,
        source_name="Lab",
        source_kind=SourceKind.FEED,
        url="https://a.example",
        title="A",
    )
    b = StorySource(
        item_id=2,
        source_name="Lab",
        source_kind=SourceKind.FEED,
        url="https://b.example",
        title="B",
    )
    c = StorySource(
        item_id=3, source_name="HN", source_kind=SourceKind.HN, url="https://c.example", title="C"
    )
    assert [s.item_id for s in unique_sources([a, b, c])] == [1, 3]


def test_labels_and_icons() -> None:
    assert category_label(Category.MODEL_RELEASE) == "Model releases"
    assert category_label("research") == "Research"
    assert kind_label(SourceKind.X_PERSON) == "People on X"
    assert trust_label(Trust.PRIMARY) == "Primary"
    assert icon_for(SourceKind.X_PERSON) == "i-x-logo"
    assert icon_for(SourceKind.FEED) == "i-rss"
    assert icon_for(SourceKind.HN) == "i-hn"
    assert icon_for(SourceKind.SEARCH) is None


def test_source_detail() -> None:
    def src(
        kind: SourceKind, config: dict[str, str | int | float | bool | list[str]]
    ) -> SourceConfig:
        return SourceConfig(id=1, kind=kind, name="n", config=config, trust=Trust.TRUSTED)

    assert (
        source_detail(src(SourceKind.FEED, {"url": "https://a.example/feed"}))
        == "https://a.example/feed"
    )
    assert source_detail(src(SourceKind.X_PERSON, {"handle": "karpathy"})) == "@karpathy"
    assert (
        source_detail(src(SourceKind.SEARCH, {"query": "new AI model"})) == "Search: new AI model"
    )
    assert source_detail(src(SourceKind.GITHUB, {"repo": "org/repo"})) == "github.com/org/repo"
    assert (
        source_detail(src(SourceKind.ARXIV, {"categories": ["cs.AI", "cs.CL"]})) == "cs.AI, cs.CL"
    )
    assert source_detail(src(SourceKind.HN, {})) == "Front-page stories above the points threshold"
