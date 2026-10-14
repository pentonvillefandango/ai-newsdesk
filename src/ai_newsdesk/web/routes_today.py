"""Today: the front page, thumbs on a story, and "never show stories like this"."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_newsdesk import feedback, taste
from ai_newsdesk.models import Category, Story
from ai_newsdesk.runtime_settings import load_runtime_settings
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import (
    NO_STORE,
    category_label,
    is_htmx,
    now_of,
    page,
    partial,
    store_of,
    zone_of,
)

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])

BLOCK_ERROR = "Enter a short word or phrase (up to 60 characters)."


@dataclass(frozen=True)
class Group:
    category: Category
    label: str
    slug: str
    stories: list[Story]


def group_by_category(stories: list[Story]) -> list[Group]:
    groups: list[Group] = []
    for category in Category:
        members = [s for s in stories if s.category == category]
        if members:
            groups.append(
                Group(category, category_label(category), f"section-{category.value}", members)
            )
    return groups


def _view(view: str) -> str:
    return "category" if view == "category" else "top"


def today_url(view: str, *, dismissed: bool = False) -> str:
    """Today's URL for the no-JS redirects; only whitelisted query values are ever echoed."""
    query: list[str] = []
    if _view(view) == "category":
        query.append("view=category")
    if dismissed:
        query.append("dismissed=1")
    return "/?" + "&".join(query) if query else "/"


@router.get("/", response_class=HTMLResponse)
async def today(
    request: Request, view: str = "top", blocked: int = 0, dismissed: int = 0
) -> HTMLResponse:
    store = store_of(request)
    now = now_of(request)
    runtime = await load_runtime_settings(store)
    showing_dismissed = dismissed == 1
    by_score = sorted(
        await store.today_stories(now, runtime.show_threshold, dismissed=showing_dismissed),
        key=lambda s: (-s.score, -s.last_updated_at.timestamp()),
    )
    # Step 4's taste model nudges scores by up to +-2 once it has enough thumbs (else a no-op).
    # Dismissed stories never reach it: they are hidden, not rated.
    stories = by_score if showing_dismissed else await taste.rank_today(store, by_score)
    return await page(
        request,
        "today.html",
        {
            "active": "today",
            "view": _view(view),
            "stories": stories,
            "groups": group_by_category(stories),
            "unscored": [] if showing_dismissed else await store.unscored_items(now),
            "source_count": len({src.source_name for s in stories for src in s.sources}),
            "local_today": now.astimezone(zone_of(request)).date(),
            "show_why": runtime.why_it_matters,
            "blocked": blocked == 1,
            "showing_dismissed": showing_dismissed,
            "dismissed_count": 0
            if showing_dismissed
            else await store.count_dismissed_stories(now, runtime.show_threshold),
            "dismissed_url": today_url(view, dismissed=True),
            "back_url": today_url(view),
        },
    )


@router.get("/dismissed/expire")
async def placeholder_expired() -> Response:
    """An empty answer for the "Dismissed · Undo" placeholder's delayed `hx-get`: htmx then
    swaps the placeholder away (`hx-swap="delete"`), with no inline script (CSP). 200, not
    204: the htmx config does not swap on 204."""
    return Response("", media_type="text/plain", headers=NO_STORE)


async def _story(request: Request, story_id: int) -> Story:
    story = (await store_of(request).stories_by_ids([story_id])).get(story_id)
    if story is None:
        raise HTTPException(status_code=404, detail="There is no such story.")
    return story


async def _card(
    request: Request,
    story_id: int,
    *,
    lead: bool,
    blocked: str | None = None,
    block_error: str | None = None,
    view: str = "top",
    focus_dismiss: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    story = await _story(request, story_id)
    runtime = await load_runtime_settings(store_of(request))
    return partial(
        request,
        "partials/story_card.html",
        {
            "story": story,
            "show_why": runtime.why_it_matters,
            "lead": lead,
            "blocked": blocked,
            "block_error": block_error,
            "view": _view(view),
            "focus_dismiss": focus_dismiss,
        },
        status_code=status_code,
    )


@router.post("/stories/{story_id}/feedback")
async def thumbs(
    request: Request,
    story_id: int,
    value: Annotated[int, Form()],
    lead: Annotated[int, Form()] = 0,
) -> Response:
    if value not in (-1, 0, 1):
        raise HTTPException(status_code=422, detail="Feedback must be -1, 0 or 1.")
    await _story(request, story_id)
    # Step 4 (R5): stores the thumb and moves the source weights straight away.
    await feedback.set_feedback(store_of(request), request.app.state.ctx.clock, story_id, value)
    if not is_htmx(request):
        return RedirectResponse(f"/#story-{story_id}", status_code=303)
    return await _card(request, story_id, lead=lead == 1)


@router.post("/stories/{story_id}/block")
async def never_show_like_this(
    request: Request,
    story_id: int,
    keyword: Annotated[str, Form()] = "",
    lead: Annotated[int, Form()] = 0,
) -> Response:
    await _story(request, story_id)
    try:
        # Step 4 (R5): lower-cases, collapses spaces, validates, and appends to the `blocklist`
        # setting that step 3's pre-filter reads on the next run.
        words = await feedback.add_blocklist_keyword(store_of(request), keyword)
    except ValueError:
        if not is_htmx(request):
            raise HTTPException(status_code=422, detail=BLOCK_ERROR) from None
        return await _card(
            request, story_id, lead=lead == 1, block_error=BLOCK_ERROR, status_code=422
        )
    if not is_htmx(request):
        return RedirectResponse(f"/?blocked=1#story-{story_id}", status_code=303)
    added = " ".join(keyword.lower().split())
    return await _card(request, story_id, lead=lead == 1, blocked=added if added in words else None)


@router.post("/stories/{story_id}/dismiss")
async def dismiss(
    request: Request,
    story_id: int,
    lead: Annotated[int, Form()] = 0,
    view: Annotated[str, Form()] = "top",
) -> Response:
    """Hide a story from Today ("done with this"). Feedback and source weights are untouched."""
    story = await _story(request, story_id)
    if not await store_of(request).dismiss_story(story_id, now_of(request)):
        raise HTTPException(status_code=404, detail="There is no such story.")
    if not is_htmx(request):
        return RedirectResponse(today_url(view), status_code=303)
    return partial(
        request,
        "partials/dismissed.html",
        {
            "dom_id": f"story-{story_id}",
            "undo_url": f"/stories/{story_id}/undismiss",
            "label": story.headline,
            "lead": lead == 1,
            "view": _view(view),
        },
    )


@router.post("/stories/{story_id}/undismiss")
async def undismiss(
    request: Request,
    story_id: int,
    lead: Annotated[int, Form()] = 0,
    view: Annotated[str, Form()] = "top",
    dismissed: Annotated[int, Form()] = 0,
) -> Response:
    if not await store_of(request).undismiss_story(story_id):
        raise HTTPException(status_code=404, detail="There is no such story.")
    if not is_htmx(request):
        return RedirectResponse(today_url(view, dismissed=dismissed == 1), status_code=303)
    return await _card(request, story_id, lead=lead == 1, view=view, focus_dismiss=True)
