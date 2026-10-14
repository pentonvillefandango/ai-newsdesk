"""GET /health: open, JSON, never cached. 200 while the database answers, 503 otherwise."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ai_newsdesk.web.deps import NO_STORE
from ai_newsdesk.web.runs import run_dict

router = APIRouter(include_in_schema=False)
log = logging.getLogger("ai_newsdesk.web.health")


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    state = request.app.state
    scheduler = getattr(state, "scheduler", None)
    body: dict[str, object] = {
        "status": "up",
        "db": True,
        "scheduler": bool(scheduler is not None and scheduler.alive),
        "last_run": None,
        "git_sha": state.settings.git_sha,
    }
    try:
        await state.store.get_setting("runs_per_day")
        runs = await state.store.last_runs(1)
    except Exception:
        log.exception("health check: database query failed")
        body.update(status="down", db=False)
        return JSONResponse(body, status_code=503, headers=NO_STORE)
    if runs:
        body["last_run"] = run_dict(runs[0])
    return JSONResponse(body, headers=NO_STORE)
