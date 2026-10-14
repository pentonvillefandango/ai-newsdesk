"""Every SQL query in the app lives here (V1-S1).

One connection, serialised by an asyncio.Lock: each public method holds the lock for its
whole transaction, so concurrent coroutines never interleave statements. Timestamps are
stored as UTC ISO-8601 text with microseconds (`_ts`), so text order is time order.
"""

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiosqlite
import yaml
from pydantic import BaseModel

from ai_newsdesk.clock import SystemClock
from ai_newsdesk.models import (
    BriefingStory,
    Candidate,
    Category,
    ItemStatus,
    ReviewInput,
    SourceConfig,
    SourceKind,
    SourceResult,
    Story,
    StoryRef,
    StorySource,
    StoryText,
    Trust,
    Usage,
    Verdict,
    WriteInput,
)
from ai_newsdesk.protocols import Clock
from ai_newsdesk.store.db import apply_migrations, connect
from ai_newsdesk.store.records import BriefingRecord, RunRecord, SourceStats

NO_TEXT = "(no text, title only)"
STORY_SOURCE_BONUS = 0.5  # added to a story's score per extra independent source
MAX_REVIEW_ATTEMPTS = 2  # a review_failed item gets one more try, then heuristic only
MAX_TEXT_ATTEMPTS = 2  # a story whose text failed gets one more try
MIN_WEIGHT, MAX_WEIGHT = 0.3, 2.0
ITEM_FIELDS = frozenset(
    {
        "title",
        "author",
        "snippet",
        "body_excerpt",
        "hn_points",
        "heuristic_score",
        "status",
        "signal",
        "category",
        "judge_json",
        "story_id",
    }
)

ConfigValue = str | int | float | bool | list[str]


def basic_canonical_url(url: str) -> str:
    """Lower-case scheme and host, drop `www.`, default ports, fragment and trailing slash.

    The default `canonicalise` for `Store`; app.py may pass a stricter one (for example one
    that also strips tracking parameters). It must be idempotent.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().removeprefix("www.")
    port = parts.port
    default_port = (scheme, port) in {("http", 80), ("https", 443)}
    netloc = host if port is None or default_port else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, ""))


def _ts(at: dt.datetime) -> str:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Store timestamps must be timezone-aware")
    return at.astimezone(dt.UTC).isoformat(timespec="microseconds")


def _dt(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value is not None else None


def _req_dt(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _zone(tz: str | dt.tzinfo) -> dt.tzinfo:
    return ZoneInfo(tz) if isinstance(tz, str) else tz


def _local_day_bounds(now: dt.datetime, tz: str | dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    zone = _zone(tz)
    day = now.astimezone(zone).date()
    start = dt.datetime(day.year, day.month, day.day, tzinfo=zone)
    nxt = day + dt.timedelta(days=1)
    end = dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return start, end


def _local_month_bounds(now: dt.datetime, tz: str | dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    zone = _zone(tz)
    local = now.astimezone(zone)
    start = dt.datetime(local.year, local.month, 1, tzinfo=zone)
    if local.month == 12:
        end = dt.datetime(local.year + 1, 1, 1, tzinfo=zone)
    else:
        end = dt.datetime(local.year, local.month + 1, 1, tzinfo=zone)
    return start, end


def _sql_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dt.datetime):
        return _ts(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, dict | list):
        return json.dumps(value)
    return value


def _new_id(lastrowid: int | None) -> int:
    if lastrowid is None:
        raise RuntimeError("INSERT returned no row id")
    return lastrowid


def _review_text(body_excerpt: str | None, snippet: str | None) -> str:
    return (body_excerpt or "").strip() or (snippet or "").strip() or NO_TEXT


def _check_weight(weight: float) -> None:
    if not MIN_WEIGHT <= weight <= MAX_WEIGHT:
        raise ValueError(f"weight must be between {MIN_WEIGHT} and {MAX_WEIGHT}")


# The lead item of each selected story (best stored signal, then lowest id: the rule
# recompute_story_score uses), with what the taste model needs. Parameters: (1, "[]") selects
# every rated story; (0, json list of ids) selects those stories. Newest thumb first.
_LEAD_ITEMS = (
    "SELECT * FROM ("
    " SELECT i.story_id, i.judge_json, st.category AS story_category, st.feedback,"
    " st.feedback_at, s.trust, s.kind,"
    " ROW_NUMBER() OVER (PARTITION BY i.story_id"
    "  ORDER BY COALESCE(i.signal, -1) DESC, i.id ASC) AS rn"
    " FROM items i JOIN stories st ON st.id = i.story_id"
    " JOIN sources s ON s.id = i.source_id"
    " WHERE (? AND st.feedback != 0) OR i.story_id IN (SELECT value FROM json_each(?))"
    ") WHERE rn = 1 ORDER BY feedback_at DESC, story_id DESC"
)


def _lead_features(row: aiosqlite.Row) -> dict[str, Any]:
    """Taste-model features of a lead item; an unjudged item has no signal."""
    if row["judge_json"] is not None:
        verdict = Verdict.model_validate_json(row["judge_json"])
        signal, junk, category = verdict.signal, verdict.junk, verdict.category.value
    else:
        signal, junk, category = None, False, row["story_category"]
    return {
        "signal": signal,
        "junk": junk,
        "category": category,
        "trust": row["trust"],
        "source_kind": row["kind"],
    }


class _SeedSource(BaseModel):
    kind: SourceKind
    name: str
    trust: Trust
    config: dict[str, ConfigValue] = {}
    enabled: bool = True
    weight: float = 1.0


class Store:
    def __init__(
        self,
        db_path: str,
        *,
        clock: Clock | None = None,
        canonicalise: Callable[[str], str] = basic_canonical_url,
    ) -> None:
        self.db_path = db_path
        self.clock: Clock = clock or SystemClock()
        self.canonicalise = canonicalise
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ---- lifecycle -------------------------------------------------------------------

    async def init(self) -> None:
        """Open the database and apply pending migrations. Safe to call twice."""
        if self._conn is not None:
            return
        self._conn = await connect(self.db_path)
        await apply_migrations(self._conn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Store.init() has not been called")
        return self._conn

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """One write transaction, holding the lock; rolled back on any error."""
        async with self._lock:
            conn = self._db
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            await conn.execute("COMMIT")

    async def _all(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock, self._db.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def _one(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self._lock, self._db.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    # ---- sources ---------------------------------------------------------------------

    async def seed_sources_if_empty(self, path: str | Path) -> int:
        """Load `config/sources.yaml` when the sources table is empty; return rows added."""
        raw = yaml.safe_load(await asyncio.to_thread(Path(path).read_text)) or {}
        seeds = [_SeedSource.model_validate(entry) for entry in raw.get("sources", [])]
        now = _ts(self.clock.now())
        async with self._tx() as conn:
            async with conn.execute("SELECT count(*) FROM sources") as cur:
                row = await cur.fetchone()
            if row is not None and row[0] > 0:
                return 0
            for seed in seeds:
                await conn.execute(
                    "INSERT INTO sources (kind, name, config_json, trust, enabled, weight,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        seed.kind.value,
                        seed.name,
                        json.dumps(seed.config),
                        seed.trust.value,
                        int(seed.enabled),
                        seed.weight,
                        now,
                    ),
                )
        return len(seeds)

    @staticmethod
    def _source(row: aiosqlite.Row) -> SourceConfig:
        return SourceConfig(
            id=row["id"],
            kind=SourceKind(row["kind"]),
            name=row["name"],
            config=json.loads(row["config_json"]),
            trust=Trust(row["trust"]),
            enabled=bool(row["enabled"]),
            weight=row["weight"],
        )

    async def list_sources(self, enabled_only: bool = True) -> list[SourceConfig]:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = await self._all(f"SELECT * FROM sources {where} ORDER BY kind, name, id")  # noqa: S608
        return [self._source(r) for r in rows]

    async def get_source(self, source_id: int) -> SourceConfig | None:
        row = await self._one("SELECT * FROM sources WHERE id = ?", (source_id,))
        return self._source(row) if row is not None else None

    async def upsert_source(
        self,
        kind: SourceKind,
        name: str,
        config: dict[str, ConfigValue],
        trust: Trust,
        *,
        enabled: bool = True,
        weight: float | None = None,
        source_id: int | None = None,
    ) -> int:
        """Update source `source_id`, or insert/update by (kind, name). Returns the id.

        `weight=None` inserts 1.0 and keeps the stored weight on update, so editing a source
        never resets the weight that feedback learned.
        """
        if weight is not None:
            _check_weight(weight)
        values = (kind.value, name, json.dumps(config), trust.value, int(enabled))
        async with self._tx() as conn:
            if source_id is not None:
                cur = await conn.execute(
                    "UPDATE sources SET kind = ?, name = ?, config_json = ?, trust = ?,"
                    " enabled = ?, weight = COALESCE(?, weight) WHERE id = ?",
                    (*values, weight, source_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"no source {source_id}")
                return source_id
            async with conn.execute(
                "INSERT INTO sources (kind, name, config_json, trust, enabled, weight, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (kind, name) DO UPDATE SET config_json = excluded.config_json,"
                " trust = excluded.trust, enabled = excluded.enabled,"
                " weight = COALESCE(?, sources.weight)"
                " RETURNING id",
                (*values, 1.0 if weight is None else weight, _ts(self.clock.now()), weight),
            ) as cur:
                row = await cur.fetchone()
            if row is None:  # RETURNING always yields the row
                raise RuntimeError("upsert returned no id")
            return int(row[0])

    async def delete_source(self, source_id: int) -> bool:
        """Delete a source and (cascade) its items. True if it existed."""
        async with self._tx() as conn:
            cur = await conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            return cur.rowcount > 0

    async def set_source_weight(self, source_id: int, weight: float) -> None:
        """Set a source's pre-filter weight (feedback.py). KeyError if no such source."""
        _check_weight(weight)
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE sources SET weight = ? WHERE id = ?", (weight, source_id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"no source {source_id}")

    async def record_source_result(self, result: SourceResult, at: dt.datetime) -> None:
        when = _ts(at)
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE sources SET last_run_at = ?, last_count = ?, last_duration_s = ?,"
                " last_error = ?, last_ok_at = CASE WHEN ? IS NULL THEN ? ELSE last_ok_at END"
                " WHERE id = ?",
                (
                    when,
                    len(result.candidates),
                    result.duration_s,
                    result.error,
                    result.error,
                    when,
                    result.source_id,
                ),
            )

    async def source_stats(self) -> list[SourceStats]:
        rows = await self._all(
            "SELECT s.*, (SELECT count(*) FROM items i WHERE i.source_id = s.id) AS item_count"
            " FROM sources s ORDER BY s.kind, s.name, s.id"
        )
        return [
            SourceStats(
                id=r["id"],
                kind=SourceKind(r["kind"]),
                name=r["name"],
                trust=Trust(r["trust"]),
                enabled=bool(r["enabled"]),
                weight=r["weight"],
                last_run_at=_dt(r["last_run_at"]),
                last_ok_at=_dt(r["last_ok_at"]),
                last_error=r["last_error"],
                last_count=r["last_count"],
                last_duration_s=r["last_duration_s"],
                item_count=r["item_count"],
            )
            for r in rows
        ]

    # ---- items -----------------------------------------------------------------------

    async def add_candidates(self, candidates: list[Candidate], at: dt.datetime) -> list[int]:
        """Insert new items (status `new`); skip known canonical URLs and unknown sources."""
        fetched_at = _ts(at)
        new_ids: list[int] = []
        async with self._tx() as conn:
            async with conn.execute("SELECT id FROM sources") as cur:
                known = {row[0] for row in await cur.fetchall()}
            for c in candidates:
                if c.source_id not in known:
                    continue
                cur = await conn.execute(
                    "INSERT INTO items (source_id, url, canonical_url, title, author,"
                    " published_at, fetched_at, snippet, hn_points)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (canonical_url) DO NOTHING",
                    (
                        c.source_id,
                        c.url,
                        self.canonicalise(c.url),
                        c.title,
                        c.author,
                        _ts(c.published_at) if c.published_at else None,
                        fetched_at,
                        c.snippet,
                        c.hn_points,
                    ),
                )
                if cur.rowcount == 1 and cur.lastrowid is not None:
                    new_ids.append(cur.lastrowid)
        return new_ids

    async def recent_titles(self, hours: int) -> list[tuple[int, str, int | None]]:
        """(item_id, title, story_id) for items fetched in the last `hours` (store clock)."""
        cutoff = _ts(self.clock.now() - dt.timedelta(hours=hours))
        rows = await self._all(
            "SELECT id, title, story_id FROM items WHERE fetched_at >= ? ORDER BY id", (cutoff,)
        )
        return [(r["id"], r["title"], r["story_id"]) for r in rows]

    async def items_for_enrichment(
        self, item_ids: list[int]
    ) -> list[tuple[int, str, SourceKind, str]]:
        """(item_id, url, source kind, snippet) for the ids that exist, in id order."""
        if not item_ids:
            return []
        rows = await self._all(
            "SELECT i.id, i.url, s.kind, i.snippet FROM items i"
            " JOIN sources s ON s.id = i.source_id"
            " WHERE i.id IN (SELECT value FROM json_each(?)) ORDER BY i.id",
            (json.dumps(item_ids),),
        )
        return [(r["id"], r["url"], SourceKind(r["kind"]), r["snippet"]) for r in rows]

    async def set_item_fields(self, item_id: int, **fields: Any) -> None:
        """Update whitelisted item columns (see ITEM_FIELDS). KeyError if no such item."""
        if not fields:
            raise ValueError("set_item_fields needs at least one field")
        unknown = set(fields) - ITEM_FIELDS
        if unknown:
            raise ValueError(f"not settable item fields: {sorted(unknown)}")
        names = sorted(fields)
        assignments = ", ".join(f"{name} = ?" for name in names)  # names are whitelisted
        params = [_sql_value(fields[name]) for name in names]
        async with self._tx() as conn:
            cur = await conn.execute(
                f"UPDATE items SET {assignments} WHERE id = ?",  # noqa: S608
                (*params, item_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no item {item_id}")

    async def queued_review_inputs(self, limit: int, now: dt.datetime) -> list[ReviewInput]:
        """Items waiting for review, best heuristic score first, then oldest first.

        Includes `review_failed` items with attempts left; skips anything older than 48 h.
        `story_candidates` is left empty: cluster.py fills it.
        """
        cutoff = _ts(now - dt.timedelta(hours=48))
        rows = await self._all(
            "SELECT i.id, i.title, i.author, i.body_excerpt, i.snippet,"
            " s.name AS source_name, s.kind AS source_kind, s.trust AS trust"
            " FROM items i JOIN sources s ON s.id = i.source_id"
            " WHERE (i.status = 'queued'"
            "        OR (i.status = 'review_failed' AND i.review_attempts < ?))"
            "   AND i.fetched_at >= ?"
            " ORDER BY i.heuristic_score DESC, i.fetched_at ASC, i.id ASC LIMIT ?",
            (MAX_REVIEW_ATTEMPTS, cutoff, limit),
        )
        return [
            ReviewInput(
                item_id=r["id"],
                source_name=r["source_name"],
                source_kind=SourceKind(r["source_kind"]),
                trust=Trust(r["trust"]),
                title=r["title"],
                author=r["author"],
                text=_review_text(r["body_excerpt"], r["snippet"]),
            )
            for r in rows
        ]

    async def expire_old_queued(self, now: dt.datetime, hours: int = 48) -> int:
        cutoff = _ts(now - dt.timedelta(hours=hours))
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE items SET status = 'expired' WHERE status = 'queued' AND fetched_at < ?",
                (cutoff,),
            )
            return cur.rowcount

    async def save_verdict(self, verdict: Verdict, signal_final: float) -> None:
        """Store the judge's verdict and the final (junk-zeroed, weighted) signal."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE items SET status = 'reviewed', judge_json = ?, signal = ?, category = ?,"
                " last_review_at = ? WHERE id = ?",
                (
                    verdict.model_dump_json(),
                    signal_final,
                    verdict.category.value,
                    _ts(self.clock.now()),
                    verdict.item_id,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no item {verdict.item_id}")

    async def mark_items(self, ids: list[int], status: ItemStatus) -> int:
        """Set `status` on items. REVIEW_FAILED also counts an attempt. Returns rows changed."""
        if not ids:
            return 0
        async with self._tx() as conn:
            if status is ItemStatus.REVIEW_FAILED:
                now = _ts(self.clock.now())
                cur = await conn.executemany(
                    "UPDATE items SET status = ?, review_attempts = review_attempts + 1,"
                    " last_review_at = ? WHERE id = ?",
                    [(status.value, now, i) for i in ids],
                )
            else:
                cur = await conn.executemany(
                    "UPDATE items SET status = ? WHERE id = ?", [(status.value, i) for i in ids]
                )
            return cur.rowcount

    async def reviewed_count_today(self, now: dt.datetime, tz: str | dt.tzinfo) -> int:
        """Items with a review attempt (success or failure) on `now`'s local day in `tz`."""
        start, end = _local_day_bounds(now, tz)
        row = await self._one(
            "SELECT count(*) FROM items WHERE last_review_at >= ? AND last_review_at < ?",
            (_ts(start), _ts(end)),
        )
        return int(row[0]) if row is not None else 0

    # ---- stories ---------------------------------------------------------------------

    async def open_stories(self, now: dt.datetime, hours: int = 72) -> list[StoryRef]:
        cutoff = _ts(now - dt.timedelta(hours=hours))
        rows = await self._all(
            "SELECT id, headline FROM stories WHERE last_updated_at >= ?"
            " ORDER BY last_updated_at DESC, id DESC",
            (cutoff,),
        )
        return [StoryRef(id=r["id"], headline=r["headline"]) for r in rows]

    async def create_story(self, headline: str, category: Category, at: dt.datetime) -> int:
        when = _ts(at)
        async with self._tx() as conn:
            cur = await conn.execute(
                "INSERT INTO stories (headline, category, first_seen_at, last_updated_at)"
                " VALUES (?, ?, ?, ?)",
                (headline, category.value, when, when),
            )
            return _new_id(cur.lastrowid)

    async def attach_item(self, item_id: int, story_id: int, at: dt.datetime) -> None:
        """Link an item to a story and touch the story. Call recompute_story_score after."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE stories SET last_updated_at = max(last_updated_at, ?) WHERE id = ?",
                (_ts(at), story_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no story {story_id}")
            cur = await conn.execute(
                "UPDATE items SET story_id = ? WHERE id = ?", (story_id, item_id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"no item {item_id}")

    async def recompute_story_score(self, story_id: int) -> float:
        """Score = best item signal + STORY_SOURCE_BONUS per extra source. Tracks the lead
        item; a new lead flags the story for (re)writing."""
        async with self._tx() as conn:
            async with conn.execute(
                "SELECT id, signal, source_id FROM items WHERE story_id = ?"
                " ORDER BY COALESCE(signal, -1) DESC, id ASC",
                (story_id,),
            ) as cur:
                rows = list(await cur.fetchall())
            async with conn.execute(
                "SELECT lead_item_id FROM stories WHERE id = ?", (story_id,)
            ) as cur:
                story = await cur.fetchone()
            if story is None:
                raise KeyError(f"no story {story_id}")
            if not rows:
                score, lead = 0.0, None
            else:
                best = rows[0]["signal"] or 0.0
                sources = len({r["source_id"] for r in rows})
                score = float(best) + STORY_SOURCE_BONUS * (sources - 1)
                lead = rows[0]["id"]
            if lead != story["lead_item_id"]:
                await conn.execute(
                    "UPDATE stories SET score = ?, lead_item_id = ?, needs_text = 1,"
                    " text_attempts = 0 WHERE id = ?",
                    (score, lead, story_id),
                )
            else:
                await conn.execute("UPDATE stories SET score = ? WHERE id = ?", (score, story_id))
            return score

    async def stories_needing_text(self, limit: int, include_why: bool) -> list[WriteInput]:
        """Stories flagged for writing, best score first, each with its best 1-2 item texts."""
        stories = await self._all(
            "SELECT id, headline FROM stories"
            " WHERE needs_text = 1 AND text_attempts < ? AND lead_item_id IS NOT NULL"
            " ORDER BY score DESC, id ASC LIMIT ?",
            (MAX_TEXT_ATTEMPTS, limit),
        )
        result: list[WriteInput] = []
        for story in stories:
            items = await self._all(
                "SELECT title, body_excerpt, snippet FROM items WHERE story_id = ?"
                " ORDER BY COALESCE(signal, -1) DESC, id ASC LIMIT 2",
                (story["id"],),
            )
            texts = [
                f"{i['title']}\n\n{_review_text(i['body_excerpt'], i['snippet'])}" for i in items
            ]
            result.append(
                WriteInput(
                    story_id=story["id"],
                    headline=story["headline"],
                    texts=texts,
                    include_why=include_why,
                )
            )
        return result

    async def mark_text_failed(self, story_ids: list[int]) -> None:
        """Count a failed write; after MAX_TEXT_ATTEMPTS the story waits for a new lead."""
        if not story_ids:
            return
        async with self._tx() as conn:
            await conn.executemany(
                "UPDATE stories SET text_attempts = text_attempts + 1 WHERE id = ?",
                [(i,) for i in story_ids],
            )

    async def save_story_text(self, text: StoryText) -> None:
        """Save a summary; a None why-it-matters keeps the stored line."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE stories SET summary = ?,"
                " why_it_matters = COALESCE(?, why_it_matters), needs_text = 0, text_attempts = 0"
                " WHERE id = ?",
                (text.summary, text.why_it_matters, text.story_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no story {text.story_id}")

    async def today_stories(
        self, now: dt.datetime, threshold: float, hours: int = 24
    ) -> list[Story]:
        """Stories updated in the last `hours` with score >= threshold, or with an item whose
        review failed (never hidden, whatever the score), best first, with every source item
        (best signal first)."""
        cutoff = _ts(now - dt.timedelta(hours=hours))
        rows = await self._all(
            "SELECT * FROM stories st WHERE st.last_updated_at >= ?"
            " AND (st.score >= ? OR EXISTS (SELECT 1 FROM items i"
            "      WHERE i.story_id = st.id AND i.status = 'review_failed'))"
            " ORDER BY st.score DESC, st.last_updated_at DESC, st.id DESC",
            (cutoff, threshold),
        )
        stories: list[Story] = []
        for r in rows:
            items = await self._all(
                "SELECT i.id, i.url, i.title, i.published_at, s.name, s.kind"
                " FROM items i JOIN sources s ON s.id = i.source_id WHERE i.story_id = ?"
                " ORDER BY COALESCE(i.signal, -1) DESC, i.id ASC",
                (r["id"],),
            )
            stories.append(
                Story(
                    id=r["id"],
                    headline=r["headline"],
                    category=Category(r["category"]),
                    score=r["score"],
                    summary=r["summary"],
                    why_it_matters=r["why_it_matters"],
                    first_seen_at=_req_dt(r["first_seen_at"]),
                    last_updated_at=_req_dt(r["last_updated_at"]),
                    feedback=r["feedback"],
                    sources=[
                        StorySource(
                            item_id=i["id"],
                            source_name=i["name"],
                            source_kind=SourceKind(i["kind"]),
                            url=i["url"],
                            title=i["title"],
                            published_at=_dt(i["published_at"]),
                        )
                        for i in items
                    ],
                )
            )
        return stories

    async def top_stories_for_briefing(
        self, now: dt.datetime, limit: int = 15
    ) -> list[BriefingStory]:
        """Summarised stories of the last 24 hours, best score first; url is the lead item's."""
        cutoff = _ts(now - dt.timedelta(hours=24))
        rows = await self._all(
            "SELECT st.id, st.headline, st.category, st.summary, i.url"
            " FROM stories st JOIN items i ON i.id = st.lead_item_id"
            " WHERE st.last_updated_at >= ? AND st.summary IS NOT NULL"
            " ORDER BY st.score DESC, st.id ASC LIMIT ?",
            (cutoff, limit),
        )
        return [
            BriefingStory(
                story_id=r["id"],
                headline=r["headline"],
                category=Category(r["category"]),
                summary=r["summary"],
                url=r["url"],
            )
            for r in rows
        ]

    # ---- briefings -------------------------------------------------------------------

    async def save_briefing(
        self, date: dt.date, model: str, body: str, story_ids: list[int], at: dt.datetime
    ) -> None:
        """Save the briefing for `date` (local date); a second save for that date replaces it."""
        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO briefings (date, model, body, story_ids_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (date) DO UPDATE SET model = excluded.model, body = excluded.body,"
                " story_ids_json = excluded.story_ids_json, created_at = excluded.created_at",
                (date.isoformat(), model, body, json.dumps(story_ids), _ts(at)),
            )

    @staticmethod
    def _briefing(row: aiosqlite.Row | None) -> BriefingRecord | None:
        if row is None:
            return None
        return BriefingRecord(
            date=dt.date.fromisoformat(row["date"]),
            model=row["model"],
            body=row["body"],
            story_ids=json.loads(row["story_ids_json"]),
            created_at=_req_dt(row["created_at"]),
        )

    async def get_briefing(self, date: dt.date) -> BriefingRecord | None:
        return self._briefing(
            await self._one("SELECT * FROM briefings WHERE date = ?", (date.isoformat(),))
        )

    async def latest_briefing(self) -> BriefingRecord | None:
        return self._briefing(await self._one("SELECT * FROM briefings ORDER BY date DESC LIMIT 1"))

    async def briefing_dates(self) -> list[dt.date]:
        rows = await self._all("SELECT date FROM briefings ORDER BY date DESC")
        return [dt.date.fromisoformat(r["date"]) for r in rows]

    # ---- spend -----------------------------------------------------------------------

    async def add_spend(self, usage: Usage, cost_usd: float, at: dt.datetime) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO spend (at, model, provider, purpose, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, cost_usd)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _ts(at),
                    usage.model,
                    usage.provider,
                    usage.purpose.value,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    cost_usd,
                ),
            )

    async def month_spend(self, now: dt.datetime, tz: str | dt.tzinfo) -> float:
        """USD spent in `now`'s calendar month, with month boundaries in `tz`."""
        start, end = _local_month_bounds(now, tz)
        row = await self._one(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE at >= ? AND at < ?",
            (_ts(start), _ts(end)),
        )
        return float(row[0]) if row is not None else 0.0

    async def spend_by_day(self, now: dt.datetime, tz: str | dt.tzinfo) -> list[tuple[str, float]]:
        """("YYYY-MM-DD", USD) for every local day from the 1st of `now`'s month to today."""
        zone = _zone(tz)
        start, _ = _local_month_bounds(now, zone)
        _, end = _local_day_bounds(now, zone)
        rows = await self._all(
            "SELECT at, cost_usd FROM spend WHERE at >= ? AND at < ?", (_ts(start), _ts(end))
        )
        today = now.astimezone(zone).date()
        first = today.replace(day=1)
        totals = {first + dt.timedelta(days=n): 0.0 for n in range(today.day)}
        for r in rows:
            day = _req_dt(r["at"]).astimezone(zone).date()
            totals[day] = totals.get(day, 0.0) + r["cost_usd"]
        return [(day.isoformat(), round(total, 6)) for day, total in sorted(totals.items())]

    # ---- runs ------------------------------------------------------------------------

    async def start_run(self, trigger: str, at: dt.datetime) -> int:
        async with self._tx() as conn:
            cur = await conn.execute(
                "INSERT INTO runs (started_at, trigger) VALUES (?, ?)", (_ts(at), trigger)
            )
            return _new_id(cur.lastrowid)

    async def finish_run(
        self,
        run_id: int,
        at: dt.datetime,
        counts: dict[str, int],
        cost: float,
        error: str | None,
    ) -> None:
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE runs SET finished_at = ?, counts_json = ?, cost_usd = ?, error = ?"
                " WHERE id = ?",
                (_ts(at), json.dumps(counts), cost, error, run_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no run {run_id}")

    async def last_runs(self, n: int = 10) -> list[RunRecord]:
        rows = await self._all("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (n,))
        return [
            RunRecord(
                id=r["id"],
                trigger=r["trigger"],
                started_at=_req_dt(r["started_at"]),
                finished_at=_dt(r["finished_at"]),
                counts=json.loads(r["counts_json"]),
                cost_usd=r["cost_usd"],
                error=r["error"],
            )
            for r in rows
        ]

    # ---- settings --------------------------------------------------------------------

    async def get_setting(self, key: str) -> Any:
        """The JSON-decoded value, or None when the key is absent."""
        row = await self._one("SELECT value_json FROM settings WHERE key = ?", (key,))
        return json.loads(row["value_json"]) if row is not None else None

    async def set_setting(self, key: str, value: Any) -> None:
        """Store any JSON-serialisable value; None deletes the key."""
        async with self._tx() as conn:
            if value is None:
                await conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                await conn.execute(
                    "INSERT INTO settings (key, value_json) VALUES (?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json",
                    (key, json.dumps(value)),
                )

    # ---- feedback --------------------------------------------------------------------

    async def set_feedback(self, story_id: int, value: int) -> None:
        if value not in (-1, 0, 1):
            raise ValueError("feedback must be -1, 0 or 1")
        at = _ts(self.clock.now()) if value else None
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE stories SET feedback = ?, feedback_at = ? WHERE id = ?",
                (value, at, story_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no story {story_id}")

    async def source_feedback(self, days: int, now: dt.datetime) -> dict[int, tuple[int, int]]:
        """{source_id: (ups, downs)} over stories rated in the last `days`; each rated story
        counts once for every distinct source among its items."""
        cutoff = _ts(now - dt.timedelta(days=days))
        rows = await self._all(
            "SELECT DISTINCT st.id AS story_id, st.feedback, i.source_id"
            " FROM stories st JOIN items i ON i.story_id = st.id"
            " WHERE st.feedback != 0 AND st.feedback_at >= ?",
            (cutoff,),
        )
        tally: dict[int, tuple[int, int]] = {}
        for r in rows:
            ups, downs = tally.get(r["source_id"], (0, 0))
            if r["feedback"] > 0:
                ups += 1
            else:
                downs += 1
            tally[r["source_id"]] = (ups, downs)
        return tally

    async def feedback_examples(self) -> list[tuple[dict[str, Any], int]]:
        """(lead-item features, label) per rated story with items, newest thumb first.

        Features: signal, junk and category from the lead item's verdict (None / False /
        the story's category when unjudged), plus its source's trust and kind. Label: -1 or 1.
        """
        rows = await self._all(_LEAD_ITEMS, (1, "[]"))
        return [(_lead_features(r), int(r["feedback"])) for r in rows]

    async def story_features(self, story_ids: list[int]) -> dict[int, dict[str, Any]]:
        """{story_id: lead-item features} for the requested stories that have items."""
        if not story_ids:
            return {}
        rows = await self._all(_LEAD_ITEMS, (0, json.dumps(story_ids)))
        return {r["story_id"]: _lead_features(r) for r in rows}

    # ---- maintenance -----------------------------------------------------------------

    async def backup(self, dest_path: str | Path) -> None:
        """Online backup to a new file (SQLite backup API; consistent while running)."""
        dest = Path(dest_path)
        if await asyncio.to_thread(dest.exists):
            raise FileExistsError(f"backup target exists: {dest}")
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        async with self._lock, aiosqlite.connect(dest) as target:
            await self._db.backup(target)
