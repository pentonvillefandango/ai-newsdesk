from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk import taste
from ai_newsdesk.app import create_app
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.normalise import canonical_url
from tests.web.fakes import fake_ctx_factory
from tests.web.support import BASE_URL, CONFIG_DIR, NOW, make_settings, run


class FakeScheduler:
    """Stands in for step 3's Scheduler: same constructor, start(), async stop(), lock, alive."""

    events: ClassVar[list[str]] = []

    def __init__(self, ctx: object, **kwargs: Any) -> None:
        self.ctx = ctx
        self.kwargs = kwargs
        self.lock = asyncio.Lock()
        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive

    def start(self) -> None:
        self._alive = True
        FakeScheduler.events.append("start")

    async def stop(self) -> None:
        self._alive = False
        FakeScheduler.events.append("stop")


def test_health_is_open_and_reports_state(anon: TestClient) -> None:
    r = anon.get("/health")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "status": "up",
        "db": True,
        "scheduler": False,
        "last_run": None,
        "git_sha": "abc1234",
    }


def test_health_reports_the_last_run(app: FastAPI, anon: TestClient) -> None:
    store = app.state.store
    run_id = run(anon, store.start_run, "manual", NOW)
    run(anon, store.finish_run, run_id, NOW, {"collected": 3}, 0.01, None)
    last = anon.get("/health").json()["last_run"]
    assert last["trigger"] == "manual"
    assert last["error"] is None
    assert set(last) == {"trigger", "started_at", "finished_at", "cost_usd", "error"}


def test_health_is_503_when_the_database_fails(
    app: FastAPI, anon: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk gone")

    monkeypatch.setattr(app.state.store, "get_setting", boom)
    r = anon.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "down"
    assert r.json()["db"] is False


def test_start_up_seeds_sources(app: FastAPI, anon: TestClient) -> None:
    sources = run(anon, app.state.store.list_sources, enabled_only=False)
    assert len(sources) > 0


def test_ctx_factory_replaces_the_real_wiring(app: FastAPI, anon: TestClient) -> None:
    assert app.state.ctx.clock.now() == NOW
    assert app.state.store.clock is app.state.ctx.clock
    assert app.state.store.canonicalise is canonical_url
    # Built but never started: SCHEDULER_ENABLED is false in the test settings.
    assert app.state.scheduler.alive is False
    assert app.state.scheduler.ctx is app.state.ctx
    assert app.state.run_lock is app.state.scheduler.lock


def test_api_docs_are_off(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_scheduler_starts_and_stops_with_the_nightly_taste_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeScheduler.events = []
    monkeypatch.setattr("ai_newsdesk.app.Scheduler", FakeScheduler)
    app = create_app(
        make_settings(tmp_path, scheduler_enabled=True),
        ctx_factory=fake_ctx_factory,
        key_testers={},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
    )
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/health").json()["scheduler"] is True
        assert app.state.run_lock is app.state.scheduler.lock
        assert app.state.scheduler.kwargs == {"nightly_fn": taste.nightly}
    assert FakeScheduler.events == ["start", "stop"]


def test_disabled_scheduler_is_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeScheduler.events = []
    monkeypatch.setattr("ai_newsdesk.app.Scheduler", FakeScheduler)
    app = create_app(
        make_settings(tmp_path, scheduler_enabled=False),
        ctx_factory=fake_ctx_factory,
        key_testers={},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
    )
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/health").json()["scheduler"] is False
    assert FakeScheduler.events == ["stop"]


def test_rebuild_context_hands_the_new_context_to_the_scheduler(
    app: FastAPI, anon: TestClient
) -> None:
    before = app.state.ctx
    run(anon, app.state.rebuild_context)
    assert app.state.ctx is not before
    assert app.state.scheduler.ctx is app.state.ctx
