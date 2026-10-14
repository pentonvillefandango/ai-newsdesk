"""Owner login: argon2 password check, signed session cookie, login rate limit and the auth gate."""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from urllib.parse import quote, unquote, urlsplit

import anyio.to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, TimestampSigner
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

SESSION_COOKIE = "nd_session"
SESSION_MAX_AGE_S = 14 * 24 * 3600
_SALT = "ai-newsdesk-session"
OPEN_PATHS = frozenset({"/login", "/health"})
OPEN_PREFIXES = ("/static/",)
MAX_PASSWORD_CHARS = 1024  # argon2 cost is per byte of input; refuse absurd inputs outright

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password_sync(stored_hash: str, password: str) -> bool:
    if not stored_hash or not password or len(password) > MAX_PASSWORD_CHARS:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def verify_password(stored_hash: str, password: str) -> bool:
    """argon2 is deliberately slow, so it runs off the event loop."""
    return await anyio.to_thread.run_sync(verify_password_sync, stored_hash, password)


class SessionCodec:
    """The cookie value is ``TimestampSigner(secret).sign(fingerprint of OWNER_PASSWORD_HASH)``.

    Changing the password hash changes the fingerprint, so every older session stops working.
    With no hash configured, nothing is ever valid.
    """

    def __init__(self, secret: str, password_hash: str) -> None:
        self._signer = TimestampSigner(secret, salt=_SALT)
        self._enabled = bool(password_hash)
        self._payload = hashlib.sha256(password_hash.encode()).hexdigest()[:32].encode()

    def issue(self) -> str:
        return self._signer.sign(self._payload).decode()

    def valid(self, value: str | None) -> bool:
        if not self._enabled or not value:
            return False
        try:
            return self._signer.unsign(value, max_age=SESSION_MAX_AGE_S) == self._payload
        except (BadSignature, UnicodeError):  # SignatureExpired is a BadSignature
            return False


class LoginRateLimiter:
    """At most ``max_failures`` failed logins per client in a sliding ``window_s``.

    Memory is bounded by ``max_clients`` (the least recently failing client is dropped first).
    """

    def __init__(
        self,
        max_failures: int = 5,
        window_s: float = 15 * 60,
        *,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_failures
        self._window = window_s
        self._cap = max_clients
        self._clock = clock
        self._fails: OrderedDict[str, deque[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._fails)

    def _recent(self, client: str) -> deque[float] | None:
        q = self._fails.get(client)
        if q is None:
            return None
        cutoff = self._clock() - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            del self._fails[client]
            return None
        return q

    def blocked(self, client: str) -> bool:
        q = self._recent(client)
        return q is not None and len(q) >= self._max

    def retry_after(self, client: str) -> int:
        """Whole seconds until ``client`` may try again (0 when not blocked)."""
        q = self._recent(client)
        if q is None or len(q) < self._max:
            return 0
        return max(1, math.ceil(q[0] + self._window - self._clock()))

    def fail(self, client: str) -> None:
        if self.blocked(client):
            return  # attempts during a lockout neither count nor extend it
        q = self._fails.get(client)
        if q is None:
            q = self._fails[client] = deque(maxlen=self._max)
        else:
            self._fails.move_to_end(client)
        q.append(self._clock())
        while len(self._fails) > self._cap:
            self._fails.popitem(last=False)

    def reset(self, client: str) -> None:
        self._fails.pop(client, None)


def _bad_path(value: str) -> bool:
    return (
        not value.startswith("/")
        or value.startswith(("//", "/\\"))
        or "\\" in value
        or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value)
    )


def safe_next(value: str | None) -> str:
    """``value`` if it is a same-site path (also once percent-decoded), otherwise ``/``."""
    if not value or len(value) > 2048 or _bad_path(value) or _bad_path(unquote(value)):
        return "/"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    return value


def is_open_path(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def login_required_response(conn: HTTPConnection, method: str) -> Response:
    target = conn.url.path + (f"?{conn.url.query}" if conn.url.query else "")
    login_url = "/login?next=" + quote(safe_next(target), safe="")
    if conn.headers.get("hx-request") == "true":
        # htmx follows HX-Redirect before it looks at the status code.
        return Response(status_code=401, headers={"HX-Redirect": login_url})
    if method in ("GET", "HEAD"):
        return RedirectResponse(login_url, status_code=303)
    return PlainTextResponse("Log in first.", status_code=401)


class AuthMiddleware:
    """Pure ASGI gate: every HTTP path except the open ones needs a valid session cookie.

    Fail-closed: a new route is protected without anyone remembering to protect it.
    """

    def __init__(self, app: ASGIApp, codec: SessionCodec) -> None:
        self.app = app
        self.codec = codec

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_open_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        if self.codec.valid(conn.cookies.get(SESSION_COOKIE)):
            await self.app(scope, receive, send)
            return
        response = login_required_response(conn, scope["method"])
        await response(scope, receive, send)
