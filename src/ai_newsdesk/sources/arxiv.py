"""New arXiv papers in AI categories via the export API (kind `arxiv`, trust `discovery`).

Volume is high, so every paper is a candidate here and the step-3 pre-filter drops most of them
(authors or affiliations linked to trusted sources, plus HN/X mentions).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, clean_text
from ai_newsdesk.sources.base import USER_AGENT, BaseSource, cfg_int, cfg_list, cutoff, keep_after
from ai_newsdesk.sources.feed import entry_datetime, parse_feed

ARXIV_API = "https://export.arxiv.org/api/query"
DEFAULT_CATEGORIES = ["cs.CL", "cs.AI", "cs.LG"]


def format_authors(names: list[str]) -> str | None:
    if not names:
        return None
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + " et al."


def query_url(src: SourceConfig) -> str:
    categories = cfg_list(src, "categories", DEFAULT_CATEGORIES)
    search = "+OR+".join(f"cat:{category}" for category in categories)
    count = cfg_int(src, "max", 50)
    return (
        f"{ARXIV_API}?search_query={search}"
        f"&sortBy=submittedDate&sortOrder=descending&max_results={count}"
    )


class ArxivSource(BaseSource):
    """Config: `categories` (default cs.CL, cs.AI, cs.LG), `max` (default 50), `lookback_hours`
    (default 48; papers reach the API a day or more after submission)."""

    kind = SourceKind.ARXIV
    lookback_hours = 48

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        resp = await self._http.get(
            query_url(src), headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        resp.raise_for_status()
        cut = cutoff(src, since, self.lookback_hours)
        out: list[Candidate] = []
        for entry in parse_feed(resp.content):
            link: str = entry.get("link") or ""
            title = clean_text(entry.get("title") or "", TITLE_MAX)
            published = entry_datetime(entry)
            if not link or not title or not keep_after(published, cut):
                continue
            authors: list[Any] = entry.get("authors") or []
            names = [clean_text(a.get("name") or "") for a in authors]
            out.append(
                Candidate(
                    source_id=src.id,
                    url=link,
                    title=title,
                    author=format_authors([n for n in names if n]),
                    published_at=published,
                    snippet=clean_text(entry.get("summary") or "", SNIPPET_MAX),
                )
            )
        return out
