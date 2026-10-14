"""Guards every route added in later tasks: session required, CSRF required on POST."""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
