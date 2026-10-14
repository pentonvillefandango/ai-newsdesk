"""Composition root: the only module that wires concrete classes to the protocols."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anthropic
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_newsdesk import taste
from ai_newsdesk.budget import Budget
from ai_newsdesk.clock import SystemClock
from ai_newsdesk.config import Settings
from ai_newsdesk.consolidate import HaikuConsolidator
from ai_newsdesk.judges.haiku import HaikuJudge
from ai_newsdesk.judges.jev import JevJudge
from ai_newsdesk.llm.catalogue import CatalogueFetch, ModelCatalogue, http_fetcher
from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.normalise import canonical_url
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.pricing import Pricing
from ai_newsdesk.protocols import Clock, Judge
from ai_newsdesk.research_engine import HttpResearchEngine
from ai_newsdesk.runtime_settings import ApiKeys, load_runtime_settings
from ai_newsdesk.scheduler import NightlyFn, Scheduler
from ai_newsdesk.sources.registry import build_sources
from ai_newsdesk.store import Store
from ai_newsdesk.web.app_routes import build_router
from ai_newsdesk.web.deps import STATIC_DIR
from ai_newsdesk.web.keys import KeyTester, default_key_testers
from ai_newsdesk.web.security import install_security
from ai_newsdesk.writer import HaikuWriter

log = logging.getLogger("ai_newsdesk.app")


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
    """Real wiring (master plan R8). Called at start-up and after any settings or key change."""
    settings, store, keys = deps.settings, deps.store, deps.api_keys
    runtime = await load_runtime_settings(store)
    # The provider is async and read per request, so a key saved in the UI applies at once (R3).
    research = HttpResearchEngine(
        settings.research_engine_url,
        lambda: keys.resolve_api_key("research_engine"),
        client=deps.http,
    )
    # An empty key still builds a client; calls then fail with 401 and items stay unscored.
    client = anthropic.AsyncAnthropic(
        api_key=(await keys.resolve_api_key("anthropic")) or "not-set"
    )
    judge: Judge
    if runtime.judge_provider == "typesafe":
        # judges.jev imports without the optional `jev` extra; the SDK is loaded only on use.
        judge = JevJudge(
            await keys.resolve_api_key("typesafe"), deps.pricing, model=runtime.judge_model
        )
    else:
        judge = HaikuJudge(client, runtime.judge_model, deps.pricing)
    return PipelineContext(
        store=store,
        sources=build_sources(deps.http, research, deps.clock),
        research_engine=research,
        judge=judge,
        writer=HaikuWriter(
            client, runtime.writer_model, deps.pricing, briefing_model=runtime.briefing_model
        ),
        budget=Budget(store, deps.pricing, deps.clock, runtime, tz=settings.tz, prices=deps.prices),
        clock=deps.clock,
        settings=settings,
        # The judge model when the judge is Claude; Jev models cannot take this call.
        consolidator=HaikuConsolidator(
            client,
            runtime.judge_model if runtime.judge_provider == "anthropic" else runtime.writer_model,
            deps.pricing,
        ),
    )


def refresh_in_background(
    catalogue: ModelCatalogue, background: set[asyncio.Task[None]]
) -> asyncio.Task[None]:
    """Refresh the catalogue without blocking the caller (or holding the pipeline lock).
    refresh() never raises: a failure is logged and the last good copy is kept."""

    async def refresh() -> None:
        await catalogue.refresh()

    task = asyncio.create_task(refresh(), name="catalogue-refresh")
    background.add(task)
    task.add_done_callback(background.discard)
    return task


def nightly_jobs(catalogue: ModelCatalogue, background: set[asyncio.Task[None]]) -> NightlyFn:
    """The nightly chain: the taste refit, then a background catalogue refresh, even when the
    refit fails. The fetch runs outside the scheduler lock, so it never delays a run."""

    async def run(ctx: PipelineContext) -> None:
        try:
            await taste.nightly(ctx)
        finally:
            refresh_in_background(catalogue, background)

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
) -> FastAPI:
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
            app.state.deps = deps
            app.state.ctx = await factory(deps)
            app.state.key_testers = (
                dict(key_testers)
                if key_testers is not None
                else default_key_testers(settings, http)
            )
            # Always built, so Refresh now can use run_now() even with the schedule switched off.
            if catalogue.is_stale():
                refresh_in_background(catalogue, background)  # never blocks start-up
            scheduler = Scheduler(app.state.ctx, nightly_fn=nightly_jobs(catalogue, background))
            if settings.scheduler_enabled:
                scheduler.start()
            app.state.scheduler = scheduler
            app.state.run_lock = scheduler.lock
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

    async def rebuild_context() -> None:
        ctx = await factory(app.state.deps)
        app.state.ctx = ctx
        if app.state.scheduler is not None:
            app.state.scheduler.ctx = ctx

    app.state.settings = settings
    app.state.rebuild_context = rebuild_context
    install_security(app, settings)
    app.include_router(build_router())
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_exception_handler(StarletteHTTPException, _http_error)
    return app
