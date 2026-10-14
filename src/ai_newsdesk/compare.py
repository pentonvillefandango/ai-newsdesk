"""The Compare page's engine (multi-provider spec 4): the bake-off of ADR-0001, in the app.

A comparison runs 2-4 models over a sample with the production job classes (LLMJudge or
LLMWriter), their prompts and the pipeline's batching. Every call is pre-checked against both
the comparison's cap and the month's budget (CompareGuard) and recorded as spend. Outputs go
to compare_results only: no verdict is saved and no summary replaced. The run holds the
pipeline lock (taken by the caller), so each call and the whole run are time-limited.

The metrics are pure functions over stored results, so they are recomputed whenever the owner
labels an item or picks a preferred summary.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from typing import Literal

from ai_newsdesk.budget import Budget
from ai_newsdesk.judges.llm import LLMJudge
from ai_newsdesk.llm.base import ModelClient
from ai_newsdesk.llm.factory import UnusableModelError
from ai_newsdesk.models import ReviewInput, StoryText, Usage, Verdict, WriteInput
from ai_newsdesk.pipeline import REVIEW_BATCH, WRITE_BATCH, judge_inputs, stored_signal
from ai_newsdesk.protocols import Clock
from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.store.records import (
    CompareItem,
    CompareJob,
    CompareModel,
    CompareResult,
    CompareStatus,
)
from ai_newsdesk.store.store import Store
from ai_newsdesk.writer import LLMWriter

log = logging.getLogger(__name__)

JOB_LABELS: dict[CompareJob, str] = {"judge": "Judge", "writer": "Summary writer"}
SAMPLE_DEFAULT: dict[CompareJob, int] = {"judge": 30, "writer": 5}
SAMPLE_MAX: dict[CompareJob, int] = {"judge": 100, "writer": 15}
MIN_MODELS, MAX_MODELS = 2, 4
CAP_DEFAULT, CAP_MIN, CAP_MAX = 0.25, 0.01, 5.0
# The run holds the pipeline lock, which a scheduled run waits up to 20 min for: bound it.
CALL_TIMEOUT_S = 120
RUN_TIMEOUT_S = 15 * 60
MAX_RETRIES = 1  # per call, for the comparison's SDK clients
TOO_LONG = "The comparison took longer than 15 minutes, so it was stopped."
INTERRUPTED = "The app restarted during the comparison."

Subject = ReviewInput | WriteInput
ClientFor = Callable[[str, str], ModelClient]  # raises UnusableModelError


# ---- samples ------------------------------------------------------------------------------


async def judge_sample(store: Store, now: datetime, n: int) -> list[ReviewInput]:
    """The last n reviewed or queued news items of 7 days, as the production judge gets them
    (body text and candidate stories)."""
    inputs, _, _ = await judge_inputs(store, await store.compare_judge_inputs(now, n), now)
    return inputs


async def writer_sample(
    store: Store, now: datetime, n: int, runtime: RuntimeSettings
) -> list[WriteInput]:
    """The top n stories of 48 h by score, as the production writer gets them."""
    return await store.compare_writer_inputs(now, n, runtime.why_it_matters)


def subject_id(subject: Subject) -> int:
    return subject.item_id if isinstance(subject, ReviewInput) else subject.story_id


# ---- one model ----------------------------------------------------------------------------

Outputs = dict[int, dict[str, object]]  # subject id -> the output as JSON


@dataclass(frozen=True)
class _Runner:
    """The production job over one client, one call per batch of the pipeline's size."""

    batch_size: int
    worst_case: Callable[[list[Subject]], float]
    call: Callable[[list[Subject]], Awaitable[tuple[Outputs, list[Usage]]]]

    def batches(self, sample: Sequence[Subject]) -> list[list[Subject]]:
        size = self.batch_size
        return [list(sample[i : i + size]) for i in range(0, len(sample), size)]


def _runner(job: CompareJob, client: ModelClient) -> _Runner:
    if job == "judge":
        judge = LLMJudge(client)

        async def review(batch: list[Subject]) -> tuple[Outputs, list[Usage]]:
            result = await judge.review(_judge_inputs(batch))
            return {v.item_id: v.model_dump(mode="json") for v in result.verdicts}, result.usage

        return _Runner(
            REVIEW_BATCH, lambda batch: judge.worst_case_cost(_judge_inputs(batch)), review
        )
    writer = LLMWriter(client, client)  # the briefing client is never used here

    async def write(batch: list[Subject]) -> tuple[Outputs, list[Usage]]:
        texts, usage = await writer.write_stories(_write_inputs(batch))
        return {t.story_id: t.model_dump(mode="json") for t in texts}, usage

    return _Runner(
        WRITE_BATCH, lambda batch: writer.worst_case_cost_stories(_write_inputs(batch)), write
    )


def _judge_inputs(batch: list[Subject]) -> list[ReviewInput]:
    return [s for s in batch if isinstance(s, ReviewInput)]


def _write_inputs(batch: list[Subject]) -> list[WriteInput]:
    return [s for s in batch if isinstance(s, WriteInput)]


def max_cost_usd(
    job: CompareJob, clients: Sequence[ModelClient], sample: Sequence[Subject]
) -> float:
    """The most the comparison can cost: every call's worst case, worked out by the jobs
    themselves with the production batching."""
    total = 0.0
    for client in clients:
        runner = _runner(job, client)
        total += sum(runner.worst_case(batch) for batch in runner.batches(sample))
    return total


class CompareGuard:
    """The comparison's spend guard over Budget: a call goes ahead only if its worst case fits
    in both what is left of the comparison's cap and what is left of the month."""

    def __init__(self, budget: Budget, cap_usd: float) -> None:
        self.budget = budget
        self.cap_usd = cap_usd
        self.recorded = 0.0  # spend recorded through Budget
        self._held = 0.0  # worst cases of calls whose usage is unknown (timed out)

    def remaining(self) -> float:
        return max(0.0, self.cap_usd - self.recorded - self._held)

    def can_afford(self, worst_case_usd: float) -> bool:
        return worst_case_usd <= self.remaining() and self.budget.can_afford(worst_case_usd)

    async def record(self, usage: Usage) -> float:
        cost = await self.budget.record(usage)
        self.recorded += cost
        return cost

    def hold(self, worst_case_usd: float) -> None:
        """A call that timed out may still be billed: count its worst case against the cap."""
        self._held += worst_case_usd


async def _run_model(
    store: Store,
    run_id: int,
    model: CompareModel,
    runner: _Runner,
    sample: Sequence[Subject],
    guard: CompareGuard,
    call_timeout_s: float,
) -> bool:
    """Runs one model over the sample; False when a call didn't fit (the rest not run)."""
    batches = runner.batches(sample)
    for n, batch in enumerate(batches):
        worst = runner.worst_case(batch)
        if not guard.can_afford(worst):
            rest = [s for b in batches[n:] for s in b]
            await store.add_compare_results(
                run_id,
                [
                    CompareResult(model_key=model.key, subject_id=subject_id(s), ok=False)
                    for s in rest
                ],
            )
            log.info("comparison %d: %s stopped at the cap", run_id, model.key)
            return False
        started = time.perf_counter()
        try:
            async with asyncio.timeout(call_timeout_s):
                outputs, usage = await runner.call(batch)
        except TimeoutError:
            log.warning("comparison %d: %s did not answer in time", run_id, model.key)
            outputs, usage = {}, []
            guard.hold(worst)
        latency_ms = round((time.perf_counter() - started) * 1000)
        cost = 0.0
        for u in usage:
            cost += await guard.record(u)
        share = cost / len(batch)
        await store.add_compare_results(
            run_id,
            [
                CompareResult(
                    model_key=model.key,
                    subject_id=subject_id(s),
                    output=outputs.get(subject_id(s)),
                    ok=subject_id(s) in outputs,
                    latency_ms=latency_ms,
                    cost_usd=share,
                )
                for s in batch
            ],
        )
    return True


async def run_comparison(
    store: Store,
    run_id: int,
    job: CompareJob,
    models: Sequence[CompareModel],
    sample: Sequence[Subject],
    *,
    cap_usd: float,
    client_for: ClientFor,
    budget: Budget,
    clock: Clock,
    call_timeout_s: float = CALL_TIMEOUT_S,
    run_timeout_s: float = RUN_TIMEOUT_S,
) -> CompareStatus:
    """Runs the comparison and always finishes its row: `done`, `capped` (a model stopped at a
    cap) or `failed` (unusable model, time limit or an unexpected error)."""
    guard = CompareGuard(budget, cap_usd)
    status: CompareStatus = "done"
    error: str | None = None
    try:
        runners = [(m, _runner(job, client_for(m.provider, m.model))) for m in models]
        async with asyncio.timeout(run_timeout_s):
            for model, runner in runners:
                if not await _run_model(
                    store, run_id, model, runner, sample, guard, call_timeout_s
                ):
                    status = "capped"
    except UnusableModelError as exc:  # the message is ours and never holds a key
        status, error = "failed", str(exc)
    except TimeoutError:
        log.warning("comparison %d stopped at the time limit", run_id)
        status, error = "failed", TOO_LONG
    except asyncio.CancelledError:
        status, error = "failed", INTERRUPTED
        raise
    except Exception as exc:
        log.exception("comparison %d failed", run_id)
        status, error = "failed", f"The comparison failed ({type(exc).__name__})."
    finally:
        await store.finish_compare_run(run_id, status, clock.now(), guard.recorded, error)
    return status


# ---- metrics ------------------------------------------------------------------------------

CellState = Literal["ok", "failed", "not_run"]


def is_shown(verdict: Verdict, weight: float, threshold: int) -> bool:
    """Production's rule: an item joins a story on Today when its stored signal (junk is 0,
    then the source weight) reaches the show threshold."""
    return stored_signal(verdict, weight) >= threshold


def _verdict(result: CompareResult | None) -> Verdict | None:
    if result is None or not result.ok or result.output is None:
        return None
    return Verdict.model_validate(result.output)


def _rate(hits: float, total: float) -> float | None:
    return hits / total if total else None


@dataclass(frozen=True)
class JudgeSummary:
    key: str
    provider: str
    model: str
    shown: int
    hidden: int
    junk: int
    avg_signal: float | None
    agree_production: float | None  # 0..1 over items with a production verdict
    production_n: int
    agree_thumbs: float | None  # 0..1 over items whose story has a thumb
    thumbs_n: int
    accuracy: float | None  # 0..1 over labelled items, important counting double
    labels_n: int
    failures: int
    not_run: int
    cost_usd: float
    avg_latency_ms: float | None


def _by_key(results: Sequence[CompareResult]) -> dict[tuple[str, int], CompareResult]:
    return {(r.model_key, r.subject_id): r for r in results}


def judge_summary(
    models: Sequence[CompareModel],
    items: Mapping[int, CompareItem],
    results: Sequence[CompareResult],
    threshold: int,
) -> list[JudgeSummary]:
    """One row per model. Items no longer in the database are left out."""
    rows: list[JudgeSummary] = []
    for m in models:
        mine = [r for r in results if r.model_key == m.key and r.subject_id in items]
        shown = hidden = junk = 0
        signals: list[int] = []
        prod_hits = prod_n = thumb_hits = thumb_n = 0
        label_hits = label_weight = 0.0
        labels_n = 0
        for r in mine:
            verdict = _verdict(r)
            if verdict is None:
                continue
            item = items[r.subject_id]
            show = is_shown(verdict, item.weight, threshold)
            shown, hidden = shown + show, hidden + (not show)
            junk += verdict.junk
            signals.append(verdict.signal)
            if item.verdict is not None:
                prod_n += 1
                prod_hits += show == is_shown(item.verdict, item.weight, threshold)
            if item.feedback:
                thumb_n += 1
                thumb_hits += show == (item.feedback > 0)
            if item.label is not None:
                weight = 2.0 if item.label == "important" else 1.0
                labels_n += 1
                label_weight += weight
                label_hits += weight * (show == (item.label != "junk"))
        latencies = [r.latency_ms for r in mine if r.latency_ms is not None]
        rows.append(
            JudgeSummary(
                key=m.key,
                provider=m.provider,
                model=m.model,
                shown=shown,
                hidden=hidden,
                junk=junk,
                avg_signal=fmean(signals) if signals else None,
                agree_production=_rate(prod_hits, prod_n),
                production_n=prod_n,
                agree_thumbs=_rate(thumb_hits, thumb_n),
                thumbs_n=thumb_n,
                accuracy=_rate(label_hits, label_weight),
                labels_n=labels_n,
                failures=sum(1 for r in mine if r.ran and not r.ok),
                not_run=sum(1 for r in mine if not r.ran),
                cost_usd=sum(r.cost_usd for r in mine),
                avg_latency_ms=fmean(latencies) if latencies else None,
            )
        )
    return rows


@dataclass(frozen=True)
class JudgeCell:
    state: CellState
    verdict: Verdict | None
    shown: bool | None


@dataclass(frozen=True)
class JudgeItemRow:
    item: CompareItem
    production_shown: bool | None
    cells: list[JudgeCell]  # in model order
    disagree: bool  # the models that answered don't all make the same show/hide call


def _cell(result: CompareResult | None, item: CompareItem, threshold: int) -> JudgeCell:
    verdict = _verdict(result)
    if verdict is not None:
        return JudgeCell("ok", verdict, is_shown(verdict, item.weight, threshold))
    state: CellState = "failed" if result is not None and result.ran else "not_run"
    return JudgeCell(state, None, None)


def judge_items(
    models: Sequence[CompareModel],
    items: Mapping[int, CompareItem],
    results: Sequence[CompareResult],
    threshold: int,
    order: Sequence[int] | None = None,
) -> list[JudgeItemRow]:
    """Per-item rows in sample order (`order`, else item id)."""
    found = _by_key(results)
    rows: list[JudgeItemRow] = []
    for item_id in order if order is not None else sorted(items):
        item = items.get(item_id)
        if item is None:
            continue
        cells = [_cell(found.get((m.key, item_id)), item, threshold) for m in models]
        calls = {c.shown for c in cells if c.shown is not None}
        production = (
            is_shown(item.verdict, item.weight, threshold) if item.verdict is not None else None
        )
        rows.append(JudgeItemRow(item, production, cells, len(calls) > 1))
    return rows


def best_judge(rows: Sequence[JudgeSummary]) -> str | None:
    """Highest accuracy against the labels; without labels, highest agreement with the
    production judge; else none. Ties go to the first model."""
    for rate in (lambda r: r.accuracy, lambda r: r.agree_production):
        scored = [(value, r.key) for r in rows if (value := rate(r)) is not None]
        if scored:
            best = max(value for value, _ in scored)
            return next(key for value, key in scored if value == best)
    return None


@dataclass(frozen=True)
class WriterSummary:
    key: str
    provider: str
    model: str
    written: int
    failures: int
    not_run: int
    preferred: int
    cost_usd: float
    avg_latency_ms: float | None


def writer_summary(
    models: Sequence[CompareModel],
    results: Sequence[CompareResult],
    prefs: Mapping[int, str],
) -> list[WriterSummary]:
    tally = list(prefs.values())
    rows: list[WriterSummary] = []
    for m in models:
        mine = [r for r in results if r.model_key == m.key]
        latencies = [r.latency_ms for r in mine if r.latency_ms is not None]
        rows.append(
            WriterSummary(
                key=m.key,
                provider=m.provider,
                model=m.model,
                written=sum(1 for r in mine if r.ok),
                failures=sum(1 for r in mine if r.ran and not r.ok),
                not_run=sum(1 for r in mine if not r.ran),
                preferred=tally.count(m.key),
                cost_usd=sum(r.cost_usd for r in mine),
                avg_latency_ms=fmean(latencies) if latencies else None,
            )
        )
    return rows


def best_writer(rows: Sequence[WriterSummary]) -> str | None:
    """The most preferred summaries; none until the owner has preferred one."""
    top = max((r.preferred for r in rows), default=0)
    return next((r.key for r in rows if r.preferred == top), None) if top else None


@dataclass(frozen=True)
class WriterStoryRow:
    story_id: int
    texts: list[StoryText | None]  # in model order; None: failed or not run
    states: list[CellState]


def writer_stories(
    models: Sequence[CompareModel], order: Sequence[int], results: Sequence[CompareResult]
) -> list[WriterStoryRow]:
    found = _by_key(results)
    rows: list[WriterStoryRow] = []
    for story_id in order:
        texts: list[StoryText | None] = []
        states: list[CellState] = []
        for m in models:
            r = found.get((m.key, story_id))
            if r is not None and r.ok and r.output is not None:
                texts.append(StoryText.model_validate(r.output))
                states.append("ok")
            else:
                texts.append(None)
                states.append("failed" if r is not None and r.ran else "not_run")
        rows.append(WriterStoryRow(story_id, texts, states))
    return rows
