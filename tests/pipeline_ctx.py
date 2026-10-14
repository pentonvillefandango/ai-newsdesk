"""Scripted fakes and a PipelineContext builder for pipeline and scheduler tests."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from ai_newsdesk.budget import Budget
from ai_newsdesk.config import Settings
from ai_newsdesk.models import (
    BriefingStory,
    Candidate,
    Category,
    ConsolidationStory,
    JudgeResult,
    Purpose,
    ReviewInput,
    SearchHit,
    SourceConfig,
    SourceKind,
    SourceResult,
    StoryText,
    Usage,
    Verdict,
    WriteInput,
)
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.pricing import Pricing
from ai_newsdesk.protocols import Clock, Consolidator, Source
from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.store.store import Store

PRICING_PATH = Path(__file__).resolve().parents[1] / "config" / "pricing.yaml"
MODEL = "claude-haiku-5-5"


def candidate(source_id: int, title: str, url: str) -> Candidate:
    return Candidate(source_id=source_id, url=url, title=title, snippet=f"{title}. Details follow.")


class ScriptedSource:
    def __init__(self, kind: SourceKind) -> None:
        self.kind = kind
        self.by_source: dict[int, list[Candidate] | Exception] = {}
        self.delays: dict[int, float] = {}
        self.seen: list[SourceConfig] = []

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult:
        self.seen.append(src)
        if src.id in self.delays:
            await asyncio.sleep(self.delays[src.id])
        value = self.by_source.get(src.id, [])
        if isinstance(value, Exception):
            raise value
        return SourceResult(source_id=src.id, candidates=value)


class NullResearchEngine:
    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]:
        return []

    async def fetch_markdown(self, url: str) -> str | None:
        return None

    async def health(self) -> bool:
        return True


class ScriptedJudge:
    """Verdicts by title substring; default signal 5. Costs: worst $0.001/item, actual less."""

    provider = "anthropic"
    model = MODEL

    def __init__(
        self,
        by_title: dict[str, tuple[int, bool, Category]] | None = None,
        fail_titles: frozenset[str] = frozenset(),
    ) -> None:
        self.by_title = by_title or {}
        self.fail_titles = fail_titles
        self.calls: list[list[ReviewInput]] = []

    def worst_case_cost(self, inputs: list[ReviewInput]) -> float:
        return 0.001 * len(inputs)

    async def review(self, inputs: list[ReviewInput]) -> JudgeResult:
        self.calls.append(list(inputs))
        verdicts: list[Verdict] = []
        failed: list[int] = []
        for inp in inputs:
            if any(t in inp.title for t in self.fail_titles):
                failed.append(inp.item_id)
                continue
            signal, junk, category = next(
                (v for key, v in self.by_title.items() if key in inp.title),
                (5, False, Category.OTHER),
            )
            verdicts.append(
                Verdict(item_id=inp.item_id, signal=signal, junk=junk, category=category)
            )
        usage = [
            Usage(
                provider="anthropic",
                model=MODEL,
                purpose=Purpose.REVIEW,
                input_tokens=1000 * len(inputs),
                output_tokens=100 * len(inputs),
            )
        ]
        return JudgeResult(verdicts=verdicts, failed_item_ids=failed, usage=usage)


class RecordingWriter:
    """Writes every story except those whose headline contains one of `skip_headlines`."""

    provider = briefing_provider = "anthropic"
    model = briefing_model = MODEL

    def __init__(self, skip_headlines: frozenset[str] = frozenset()) -> None:
        self.skip_headlines = skip_headlines
        self.story_calls: list[list[WriteInput]] = []
        self.briefing_calls: list[list[BriefingStory]] = []

    def worst_case_cost_stories(self, inputs: list[WriteInput]) -> float:
        return 0.001 * len(inputs)

    def worst_case_cost_briefing(self, stories: list[BriefingStory]) -> float:
        return 0.002

    async def write_stories(self, inputs: list[WriteInput]) -> tuple[list[StoryText], list[Usage]]:
        self.story_calls.append(list(inputs))
        texts = [
            StoryText(
                story_id=i.story_id,
                summary=f"Summary: {i.headline}",
                why_it_matters="It matters." if i.include_why else None,
            )
            for i in inputs
            if not any(s in i.headline for s in self.skip_headlines)
        ]
        return texts, []

    async def write_briefing(
        self, stories: list[BriefingStory], date_label: str
    ) -> tuple[str | None, list[Usage]]:
        self.briefing_calls.append(list(stories))
        body = "\n\n".join(f"## {s.category.value}\n\n[{s.headline}]({s.url})" for s in stories)
        return f"Briefing for {date_label}\n\n{body}", []


class ScriptedConsolidator:
    """Returns `groups` (or raises `error`); worst case `worst` USD, actual usage is small."""

    provider = "anthropic"
    model = MODEL

    def __init__(
        self,
        groups: list[list[int]],
        *,
        worst: float = 0.001,
        error: Exception | None = None,
    ) -> None:
        self.groups = groups
        self.worst = worst
        self.error = error
        self.calls: list[list[ConsolidationStory]] = []

    def worst_case_cost(self, stories: list[ConsolidationStory]) -> float:
        return self.worst

    async def find_groups(
        self, stories: list[ConsolidationStory]
    ) -> tuple[list[list[int]], list[Usage]]:
        self.calls.append(list(stories))
        if self.error is not None:
            raise self.error
        usage = Usage(
            provider="anthropic",
            model=MODEL,
            purpose=Purpose.REVIEW,
            input_tokens=500,
            output_tokens=50,
        )
        return [list(g) for g in self.groups], [usage]


def make_ctx(
    store: Store,
    clock: Clock,
    *,
    sources: dict[SourceKind, Source] | None = None,
    judge: ScriptedJudge | None = None,
    writer: RecordingWriter | None = None,
    consolidator: Consolidator | None = None,
) -> PipelineContext:
    return PipelineContext(
        store=store,
        sources=sources or {},
        research_engine=NullResearchEngine(),
        judge=judge or ScriptedJudge(),
        writer=writer or RecordingWriter(),
        budget=Budget(
            store, Pricing.load(PRICING_PATH), clock, RuntimeSettings(), tz="Europe/London"
        ),
        clock=clock,
        settings=cast(Settings, SimpleNamespace(tz="Europe/London")),
        consolidator=consolidator,
    )
