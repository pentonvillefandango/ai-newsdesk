"""X: posts from the people on X, on their own page (never mixed into Today's stories)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import NO_STORE, is_htmx, now_of, page, partial, store_of

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])

NO_POST = "There is no such post from X."


def _x_url(*, dismissed: bool = False) -> str:
    return "/x?dismissed=1" if dismissed else "/x"


@router.get("/x", response_class=HTMLResponse)
async def x_posts(request: Request, dismissed: int = 0) -> HTMLResponse:
    store = store_of(request)
    now = now_of(request)
    showing_dismissed = dismissed == 1
    if showing_dismissed:
        posts = await store.x_posts(now, dismissed=True)
        count = 0
    else:
        posts = await store.x_posts(now)
        count = await store.count_dismissed_x_posts(now)
    return await page(
        request,
        "x.html",
        {
            "active": "x",
            "posts": posts,
            "showing_dismissed": showing_dismissed,
            "dismissed_count": count,
        },
    )


@router.post("/x/{item_id}/dismiss")
async def dismiss(request: Request, item_id: int) -> Response:
    """Hide a post from the X page ("done with this"). Feedback and weights are untouched."""
    store = store_of(request)
    now = now_of(request)
    post = next((p for p in await store.x_posts(now) if p.item_id == item_id), None)
    if not await store.dismiss_item(item_id, now):
        raise HTTPException(status_code=404, detail=NO_POST)
    if not is_htmx(request):
        return RedirectResponse(_x_url(), status_code=303)
    return partial(
        request,
        "partials/dismissed.html",
        {
            "dom_id": f"post-{item_id}",
            "undo_url": f"/x/{item_id}/undismiss",
            "label": f"@{post.handle}" if post is not None else "this post",
        },
    )


@router.post("/x/{item_id}/undismiss")
async def undismiss(
    request: Request, item_id: int, dismissed: Annotated[int, Form()] = 0
) -> Response:
    store = store_of(request)
    if not await store.undismiss_item(item_id):
        raise HTTPException(status_code=404, detail=NO_POST)
    if not is_htmx(request):
        return RedirectResponse(_x_url(dismissed=dismissed == 1), status_code=303)
    post = next((p for p in await store.x_posts(now_of(request)) if p.item_id == item_id), None)
    if post is None:  # restored but outside the page's window (or dropped): nothing to show
        return HTMLResponse("", headers=NO_STORE)
    return partial(request, "partials/x_post.html", {"post": post, "focus_dismiss": True})
