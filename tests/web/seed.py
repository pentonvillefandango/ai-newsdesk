"""Seed data through the public Store API (shared by web tests, store tests and the dev seed)."""

from __future__ import annotations

from datetime import datetime

from ai_newsdesk.models import (
    Candidate,
    Category,
    ItemStatus,
    SourceKind,
    StoryText,
    Trust,
    Verdict,
)
from ai_newsdesk.store import Store

ConfigValue = str | int | float | bool | list[str]


async def add_source(
    store: Store,
    *,
    name: str,
    kind: SourceKind = SourceKind.FEED,
    trust: Trust = Trust.PRIMARY,
    config: dict[str, ConfigValue] | None = None,
    enabled: bool = True,
) -> int:
    slug = "-".join(name.lower().split())
    cfg: dict[str, ConfigValue] = (
        config if config is not None else {"url": f"https://{slug}.example/feed.xml"}
    )
    return await store.upsert_source(kind=kind, name=name, config=cfg, trust=trust, enabled=enabled)


async def add_item(
    store: Store,
    *,
    source_id: int,
    url: str,
    title: str,
    at: datetime,
    status: ItemStatus | None = None,
) -> int:
    ids = await store.add_candidates(
        [Candidate(source_id=source_id, url=url, title=title, published_at=at)], at
    )
    assert len(ids) == 1, f"{url} was already stored"
    if status is not None:
        await store.mark_items(ids, status)
    return ids[0]


async def add_story(
    store: Store,
    *,
    at: datetime,
    headline: str,
    sources: list[tuple[int, str, str]],
    category: Category = Category.RESEARCH,
    signal: int = 8,
    summary: str | None = "A short summary.",
    why: str | None = "It matters because it changes the defaults.",
) -> int:
    """A reviewed story with one item per (source_id, url, title)."""
    story_id = await store.create_story(headline, category, at)
    for source_id, url, title in sources:
        item_id = await add_item(store, source_id=source_id, url=url, title=title, at=at)
        await store.save_verdict(
            Verdict(item_id=item_id, signal=signal, junk=False, category=category), float(signal)
        )
        await store.attach_item(item_id, story_id, at)
    await store.recompute_story_score(story_id)
    if summary is not None:
        await store.save_story_text(
            StoryText(story_id=story_id, summary=summary, why_it_matters=why)
        )
    return story_id
