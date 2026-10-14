"""Minimal protocol fakes so the web tests never touch the network or spend money."""

from __future__ import annotations

from ai_newsdesk.app import AppDeps
from ai_newsdesk.budget import Budget
from ai_newsdesk.models import (
    BriefingStory,
    JudgeResult,
    ReviewInput,
    SearchHit,
    StoryText,
    Usage,
    WriteInput,
)
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.runtime_settings import load_runtime_settings


class FakeResearchEngine:
    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]:
        return []

    async def fetch_markdown(self, url: str) -> str | None:
        return None

    async def health(self) -> bool:
        return True


class FakeJudge:
    provider = "anthropic"
    model = "claude-haiku-5-5"

    def worst_case_cost(self, inputs: list[ReviewInput]) -> float:
        return 0.0

    async def review(self, inputs: list[ReviewInput]) -> JudgeResult:
        return JudgeResult(failed_item_ids=[i.item_id for i in inputs])


class FakeWriter:
    provider = briefing_provider = "anthropic"
    model = briefing_model = "claude-haiku-5-5"

    def worst_case_cost_stories(self, inputs: list[WriteInput]) -> float:
        return 0.0

    def worst_case_cost_briefing(self, stories: list[BriefingStory]) -> float:
        return 0.0

    async def write_stories(self, inputs: list[WriteInput]) -> tuple[list[StoryText], list[Usage]]:
        return [], []

    async def write_briefing(
        self, stories: list[BriefingStory], date_label: str
    ) -> tuple[str | None, list[Usage]]:
        return None, []


async def fake_ctx_factory(deps: AppDeps) -> PipelineContext:
    """Same shape as `app.build_context`, with fakes for everything that would leave the process."""
    runtime = await load_runtime_settings(deps.store)
    return PipelineContext(
        store=deps.store,
        sources={},
        research_engine=FakeResearchEngine(),
        judge=FakeJudge(),
        writer=FakeWriter(),
        budget=Budget(deps.store, deps.pricing, deps.clock, runtime, tz=deps.settings.tz),
        clock=deps.clock,
        settings=deps.settings,
    )
