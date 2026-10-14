# src/ai_newsdesk/cluster.py
"""Story clustering: code shortlists candidate stories before judging; the judge's same-story
answer decides, with the dedupe link and in-pass near-duplicates as fall-backs."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ai_newsdesk.dedupe import find_story_link, title_tokens
from ai_newsdesk.models import Category, ReviewInput, SourceKind, StoryRef, Verdict
from ai_newsdesk.store.store import Store

log = logging.getLogger(__name__)

MAX_CANDIDATES = 5
MIN_SHARED_TOKENS = 2
EXTRA_SOURCE_BONUS = 0.5


def shortlist(title: str, stories: Sequence[StoryRef], k: int = MAX_CANDIDATES) -> list[StoryRef]:
    tokens = title_tokens(title)
    scored: list[tuple[int, StoryRef]] = []
    for story in stories:
        shared = len(tokens & title_tokens(story.headline))
        if shared >= MIN_SHARED_TOKENS:
            scored.append((shared, story))
    scored.sort(key=lambda pair: (-pair[0], -pair[1].id))
    return [story for _, story in scored[:k]]


def dedupe_links(
    inputs: Sequence[ReviewInput], recent: Sequence[tuple[int, str, int | None]]
) -> dict[int, int]:
    links: dict[int, int] = {}
    for inp in inputs:
        story_id = find_story_link(inp.item_id, inp.title, recent)
        if story_id is not None:
            links[inp.item_id] = story_id
    return links


def with_candidates(
    inputs: Sequence[ReviewInput], stories: Sequence[StoryRef], links: dict[int, int]
) -> list[ReviewInput]:
    by_id = {s.id: s for s in stories}
    out: list[ReviewInput] = []
    for inp in inputs:
        candidates = shortlist(inp.title, stories)
        linked = links.get(inp.item_id)
        if linked is not None and linked in by_id and all(c.id != linked for c in candidates):
            candidates = [by_id[linked], *candidates][:MAX_CANDIDATES]
        out.append(inp.model_copy(update={"story_candidates": candidates}))
    return out


def story_score(signals: Sequence[int | None], source_ids: Sequence[int]) -> float:
    known = [s for s in signals if s is not None]
    best = float(max(known)) if known else 0.0
    return best + EXTRA_SOURCE_BONUS * max(0, len(set(source_ids)) - 1)


@dataclass(frozen=True)
class Judged:
    input: ReviewInput
    verdict: Verdict | None  # None: review failed, never hidden
    signal: int | None  # stored signal (after junk and source weight)


async def attach(
    store: Store,
    judged: Sequence[Judged],
    links: dict[int, int],
    show_threshold: int,
    at: datetime,
    current: dict[int, int] | None = None,
) -> list[int]:
    """Attach shown items to stories; returns the touched story IDs in first-touch order.

    Items from X account sources are never attached: they stay unlinked (story_id NULL) and are
    shown on their own page, so Today is news stories only.

    `current` maps item IDs that already belong to a story (a `review_failed` item shown after an
    earlier run and now retried) to that story: such an item stays where it is and the story is
    rescored, so a retry never moves an item or leaves an empty story behind.
    """
    current = current or {}
    attached: list[tuple[int, str, int | None]] = []
    touched: list[int] = []
    for j in judged:
        if j.input.source_kind is SourceKind.X_PERSON:  # X posts never join stories
            continue
        item_id, title = j.input.item_id, j.input.title
        story_id: int | None = current.get(item_id)
        if story_id is None:
            if j.verdict is not None and (j.signal or 0) < show_threshold:
                continue
            offered = {s.id for s in j.input.story_candidates}
            if j.verdict is not None and j.verdict.same_story_id in offered:
                story_id = j.verdict.same_story_id
            if story_id is None:
                story_id = links.get(item_id)
            if story_id is None:
                story_id = find_story_link(item_id, title, attached)
            if story_id is None:
                category = j.verdict.category if j.verdict is not None else Category.OTHER
                story_id = await store.create_story(title, category, at)
            try:
                await store.attach_item(item_id, story_id, at)
            except KeyError:  # deleted with its source mid-run; the other items still attach
                log.warning("item %d no longer exists (attach); skipped", item_id)
                continue
        attached.append((item_id, title, story_id))
        if story_id not in touched:
            touched.append(story_id)
    for story_id in touched:
        await store.recompute_story_score(story_id)
    return touched
