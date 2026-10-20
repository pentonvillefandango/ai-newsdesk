"""Briefing: the latest one, any earlier one by date, and the archive."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ai_newsdesk import pipeline
from ai_newsdesk.models import Story
from ai_newsdesk.pipeline import NarrationOutcome
from ai_newsdesk.runtime_settings import load_runtime_settings
from ai_newsdesk.scheduler import parse_hhmm
from ai_newsdesk.store.records import BriefingRecord
from ai_newsdesk.web.briefing_text import parse_briefing
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import page, store_of

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])
log = logging.getLogger(__name__)
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_day(value: str) -> date | None:
    if not _DAY_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def _notice(request: Request, latest: BriefingRecord | None) -> dict[str, object] | None:
    """The "today's briefing isn't ready" notice, or None when it is, or isn't due yet."""
    store = store_of(request)
    ctx = request.app.state.ctx
    local = ctx.clock.now().astimezone(ZoneInfo(ctx.settings.tz))
    rs = await load_runtime_settings(store)
    today = local.date()
    if local.time() < parse_hhmm(rs.briefing_time) or await store.get_briefing(today) is not None:
        return None
    scheduler = getattr(request.app.state, "scheduler", None)
    status = scheduler.briefing_status(today) if scheduler is not None else None
    return {
        "showing": f"{latest.date:%A} {latest.date.day} {latest.date:%B}"
        if latest is not None
        else None,
        "tries": status.tries if status is not None else 0,
        "max_tries": status.max_tries if status is not None else 0,
        "gave_up": status.gave_up if status is not None else False,
    }


# Bytes per second of the narrator's 64 kbps MP3.
_AUDIO_BYTES_PER_SECOND = 8000
_REASONS = {
    "off": "Audio is off in Settings.",
    "no_key": "Add an ElevenLabs key in Settings to hear this briefing.",
    "making": "Making audio. This takes a minute or two; reload to listen.",
    "remaking": "Making new audio. This takes a minute or two; reload to listen.",
    "busy": "A run is in progress; try again in a few minutes.",
    "over_cap": "This month's audio allowance is used up.",
    "failed_retry": "Audio failed; it will be retried.",
    "failed": "Audio failed today.",
    "none": "No audio yet.",
}


async def _listen(request: Request, briefing: BriefingRecord, audio_flag: str) -> dict[str, object]:
    """The Listen bar's context: the day's audio, or the reason there is none."""
    store = store_of(request)
    ctx = request.app.state.ctx
    record = await store.get_briefing_audio(briefing.date)
    if record is not None:
        return {
            "audio": record,
            # A new file per remake: the MP3 is cached for a day, so its URL must change.
            "version": record.file.removesuffix(".mp3").rsplit("-", 1)[-1],
            "minutes": max(1, round(record.bytes / _AUDIO_BYTES_PER_SECOND / 60)),
            "reason": _REASONS["remaking"] if briefing.date in _in_flight(request.app) else None,
            "can_make": True,
        }
    rs = await load_runtime_settings(store)
    scheduler = getattr(request.app.state, "scheduler", None)
    status = scheduler.narration_status(briefing.date) if scheduler is not None else None
    key = "none"
    if not rs.narration_enabled:
        key = "off"
    elif ctx.has_narration_key is not None and not await ctx.has_narration_key():
        key = "no_key"
    elif briefing.date in _in_flight(request.app):
        key = "making"
    elif audio_flag == "busy":
        key = "busy"
    elif (status is not None and status.last_outcome == NarrationOutcome.OVER_CAP) or (
        await store.month_tts_characters(ctx.clock.now(), ctx.settings.tz)
        >= rs.narration_monthly_chars
    ):
        key = "over_cap"
    elif status is not None and status.last_outcome == NarrationOutcome.FAILED:
        key = "failed" if status.gave_up else "failed_retry"
    return {
        "audio": None,
        "reason": _REASONS[key],
        "can_make": key not in ("off", "no_key"),
    }


async def _render(
    request: Request,
    briefing: BriefingRecord | None,
    *,
    status_code: int = 200,
    notice: dict[str, object] | None = None,
    writing: str = "",
    audio_flag: str = "",
) -> HTMLResponse:
    store = store_of(request)
    allowed: set[str] = set()
    covered: list[Story] = []
    if briefing is not None and briefing.story_ids:
        # stories_by_ids returns only stories that still exist: ids merged away since the
        # briefing was written (feedback-1) are skipped, and their links render as plain text.
        found = await store.stories_by_ids(briefing.story_ids)
        covered = [found[i] for i in dict.fromkeys(briefing.story_ids) if i in found]
        for story in covered:
            allowed.update(src.url for src in story.sources)
    return await page(
        request,
        "briefing.html",
        {
            "active": "briefing",
            "briefing": briefing,
            "covered": covered,
            "blocks": parse_briefing(briefing.body, allowed) if briefing is not None else [],
            "archive": await store.briefing_dates(),
            "missing": status_code == 404,
            "notice": notice,
            "writing": writing,
            "listen": await _listen(request, briefing, audio_flag)
            if briefing is not None
            else None,
        },
        status_code=status_code,
    )


@router.get("/briefing", response_class=HTMLResponse)
async def latest_briefing(request: Request, writing: str = "", audio: str = "") -> HTMLResponse:
    latest = await store_of(request).latest_briefing()
    return await _render(
        request,
        latest,
        notice=await _notice(request, latest),
        writing=writing if writing in ("1", "busy") else "",
        audio_flag=audio,
    )


def _in_flight(app: FastAPI) -> set[date]:
    """Days whose audio a web background task (Write it now, Make audio) is making now."""
    days: set[date] | None = getattr(app.state, "audio_in_flight", None)
    if days is None:
        days = app.state.audio_in_flight = set()
    return days


async def _write_briefing_now(app: FastAPI) -> None:
    """Write today's briefing under the scheduler lock, then make its audio under the lock
    again (released in between, so a waiting job is not held up twice). If today's briefing
    exists by the time the lock is held (the scheduler wrote it), it is not written again;
    its audio is still made if it has none. Logs and swallows any failure."""
    ctx = app.state.ctx
    today = ctx.clock.now().astimezone(ZoneInfo(ctx.settings.tz)).date()
    in_flight = _in_flight(app)
    in_flight.add(today)
    try:
        try:
            async with app.state.scheduler.lock:
                if await ctx.store.get_briefing(today) is not None:
                    log.info("today's briefing already exists; not written again")
                    if await ctx.store.get_briefing_audio(today) is not None:
                        return
                elif not await pipeline.run_briefing(ctx, today):
                    log.warning("manual briefing for %s was not written", today.isoformat())
                    return
        except Exception:
            log.exception("manual briefing failed")
            return
        try:
            async with app.state.scheduler.lock:
                outcome = await pipeline.run_narration(ctx, today)
            log.info("audio for the manual %s briefing: %s", today.isoformat(), outcome.value)
        except Exception:
            log.exception("audio for the manual briefing failed")
    finally:
        in_flight.discard(today)


@router.post("/briefing/write")
async def write_briefing(request: Request) -> Response:
    app: FastAPI = request.app
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        return Response("The scheduler is not running.", status_code=503)
    if scheduler.lock.locked():
        return RedirectResponse("/briefing?writing=busy", status_code=303)
    task = asyncio.create_task(_write_briefing_now(app), name="manual-briefing")
    app.state.background.add(task)
    task.add_done_callback(app.state.background.discard)
    return RedirectResponse("/briefing?writing=1", status_code=303)


@router.get("/briefing/{day}", response_class=HTMLResponse)
async def briefing_for_day(request: Request, day: str, audio: str = "") -> HTMLResponse:
    parsed = _parse_day(day)
    briefing = await store_of(request).get_briefing(parsed) if parsed is not None else None
    if briefing is None:
        return await _render(request, None, status_code=404)
    return await _render(request, briefing, audio_flag=audio)


@router.get("/briefing/{day}/audio.mp3")
async def briefing_audio(request: Request, day: str) -> Response:
    parsed = _parse_day(day)
    files = request.app.state.ctx.audio
    record = await store_of(request).get_briefing_audio(parsed) if parsed is not None else None
    if record is None or files is None:
        return Response("No audio for that day.", status_code=404)
    try:
        path = files.path(record.file)
    except ValueError:
        return Response("No audio for that day.", status_code=404)
    if not path.is_file():
        return Response("No audio for that day.", status_code=404)
    return FileResponse(
        path, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"}
    )


async def _make_audio(app: FastAPI, day: date) -> None:
    """Make (or remake) the day's audio under the scheduler lock. Logs and swallows failure.
    The day is in flight (the page says "Making audio") until the work ends."""
    in_flight = _in_flight(app)
    in_flight.add(day)
    try:
        async with app.state.scheduler.lock:
            outcome = await pipeline.run_narration(app.state.ctx, day)
        log.info("audio for %s: %s", day.isoformat(), outcome.value)
    except Exception:
        log.exception("making audio for %s failed", day.isoformat())
    finally:
        in_flight.discard(day)


@router.post("/briefing/{day}/audio")
async def make_audio(request: Request, day: str) -> Response:
    app: FastAPI = request.app
    parsed = _parse_day(day)
    store = store_of(request)
    if parsed is None or await store.get_briefing(parsed) is None:
        return Response("No briefing for that day.", status_code=404)
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        return Response("The scheduler is not running.", status_code=503)
    latest = await store.latest_briefing()
    base = "/briefing" if latest is not None and latest.date == parsed else f"/briefing/{day}"
    if scheduler.lock.locked():
        return RedirectResponse(f"{base}?audio=busy", status_code=303)
    task = asyncio.create_task(_make_audio(app, parsed), name="manual-audio")
    app.state.background.add(task)
    task.add_done_callback(app.state.background.discard)
    return RedirectResponse(f"{base}?audio=making", status_code=303)
