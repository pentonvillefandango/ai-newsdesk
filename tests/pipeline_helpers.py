# tests/pipeline_helpers.py
"""Helpers for pipeline-level tests that use the real Store."""

from __future__ import annotations

from datetime import datetime, timedelta

from ai_newsdesk.models import Candidate, ReviewInput, SourceConfig, SourceKind, StoryRef, Trust
from ai_newsdesk.store.store import Store


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.t = now

    def now(self) -> datetime:
        return self.t

    def advance(self, **delta: float) -> None:
        self.t += timedelta(**delta)


async def add_source(
    store: Store,
    name: str,
    *,
    kind: SourceKind = SourceKind.FEED,
    trust: Trust = Trust.PRIMARY,
    weight: float = 1.0,
    config: dict[str, str | int | float | bool | list[str]] | None = None,
) -> SourceConfig:
    """Insert a source with step 1's keyword `upsert_source` and return it as stored."""
    cfg = config or {"url": f"https://example.com/{name.replace(' ', '-').lower()}.xml"}
    new_id = await store.upsert_source(kind, name, cfg, trust, weight=weight)
    src = await store.get_source(new_id)
    assert src is not None
    return src


async def add_item(store: Store, source_id: int, title: str, url: str, at: datetime) -> int:
    ids = await store.add_candidates(
        [Candidate(source_id=source_id, url=url, title=title, snippet=f"{title}. Details follow.")],
        at,
    )
    return ids[0]


def review_input(
    item_id: int,
    title: str,
    *,
    source_name: str = "Lab blog",
    kind: SourceKind = SourceKind.FEED,
    trust: Trust = Trust.PRIMARY,
    candidates: list[StoryRef] | None = None,
) -> ReviewInput:
    return ReviewInput(
        item_id=item_id,
        source_name=source_name,
        source_kind=kind,
        trust=trust,
        title=title,
        text=f"{title}. Details follow.",
        story_candidates=candidates or [],
    )
