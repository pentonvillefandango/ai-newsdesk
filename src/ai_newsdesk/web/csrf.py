"""Double-submit CSRF protection.

A random token lives in the ``nd_csrf`` cookie (set on the first response to any browser). Every
unsafe request must echo it in the ``csrf_token`` form field or the ``X-CSRF-Token`` header.
"""

from __future__ import annotations

import hmac
import re
import secrets

from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_COOKIE = "nd_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
CSRF_MAX_AGE_S = 14 * 24 * 3600
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def new_token() -> str:
    return secrets.token_urlsafe(32)  # 43 characters


class CSRFCookieMiddleware:
    """Gives every browser a CSRF token, exposed as ``request.state.csrf_token``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = HTTPConnection(scope).cookies.get(CSRF_COOKIE, "")
        fresh = not _TOKEN_RE.match(token)
        if fresh:
            token = new_token()
        scope.setdefault("state", {})["csrf_token"] = token
        if not fresh:
            await self.app(scope, receive, send)
            return
        cookie = (
            f"{CSRF_COOKIE}={token}; Path=/; Max-Age={CSRF_MAX_AGE_S}; "
            "HttpOnly; Secure; SameSite=Strict"
        ).encode()

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"set-cookie", cookie))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_cookie)


async def csrf_protect(request: Request) -> None:
    """FastAPI dependency: refuse an unsafe request whose token does not match the cookie."""
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE, "")
    supplied = request.headers.get(CSRF_HEADER, "")
    if not supplied and request.headers.get("content-type", "").startswith(_FORM_TYPES):
        value = (await request.form()).get(CSRF_FIELD)
        supplied = value if isinstance(value, str) else ""
    if not cookie or not supplied or not hmac.compare_digest(cookie.encode(), supplied.encode()):
        raise HTTPException(
            status_code=403,
            detail="The form expired or came from somewhere else. Reload the page and try again.",
        )
