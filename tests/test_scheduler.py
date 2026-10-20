import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.pipeline import NarrationOutcome, PipelineContext, RunReport
from ai_newsdesk.runtime_settings import update_runtime_settings
from ai_newsdesk.scheduler import (
    NARRATION_MAX_ATTEMPTS,
    NARRATION_RETRY_AFTER,
    Scheduler,
    briefing_due,
    latest_slot,
    next_slot,
    parse_hhmm,
    slot_times,
)
from ai_newsdesk.store.records import BriefingAudioRecord
from ai_newsdesk.store.store import Store
from tests.pipeline_ctx import make_ctx
from tests.pipeline_helpers import MutableClock

LONDON = ZoneInfo("Europe/London")


class Recorder:
    def __init__(self) -> None:
        self.runs: list[str] = []
        self.briefings: list[date] = []

    async def run(self, ctx: PipelineContext, trigger: str) -> RunReport:
        self.runs.append(trigger)
        return RunReport(run_id=len(self.runs))

    async def briefing(self, ctx: PipelineContext, day: date) -> bool:
        self.briefings.append(day)
        return True


def scheduler_for(store: Store, clock: MutableClock, rec: Recorder, **kw: object) -> Scheduler:
    return Scheduler(make_ctx(store, clock), run_fn=rec.run, briefing_fn=rec.briefing, **kw)  # type: ignore[arg-type]


def test_slot_times() -> None:
    assert slot_times(4) == [time(0), time(6), time(12), time(18)]
    assert len(slot_times(24)) == 24
    assert slot_times(1) == [time(0)]
    assert slot_times(5) == slot_times(4)  # invalid value falls back to the default


def test_latest_slot_in_summer_and_winter_time() -> None:
    # 12:05 BST -> 12:00 BST = 11:00 UTC
    assert latest_slot(datetime(2026, 10, 14, 11, 5, tzinfo=UTC), 4, LONDON) == datetime(
        2026, 10, 14, 11, 0, tzinfo=UTC
    )
    # Clocks went back on 25 Oct 2026: 07:00 GMT -> 06:00 GMT = 06:00 UTC
    assert latest_slot(datetime(2026, 10, 25, 7, 0, tzinfo=UTC), 4, LONDON) == datetime(
        2026, 10, 25, 6, 0, tzinfo=UTC
    )
    # 00:30 BST -> today's 00:00 BST = 23:00 UTC the day before
    assert latest_slot(datetime(2026, 10, 13, 23, 30, tzinfo=UTC), 4, LONDON) == datetime(
        2026, 10, 13, 23, 0, tzinfo=UTC
    )


def test_next_slot() -> None:
    # 20:00 BST -> next 00:00 BST on the 15th = 23:00 UTC on the 14th
    assert next_slot(datetime(2026, 10, 14, 19, 0, tzinfo=UTC), 4, LONDON) == datetime(
        2026, 10, 14, 23, 0, tzinfo=UTC
    )


def test_briefing_due_and_parse() -> None:
    assert parse_hhmm("07:30") == time(7, 30)
    assert parse_hhmm("nonsense") == time(7, 0)
    assert briefing_due(datetime(2026, 10, 14, 5, 59, tzinfo=UTC), "07:00", LONDON) is None
    assert briefing_due(datetime(2026, 10, 14, 6, 1, tzinfo=UTC), "07:00", LONDON) == date(
        2026, 10, 14
    )


async def test_runs_once_per_new_slot_not_at_startup(store: Store) -> None:
    clock = MutableClock(datetime(2026, 10, 14, 11, 5, tzinfo=UTC))
    rec = Recorder()
    sched = scheduler_for(store, clock, rec)
    await sched.tick()
    assert rec.runs == []
    clock.advance(minutes=30)
    await sched.tick()
    assert rec.runs == []
    clock.advance(hours=5, minutes=30)  # 18:05 BST
    await sched.tick()
    await sched.tick()
    assert rec.runs == ["schedule"]
    assert sched.next_run_at == datetime(2026, 10, 14, 23, 0, tzinfo=UTC)


async def test_rereads_runs_per_day_each_tick(store: Store) -> None:
    clock = MutableClock(datetime(2026, 10, 14, 11, 5, tzinfo=UTC))
    rec = Recorder()
    sched = scheduler_for(store, clock, rec)
    await sched.tick()
    await store.set_setting("runs_per_day", 24)
    clock.advance(hours=1)  # 13:05 BST: a new hourly slot
    await sched.tick()
    assert rec.runs == ["schedule"]


async def test_briefing_once_per_day_after_briefing_time(store: Store) -> None:
    clock = MutableClock(datetime(2026, 10, 14, 5, 0, tzinfo=UTC))  # 06:00 BST
    rec = Recorder()
    sched = scheduler_for(store, clock, rec)
    await sched.tick()
    assert rec.briefings == []
    clock.advance(hours=1, minutes=1)  # 07:01 BST
    await sched.tick()
    await sched.tick()
    assert rec.briefings == [date(2026, 10, 14)]


async def test_existing_briefing_is_not_rewritten(store: Store) -> None:
    now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    await store.save_briefing(date(2026, 10, 14), "claude-haiku-5-5", "Body", [], now)
    rec = Recorder()
    await scheduler_for(store, MutableClock(now), rec).tick()
    assert rec.briefings == []


async def test_a_tick_waiting_behind_write_it_now_does_not_write_a_second_briefing(
    store: Store,
) -> None:
    """Write it now holds the lock; a tick (no briefing yet) waits for it. When the manual
    write finishes, the tick finds the briefing and writes nothing (no attempt counted)."""
    now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    day = date(2026, 10, 14)
    rec = Recorder()
    sched = scheduler_for(store, MutableClock(now), rec)
    release = asyncio.Event()

    async def write_it_now() -> None:
        async with sched.lock:
            await release.wait()
            await store.save_briefing(day, "m", "manual body", [], now)

    manual = asyncio.create_task(write_it_now())
    await asyncio.sleep(0)
    tick = asyncio.create_task(sched.tick())
    await asyncio.sleep(0.05)
    assert not tick.done()  # waiting for the lock
    release.set()
    await manual
    await tick
    assert rec.briefings == []
    briefing = await store.get_briefing(day)
    assert briefing is not None and briefing.body == "manual body"
    assert sched.briefing_status(day).tries == 0
    await sched.tick()
    assert rec.briefings == []


async def test_run_now_never_overlaps(store: Store) -> None:
    rec = Recorder()
    sched = scheduler_for(store, MutableClock(datetime(2026, 10, 14, 9, 0, tzinfo=UTC)), rec)
    async with sched.lock:
        assert await sched.run_now("manual") is None
    report = await sched.run_now("manual")
    assert report is not None
    assert rec.runs == ["manual"]


async def test_start_stop_and_cancel_mid_job(store: Store) -> None:
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def hang(ctx: PipelineContext) -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    clock = MutableClock(datetime(2026, 10, 14, 9, 0, tzinfo=UTC))  # after 03:30 local
    sched = scheduler_for(store, clock, Recorder(), nightly_fn=hang, tick_seconds=0.01)
    sched.start()
    await asyncio.wait_for(started.wait(), 2)
    assert sched.alive
    assert sched.last_tick_at == clock.now()
    await asyncio.wait_for(sched.stop(), 2)
    assert not sched.alive
    assert cancelled == [True]
    assert not sched.lock.locked()


class FlakyBriefing:
    """Returns the scripted results in order (an Exception is raised), then True."""

    def __init__(self, *results: bool | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[datetime, date]] = []

    async def __call__(self, ctx: PipelineContext, day: date) -> bool:
        self.calls.append((ctx.clock.now(), day))
        result = self.results.pop(0) if self.results else True
        if isinstance(result, Exception):
            raise result
        return result


def briefing_scheduler(store: Store, clock: FixedClock, fn: FlakyBriefing) -> Scheduler:
    return Scheduler(make_ctx(store, clock), run_fn=Recorder().run, briefing_fn=fn)


async def test_failed_briefing_is_retried_after_30_minutes_until_it_succeeds(
    store: Store,
) -> None:
    clock = FixedClock(datetime(2026, 10, 14, 6, 0, tzinfo=UTC))  # 07:00 BST
    fn = FlakyBriefing(False, RuntimeError("writer down"))
    sched = briefing_scheduler(store, clock, fn)
    await sched.tick()  # 07:00 attempt 1: False
    clock.advance(timedelta(minutes=29))
    await sched.tick()  # 07:29: too soon
    assert len(fn.calls) == 1
    clock.advance(timedelta(minutes=1))
    await sched.tick()  # 07:30 attempt 2: raises, but the scheduler carries on
    clock.advance(timedelta(minutes=30))
    await sched.tick()  # 08:00 attempt 3: True
    clock.advance(timedelta(minutes=30))
    await sched.tick()  # done for the day
    assert [at.astimezone(LONDON).time() for at, _ in fn.calls] == [
        time(7, 0),
        time(7, 30),
        time(8, 0),
    ]


async def test_briefing_attempts_are_capped_per_day_and_reset_next_day(store: Store) -> None:
    clock = FixedClock(datetime(2026, 10, 14, 6, 0, tzinfo=UTC))  # 07:00 BST
    fn = FlakyBriefing(*([False] * 7))
    sched = briefing_scheduler(store, clock, fn)
    for _ in range(12):  # every 30 minutes until 12:30 BST
        await sched.tick()
        clock.advance(timedelta(minutes=30))
    assert len(fn.calls) == 6
    assert {day for _, day in fn.calls} == {date(2026, 10, 14)}
    clock.set(datetime(2026, 10, 15, 5, 59, tzinfo=UTC))  # 06:59 BST next day: not yet due
    await sched.tick()
    assert len(fn.calls) == 6
    clock.advance(timedelta(minutes=1))
    await sched.tick()  # 07:00 BST: a fresh day, attempt 1 (False, the 7th scripted)
    clock.advance(timedelta(minutes=30))
    await sched.tick()  # 07:30: attempt 2 succeeds
    assert [day for _, day in fn.calls[6:]] == [date(2026, 10, 15)] * 2


async def test_a_scheduled_run_waits_for_the_lock(store: Store) -> None:
    """A Test or (stage 4) a comparison may hold the lock: the slot's run waits, not skips."""
    rec = Recorder()
    sched = scheduler_for(
        store, MutableClock(datetime(2026, 10, 14, 9, 0, tzinfo=UTC)), rec, schedule_wait_s=5.0
    )
    await sched.lock.acquire()
    waiting = asyncio.create_task(sched.run_now("schedule"))
    await asyncio.sleep(0.05)
    assert not waiting.done() and rec.runs == []
    sched.lock.release()
    report = await asyncio.wait_for(waiting, 1)
    assert report is not None and rec.runs == ["schedule"]
    assert not sched.lock.locked()


async def test_a_scheduled_run_skips_after_waiting_too_long(store: Store, caplog: object) -> None:
    import logging

    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)
    rec = Recorder()
    sched = scheduler_for(
        store, MutableClock(datetime(2026, 10, 14, 9, 0, tzinfo=UTC)), rec, schedule_wait_s=0.05
    )
    async with sched.lock:
        with caplog.at_level(logging.WARNING):
            assert await sched.run_now("schedule") is None
    assert rec.runs == []
    assert "schedule run skipped" in caplog.text
    assert not sched.lock.locked()


async def test_a_manual_run_still_reports_busy_at_once(store: Store) -> None:
    rec = Recorder()
    sched = scheduler_for(
        store, MutableClock(datetime(2026, 10, 14, 9, 0, tzinfo=UTC)), rec, schedule_wait_s=5.0
    )
    async with sched.lock:
        assert await asyncio.wait_for(sched.run_now("manual"), 0.5) is None
    assert rec.runs == []


def test_the_default_schedule_wait_is_20_minutes() -> None:
    from ai_newsdesk.scheduler import SCHEDULE_LOCK_WAIT_S

    assert SCHEDULE_LOCK_WAIT_S == 20 * 60


async def test_a_scheduled_run_after_a_manual_run_while_waiting_is_skipped(
    store: Store, caplog: object
) -> None:
    """Review R4: a full collection run just happened, so the slot's run would find little."""
    import logging

    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)
    release = asyncio.Event()
    runs: list[str] = []

    async def slow_run(ctx: PipelineContext, trigger: str) -> RunReport:
        runs.append(trigger)
        if trigger == "manual":
            await release.wait()
        return RunReport(run_id=len(runs))

    sched = Scheduler(
        make_ctx(store, MutableClock(datetime(2026, 10, 14, 9, 0, tzinfo=UTC))),
        run_fn=slow_run,
        schedule_wait_s=5.0,
    )
    manual = asyncio.create_task(sched.run_now("manual"))
    await asyncio.sleep(0.01)
    scheduled = asyncio.create_task(sched.run_now("schedule"))
    await asyncio.sleep(0.01)
    release.set()
    with caplog.at_level(logging.INFO):
        assert await asyncio.wait_for(manual, 1) is not None
        assert await asyncio.wait_for(scheduled, 1) is None
    assert runs == ["manual"]
    assert "a run finished while waiting; schedule run skipped" in caplog.text
    assert not sched.lock.locked()


async def test_a_scheduled_run_after_a_non_run_holder_still_runs(store: Store) -> None:
    """A Settings Test or a comparison holds the lock without collecting: the slot still runs."""
    rec = Recorder()
    sched = scheduler_for(
        store, MutableClock(datetime(2026, 10, 14, 9, 0, tzinfo=UTC)), rec, schedule_wait_s=5.0
    )
    async with sched.lock:  # e.g. the Test button
        scheduled = asyncio.create_task(sched.run_now("schedule"))
        await asyncio.sleep(0.01)
    assert await asyncio.wait_for(scheduled, 1) is not None
    assert rec.runs == ["schedule"]


async def test_shutdown_during_the_scheduled_wait(store: Store) -> None:
    """Review N6: stop() returns, nothing runs, and the holder keeps the lock."""
    rec = Recorder()
    clock = MutableClock(datetime(2026, 10, 14, 9, 0, tzinfo=UTC))
    sched = scheduler_for(store, clock, rec, schedule_wait_s=60.0, tick_seconds=0.01)
    sched._last_slot = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)  # a slot is due
    await sched.lock.acquire()  # the holder (e.g. a Test)
    sched.start()
    await asyncio.sleep(0.05)  # the tick is now waiting for the lock
    await asyncio.wait_for(sched.stop(), 1)
    assert rec.runs == []
    assert sched.lock.locked()  # still ours, not taken by the cancelled waiter
    sched.lock.release()
    assert not sched.lock.locked()


async def test_briefing_status_reports_tries_and_give_up(store: Store) -> None:
    clock = FixedClock(datetime(2026, 10, 14, 6, 0, tzinfo=UTC))  # 07:00 BST
    fn = FlakyBriefing(*([False] * 6))
    sched = briefing_scheduler(store, clock, fn)
    day = date(2026, 10, 14)
    assert sched.briefing_status(day).tries == 0
    await sched.tick()
    st = sched.briefing_status(day)
    assert (st.tries, st.max_tries, st.gave_up) == (1, 6, False)
    for _ in range(5):
        clock.advance(timedelta(minutes=30))
        await sched.tick()
    assert sched.briefing_status(day).gave_up is True
    assert sched.briefing_status(date(2026, 10, 15)).tries == 0  # another day: no attempts
    assert sched.briefing_status(day).tries == 6  # reading never resets the state


def _audio_row(day: date, at: datetime) -> BriefingAudioRecord:
    return BriefingAudioRecord(
        date=day,
        voice_id="v",
        voice_name="V",
        model_id="eleven_v4",
        characters=1,
        bytes=1,
        file=f"briefing-{day.isoformat()}-aaaaaaaa.mp3",
        created_at=at,
    )


class ScriptedNarration:
    """Returns the scripted outcomes in order (an Exception is raised), then DONE; DONE saves
    the day's audio row (and a briefing, when the fake briefing job left none), as the real
    job does. Records whether the scheduler lock was held for each call."""

    def __init__(self, sched_lock: list[asyncio.Lock], *results: NarrationOutcome | Exception):
        self.results = list(results)
        self.calls: list[tuple[datetime, date, bool]] = []
        self._lock = sched_lock

    async def __call__(self, ctx: PipelineContext, day: date) -> NarrationOutcome:
        self.calls.append((ctx.clock.now(), day, self._lock[0].locked()))
        result = self.results.pop(0) if self.results else NarrationOutcome.DONE
        if isinstance(result, Exception):
            raise result
        if result is NarrationOutcome.DONE:
            if await ctx.store.get_briefing(day) is None:
                await ctx.store.save_briefing(day, "m", "body", [], ctx.clock.now())
            await ctx.store.save_briefing_audio(_audio_row(day, ctx.clock.now()))
        return result


def narration_scheduler(
    store: Store,
    clock: FixedClock,
    *results: NarrationOutcome | Exception,
    briefing: FlakyBriefing | None = None,
) -> tuple[Scheduler, ScriptedNarration]:
    holder: list[asyncio.Lock] = []
    fn = ScriptedNarration(holder, *results)
    sched = Scheduler(
        make_ctx(store, clock),
        run_fn=Recorder().run,
        briefing_fn=briefing or FlakyBriefing(),
        narration_fn=fn,
    )
    holder.append(sched.lock)
    return sched, fn


BRIEF_AT = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)  # 07:00 BST
D = date(2026, 10, 14)


async def _ticks(sched: Scheduler, clock: FixedClock, n: int, every_min: int = 30) -> None:
    for _ in range(n):
        await sched.tick()
        clock.advance(timedelta(minutes=every_min))


async def test_narration_follows_the_briefing_under_the_lock_and_done_ends_the_day(
    store: Store,
) -> None:
    clock = FixedClock(BRIEF_AT)
    sched, fn = narration_scheduler(store, clock)
    await _ticks(sched, clock, 4)
    assert [(day, locked) for _, day, locked in fn.calls] == [(D, True)]
    st = sched.narration_status(D)
    assert (st.tries, st.max_tries, st.gave_up, st.last_outcome) == (
        0,
        3,
        False,
        NarrationOutcome.DONE,
    )


async def test_no_narration_before_the_briefing_exists(store: Store) -> None:
    clock = FixedClock(BRIEF_AT)
    sched, fn = narration_scheduler(store, clock, briefing=FlakyBriefing(False))
    await sched.tick()  # the briefing failed: nothing to narrate
    assert fn.calls == []
    clock.advance(timedelta(minutes=30))
    await sched.tick()  # the retry wrote it: narration follows in the same tick
    assert len(fn.calls) == 1


async def test_failed_narration_is_retried_every_30_minutes_three_times_a_day(
    store: Store,
) -> None:
    clock = FixedClock(BRIEF_AT)
    failed = NarrationOutcome.FAILED
    sched, fn = narration_scheduler(store, clock, *([failed] * 4))
    await sched.tick()  # 07:00 attempt 1
    clock.advance(timedelta(minutes=29))
    await sched.tick()  # too soon
    assert len(fn.calls) == 1
    assert sched.narration_status(D).tries == 1
    clock.advance(timedelta(minutes=1))
    await _ticks(sched, clock, 6)  # 07:30 attempt 2, 08:00 attempt 3, then it gives up
    assert [at.astimezone(LONDON).time() for at, _, _ in fn.calls] == [
        time(7, 0),
        time(7, 30),
        time(8, 0),
    ]
    st = sched.narration_status(D)
    assert (st.tries, st.gave_up, st.last_outcome) == (3, True, NarrationOutcome.FAILED)
    clock.set(datetime(2026, 10, 15, 6, 0, tzinfo=UTC))  # next day: fresh attempts
    await _ticks(sched, clock, 2)
    assert [day for _, day, _ in fn.calls[3:]] == [date(2026, 10, 15)] * 2
    assert sched.narration_status(date(2026, 10, 15)).tries == 1
    assert sched.narration_status(D).tries == 0  # only the current day is remembered


@pytest.mark.parametrize("outcome", [NarrationOutcome.OFF, NarrationOutcome.NO_KEY])
async def test_off_and_no_key_use_no_attempts_and_are_rechecked_next_tick(
    store: Store, outcome: NarrationOutcome
) -> None:
    clock = FixedClock(BRIEF_AT)
    sched, fn = narration_scheduler(store, clock, outcome, outcome, outcome, outcome)
    await _ticks(sched, clock, 4, every_min=1)
    st = sched.narration_status(D)
    assert (st.tries, st.gave_up, st.last_outcome) == (0, False, outcome)
    await _ticks(sched, clock, 2, every_min=1)  # switched on (or a key added): done today
    assert len(fn.calls) == 5
    assert sched.narration_status(D).last_outcome is NarrationOutcome.DONE


async def test_over_cap_stops_the_day_without_using_an_attempt(store: Store) -> None:
    clock = FixedClock(BRIEF_AT)
    sched, fn = narration_scheduler(store, clock, NarrationOutcome.OVER_CAP)
    await _ticks(sched, clock, 6)
    assert len(fn.calls) == 1
    st = sched.narration_status(D)
    assert (st.tries, st.gave_up, st.last_outcome) == (0, False, NarrationOutcome.OVER_CAP)
    clock.set(datetime(2026, 10, 15, 6, 0, tzinfo=UTC))
    await sched.tick()  # a new day tries again
    assert len(fn.calls) == 2


async def test_a_narration_exception_is_a_failed_attempt_and_leaves_the_briefing_alone(
    store: Store,
) -> None:
    clock = FixedClock(BRIEF_AT)
    briefing = FlakyBriefing()
    sched, fn = narration_scheduler(store, clock, RuntimeError("tts down"), briefing=briefing)
    await sched.tick()
    assert not sched.lock.locked()
    st = sched.narration_status(D)
    assert (st.tries, st.last_outcome) == (1, NarrationOutcome.FAILED)
    assert sched.briefing_status(D).tries == 1
    await _ticks(sched, clock, 3)
    assert len(briefing.calls) == 1  # the briefing is still done: never rewritten
    assert len(fn.calls) == 2  # the retry succeeded


async def test_existing_audio_means_no_narration(store: Store) -> None:
    clock = FixedClock(BRIEF_AT)
    await store.save_briefing(D, "m", "body", [], clock.now())
    await store.save_briefing_audio(_audio_row(D, clock.now()))
    sched, fn = narration_scheduler(store, clock)
    await _ticks(sched, clock, 3)
    assert fn.calls == []


async def test_a_held_lock_delays_narration_without_using_an_attempt(store: Store) -> None:
    clock = FixedClock(BRIEF_AT)
    await store.save_briefing(D, "m", "body", [], clock.now())  # the briefing is done already
    sched, fn = narration_scheduler(store, clock, NarrationOutcome.FAILED)
    await sched.lock.acquire()
    try:
        await sched.tick()
    finally:
        sched.lock.release()
    assert fn.calls == []
    assert sched.narration_status(D).tries == 0
    clock.advance(timedelta(minutes=1))
    await sched.tick()
    assert len(fn.calls) == 1


def test_the_narration_retry_constants() -> None:
    assert NARRATION_MAX_ATTEMPTS == 3
    assert timedelta(minutes=30) == NARRATION_RETRY_AFTER


async def test_audio_lost_to_a_rewrite_is_made_again(store: Store) -> None:
    clock = FixedClock(BRIEF_AT)
    sched, fn = narration_scheduler(store, clock)
    await _ticks(sched, clock, 2)
    assert len(fn.calls) == 1  # DONE: the row exists, so no more calls
    await store.save_briefing(D, "m", "rewritten", [], clock.now())  # drops the audio row
    await sched.tick()
    assert len(fn.calls) == 2
    assert await store.get_briefing_audio(D) is not None


async def test_a_failed_remake_after_a_rewrite_gets_the_days_remaining_attempts(
    store: Store,
) -> None:
    clock = FixedClock(BRIEF_AT)
    failed = NarrationOutcome.FAILED
    sched, fn = narration_scheduler(store, clock, failed, NarrationOutcome.DONE, failed, failed)
    await _ticks(sched, clock, 2)  # 07:00 FAILED (1), 07:30 DONE
    await store.save_briefing(D, "m", "rewritten", [], clock.now())
    await _ticks(sched, clock, 6)  # FAILED (2), FAILED (3), then it gives up
    assert len(fn.calls) == 4
    assert sched.narration_status(D).gave_up is True


class CountingLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.acquired = 0

    async def acquire(self) -> Literal[True]:
        self.acquired += 1
        return await super().acquire()


async def _no_key() -> bool:
    return False


@pytest.mark.parametrize("why", ["off", "no_key"])
async def test_no_lock_is_taken_when_narration_cannot_run(store: Store, why: str) -> None:
    clock = FixedClock(BRIEF_AT)
    await store.save_briefing(D, "m", "body", [], clock.now())  # no briefing job, no lock
    sched, fn = narration_scheduler(store, clock)
    if why == "off":
        await update_runtime_settings(store, narration_enabled=False)
    else:
        sched.ctx.has_narration_key = _no_key
    lock = CountingLock()
    sched.lock = lock
    await _ticks(sched, clock, 3, every_min=1)
    assert lock.acquired == 0
    assert fn.calls == []
    expected = NarrationOutcome.OFF if why == "off" else NarrationOutcome.NO_KEY
    st = sched.narration_status(D)
    assert (st.tries, st.last_outcome) == (0, expected)


async def test_on_demand_audio_is_never_made_by_the_scheduler(store: Store) -> None:
    # feedback-5: "Only when I press Make audio".
    await update_runtime_settings(store, narration_on_demand=True)
    clock = FixedClock(BRIEF_AT)
    sched, fn = narration_scheduler(store, clock)
    await _ticks(sched, clock, 6)
    assert fn.calls == []
    assert sched.narration_status(D).tries == 0
