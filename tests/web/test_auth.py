from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.config import Settings
from ai_newsdesk.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    LoginRateLimiter,
    SessionCodec,
    is_open_path,
    safe_next,
    verify_password_sync,
)
from ai_newsdesk.web.csrf import CSRF_COOKIE, csrf_protect
from ai_newsdesk.web.security import install_security
from tests.web.support import BASE_URL, TEST_HASH, TEST_PASSWORD, csrf_token, login, make_settings


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    install_security(app, settings)

    @app.get("/private")
    async def private() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/private", dependencies=[Depends(csrf_protect)])
    async def private_post() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(_app(make_settings(tmp_path)), base_url=BASE_URL) as c:
        yield c


def test_private_page_redirects_to_login(client: TestClient) -> None:
    r = client.get("/private?x=1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fprivate%3Fx%3D1"


def test_htmx_request_without_session_gets_hx_redirect(client: TestClient) -> None:
    r = client.get("/private", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["HX-Redirect"].startswith("/login?next=")


def test_post_without_session_is_401(client: TestClient) -> None:
    assert client.post("/private", follow_redirects=False).status_code == 401


def test_login_page_shows_form_and_sets_csrf_cookie(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert client.cookies.get(CSRF_COOKIE)
    cookie = next(v for v in r.headers.get_list("set-cookie") if v.startswith(f"{CSRF_COOKIE}="))
    for part in ("httponly", "secure", "samesite=strict"):
        assert part in cookie.lower()


def test_wrong_password_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"password": "nope", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Wrong password. Try again." in r.text
    assert client.get("/private", follow_redirects=False).status_code == 303


def test_login_sets_hardened_session_cookie(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"password": TEST_PASSWORD, "next": "/private", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/private"
    cookie = next(v for v in r.headers.get_list("set-cookie") if v.startswith(f"{SESSION_COOKIE}="))
    lowered = cookie.lower()
    for part in ("httponly", "secure", "samesite=lax", f"max-age={SESSION_MAX_AGE_S}"):
        assert part in lowered
    assert client.get("/private").json() == {"ok": True}


def test_login_needs_csrf_token(client: TestClient) -> None:
    client.get("/login")
    r = client.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert r.status_code == 403


def test_open_redirect_falls_back_to_root(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={
            "password": TEST_PASSWORD,
            "next": "//evil.example/x",
            "csrf_token": csrf_token(client),
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_five_failures_lock_the_client_out(client: TestClient) -> None:
    token = csrf_token(client)
    for _ in range(5):
        r = client.post(
            "/login", data={"password": "nope", "csrf_token": token}, follow_redirects=False
        )
        assert r.status_code == 401
    r = client.post(
        "/login", data={"password": TEST_PASSWORD, "csrf_token": token}, follow_redirects=False
    )
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert "Too many wrong passwords" in r.text


def test_rate_limiter_forgets_after_the_window() -> None:
    now = [0.0]
    limiter = LoginRateLimiter(clock=lambda: now[0])
    for _ in range(5):
        limiter.fail("192.0.2.1")
    assert limiter.blocked("192.0.2.1")
    assert not limiter.blocked("192.0.2.2")
    assert limiter.retry_after("192.0.2.1") == 900
    now[0] = 15 * 60 + 1
    assert not limiter.blocked("192.0.2.1")
    limiter.fail("192.0.2.3")
    limiter.reset("192.0.2.3")
    assert not limiter.blocked("192.0.2.3")
    assert len(limiter) == 0


def test_tampered_session_cookie_is_refused(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "forged.value.sig", domain="testserver")
    assert client.get("/private", follow_redirects=False).status_code == 303


def test_changing_the_password_hash_ends_sessions() -> None:
    a = SessionCodec("k" * 44, "hash-a")
    b = SessionCodec("k" * 44, "hash-b")
    assert a.valid(a.issue())
    assert not b.valid(a.issue())
    disabled = SessionCodec("k" * 44, "")
    assert not disabled.valid(disabled.issue())


def test_logout_ends_the_session(client: TestClient) -> None:
    login(client)
    r = client.post("/logout", data={"csrf_token": csrf_token(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert client.get("/private", follow_redirects=False).status_code == 303


def test_csrf_header_is_accepted_and_wrong_tokens_refused(client: TestClient) -> None:
    login(client)
    assert client.post("/private", headers={"X-CSRF-Token": csrf_token(client)}).status_code == 200
    assert client.post("/private", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/private").status_code == 403


def test_empty_password_hash_disables_login(tmp_path: Path) -> None:
    with TestClient(_app(make_settings(tmp_path, owner_password_hash="")), base_url=BASE_URL) as c:
        page = c.get("/login")
        assert "make set-password" in page.text
        assert 'name="password"' not in page.text
        r = c.post(
            "/login",
            data={"password": "anything", "csrf_token": csrf_token(c)},
            follow_redirects=False,
        )
        assert r.status_code == 403


def test_open_paths() -> None:
    for path in ("/login", "/health", "/static/app.css"):
        assert is_open_path(path)
    for path in ("/", "/loginx", "/settings", "/healthz", "/static"):
        assert not is_open_path(path)


def test_safe_next() -> None:
    assert safe_next("/settings?x=1") == "/settings?x=1"
    for bad in (
        "//evil.example",
        "https://evil.example/",
        "/\\evil",
        "",
        None,
        "/%2F%2Fevil",
        "settings",
    ):
        assert safe_next(bad) == "/"


def test_verify_password() -> None:
    assert verify_password_sync(TEST_HASH, TEST_PASSWORD)
    assert not verify_password_sync(TEST_HASH, "nope")
    assert not verify_password_sync("not-a-hash", TEST_PASSWORD)
    assert not verify_password_sync("", TEST_PASSWORD)


def test_security_headers(client: TestClient) -> None:
    r = client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_cookies_use_host_prefix_without_domain(client: TestClient) -> None:
    assert SESSION_COOKIE == "__Host-nd_session"
    assert CSRF_COOKIE == "__Host-nd_csrf"
    r1 = client.get("/login")
    r2 = client.post(
        "/login",
        data={"password": TEST_PASSWORD, "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    for resp, name in ((r1, CSRF_COOKIE), (r2, SESSION_COOKIE)):
        cookie = next(v for v in resp.headers.get_list("set-cookie") if v.startswith(f"{name}="))
        lowered = cookie.lower()
        assert "path=/" in lowered
        assert "secure" in lowered
        assert "domain" not in lowered
    out = client.post("/logout", data={"csrf_token": csrf_token(client)}, follow_redirects=False)
    gone = next(v for v in out.headers.get_list("set-cookie") if v.startswith(f"{SESSION_COOKIE}="))
    assert "domain" not in gone.lower()
    assert "path=/" in gone.lower()


async def test_concurrent_guesses_cannot_bypass_the_limiter(tmp_path: Path) -> None:
    app = _app(make_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as c:
        await c.get("/login")
        token = c.cookies.get(CSRF_COOKIE)
        assert token
        results = await asyncio.gather(
            *[c.post("/login", data={"password": "nope", "csrf_token": token}) for _ in range(30)]
        )
    codes = [r.status_code for r in results]
    assert codes.count(401) <= 5
    assert codes.count(429) >= 25


async def test_password_verifies_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"now": 0, "max": 0}

    async def slow_verify(stored_hash: str, password: str) -> bool:
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return False

    monkeypatch.setattr("ai_newsdesk.web.routes_auth.verify_password", slow_verify)
    app = _app(make_settings(tmp_path))
    app.state.login_limiter = LoginRateLimiter(max_failures=100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as c:
        await c.get("/login")
        token = c.cookies.get(CSRF_COOKIE)
        await asyncio.gather(
            *[c.post("/login", data={"password": "x", "csrf_token": token}) for _ in range(10)]
        )
    assert state["max"] == 2
