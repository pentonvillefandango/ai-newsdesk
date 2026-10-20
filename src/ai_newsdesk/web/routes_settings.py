"""Settings: schedule, budget and reading settings, write-only API keys, Refresh now, spend
meter and chart. Each job's model is chosen on the Models page (routes_models)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Annotated, Any, cast
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from ai_newsdesk.models import QuotaInfo, VoiceInfo
from ai_newsdesk.runtime_settings import (
    API_KEY_NAMES,
    ApiKeyName,
    RuntimeSettings,
    load_runtime_settings,
    update_runtime_settings,
)
from ai_newsdesk.scheduler import Scheduler
from ai_newsdesk.tts.catalogue import SUGGESTED_VOICES
from ai_newsdesk.web.chart import spend_chart
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import api_keys_of, now_of, page, store_of, zone_of
from ai_newsdesk.web.estimate import choices_of, frequency_options, profile_from, rates_for
from ai_newsdesk.web.forms import NARRATION_MODELS, parse_settings_form
from ai_newsdesk.web.keys import KEY_LABELS, MAX_KEY_CHARS, scrub
from ai_newsdesk.web.runs import run_dict

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])
log = logging.getLogger("ai_newsdesk.web.settings")

SAVED = {
    "settings": "Settings saved.",
    "key": "Key tested and saved.",
    "cleared": "Key cleared. The .env value applies now, if there is one.",
    "voices": "Voice list refreshed.",
    "voices_kept": "Could not reach ElevenLabs, so kept the old list.",
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
    "narration_monthly_chars",
    "narration_voice_id",
    "narration_model",
)
SAMPLE_MAX_BYTES = 2 * 1024 * 1024
SAMPLE_TIMEOUT_S = 15.0
QUOTA_TTL = timedelta(minutes=10)


def _values_from(runtime: RuntimeSettings) -> dict[str, str]:
    values = {key: str(getattr(runtime, key)) for key in FIELDS}
    values["monthly_budget_usd"] = f"{runtime.monthly_budget_usd:.2f}"
    values["why_it_matters"] = "on" if runtime.why_it_matters else ""
    values["narration_enabled"] = "on" if runtime.narration_enabled else ""
    return values


def _values_echo(form: dict[str, str]) -> dict[str, str]:
    values = {key: form.get(key, "") for key in FIELDS}
    values["why_it_matters"] = "on" if form.get("why_it_matters") == "on" else ""
    values["narration_enabled"] = "on" if form.get("narration_enabled") == "on" else ""
    return values


def _voice_label(voice: VoiceInfo) -> str:
    traits = [voice.labels[k] for k in ("accent", "gender") if voice.labels.get(k)]
    return f"{voice.name} ({', '.join(traits)})" if traits else voice.name


def _voice_groups(
    voices: list[VoiceInfo], current: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str | None]:
    """The dropdown: Suggested (always), the account's other voices by name, and the current
    voice's id when it is in neither."""
    suggested = [(vid, f"{name} ({note})") for vid, name, note in SUGGESTED_VOICES]
    skip = {vid for vid, _ in suggested}
    rest = sorted(
        ((v.voice_id, _voice_label(v)) for v in voices if v.voice_id not in skip),
        key=lambda pair: pair[1].lower(),
    )
    known = skip | {vid for vid, _ in rest}
    return suggested, rest, None if current in known else current


async def _quota(request: Request, now: datetime) -> QuotaInfo | None:
    """ElevenLabs' own count, cached in memory for 10 minutes (a failure too). None: unknown."""
    app: FastAPI = request.app
    cached: tuple[datetime, QuotaInfo | None] | None = getattr(app.state, "quota_cache", None)
    if cached is not None and now - cached[0] < QUOTA_TTL:
        return cached[1]
    try:
        info = await app.state.deps.narrator.quota()
    except Exception as exc:  # the meter still shows our own count
        log.warning("ElevenLabs quota check failed (%s)", type(exc).__name__)
        info = None
    app.state.quota_cache = (now, info)
    return info


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
    tts = app.state.deps.tts_catalogue
    has_tts_key = bool(await api_keys.resolve_api_key("elevenlabs"))
    data = tts.cached()
    suggested, others, odd = _voice_groups(
        data.voices if data is not None else [], form.get("narration_voice_id", "")
    )
    tts_chars = await store.month_tts_characters(now, settings.tz)
    quota = await _quota(request, now) if has_tts_key else None
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
            "voices_suggested": suggested,
            "voices_all": others,
            "voice_odd": odd,
            "narration_models": NARRATION_MODELS,
            "tts_chars": tts_chars,
            "tts_cap": runtime.narration_monthly_chars,
            "tts_quota": quota,
            "tts_quota_left": (
                max(0, quota.character_limit - quota.character_count) if quota else None
            ),
            "has_tts_key": has_tts_key,
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
    from ai_newsdesk import app as app_module  # late: app.py imports this package

    app: FastAPI = request.app
    # A stale or missing voice list refreshes in the background, only with a key.
    if app.state.deps.tts_catalogue.is_stale() and await api_keys_of(request).resolve_api_key(
        "elevenlabs"
    ):
        app_module.refresh_voices_in_background(app.state.ctx, app.state.background)
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
    request.app.state.quota_cache = None
    await request.app.state.rebuild_context()
    return RedirectResponse(f"/settings?saved=key#key-{key_name}", status_code=303)


@router.post("/settings/keys/{name}/clear")
async def clear_api_key(request: Request, name: str) -> Response:
    key_name = _key_name(name)
    await api_keys_of(request).clear_api_key(key_name)
    request.app.state.quota_cache = None
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


@router.post("/settings/voices/refresh")
async def refresh_voices(request: Request) -> Response:
    refreshed = await request.app.state.deps.tts_catalogue.refresh()
    flash = "voices" if refreshed else "voices_kept"
    return RedirectResponse(f"/settings?saved={flash}#read-aloud", status_code=303)


def _sample_key_rule(url: str) -> bool | None:
    """None: not on the allow-list. True: an ElevenLabs host, which may be sent the key."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port not in (None, 443) or "@" in parts.netloc:
        return None
    if ".." in parts.path.split("/"):
        return None
    if host == "storage.googleapis.com" and parts.path.startswith("/eleven-public-prod/"):
        return False
    if host == "elevenlabs.io" or host.endswith(".elevenlabs.io"):
        return True
    return None


@router.get("/settings/voice-sample/{voice_id}")
async def voice_sample(request: Request, voice_id: str) -> Response:
    """A voice's free preview, fetched server side so the browser stays on our origin."""
    app: FastAPI = request.app
    data = app.state.deps.tts_catalogue.cached()
    voice = next((v for v in data.voices if v.voice_id == voice_id), None) if data else None
    url = voice.preview_url if voice is not None else None
    send_key = _sample_key_rule(url) if url else None
    if url is None or send_key is None:
        raise HTTPException(status_code=404, detail="There is no sample for that voice.")
    headers: dict[str, str] = {}
    if send_key:
        key = await api_keys_of(request).resolve_api_key("elevenlabs")
        if key:
            headers["xi-api-key"] = key
    http: httpx.AsyncClient = app.state.http
    body = bytearray()
    kind = "audio/mpeg"
    try:
        async with asyncio.timeout(SAMPLE_TIMEOUT_S):
            async with http.stream(
                "GET",
                url,
                headers=headers,
                timeout=SAMPLE_TIMEOUT_S,
                follow_redirects=False,
            ) as resp:
                kind = resp.headers.get("content-type", "audio/mpeg").split(";")[0].strip()
                length = resp.headers.get("content-length", "")
                if (
                    resp.status_code != 200
                    or not kind.startswith("audio/")
                    or (length.isdigit() and int(length) > SAMPLE_MAX_BYTES)
                ):
                    raise HTTPException(status_code=502, detail="The sample could not be loaded.")
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > SAMPLE_MAX_BYTES:
                        raise HTTPException(
                            status_code=502, detail="The sample could not be loaded."
                        )
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="The sample took too long.") from None
    except httpx.HTTPError as exc:  # never echo the error: it may carry the URL or key
        log.warning("voice sample fetch failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="The sample could not be loaded.") from None
    return Response(
        bytes(body),
        media_type=kind,
        headers={"Cache-Control": "private, max-age=86400"},
    )
