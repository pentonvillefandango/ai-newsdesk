"""build_context: one ModelClient per job from the runtime settings, with a safe fall-back."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.app import create_app
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.consolidate import LLMConsolidator
from ai_newsdesk.judges.jev import JevJudge
from ai_newsdesk.judges.llm import LLMJudge
from ai_newsdesk.llm.anthropic_client import AnthropicClient
from ai_newsdesk.llm.openai_client import OpenAICompatibleClient
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.runtime_settings import update_runtime_settings
from ai_newsdesk.store import Store
from ai_newsdesk.writer import LLMWriter
from tests.web.support import BASE_URL, CONFIG_DIR, NOW, fixture_catalogue, make_settings, run

OPENAI_KEY = "sk-oa-" + "a" * 30  # gitleaks:allow
OPENROUTER_KEY = "sk-or-" + "b" * 30  # gitleaks:allow
BOTH_KEYS = {"openai_api_key": OPENAI_KEY, "openrouter_api_key": OPENROUTER_KEY}


def _app(tmp_path: Path, fetch: Any = fixture_catalogue, **overrides: Any) -> FastAPI:
    return create_app(
        make_settings(tmp_path, **overrides),
        key_testers={},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
        catalogue_fetch=fetch,
    )


def _settle(client: TestClient, app: FastAPI) -> None:
    async def wait() -> None:
        while app.state.background:
            await asyncio.gather(*list(app.state.background), return_exceptions=True)

    run(client, wait)


def _use(client: TestClient, app: FastAPI, **changes: Any) -> PipelineContext:
    run(client, update_runtime_settings, app.state.store, **changes)
    run(client, app.state.rebuild_context)
    return app.state.ctx


def _clients(ctx: PipelineContext) -> dict[str, Any]:
    assert isinstance(ctx.writer, LLMWriter)
    assert isinstance(ctx.consolidator, LLMConsolidator)
    judge = ctx.judge.client if isinstance(ctx.judge, LLMJudge) else ctx.judge
    return {
        "judge": judge,
        "writer": ctx.writer.client,
        "briefing": ctx.writer.briefing_client,
        "consolidator": ctx.consolidator.client,
    }


def test_the_default_context_is_all_claude_haiku_with_todays_options(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as c:
        _settle(c, app)
        clients = _clients(app.state.ctx)
    for job, client in clients.items():
        assert isinstance(client, AnthropicClient), job
        assert client.model == "claude-haiku-5-5", job
    assert clients["judge"].effort == "low" and clients["judge"].cache_system is True
    assert clients["writer"].effort == "low"
    assert clients["briefing"].effort == "medium"
    assert clients["consolidator"].cache_system is False
    assert len({id(cl._client) for cl in clients.values()}) == 1  # one Anthropic SDK client


def test_each_job_gets_the_chosen_provider_and_model(tmp_path: Path) -> None:
    app = _app(tmp_path, **BOTH_KEYS)
    with TestClient(app, base_url=BASE_URL) as c:
        _settle(c, app)
        ctx = _use(
            c,
            app,
            judge_provider="openrouter",
            judge_model="google/gemini-3.8-flash",
            writer_provider="openai",
            writer_model="gpt-5.4-nano",
            briefing_provider="anthropic",
            briefing_model="claude-sonnet-5-5",
            consolidator_provider="openrouter",
            consolidator_model="openai/gpt-5.4-nano",
        )
        clients = _clients(ctx)
    judge, writer, briefing, merger = (clients[k] for k in clients)
    assert isinstance(judge, OpenAICompatibleClient)
    assert (judge.provider, judge.model) == ("openrouter", "google/gemini-3.8-flash")
    assert isinstance(writer, OpenAICompatibleClient)
    assert (writer.provider, writer.model) == ("openai", "gpt-5.4-nano")
    assert isinstance(briefing, AnthropicClient)
    assert (briefing.model, briefing.effort) == ("claude-sonnet-5-5", "medium")
    assert isinstance(merger, OpenAICompatibleClient)
    assert (merger.provider, merger.model) == ("openrouter", "openai/gpt-5.4-nano")
    assert judge._client is merger._client  # one SDK client per provider per build
    assert judge._client is not writer._client
    assert isinstance(ctx.writer, LLMWriter)
    assert ctx.writer.model == "gpt-5.4-nano" and ctx.writer.briefing_model == "claude-sonnet-5-5"


def test_unusable_choices_fall_back_to_claude_haiku_and_are_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = _app(tmp_path, openai_api_key=OPENAI_KEY)  # no OpenRouter key
    with TestClient(app, base_url=BASE_URL) as c:
        _settle(c, app)
        store = app.state.store
        # Saved behind the form's back, as an old or hand-edited database might be.
        run(c, store.set_setting, "writer_provider", "openrouter")
        run(c, store.set_setting, "writer_model", "google/gemini-3.8-flash")
        run(c, store.set_setting, "consolidator_provider", "openai")
        run(c, store.set_setting, "consolidator_model", "gpt-unknown")
        run(c, store.set_setting, "judge_model", "claude-unknown")
        run(c, store.set_setting, "briefing_provider", "openrouter")
        run(c, store.set_setting, "briefing_model", "google/gemini-3.8-flash")
        with caplog.at_level(logging.WARNING):
            run(c, app.state.rebuild_context)
        clients = _clients(app.state.ctx)
    for job in ("judge", "writer", "briefing", "consolidator"):
        client = clients[job]
        assert isinstance(client, AnthropicClient), job
        assert client.model == "claude-haiku-5-5", job
    # Each job's own options survive the fall-back.
    assert clients["briefing"].effort == "medium"
    assert clients["consolidator"].cache_system is False
    assert "Add your OpenRouter API key first." in caplog.text
    assert "no known price" in caplog.text
    assert OPENAI_KEY not in caplog.text


def test_the_typesafe_judge_is_unchanged(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as c:
        _settle(c, app)
        ctx = _use(c, app, judge_provider="typesafe", judge_model="jev-1.13.0")
    assert isinstance(ctx.judge, JevJudge)
    assert isinstance(ctx.consolidator, LLMConsolidator)
    assert ctx.consolidator.model == "claude-haiku-5-5"


def test_a_first_start_rebuilds_once_the_model_list_arrives(tmp_path: Path) -> None:
    """With no stored catalogue an OpenRouter choice cannot be priced at start-up; it falls back
    until the background refresh lands, then the context is rebuilt with it."""
    settings = make_settings(tmp_path, **BOTH_KEYS)

    async def seed() -> None:
        store = Store(settings.db_path, clock=FixedClock(NOW))
        await store.init()
        await store.set_setting("judge_provider", "openrouter")
        await store.set_setting("judge_model", "google/gemini-3.8-flash")
        await store.close()

    asyncio.run(seed())
    release = asyncio.Event()

    async def slow_fetch() -> object:
        await release.wait()
        return await fixture_catalogue()

    app = _app(tmp_path, slow_fetch, **BOTH_KEYS)
    with TestClient(app, base_url=BASE_URL) as c:
        first = _clients(app.state.ctx)["judge"]
        assert isinstance(first, AnthropicClient)  # the safe fall-back

        async def go() -> None:
            release.set()

        run(c, go)
        _settle(c, app)
        judge = _clients(app.state.ctx)["judge"]
        assert isinstance(judge, OpenAICompatibleClient)
        assert app.state.scheduler.ctx is app.state.ctx


def test_the_list_refresh_rebuilds_only_when_a_job_uses_it(tmp_path: Path) -> None:
    """With every job on Anthropic the model list changes no client, so a refresh leaves the
    context (and its SDK clients) alone."""
    app = _app(tmp_path, **BOTH_KEYS)
    with TestClient(app, base_url=BASE_URL) as c:
        _settle(c, app)
        before = app.state.ctx
        run(c, app.state.refresh_model_list)
        _settle(c, app)
        assert app.state.ctx is before
        ctx = _use(c, app, writer_provider="openai", writer_model="gpt-5.4-nano")
        run(c, app.state.refresh_model_list)
        _settle(c, app)
        assert app.state.ctx is not ctx


def test_rebuilds_never_overlap(tmp_path: Path) -> None:
    """A slow rebuild that started first can never overwrite the context of a later one."""
    release = asyncio.Event()
    armed: list[bool] = []
    built: list[str] = []

    async def factory(deps: Any) -> Any:
        if not armed:
            return "startup"
        label = "first" if not built else "second"
        built.append(label)
        if label == "first":
            await release.wait()
        return label

    app = create_app(
        make_settings(tmp_path),
        ctx_factory=factory,
        key_testers={},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
        catalogue_fetch=fixture_catalogue,
    )
    with TestClient(app, base_url=BASE_URL) as c:
        _settle(c, app)
        armed.append(True)

        async def race() -> None:
            first = asyncio.create_task(app.state.rebuild_context())
            await asyncio.sleep(0)
            second = asyncio.create_task(app.state.rebuild_context())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

        run(c, race)
        assert built == ["first", "second"]
        assert app.state.ctx == "second"
        assert app.state.scheduler.ctx == "second"
