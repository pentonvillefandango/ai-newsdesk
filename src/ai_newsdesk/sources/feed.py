"""RSS/Atom feeds (kind `feed`). GitHubReleasesSource reuses this with a different URL and title."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any, cast

import feedparser  # pyright: ignore[reportMissingTypeStubs]
import httpx

from ai_newsdesk.models import Candidate, SourceConfig, SourceKind
from ai_newsdesk.normalise import SNIPPET_MAX, TITLE_MAX, clean_text
from ai_newsdesk.sources.base import USER_AGENT, BaseSource, cfg_int, cfg_str, cutoff, keep_after


def entry_datetime(entry: Any) -> datetime | None:
    """feedparser gives UTC struct_times; published wins over updated."""
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    return None


def entry_text(entry: Any) -> str:
    text: str = entry.get("summary") or ""
    if not text:
        content: list[Any] = entry.get("content") or []
        if content:
            text = content[0].get("value") or ""
    return clean_text(text, SNIPPET_MAX)


def entry_author(entry: Any) -> str | None:
    return clean_text(entry.get("author") or "", 200) or None


def parse_feed(payload: bytes) -> list[Any]:
    parsed = cast(Any, feedparser.parse(payload))  # pyright: ignore[reportUnknownMemberType]
    entries: list[Any] = list(parsed.entries)
    if not entries and parsed.get("bozo"):
        raise ValueError(f"unparseable feed ({type(parsed.get('bozo_exception')).__name__})")
    return entries


class FeedSource(BaseSource):
    """Config: `url` (required), `max` (default 10), `lookback_hours` (default 24).

    ETag/Last-Modified validators are kept in memory per source id (V1). After a restart each feed
    is fetched in full once; the store's canonical-URL dedupe drops the repeats.
    """

    kind = SourceKind.FEED
    default_max = 10
    lookback_hours = 24

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._validators: dict[int, tuple[str | None, str | None]] = {}

    def feed_url(self, src: SourceConfig) -> str:
        return cfg_str(src, "url")

    def entry_title(self, src: SourceConfig, entry: Any) -> str:
        return clean_text(entry.get("title") or "", TITLE_MAX)

    async def fetch_candidates(self, src: SourceConfig, since: datetime) -> list[Candidate]:
        url = self.feed_url(src)
        limit = cfg_int(src, "max", self.default_max)
        cut = cutoff(src, since, self.lookback_hours)
        headers = {"User-Agent": USER_AGENT}
        etag, last_modified = self._validators.get(src.id, (None, None))
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        resp = await self._http.get(url, headers=headers, follow_redirects=True)
        if resp.status_code == 304:
            return []
        resp.raise_for_status()
        entries = parse_feed(resp.content)
        self._validators[src.id] = (resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
        out: list[Candidate] = []
        for entry in entries:
            link: str = entry.get("link") or ""
            title = self.entry_title(src, entry)
            published = entry_datetime(entry)
            if not link or not title or not keep_after(published, cut):
                continue
            out.append(
                Candidate(
                    source_id=src.id,
                    url=link,
                    title=title,
                    author=entry_author(entry),
                    published_at=published,
                    snippet=entry_text(entry),
                )
            )
            if len(out) >= limit:
                break
        return out
