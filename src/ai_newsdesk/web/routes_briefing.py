"""Briefing: the latest one, any earlier one by date, and the archive."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_newsdesk import pipeline
from ai_newsdesk.models import Story
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


async def _render(
    request: Request,
    briefing: BriefingRecord | None,
    *,
    status_code: int = 200,
    notice: dict[str, object] | None = None,
    writing: str = "",
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
        },
        status_code=status_code,
    )


@router.get("/briefing", response_class=HTMLResponse)
async def latest_briefing(request: Request, writing: str = "") -> HTMLResponse:
    latest = await store_of(request).latest_briefing()
    return await _render(
        request,
        latest,
        notice=await _notice(request, latest),
        writing=writing if writing in ("1", "busy") else "",
    )


async def _write_briefing_now(app: FastAPI) -> None:
    """Write today's briefing under the scheduler lock, then make its audio under the lock
    again (released in between, so a waiting job is not held up twice). Logs and swallows
    any failure."""
    try:
        ctx = app.state.ctx
        today = ctx.clock.now().astimezone(ZoneInfo(ctx.settings.tz)).date()
        async with app.state.scheduler.lock:
            written = await pipeline.run_briefing(ctx, today)
        if not written:
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
async def briefing_for_day(request: Request, day: str) -> HTMLResponse:
    parsed = _parse_day(day)
    briefing = await store_of(request).get_briefing(parsed) if parsed is not None else None
    if briefing is None:
        return await _render(request, None, status_code=404)
    return await _render(request, briefing)
