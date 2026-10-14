"""The Models page's flow diagram: how and when the desk uses its AI models and its tools, end
to end, with each AI step showing the provider and model chosen now.

The steps and their order follow the code: scheduler.py (slots, briefing time, nightly upkeep),
pipeline.run_once (collect, pre-filter, enrich, judge, cluster, consolidate, write), the Today
ranking (taste.py) and pipeline.run_briefing. Pure functions over the runtime settings and the
PriceBook; the template only renders what is built here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.pipeline import BRIEFING_STORY_LIMIT, REVIEW_BATCH, WRITE_LIMIT
from ai_newsdesk.pricing import UnknownModelError
from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.scheduler import NIGHTLY_AT
from ai_newsdesk.web.model_choices import PROVIDER_NAMES, price_text

StepKind = Literal["ai", "tool"]


@dataclass(frozen=True)
class FlowModel:
    """The model an AI step uses now."""

    job: str  # the Models row it is chosen in
    label: str
    provider: str  # display name
    model: str
    price: str | None  # None: no known price


@dataclass(frozen=True)
class FlowStep:
    key: str
    kind: StepKind  # "ai": a paid model call, budget-checked; "tool": code or a tool, no AI cost
    title: str
    when: str
    text: str
    model: FlowModel | None = None
    branch: str | None = None  # a side path out of this step


def _model(job: str, label: str, provider: str, model: str, prices: PriceBook) -> FlowModel:
    try:
        price: str | None = price_text(prices.price(provider, model))
    except UnknownModelError:
        price = None
    return FlowModel(job, label, PROVIDER_NAMES.get(provider, provider), model, price)


def _times(n: int) -> str:
    return {1: "Once a day", 2: "Twice a day"}.get(n, f"{n} times a day")


def _schedule(rs: RuntimeSettings, scheduler_on: bool) -> FlowStep:
    times = _times(rs.runs_per_day)
    if scheduler_on:
        return FlowStep(
            "schedule",
            "tool",
            "Schedule",
            times,
            f"{times}, evenly spaced from midnight, or straight away with Refresh now. Runs"
            " never overlap.",
        )
    return FlowStep(
        "schedule",
        "tool",
        "Schedule",
        "Scheduler off",
        "The scheduler is switched off (SCHEDULER_ENABLED=false), so runs, the briefing and"
        f" the nightly upkeep wait; Refresh now still runs. When it is on: {times.lower()},"
        " evenly spaced from midnight.",
    )


def _judge_text(rs: RuntimeSettings) -> str:
    per_call = (
        "one call per item" if rs.judge_provider == "typesafe" else f"{REVIEW_BATCH} items a call"
    )
    return (
        f"Scores each item's signal from 0 to 10 and flags junk, {per_call}, up to"
        f" {rs.daily_review_cap} items a day."
    )


def flow_steps(
    rs: RuntimeSettings, prices: PriceBook, *, scheduler_on: bool = True
) -> list[FlowStep]:
    each_run = "Each run"
    nightly = f"Nightly, {NIGHTLY_AT:%H:%M}"
    why = " and a “why it matters” line" if rs.why_it_matters else ""
    return [
        _schedule(rs, scheduler_on),
        FlowStep(
            "collect",
            "tool",
            "Collect",
            each_run,
            "Lab feeds and blogs (RSS), Hacker News, arXiv and GitHub releases, plus"
            " research-engine searches: topics, and people's X posts (site:x.com).",
        ),
        FlowStep(
            "prefilter",
            "tool",
            "Clean, dedupe, pre-filter",
            each_run,
            "Tidies links, spots near-duplicate titles and drops weak items by source trust,"
            " your source weights, Hacker News points and the blocklist. No AI.",
        ),
        FlowStep(
            "read",
            "tool",
            "Read the article",
            each_run,
            "Items with only a headline or a short snippet get an article excerpt through"
            " research-engine. X pages are never fetched.",
        ),
        FlowStep(
            "judge",
            "ai",
            "Judge",
            each_run,
            _judge_text(rs),
            _model("judge", "Judge", rs.judge_provider, rs.judge_model, prices),
        ),
        FlowStep(
            "group",
            "tool",
            "Group into stories",
            each_run,
            f"Items scoring {rs.show_threshold} or more join the story the judge matched,"
            " or start a new one.",
            branch="X posts skip this and go to the X page, not into stories.",
        ),
        FlowStep(
            "merge",
            "ai",
            "Duplicate merger",
            each_run,
            "Merges open stories that report the same event, so each event is written once.",
            _model(
                "consolidator",
                "Duplicate merger",
                rs.consolidator_provider,
                rs.consolidator_model,
                prices,
            ),
        ),
        FlowStep(
            "write",
            "ai",
            "Summary writer",
            each_run,
            f"Writes a short summary{why} for each new or changed story, up to"
            f" {WRITE_LIMIT} a run.",
            _model("writer", "Summary writer", rs.writer_provider, rs.writer_model, prices),
        ),
        FlowStep(
            "rank",
            "tool",
            "Rank on Today",
            "Each visit",
            "Orders stories by signal, adjusted by the taste model learned from your thumbs."
            " No AI.",
        ),
        FlowStep(
            "briefing",
            "ai",
            "Briefing writer",
            f"Daily, {rs.briefing_time}",
            f"Writes the morning briefing from the top {BRIEFING_STORY_LIMIT} stories; if it"
            " fails, it tries again every 30 minutes.",
            _model("briefing", "Briefing writer", rs.briefing_provider, rs.briefing_model, prices),
        ),
        FlowStep(
            "nightly",
            "tool",
            "Nightly upkeep",
            nightly,
            "Updates source weights from your thumbs, refits the taste model and refreshes the"
            " model list and prices from OpenRouter. Backups are manual (make backup).",
        ),
    ]
