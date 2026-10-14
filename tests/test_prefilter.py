# tests/test_prefilter.py
from typing import Any

from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.prefilter import (
    BLOCKLIST_KEY,
    DROP_BELOW,
    PrefilterInput,
    corroboration,
    decide,
    heuristic_score,
    load_blocklist,
)


def source(
    sid: int = 1,
    *,
    kind: SourceKind = SourceKind.FEED,
    trust: Trust = Trust.PRIMARY,
    weight: float = 1.0,
    config: dict[str, str | int | float | bool | list[str]] | None = None,
) -> SourceConfig:
    return SourceConfig(
        id=sid,
        kind=kind,
        name=f"source {sid}",
        config=config or {"url": "https://example.com/feed.xml"},
        trust=trust,
        weight=weight,
    )


def row(item_id: int, title: str, src: SourceConfig, **kw: object) -> PrefilterInput:
    return PrefilterInput(item_id=item_id, source=src, title=title, **kw)  # type: ignore[arg-type]


def test_trust_weight_times_source_weight() -> None:
    assert heuristic_score(row(1, "Lab post", source(trust=Trust.PRIMARY)), 0, []) == 3.0
    assert (
        heuristic_score(row(1, "Person post", source(trust=Trust.TRUSTED, weight=1.5)), 0, [])
        == 3.0
    )
    assert heuristic_score(row(1, "Search hit", source(trust=Trust.DISCOVERY)), 0, []) == 1.0


def test_low_weight_discovery_item_is_dropped() -> None:
    d = decide(row(7, "Search hit", source(trust=Trust.DISCOVERY, weight=0.5)), 0, [])
    assert d.item_id == 7
    assert d.score == 0.5
    assert d.score < DROP_BELOW
    assert (d.keep, d.reason) == (False, "below_threshold")


def test_hn_points_bonus_is_capped() -> None:
    low = source(kind=SourceKind.HN, trust=Trust.DISCOVERY, weight=0.5)
    assert heuristic_score(row(1, "Show HN", low, hn_points=300), 0, []) == 2.0
    assert heuristic_score(row(1, "Show HN", low, hn_points=5000), 0, []) == 2.5


def test_multi_source_bonus_is_capped() -> None:
    src = source(trust=Trust.DISCOVERY)
    assert heuristic_score(row(1, "Story", src), 1, []) == 2.0
    assert heuristic_score(row(1, "Story", src), 5, []) == 3.0


def test_blocklist_drops_case_insensitive_whole_words() -> None:
    src = source(trust=Trust.PRIMARY)
    hit = decide(row(1, "New AI Crypto airdrop", src), 0, ["crypto"])
    assert (hit.keep, hit.reason) == (False, "blocklist")
    assert hit.score == 3.0 - 10.0
    miss = decide(row(2, "Post-quantum cryptography and LLMs", src), 0, ["crypto"])
    assert (miss.keep, miss.reason) == (True, "ok")


def test_blocklist_matches_phrases_in_snippet() -> None:
    d = decide(
        row(1, "Weekly links", source(), snippet="Join our prompt giveaway now"),
        0,
        ["prompt giveaway"],
    )
    assert d.reason == "blocklist"


def test_arxiv_without_keyword_is_dropped_even_with_high_score() -> None:
    src = source(kind=SourceKind.ARXIV, trust=Trust.PRIMARY, config={"keywords": ["DeepMind"]})
    d = decide(row(1, "A study of attention sinks", src, author="A. Person"), 2, [])
    assert (d.keep, d.reason) == (False, "arxiv_no_keyword")


def test_arxiv_keyword_in_author_keeps_item() -> None:
    src = source(kind=SourceKind.ARXIV, trust=Trust.DISCOVERY, config={"keywords": ["DeepMind"]})
    d = decide(row(1, "A study of attention sinks", src, author="Google DeepMind team"), 0, [])
    assert (d.keep, d.reason) == (True, "ok")


def test_arxiv_source_without_keywords_drops_everything() -> None:
    src = source(kind=SourceKind.ARXIV, trust=Trust.PRIMARY, config={"url": "x"})
    assert decide(row(1, "Anything", src), 0, []).reason == "arxiv_no_keyword"


def test_corroboration_counts_other_sources_and_links() -> None:
    a, b, c = source(1), source(2), source(3)
    rows = [
        row(10, "OpenAI launches GPT-5.5 for developers", a),
        row(11, "OpenAI Launches GPT-5.5 For Developers!", b),
        row(12, "OpenAI launches GPT-5.5 for developers", a),  # same source as 10: no credit
        row(13, "Google updates Gemini app", c),
    ]
    counts = corroboration(rows, linked={13})
    assert counts == {10: 1, 11: 1, 12: 1, 13: 1}


def test_corroboration_empty() -> None:
    assert corroboration([]) == {}


class SettingsStub:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def get_setting(self, key: str) -> Any:
        assert key == BLOCKLIST_KEY
        return self.value


async def test_load_blocklist() -> None:
    assert await load_blocklist(SettingsStub(None)) == []
    assert await load_blocklist(SettingsStub("crypto")) == []
    assert await load_blocklist(SettingsStub([" Crypto ", "", "prompt giveaway"])) == [
        "crypto",
        "prompt giveaway",
    ]
