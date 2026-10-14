"""Read models the Store returns that are not part of the shared pipeline models (V1-S1)."""

import datetime as dt

from pydantic import BaseModel, Field

from ai_newsdesk.models import SourceKind, Trust


class SourceStats(BaseModel):
    """One row of the Sources page."""

    id: int
    kind: SourceKind
    name: str
    trust: Trust
    enabled: bool
    weight: float
    last_run_at: dt.datetime | None
    last_ok_at: dt.datetime | None
    last_error: str | None
    last_count: int
    last_duration_s: float
    item_count: int


class BriefingRecord(BaseModel):
    date: dt.date
    model: str
    body: str
    story_ids: list[int] = Field(default_factory=list)
    created_at: dt.datetime


class RunRecord(BaseModel):
    id: int
    trigger: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    counts: dict[str, int] = Field(default_factory=dict)
    cost_usd: float
    error: str | None
