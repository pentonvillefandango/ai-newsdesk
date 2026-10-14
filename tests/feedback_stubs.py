# tests/feedback_stubs.py
"""An in-memory stand-in for the Store methods that feedback.py and taste.py use."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ai_newsdesk.models import SourceConfig


class StubStore:
    def __init__(
        self,
        *,
        sources: list[SourceConfig] | None = None,
        feedback: dict[int, tuple[int, int]] | None = None,
        examples: list[tuple[dict[str, Any], int]] | None = None,
        features: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.sources = sources or []
        self.feedback = feedback or {}
        self.examples = examples or []
        self.features = features or {}
        self.settings: dict[str, Any] = {}
        self.weights: dict[int, float] = {}
        self.story_feedback: dict[int, int] = {}
        self.feedback_asked: list[tuple[int, datetime]] = []
        self.feature_calls = 0

    async def list_sources(self, enabled_only: bool = True) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled or not enabled_only]

    async def source_feedback(self, days: int, now: datetime) -> dict[int, tuple[int, int]]:
        self.feedback_asked.append((days, now))
        return self.feedback

    async def set_source_weight(self, source_id: int, weight: float) -> None:
        self.weights[source_id] = weight

    async def set_feedback(self, story_id: int, value: int) -> None:
        self.story_feedback[story_id] = value

    async def get_setting(self, key: str) -> Any:
        return self.settings.get(key)

    async def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value

    async def feedback_examples(self) -> list[tuple[dict[str, Any], int]]:
        return self.examples

    async def story_features(self, story_ids: list[int]) -> dict[int, dict[str, Any]]:
        self.feature_calls += 1
        return {i: self.features[i] for i in story_ids if i in self.features}
