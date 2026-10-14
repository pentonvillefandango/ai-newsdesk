"""Settings: schedule, budget and reading settings, write-only API keys, Refresh now, spend
meter and chart. Each job's model is chosen on the Models page (routes_models)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from ai_newsdesk.runtime_settings import (
    API_KEY_NAMES,
    ApiKeyName,
    RuntimeSettings,
    load_runtime_settings,
    update_runtime_settings,
)
from ai_newsdesk.scheduler import Scheduler
from ai_newsdesk.web.chart import spend_chart
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import api_keys_of, now_of, page, store_of, zone_of
from ai_newsdesk.web.estimate import choices_of, frequency_options, profile_from, rates_for
from ai_newsdesk.web.forms import parse_settings_form
from ai_newsdesk.web.keys import KEY_LABELS, MAX_KEY_CHARS, scrub
from ai_newsdesk.web.runs import run_dict

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])
log = logging.getLogger("ai_newsdesk.web.settings")

SAVED = {
    "settings": "Settings saved.",
    "key": "Key tested and saved.",
    "cleared": "Key cleared. The .env value applies now, if there is one.",
}
RUN = {
    "started": "Run started. New stories appear on Today when it finishes.",
    "busy": "A run is already going. Try again when it has finished.",
}
FIELDS = (
    "runs_per_day",
    "briefing_time",
    "daily_review_cap",
    "monthly_budget_usd",
    "show_threshold",
    "hn_min_points",
)


def _values_from(runtime: RuntimeSettings) -> dict[str, str]:
    values = {key: str(getattr(runtime, key)) for key in FIELDS}
    values["monthly_budget_usd"] = f"{runtime.monthly_budget_usd:.2f}"
    values["why_it_matters"] = "on" if runtime.why_it_matters else ""
    return values


def _values_echo(form: dict[str, str]) -> dict[str, str]:
    values = {key: form.get(key, "") for key in FIELDS}
    values["why_it_matters"] = "on" if form.get("why_it_matters") == "on" else ""
    return values


def _parse_iso(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def _key_name(name: str) -> ApiKeyName:
    if name not in API_KEY_NAMES:
        raise HTTPException(status_code=404, detail="There is no such key.")
    return cast(ApiKeyName, name)


async def _render(
    request: Request,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    key_errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    store = store_of(request)
    app = request.app
    settings = app.state.settings
    now = now_of(request)
    today = now.astimezone(zone_of(request)).date()
    runtime = await load_runtime_settings(store)
    form = values if values is not None else _values_from(runtime)
    profile = profile_from(await store.usage_last_days(now))
    rates = rates_for(profile, app.state.prices, choices_of(runtime))
    spent = await store.month_spend(now, settings.tz)
    budget = runtime.monthly_budget_usd
    api_keys = api_keys_of(request)
    # Step 1's ApiKeyStatus: name, set_in_ui, set_in_env, updated_at, is_set. Never the key.
    # Every key, TypeSafe included, so a key can be saved before the judge is switched to Jev.
    keys = [await api_keys.api_key_status(name) for name in API_KEY_NAMES]
    runs: list[dict[str, Any]] = []
    for record in await store.last_runs(5):
        data = run_dict(record)
        data["started"] = _parse_iso(data["started_at"])
        runs.append(data)
    return await page(
        request,
        "settings.html",
        {
            "active": "settings",
            "values": form,
            "errors": errors or {},
            "key_errors": key_errors or {},
            "frequency_options": frequency_options(
                rates,
                daily_cap=runtime.daily_review_cap,
                why_it_matters=runtime.why_it_matters,
                budget=budget,
            ),
            "profile": profile,
            "estimate_unknown": rates is None,
            "spent": spent,
            "spent_pct": 100.0 if budget <= 0 else min(100.0, 100.0 * spent / budget),
            "over_budget": spent >= budget,
            "month_label": f"{today:%B}",
            "chart": spend_chart(await store.spend_by_day(now, settings.tz), today),
            "keys": keys,
            "key_labels": KEY_LABELS,
            "runs": runs,
            "flash": SAVED.get(request.query_params.get("saved", "")),
            "run_flash": RUN.get(request.query_params.get("run", "")),
        },
        status_code=status_code,
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return await _render(request)


@router.post("/settings")
async def save_settings(request: Request) -> Response:
    raw = await request.form()
    form = {key: value for key, value in raw.items() if isinstance(value, str)}
    store = store_of(request)
    parsed = parse_settings_form(form)
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
    return RedirectResponse("/settings?saved=settings", status_code=303)


@router.post("/settings/keys/{name}")
async def save_api_key(request: Request, name: str, key: Annotated[str, Form()] = "") -> Response:
    key_name = _key_name(name)
    candidate = key.strip()
    error: str | None
    if not candidate:
        error = "Paste a key first."
    elif len(candidate) > MAX_KEY_CHARS or any(ch.isspace() for ch in candidate):
        error = "That does not look like an API key."
    else:
        tester = request.app.state.key_testers.get(key_name)
        if tester is None:
            error = "No test is configured for this key, so it was not saved."
        else:
            try:
                result = await tester(candidate, await load_runtime_settings(store_of(request)))
            except Exception as exc:  # a tester must never leak the key through a traceback
                result = f"The test call failed ({type(exc).__name__})."
            error = None if result is None else scrub(result, candidate)
    if error is not None:
        log.info("API key not saved: test failed for %s", key_name)
        return await _render(request, key_errors={key_name: error}, status_code=422)
    await api_keys_of(request).set_api_key(key_name, candidate, now_of(request))
    await request.app.state.rebuild_context()
    return RedirectResponse(f"/settings?saved=key#key-{key_name}", status_code=303)


@router.post("/settings/keys/{name}/clear")
async def clear_api_key(request: Request, name: str) -> Response:
    key_name = _key_name(name)
    await api_keys_of(request).clear_api_key(key_name)
    await request.app.state.rebuild_context()
    return RedirectResponse(f"/settings?saved=cleared#key-{key_name}", status_code=303)


async def _run_now(scheduler: Scheduler) -> None:
    try:
        # Step 3 (R6): returns None without running if a run started in the meantime.
        if await scheduler.run_now("manual") is None:
            log.info("manual run skipped or produced no report")
    except Exception:
        log.exception("manual run failed")


@router.post("/settings/refresh")
async def refresh_now(request: Request) -> Response:
    app: FastAPI = request.app
    scheduler: Scheduler = app.state.scheduler
    if scheduler.lock.locked():
        return RedirectResponse("/settings?run=busy#run", status_code=303)
    task = asyncio.create_task(_run_now(scheduler), name="manual-run")
    app.state.background.add(task)
    task.add_done_callback(app.state.background.discard)
    return RedirectResponse("/settings?run=started#run", status_code=303)
