from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.app import create_app, nightly_jobs
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.normalise import canonical_url
from tests.web.fakes import fake_ctx_factory
from tests.web.support import BASE_URL, CONFIG_DIR, NOW, fixture_catalogue, make_settings, run


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
        catalogue_fetch=fixture_catalogue,
    )
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/health").json()["scheduler"] is True
        assert app.state.run_lock is app.state.scheduler.lock
        assert set(app.state.scheduler.kwargs) == {"nightly_fn"}
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
        catalogue_fetch=fixture_catalogue,
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


# ---- multi-provider: the model catalogue at start-up and in the nightly chain ---------------


def _app(tmp_path: Path, fetch: Any, **overrides: Any) -> FastAPI:
    return create_app(
        make_settings(tmp_path, **overrides),
        ctx_factory=fake_ctx_factory,
        key_testers={},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
        catalogue_fetch=fetch,
    )


def _settle(client: TestClient, app: FastAPI) -> None:
    """Wait for the start-up background tasks (the catalogue refresh) to finish."""

    async def wait() -> None:
        await asyncio.gather(*list(app.state.background), return_exceptions=True)

    run(client, wait)


def test_startup_refreshes_a_missing_catalogue_in_the_background(tmp_path: Path) -> None:
    calls: list[int] = []

    async def fetch() -> object:
        calls.append(1)
        return await fixture_catalogue()

    app = _app(tmp_path, fetch)
    with TestClient(app, base_url=BASE_URL) as c:
        _settle(c, app)
        assert calls == [1]
        assert app.state.catalogue.current is not None
        assert "openai/gpt-5.4-nano" in app.state.catalogue.current.ids()
        # The shared PriceBook sees the refreshed catalogue straight away.
        assert app.state.prices.is_priced("openrouter", "google/gemini-3.8-flash")
        assert app.state.deps.prices is app.state.prices
        assert app.state.ctx.budget is not None

    # A fresh stored copy is loaded at the next start-up and not fetched again.
    again = _app(tmp_path, fetch)
    with TestClient(again, base_url=BASE_URL) as c:
        _settle(c, again)
        assert calls == [1]
        assert again.state.catalogue.current is not None


def test_a_failed_startup_refresh_never_stops_the_app(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def down() -> object:
        raise ConnectionError("no route")

    app = _app(tmp_path, down)
    with TestClient(app, base_url=BASE_URL) as c:
        _settle(c, app)
        assert c.get("/health").status_code == 200
        assert app.state.catalogue.current is None
        assert app.state.prices.is_priced("anthropic", "claude-haiku-5-5")
        assert not app.state.prices.is_priced("openrouter", "google/gemini-3.8-flash")
    assert "model catalogue refresh failed" in caplog.text


def test_real_context_budget_prices_through_the_shared_price_book(tmp_path: Path) -> None:
    app = create_app(
        make_settings(tmp_path),
        key_testers={},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
        catalogue_fetch=fixture_catalogue,
    )
    with TestClient(app, base_url=BASE_URL) as c:
        _settle(c, app)
        assert app.state.ctx.budget.prices is app.state.prices


async def test_nightly_jobs_run_taste_then_refresh_the_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def fake_taste(ctx: object) -> None:
        order.append("taste")
        raise RuntimeError("taste failed")

    class Cat:
        async def refresh(self) -> bool:
            order.append("catalogue")
            return True

    monkeypatch.setattr("ai_newsdesk.app.taste.nightly", fake_taste)
    job = nightly_jobs(Cat())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await job(object())  # type: ignore[arg-type]
    assert order == ["taste", "catalogue"]  # a failed taste refit never skips the refresh
