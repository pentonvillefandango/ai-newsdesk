"""One collection-and-review run, and the daily briefing.

Failures are local: a source, an item or an AI batch that fails is recorded and the run carries
on. After clustering, `consolidate.consolidate` merges open stories that report the same event
(one Claude call), before the writer runs. Every AI call is preceded by a budget pre-check using
that call's worst-case cost; a call cut off by shutdown may still be billed, so it is recorded at
that worst case. Nothing is retried within a run: an item the judge fails on is
marked `review_failed` (and shown), and `Store.queued_review_inputs` offers it again on the next
run, up to MAX_REVIEW_ATTEMPTS; a story the writer leaves out is counted with
`Store.mark_text_failed` and offered again the same way. An item deleted mid-run (its source
removed) is skipped with a warning, so the other verdicts are still saved; items a crashed run
left `new` are put back through the pre-filter by the next run; and the runs row is always
finished, with error "cancelled" when the task is cancelled.

`run_narration` is the one code path that turns a day's briefing into an MP3 (feedback-3). It
never takes the scheduler lock: its callers hold it.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from ai_newsdesk import cluster, prefilter
from ai_newsdesk.audio_files import AudioFiles
from ai_newsdesk.briefing import briefing_date_label
from ai_newsdesk.budget import Budget
from ai_newsdesk.config import Settings
from ai_newsdesk.consolidate import ConsolidationResult, consolidate
from ai_newsdesk.dedupe import DEDUPE_WINDOW_HOURS, find_story_link
from ai_newsdesk.enrich import enrich_bodies
from ai_newsdesk.models import (
    Candidate,
    ItemStatus,
    Purpose,
    ReviewInput,
    SourceConfig,
    SourceKind,
    SourceResult,
    Verdict,
)
from ai_newsdesk.narration import chunks, speech_text
from ai_newsdesk.protocols import (
    NO_NARRATION_KEY,
    Clock,
    Consolidator,
    Judge,
    Narrator,
    ResearchEngine,
    Source,
    Writer,
)
from ai_newsdesk.runtime_settings import RuntimeSettings, load_runtime_settings
from ai_newsdesk.store.records import BriefingAudioRecord
from ai_newsdesk.store.store import Store
from ai_newsdesk.tts.catalogue import ElevenLabsCatalogue, fallback_voice_name

log = logging.getLogger(__name__)

SOURCE_TIMEOUT_S = 60.0
REVIEW_BATCH = 10
WRITE_BATCH = 10
WRITE_LIMIT = 50
QUEUE_EXPIRY_HOURS = 48
BRIEFING_STORY_LIMIT = 15
AUDIO_KEEP_DAYS = 30
AUDIO_TEMP_MAX_AGE = timedelta(hours=1)

COUNT_KEYS = (
    "sources",
    "sources_failed",
    "collected",
    "new",
    "recovered",
    "near_duplicates",
    "dropped",
    "queued",
    "enrich_failed",
    "expired",
    "reviewed",
    "review_failed",
    "budget_skipped",
    "stories_touched",
    "stories_merged",
    "consolidate_skipped",
    "written",
    "write_failed",
)


@dataclass
class PipelineContext:
    store: Store
    sources: dict[SourceKind, Source]
    research_engine: ResearchEngine
    judge: Judge
    writer: Writer
    budget: Budget
    clock: Clock
    settings: Settings
    consolidator: Consolidator | None = None  # None: the consolidation step is off
    # Read aloud (feedback-3). No narrator or no audio directory: narration is off.
    narrator: Narrator | None = None
    audio: AudioFiles | None = None
    tts_catalogue: ElevenLabsCatalogue | None = None  # None: suggested names or ids, factor 1.0
    # Whether an ElevenLabs key resolves; None leaves it to the narrator's own answer.
    has_narration_key: Callable[[], Awaitable[bool]] | None = None


@dataclass
class RunReport:
    run_id: int
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNT_KEYS, 0))
    cost_usd: float = 0.0
    error: str | None = None


def stored_signal(verdict: Verdict, source_weight: float) -> int:
    if verdict.junk:
        return 0
    return max(0, min(10, round(verdict.signal * source_weight)))


def collection_window(runs_per_day: int) -> timedelta:
    return timedelta(hours=max(6.0, 48.0 / max(1, runs_per_day)))


def with_runtime_defaults(src: SourceConfig, rs: RuntimeSettings) -> SourceConfig:
    """Fill per-kind config from runtime settings: an `hn` source without its own `min_points`
    gets `hn_min_points` (master plan R9)."""
    if src.kind == SourceKind.HN and "min_points" not in src.config:
        return src.model_copy(update={"config": {**src.config, "min_points": rs.hn_min_points}})
    return src


def _affordable[T](
    budget: Budget, estimate: Callable[[list[T]], float], items: list[T]
) -> float | None:
    """The call's worst case when the budget covers it, else None (the call is not sent)."""
    try:
        worst = estimate(items)
    except Exception:  # an estimate we cannot make is a call we do not send
        log.exception("worst-case cost estimate failed; call skipped")
        return None
    return worst if budget.can_afford(worst) else None


async def _collect_one(adapter: Source | None, src: SourceConfig, since: datetime) -> SourceResult:
    if adapter is None:
        return SourceResult(source_id=src.id, error=f"no adapter for kind {src.kind.value}")
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        result = await asyncio.wait_for(adapter.collect(src, since), SOURCE_TIMEOUT_S)
    except TimeoutError:
        return SourceResult(
            source_id=src.id,
            error=f"timed out after {SOURCE_TIMEOUT_S:.0f} s",
            duration_s=loop.time() - started,
        )
    except Exception as exc:  # one source never fails the run
        return SourceResult(
            source_id=src.id,
            error=f"{type(exc).__name__}: {exc}"[:300],
            duration_s=loop.time() - started,
        )
    return result.model_copy(update={"source_id": src.id})


async def run_once(ctx: PipelineContext, trigger: str) -> RunReport:
    run_id = await ctx.store.start_run(trigger, ctx.clock.now())
    report = RunReport(run_id=run_id)
    try:
        await _run(ctx, report)
    except asyncio.CancelledError:  # shutdown: finish the run row, then let it propagate
        log.warning("run %d cancelled", run_id)
        report.error = "cancelled"
        raise
    except Exception as exc:  # recorded on the run, never raised
        log.exception("run %d failed", run_id)
        report.error = f"{type(exc).__name__}: {exc}"[:500]
    finally:  # no run stays "running" forever
        await ctx.store.finish_run(
            run_id, ctx.clock.now(), report.counts, report.cost_usd, report.error
        )
    return report


def _gone(item_id: int, stage: str) -> None:
    log.warning("item %d no longer exists (%s); skipped", item_id, stage)


async def _run(ctx: PipelineContext, report: RunReport) -> None:
    rs = await load_runtime_settings(ctx.store)
    ctx.budget.runtime = rs
    await ctx.budget.refresh()
    now = ctx.clock.now()
    # Items a failed run left `new` go back through the pre-filter with this run's items.
    stranded = await ctx.store.stranded_new_items(now, hours=QUEUE_EXPIRY_HOURS)
    report.counts["recovered"] = len(stranded)
    new_items = await _collect(ctx, rs, now, report)
    queued_ids = await _prefilter(ctx, stranded + new_items, report)
    if queued_ids:
        try:
            await enrich_bodies(ctx.store, ctx.research_engine, queued_ids)
        except Exception:  # snippets are the fall-back
            log.exception("article enrichment failed; snippets will be used")
            report.counts["enrich_failed"] = 1
    report.counts["expired"] = await ctx.store.expire_old_queued(now, hours=QUEUE_EXPIRY_HOURS)
    await _review(ctx, rs, now, report)
    await _consolidate(ctx, now, report)
    await _write(ctx, rs, report)


async def _collect(
    ctx: PipelineContext, rs: RuntimeSettings, now: datetime, report: RunReport
) -> list[tuple[int, Candidate, SourceConfig]]:
    sources = await ctx.store.list_sources(enabled_only=True)
    since = now - collection_window(rs.runs_per_day)
    results = await asyncio.gather(
        *(
            _collect_one(ctx.sources.get(s.kind), with_runtime_defaults(s, rs), since)
            for s in sources
        )
    )
    counts = report.counts
    counts["sources"] = len(sources)
    new_items: list[tuple[int, Candidate, SourceConfig]] = []
    for src, result in zip(sources, results, strict=True):
        await ctx.store.record_source_result(result, now)
        if result.error:
            counts["sources_failed"] += 1
            log.warning("source %s failed: %s", src.name, result.error)
        for cand in result.candidates:
            counts["collected"] += 1
            fixed = cand.model_copy(update={"source_id": src.id})
            for item_id in await ctx.store.add_candidates([fixed], now):
                new_items.append((item_id, fixed, src))
    counts["new"] = len(new_items)
    return new_items


async def _prefilter(
    ctx: PipelineContext,
    new_items: list[tuple[int, Candidate, SourceConfig]],
    report: RunReport,
) -> list[int]:
    if not new_items:
        return []
    recent = await ctx.store.recent_titles(DEDUPE_WINDOW_HOURS)
    linked = {
        item_id
        for item_id, cand, _ in new_items
        if find_story_link(item_id, cand.title, recent) is not None
    }
    rows = [
        prefilter.PrefilterInput(
            item_id=item_id,
            source=src,
            title=cand.title,
            author=cand.author,
            snippet=cand.snippet,
            hn_points=cand.hn_points,
        )
        for item_id, cand, src in new_items
    ]
    others = prefilter.corroboration(rows, linked)
    blocklist = await prefilter.load_blocklist(ctx.store)
    keep: list[int] = []
    drop: list[int] = []
    for row in rows:
        decision = prefilter.decide(row, others.get(row.item_id, 0), blocklist)
        try:
            await ctx.store.set_item_fields(decision.item_id, heuristic_score=decision.score)
        except KeyError:  # deleted with its source mid-run
            _gone(decision.item_id, "prefilter")
            continue
        (keep if decision.keep else drop).append(decision.item_id)
    if keep:
        await ctx.store.mark_items(keep, ItemStatus.QUEUED)
    if drop:
        await ctx.store.mark_items(drop, ItemStatus.DROPPED)
    report.counts.update(near_duplicates=len(linked), queued=len(keep), dropped=len(drop))
    return keep


async def _judge_all(
    ctx: PipelineContext,
    inputs: list[ReviewInput],
    verdicts: dict[int, Verdict],
    failed: list[ReviewInput],
    report: RunReport,
) -> list[ReviewInput]:
    """Judge in batches; returns the inputs never sent because the budget ran out."""
    for start in range(0, len(inputs), REVIEW_BATCH):
        batch = inputs[start : start + REVIEW_BATCH]
        worst = _affordable(ctx.budget, ctx.judge.worst_case_cost, batch)
        if worst is None:
            log.warning("monthly budget reached: %d items stay queued", len(inputs) - start)
            return inputs[start:]
        try:
            result = await ctx.judge.review(batch)
        except asyncio.CancelledError:  # shutdown mid-call: it may still be billed
            report.cost_usd += await ctx.budget.record_abandoned(
                ctx.judge.provider, ctx.judge.model, Purpose.REVIEW, worst
            )
            raise
        except Exception:  # Judge.review should never raise; be safe anyway
            log.exception("judge raised; batch counted as failed")
            failed.extend(batch)
            continue
        for usage in result.usage:
            report.cost_usd += await ctx.budget.record(usage)
        ids = {i.item_id for i in batch}
        got = {v.item_id: v for v in result.verdicts if v.item_id in ids}
        verdicts.update(got)
        failed.extend(i for i in batch if i.item_id not in got)
    return []


async def judge_inputs(
    store: Store, queued: list[ReviewInput], now: datetime
) -> tuple[list[ReviewInput], dict[int, int], list[tuple[int, str, int | None]]]:
    """What the judge is sent for these items: each with its candidate stories. Also returns
    the dedupe links and recent titles used. The Compare page sends its sample through here
    too, so a comparison judges exactly what production would."""
    stories = await store.open_stories(now, hours=DEDUPE_WINDOW_HOURS)
    recent = await store.recent_titles(DEDUPE_WINDOW_HOURS)
    links = cluster.dedupe_links(queued, recent)
    return cluster.with_candidates(queued, stories, links), links, recent


async def _review(
    ctx: PipelineContext, rs: RuntimeSettings, now: datetime, report: RunReport
) -> None:
    limit = ctx.budget.remaining_reviews_today()
    if limit <= 0:
        return
    queued = await ctx.store.queued_review_inputs(limit, now)
    if not queued:
        return
    inputs, links, recent = await judge_inputs(ctx.store, queued, now)
    # A retried review_failed item is already shown in a story; it stays there.
    queued_ids = {q.item_id for q in queued}
    current = {iid: sid for iid, _, sid in recent if iid in queued_ids and sid is not None}

    verdicts: dict[int, Verdict] = {}
    failed: list[ReviewInput] = []
    unsent = await _judge_all(ctx, inputs, verdicts, failed, report)
    report.counts["budget_skipped"] = len(unsent)

    weights = {(s.kind, s.name): s.weight for s in await ctx.store.list_sources(enabled_only=False)}
    failed_ids = {f.item_id for f in failed}
    judged: list[cluster.Judged] = []
    for inp in inputs:
        verdict = verdicts.get(inp.item_id)
        if verdict is not None:
            signal = stored_signal(verdict, weights.get((inp.source_kind, inp.source_name), 1.0))
            try:
                await ctx.store.save_verdict(verdict, signal)
            except KeyError:  # deleted with its source mid-run; keep the other verdicts
                _gone(inp.item_id, "save_verdict")
                continue
            judged.append(cluster.Judged(inp, verdict, signal))
        elif inp.item_id in failed_ids:
            judged.append(cluster.Judged(inp, None, None))
    if failed_ids:  # retried on the next run by queued_review_inputs (R2)
        await ctx.store.mark_items(sorted(failed_ids), ItemStatus.REVIEW_FAILED)
    ctx.budget.note_reviewed(len(verdicts) + len(failed_ids))
    report.counts["reviewed"] = sum(1 for j in judged if j.verdict is not None)
    report.counts["review_failed"] = len(failed_ids)
    touched = await cluster.attach(ctx.store, judged, links, rs.show_threshold, now, current)
    report.counts["stories_touched"] = len(touched)


async def _consolidate(ctx: PipelineContext, now: datetime, report: RunReport) -> None:
    """Merge open stories that report the same event, so the writer sees each event once."""
    if ctx.consolidator is None:
        return
    result = ConsolidationResult()
    try:
        await consolidate(ctx.store, ctx.consolidator, ctx.budget, now, into=result)
    finally:  # a call cut off by shutdown counts in the run's cost too
        report.cost_usd += result.cost_usd
    report.counts["stories_merged"] = result.merged
    report.counts["consolidate_skipped"] = int(result.skipped)


async def _write(ctx: PipelineContext, rs: RuntimeSettings, report: RunReport) -> None:
    inputs = await ctx.store.stories_needing_text(WRITE_LIMIT, rs.why_it_matters)
    for start in range(0, len(inputs), WRITE_BATCH):
        batch = inputs[start : start + WRITE_BATCH]
        worst = _affordable(ctx.budget, ctx.writer.worst_case_cost_stories, batch)
        if worst is None:
            log.warning("monthly budget reached: %d stories left unsummarised", len(inputs) - start)
            return
        try:
            texts, usage = await ctx.writer.write_stories(batch)
        except asyncio.CancelledError:  # shutdown mid-call: it may still be billed
            report.cost_usd += await ctx.budget.record_abandoned(
                ctx.writer.provider, ctx.writer.model, Purpose.WRITE, worst
            )
            raise
        except Exception:  # Writer should never raise; be safe anyway
            log.exception("writer raised; batch counted as failed")
            texts, usage = [], []
        for u in usage:
            report.cost_usd += await ctx.budget.record(u)
        wanted = {w.story_id: w for w in batch}
        written: set[int] = set()
        for text in texts:
            inp = wanted.get(text.story_id)
            if inp is None or text.story_id in written:
                continue
            if not inp.include_why:
                text = text.model_copy(update={"why_it_matters": None})
            try:
                await ctx.store.save_story_text(text)
            except KeyError:
                log.warning("story %d no longer exists (write); skipped", text.story_id)
                continue
            written.add(text.story_id)
        missing = [w.story_id for w in batch if w.story_id not in written]
        if missing:  # offered again next run until MAX_TEXT_ATTEMPTS
            await ctx.store.mark_text_failed(missing)
        report.counts["written"] += len(written)
        report.counts["write_failed"] += len(missing)


async def run_briefing(ctx: PipelineContext, day: date) -> bool:
    rs = await load_runtime_settings(ctx.store)
    ctx.budget.runtime = rs
    await ctx.budget.refresh()
    now = ctx.clock.now()
    stories = await ctx.store.top_stories_for_briefing(now, limit=BRIEFING_STORY_LIMIT)
    if not stories:
        log.info("no stories for the %s briefing", day.isoformat())
        return False
    worst = _affordable(ctx.budget, ctx.writer.worst_case_cost_briefing, stories)
    if worst is None:
        log.warning("monthly budget reached: no briefing for %s", day.isoformat())
        return False
    try:
        body, usage = await ctx.writer.write_briefing(stories, briefing_date_label(day))
    except asyncio.CancelledError:  # shutdown mid-call: it may still be billed
        await ctx.budget.record_abandoned(
            ctx.writer.briefing_provider, ctx.writer.briefing_model, Purpose.BRIEFING, worst
        )
        raise
    except Exception:  # Writer should never raise; be safe anyway
        log.exception("briefing writer raised")
        return False
    for u in usage:
        await ctx.budget.record(u)
    if not body:
        return False
    model = usage[0].model if usage else ctx.writer.model
    await ctx.store.save_briefing(day, model, body, [s.story_id for s in stories], now)
    return True


class NarrationOutcome(StrEnum):
    DONE = "done"
    OFF = "off"
    NO_KEY = "no_key"
    NO_BRIEFING = "no_briefing"
    OVER_CAP = "over_cap"
    FAILED = "failed"
    BUSY = "busy"  # for callers that find the lock held; run_narration never returns it


async def run_narration(ctx: PipelineContext, day: date) -> NarrationOutcome:
    """Make (or remake) `day`'s briefing audio. The caller holds the scheduler lock.

    Billed characters (or maybe billed: a timeout, a 5xx, chunks already made) are always
    recorded; a clean refusal records nothing. A briefing rewritten while the audio was being
    made throws the new audio away (FAILED)."""
    rs = await load_runtime_settings(ctx.store)
    narrator, audio = ctx.narrator, ctx.audio
    if not rs.narration_enabled or narrator is None or audio is None:
        return NarrationOutcome.OFF
    if ctx.has_narration_key is not None and not await ctx.has_narration_key():
        return NarrationOutcome.NO_KEY
    briefing = await ctx.store.get_briefing(day)
    if briefing is None:
        return NarrationOutcome.NO_BRIEFING
    text = speech_text(briefing.body, briefing_date_label(day))
    catalogue = ctx.tts_catalogue
    factor = catalogue.factor(rs.narration_model) if catalogue is not None else 1.0
    estimate = math.ceil(len(text) * factor)
    used = await ctx.store.month_tts_characters(ctx.clock.now(), ctx.settings.tz)
    if used + estimate > rs.narration_monthly_chars:
        log.warning(
            "no audio for %s: %d of %d characters used this month, this one needs about %d"
            " (the monthly allowance)",
            day.isoformat(),
            used,
            rs.narration_monthly_chars,
            estimate,
        )
        return NarrationOutcome.OVER_CAP
    try:
        result = await narrator.narrate(
            chunks(text),
            voice_id=rs.narration_voice_id,
            model_id=rs.narration_model,
            credit_factor=factor,
        )
    except asyncio.CancelledError:  # shutdown mid-request: it may still be billed
        await ctx.store.record_tts_usage(
            ctx.clock.now(), day, rs.narration_model, estimate, ok=False
        )
        raise
    if result.characters_billed:
        await ctx.store.record_tts_usage(
            ctx.clock.now(),
            day,
            rs.narration_model,
            result.characters_billed,
            ok=result.audio is not None,
        )
    if result.audio is None:
        if result.error == NO_NARRATION_KEY:
            return NarrationOutcome.NO_KEY
        log.warning("audio for %s failed: %s", day.isoformat(), result.error)
        return NarrationOutcome.FAILED
    name = audio.write(day, result.audio)
    current = await ctx.store.get_briefing(day)
    if current is None or current.body != briefing.body:
        audio.delete(name)
        log.warning("the %s briefing changed while its audio was made; discarded", day)
        return NarrationOutcome.FAILED
    old = await ctx.store.save_briefing_audio(
        BriefingAudioRecord(
            date=day,
            voice_id=rs.narration_voice_id,
            voice_name=(
                catalogue.voice_name(rs.narration_voice_id)
                if catalogue is not None
                else fallback_voice_name(rs.narration_voice_id)
            ),
            model_id=rs.narration_model,
            characters=result.characters_billed,
            bytes=len(result.audio),
            file=name,
            created_at=ctx.clock.now(),
        )
    )
    if old is not None and old != name:
        audio.delete(old)
    log.info("audio for %s made: %d bytes", day.isoformat(), len(result.audio))
    return NarrationOutcome.DONE


async def prune_audio(ctx: PipelineContext) -> int:
    """Nightly upkeep (the caller holds the lock): delete audio rows and files for briefing
    days more than AUDIO_KEEP_DAYS before today, `briefing-*.mp3` files with no row, and temp
    files older than AUDIO_TEMP_MAX_AGE. Returns how many files went; no other file is touched."""
    audio = ctx.audio
    if audio is None:
        return 0
    now = ctx.clock.now()
    today = now.astimezone(ZoneInfo(ctx.settings.tz)).date()
    removed = 0
    for rec in await ctx.store.audio_older_than(today - timedelta(days=AUDIO_KEEP_DAYS)):
        name = await ctx.store.delete_briefing_audio(rec.date)
        if name is not None:
            audio.delete(name)
            removed += 1
    in_use = {rec.file for rec in await ctx.store.audio_older_than(date.max)}
    for name in audio.names() - in_use:
        audio.delete(name)
        removed += 1
    removed += audio.delete_temps_before((now - AUDIO_TEMP_MAX_AGE).timestamp())
    if removed:
        log.info("audio upkeep: %d old or orphaned files removed", removed)
    return removed
