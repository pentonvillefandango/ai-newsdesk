"""Run records as plain JSON-able dicts (for /health and the Settings page)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel

RUN_FIELDS = ("trigger", "started_at", "finished_at", "cost_usd", "error")


def _jsonable(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


def run_dict(run: object) -> dict[str, Any]:
    """Accepts whatever `Store.last_runs` returns per row: a pydantic model or a mapping."""
    if isinstance(run, BaseModel):
        data: Mapping[str, Any] = run.model_dump(mode="json")
    else:
        mapping = cast(Mapping[str, Any], run)
        data = dict(mapping)
    return {key: _jsonable(data.get(key)) for key in RUN_FIELDS}
