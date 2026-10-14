"""One X account (kind `x_person`), via research-engine search. Snippets only: X pages are never
fetched."""

from __future__ import annotations

from datetime import UTC, datetime

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, clean_text, x_handle
from ai_newsdesk.protocols import Clock, ResearchEngine
from ai_newsdesk.sources.base import BaseSource, cfg_int, cfg_str, time_range_for


class XPersonSource(BaseSource):
    """Config: `handle` (required, with or without @), `max` (default 10).

    Query `site:x.com <handle>` (validated by the 2026-10-14 probe). Only status URLs whose handle
    equals the source handle (case-insensitive) are kept, so reposts and replies by other accounts
    are dropped. Titles such as `Name on X: "..."` are kept as they are.
    """

    kind = SourceKind.X_PERSON

    def __init__(self, research_engine: ResearchEngine, clock: Clock | None = None) -> None:
        self._re = research_engine
        self._clock = clock

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        handle = cfg_str(src, "handle").strip().lstrip("@")
        now = self._clock.now() if self._clock else datetime.now(UTC)
        hits = await self._re.search(
            f"site:x.com {handle}",
            max_results=cfg_int(src, "max", 10),
            time_range=time_range_for(since, now),
        )
        out: list[Candidate] = []
        for hit in hits:
            author = x_handle(hit.url)
            if author is None or author.lower() != handle.lower():
                continue
            snippet = clean_text(hit.snippet, SNIPPET_MAX)
            title = clean_text(hit.title, TITLE_MAX) or snippet[:TITLE_MAX]
            if not title:
                continue
            out.append(
                Candidate(
                    source_id=src.id, url=hit.url, title=title, author=handle, snippet=snippet
                )
            )
        return out
