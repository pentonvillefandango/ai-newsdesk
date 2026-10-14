"""Shared helpers for the web tests: settings, login, CSRF and running coroutines."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from ai_newsdesk.config import Settings
from ai_newsdesk.web.csrf import CSRF_COOKIE

NOW = datetime(2026, 10, 14, 8, 30, tzinfo=UTC)
TEST_PASSWORD = "correct horse battery staple"
# Cheap parameters keep the tests fast; verify() reads the parameters from the hash itself.
TEST_HASH = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(TEST_PASSWORD)
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
BASE_URL = "https://testserver"  # https so the Secure cookies are sent back
HX = {"HX-Request": "true"}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "site_host": "ai-newsdesk.localhost",
        "secrets_key": "s" * 44,
        "session_secret": "k" * 44,
        "owner_password_hash": TEST_HASH,
        "anthropic_api_key": "",
        "research_engine_api_key": "",
        "typesafe_api_key": "",
        "research_engine_url": "http://research-engine.invalid:8000",
        "db_path": str(tmp_path / "web.sqlite"),
        "tz": "Europe/London",
        "log_level": "INFO",
        "git_sha": "abc1234",
        "scheduler_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def csrf_token(client: TestClient) -> str:
    """The CSRF cookie value (fetching /login first if the client has none yet)."""
    if client.cookies.get(CSRF_COOKIE) is None:
        client.get("/login")
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return token


def login(client: TestClient, password: str = TEST_PASSWORD) -> None:
    response = client.post(
        "/login",
        data={"password": password, "next": "/", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def api_routes(app: FastAPI) -> list[APIRoute]:
    """Every APIRoute, including those of included routers.

    FastAPI >= 0.142 keeps an included router as one lazy entry in `app.routes` (its routes are
    reached through `original_router`), so walk the tree instead of reading `app.routes` flat.
    """
    found: list[APIRoute] = []

    def walk(routes: list[Any]) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif (inner := getattr(route, "original_router", None)) is not None:
                walk(list(inner.routes))

    walk(list(app.routes))
    return found


def run[T](client: TestClient, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """Run an async callable on the app's event loop (the Store's connection lives there)."""
    portal = client.portal
    assert portal is not None, "use the TestClient inside its `with` block"
    return portal.call(functools.partial(fn, *args, **kwargs))
