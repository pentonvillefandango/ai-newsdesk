"""Fill thin item text with an article excerpt read through research-engine.

Spec section 3, "Full text".

The bake-off showed judging is much worse on headline-only items, so items whose snippet is shorter
than `thin_chars` get a Markdown excerpt. X items are never fetched (robots.txt), and a failed fetch
simply leaves the snippet in place.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from ai_newsdesk.models import SourceKind
from ai_newsdesk.normalise import excerpt_from_markdown, is_x_url
from ai_newsdesk.protocols import ResearchEngine


class EnrichStore(Protocol):
    async def items_for_enrichment(
        self, item_ids: list[int]
    ) -> list[tuple[int, str, SourceKind, str]]: ...
    async def set_item_fields(self, item_id: int, **fields: Any) -> None: ...


async def enrich_bodies(
    store: EnrichStore,
    research_engine: ResearchEngine,
    item_ids: list[int],
    *,
    thin_chars: int = 300,
) -> int:
    """Fetch excerpts for thin, non-X items. Returns how many items got a `body_excerpt`. Never
    raises for a single item's failure."""
    if not item_ids:
        return 0
    rows = await store.items_for_enrichment(item_ids)
    targets = [
        (item_id, url, snippet)
        for item_id, url, kind, snippet in rows
        if kind != SourceKind.X_PERSON and not is_x_url(url) and len(snippet) < thin_chars
    ]

    async def one(item_id: int, url: str, snippet: str) -> bool:
        markdown = await research_engine.fetch_markdown(url)
        if not markdown:
            return False
        excerpt = excerpt_from_markdown(markdown)
        if len(excerpt) <= len(snippet):
            return False
        await store.set_item_fields(item_id, body_excerpt=excerpt)
        return True

    results = await asyncio.gather(*(one(*target) for target in targets), return_exceptions=True)
    return sum(1 for result in results if result is True)
