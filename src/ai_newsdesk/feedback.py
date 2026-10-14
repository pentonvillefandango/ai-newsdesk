# src/ai_newsdesk/feedback.py
"""Thumbs feedback: per-source weights (fed to the pre-filter) and blocklist keywords.

weight = clamp(1 + 0.1 x (ups - downs over 30 days), 0.3, 2.0), written to the sources table on
every thumb and nightly (taste.nightly), so old thumbs age out.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ai_newsdesk.protocols import Clock
from ai_newsdesk.store.store import Store

log = logging.getLogger(__name__)

FEEDBACK_DAYS = 30
WEIGHT_STEP = 0.1
WEIGHT_MIN = 0.3
WEIGHT_MAX = 2.0
BLOCKLIST_KEY = "blocklist"
MAX_KEYWORD_CHARS = 60
VALID_FEEDBACK = (-1, 0, 1)


def weight_for(ups: int, downs: int) -> float:
    raw = 1.0 + WEIGHT_STEP * (ups - downs)
    return round(min(WEIGHT_MAX, max(WEIGHT_MIN, raw)), 3)


async def update_source_weights(
    store: Store, now: datetime, *, days: int = FEEDBACK_DAYS
) -> dict[int, float]:
    counts = await store.source_feedback(days, now)
    changed: dict[int, float] = {}
    for src in await store.list_sources(enabled_only=False):
        ups, downs = counts.get(src.id, (0, 0))
        weight = weight_for(ups, downs)
        if abs(weight - src.weight) > 1e-9:
            await store.set_source_weight(src.id, weight)
            changed[src.id] = weight
    if changed:
        log.info("source weights updated: %s", changed)
    return changed


async def set_feedback(store: Store, clock: Clock, story_id: int, value: int) -> dict[int, float]:
    if value not in VALID_FEEDBACK:
        raise ValueError("feedback must be -1, 0 or 1")
    await store.set_feedback(story_id, value)
    return await update_source_weights(store, clock.now())


async def add_blocklist_keyword(store: Store, keyword: str) -> list[str]:
    kw = " ".join(keyword.lower().split())
    if not kw or len(kw) > MAX_KEYWORD_CHARS:
        raise ValueError(f"a blocklist keyword must be 1-{MAX_KEYWORD_CHARS} characters")
    current = await store.get_setting(BLOCKLIST_KEY)
    words = [str(w) for w in current] if isinstance(current, list) else []
    if kw not in words:
        words.append(kw)
        await store.set_setting(BLOCKLIST_KEY, words)
    return words
