"""Models: the flow diagram (how and when each model and tool is used), then one row per AI job
with its provider and model, the Test button and the model-list refresh (feedback-2: moved off
Settings)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from ai_newsdesk.budget import Budget
from ai_newsdesk.runtime_settings import (
    RuntimeSettings,
    load_runtime_settings,
    update_runtime_settings,
)
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import api_keys_of, is_htmx, page, partial, store_of
from ai_newsdesk.web.flow import flow_steps
from ai_newsdesk.web.forms import parse_models_form
from ai_newsdesk.web.model_choices import (
    JOB_NAMES,
    JUDGE_WARNING,
    PROVIDER_NAMES,
    datalists,
    effective_model,
    judge_warning,
    model_rows,
    price_text,
)
from ai_newsdesk.web.model_test import ModelTestResult, run_model_test

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])
log = logging.getLogger("ai_newsdesk.web.models")

MODEL_LIST = {
    "refreshed": "Model list refreshed.",
    "kept": "The model list couldn't be refreshed, so we kept the previous list.",
    "failed": (
        "The model list couldn't be loaded. Until it loads, only the models priced in"
        " config/pricing.yaml can be used."
    ),
}
RUN_BUSY = "A run is in progress; try again when it finishes."
FIELDS = tuple(f"{job}_{part}" for job in JOB_NAMES for part in ("provider", "model"))


def _values_from(runtime: RuntimeSettings) -> dict[str, str]:
    return {key: str(getattr(runtime, key)) for key in FIELDS}


def _values_echo(form: dict[str, str]) -> dict[str, str]:
    values = {key: form.get(key, "") for key in FIELDS}
    for job in JOB_NAMES:
        values[f"{job}_model_other"] = form.get(f"{job}_model_other", "")
    return values


async def _render(
    request: Request,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    app = request.app
    runtime = await load_runtime_settings(store_of(request))
    form = values if values is not None else _values_from(runtime)
    catalogue = app.state.catalogue.current
    tested = request.query_params.get("tested", "")
    model_tests: dict[str, ModelTestResult] = app.state.model_tests
    return await page(
        request,
        "models.html",
        {
            "active": "models",
            "values": form,
            "errors": errors or {},
            # The diagram shows what is saved, not an unsaved form being corrected.
            "flow": flow_steps(
                runtime, app.state.prices, scheduler_on=app.state.settings.scheduler_enabled
            ),
            "model_rows": model_rows(form, shortlist=app.state.shortlist, prices=app.state.prices),
            "datalists": datalists(app.state.prices, catalogue),
            "judge_warning": JUDGE_WARNING if judge_warning(form) else None,
            "catalogue_at": catalogue.fetched_at if catalogue is not None else None,
            # Shown once: a bookmarked ?tested= link never brings an old result back.
            "model_tests": {tested: model_tests.pop(tested)} if tested in model_tests else {},
            "model_list_flash": MODEL_LIST.get(request.query_params.get("list", "")),
            "flash": "Models saved." if request.query_params.get("saved") else None,
            "price_text": price_text,
            "provider_names": PROVIDER_NAMES,
        },
        status_code=status_code,
    )


@router.get("/models", response_class=HTMLResponse)
async def models_page(request: Request) -> HTMLResponse:
    return await _render(request)


@router.post("/models")
async def save_models(request: Request) -> Response:
    raw = await request.form()
    form = {key: value for key, value in raw.items() if isinstance(value, str)}
    store = store_of(request)
    parsed = parse_models_form(
        form,
        current=await load_runtime_settings(store),
        prices=request.app.state.prices,
        catalogue=request.app.state.catalogue.current,
        keys=await api_keys_of(request).available(),
    )
    if not parsed.ok:
        return await _render(
            request, values=_values_echo(form), errors=parsed.errors, status_code=422
        )
    try:
        # Step 1 validates everything again and saves nothing on error.
        await update_runtime_settings(store, **parsed.values)
    except ValidationError as exc:
        errors = {
            str(e["loc"][0]) if e["loc"] else "form": "This value was refused."
            for e in exc.errors()
        }
        return await _render(request, values=_values_echo(form), errors=errors, status_code=422)
    await request.app.state.rebuild_context()
    return RedirectResponse("/models?saved=1", status_code=303)


@router.post("/models/test")
async def test_model(request: Request) -> Response:
    """One tiny structured call to a row's (provider, model), from the Models form as it is
    now (saved or not). htmx gets the result partial; without JS, a redirect shows it."""
    raw = await request.form()
    form = {key: value for key, value in raw.items() if isinstance(value, str)}
    job = form.get("test_job", "")
    if job not in JOB_NAMES:
        raise HTTPException(status_code=404, detail="There is no such job.")
    app: FastAPI = request.app
    provider, model = form.get(f"{job}_provider", ""), effective_model(form, job)
    lock: asyncio.Lock = app.state.run_lock
    if lock.locked():  # a run's Budget would not see the test's spend: never race it
        result = ModelTestResult(job, provider, model, ok=False, called=False, message=RUN_BUSY)
    else:
        async with lock:
            store = store_of(request)
            budget = Budget(
                store,
                app.state.pricing,
                app.state.deps.clock,
                await load_runtime_settings(store),
                tz=app.state.settings.tz,
                prices=app.state.prices,
            )
            await budget.refresh()
            result = await run_model_test(
                job,
                provider,
                model,
                client_for=app.state.model_client,
                prices=app.state.prices,
                catalogue=app.state.catalogue.current,
                keys=await api_keys_of(request).available(),
                budget=budget,
            )
    if result.called:  # validated by now: a known catalogue or pricing.yaml id
        outcome = "ok" if result.ok else "failed"
        log.info("model test for %s: %s:%s %s", job, result.provider, result.model, outcome)
    else:  # the typed id may be anything, even a pasted key: never log it
        log.info("model test for %s: refused", job)
    if is_htmx(request):
        return partial(
            request,
            "partials/model_test.html",
            {"t": result, "price_text": price_text, "provider_names": PROVIDER_NAMES},
        )
    app.state.model_tests[job] = result
    return RedirectResponse(f"/models?tested={job}#model-{job}", status_code=303)


@router.post("/models/refresh")
async def refresh_model_list(request: Request) -> Response:
    app: FastAPI = request.app
    if await app.state.refresh_model_list():
        outcome = "refreshed"
    else:
        outcome = "kept" if app.state.catalogue.current is not None else "failed"
    return RedirectResponse(f"/models?list={outcome}#models", status_code=303)
