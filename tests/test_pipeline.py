import asyncio
from collections.abc import Coroutine
from datetime import UTC, date, datetime
from typing import Any

import pytest

from ai_newsdesk import pipeline
from ai_newsdesk.models import (
    BriefingStory,
    Category,
    JudgeResult,
    ReviewInput,
    SourceConfig,
    SourceKind,
    SourceResult,
    StoryText,
    Trust,
    Usage,
    Verdict,
    WriteInput,
)
from ai_newsdesk.pipeline import COUNT_KEYS, run_briefing, run_once, stored_signal
from ai_newsdesk.protocols import Source
from ai_newsdesk.store.store import Store
from tests.pipeline_ctx import (
    MODEL,
    RecordingWriter,
    ScriptedJudge,
    ScriptedSource,
    candidate,
    make_ctx,
)
from tests.pipeline_helpers import MutableClock, add_source

NOW = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)
LAB_TITLE = "Anthropic releases Claude Haiku 5.5 with faster tool use"
JUNK_TITLE = "Ten AI prompts that will change your life"
MYSTERY_TITLE = "Mystery lab teases new agent"


async def two_source_setup(store: Store) -> tuple[ScriptedSource, ScriptedSource, int, int]:
    lab = await add_source(store, "Lab blog", kind=SourceKind.FEED, trust=Trust.PRIMARY)
    search = await add_source(
        store,
        "Model news search",
        kind=SourceKind.SEARCH,
        trust=Trust.DISCOVERY,
        config={"query": "new AI model release"},
    )
    feeds, searches = ScriptedSource(SourceKind.FEED), ScriptedSource(SourceKind.SEARCH)
    feeds.by_source[lab.id] = [candidate(lab.id, LAB_TITLE, "https://example.com/haiku")]
    searches.by_source[search.id] = [
        candidate(search.id, JUNK_TITLE, "https://example.net/prompts")
    ]
    return feeds, searches, lab.id, search.id


def scripted_judge(**kw: object) -> ScriptedJudge:
    return ScriptedJudge(
        by_title={
            "Claude Haiku 5.5": (8, False, Category.MODEL_RELEASE),
            "AI prompts": (2, True, Category.OTHER),
            "GPT-5.5": (8, False, Category.MODEL_RELEASE),
        },
        **kw,  # type: ignore[arg-type]
    )


def test_stored_signal() -> None:
    v = Verdict(item_id=1, signal=8, junk=False, category=Category.RESEARCH)
    assert stored_signal(v, 0.5) == 4
    assert stored_signal(v, 2.0) == 10
    assert stored_signal(v.model_copy(update={"signal": 7}), 0.3) == 2
    assert stored_signal(v.model_copy(update={"junk": True}), 2.0) == 0


async def test_happy_path(store: Store) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    clock = MutableClock(NOW)
    writer = RecordingWriter()
    ctx = make_ctx(
        store,
        clock,
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
        writer=writer,
    )
    report = await run_once(ctx, "test")
    assert report.error is None
    assert set(COUNT_KEYS) <= set(report.counts)
    c = report.counts
    assert (c["sources"], c["sources_failed"], c["collected"], c["new"]) == (2, 0, 2, 2)
    assert (c["queued"], c["dropped"], c["reviewed"], c["review_failed"]) == (2, 0, 2, 0)
    assert c["written"] == 1
    assert report.cost_usd > 0
    stories = await store.today_stories(NOW, 4)
    assert [s.headline for s in stories] == [LAB_TITLE]
    assert stories[0].summary == f"Summary: {LAB_TITLE}"
    assert stories[0].why_it_matters == "It matters."
    assert await store.month_spend(NOW, "Europe/London") == pytest.approx(report.cost_usd)


async def test_why_it_matters_toggle_off(store: Store) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    await store.set_setting("why_it_matters", False)
    writer = RecordingWriter()
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
        writer=writer,
    )
    await run_once(ctx, "test")
    assert writer.story_calls
    assert all(not w.include_why for call in writer.story_calls for w in call)


async def test_one_source_raising_does_not_fail_the_run(store: Store) -> None:
    feeds, searches, lab_id, _ = await two_source_setup(store)
    feeds.by_source[lab_id] = RuntimeError("feed exploded")
    judge = scripted_judge()
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=judge,
    )
    report = await run_once(ctx, "test")
    assert report.error is None
    assert report.counts["sources_failed"] == 1
    assert report.counts["reviewed"] == 1
    assert [i.title for i in judge.calls[0]] == [JUNK_TITLE]


async def test_slow_source_times_out(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    feeds, searches, lab_id, _ = await two_source_setup(store)
    feeds.delays[lab_id] = 10.0
    monkeypatch.setattr(pipeline, "SOURCE_TIMEOUT_S", 0.05)
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
    )
    report = await run_once(ctx, "test")
    assert report.error is None
    assert report.counts["sources_failed"] == 1
    assert report.counts["reviewed"] == 1


async def test_source_kind_without_adapter_is_a_source_failure(store: Store) -> None:
    feeds, _, _, _ = await two_source_setup(store)
    ctx = make_ctx(
        store, MutableClock(NOW), sources={SourceKind.FEED: feeds}, judge=scripted_judge()
    )
    report = await run_once(ctx, "test")
    assert report.error is None
    assert report.counts["sources_failed"] == 1


async def test_budget_exhausted_no_judge_call_items_stay_queued(store: Store) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    await store.set_setting("monthly_budget_usd", 0.0)
    judge = scripted_judge()
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=judge,
    )
    report = await run_once(ctx, "test")
    assert report.error is None
    assert judge.calls == []
    assert report.counts["budget_skipped"] == 2
    assert report.counts["reviewed"] == 0
    assert len(await store.queued_review_inputs(10, NOW)) == 2
    assert report.cost_usd == 0.0


async def test_daily_review_cap_limits_reviews(store: Store) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    await store.set_setting("daily_review_cap", 1)
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
    )
    report = await run_once(ctx, "test")
    assert report.counts["reviewed"] == 1
    assert len(await store.queued_review_inputs(10, NOW)) == 1


async def test_items_with_no_verdict_stay_visible(store: Store) -> None:
    lab = await add_source(store, "Lab blog")
    feeds = ScriptedSource(SourceKind.FEED)
    feeds.by_source[lab.id] = [
        candidate(lab.id, MYSTERY_TITLE, "https://example.com/mystery"),
        candidate(lab.id, LAB_TITLE, "https://example.com/haiku"),
    ]
    judge = scripted_judge(fail_titles=frozenset({"Mystery"}))
    ctx = make_ctx(store, MutableClock(NOW), sources={SourceKind.FEED: feeds}, judge=judge)
    report = await run_once(ctx, "test")
    assert report.error is None
    assert report.counts["review_failed"] == 1
    assert report.counts["reviewed"] == 1
    assert len(judge.calls) == 1  # no retry within the run (R2)
    shown_urls = {src.url for s in await store.today_stories(NOW, 4) for src in s.sources}
    assert "https://example.com/mystery" in shown_urls
    # queued_review_inputs offers the failed item again, for the next run.
    assert [i.title for i in await store.queued_review_inputs(10, NOW)] == [MYSTERY_TITLE]


async def test_failed_item_is_retried_next_run_and_keeps_its_story(store: Store) -> None:
    lab = await add_source(store, "Lab blog")
    feeds = ScriptedSource(SourceKind.FEED)
    feeds.by_source[lab.id] = [candidate(lab.id, MYSTERY_TITLE, "https://example.com/mystery")]
    clock = MutableClock(NOW)
    failing = scripted_judge(fail_titles=frozenset({"Mystery"}))
    await run_once(make_ctx(store, clock, sources={SourceKind.FEED: feeds}, judge=failing), "test")
    [before] = await store.today_stories(clock.now(), 4)

    clock.advance(hours=6)
    judge = scripted_judge()
    report = await run_once(
        make_ctx(store, clock, sources={SourceKind.FEED: feeds}, judge=judge), "test"
    )
    assert report.error is None
    assert report.counts["new"] == 0
    assert (report.counts["reviewed"], report.counts["review_failed"]) == (1, 0)
    assert [i.title for i in judge.calls[0]] == [MYSTERY_TITLE]
    [after] = await store.today_stories(clock.now(), 4)
    assert after.id == before.id
    assert after.score == 5.0
    assert await store.queued_review_inputs(10, clock.now()) == []


async def test_item_failing_twice_is_not_offered_again_but_stays_visible(store: Store) -> None:
    lab = await add_source(store, "Lab blog")
    feeds = ScriptedSource(SourceKind.FEED)
    feeds.by_source[lab.id] = [candidate(lab.id, MYSTERY_TITLE, "https://example.com/mystery")]
    clock = MutableClock(NOW)
    judge = scripted_judge(fail_titles=frozenset({"Mystery"}))
    ctx = make_ctx(store, clock, sources={SourceKind.FEED: feeds}, judge=judge)
    for _ in range(3):
        await run_once(ctx, "test")
        clock.advance(hours=6)
    assert len(judge.calls) == 2  # attempts 1 and 2; MAX_REVIEW_ATTEMPTS stops the third
    assert len(await store.today_stories(clock.now(), 4)) == 1


async def test_near_duplicate_in_later_run_joins_story(store: Store) -> None:
    lab = await add_source(store, "Lab blog")
    person = await add_source(store, "Researcher", trust=Trust.TRUSTED)
    feeds = ScriptedSource(SourceKind.FEED)
    feeds.by_source[lab.id] = [
        candidate(lab.id, "OpenAI launches GPT-5.5 for developers", "https://example.com/1")
    ]
    clock = MutableClock(NOW)
    judge = scripted_judge()
    ctx = make_ctx(store, clock, sources={SourceKind.FEED: feeds}, judge=judge)
    await run_once(ctx, "test")
    clock.advance(hours=1)
    feeds.by_source[person.id] = [
        candidate(person.id, "OpenAI Launches GPT-5.5 For Developers!", "https://example.org/2")
    ]
    report = await run_once(ctx, "test")
    assert report.error is None
    stories = await store.today_stories(clock.now(), 4)
    assert len(stories) == 1
    assert len(stories[0].sources) == 2
    assert stories[0].score == 8.5
    assert [s.id for s in judge.calls[1][0].story_candidates] == [stories[0].id]


async def test_blocklisted_item_is_dropped_before_review(store: Store) -> None:
    lab = await add_source(store, "Lab blog")
    await store.set_setting("blocklist", ["crypto"])
    feeds = ScriptedSource(SourceKind.FEED)
    feeds.by_source[lab.id] = [
        candidate(lab.id, "AI crypto airdrop for agents", "https://example.com/airdrop"),
        candidate(lab.id, LAB_TITLE, "https://example.com/haiku"),
    ]
    judge = scripted_judge()
    ctx = make_ctx(store, MutableClock(NOW), sources={SourceKind.FEED: feeds}, judge=judge)
    report = await run_once(ctx, "test")
    assert report.counts["dropped"] == 1
    assert all("crypto" not in i.title for call in judge.calls for i in call)


async def test_stage_crash_is_recorded_not_raised(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    feeds, searches, _, _ = await two_source_setup(store)

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("prefilter bug")

    monkeypatch.setattr(pipeline.prefilter, "decide", boom)
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
    )
    report = await run_once(ctx, "test")
    assert report.error is not None and "RuntimeError" in report.error


async def test_run_briefing_saves_briefing(store: Store) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    writer = RecordingWriter()
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
        writer=writer,
    )
    await run_once(ctx, "test")
    assert await run_briefing(ctx, date(2026, 10, 14)) is True
    assert len(writer.briefing_calls) == 1
    [story] = writer.briefing_calls[0]
    record = await store.get_briefing(date(2026, 10, 14))
    assert record is not None
    assert record.story_ids == [story.story_id]
    assert record.model == "claude-haiku-5-5"
    # R4 body format, as stored: plain text, "## " headings, blank lines, [text](story url) links.
    assert record.body.startswith("Briefing for Wednesday 14 October 2026\n\n## model_release\n\n")
    assert f"[{LAB_TITLE}](https://example.com/haiku)" in record.body


async def test_run_briefing_without_stories_or_budget(store: Store) -> None:
    writer = RecordingWriter()
    ctx = make_ctx(store, MutableClock(NOW), writer=writer)
    assert await run_briefing(ctx, date(2026, 10, 14)) is False
    feeds, searches, _, _ = await two_source_setup(store)
    ctx2 = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
        writer=writer,
    )
    await run_once(ctx2, "test")
    await store.set_setting("monthly_budget_usd", 0.0)
    assert await run_briefing(ctx2, date(2026, 10, 14)) is False
    assert writer.briefing_calls == []


async def test_hn_source_gets_runtime_min_points(store: Store) -> None:
    plain = await add_source(
        store, "Hacker News", kind=SourceKind.HN, trust=Trust.DISCOVERY, config={"queries": ["AI"]}
    )
    own = await add_source(
        store,
        "HN strict",
        kind=SourceKind.HN,
        trust=Trust.DISCOVERY,
        config={"queries": ["LLM"], "min_points": 300},
    )
    await store.set_setting("hn_min_points", 150)
    hn = ScriptedSource(SourceKind.HN)
    ctx = make_ctx(store, MutableClock(NOW), sources={SourceKind.HN: hn}, judge=scripted_judge())
    report = await run_once(ctx, "test")
    assert report.error is None
    seen = {s.id: s.config for s in hn.seen}
    assert seen[plain.id] == {"queries": ["AI"], "min_points": 150}
    assert seen[own.id] == {"queries": ["LLM"], "min_points": 300}
    stored = await store.get_source(plain.id)
    assert stored is not None and "min_points" not in stored.config  # injected, never saved


async def test_story_the_writer_leaves_out_is_marked_and_offered_again(store: Store) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    clock = MutableClock(NOW)
    writer = RecordingWriter(skip_headlines=frozenset({"Claude Haiku"}))
    ctx = make_ctx(
        store,
        clock,
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
        writer=writer,
    )
    report = await run_once(ctx, "test")
    assert (report.counts["written"], report.counts["write_failed"]) == (0, 1)
    [story] = await store.today_stories(clock.now(), 4)
    assert story.summary is None
    clock.advance(hours=6)
    await run_once(ctx, "test")
    assert [[w.story_id for w in call] for call in writer.story_calls] == [[story.id], [story.id]]
    clock.advance(hours=6)
    await run_once(ctx, "test")
    assert len(writer.story_calls) == 2  # MAX_TEXT_ATTEMPTS reached: no third attempt


async def test_same_name_sources_of_different_kinds_use_their_own_weight(store: Store) -> None:
    feed = await add_source(store, "Anthropic", kind=SourceKind.FEED, weight=1.6)
    search = await add_source(
        store,
        "Anthropic",
        kind=SourceKind.SEARCH,
        weight=0.8,
        config={"query": "anthropic"},
    )
    feeds, searches = ScriptedSource(SourceKind.FEED), ScriptedSource(SourceKind.SEARCH)
    feeds.by_source[feed.id] = [
        candidate(feed.id, "Alpha lab ships robot arm", "https://a.example/1")
    ]
    searches.by_source[search.id] = [
        candidate(search.id, "Zeta startup opens data centre", "https://b.example/2")
    ]
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
    )
    report = await run_once(ctx, "test")
    assert report.error is None
    scores = {s.headline: s.score for s in await store.today_stories(NOW, 4)}
    assert scores == {
        "Alpha lab ships robot arm": 8.0,  # 5 x 1.6
        "Zeta startup opens data centre": 4.0,  # 5 x 0.8
    }


class SourceDeletingJudge(ScriptedJudge):
    """Deletes the source of the first item in each batch while 'judging' (mid-run deletion)."""

    def __init__(self, store: Store, source_of: dict[str, int]) -> None:
        super().__init__()
        self.store = store
        self.source_of = source_of

    async def review(self, inputs):  # type: ignore[no-untyped-def]
        result = await super().review(inputs)
        await self.store.delete_source(self.source_of[inputs[0].title])
        return result


async def test_item_deleted_mid_run_does_not_discard_other_verdicts(store: Store) -> None:
    gone = await add_source(store, "Doomed blog")
    lab = await add_source(store, "Lab blog")
    feeds = ScriptedSource(SourceKind.FEED)
    feeds.by_source[gone.id] = [candidate(gone.id, MYSTERY_TITLE, "https://example.com/doomed")]
    feeds.by_source[lab.id] = [candidate(lab.id, LAB_TITLE, "https://example.com/haiku")]
    judge = SourceDeletingJudge(store, {MYSTERY_TITLE: gone.id, LAB_TITLE: lab.id})
    ctx = make_ctx(store, MutableClock(NOW), sources={SourceKind.FEED: feeds}, judge=judge)
    report = await run_once(ctx, "test")
    assert report.error is None
    assert judge.calls[0][0].title == MYSTERY_TITLE  # the deleted one comes first
    [story] = await store.today_stories(NOW, 4)
    assert story.headline == LAB_TITLE
    assert report.counts["reviewed"] == 1


async def test_items_stranded_by_a_crash_are_moved_on_next_run(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    clock = MutableClock(NOW)
    real_decide = pipeline.prefilter.decide

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("prefilter bug")

    monkeypatch.setattr(pipeline.prefilter, "decide", boom)
    sources: dict[SourceKind, Source] = {SourceKind.FEED: feeds, SourceKind.SEARCH: searches}
    first = await run_once(make_ctx(store, clock, sources=sources, judge=scripted_judge()), "t")
    assert first.error is not None and first.counts["new"] == 2

    monkeypatch.setattr(pipeline.prefilter, "decide", real_decide)
    clock.advance(hours=6)
    second = await run_once(make_ctx(store, clock, sources=sources, judge=scripted_judge()), "t")
    assert second.error is None
    assert second.counts["new"] == 0  # same URLs: deduped
    assert second.counts["recovered"] == 2
    assert (second.counts["queued"], second.counts["reviewed"]) == (2, 2)
    assert [s.headline for s in await store.today_stories(clock.now(), 4)] == [LAB_TITLE]


class BlockingSource(ScriptedSource):
    """Signals that collection started, then blocks until cancelled."""

    def __init__(self, kind: SourceKind) -> None:
        super().__init__(kind)
        self.started = asyncio.Event()

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_cancelled_run_is_finished_with_error_cancelled(store: Store) -> None:
    await add_source(store, "Lab blog")
    feeds = BlockingSource(SourceKind.FEED)
    ctx = make_ctx(store, MutableClock(NOW), sources={SourceKind.FEED: feeds})
    task = asyncio.create_task(run_once(ctx, "test"))
    await feeds.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [run] = await store.last_runs(1)
    assert run.finished_at is not None
    assert run.error == "cancelled"


class HangingJudge(ScriptedJudge):
    """Signals that the call started, then never answers."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def review(self, inputs: list[ReviewInput]) -> JudgeResult:
        self.calls.append(list(inputs))
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class HangingWriter(RecordingWriter):
    """Writes stories as usual (`hang_stories` off) and never answers the briefing."""

    def __init__(self, *, hang_stories: bool) -> None:
        super().__init__()
        self.hang_stories = hang_stories
        self.started = asyncio.Event()

    async def write_stories(self, inputs: list[WriteInput]) -> tuple[list[StoryText], list[Usage]]:
        if not self.hang_stories:
            return await super().write_stories(inputs)
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def write_briefing(
        self, stories: list[BriefingStory], date_label: str
    ) -> tuple[str | None, list[Usage]]:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def spend_rows(store: Store) -> list[tuple[object, ...]]:
    rows = await store._all("SELECT provider, model, purpose, cost_usd FROM spend ORDER BY id")
    return [tuple(r) for r in rows]


async def _cancel_when_started(started: asyncio.Event, coro: Coroutine[Any, Any, object]) -> None:
    task = asyncio.create_task(coro)
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_judge_call_cut_off_by_shutdown_is_recorded_at_its_worst_case(
    store: Store,
) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    judge = HangingJudge()
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=judge,
    )
    await _cancel_when_started(judge.started, run_once(ctx, "test"))
    worst = judge.worst_case_cost(judge.calls[0])
    assert await spend_rows(store) == [("anthropic", MODEL, "review", pytest.approx(worst))]
    [run] = await store.last_runs(1)
    assert run.error == "cancelled" and run.cost_usd == pytest.approx(worst)
    assert ctx.budget.remaining_month() == pytest.approx(5.0 - worst)


async def test_a_writer_call_cut_off_by_shutdown_is_recorded_at_its_worst_case(
    store: Store,
) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    writer = HangingWriter(hang_stories=True)
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
        writer=writer,
    )
    await _cancel_when_started(writer.started, run_once(ctx, "test"))
    rows = await spend_rows(store)
    assert [r[2] for r in rows] == ["review", "write"]
    assert rows[1] == ("anthropic", MODEL, "write", pytest.approx(0.001))  # one story's worst
    [run] = await store.last_runs(1)
    assert run.cost_usd == pytest.approx(sum(float(str(r[3])) for r in rows))


async def test_a_briefing_call_cut_off_by_shutdown_is_recorded_at_its_worst_case(
    store: Store,
) -> None:
    feeds, searches, _, _ = await two_source_setup(store)
    ctx = make_ctx(
        store,
        MutableClock(NOW),
        sources={SourceKind.FEED: feeds, SourceKind.SEARCH: searches},
        judge=scripted_judge(),
    )
    await run_once(ctx, "test")
    before = len(await spend_rows(store))
    writer = HangingWriter(hang_stories=False)
    ctx.writer = writer
    await _cancel_when_started(writer.started, run_briefing(ctx, date(2026, 10, 14)))
    rows = await spend_rows(store)
    assert rows[before:] == [("anthropic", MODEL, "briefing", pytest.approx(0.002))]
