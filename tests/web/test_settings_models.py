"""Settings' Models section (multi-provider 3.5): per-job provider and model rows, manual
entry, the Test button, the judge warning and the model-list refresh."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.app import AppDeps, create_app
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.runtime_settings import (
    API_KEY_NAMES,
    RuntimeSettings,
    load_runtime_settings,
    update_runtime_settings,
)
from ai_newsdesk.web.model_choices import JUDGE_WARNING
from tests.fake_openai import completion
from tests.web.fakes import fake_ctx_factory
from tests.web.support import (
    BASE_URL,
    CONFIG_DIR,
    HX,
    NOW,
    csrf_token,
    fixture_catalogue,
    login,
    make_settings,
    run,
)

OPENROUTER_KEY = "sk-or-" + "r" * 40
OPENAI_KEY = "sk-oa-" + "o" * 40
JOBS = ("judge", "writer", "briefing", "consolidator")


async def _drain(app: FastAPI) -> None:
    await asyncio.gather(*list(app.state.background))


class _Form(HTMLParser):
    """What a browser would submit for the form whose action is /settings, unchanged."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self._in_form = False
        self._select: str | None = None
        self._first: str | None = None
        self._chosen: str | None = None
        self._optgroup_disabled = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self._in_form = a.get("action") == "/settings"
        if not self._in_form:
            return
        name = a.get("name")
        if tag == "input" and name and a.get("type") not in ("submit", "button"):
            if a.get("type") == "checkbox":
                if "checked" in a:
                    self.fields[name] = "on"
            else:
                self.fields[name] = a.get("value") or ""
        elif tag == "select" and name:
            self._select, self._first, self._chosen = name, None, None
        elif tag == "option" and self._select is not None and "disabled" not in a:
            value = a.get("value") or ""
            if self._first is None:
                self._first = value
            if "selected" in a:
                self._chosen = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False
        elif tag == "select" and self._select is not None:
            self.fields[self._select] = (
                self._chosen if self._chosen is not None else self._first or ""
            )
            self._select = None


def browser_form(html: str) -> dict[str, str]:
    parser = _Form()
    parser.feed(html)
    return parser.fields


def _post(client: TestClient, path: str, data: dict[str, str], **kw: Any) -> Any:
    return client.post(
        path, data={**data, "csrf_token": csrf_token(client)}, follow_redirects=False, **kw
    )


def _save_keys(client: TestClient) -> None:
    for name, key in (("openrouter", OPENROUTER_KEY), ("openai", OPENAI_KEY)):
        assert _post(client, f"/settings/keys/{name}", {"key": key}).status_code == 303


def _settings(client: TestClient, app: FastAPI) -> RuntimeSettings:
    return run(client, load_runtime_settings, app.state.store)


# ---- rows, shortlists, datalists --------------------------------------------------------------


def test_one_row_per_job_with_its_providers(app: FastAPI, client: TestClient) -> None:
    run(client, _drain, app)
    page = client.get("/settings").text
    for job, label in (
        ("judge", "Judge"),
        ("writer", "Summary writer"),
        ("briefing", "Briefing writer"),
        ("consolidator", "Duplicate merger"),
    ):
        assert f'id="model-{job}"' in page, job
        assert f">{label}<" in page, job
        select = re.search(
            rf'<select id="{job}_provider" name="{job}_provider".*?</select>', page, re.S
        )
        assert select is not None, job
        values = re.findall(r'<option value="([a-z]+)"', select.group(0))
        expected = ["anthropic", "openai", "openrouter"] + (["typesafe"] if job == "judge" else [])
        assert values == expected, job
        assert f'name="{job}_model"' in page
        assert f'name="{job}_model_other"' in page
        assert f'formaction="/settings/models/test" name="test_job" value="{job}"' in page
    assert "TypeSafe (Jev)" in page


def test_shortlists_with_prices_and_datalists(app: FastAPI, client: TestClient) -> None:
    run(client, _drain, app)  # the start-up catalogue refresh (fixture)
    page = client.get("/settings").text
    assert "claude-haiku-5-5 ($0.1 in, $0.5 out per million tokens)" in page
    assert "google/gemini-3.8-flash ($0.75 in, $3.75 out per million tokens)" in page
    assert "gpt-5.4-nano ($0.2 in, $1.25 out per million tokens)" in page
    assert re.search(r'<option value="gpt-6-luna" disabled>gpt-6-luna \(no price yet\)', page)
    openrouter = re.search(r'<datalist id="models-openrouter">(.*?)</datalist>', page, re.S)
    openai = re.search(r'<datalist id="models-openai">(.*?)</datalist>', page, re.S)
    assert openrouter is not None and openai is not None
    or_ids = re.findall(r'<option value="([^"]+)"', openrouter.group(1))
    assert "google/gemini-3.8-flash" in or_ids and "unbiased/pareto-26.9" not in or_ids
    assert not any(i.endswith(":free") for i in or_ids)
    assert re.findall(r'<option value="([^"]+)"', openai.group(1)) == ["gpt-5.4-nano"]
    # each text box starts on its provider's list
    assert 'name="judge_model_other" type="text" list="models-anthropic"' in page


def test_catalogue_age_and_refresh_button(app: FastAPI, client: TestClient) -> None:
    page = client.get("/settings").text
    run(client, _drain, app)
    page = client.get("/settings").text
    assert "Model list updated just now" in page
    assert 'action="/settings/models/refresh"' in page
    assert "Refresh model list" in page


def test_model_list_not_loaded_yet(tmp_path: Path) -> None:
    async def failing() -> object:
        raise httpx.ConnectError("offline")

    app = _app(tmp_path, catalogue_fetch=failing)
    with TestClient(app, base_url=BASE_URL) as c:
        login(c)
        run(c, _drain, app)
        assert "Model list not loaded yet" in c.get("/settings").text


# ---- saving ----------------------------------------------------------------------------------


def test_saving_an_unchanged_page_keeps_every_provider_and_model(
    app: FastAPI, client: TestClient
) -> None:
    """Carry-over M4: the form posts each job's provider and model explicitly."""
    run(client, _drain, app)
    _save_keys(client)
    chosen = {
        "judge_provider": "openrouter",
        "judge_model": "google/gemini-3.8-flash",
        "writer_provider": "openai",
        "writer_model": "gpt-5.4-nano",
        "briefing_provider": "openrouter",
        "briefing_model": "example/costly-cache-write",  # catalogue only: the text box
        "consolidator_provider": "anthropic",
        "consolidator_model": "claude-sonnet-5-5",
    }
    run(client, update_runtime_settings, app.state.store, **chosen)
    form = browser_form(client.get("/settings").text)
    assert form["briefing_model_other"] == "example/costly-cache-write"
    assert form["judge_model"] == "google/gemini-3.8-flash" and form["judge_model_other"] == ""
    r = _post(client, "/settings", form)
    assert r.status_code == 303, r.text
    saved = _settings(client, app)
    assert {k: getattr(saved, k) for k in chosen} == chosen


def test_saving_the_defaults_unchanged(app: FastAPI, client: TestClient) -> None:
    before = _settings(client, app)
    r = _post(client, "/settings", browser_form(client.get("/settings").text))
    assert r.status_code == 303, r.text
    after = _settings(client, app)
    for job in JOBS:
        for field in ("provider", "model"):
            key = f"{job}_{field}"
            assert getattr(after, key) == getattr(before, key), key


def test_an_openrouter_judge_is_kept(app: FastAPI, client: TestClient) -> None:
    run(client, _drain, app)
    _save_keys(client)
    form = browser_form(client.get("/settings").text)
    form.update(judge_provider="openrouter", judge_model="google/gemini-3.8-flash")
    assert _post(client, "/settings", form).status_code == 303
    saved = _settings(client, app)
    assert (saved.judge_provider, saved.judge_model) == ("openrouter", "google/gemini-3.8-flash")
    again = browser_form(client.get("/settings").text)
    assert again["judge_provider"] == "openrouter"
    assert _post(client, "/settings", again).status_code == 303
    assert _settings(client, app).judge_provider == "openrouter"


def test_the_typed_model_wins_over_the_list(app: FastAPI, client: TestClient) -> None:
    run(client, _drain, app)
    _save_keys(client)
    form = browser_form(client.get("/settings").text)
    form.update(
        writer_provider="openrouter",
        writer_model="anthropic/claude-haiku-5.5",  # the list's choice loses
        writer_model_other=" example/costly-cache-write ",
    )
    assert _post(client, "/settings", form).status_code == 303
    saved = _settings(client, app)
    assert (saved.writer_provider, saved.writer_model) == (
        "openrouter",
        "example/costly-cache-write",
    )


def test_errors_are_shown_next_to_their_row(app: FastAPI, client: TestClient) -> None:
    run(client, _drain, app)
    form = browser_form(client.get("/settings").text)
    form.update(
        briefing_provider="openrouter",
        briefing_model="",
        briefing_model_other="nobody/unknown",
        consolidator_provider="openai",
        consolidator_model="gpt-5.4-nano",
    )
    r = _post(client, "/settings", form)
    assert r.status_code == 422
    page = r.text
    assert 'id="briefing_model-error"' in page
    assert "isn&#39;t in the OpenRouter model list" in page
    assert re.search(
        r'name="briefing_model_other"[^>]*aria-describedby="[^"]*briefing_model-error', page
    )
    assert 'id="consolidator_provider-error"' in page
    assert "Add your OpenAI API key first." in page
    assert re.search(
        r'id="consolidator_provider"[^>]*aria-describedby="[^"]*consolidator_provider-error', page
    )
    # the echo keeps what was typed and chosen
    assert 'value="nobody/unknown"' in page
    assert re.search(r'<option value="openai" selected>OpenAI</option>', page)
    assert _settings(client, app).briefing_provider == "anthropic"


# ---- judge warning ---------------------------------------------------------------------------


def test_judge_warning_shown_only_off_haiku(app: FastAPI, client: TestClient) -> None:
    warning = JUDGE_WARNING.replace("'", "&#39;")
    assert warning not in client.get("/settings").text
    run(client, update_runtime_settings, app.state.store, judge_model="claude-sonnet-5-5")
    assert warning in client.get("/settings").text
    run(
        client,
        update_runtime_settings,
        app.state.store,
        judge_provider="typesafe",
        judge_model="jev-1.13.0",
    )
    assert warning in client.get("/settings").text


# ---- the Test button -------------------------------------------------------------------------


class Provider:
    """A fake chat-completions endpoint for the Test button's SDK clients."""

    def __init__(self, reply: httpx.Response | None = None) -> None:
        self.reply = reply
        self.requests: list[httpx.Request] = []
        self.on_request: Any = None  # called with each request, before the reply

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        if self.reply is not None:
            return self.reply
        body = completion(
            json.dumps({"ok": True}), prompt_tokens=60, completion_tokens=5, cost=0.0007
        )
        return httpx.Response(200, json=body)


async def _ok(key: str, runtime: RuntimeSettings) -> str | None:
    return None


def _app(tmp_path: Path, provider: Provider | None = None, **kw: Any) -> FastAPI:
    http = (
        httpx.AsyncClient(transport=httpx.MockTransport(provider.handle))
        if provider is not None
        else None
    )
    options: dict[str, Any] = {
        "ctx_factory": fake_ctx_factory,
        "key_testers": {name: _ok for name in API_KEY_NAMES},
        "config_dir": CONFIG_DIR,
        "clock": FixedClock(NOW),
        "catalogue_fetch": fixture_catalogue,
        "model_http_client": http,
    }
    options.update(kw)
    return create_app(make_settings(tmp_path), **options)


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def tapp(tmp_path: Path, provider: Provider) -> FastAPI:
    return _app(tmp_path, provider)


@pytest.fixture
def tclient(tapp: FastAPI) -> Iterator[TestClient]:
    with TestClient(tapp, base_url=BASE_URL) as c:
        login(c)
        run(c, _drain, tapp)
        yield c


def _test_form(client: TestClient, job: str, **row: str) -> dict[str, str]:
    form = browser_form(client.get("/settings").text)
    form.update(row)
    form["test_job"] = job
    return form


def test_the_test_button_needs_csrf(tclient: TestClient, provider: Provider) -> None:
    form = _test_form(tclient, "writer")
    del form["csrf_token"]
    r = tclient.post("/settings/models/test", data=form, follow_redirects=False)
    assert r.status_code == 403
    assert provider.requests == []


def test_a_missing_key_is_refused_without_a_call(tclient: TestClient, provider: Provider) -> None:
    form = _test_form(
        tclient,
        "writer",
        writer_provider="openai",
        writer_model="gpt-5.4-nano",
        writer_model_other="",
    )
    r = _post(tclient, "/settings/models/test", form, headers=HX)
    assert r.status_code == 200
    assert "Add your OpenAI API key first." in r.text
    assert "Not tested" in r.text
    assert "<html" not in r.text  # a partial
    assert provider.requests == []


def test_an_invalid_model_is_refused_without_a_call(
    tclient: TestClient, provider: Provider
) -> None:
    _save_keys(tclient)
    form = _test_form(
        tclient, "judge", judge_provider="openrouter", judge_model_other="nobody/unknown"
    )
    r = _post(tclient, "/settings/models/test", form, headers=HX)
    assert "isn&#39;t in the OpenRouter model list" in r.text
    assert provider.requests == []


def test_an_unaffordable_test_is_refused(
    tapp: FastAPI, tclient: TestClient, provider: Provider
) -> None:
    _save_keys(tclient)
    run(tclient, update_runtime_settings, tapp.state.store, monthly_budget_usd=0.0)
    form = _test_form(
        tclient,
        "writer",
        writer_provider="openrouter",
        writer_model_other="google/gemini-3.8-flash",
    )
    r = _post(tclient, "/settings/models/test", form, headers=HX)
    assert "budget can&#39;t cover a test call" in r.text
    assert provider.requests == []


def test_a_working_model_records_its_spend(
    tapp: FastAPI, tclient: TestClient, provider: Provider
) -> None:
    _save_keys(tclient)
    form = _test_form(
        tclient,
        "briefing",
        briefing_provider="openrouter",
        briefing_model="google/gemini-3.8-flash",
        briefing_model_other="",
    )
    r = _post(tclient, "/settings/models/test", form, headers=HX)
    assert r.status_code == 200
    assert "It works." in r.text
    assert " ms" in r.text
    assert "$0.75 in, $3.75 out per million tokens" in r.text
    [request] = provider.requests
    assert request.headers["authorization"] == f"Bearer {OPENROUTER_KEY}"
    body = json.loads(request.content)
    assert body["model"] == "google/gemini-3.8-flash"
    assert body["response_format"]["type"] == "json_schema"
    spent = run(tclient, tapp.state.store.month_spend, NOW, "Europe/London")
    assert spent == pytest.approx(0.0007)
    # nothing about the saved settings changed
    assert _settings(tclient, tapp).briefing_provider == "anthropic"


def test_a_provider_error_is_scrubbed(
    tapp: FastAPI, tclient: TestClient, provider: Provider
) -> None:
    _save_keys(tclient)
    provider.reply = httpx.Response(
        401, json={"error": {"message": f"Incorrect API key provided: {OPENAI_KEY}"}}
    )
    form = _test_form(
        tclient,
        "writer",
        writer_provider="openai",
        writer_model="gpt-5.4-nano",
        writer_model_other="",
    )
    r = _post(tclient, "/settings/models/test", form, headers=HX)
    assert "It failed: HTTP 401 (AuthenticationError)." in r.text
    assert OPENAI_KEY not in r.text and "Incorrect API key" not in r.text
    assert len(provider.requests) == 1


def test_without_javascript_the_result_follows_a_redirect(
    tclient: TestClient, provider: Provider
) -> None:
    _save_keys(tclient)
    form = _test_form(
        tclient,
        "consolidator",
        consolidator_provider="openrouter",
        consolidator_model="google/gemini-3.8-flash",
        consolidator_model_other="",
    )
    r = _post(tclient, "/settings/models/test", form)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?tested=consolidator#model-consolidator"
    page = tclient.get(r.headers["location"]).text
    result = re.search(r'id="test-consolidator".*?</div>', page, re.S)
    assert result is not None and "It works." in result.group(0)
    assert "google/gemini-3.8-flash" in result.group(0)
    assert "It works." not in tclient.get("/settings").text  # only right after the test


def test_an_unknown_job_is_404(tclient: TestClient) -> None:
    form = _test_form(tclient, "painter")
    assert _post(tclient, "/settings/models/test", form).status_code == 404


# ---- refresh model list ----------------------------------------------------------------------


def test_refresh_model_list_reports_success(app: FastAPI, client: TestClient) -> None:
    run(client, _drain, app)
    r = _post(client, "/settings/models/refresh", {})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?models=refreshed#models"
    assert "Model list refreshed." in client.get(r.headers["location"]).text


def test_a_failed_refresh_keeps_the_previous_list(tmp_path: Path) -> None:
    calls = {"n": 0}

    async def first_only() -> object:
        calls["n"] += 1
        if calls["n"] > 1:
            raise httpx.ConnectError("offline")
        return await fixture_catalogue()

    app = _app(tmp_path, catalogue_fetch=first_only)
    with TestClient(app, base_url=BASE_URL) as c:
        login(c)
        run(c, _drain, app)
        before = app.state.catalogue.current
        r = _post(c, "/settings/models/refresh", {})
        assert r.headers["location"] == "/settings?models=kept#models"
        assert "kept the previous list" in c.get(r.headers["location"]).text
        assert app.state.catalogue.current is before


def test_a_refresh_with_no_list_at_all(tmp_path: Path) -> None:
    async def failing() -> object:
        raise httpx.ConnectError("offline")

    app = _app(tmp_path, catalogue_fetch=failing)
    with TestClient(app, base_url=BASE_URL) as c:
        login(c)
        r = _post(c, "/settings/models/refresh", {})
        assert r.headers["location"] == "/settings?models=failed#models"
        assert "couldn&#39;t be loaded" in c.get(r.headers["location"]).text


def test_a_failing_rebuild_after_refresh_keeps_the_old_context(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Carry-over N1: the rebuild's exception is logged (scrubbed), not raised."""
    builds = {"n": 0}

    async def factory(deps: AppDeps) -> PipelineContext:
        builds["n"] += 1
        if builds["n"] > 1:
            raise RuntimeError(f"boom {OPENROUTER_KEY}")
        return await fake_ctx_factory(deps)

    app = _app(tmp_path, ctx_factory=factory)
    with TestClient(app, base_url=BASE_URL) as c:
        login(c)
        run(c, _drain, app)
        run(
            c,
            update_runtime_settings,
            app.state.store,
            writer_provider="openrouter",
            writer_model="google/gemini-3.8-flash",
        )  # a job uses the list, so a refresh rebuilds
        before = app.state.ctx
        r = _post(c, "/settings/models/refresh", {})
        assert r.status_code == 303
        assert r.headers["location"] == "/settings?models=refreshed#models"
        assert app.state.ctx is before
        assert builds["n"] == 2
    assert OPENROUTER_KEY not in caplog.text
    assert "RuntimeError" in caplog.text


# ---- the OpenAI and OpenRouter key rows -------------------------------------------------------


@pytest.mark.parametrize(("name", "key"), [("openai", OPENAI_KEY), ("openrouter", OPENROUTER_KEY)])
def test_new_key_rows_are_tested_and_write_only(
    app: FastAPI, client: TestClient, key_results: dict[str, str | None], name: str, key: str
) -> None:
    page = client.get("/settings").text
    assert f'id="key-{name}"' in page and f'action="/settings/keys/{name}"' in page
    key_results[name] = f"rejected {key}"
    r = _post(client, f"/settings/keys/{name}", {"key": key})
    assert r.status_code == 422
    assert key not in r.text and "[key removed]" in r.text
    assert run(client, app.state.api_keys.resolve_api_key, name) == ""
    key_results[name] = None
    r = _post(client, f"/settings/keys/{name}", {"key": key})
    assert r.status_code == 303
    assert run(client, app.state.api_keys.resolve_api_key, name) == key
    for path in ("/settings", "/settings?saved=key", "/"):
        assert key not in client.get(path).text, path


def test_a_refused_test_never_logs_the_typed_model(
    tclient: TestClient, provider: Provider, caplog: pytest.LogCaptureFixture
) -> None:
    """Review M1: a key pasted into the wrong box must not reach the log."""
    pasted = "sk-or-" + "p" * 40
    form = _test_form(tclient, "writer", writer_provider="openrouter", writer_model_other=pasted)
    with caplog.at_level("INFO"):
        r = _post(tclient, "/settings/models/test", form, headers=HX)
    assert "Not tested" in r.text
    assert pasted not in caplog.text
    assert "model test for writer: refused" in caplog.text
    assert provider.requests == []


def test_a_test_is_refused_while_a_run_holds_the_lock(
    tapp: FastAPI, tclient: TestClient, provider: Provider
) -> None:
    """Review M3: the budget cap stays hard; a test never races a run."""
    _save_keys(tclient)
    lock: asyncio.Lock = tapp.state.run_lock
    form = _test_form(
        tclient,
        "writer",
        writer_provider="openrouter",
        writer_model="google/gemini-3.8-flash",
        writer_model_other="",
    )
    run(tclient, lock.acquire)
    try:
        r = _post(tclient, "/settings/models/test", form, headers=HX)
    finally:
        run(tclient, _release, lock)
    assert "A run is in progress; try again when it finishes." in r.text
    assert provider.requests == []
    r = _post(tclient, "/settings/models/test", form, headers=HX)
    assert "It works." in r.text and len(provider.requests) == 1


def test_a_test_holds_the_run_lock_while_it_calls(
    tapp: FastAPI, tclient: TestClient, provider: Provider
) -> None:
    _save_keys(tclient)
    seen: list[bool] = []
    provider.on_request = lambda request: seen.append(tapp.state.run_lock.locked())
    form = _test_form(
        tclient,
        "writer",
        writer_provider="openrouter",
        writer_model="google/gemini-3.8-flash",
        writer_model_other="",
    )
    assert "It works." in _post(tclient, "/settings/models/test", form, headers=HX).text
    assert seen == [True]
    assert not tapp.state.run_lock.locked()


def test_a_no_js_result_is_shown_once(tclient: TestClient) -> None:
    form = _test_form(tclient, "writer", writer_provider="openai", writer_model_other="")
    r = _post(tclient, "/settings/models/test", form)
    assert "Not tested." in tclient.get(r.headers["location"]).text
    assert "Not tested." not in tclient.get(r.headers["location"]).text


async def _release(lock: asyncio.Lock) -> None:
    lock.release()


def test_a_hung_test_times_out_and_frees_the_run_lock(
    tapp: FastAPI, tclient: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R1: a stalled provider can't hold the run lock for long."""
    from contextlib import asynccontextmanager

    from ai_newsdesk.web import model_test
    from tests.fake_model_client import FakeModelClient

    class Hung(FakeModelClient):
        async def structured(self, **kwargs: Any) -> Any:  # type: ignore[override]
            await asyncio.sleep(3600)

    @asynccontextmanager
    async def hung_client(provider: str, model: str) -> AsyncIterator[Any]:
        yield Hung([])

    monkeypatch.setattr(model_test, "TEST_TIMEOUT_S", 0.05)
    monkeypatch.setattr(tapp.state, "model_client", hung_client)
    _save_keys(tclient)
    form = _test_form(
        tclient,
        "writer",
        writer_provider="openrouter",
        writer_model="google/gemini-3.8-flash",
        writer_model_other="",
    )
    r = _post(tclient, "/settings/models/test", form, headers=HX)
    assert "The model didn&#39;t answer within 0.05 seconds." in r.text
    assert not tapp.state.run_lock.locked()


def test_the_test_clients_never_retry(tapp: FastAPI, tclient: TestClient) -> None:
    from ai_newsdesk.llm.openai_client import OpenAICompatibleClient

    # Anthropic's pool setting is covered in test_factory: its SDK can't use this test's
    # httpx transport.
    _save_keys(tclient)

    async def build_or() -> int:
        async with tapp.state.model_client("openrouter", "google/gemini-3.8-flash") as client:
            assert isinstance(client, OpenAICompatibleClient)
            return client._client.max_retries  # type: ignore[attr-defined]

    assert run(tclient, build_or) == 0
