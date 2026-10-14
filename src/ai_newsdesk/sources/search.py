"""research-engine searches (kind `search`): topic queries and domain-scoped (`site:`) queries."""

from __future__ import annotations

from datetime import UTC, datetime

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, canonical_url, clean_text, x_handle
from ai_newsdesk.protocols import Clock, ResearchEngine
from ai_newsdesk.sources.base import BaseSource, SourceConfigError, cfg_int, cfg_str, time_range_for


def build_query(src: SourceConfig) -> str:
    query = cfg_str(src, "query", "").strip()
    site = cfg_str(src, "site", "").strip().strip("/")
    built = f"site:{site} {query}".strip() if site else query
    if not built:
        raise SourceConfigError(f"source {src.id} ({src.name}): config needs 'query' or 'site'")
    return built


def matches_site(url: str, site: str) -> bool:
    """True when the URL is on `site` (host plus optional path prefix, `www.` ignored)."""
    rest = canonical_url(url).split("://", 1)[-1].lower()
    prefix = site.strip().strip("/").lower().removeprefix("www.")
    return rest == prefix or rest.startswith((prefix + "/", prefix + "?"))


class SearchSource(BaseSource):
    """Config: `query` and/or `site` (at least one), `max` (default 10).

    With `site`, results off that site are dropped: not every engine honours `site:`, and a
    site-scoped source may carry `primary` trust. X posts found by a topic search keep their handle
    as author.
    """

    kind = SourceKind.SEARCH

    def __init__(self, research_engine: ResearchEngine, clock: Clock | None = None) -> None:
        self._re = research_engine
        self._clock = clock

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        query = build_query(src)
        site = cfg_str(src, "site", "")
        now = self._clock.now() if self._clock else datetime.now(UTC)
        hits = await self._re.search(
            query, max_results=cfg_int(src, "max", 10), time_range=time_range_for(since, now)
        )
        out: list[Candidate] = []
        for hit in hits:
            if site and not matches_site(hit.url, site):
                continue
            snippet = clean_text(hit.snippet, SNIPPET_MAX)
            title = clean_text(hit.title, TITLE_MAX) or snippet[:TITLE_MAX]
            if not title:
                continue
            out.append(
                Candidate(
                    source_id=src.id,
                    url=hit.url,
                    title=title,
                    author=x_handle(hit.url),
                    snippet=snippet,
                )
            )
        return out
