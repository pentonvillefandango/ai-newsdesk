"""Hacker News stories above a points threshold, via the Algolia API (kind `hn`)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, clean_text
from ai_newsdesk.sources.base import USER_AGENT, BaseSource, cfg_int, cfg_list, cutoff

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={}"
DEFAULT_MIN_POINTS = (
    100  # RuntimeSettings.hn_min_points default; the pipeline injects the live value
)


class _Hit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    object_id: str = Field(alias="objectID")
    title: str | None = None
    url: str | None = None
    author: str | None = None
    points: int | None = None
    created_at_i: int | None = None
    story_text: str | None = None


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hits: list[_Hit] = Field(default_factory=list[_Hit])


class HNSource(BaseSource):
    """Config: `queries` (default ["AI"]), `min_points` (default 100), `max` hits per query (default
    30), `lookback_hours` (default 24). Stories are found by creation time, so the look-back lets a
    story that crossed the threshold after the last run still be caught.
    """

    kind = SourceKind.HN
    lookback_hours = 24

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        queries = cfg_list(src, "queries", ["AI"])
        min_points = cfg_int(src, "min_points", DEFAULT_MIN_POINTS)
        per_query = cfg_int(src, "max", 30)
        created_after = int(cutoff(src, since, self.lookback_hours).timestamp())
        seen: set[str] = set()
        out: list[Candidate] = []
        for query in queries:
            resp = await self._http.get(
                HN_SEARCH_URL,
                params={
                    "query": query,
                    "tags": "story",
                    "numericFilters": f"points>={min_points},created_at_i>{created_after}",
                    "hitsPerPage": per_query,
                },
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            for hit in _Response.model_validate(resp.json()).hits:
                title = clean_text(hit.title, TITLE_MAX)
                if hit.object_id in seen or not title:
                    continue
                seen.add(hit.object_id)
                out.append(
                    Candidate(
                        source_id=src.id,
                        url=hit.url or HN_ITEM_URL.format(hit.object_id),
                        title=title,
                        author=hit.author,
                        published_at=datetime.fromtimestamp(hit.created_at_i, tz=UTC)
                        if hit.created_at_i
                        else None,
                        snippet=clean_text(hit.story_text, SNIPPET_MAX),
                        hn_points=hit.points,
                    )
                )
        return out
