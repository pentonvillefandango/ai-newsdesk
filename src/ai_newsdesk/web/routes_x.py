"""X: posts from the people on X, on their own page (never mixed into Today's stories)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import now_of, page, store_of

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])


@router.get("/x", response_class=HTMLResponse)
async def x_posts(request: Request) -> HTMLResponse:
    posts = await store_of(request).x_posts(now_of(request))
    return await page(request, "x.html", {"active": "x", "posts": posts})
