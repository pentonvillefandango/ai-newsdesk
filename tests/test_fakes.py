"""The fakes satisfy the protocols (checked by pyright) and behave as documented."""

from datetime import timedelta

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import (
    BriefingStory,
    Category,
    Purpose,
    SearchHit,
    SourceKind,
    SourceResult,
    StoryRef,
    Verdict,
    WriteInput,
)
from ai_newsdesk.protocols import Clock, Judge, ResearchEngine, Source, Writer
from tests.fakes import (
    FakeJudge,
    FakeResearchEngine,
    FakeSource,
    FakeWriter,
    make_candidate,
    make_review_input,
    make_source_config,
    utc,
)

# Static protocol conformance: pyright fails `make test` if a fake drifts from its protocol.
_engine: ResearchEngine = FakeResearchEngine()
_source: Source = FakeSource()
_judge: Judge = FakeJudge()
_writer: Writer = FakeWriter()
_clock: Clock = FixedClock(utc(2026, 10, 14))


def test_utc_helper() -> None:
    assert utc(2026, 10, 14, 7, 30).isoformat() == "2026-10-14T07:30:00+00:00"


async def test_fake_research_engine_records_and_answers() -> None:
    hit = SearchHit(url="https://x.com/someone/status/1", title="post")
    engine = FakeResearchEngine(
        search_results={"q": [hit, hit, hit]}, pages={"https://a.test": "# A"}
    )
    assert await engine.search("q", max_results=2) == [hit, hit]
    assert await engine.search("other", time_range=None) == []
    assert engine.search_calls == [("q", 2, "day"), ("other", 10, None)]
    assert await engine.fetch_markdown("https://a.test") == "# A"
    assert await engine.fetch_markdown("https://missing.test") is None
    assert engine.fetch_calls == ["https://a.test", "https://missing.test"]
    assert await engine.health() is True


async def test_fake_research_engine_can_raise() -> None:
    engine = FakeResearchEngine(search_error=RuntimeError("down"), healthy=False)
    with pytest.raises(RuntimeError, match="down"):
        await engine.search("q")
    assert await engine.health() is False


async def test_fake_source_rehomes_candidates_and_records() -> None:
    since = utc(2026, 10, 14) - timedelta(hours=6)
    source = FakeSource(kind=SourceKind.HN, candidates=[make_candidate(source_id=99)])
    result = await source.collect(make_source_config(id=7, kind=SourceKind.HN), since)
    assert result.source_id == 7
    assert [c.source_id for c in result.candidates] == [7]
    assert source.calls == [(7, since)]


async def test_fake_source_canned_result_and_error() -> None:
    canned = SourceResult(source_id=3, error="timeout")
    source = FakeSource(results={3: canned}, error="boom")
    assert await source.collect(make_source_config(id=3), utc(2026, 10, 14)) is canned
    other = await source.collect(make_source_config(id=4), utc(2026, 10, 14))
    assert (other.error, other.candidates) == ("boom", [])


async def test_fake_judge_defaults_overrides_failures() -> None:
    special = Verdict(item_id=2, signal=9, junk=False, category=Category.MODEL_RELEASE)
    judge = FakeJudge(verdicts={2: special}, fail_ids={3}, match_first_candidate=True)
    first = make_review_input(item_id=1).model_copy(
        update={"story_candidates": [StoryRef(id=40, headline="H")]}
    )
    inputs = [first, make_review_input(item_id=2), make_review_input(item_id=3)]
    result = await judge.review(inputs)
    assert [v.item_id for v in result.verdicts] == [1, 2]
    assert result.verdicts[0].signal == 6
    assert result.verdicts[0].same_story_id == 40
    assert result.verdicts[1] is special
    assert result.failed_item_ids == [3]
    assert result.usage[0].purpose is Purpose.REVIEW
    assert result.usage[0].input_tokens == 300
    assert judge.worst_case_cost(inputs) == pytest.approx(0.003)
    assert judge.calls == [inputs]


async def test_fake_judge_empty_batch_has_no_usage() -> None:
    result = await FakeJudge().review([])
    assert result.verdicts == [] and result.usage == []


async def test_fake_writer_stories_and_briefing() -> None:
    writer = FakeWriter(fail_story_ids={2})
    inputs = [
        WriteInput(story_id=1, headline="One", texts=["t"], include_why=True),
        WriteInput(story_id=2, headline="Two", texts=["t"], include_why=True),
        WriteInput(story_id=3, headline="Three", texts=["t"], include_why=False),
    ]
    texts, usage = await writer.write_stories(inputs)
    assert [(t.story_id, t.summary, t.why_it_matters) for t in texts] == [
        (1, "Summary: One", "Why: One"),
        (3, "Summary: Three", None),
    ]
    assert usage[0].purpose is Purpose.WRITE
    assert writer.worst_case_cost_stories(inputs) == pytest.approx(0.003)

    story = BriefingStory(
        story_id=1, headline="One", category=Category.RESEARCH, summary="s", url="https://a.test"
    )
    body, usage = await writer.write_briefing([story], "Wednesday 14 October")
    assert body == "Today's briefing."
    assert usage[0].purpose is Purpose.BRIEFING
    assert writer.briefing_calls == [([story], "Wednesday 14 October")]
    assert await writer.write_briefing([], "x") == (None, [])
    assert writer.worst_case_cost_briefing([]) == 0.0
