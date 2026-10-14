"""Login and logout."""

from __future__ import annotations

import math
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_newsdesk.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    LoginRateLimiter,
    SessionCodec,
    safe_next,
    verify_password,
)
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import NO_STORE, templates

router = APIRouter(include_in_schema=False)


def _client_ip(request: Request) -> str:
    # Behind Caddy, uvicorn --proxy-headers has already put the forwarded client address here.
    return request.client.host if request.client else "unknown"


def _login_page(
    request: Request, next_path: str, error: str | None, status_code: int
) -> HTMLResponse:
    enabled = bool(request.app.state.settings.owner_password_hash)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": next_path,
            "error": error,
            "login_enabled": enabled,
            "csrf_token": request.state.csrf_token,
        },
        status_code=status_code,
        headers=NO_STORE,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> Response:
    target = safe_next(next)
    codec: SessionCodec = request.app.state.session_codec
    if codec.valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(target, status_code=303)
    return _login_page(request, target, None, 200)


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login(
    request: Request,
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    target = safe_next(next)
    stored_hash: str = request.app.state.settings.owner_password_hash
    if not stored_hash:
        return _login_page(request, target, None, 403)
    limiter: LoginRateLimiter = request.app.state.login_limiter
    client = _client_ip(request)
    if limiter.blocked(client):
        wait_s = limiter.retry_after(client)
        minutes = max(1, math.ceil(wait_s / 60))
        response = _login_page(
            request, target, f"Too many wrong passwords. Try again in {minutes} min.", 429
        )
        response.headers["Retry-After"] = str(wait_s)
        return response
    if not await verify_password(stored_hash, password):
        limiter.fail(client)
        return _login_page(request, target, "Wrong password. Try again.", 401)
    limiter.reset(client)
    codec: SessionCodec = request.app.state.session_codec
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        codec.issue(),
        max_age=SESSION_MAX_AGE_S,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.post("/logout", dependencies=[Depends(csrf_protect)])
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="lax")
    return response
