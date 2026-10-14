"""Compare (multi-provider spec 4): set up a comparison of 2-4 models, check its maximum cost,
run it in the background under the pipeline lock, and read the results: labels, preferred
summaries, "Use this model" and the history of past comparisons."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_newsdesk import compare
from ai_newsdesk.budget import Budget
from ai_newsdesk.llm.factory import UnusableModelError
from ai_newsdesk.llm.validate import missing_key_error, validate_model_choice
from ai_newsdesk.pipeline import REVIEW_BATCH, WRITE_BATCH
from ai_newsdesk.runtime_settings import (
    RuntimeSettings,
    load_runtime_settings,
    update_runtime_settings,
)
from ai_newsdesk.store.records import LABELS, CompareJob, CompareModel, CompareRun
from ai_newsdesk.web.compare_form import MODEL_KEYS, CompareSetup, parse_compare_form
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import NO_STORE, api_keys_of, is_htmx, now_of, page, partial, store_of
from ai_newsdesk.web.model_choices import (
    DEFAULT_JUDGE,
    JUDGE_WARNING,
    MODEL_PROVIDERS,
    PROVIDER_NAMES,
    datalists,
    model_row,
)

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])
log = logging.getLogger("ai_newsdesk.web.compare")

BUSY = "A run is in progress; try again when it finishes."
STATUS_LABELS = {
    "running": "Running",
    "done": "Done",
    "capped": "Stopped at the cap",
    "failed": "Failed",
}
LABEL_TEXT = {"junk": "Junk", "show": "Show", "important": "Important"}
SUBJECT_NOUN = {"judge": ("item", "items"), "writer": ("story", "stories")}


@dataclass(frozen=True)
class CostCheck:
    subjects: int
    noun: str  # "items" or "stories" (or the singular)
    calls: int
    max_usd: float
    cap_usd: float

    @property
    def fits(self) -> bool:
        return self.subjects > 0 and self.max_usd <= self.cap_usd


@dataclass(frozen=True)
class HistoryRow:
    run: CompareRun
    best: CompareModel | None


def _form(raw: Mapping[str, Any]) -> dict[str, str]:
    return {key: value for key, value in raw.items() if isinstance(value, str)}


async def _budget(request: Request, runtime: RuntimeSettings) -> Budget:
    app: FastAPI = request.app
    budget = Budget(
        store_of(request),
        app.state.pricing,
        app.state.deps.clock,
        runtime,
        tz=app.state.settings.tz,
        prices=app.state.prices,
    )
    await budget.refresh()
    return budget


def _default_values(runtime: RuntimeSettings) -> dict[str, str]:
    """A new comparison starts from the production judge, with the cap's default."""
    provider = runtime.judge_provider if runtime.judge_provider in MODEL_PROVIDERS else ""
    values = {"job": "judge", "sample_size": "", "cap_usd": f"{compare.CAP_DEFAULT:.2f}"}
    for key in MODEL_KEYS:
        values[f"{key}_provider"] = ""
        values[f"{key}_model"] = ""
    if provider:
        values["m1_provider"], values["m1_model"] = provider, runtime.judge_model
    return values


def _echo(form: Mapping[str, str]) -> dict[str, str]:
    values = {key: form.get(key, "") for key in ("job", "sample_size", "cap_usd")}
    for key in MODEL_KEYS:
        for part in ("provider", "model", "model_other"):
            values[f"{key}_{part}"] = form.get(f"{key}_{part}", "")
    return values


async def _best(request: Request, run: CompareRun, threshold: int) -> CompareModel | None:
    store = store_of(request)
    results = await store.compare_results(run.id)
    if run.job == "judge":
        items = await store.compare_items(run.sample_ids)
        key = compare.best_judge(compare.judge_summary(run.models, items, results, threshold))
    else:
        rows = compare.writer_summary(run.models, results, await store.writer_prefs(run.id))
        key = compare.best_writer(rows)
    return next((m for m in run.models if m.key == key), None)


async def _render_setup(
    request: Request,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    check: CostCheck | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    app: FastAPI = request.app
    store = store_of(request)
    runtime = await load_runtime_settings(store)
    form = values if values is not None else _default_values(runtime)
    rows = [
        model_row(
            key,
            f"Model {n}",
            "",
            MODEL_PROVIDERS,
            form,
            shortlist=app.state.shortlist,
            prices=app.state.prices,
            optional=True,
        )
        for n, key in enumerate(MODEL_KEYS, start=1)
    ]
    history = [
        HistoryRow(r, await _best(request, r, runtime.show_threshold))
        for r in await store.compare_runs()
    ]
    budget = await _budget(request, runtime)
    return await page(
        request,
        "compare.html",
        {
            "active": "compare",
            "values": form,
            "errors": errors or {},
            "rows": rows,
            "datalists": datalists(app.state.prices, app.state.catalogue.current),
            "check": check,
            "notice": notice,
            "history": history,
            "money_left": budget.remaining_month(),
            "job_labels": compare.JOB_LABELS,
            "sample_default": compare.SAMPLE_DEFAULT,
            "sample_max": compare.SAMPLE_MAX,
            "cap_min": compare.CAP_MIN,
            "cap_max": compare.CAP_MAX,
            "status_labels": STATUS_LABELS,
            "provider_names": PROVIDER_NAMES,
        },
        status_code=status_code,
    )


async def _setup_from(request: Request, form: Mapping[str, str]) -> CompareSetup:
    store = store_of(request)
    budget = await _budget(request, await load_runtime_settings(store))
    keys = await api_keys_of(request).available()
    return parse_compare_form(
        form,
        prices=request.app.state.prices,
        catalogue=request.app.state.catalogue.current,
        keys=keys,
        money_left=budget.remaining_month(),
    )


async def _sample(request: Request, job: CompareJob, n: int) -> list[compare.Subject]:
    store = store_of(request)
    now = now_of(request)
    if job == "judge":
        return list(await compare.judge_sample(store, now, n))
    return list(await compare.writer_sample(store, now, n, await load_runtime_settings(store)))


async def _check(
    request: Request, job: CompareJob, setup: CompareSetup, sample: Sequence[compare.Subject]
) -> CostCheck:
    async with request.app.state.compare_clients() as client_for:
        clients = [client_for(m.provider, m.model) for m in setup.models]
        max_usd = compare.max_cost_usd(job, clients, sample)
    batch = REVIEW_BATCH if job == "judge" else WRITE_BATCH
    one, many = SUBJECT_NOUN[job]
    return CostCheck(
        subjects=len(sample),
        noun=one if len(sample) == 1 else many,
        calls=len(setup.models) * math.ceil(len(sample) / batch),
        max_usd=max_usd,
        cap_usd=setup.cap_usd,
    )


@dataclass(frozen=True)
class Checked:
    """A valid setup with its sample and maximum cost."""

    job: CompareJob
    setup: CompareSetup
    sample: list[compare.Subject]
    check: CostCheck


async def _checked(request: Request, form: dict[str, str]) -> Checked | HTMLResponse:
    """Validates the form and works out the maximum cost; a refusal is the page to send."""
    setup = await _setup_from(request, form)
    job = setup.job
    if not setup.ok or job is None:
        return await _render_setup(
            request, values=_echo(form), errors=setup.errors, status_code=422
        )
    sample = await _sample(request, job, setup.sample_size)
    try:
        check = await _check(request, job, setup, sample)
    except UnusableModelError as exc:
        errors = {"models": str(exc)}
        return await _render_setup(request, values=_echo(form), errors=errors, status_code=422)
    return Checked(job, setup, sample, check)


@router.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request) -> HTMLResponse:
    return await _render_setup(request)


@router.post("/compare/check")
async def check_cost(request: Request) -> Response:
    form = _form(await request.form())
    checked = await _checked(request, form)
    if isinstance(checked, HTMLResponse):
        return checked
    return await _render_setup(request, values=_echo(form), check=checked.check)


async def _comparison(
    app: FastAPI, lock: asyncio.Lock, run_id: int, checked: Checked, budget: Budget
) -> None:
    """The background task: it owns the pipeline lock and releases it when done."""
    store = app.state.store
    try:
        async with app.state.compare_clients() as client_for:
            status = await compare.run_comparison(
                store,
                run_id,
                checked.job,
                checked.setup.models,
                checked.sample,
                cap_usd=checked.setup.cap_usd,
                client_for=client_for,
                budget=budget,
                clock=app.state.deps.clock,
            )
        log.info("comparison %d finished: %s", run_id, status)
    except Exception as exc:  # run_comparison finishes its row; anything around it must too
        log.warning("comparison %d failed (%s)", run_id, type(exc).__name__)
        await store.finish_compare_run(
            run_id,
            "failed",
            app.state.deps.clock.now(),
            0.0,
            f"The comparison failed ({type(exc).__name__}).",
        )
    finally:
        lock.release()


@router.post("/compare/run")
async def start_comparison(request: Request) -> Response:
    app: FastAPI = request.app
    form = _form(await request.form())
    checked = await _checked(request, form)
    if isinstance(checked, HTMLResponse):
        return checked
    if not checked.check.fits:
        return await _render_setup(
            request, values=_echo(form), check=checked.check, status_code=422
        )
    lock: asyncio.Lock = app.state.run_lock
    if lock.locked():  # never queue behind a run, and never create a row for nothing
        return await _render_setup(request, values=_echo(form), notice=BUSY, status_code=409)
    await lock.acquire()  # free, so this returns at once: nothing can slip in
    try:
        store = store_of(request)
        budget = await _budget(request, await load_runtime_settings(store))
        run_id = await store.create_compare_run(
            checked.job,
            checked.setup.models,
            [compare.subject_id(s) for s in checked.sample],
            checked.setup.cap_usd,
            now_of(request),
        )
        task = asyncio.create_task(
            _comparison(app, lock, run_id, checked, budget), name=f"comparison-{run_id}"
        )
    except BaseException:
        lock.release()
        raise
    app.state.background.add(task)
    task.add_done_callback(app.state.background.discard)
    log.info("comparison %d started: %s, %d models", run_id, checked.job, len(checked.setup.models))
    return RedirectResponse(f"/compare/runs/{run_id}", status_code=303)


# ---- results ------------------------------------------------------------------------------


async def _run(request: Request, run_id: int) -> CompareRun:
    run = await store_of(request).compare_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="There is no such comparison.")
    return run


def _model(run: CompareRun, key: str) -> CompareModel:
    found = next((m for m in run.models if m.key == key), None)
    if found is None:
        raise HTTPException(status_code=404, detail="That model is not in this comparison.")
    return found


def _in_use(runtime: RuntimeSettings, job: CompareJob) -> str:
    if job == "judge":
        return f"{runtime.judge_provider}:{runtime.judge_model}"
    return f"{runtime.writer_provider}:{runtime.writer_model}"


async def _results_context(request: Request, run: CompareRun) -> dict[str, Any]:
    store = store_of(request)
    runtime = await load_runtime_settings(store)
    results = await store.compare_results(run.id)
    context: dict[str, Any] = {
        "run": run,
        "in_use": _in_use(runtime, run.job),
        "provider_names": PROVIDER_NAMES,
        "status_labels": STATUS_LABELS,
        "job_labels": compare.JOB_LABELS,
        "done_count": len(results),
        "total_count": len(run.models) * len(run.sample_ids),
    }
    if run.job == "judge":
        items = await store.compare_items(run.sample_ids)
        summary = compare.judge_summary(run.models, items, results, runtime.show_threshold)
        context.update(
            summary=summary,
            best=compare.best_judge(summary),
            item_rows=compare.judge_items(
                run.models, items, results, runtime.show_threshold, order=run.sample_ids
            ),
            threshold=runtime.show_threshold,
            labels=LABEL_TEXT,
            judge_warning=(
                JUDGE_WARNING
                if any((m.provider, m.model) != DEFAULT_JUDGE for m in run.models)
                else None
            ),
        )
    else:
        stories = await store.stories_by_ids(run.sample_ids)
        prefs = await store.writer_prefs(run.id)
        summary = compare.writer_summary(run.models, results, prefs)
        order = [sid for sid in run.sample_ids if sid in stories]
        context.update(
            summary=summary,
            best=compare.best_writer(summary),
            stories=stories,
            story_rows=compare.writer_stories(run.models, order, results),
            prefs=prefs,
        )
    return context


async def _render_run(
    request: Request,
    run: CompareRun,
    *,
    use_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    context = await _results_context(request, run)
    used = request.query_params.get("used") == "1"
    context.update(active="compare", use_error=use_error, used=used)
    if used:
        runtime = await load_runtime_settings(store_of(request))
        context["used_model"] = context["in_use"].split(":", 1)[1]
        context["used_warning"] = (
            JUDGE_WARNING
            if run.job == "judge" and (runtime.judge_provider, runtime.judge_model) != DEFAULT_JUDGE
            else None
        )
    return await page(request, "compare_run.html", context, status_code=status_code)


@router.get("/compare/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: int) -> HTMLResponse:
    return await _render_run(request, await _run(request, run_id))


@router.get("/compare/runs/{run_id}/status", response_class=HTMLResponse)
async def run_status(request: Request, run_id: int) -> HTMLResponse:
    """Polled every 3 s while the comparison runs; once it has finished, htmx reloads the
    page (HX-Refresh) to show the results."""
    run = await _run(request, run_id)
    context = {
        "run": run,
        "status_labels": STATUS_LABELS,
        "done_count": len(await store_of(request).compare_results(run.id)),
        "total_count": len(run.models) * len(run.sample_ids),
    }
    response = partial(request, "partials/compare_status.html", context)
    if run.status != "running":
        response.headers["HX-Refresh"] = "true"
    return response


async def _results_partial(request: Request, run: CompareRun) -> HTMLResponse:
    name = "partials/compare_judge.html" if run.job == "judge" else "partials/compare_writer.html"
    context = await _results_context(request, run)
    return partial(request, name, context)


@router.post("/compare/runs/{run_id}/label")
async def label_item(
    request: Request,
    run_id: int,
    item_id: Annotated[int, Form()],
    label: Annotated[str, Form()],
) -> Response:
    """Junk / Show / Important (or clear) for one item: the owner's reusable labels."""
    run = await _run(request, run_id)
    if run.job != "judge" or item_id not in run.sample_ids:
        raise HTTPException(status_code=404, detail="That item is not in this comparison.")
    if label != "clear" and label not in LABELS:
        raise HTTPException(status_code=422, detail="Choose Junk, Show or Important.")
    try:
        await store_of(request).set_label(
            item_id,
            None if label == "clear" else label,
            now_of(request),  # type: ignore[arg-type]
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="That item no longer exists.") from None
    if not is_htmx(request):
        return RedirectResponse(f"/compare/runs/{run_id}#item-{item_id}", status_code=303)
    return await _results_partial(request, run)


@router.post("/compare/runs/{run_id}/prefer")
async def prefer_summary(
    request: Request,
    run_id: int,
    story_id: Annotated[int, Form()],
    model_key: Annotated[str, Form()],
) -> Response:
    run = await _run(request, run_id)
    if run.job != "writer" or story_id not in run.sample_ids:
        raise HTTPException(status_code=404, detail="That story is not in this comparison.")
    _model(run, model_key)
    await store_of(request).set_writer_pref(run_id, story_id, model_key, now_of(request))
    if not is_htmx(request):
        return RedirectResponse(f"/compare/runs/{run_id}#story-{story_id}", status_code=303)
    return await _results_partial(request, run)


@router.post("/compare/runs/{run_id}/use")
async def use_model(request: Request, run_id: int, model_key: Annotated[str, Form()]) -> Response:
    """Make a compared model the production judge or writer, with Settings' validation."""
    run = await _run(request, run_id)
    model = _model(run, model_key)
    app: FastAPI = request.app
    error = validate_model_choice(
        model.provider,
        model.model,
        pricebook=app.state.prices,
        catalogue=app.state.catalogue.current,
    ) or missing_key_error(model.provider, await api_keys_of(request).available())
    if error is not None:
        return await _render_run(request, run, use_error=error, status_code=422)
    fields = (
        {"judge_provider": model.provider, "judge_model": model.model}
        if run.job == "judge"
        else {"writer_provider": model.provider, "writer_model": model.model}
    )
    await update_runtime_settings(store_of(request), **fields)
    await app.state.rebuild_context()
    log.info("comparison %d: %s now uses %s", run_id, run.job, model.key)
    return RedirectResponse(f"/compare/runs/{run_id}?used=1", status_code=303, headers=NO_STORE)
