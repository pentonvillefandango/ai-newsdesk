"""Read models the Store returns that are not part of the shared pipeline models (V1-S1)."""

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field

from ai_newsdesk.models import SourceKind, Trust, Verdict


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


CompareJob = Literal["judge", "writer"]
CompareStatus = Literal["running", "done", "capped", "failed"]
Label = Literal["junk", "show", "important"]
LABELS: tuple[Label, ...] = ("junk", "show", "important")


class CompareModel(BaseModel):
    """One model in a comparison; `key` names it in results and preferences."""

    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


class CompareRun(BaseModel):
    id: int
    job: CompareJob
    models: list[CompareModel]
    sample_ids: list[int]  # item ids (judge) or story ids (writer), in sample order
    cap_usd: float
    status: CompareStatus
    started_at: dt.datetime
    finished_at: dt.datetime | None
    cost_usd: float
    error: str | None


class CompareResult(BaseModel):
    """One model's output for one subject. latency_ms None: never sent (cap or time limit)."""

    model_key: str
    subject_id: int
    output: dict[str, Any] | None = None  # a Verdict (judge) or StoryText (writer) as JSON
    ok: bool
    latency_ms: int | None = None
    cost_usd: float = 0.0

    @property
    def ran(self) -> bool:
        return self.latency_ms is not None


class CompareItem(BaseModel):
    """A judge comparison's item with what the metrics compare against."""

    item_id: int
    title: str
    url: str
    source_name: str
    weight: float  # its source's weight, for the production "shown" rule
    verdict: Verdict | None  # the production judge's, if any
    feedback: int  # its story's thumb: 1, -1 or 0
    label: Label | None
