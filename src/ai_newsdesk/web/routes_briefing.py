"""Briefing: the latest one, any earlier one by date, and the archive."""

from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ai_newsdesk.store.records import BriefingRecord
from ai_newsdesk.web.briefing_text import parse_briefing
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import page, store_of

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_day(value: str) -> date | None:
    if not _DAY_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def _render(
    request: Request, briefing: BriefingRecord | None, *, status_code: int = 200
) -> HTMLResponse:
    store = store_of(request)
    allowed: set[str] = set()
    if briefing is not None and briefing.story_ids:
        # stories_by_ids returns only stories that still exist: ids merged away since the
        # briefing was written (feedback-1) are skipped, and their links render as plain text.
        for story in (await store.stories_by_ids(briefing.story_ids)).values():
            allowed.update(src.url for src in story.sources)
    return await page(
        request,
        "briefing.html",
        {
            "active": "briefing",
            "briefing": briefing,
            "blocks": parse_briefing(briefing.body, allowed) if briefing is not None else [],
            "archive": await store.briefing_dates(),
            "missing": status_code == 404,
        },
        status_code=status_code,
    )


@router.get("/briefing", response_class=HTMLResponse)
async def latest_briefing(request: Request) -> HTMLResponse:
    return await _render(request, await store_of(request).latest_briefing())


@router.get("/briefing/{day}", response_class=HTMLResponse)
async def briefing_for_day(request: Request, day: str) -> HTMLResponse:
    parsed = _parse_day(day)
    briefing = await store_of(request).get_briefing(parsed) if parsed is not None else None
    if briefing is None:
        return await _render(request, None, status_code=404)
    return await _render(request, briefing)
