from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.app import create_app
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import Purpose, Usage
from ai_newsdesk.runtime_settings import (
    API_KEY_NAMES,
    RuntimeSettings,
    load_runtime_settings,
    update_runtime_settings,
)
from tests.web.fakes import fake_ctx_factory
from tests.web.support import (
    BASE_URL,
    CONFIG_DIR,
    NOW,
    csrf_token,
    fixture_catalogue,
    login,
    make_settings,
    run,
)

SECRET = "sk-test-SECRET-123"  # gitleaks:allow
VALID = {
    "runs_per_day": "4",
    "briefing_time": "07:00",
    "daily_review_cap": "500",
    "monthly_budget_usd": "5.00",
    "judge_provider": "anthropic",
    "judge_model": "claude-haiku-5-5",
    "writer_model": "claude-haiku-5-5",
    "briefing_model": "claude-haiku-5-5",
    "show_threshold": "4",
    "why_it_matters": "on",
    "hn_min_points": "100",
}


def _post(client: TestClient, path: str, data: dict[str, str]) -> object:
    return client.post(
        path, data={"csrf_token": csrf_token(client), **data}, follow_redirects=False
    )


async def _drain(app: FastAPI) -> None:
    await asyncio.gather(*list(app.state.background))


async def _release(lock: asyncio.Lock) -> None:
    lock.release()


def _resolve(client: TestClient, app: FastAPI, name: str) -> str:
    return run(client, app.state.api_keys.resolve_api_key, name)


def test_settings_page_shows_estimates_models_and_spend(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    for text in (
        "4 times a day, about $2.07 a month",
        "Once a day, about $0.63 a month",
        "claude-haiku-5-5 ($0.1 in, $0.5 out per million tokens)",
        "of $5.00 spent in October",
        'role="img"',
        "Show as a table",
        "Refresh now",
        'aria-current="page">Settings',
        'name="why_it_matters" type="checkbox" checked',
    ):
        assert text in r.text, text


def test_saving_valid_settings(app: FastAPI, client: TestClient) -> None:
    before = app.state.ctx
    data = {
        **VALID,
        "runs_per_day": "6",
        "briefing_time": "06:30",
        "monthly_budget_usd": "7.5",
        "show_threshold": "5",
    }
    del data["why_it_matters"]
    r = client.post(
        "/settings", data={"csrf_token": csrf_token(client), **data}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=settings"
    rt = run(client, load_runtime_settings, app.state.store)
    assert (rt.runs_per_day, rt.briefing_time, rt.monthly_budget_usd, rt.show_threshold) == (
        6,
        "06:30",
        7.5,
        5,
    )
    assert rt.why_it_matters is False
    assert app.state.ctx is not before
    assert "Settings saved." in client.get(r.headers["location"]).text


def test_invalid_frequency_is_rejected(app: FastAPI, client: TestClient) -> None:
    r = client.post(
        "/settings", data={"csrf_token": csrf_token(client), **VALID, "runs_per_day": "5"}
    )
    assert r.status_code == 422
    assert "Choose one of the listed frequencies." in r.text
    assert "Nothing was saved." in r.text
    assert run(client, load_runtime_settings, app.state.store).runs_per_day == 4


def test_other_invalid_values_are_rejected(client: TestClient) -> None:
    for field, bad, message in (
        ("briefing_time", "25:00", "Use the 24-hour form HH:MM, for example 07:00."),
        ("monthly_budget_usd", "-1", "Enter an amount from 0 to 100 dollars."),
        ("writer_model", "gpt-5", "Choose a Claude model from the list."),
    ):
        r = client.post("/settings", data={"csrf_token": csrf_token(client), **VALID, field: bad})
        assert r.status_code == 422, field
        assert message in r.text, field
        assert f'value="{bad}"' in r.text or field == "writer_model", field


def test_settings_posts_need_csrf(client: TestClient) -> None:
    assert client.post("/settings", data=VALID).status_code == 403
    assert client.post("/settings/keys/anthropic", data={"key": SECRET}).status_code == 403
    assert client.post("/settings/keys/anthropic/clear").status_code == 403
    assert client.post("/settings/refresh").status_code == 403


def test_api_key_is_write_only(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    r = client.post(
        "/settings/keys/anthropic",
        data={"csrf_token": csrf_token(client), "key": SECRET},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=key#key-anthropic"
    page = client.get(r.headers["location"]).text
    assert SECRET not in page
    assert "SECRET" not in page
    assert "Key tested and saved." in page
    assert "Saved here" in page
    stored = run(client, store.get_setting, "secret.anthropic")  # step 1's {"token", "updated_at"}
    assert isinstance(stored, dict) and stored["token"] and SECRET not in json.dumps(stored)
    assert _resolve(client, app, "anthropic") == SECRET
    for path in ("/", "/sources", "/briefing", "/health", "/settings?saved=key"):
        assert SECRET not in client.get(path).text, path


def test_failed_key_test_refuses_to_save_and_hides_the_key(
    app: FastAPI, client: TestClient, key_results: dict[str, str | None]
) -> None:
    key_results["anthropic"] = f"Anthropic answered 401: invalid x-api-key {SECRET}"
    r = client.post(
        "/settings/keys/anthropic", data={"csrf_token": csrf_token(client), "key": SECRET}
    )
    assert r.status_code == 422
    assert SECRET not in r.text
    assert "SECRET" not in r.text
    assert "Anthropic answered 401: invalid x-api-key [key removed]" in r.text
    assert run(client, app.state.store.get_setting, "secret.anthropic") is None


def test_empty_key_and_unknown_key_name(client: TestClient) -> None:
    r = client.post(
        "/settings/keys/anthropic", data={"csrf_token": csrf_token(client), "key": "  "}
    )
    assert r.status_code == 422
    assert "Paste a key first." in r.text
    r = client.post("/settings/keys/google", data={"csrf_token": csrf_token(client), "key": "x"})
    assert r.status_code == 404


def test_clear_key_falls_back_to_env(tmp_path: Path) -> None:
    async def ok(key: str, runtime: RuntimeSettings) -> str | None:
        return None

    app = create_app(
        make_settings(tmp_path, anthropic_api_key="sk-env-fallback-key"),
        ctx_factory=fake_ctx_factory,
        key_testers={name: ok for name in API_KEY_NAMES},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
        catalogue_fetch=fixture_catalogue,
    )
    with TestClient(app, base_url=BASE_URL) as c:
        login(c)
        c.post("/settings/keys/anthropic", data={"csrf_token": csrf_token(c), "key": SECRET})
        assert _resolve(c, app, "anthropic") == SECRET
        r = c.post(
            "/settings/keys/anthropic/clear",
            data={"csrf_token": csrf_token(c)},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/settings?saved=cleared#key-anthropic"
        assert _resolve(c, app, "anthropic") == "sk-env-fallback-key"
        page = c.get("/settings").text
        assert "Using the value from .env." in page
        assert "sk-env-fallback-key" not in page
        assert SECRET not in page


def test_typesafe_key_box_is_always_shown(app: FastAPI, client: TestClient) -> None:
    """Owner feedback 1: the key can be entered before switching the judge to Jev."""
    page = client.get("/settings").text
    assert 'id="key-typesafe"' in page
    assert 'action="/settings/keys/typesafe"' in page
    assert 'id="key-input-typesafe"' in page
    run(client, update_runtime_settings, app.state.store, judge_provider="typesafe")
    assert 'id="key-typesafe"' in client.get("/settings").text


def test_refresh_now_runs_the_scheduler_in_the_background(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_run_now(trigger: str = "manual") -> None:
        calls.append(trigger)

    monkeypatch.setattr(app.state.scheduler, "run_now", fake_run_now)
    r = client.post(
        "/settings/refresh", data={"csrf_token": csrf_token(client)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?run=started#run"
    run(client, _drain, app)
    assert calls == ["manual"]
    assert "Run started." in client.get(r.headers["location"]).text


def test_refresh_now_waits_for_a_running_run(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_run_now(trigger: str = "manual") -> None:
        calls.append(trigger)

    monkeypatch.setattr(app.state.scheduler, "run_now", fake_run_now)
    lock: asyncio.Lock = app.state.scheduler.lock
    run(client, lock.acquire)
    try:
        r = client.post(
            "/settings/refresh", data={"csrf_token": csrf_token(client)}, follow_redirects=False
        )
        assert r.headers["location"] == "/settings?run=busy#run"
        assert "A run is already going." in client.get(r.headers["location"]).text
    finally:
        run(client, _release, lock)
    assert calls == []


def test_spend_meter_chart_and_last_runs(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    usage = Usage(
        provider="anthropic", model="claude-haiku-5-5", purpose=Purpose.REVIEW, input_tokens=100
    )
    run(client, store.add_spend, usage, 0.12, NOW - timedelta(hours=1))
    run_id = run(client, store.start_run, "manual", NOW - timedelta(minutes=30))
    run(client, store.finish_run, run_id, NOW - timedelta(minutes=29), {}, 0.12, "boom <x>")
    text = client.get("/settings").text
    assert '<span class="meter__spent">$0.12</span>' in text
    assert 'class="chart__bar"' in text
    assert 'class="chart" width="100%" preserveAspectRatio="none" viewBox="0 0 ' in text
    assert "Wed 14 Oct: $0.12" in text
    assert "boom &lt;x&gt;" in text
    assert "30 min ago" in text


def test_judge_model_must_match_judge_provider(app: FastAPI, client: TestClient) -> None:
    bad = {**VALID, "judge_provider": "typesafe", "judge_model": "claude-haiku-5-5"}
    r = client.post("/settings", data={"csrf_token": csrf_token(client), **bad})
    assert r.status_code == 422
    assert "Choose a model that the selected judge provider offers." in r.text
    assert run(client, load_runtime_settings, app.state.store).judge_provider == "anthropic"
    bad = {**VALID, "judge_provider": "anthropic", "judge_model": "jev-1.13.0"}
    assert (
        client.post("/settings", data={"csrf_token": csrf_token(client), **bad}).status_code == 422
    )
    good = {**VALID, "judge_provider": "typesafe", "judge_model": "jev-1.13.0"}
    r = client.post(
        "/settings", data={"csrf_token": csrf_token(client), **good}, follow_redirects=False
    )
    assert r.status_code == 303
    assert run(client, load_runtime_settings, app.state.store).judge_model == "jev-1.13.0"


def test_saving_keeps_fields_the_page_does_not_edit(app: FastAPI, client: TestClient) -> None:
    run(client, app.state.store.set_setting, "blocklist", ["keepme"])
    r = client.post(
        "/settings", data={"csrf_token": csrf_token(client), **VALID}, follow_redirects=False
    )
    assert r.status_code == 303
    assert run(client, app.state.store.get_setting, "blocklist") == ["keepme"]


def test_a_crashing_key_tester_never_leaks_the_key(app: FastAPI, client: TestClient) -> None:
    async def boom(key: str, runtime: RuntimeSettings) -> str | None:
        raise RuntimeError(f"bad {key}")

    app.state.key_testers["anthropic"] = boom
    r = client.post(
        "/settings/keys/anthropic", data={"csrf_token": csrf_token(client), "key": SECRET}
    )
    assert r.status_code == 422
    assert SECRET not in r.text
    assert "The test call failed (RuntimeError)." in r.text
    assert run(client, app.state.store.get_setting, "secret.anthropic") is None


async def test_dev_seed_fills_a_database(tmp_path: Path) -> None:
    from ai_newsdesk.store import Store
    from tests.web import dev_seed

    db = tmp_path / "dev.sqlite"
    await dev_seed.main(str(db))
    store = Store(str(db))
    await store.init()
    try:
        stories = await store.today_stories(datetime.now(UTC), 0)
        assert len(stories) >= 1
        assert await store.latest_briefing() is not None
        assert await store.month_spend(datetime.now(UTC), "Europe/London") > 0
        assert len(await store.last_runs(5)) == 3
    finally:
        await store.close()


# ---- multi-provider: model choices are validated, keys are checked, spend is shared ----------


def test_saving_an_openrouter_model_needs_the_key_first(app: FastAPI, client: TestClient) -> None:
    run(client, _drain, app)  # the start-up catalogue refresh
    choice = {
        **VALID,
        "writer_provider": "openrouter",
        "writer_model": "google/gemini-3.8-flash",
    }
    r = _post(client, "/settings", choice)
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "Add your OpenRouter API key first." in r.text  # type: ignore[attr-defined]
    assert run(client, load_runtime_settings, app.state.store).writer_provider == "anthropic"

    _post(client, "/settings/keys/openrouter", {"key": "sk-or-" + "k" * 30})
    r = _post(client, "/settings", choice)
    assert r.status_code == 303  # type: ignore[attr-defined]
    rt = run(client, load_runtime_settings, app.state.store)
    assert (rt.writer_provider, rt.writer_model) == ("openrouter", "google/gemini-3.8-flash")


def test_unpriced_and_free_models_are_refused(app: FastAPI, client: TestClient) -> None:
    run(client, _drain, app)
    for provider, model, message in (
        ("openrouter", "nvidia/nemotron-3.5-lightning:free", "isn&#39;t in the OpenRouter model"),
        ("openai", "gpt-unknown", "no known price"),
        ("anthropic", "anthropic/claude-haiku-5.5", "Choose a Claude model from the list."),
    ):
        data = {**VALID, "consolidator_provider": provider, "consolidator_model": model}
        r = _post(client, "/settings", data)
        assert r.status_code == 422, model  # type: ignore[attr-defined]
        assert message in r.text, model  # type: ignore[attr-defined]


def test_spend_from_every_provider_counts_in_the_meter_and_estimates(
    app: FastAPI, client: TestClient
) -> None:
    store = app.state.store
    for provider, model, purpose, usd in (
        ("anthropic", "claude-haiku-5-5", Purpose.REVIEW, 0.10),
        ("openrouter", "google/gemini-3.8-flash", Purpose.REVIEW, 0.20),
        ("openai", "gpt-5.4-nano", Purpose.WRITE, 0.30),
        ("openrouter", "openai/gpt-5.4-nano", Purpose.BRIEFING, 0.40),
    ):
        usage = Usage(provider=provider, model=model, purpose=purpose, input_tokens=100)
        run(client, store.add_spend, usage, usd, NOW - timedelta(hours=1))
    stats = run(client, store.usage_last_days, NOW)
    assert (stats.review_usd, stats.write_usd, stats.briefing_usd) == pytest.approx(
        (0.30, 0.30, 0.40)
    )
    r = client.get("/settings")
    assert r.status_code == 200
    assert '<span class="meter__spent">$1.00</span>' in r.text
