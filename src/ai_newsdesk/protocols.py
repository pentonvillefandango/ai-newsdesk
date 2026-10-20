"""The seams around every external dependency (V1-S0). Only app.py wires concrete classes."""

from datetime import datetime
from typing import Protocol

from ai_newsdesk.models import (
    BriefingStory,
    ConsolidationStory,
    JudgeResult,
    NarrationResult,
    QuotaInfo,
    ReviewInput,
    SearchHit,
    SourceConfig,
    SourceKind,
    SourceResult,
    StoryText,
    Usage,
    VoiceInfo,
    WriteInput,
)


class Clock(Protocol):
    def now(self) -> datetime: ...  # timezone-aware UTC


class ResearchEngine(Protocol):
    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]: ...

    async def fetch_markdown(self, url: str) -> str | None: ...  # None on any failure

    async def health(self) -> bool: ...


class Source(Protocol):
    kind: SourceKind

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult: ...  # never raises


class Judge(Protocol):
    provider: str
    model: str

    def worst_case_cost(self, inputs: list[ReviewInput]) -> float: ...  # USD, budget pre-check

    # never raises; failures -> failed_item_ids
    async def review(self, inputs: list[ReviewInput]) -> JudgeResult: ...


class Writer(Protocol):
    provider: str  # the summary writer's
    model: str
    briefing_provider: str
    briefing_model: str

    def worst_case_cost_stories(self, inputs: list[WriteInput]) -> float: ...

    def worst_case_cost_briefing(self, stories: list[BriefingStory]) -> float: ...

    # never raises; missing stories simply absent
    async def write_stories(
        self, inputs: list[WriteInput]
    ) -> tuple[list[StoryText], list[Usage]]: ...

    async def write_briefing(
        self, stories: list[BriefingStory], date_label: str
    ) -> tuple[str | None, list[Usage]]: ...


class Consolidator(Protocol):
    provider: str
    model: str

    def worst_case_cost(self, stories: list[ConsolidationStory]) -> float: ...  # USD

    # never raises; groups of story ids that report the same event (unvalidated)
    async def find_groups(
        self, stories: list[ConsolidationStory]
    ) -> tuple[list[list[int]], list[Usage]]: ...


# The error a Narrator returns when no key resolves (no request is sent, nothing is billed).
NO_NARRATION_KEY = "No ElevenLabs key."


class Narrator(Protocol):
    # never raises
    async def narrate(
        self, text_chunks: list[str], *, voice_id: str, model_id: str, credit_factor: float
    ) -> NarrationResult: ...

    async def voices(self) -> list[VoiceInfo] | None: ...  # None on failure

    async def model_factors(self) -> dict[str, float] | None: ...  # TTS models only

    async def quota(self) -> QuotaInfo | None: ...
