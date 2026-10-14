"""In-process scheduler: collection runs on evenly spaced slots from 00:00 in TZ, the daily
briefing after briefing_time, and nightly upkeep after 03:30. Settings are re-read every tick,
and one asyncio.Lock stops any two jobs from overlapping."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ai_newsdesk import pipeline
from ai_newsdesk.pipeline import PipelineContext, RunReport
from ai_newsdesk.runtime_settings import load_runtime_settings

log = logging.getLogger(__name__)

VALID_RUNS_PER_DAY = (1, 2, 4, 6, 12, 24)
DEFAULT_RUNS_PER_DAY = 4
DEFAULT_BRIEFING_AT = time(7, 0)
NIGHTLY_AT = time(3, 30)

RunFn = Callable[[PipelineContext, str], Awaitable[RunReport]]
BriefingFn = Callable[[PipelineContext, date], Awaitable[bool]]
NightlyFn = Callable[[PipelineContext], Awaitable[None]]


def slot_times(runs_per_day: int) -> list[time]:
    if runs_per_day not in VALID_RUNS_PER_DAY:
        runs_per_day = DEFAULT_RUNS_PER_DAY
    step = 24 // runs_per_day
    return [time(hour=h) for h in range(0, 24, step)]


def _slots_on(day: date, runs_per_day: int, tz: ZoneInfo) -> list[datetime]:
    return [datetime.combine(day, t, tzinfo=tz) for t in slot_times(runs_per_day)]


def latest_slot(now: datetime, runs_per_day: int, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    days = (local.date() - timedelta(days=1), local.date())
    past = [s for d in days for s in _slots_on(d, runs_per_day, tz) if s <= local]
    return max(past).astimezone(UTC)


def next_slot(now: datetime, runs_per_day: int, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    days = (local.date(), local.date() + timedelta(days=1))
    future = [s for d in days for s in _slots_on(d, runs_per_day, tz) if s > local]
    return min(future).astimezone(UTC)


def parse_hhmm(value: str) -> time:
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        return DEFAULT_BRIEFING_AT


def briefing_due(now: datetime, briefing_time: str, tz: ZoneInfo) -> date | None:
    local = now.astimezone(tz)
    return local.date() if local.time() >= parse_hhmm(briefing_time) else None


class Scheduler:
    def __init__(
        self,
        ctx: PipelineContext,
        *,
        run_fn: RunFn | None = None,
        briefing_fn: BriefingFn | None = None,
        nightly_fn: NightlyFn | None = None,
        tick_seconds: float = 60.0,
    ) -> None:
        self.ctx = ctx
        self._run_fn: RunFn = run_fn or pipeline.run_once
        self._briefing_fn: BriefingFn = briefing_fn or pipeline.run_briefing
        self._nightly_fn = nightly_fn
        self._tick_seconds = tick_seconds
        self.lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._last_slot: datetime | None = None
        self._briefing_day: date | None = None
        self._nightly_day: date | None = None
        self.last_tick_at: datetime | None = None
        self.next_run_at: datetime | None = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.alive:
            return
        self._task = asyncio.create_task(self._loop(), name="ai-newsdesk-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # the scheduler must outlive any one job
                log.exception("scheduler tick failed")
            await asyncio.sleep(self._tick_seconds)

    async def run_now(self, trigger: str = "manual") -> RunReport | None:
        if self.lock.locked():
            log.info("a job is already running; %s run skipped", trigger)
            return None
        async with self.lock:
            return await self._run_fn(self.ctx, trigger)

    async def tick(self) -> None:
        now = self.ctx.clock.now()
        self.last_tick_at = now
        rs = await load_runtime_settings(self.ctx.store)
        tz = ZoneInfo(self.ctx.settings.tz)

        slot = latest_slot(now, rs.runs_per_day, tz)
        self.next_run_at = next_slot(now, rs.runs_per_day, tz)
        if self._last_slot is None:
            self._last_slot = slot  # no run at start-up: wait for the next slot
        elif slot > self._last_slot:
            self._last_slot = slot
            await self.run_now("schedule")

        day = briefing_due(now, rs.briefing_time, tz)
        if day is not None and day != self._briefing_day:
            self._briefing_day = day
            if await self.ctx.store.get_briefing(day) is None:
                async with self.lock:
                    await self._briefing_fn(self.ctx, day)

        if self._nightly_fn is not None:
            local = now.astimezone(tz)
            if local.time() >= NIGHTLY_AT and local.date() != self._nightly_day:
                self._nightly_day = local.date()
                async with self.lock:
                    await self._nightly_fn(self.ctx)
