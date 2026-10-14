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
