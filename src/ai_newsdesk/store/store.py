"""Every SQL query in the app lives here (V1-S1).

One connection, serialised by an asyncio.Lock: each public method holds the lock for its
whole transaction, so concurrent coroutines never interleave statements. Timestamps are
stored as UTC ISO-8601 text with microseconds (`_ts`), so text order is time order.
"""

import asyncio
import datetime as dt
import json
import logging
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
    ConsolidationStory,
    ItemStatus,
    ReviewInput,
    SourceConfig,
    SourceKind,
    SourceOverview,
    SourceResult,
    Story,
    StoryRef,
    StorySource,
    StoryText,
    TokenTotals,
    Trust,
    UnscoredItem,
    Usage,
    UsageStats,
    Verdict,
    WriteInput,
    XPost,
)
from ai_newsdesk.protocols import Clock
from ai_newsdesk.store.db import apply_migrations, connect
from ai_newsdesk.store.records import (
    LABELS,
    BriefingRecord,
    CompareItem,
    CompareJob,
    CompareModel,
    CompareResult,
    CompareRun,
    CompareStatus,
    Label,
    RunRecord,
    SourceStats,
)

log = logging.getLogger(__name__)

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


def _require_aware(at: dt.datetime) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Store timestamps must be timezone-aware")


def _dt(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value is not None else None


def _req_dt(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _zone(tz: str | dt.tzinfo) -> dt.tzinfo:
    return ZoneInfo(tz) if isinstance(tz, str) else tz


def _local_day_bounds(now: dt.datetime, tz: str | dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    _require_aware(now)
    zone = _zone(tz)
    day = now.astimezone(zone).date()
    start = dt.datetime(day.year, day.month, day.day, tzinfo=zone)
    nxt = day + dt.timedelta(days=1)
    end = dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return start, end


def _local_month_bounds(now: dt.datetime, tz: str | dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    _require_aware(now)
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


def _review_input(r: aiosqlite.Row) -> ReviewInput:
    """What the judge reads for an item row; `story_candidates` is left for cluster.py."""
    return ReviewInput(
        item_id=r["id"],
        source_name=r["source_name"],
        source_kind=SourceKind(r["source_kind"]),
        trust=Trust(r["trust"]),
        title=r["title"],
        author=r["author"],
        text=_review_text(r["body_excerpt"], r["snippet"]),
    )


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


# Today's window: stories updated since a cutoff with score >= threshold, or with an item whose
# review failed (never hidden, whatever the score). News only: a story whose items are all X
# posts is not listed. Parameters: (cutoff, threshold). Callers add the dismissed filter.
_TODAY_WHERE = (
    "st.last_updated_at >= ?"
    " AND EXISTS (SELECT 1 FROM items i JOIN sources s ON s.id = i.source_id"
    "      WHERE i.story_id = st.id AND s.kind != 'x_person')"
    " AND (st.score >= ? OR EXISTS (SELECT 1 FROM items i"
    "      JOIN sources s ON s.id = i.source_id WHERE i.story_id = st.id"
    "      AND i.status = 'review_failed' AND s.kind != 'x_person'))"
)

# The X page's window: posts from X account sources fetched since a cutoff, except dropped posts
# and posts the judge marked junk. Parameter: (cutoff,). Callers add the dismissed filter.
_X_POSTS_WHERE = (
    "s.kind = 'x_person' AND i.status != 'dropped' AND i.fetched_at >= ?"
    " AND COALESCE(json_extract(i.judge_json, '$.junk'), 0) = 0"
)


def _dismissed_sql(alias: str, dismissed: bool) -> str:
    return f"{alias}.dismissed_at IS {'NOT NULL' if dismissed else 'NULL'}"


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
        """One write transaction, holding the lock; rolled back on any error.

        A failed COMMIT is rolled back too, so the connection is never left inside an open
        transaction. ROLLBACK runs only while a transaction is still open (SQLite rolls some
        errors back by itself), so the original error is always the one raised.
        """
        async with self._lock:
            conn = self._db
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                await conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
                raise

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

    async def source_overview(self, now: dt.datetime) -> list[SourceOverview]:
        """Every source (muted ones too) with step 1's health stats plus 7-day item counts."""
        configs = {s.id: s for s in await self.list_sources(enabled_only=False)}
        recent = {
            int(r["source_id"]): (int(r["items_7d"]), int(r["stories_7d"]))
            for r in await self._all(
                "SELECT source_id, COUNT(*) AS items_7d,"
                " COUNT(story_id) AS stories_7d"
                " FROM items WHERE fetched_at >= ? GROUP BY source_id",
                (_ts(now - dt.timedelta(days=7)),),
            )
        }
        rows: list[SourceOverview] = []
        for stats in await self.source_stats():
            src = configs.get(stats.id)
            if src is None:  # deleted between the two reads
                continue
            items_7d, stories_7d = recent.get(stats.id, (0, 0))
            rows.append(
                SourceOverview(
                    source=src,
                    last_ok_at=stats.last_ok_at,
                    last_error=stats.last_error or None,
                    items_7d=items_7d,
                    stories_7d=stories_7d,
                    items_total=stats.item_count,
                )
            )
        return rows

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
                try:
                    canonical = self.canonicalise(c.url)
                    published = _ts(c.published_at) if c.published_at else None
                except ValueError as exc:
                    log.warning("skipping candidate from source %s: %s", c.source_id, exc)
                    continue
                cur = await conn.execute(
                    "INSERT INTO items (source_id, url, canonical_url, title, author,"
                    " published_at, fetched_at, snippet, hn_points)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (canonical_url) DO NOTHING",
                    (
                        c.source_id,
                        c.url,
                        canonical,
                        c.title,
                        c.author,
                        published,
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

    async def stranded_new_items(
        self, now: dt.datetime, hours: int = 48
    ) -> list[tuple[int, Candidate, SourceConfig]]:
        """Items still `new` that were fetched in the last `hours`, with their source, in id
        order. Only a run that failed after inserting them leaves items `new`; the next run puts
        them back through the pre-filter so they are queued or dropped, never stranded."""
        rows = await self._all(
            "SELECT i.id AS item_id, i.url, i.title, i.author, i.published_at, i.snippet,"
            " i.hn_points, s.* FROM items i JOIN sources s ON s.id = i.source_id"
            " WHERE i.status = 'new' AND i.fetched_at >= ? ORDER BY i.id",
            (_ts(now - dt.timedelta(hours=hours)),),
        )
        return [
            (
                int(r["item_id"]),
                Candidate(
                    source_id=r["id"],
                    url=r["url"],
                    title=r["title"],
                    author=r["author"],
                    published_at=_dt(r["published_at"]),
                    snippet=r["snippet"] or "",
                    hn_points=r["hn_points"],
                ),
                self._source(r),
            )
            for r in rows
        ]

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
        return [_review_input(r) for r in rows]

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
        item; a new lead flags the story for (re)writing. X posts never count (feedback-1)."""
        async with self._tx() as conn:
            return await self._recompute(conn, story_id)

    @staticmethod
    async def _recompute(conn: aiosqlite.Connection, story_id: int) -> float:
        async with conn.execute(
            "SELECT i.id, i.signal, i.source_id FROM items i JOIN sources s ON s.id = i.source_id"
            " WHERE i.story_id = ? AND s.kind != 'x_person'"
            " ORDER BY COALESCE(i.signal, -1) DESC, i.id ASC",
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

    async def consolidation_candidates(
        self, now: dt.datetime, hours: int = 48, limit: int = 200
    ) -> list[ConsolidationStory]:
        """Open stories (updated in the last `hours`) with at least one item that is not an X
        post, best score first (then oldest), at most `limit`."""
        rows = await self._all(
            "SELECT st.id, st.headline, st.category, st.score, st.first_seen_at"
            " FROM stories st WHERE st.last_updated_at >= ?"
            " AND EXISTS (SELECT 1 FROM items i JOIN sources s ON s.id = i.source_id"
            "             WHERE i.story_id = st.id AND s.kind != 'x_person')"
            " ORDER BY st.score DESC, st.first_seen_at ASC, st.id ASC LIMIT ?",
            (_ts(now - dt.timedelta(hours=hours)), limit),
        )
        return [
            ConsolidationStory(
                id=r["id"],
                headline=r["headline"],
                category=Category(r["category"]),
                score=float(r["score"]),
                first_seen_at=_req_dt(r["first_seen_at"]),
            )
            for r in rows
        ]

    async def merge_stories(self, keep_id: int, merge_ids: list[int]) -> None:
        """Fold `merge_ids` into `keep_id` in one transaction: move their items, carry the most
        recent non-zero feedback when `keep_id` has none, keep the earliest first-seen and latest
        update times, delete the merged stories, rescore and flag `keep_id` for rewriting.
        Unknown ids (or an unknown `keep_id`) are skipped silently.

        Dismissal (feedback-1): `keep_id` keeps its own `dismissed_at`, whatever the merged
        stories' state. A dismissed story folded into a visible one shows again (as the kept
        story); a dismissed `keep_id` stays hidden even when visible stories join it."""
        others = sorted({i for i in merge_ids if i != keep_id})
        if not others:
            return
        async with self._tx() as conn:
            async with conn.execute(
                "SELECT id, feedback, feedback_at, first_seen_at, last_updated_at FROM stories"
                " WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps([keep_id, *others]),),
            ) as cur:
                rows = {int(r["id"]): r for r in await cur.fetchall()}
            keep = rows.pop(keep_id, None)
            if keep is None or not rows:
                return
            merged = sorted(rows)
            first_seen = min(r["first_seen_at"] for r in [keep, *rows.values()])
            last_updated = max(r["last_updated_at"] for r in [keep, *rows.values()])
            feedback, feedback_at = keep["feedback"], keep["feedback_at"]
            if feedback == 0:
                rated = [r for r in rows.values() if r["feedback"] != 0]
                if rated:
                    best = max(rated, key=lambda r: (r["feedback_at"] or "", r["id"]))
                    feedback, feedback_at = best["feedback"], best["feedback_at"]
            ids_json = json.dumps(merged)
            await conn.execute(
                "UPDATE items SET story_id = ? WHERE story_id IN (SELECT value FROM json_each(?))",
                (keep_id, ids_json),
            )
            await conn.execute(
                "DELETE FROM stories WHERE id IN (SELECT value FROM json_each(?))", (ids_json,)
            )
            await conn.execute(
                "UPDATE stories SET first_seen_at = ?, last_updated_at = ?, feedback = ?,"
                " feedback_at = ?, needs_text = 1, text_attempts = 0 WHERE id = ?",
                (first_seen, last_updated, feedback, feedback_at, keep_id),
            )
            await self._recompute(conn, keep_id)

    async def stories_needing_text(self, limit: int, include_why: bool) -> list[WriteInput]:
        """Stories flagged for writing, best score first, each with its best 1-2 item texts."""
        stories = await self._all(
            "SELECT id, headline FROM stories"
            " WHERE needs_text = 1 AND text_attempts < ? AND lead_item_id IS NOT NULL"
            " ORDER BY score DESC, id ASC LIMIT ?",
            (MAX_TEXT_ATTEMPTS, limit),
        )
        return [await self._write_input(story, include_why) for story in stories]

    async def _write_input(self, story: aiosqlite.Row, include_why: bool) -> WriteInput:
        """What the writer reads for a story (id, headline): its best 1-2 item texts."""
        items = await self._all(
            "SELECT i.title, i.body_excerpt, i.snippet FROM items i"
            " JOIN sources s ON s.id = i.source_id"
            " WHERE i.story_id = ? AND s.kind != 'x_person'"
            " ORDER BY COALESCE(i.signal, -1) DESC, i.id ASC LIMIT 2",
            (story["id"],),
        )
        texts = [f"{i['title']}\n\n{_review_text(i['body_excerpt'], i['snippet'])}" for i in items]
        return WriteInput(
            story_id=story["id"], headline=story["headline"], texts=texts, include_why=include_why
        )

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
        self, now: dt.datetime, threshold: float, hours: int = 24, *, dismissed: bool = False
    ) -> list[Story]:
        """Stories updated in the last `hours` with score >= threshold, or with an item whose
        review failed (never hidden, whatever the score), best first, with every source item
        (best signal first). News only: X posts are ignored, and a story with nothing else is
        not listed. Dismissed stories are left out; `dismissed=True` lists only those."""
        cutoff = _ts(now - dt.timedelta(hours=hours))
        rows = await self._all(
            f"SELECT * FROM stories st WHERE {_TODAY_WHERE}"  # noqa: S608
            f" AND {_dismissed_sql('st', dismissed)}"
            " ORDER BY st.score DESC, st.last_updated_at DESC, st.id DESC",
            (cutoff, threshold),
        )
        stories: list[Story] = []
        for r in rows:
            items = await self._all(
                "SELECT i.id, i.url, i.title, i.published_at, i.signal, s.name, s.kind"
                " FROM items i JOIN sources s ON s.id = i.source_id"
                " WHERE i.story_id = ? AND s.kind != 'x_person'"
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
                    scored=any(i["signal"] is not None for i in items),
                    dismissed=r["dismissed_at"] is not None,
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

    async def count_dismissed_stories(
        self, now: dt.datetime, threshold: float, hours: int = 24
    ) -> int:
        """How many dismissed stories `today_stories` would otherwise list."""
        row = await self._one(
            f"SELECT count(*) FROM stories st WHERE {_TODAY_WHERE}"  # noqa: S608
            " AND st.dismissed_at IS NOT NULL",
            (_ts(now - dt.timedelta(hours=hours)), threshold),
        )
        return int(row[0]) if row is not None else 0

    async def dismiss_story(self, story_id: int, at: dt.datetime) -> bool:
        """Hide a story from Today. Idempotent (keeps the first time). False if no such story.
        Only hides: feedback, source weights and the taste model are untouched."""
        when = _ts(at)
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE stories SET dismissed_at = COALESCE(dismissed_at, ?) WHERE id = ?",
                (when, story_id),
            )
            return cur.rowcount > 0

    async def undismiss_story(self, story_id: int) -> bool:
        """Show a dismissed story again. Idempotent. False if no such story."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE stories SET dismissed_at = NULL WHERE id = ?", (story_id,)
            )
            return cur.rowcount > 0

    async def top_stories_for_briefing(
        self, now: dt.datetime, limit: int = 15
    ) -> list[BriefingStory]:
        """Summarised stories of the last 24 hours, best score first; url is the lead item's
        (never an X post's)."""
        cutoff = _ts(now - dt.timedelta(hours=24))
        rows = await self._all(
            "SELECT st.id, st.headline, st.category, st.summary, i.url"
            " FROM stories st JOIN items i ON i.id = st.lead_item_id"
            " JOIN sources s ON s.id = i.source_id"
            " WHERE st.last_updated_at >= ? AND st.summary IS NOT NULL"
            " AND s.kind != 'x_person'"
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

    async def usage_last_days(self, now: dt.datetime, days: int = 7) -> UsageStats:
        """Finished runs, reviewed items and spend by purpose over the last ``days`` days."""
        since = _ts(now - dt.timedelta(days=days))
        runs = await self._one(
            "SELECT COUNT(*) AS n FROM runs WHERE started_at >= ? AND finished_at IS NOT NULL",
            (since,),
        )
        reviewed = await self._one(
            "SELECT COUNT(*) AS n FROM items WHERE status = 'reviewed' AND fetched_at >= ?",
            (since,),
        )
        rows = await self._all(
            "SELECT purpose, COALESCE(SUM(cost_usd), 0) AS usd,"
            " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,"
            " SUM(cache_read_tokens) AS cache_read_tokens,"
            " SUM(cache_write_tokens) AS cache_write_tokens"
            " FROM spend WHERE at >= ? GROUP BY purpose",
            (since,),
        )
        by_purpose = {str(r["purpose"]): r for r in rows}

        def usd(purpose: str) -> float:
            row = by_purpose.get(purpose)
            return float(row["usd"]) if row is not None else 0.0

        def tokens(purpose: str) -> TokenTotals:
            row = by_purpose.get(purpose)
            if row is None:
                return TokenTotals()
            return TokenTotals(
                input_tokens=int(row["input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
                cache_read_tokens=int(row["cache_read_tokens"] or 0),
                cache_write_tokens=int(row["cache_write_tokens"] or 0),
            )

        return UsageStats(
            days=days,
            runs=int(runs["n"]) if runs is not None else 0,
            items_reviewed=int(reviewed["n"]) if reviewed is not None else 0,
            review_usd=usd("review"),
            write_usd=usd("write"),
            briefing_usd=usd("briefing"),
            review_tokens=tokens("review"),
            write_tokens=tokens("write"),
            briefing_tokens=tokens("briefing"),
        )

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

    # --- web queries (step 5) -------------------------------------------------------------

    async def stories_by_ids(self, ids: list[int]) -> dict[int, Story]:
        """Stories by ID with their sources, lead (highest signal, then lowest id) first.
        Dismissed stories are included (and say so): the briefing and card refreshes read them."""
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = await self._all(
            "SELECT id, headline, category, score, summary, why_it_matters, first_seen_at,"  # noqa: S608
            f" last_updated_at, feedback, dismissed_at FROM stories WHERE id IN ({marks})",
            ids,
        )
        stories: dict[int, Story] = {
            int(r["id"]): Story(
                id=int(r["id"]),
                headline=r["headline"],
                category=Category(r["category"]),
                score=float(r["score"] or 0.0),
                summary=r["summary"],
                why_it_matters=r["why_it_matters"],
                first_seen_at=_req_dt(r["first_seen_at"]),
                last_updated_at=_req_dt(r["last_updated_at"]),
                feedback=int(r["feedback"] or 0),
                scored=False,  # set below from the items' signals
                dismissed=r["dismissed_at"] is not None,
            )
            for r in rows
        }
        items = await self._all(
            "SELECT i.id AS item_id, i.story_id, i.url, i.title, i.published_at, i.signal,"  # noqa: S608
            " s.name AS source_name, s.kind AS source_kind"
            " FROM items i JOIN sources s ON s.id = i.source_id"
            f" WHERE i.story_id IN ({marks}) AND s.kind != 'x_person'"
            " ORDER BY i.story_id, COALESCE(i.signal, -1) DESC, i.id",
            ids,
        )
        for r in items:
            story = stories.get(int(r["story_id"]))
            if story is not None:
                story.scored = story.scored or r["signal"] is not None
                story.sources.append(
                    StorySource(
                        item_id=int(r["item_id"]),
                        source_name=r["source_name"],
                        source_kind=SourceKind(r["source_kind"]),
                        url=r["url"],
                        title=r["title"],
                        published_at=_dt(r["published_at"]),
                    )
                )
        return stories

    async def unscored_items(
        self, now: dt.datetime, hours: int = 24, limit: int = 30
    ) -> list[UnscoredItem]:
        """Recent items with no verdict that are not already shown in a story, best heuristic
        score first. X posts are left out (they have their own page, `x_posts`)."""
        rows = await self._all(
            "SELECT i.id AS item_id, i.url, i.title, i.published_at, i.status,"
            " s.name AS source_name, s.kind AS source_kind"
            " FROM items i JOIN sources s ON s.id = i.source_id"
            " WHERE i.status IN ('review_failed', 'queued') AND i.story_id IS NULL"
            " AND s.kind != 'x_person' AND i.fetched_at >= ? AND i.dismissed_at IS NULL"
            " ORDER BY i.heuristic_score DESC, i.fetched_at DESC, i.id DESC LIMIT ?",
            (_ts(now - dt.timedelta(hours=hours)), limit),
        )
        return [
            UnscoredItem(
                item_id=int(r["item_id"]),
                source_name=r["source_name"],
                source_kind=SourceKind(r["source_kind"]),
                url=r["url"],
                title=r["title"],
                published_at=_dt(r["published_at"]),
                status=ItemStatus(r["status"]),
            )
            for r in rows
        ]

    async def x_posts(
        self, now: dt.datetime, days: int = 7, limit: int = 200, *, dismissed: bool = False
    ) -> list[XPost]:
        """Posts from X account sources fetched in the last `days`, newest first (published,
        else fetched). Reviewed and unreviewed posts are included; dropped posts and posts the
        judge marked junk are not. Dismissed posts are left out; `dismissed=True` lists only
        those."""
        rows = await self._all(
            "SELECT i.id, i.url, i.title, i.author, i.snippet, i.published_at, i.fetched_at,"  # noqa: S608
            " i.signal, i.status, json_extract(i.judge_json, '$.junk') AS junk,"
            " i.dismissed_at, s.name AS source_name, s.config_json"
            " FROM items i JOIN sources s ON s.id = i.source_id"
            f" WHERE {_X_POSTS_WHERE} AND {_dismissed_sql('i', dismissed)}"
            " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC LIMIT ?",
            (_ts(now - dt.timedelta(days=days)), limit),
        )
        posts: list[XPost] = []
        for r in rows:
            handle = r["author"] or str(json.loads(r["config_json"]).get("handle", ""))
            posts.append(
                XPost(
                    item_id=r["id"],
                    handle=handle.strip().lstrip("@"),
                    source_name=r["source_name"],
                    url=r["url"],
                    title=r["title"],
                    text=r["snippet"] or "",
                    published_at=_dt(r["published_at"]),
                    fetched_at=_req_dt(r["fetched_at"]),
                    signal=round(r["signal"]) if r["signal"] is not None else None,
                    status=ItemStatus(r["status"]),
                    junk=bool(r["junk"]) if r["junk"] is not None else None,
                    dismissed=r["dismissed_at"] is not None,
                )
            )
        return posts

    async def count_dismissed_x_posts(self, now: dt.datetime, days: int = 7) -> int:
        """How many dismissed posts `x_posts` would otherwise list."""
        row = await self._one(
            "SELECT count(*) FROM items i JOIN sources s ON s.id = i.source_id"  # noqa: S608
            f" WHERE {_X_POSTS_WHERE} AND i.dismissed_at IS NOT NULL",
            (_ts(now - dt.timedelta(days=days)),),
        )
        return int(row[0]) if row is not None else 0

    async def dismiss_item(self, item_id: int, at: dt.datetime) -> bool:
        """Hide an X post from the X page. Idempotent (keeps the first time). False if there is
        no such item or it is not an X post. Feedback and source weights are untouched."""
        when = _ts(at)
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE items SET dismissed_at = COALESCE(dismissed_at, ?) WHERE id = ?"
                " AND source_id IN (SELECT id FROM sources WHERE kind = 'x_person')",
                (when, item_id),
            )
            return cur.rowcount > 0

    async def undismiss_item(self, item_id: int) -> bool:
        """Show a dismissed X post again. Idempotent. False if no such X post."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE items SET dismissed_at = NULL WHERE id = ?"
                " AND source_id IN (SELECT id FROM sources WHERE kind = 'x_person')",
                (item_id,),
            )
            return cur.rowcount > 0

    # ---- comparisons (multi-provider spec 4) ---------------------------------------------
    # A comparison only ever writes these four tables: never items or stories.

    async def compare_judge_inputs(
        self, now: dt.datetime, limit: int, days: int = 7
    ) -> list[ReviewInput]:
        """A judge comparison's sample: the newest reviewed or queued news items (no X posts)
        of the last `days`, with the text the production judge reads."""
        rows = await self._all(
            "SELECT i.id, i.title, i.author, i.body_excerpt, i.snippet,"
            " s.name AS source_name, s.kind AS source_kind, s.trust AS trust"
            " FROM items i JOIN sources s ON s.id = i.source_id"
            " WHERE i.status IN ('reviewed', 'queued') AND s.kind != 'x_person'"
            "   AND i.fetched_at >= ?"
            " ORDER BY i.fetched_at DESC, i.id DESC LIMIT ?",
            (_ts(now - dt.timedelta(days=days)), limit),
        )
        return [_review_input(r) for r in rows]

    async def compare_writer_inputs(
        self, now: dt.datetime, limit: int, include_why: bool, hours: int = 48
    ) -> list[WriteInput]:
        """A writer comparison's sample: the best-scored news stories updated in the last
        `hours`, each with the texts the production writer reads."""
        stories = await self._all(
            "SELECT st.id, st.headline FROM stories st"
            " WHERE st.last_updated_at >= ? AND st.lead_item_id IS NOT NULL"
            " AND EXISTS (SELECT 1 FROM items i JOIN sources s ON s.id = i.source_id"
            "      WHERE i.story_id = st.id AND s.kind != 'x_person')"
            " ORDER BY st.score DESC, st.id ASC LIMIT ?",
            (_ts(now - dt.timedelta(hours=hours)), limit),
        )
        return [await self._write_input(story, include_why) for story in stories]

    async def compare_items(self, ids: list[int]) -> dict[int, CompareItem]:
        """Items by id with their production verdict, story thumb, source weight and label."""
        if not ids:
            return {}
        rows = await self._all(
            "SELECT i.id, i.title, i.url, i.judge_json, s.name, s.weight,"
            " COALESCE(st.feedback, 0) AS feedback, l.label"
            " FROM items i JOIN sources s ON s.id = i.source_id"
            " LEFT JOIN stories st ON st.id = i.story_id"
            " LEFT JOIN labels l ON l.item_id = i.id"
            " WHERE i.id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),),
        )
        return {
            int(r["id"]): CompareItem(
                item_id=int(r["id"]),
                title=r["title"],
                url=r["url"],
                source_name=r["name"],
                weight=float(r["weight"]),
                verdict=(
                    Verdict.model_validate_json(r["judge_json"])
                    if r["judge_json"] is not None
                    else None
                ),
                feedback=int(r["feedback"]),
                label=r["label"],
            )
            for r in rows
        }

    async def create_compare_run(
        self,
        job: CompareJob,
        models: list[CompareModel],
        sample_ids: list[int],
        cap_usd: float,
        at: dt.datetime,
    ) -> int:
        async with self._tx() as conn:
            cur = await conn.execute(
                "INSERT INTO compare_runs (job, models_json, sample_ids_json, cap_usd, status,"
                " started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                (
                    job,
                    json.dumps([m.model_dump() for m in models]),
                    json.dumps(sample_ids),
                    cap_usd,
                    _ts(at),
                ),
            )
            return _new_id(cur.lastrowid)

    async def finish_compare_run(
        self,
        run_id: int,
        status: CompareStatus,
        at: dt.datetime,
        cost_usd: float,
        error: str | None,
    ) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE compare_runs SET status = ?, finished_at = ?, cost_usd = ?, error = ?"
                " WHERE id = ?",
                (status, _ts(at), cost_usd, error, run_id),
            )

    async def fail_running_compare_runs(self, at: dt.datetime, error: str) -> int:
        """At start-up: a comparison still `running` was cut off by a restart."""
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE compare_runs SET status = 'failed', finished_at = ?, error = ?"
                " WHERE status = 'running'",
                (_ts(at), error),
            )
            return cur.rowcount

    @staticmethod
    def _compare_run(r: aiosqlite.Row) -> CompareRun:
        return CompareRun(
            id=int(r["id"]),
            job=r["job"],
            models=[CompareModel.model_validate(m) for m in json.loads(r["models_json"])],
            sample_ids=json.loads(r["sample_ids_json"]),
            cap_usd=float(r["cap_usd"]),
            status=r["status"],
            started_at=_req_dt(r["started_at"]),
            finished_at=_dt(r["finished_at"]),
            cost_usd=float(r["cost_usd"]),
            error=r["error"],
        )

    async def compare_run(self, run_id: int) -> CompareRun | None:
        row = await self._one("SELECT * FROM compare_runs WHERE id = ?", (run_id,))
        return self._compare_run(row) if row is not None else None

    async def compare_runs(self, limit: int = 20) -> list[CompareRun]:
        """Past comparisons, newest first."""
        rows = await self._all(
            "SELECT * FROM compare_runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [self._compare_run(r) for r in rows]

    async def add_compare_results(self, run_id: int, results: list[CompareResult]) -> None:
        async with self._tx() as conn:
            await conn.executemany(
                "INSERT OR REPLACE INTO compare_results (run_id, model_key, subject_id,"
                " output_json, ok, latency_ms, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        r.model_key,
                        r.subject_id,
                        json.dumps(r.output) if r.output is not None else None,
                        int(r.ok),
                        r.latency_ms,
                        r.cost_usd,
                    )
                    for r in results
                ],
            )

    async def compare_results(self, run_id: int) -> list[CompareResult]:
        rows = await self._all(
            "SELECT * FROM compare_results WHERE run_id = ? ORDER BY rowid", (run_id,)
        )
        return [
            CompareResult(
                model_key=r["model_key"],
                subject_id=int(r["subject_id"]),
                output=json.loads(r["output_json"]) if r["output_json"] is not None else None,
                ok=bool(r["ok"]),
                latency_ms=r["latency_ms"],
                cost_usd=float(r["cost_usd"]),
            )
            for r in rows
        ]

    async def set_label(self, item_id: int, label: Label | None, at: dt.datetime) -> None:
        """The owner's label for an item (one per item); None clears it."""
        if label is not None and label not in LABELS:
            raise ValueError(f"label must be one of {', '.join(LABELS)}")
        async with self._tx() as conn:
            if label is None:
                await conn.execute("DELETE FROM labels WHERE item_id = ?", (item_id,))
                return
            async with conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)) as cur:
                if await cur.fetchone() is None:
                    raise KeyError(f"no item {item_id}")
            await conn.execute(
                "INSERT INTO labels (item_id, label, set_at) VALUES (?, ?, ?)"
                " ON CONFLICT (item_id) DO UPDATE SET label = excluded.label,"
                " set_at = excluded.set_at",
                (item_id, label, _ts(at)),
            )

    async def labels(self, item_ids: list[int]) -> dict[int, Label]:
        rows = await self._all(
            "SELECT item_id, label FROM labels WHERE item_id IN (SELECT value FROM json_each(?))",
            (json.dumps(item_ids),),
        )
        return {int(r["item_id"]): r["label"] for r in rows}

    async def set_writer_pref(
        self, run_id: int, story_id: int, model_key: str, at: dt.datetime
    ) -> None:
        """The owner's preferred summary for a story in a writer comparison (one per story)."""
        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO writer_prefs (run_id, story_id, model_key, set_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (run_id, story_id) DO UPDATE SET model_key = excluded.model_key,"
                " set_at = excluded.set_at",
                (run_id, story_id, model_key, _ts(at)),
            )

    async def writer_prefs(self, run_id: int) -> dict[int, str]:
        rows = await self._all(
            "SELECT story_id, model_key FROM writer_prefs WHERE run_id = ?", (run_id,)
        )
        return {int(r["story_id"]): r["model_key"] for r in rows}
