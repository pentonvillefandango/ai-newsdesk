"""Installs the security middlewares, auth state and login routes on an app."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_newsdesk.config import Settings
from ai_newsdesk.web import routes_auth
from ai_newsdesk.web.auth import AuthMiddleware, LoginRateLimiter, SessionCodec
from ai_newsdesk.web.csrf import CSRFCookieMiddleware

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_SECURITY_HEADERS = (
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
)


class SecurityHeadersMiddleware:
    """Adds the CSP and hardening headers to every HTTP response (headers only, body untouched)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def install_security(app: FastAPI, settings: Settings) -> None:
    """Session codec, login limiter, middlewares and the /login + /logout routes.

    Middleware order (the last added is outermost): security headers wrap everything, so
    redirects and 401s carry the CSP; the CSRF cookie is issued even on the login redirect; the
    auth gate is innermost.
    """
    app.state.session_codec = SessionCodec(settings.session_secret, settings.owner_password_hash)
    app.state.login_limiter = LoginRateLimiter()
    app.add_middleware(AuthMiddleware, codec=app.state.session_codec)
    app.add_middleware(CSRFCookieMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(routes_auth.router)
