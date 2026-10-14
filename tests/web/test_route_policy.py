"""Guards every route added in later tasks: session required, CSRF required on POST."""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.web.auth import SESSION_COOKIE
from ai_newsdesk.web.csrf import CSRF_COOKIE
from tests.web.support import api_routes

OPEN_GET = {"/login", "/health"}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _paths(app: FastAPI, method: str) -> list[str]:
    paths = [r.path for r in api_routes(app) if r.methods and method in r.methods]
    assert paths, "no routes found: the policy tests would pass vacuously"
    return paths


def test_every_page_needs_a_session(app: FastAPI, anon: TestClient) -> None:
    for path in _paths(app, "GET"):
        if path in OPEN_GET:
            continue
        r = anon.get(_concrete(path), follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"].startswith("/login?next="), path


def test_every_post_needs_a_session(app: FastAPI, anon: TestClient) -> None:
    for path in _paths(app, "POST"):
        if path == "/login":
            continue
        assert anon.post(_concrete(path), follow_redirects=False).status_code == 401, path


def test_every_post_needs_a_csrf_token(app: FastAPI, client: TestClient) -> None:
    posts = _paths(app, "POST")
    assert "/login" in posts and "/logout" in posts
    for path in posts:
        r = client.post(_concrete(path), data={"x": "1"}, follow_redirects=False)
        assert r.status_code == 403, path


def test_redirects_stay_relative_without_forwarded_scheme(app: FastAPI, client: TestClient) -> None:
    """uvicorn no longer trusts X-Forwarded-Proto, so behind Caddy the app sees plain http.
    Every redirect must still be a relative path (never an absolute http:// URL), and the
    __Host- cookies must keep their Secure flag."""
    session, csrf = client.cookies.get(SESSION_COOKIE), client.cookies.get(CSRF_COOKIE)
    assert session and csrf
    cookie = {"cookie": f"{SESSION_COOKIE}={session}; {CSRF_COOKIE}={csrf}"}
    plain = TestClient(app, base_url="http://testserver", follow_redirects=False)
    anon = TestClient(app, base_url="http://testserver", follow_redirects=False)
    responses = [
        anon.get("/", headers={"X-Forwarded-Proto": "https"}),
        anon.get("/settings?x=1"),
        plain.get("/login", headers=cookie),
        plain.get("/settings/", headers=cookie),
        plain.get("/sources/", headers=cookie),
        plain.post("/logout", data={"csrf_token": csrf}, headers=cookie),
    ]
    locations = [r.headers.get("location") for r in responses]
    assert all(loc is None or loc.startswith("/") for loc in locations), locations
    assert sum(loc is not None for loc in locations) >= 4
    fresh = anon.get("/login")
    set_cookie = [v.lower() for v in fresh.headers.get_list("set-cookie")]
    assert any(v.startswith(CSRF_COOKIE.lower()) and "secure" in v for v in set_cookie)
