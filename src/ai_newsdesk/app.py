"""Composition root: the only module that wires concrete classes to the protocols."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_newsdesk import taste
from ai_newsdesk.budget import Budget
from ai_newsdesk.clock import SystemClock
from ai_newsdesk.config import Settings
from ai_newsdesk.consolidate import LLMConsolidator
from ai_newsdesk.judges.jev import JevJudge
from ai_newsdesk.judges.llm import LLMJudge
from ai_newsdesk.llm.anthropic_client import AnthropicClient, Effort
from ai_newsdesk.llm.base import ModelClient
from ai_newsdesk.llm.catalogue import CatalogueFetch, ModelCatalogue, http_fetcher
from ai_newsdesk.llm.factory import SdkPool, UnusableModelError, make_client
from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.llm.shortlist import Shortlist
from ai_newsdesk.normalise import canonical_url
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.pricing import Pricing
from ai_newsdesk.protocols import Clock, Judge
from ai_newsdesk.research_engine import HttpResearchEngine
from ai_newsdesk.runtime_settings import (
    DEFAULT_MODEL,
    ApiKeyName,
    ApiKeys,
    load_runtime_settings,
)
from ai_newsdesk.scheduler import NightlyFn, Scheduler
from ai_newsdesk.sources.registry import build_sources
from ai_newsdesk.store import Store
from ai_newsdesk.web.app_routes import build_router
from ai_newsdesk.web.deps import STATIC_DIR
from ai_newsdesk.web.keys import KeyTester, default_key_testers
from ai_newsdesk.web.security import install_security
from ai_newsdesk.writer import LLMWriter

log = logging.getLogger("ai_newsdesk.app")
MODEL_PROVIDERS: tuple[ApiKeyName, ...] = ("anthropic", "openai", "openrouter")


@dataclass
class AppDeps:
    settings: Settings
    store: Store
    pricing: Pricing
    http: httpx.AsyncClient
    clock: Clock
    api_keys: ApiKeys
    prices: PriceBook  # (provider, model) prices; reads the live catalogue
    catalogue: ModelCatalogue


CtxFactory = Callable[[AppDeps], Awaitable[PipelineContext]]


async def build_context(deps: AppDeps) -> PipelineContext:
    """Real wiring (master plan R8). Called at start-up, after any settings or key change and
    after a model-list refresh. Each AI job gets its own ModelClient (multi-provider 3.4)."""
    settings, store, keys = deps.settings, deps.store, deps.api_keys
    runtime = await load_runtime_settings(store)
    # The provider is async and read per request, so a key saved in the UI applies at once (R3).
    research = HttpResearchEngine(
        settings.research_engine_url,
        lambda: keys.resolve_api_key("research_engine"),
        client=deps.http,
    )
    sdks = SdkPool({name: await keys.resolve_api_key(name) for name in MODEL_PROVIDERS})
    catalogue = deps.catalogue.current

    def job_client(
        job: str, provider: str, model: str, *, effort: Effort = "low", cache_system: bool = True
    ) -> ModelClient:
        """The chosen client, or Claude Haiku when the choice is unusable: start-up never
        fails over a model setting. The logged reason is ours and never holds a key."""
        try:
            return make_client(
                provider,
                model,
                sdks=sdks,
                pricebook=deps.prices,
                catalogue=catalogue,
                effort=effort,
                cache_system=cache_system,
            )
        except UnusableModelError as exc:
            log.warning(
                "%s model %s:%s is unusable (%s); using %s instead",
                job,
                provider,
                model,
                exc,
                DEFAULT_MODEL,
            )
            return AnthropicClient(
                sdks.anthropic(),
                DEFAULT_MODEL,
                deps.prices,
                effort=effort,
                cache_system=cache_system,
            )

    judge: Judge
    if runtime.judge_provider == "typesafe":
        # judges.jev imports without the optional `jev` extra; the SDK is loaded only on use.
        judge = JevJudge(
            await keys.resolve_api_key("typesafe"), deps.pricing, model=runtime.judge_model
        )
    else:
        judge = LLMJudge(job_client("judge", runtime.judge_provider, runtime.judge_model))
    return PipelineContext(
        store=store,
        sources=build_sources(deps.http, research, deps.clock),
        research_engine=research,
        judge=judge,
        writer=LLMWriter(
            job_client("writer", runtime.writer_provider, runtime.writer_model),
            job_client(
                "briefing", runtime.briefing_provider, runtime.briefing_model, effort="medium"
            ),
        ),
        budget=Budget(store, deps.pricing, deps.clock, runtime, tz=settings.tz, prices=deps.prices),
        clock=deps.clock,
        settings=settings,
        # A one-off call a run: no cached system block.
        consolidator=LLMConsolidator(
            job_client(
                "consolidator",
                runtime.consolidator_provider,
                runtime.consolidator_model,
                cache_system=False,
            )
        ),
    )


Rebuild = Callable[[], Awaitable[None]]


def refresh_in_background(
    catalogue: ModelCatalogue, background: set[asyncio.Task[None]], then: Rebuild | None = None
) -> asyncio.Task[None]:
    """Refresh the catalogue without blocking the caller (or holding the pipeline lock).
    refresh() never raises: a failure is logged and the last good copy is kept. After a
    successful refresh `then` runs (the context rebuild when a job uses the list), so a job
    whose model could not be priced before the list arrived gets its real client."""

    async def refresh() -> None:
        if not await catalogue.refresh() or then is None:
            return
        try:
            await then()
        except Exception:  # the old context keeps working
            log.exception("rebuilding the context after the model-list refresh failed")

    task = asyncio.create_task(refresh(), name="catalogue-refresh")
    background.add(task)
    task.add_done_callback(background.discard)
    return task


def nightly_jobs(
    catalogue: ModelCatalogue, background: set[asyncio.Task[None]], then: Rebuild | None = None
) -> NightlyFn:
    """The nightly chain: the taste refit, then a background catalogue refresh, even when the
    refit fails. The fetch runs outside the scheduler lock, so it never delays a run."""

    async def run(ctx: PipelineContext) -> None:
        try:
            await taste.nightly(ctx)
        finally:
            refresh_in_background(catalogue, background, then)

    return run


async def _http_error(request: Request, exc: Exception) -> Response:
    if isinstance(exc, StarletteHTTPException):
        return PlainTextResponse(
            f"{exc.status_code}: {exc.detail}", status_code=exc.status_code, headers=exc.headers
        )
    raise exc


def create_app(
    settings: Settings | None = None,
    *,
    ctx_factory: CtxFactory | None = None,
    key_testers: Mapping[str, KeyTester] | None = None,
    config_dir: Path = Path("config"),
    clock: Clock | None = None,
    catalogue_fetch: CatalogueFetch | None = None,
    model_http_client: Any | None = None,
) -> FastAPI:
    """`model_http_client` is for tests: the Test button's SDK clients send through it."""
    settings = settings if settings is not None else Settings()  # pyright: ignore[reportCallIssue]
    factory: CtxFactory = ctx_factory if ctx_factory is not None else build_context
    app_clock: Clock = clock if clock is not None else SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.db_path, clock=app_clock, canonicalise=canonical_url)
        await store.init()
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": f"ai-newsdesk/{settings.git_sha}"},
        )
        scheduler: Scheduler | None = None
        background: set[asyncio.Task[None]] = set()
        app.state.background = background
        try:
            await store.seed_sources_if_empty(config_dir / "sources.yaml")
            pricing = Pricing.load(config_dir / "pricing.yaml")
            api_keys = ApiKeys(store, settings)
            catalogue = ModelCatalogue(
                store,
                catalogue_fetch if catalogue_fetch is not None else http_fetcher(http),
                app_clock,
            )
            await catalogue.load()
            prices = PriceBook(pricing, lambda: catalogue.current)
            deps = AppDeps(
                settings=settings,
                store=store,
                pricing=pricing,
                http=http,
                clock=app_clock,
                api_keys=api_keys,
                prices=prices,
                catalogue=catalogue,
            )
            app.state.store = store
            app.state.pricing = pricing
            app.state.http = http
            app.state.api_keys = api_keys
            app.state.catalogue = catalogue
            app.state.prices = prices
            app.state.shortlist = Shortlist.load(config_dir / "models.yaml")
            app.state.model_tests = {}  # job -> the last Test result, for the no-JS redirect
            app.state.deps = deps
            app.state.ctx = await factory(deps)
            app.state.key_testers = (
                dict(key_testers)
                if key_testers is not None
                else default_key_testers(settings, http)
            )
            # Always built, so Refresh now can use run_now() even with the schedule switched off.
            scheduler = Scheduler(
                app.state.ctx, nightly_fn=nightly_jobs(catalogue, background, rebuild_if_list_used)
            )
            if settings.scheduler_enabled:
                scheduler.start()
            app.state.scheduler = scheduler
            app.state.run_lock = scheduler.lock
            if catalogue.is_stale():  # never blocks start-up
                refresh_in_background(catalogue, background, rebuild_if_list_used)
            log.info(
                "ai-newsdesk started (scheduler %s)", "on" if settings.scheduler_enabled else "off"
            )
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            for task in list(background):
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await http.aclose()
            await store.close()

    app = FastAPI(
        title="ai-newsdesk",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # A trailing-slash redirect would be absolute and built from the request scheme, which
        # is plain http behind Caddy now that forwarded headers are not trusted: 404 instead.
        redirect_slashes=False,
    )

    # Rebuilds are serialised, so the later caller always reads the latest settings and keys
    # and a slow, stale rebuild can never overwrite a newer context. Not the pipeline lock: a
    # rebuild never waits for a run (a run keeps the context it started with).
    rebuild_lock = asyncio.Lock()

    async def rebuild_context() -> None:
        async with rebuild_lock:
            ctx = await factory(app.state.deps)
            app.state.ctx = ctx
            if app.state.scheduler is not None:
                app.state.scheduler.ctx = ctx

    async def rebuild_if_list_used() -> None:
        """After a model-list refresh. With every job on Anthropic the list changes no client,
        so skip it (each rebuild leaves the old SDK clients to the garbage collector)."""
        if (await load_runtime_settings(app.state.store)).uses_model_list():
            await rebuild_context()

    async def refresh_model_list() -> bool:
        """Refresh the list now (Settings' refresh button) and rebuild when a job uses it.
        A failed rebuild is logged by class name only and the old context keeps working."""
        ok = await app.state.catalogue.refresh()
        if ok:
            try:
                await rebuild_if_list_used()
            except Exception as exc:
                log.warning(
                    "rebuilding the context after the model-list refresh failed (%s);"
                    " kept the old one",
                    type(exc).__name__,
                )
        return ok

    def model_client(provider: str, model: str) -> AbstractAsyncContextManager[ModelClient]:
        """A one-off client for Settings' Test button, over fresh SDK clients closed after use.
        Raises UnusableModelError (a message for the owner) when the choice cannot be used."""

        @asynccontextmanager
        async def client() -> AsyncIterator[ModelClient]:
            deps: AppDeps = app.state.deps
            keys = {name: await deps.api_keys.resolve_api_key(name) for name in MODEL_PROVIDERS}
            # One attempt only: the Test holds the run lock, so it must stay short.
            sdks = SdkPool(keys, http_client=model_http_client, max_retries=0)
            try:
                yield make_client(
                    provider,
                    model,
                    sdks=sdks,
                    pricebook=deps.prices,
                    catalogue=deps.catalogue.current,
                )
            finally:
                await sdks.aclose()

        return client()

    app.state.settings = settings
    app.state.rebuild_context = rebuild_context
    app.state.refresh_model_list = refresh_model_list
    app.state.model_client = model_client
    install_security(app, settings)
    app.include_router(build_router())
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_exception_handler(StarletteHTTPException, _http_error)
    return app
