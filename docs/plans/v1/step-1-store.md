# Step 1: Store (SQLite schema and migrations, Store, secrets box, runtime settings, pricing)

> Part of `docs/plans/2026-10-14-ai-newsdesk-v1.md`. Read that file first, for the Global Constraints and the shared contract, and also read the spec (section 9, data model) and ADR-0001. Steps use checkbox (`- [ ]`) syntax for tracking.

**Step goal:** All persistence goes through one async `Store`, on SQLite in WAL mode, with numbered SQL migrations applied at start-up. API keys entered in the UI are encrypted at rest. Run-time settings are typed and validated. A price table turns token counts into dollars for the budget.

**Branch:** `step-1-store`. **Depends on:** step 0, which provides models, protocols, clocks, fakes and fixtures.

**Task order:** 5 → 6 → 7 → 8. Each task is one commit. Task 7 needs the Store from Task 6. Task 8 reads `RuntimeSettings` in one test, so it comes after Task 7.

**House patterns:** WAL, `busy_timeout`, `foreign_keys=ON` and `synchronous=NORMAL` follow research-engine's `store/db.py`. Unlike research-engine, there is no SQLModel. Plain SQL lives in one file per migration, and all queries live in `store/store.py`.

---

## Contract additions

Other steps may rely on these names, in addition to the master contract. Methods named in the master contract keep their names; this section pins their parameters and behaviour.

**Imports.** `from ai_newsdesk.store import Store` (the form steps 3–5 use). `store/__init__.py` re-exports `Store` and nothing else, so `from ai_newsdesk.store.store import Store` (step 7) works too.

**`Store(db_path: str, *, clock: Clock | None = None, canonicalise: Callable[[str], str] = basic_canonical_url)`**
- `clock` defaults to `SystemClock()`. It is used where the contract passes no time: `recent_titles`, the `save_verdict` and `mark_items` review timestamps, and `set_feedback`.
- `canonicalise` must be idempotent. `app.py` may pass step 2's stricter normaliser.
- The store holds one aiosqlite connection behind an `asyncio.Lock`. Every method is atomic and safe to call concurrently.
- A later step that adds its own query methods (step 5: `stories_by_ids`, `unscored_items`, `source_overview`, `usage_last_days`) goes through the private helpers `self._all(sql, params)`, `self._one(sql, params)` (reads, under the lock) and `async with self._tx() as conn` (writes), never `self._db.execute` directly, so it can't interleave with another coroutine's transaction.

**Methods added beyond the master contract's list** (master plan R1):
- `close() -> None`
- `source_stats() -> list[SourceStats]`, for the Sources page.
- `items_for_enrichment(item_ids: list[int]) -> list[tuple[int, str, SourceKind, str]]` (step 2's `enrich_bodies`): `(item_id, url, kind of the item's source, snippet)` for each given id that exists, in id order. Unknown ids are skipped; `[]` gives `[]`. The excerpt is written back with `set_item_fields(item_id, body_excerpt=...)`.
- `mark_text_failed(story_ids: list[int]) -> None`. The pipeline (step 3) calls it for stories the writer left out; after `MAX_TEXT_ATTEMPTS` failures the story waits for a new lead.
- `set_source_weight(source_id: int, weight: float) -> None` (step 4's `feedback.update_source_weights`): `ValueError` outside 0.3–2.0, `KeyError` for an unknown source.
- `story_features(story_ids: list[int]) -> dict[int, dict[str, Any]]` (step 4's `taste.rank_today`): the lead-item feature dict (see `feedback_examples`) for each requested story that has at least one item; others are absent. `[]` gives `{}`.

**Pinned signatures** (`dt` = `datetime`; every datetime must be timezone-aware, or `ValueError`):

Sources:
- `seed_sources_if_empty(path: str | Path) -> int`: rows added; 0 if any source exists.
- `list_sources(enabled_only: bool = True) -> list[SourceConfig]`: ordered by kind, name.
- `get_source(source_id: int) -> SourceConfig | None`
- `upsert_source(kind: SourceKind, name: str, config: dict[str, ConfigValue], trust: Trust, *, enabled: bool = True, weight: float | None = None, source_id: int | None = None) -> int`
  - Later steps call it with these keywords (master plan R1); there is no `SourceConfig` overload and no `id=` alias.
  - With `source_id`, it updates that row (`KeyError` if none).
  - Otherwise it inserts, or updates by the unique `(kind, name)`.
  - `weight=None` (the default) means 1.0 on insert and **keeps the stored weight** on update, so editing or muting a source on the Sources page never resets the weight that feedback learned.
  - A `weight` outside 0.3–2.0 raises `ValueError`.
- `delete_source(source_id: int) -> bool`: also deletes the source's items (cascade).
- `record_source_result(result: SourceResult, at: dt) -> None`: sets `last_run_at`, `last_count`, `last_duration_s` and `last_error`. It sets `last_ok_at` only when `error is None`.

Items and review:
- `add_candidates(candidates: list[Candidate], at: dt) -> list[int]`
  - New items get status `new`.
  - It skips canonical URLs already stored, duplicates within the batch, and candidates whose `source_id` is unknown.
- `recent_titles(hours: int) -> list[tuple[int, str, int | None]]`: by `fetched_at`, relative to the store clock.
- `set_item_fields(item_id: int, **fields: Any) -> None`
  - Only `ITEM_FIELDS` are allowed: `title`, `author`, `snippet`, `body_excerpt`, `hn_points`, `heuristic_score`, `status`, `signal`, `category`, `judge_json`, `story_id`. Anything else raises `ValueError`.
  - Enums, datetimes, bools and dicts/lists are converted.
  - A missing item raises `KeyError`.
- `queued_review_inputs(limit: int, now: dt) -> list[ReviewInput]` (read-only)
  - It returns status `queued`, plus `review_failed` items with `review_attempts < MAX_REVIEW_ATTEMPTS (2)` (master plan R2). The pipeline does not retry within a run: an item that fails once comes back on the next run, and after a second failure it stays `review_failed` and is shown unscored.
  - Only items with `fetched_at` within 48 h of `now`.
  - Order: `heuristic_score` DESC, then oldest first, then id.
  - `text` is `body_excerpt`, else `snippet`, else `NO_TEXT = "(no text, title only)"`.
  - `story_candidates` is always empty here; cluster.py fills it.
- `expire_old_queued(now: dt, hours: int = 48) -> int`: only `queued` items become `expired`.
- `save_verdict(verdict: Verdict, signal_final: float) -> None`
  - It sets `status=reviewed`, `judge_json = verdict.model_dump_json()`, `signal = signal_final`, `category`, and `last_review_at = clock.now()`.
  - A missing item raises `KeyError`.
- `mark_items(ids: list[int], status: ItemStatus) -> int`: rows changed. `REVIEW_FAILED` also increments `review_attempts` and sets `last_review_at`.
- `reviewed_count_today(now: dt, tz: str | tzinfo) -> int`: items whose last review attempt (success or failure) falls on `now`'s local day.

Stories:
- `open_stories(now: dt, hours: int = 72) -> list[StoryRef]`: by `last_updated_at`, newest first.
- `create_story(headline: str, category: Category, at: dt) -> int`: score 0, `needs_text=1`.
- `attach_item(item_id: int, story_id: int, at: dt) -> None`
  - It sets the item's `story_id` and bumps the story's `last_updated_at`.
  - It does **not** rescore: call `recompute_story_score` afterwards.
  - A missing story or item raises `KeyError`.
- `recompute_story_score(story_id: int) -> float`
  - Score = best item `signal` + `STORY_SOURCE_BONUS (0.5)` × (distinct sources − 1).
  - The lead item is the highest signal, then the lowest id.
  - A **new lead** sets `needs_text=1` and `text_attempts=0`.
- `stories_needing_text(limit: int, include_why: bool) -> list[WriteInput]`
  - Stories with `needs_text=1`, `text_attempts < MAX_TEXT_ATTEMPTS (2)` and a lead item, best score first.
  - `texts` are the best two items as `"title\n\ntext"`, using the same text fallback as review.
- `save_story_text(text: StoryText) -> None`
  - It clears `needs_text` and `text_attempts`.
  - `why_it_matters=None` keeps the stored line (spec 7).
  - A missing story raises `KeyError`.
- `today_stories(now: dt, threshold: float, hours: int = 24) -> list[Story]`
  - Stories with `last_updated_at` in the window and either `score >= threshold` **or** at least one item in status `review_failed`, whatever the score ("an item with no verdict is never hidden"). Ordered by score DESC.
  - `sources` lists every item, best signal first.
- `top_stories_for_briefing(now: dt, limit: int = 15) -> list[BriefingStory]`: stories of the last 24 h that have a summary. `url` is the lead item's url.

Briefings (`date` is the local `datetime.date`):
- `save_briefing(date: date, model: str, body: str, story_ids: list[int], at: dt) -> None`: one per date; a second save replaces it.
- `get_briefing(date: date) -> BriefingRecord | None`
- `latest_briefing() -> BriefingRecord | None`
- `briefing_dates() -> list[date]`: newest first.

Spend and runs:
- `add_spend(usage: Usage, cost_usd: float, at: dt) -> None`
- `month_spend(now: dt, tz: str | tzinfo) -> float`: the calendar month in `tz` (pass `settings.tz`).
- `spend_by_day(now: dt, tz: str | tzinfo) -> list[tuple[str, float]]`: one `("YYYY-MM-DD", usd)` entry for every local day from the 1st to today, with zeros included.
- `start_run(trigger: str, at: dt) -> int`
- `finish_run(run_id: int, at: dt, counts: dict[str, int], cost: float, error: str | None) -> None`
- `last_runs(n: int = 10) -> list[RunRecord]`: newest first.

Settings, feedback and maintenance:
- `get_setting(key: str) -> Any`: the JSON-decoded value, or `None` when absent.
- `set_setting(key: str, value: Any) -> None`: JSON-encoded; `None` deletes the key.
- `set_feedback(story_id: int, value: int) -> None`: value in {-1, 0, 1} (`ValueError` otherwise); it stamps `feedback_at`.
- `source_feedback(days: int, now: dt) -> dict[int, tuple[int, int]]`: `{source_id: (ups, downs)}` over stories whose thumb (`feedback_at`, not `last_updated_at`) falls within `days` of `now`. Each rated story counts once per distinct source among its items.
- `feedback_examples() -> list[tuple[dict[str, Any], int]]`: one entry per story with `feedback != 0` that has items, newest thumb first (`feedback_at` DESC, then story id DESC). The label is the story's -1 or 1.
  - The features describe the story's **lead item**: highest stored `signal`, then lowest id (the same rule as `recompute_story_score`, computed on the fly so it works before a rescore).
  - Features: `{"signal": int | None, "junk": bool, "category": str, "trust": str, "source_kind": str}`. `signal`, `junk` and `category` come from the lead item's verdict (`judge_json`; `signal` is the judge's raw 0–10, not the weighted stored signal). An unjudged lead (`review_failed`) gives `signal=None`, `junk=False` and the story's category. `trust` and `source_kind` are the lead item's source's enum values.
  - These are exactly the keys step 4's `taste.featurize` reads (`signal`, `junk`, `category`, `trust`); it ignores `source_kind`.
- `backup(dest_path: str | Path) -> None`: an online SQLite backup to a **new** file (`FileExistsError` if the file exists).

**Module constants and helpers in `store/store.py`:** `NO_TEXT`, `STORY_SOURCE_BONUS`, `MAX_REVIEW_ATTEMPTS`, `MAX_TEXT_ATTEMPTS`, `MIN_WEIGHT`, `MAX_WEIGHT`, `ITEM_FIELDS`, `ConfigValue = str | int | float | bool | list[str]`, and `basic_canonical_url(url) -> str`. That function lower-cases the scheme and host and drops `www.`, default ports, the fragment and any trailing slash.

**`store/records.py`:**
- `SourceStats(id, kind, name, trust, enabled, weight, last_run_at, last_ok_at, last_error, last_count, last_duration_s, item_count)`
- `BriefingRecord(date, model, body, story_ids, created_at)`
- `RunRecord(id, trigger, started_at, finished_at, counts, cost_usd, error)`

**`store/db.py`:**
- `connect(path) -> aiosqlite.Connection`
- `migration_files(directory=MIGRATIONS_DIR) -> list[tuple[int, Path]]`
- `apply_migrations(conn, directory=MIGRATIONS_DIR) -> list[int]`
- A new migration is `src/ai_newsdesk/store/migrations/NNNN_name.sql`.

**`config/sources.yaml` schema** (step 2 writes the seed file; `seed_sources_if_empty` reads it):

```yaml
sources:
  - kind: feed            # a SourceKind value
    name: Example Lab blog # unique per kind
    trust: primary        # a Trust value
    config: {url: "https://lab.example.com/feed.xml"}   # any SourceConfig.config mapping
    enabled: true         # optional, default true
    weight: 1.0           # optional, default 1.0
```

**`secrets_box.py`:**
- `SecretsBox(secrets_key: str)`, with `.encrypt(plaintext: str) -> str` and `.decrypt(token: str) -> str`. `decrypt` raises `SecretsBoxError` and never includes the token. `repr` is redacted.
- `derive_fernet_key(secrets_key) -> bytes` uses HKDF-SHA256 with `info=b"ai-newsdesk/secrets-box/v1"`.

**`runtime_settings.py`:**
- `RuntimeSettings`: the contract fields and defaults, `extra="forbid"`, `validate_assignment=True`.
- Constants: `RUNS_PER_DAY_CHOICES = (1, 2, 4, 6, 12, 24)` and `DEFAULT_MODEL = "claude-haiku-5-5"`.
- Each field is stored under its own name as a settings key.
- Functions:
  - `async load_runtime_settings(store) -> RuntimeSettings`: invalid stored values fall back to the default, with a warning that names only the key.
  - `async save_runtime_settings(store, rs) -> None`
  - `async update_runtime_settings(store, **changes) -> RuntimeSettings`: validates everything, saves nothing on error, and raises `ValidationError`.
- `ApiKeyName = Literal["anthropic", "research_engine", "typesafe"]` and `API_KEY_NAMES`.
- `ApiKeys(store, settings: Settings, box: SecretsBox | None = None)`:
  - `box` defaults to `SecretsBox(settings.secrets_key)`.
  - `async set_api_key(name, value, at) -> None`: strips the value; empty raises `ValueError`.
  - `async clear_api_key(name) -> None`
  - `async api_key_status(name) -> ApiKeyStatus`
  - `async resolve_api_key(name) -> str`: UI value, then env, then `""`. A UI value that can't be decrypted falls back to env, with a warning.
- Keys are stored as `secret.<name>` → `{"token": <fernet>, "updated_at": <iso>}`.
- `ApiKeyStatus(name, set_in_ui: bool, set_in_env: bool, updated_at: datetime | None)`, with property `is_set`.
- Model names aren't checked against `pricing.yaml` here. The Settings page (step 5) offers only `Pricing.models(...)`.

**`pricing.py`:**
- `Pricing(models: dict[str, ModelPrice], as_of: str)`
- `Pricing.load(path=DEFAULT_PRICING_PATH)`, where `DEFAULT_PRICING_PATH = Path("config/pricing.yaml")`.
- `.price(model) -> ModelPrice`
- `.models(provider: str | None = None) -> list[str]`: sorted.
- `.cost(model, in_tokens, out_tokens, *, cache_read_tokens=0, cache_write_tokens=0) -> float`
- `.usage_cost(usage: Usage) -> float`
- `.as_of`
- `ModelPrice(provider: Literal["anthropic", "typesafe"], input_per_mtok, output_per_mtok)`
- `UnknownModelError(KeyError)`
- Cache multipliers: `CACHE_WRITE_MULTIPLIER = 1.25` and `CACHE_READ_MULTIPLIER = 0.10`, as multiples of the input price. `in_tokens` excludes cached tokens, as the Anthropic API reports them.

**Test fixture:** `store` (in `tests/conftest.py`) is a migrated `Store` on `db_path`, using the `clock` fixture, and it is closed after the test.

---

### Task 5: Database connection and migrations

**Goal:** Open SQLite with WAL, foreign keys and a busy timeout, and apply numbered SQL migrations at start-up, each atomically. `0001_init.sql` creates the spec's tables, adjusted for the Haiku judge.

**Files:**
- Create: `src/ai_newsdesk/store/__init__.py`, `src/ai_newsdesk/store/db.py`, `src/ai_newsdesk/store/migrations/0001_init.sql`
- Test: `tests/test_db.py`

**Acceptance Criteria:**
- [ ] `connect(path)` creates the parent directory. `PRAGMA journal_mode` returns `wal` and `PRAGMA foreign_keys` returns `1`.
- [ ] `apply_migrations` returns `[1]` the first time and `[]` the second time.
- [ ] Tables `sources`, `items`, `stories`, `briefings`, `runs`, `spend`, `settings` and `schema_migrations` exist.
  - `items` has `judge_json`, `signal`, `category`, `story_id`, `body_excerpt`, `hn_points`, `canonical_url`, `review_attempts` and `last_review_at`.
  - `stories` has `summary`, `why_it_matters`, `score`, `feedback` and `needs_text`.
  - `spend` has `provider`, `purpose` and `cost_usd`.
  - `settings` is exactly `(key, value_json)`.
- [ ] Inserting an item with an unknown `source_id` raises `IntegrityError`.
- [ ] If a migration fails partway, it leaves no trace: no table from it, no `schema_migrations` row, no open transaction. Earlier migrations stay applied.
- [ ] A badly named `.sql` file (`1_bad.sql`) raises `ValueError` naming the `NNNN_name.sql` pattern.

**Verify:** `uv run pytest tests/test_db.py -v` → `5 passed`

**Steps:**

- [ ] **Step 1: Create the branch.**

```bash
git switch main && git pull --ff-only && git switch -c step-1-store
```

- [ ] **Step 2: Write the failing tests** in `tests/test_db.py`.

```python
from pathlib import Path

import aiosqlite
import pytest

from ai_newsdesk.store.db import apply_migrations, connect, migration_files

TABLES = {"sources", "items", "stories", "briefings", "runs", "spend", "settings"}


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row["name"] for row in await cur.fetchall()}


async def test_connect_sets_pragmas_and_creates_parent(db_path: str) -> None:
    conn = await connect(db_path)
    try:
        assert Path(db_path).parent.is_dir()
        async with conn.execute("PRAGMA journal_mode") as cur:
            assert (await cur.fetchone())[0] == "wal"  # type: ignore[index]
        async with conn.execute("PRAGMA foreign_keys") as cur:
            assert (await cur.fetchone())[0] == 1  # type: ignore[index]
    finally:
        await conn.close()


async def test_migrations_create_schema_once(db_path: str) -> None:
    conn = await connect(db_path)
    try:
        assert await apply_migrations(conn) == [1]
        assert await apply_migrations(conn) == []
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            names = {row[0] for row in await cur.fetchall()}
        assert names >= TABLES | {"schema_migrations"}
        assert {
            "judge_json",
            "signal",
            "category",
            "story_id",
            "body_excerpt",
            "hn_points",
            "canonical_url",
            "review_attempts",
            "last_review_at",
        } <= await _columns(conn, "items")
        assert {"summary", "why_it_matters", "score", "feedback", "needs_text"} <= await _columns(
            conn, "stories"
        )
        assert {"provider", "purpose", "cost_usd"} <= await _columns(conn, "spend")
        assert await _columns(conn, "settings") == {"key", "value_json"}
    finally:
        await conn.close()


async def test_foreign_keys_enforced(db_path: str) -> None:
    conn = await connect(db_path)
    try:
        await apply_migrations(conn)
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO items (source_id, url, canonical_url, title, fetched_at)"
                " VALUES (999, 'u', 'u', 't', '2026-10-14T00:00:00.000000+00:00')"
            )
    finally:
        await conn.close()


async def test_failing_migration_rolls_back(db_path: str, tmp_path: Path) -> None:
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (mig / "0002_bad.sql").write_text("CREATE TABLE b (x INTEGER);\nTHIS IS NOT SQL;")
    conn = await connect(db_path)
    try:
        with pytest.raises(aiosqlite.OperationalError):
            await apply_migrations(conn, mig)
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            names = {row[0] for row in await cur.fetchall()}
        assert "a" in names
        assert "b" not in names
        async with conn.execute("SELECT version FROM schema_migrations") as cur:
            assert [row[0] for row in await cur.fetchall()] == [1]
        assert not conn.in_transaction
    finally:
        await conn.close()


def test_migration_files_are_numbered(tmp_path: Path) -> None:
    assert [v for v, _ in migration_files()] == [1]
    (tmp_path / "1_bad.sql").write_text("")
    with pytest.raises(ValueError, match=r"NNNN_name\.sql"):
        migration_files(tmp_path)
```

- [ ] **Step 3: Run them.** `uv run pytest tests/test_db.py -v`. Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ai_newsdesk.store'`.

- [ ] **Step 4: Create `src/ai_newsdesk/store/__init__.py`.**

```python
"""SQLite storage: connection and migrations (db.py) and every query (store.py)."""
```

- [ ] **Step 5: Create `src/ai_newsdesk/store/migrations/0001_init.sql`.** It follows spec section 9, with additions for the Haiku judge and the pipeline:
  - `items`: `body_excerpt`, `hn_points`, `judge_json`, `signal`, `category`, `story_id`, plus `review_attempts` and `last_review_at`, which hold the one-retry rule and the daily review cap.
  - `stories`: `lead_item_id`, `needs_text`, `text_attempts` and `feedback_at`.
  - `sources`: run stats for the Sources page.
  - `spend.purpose` drops the spec's `cluster` value, because clustering is done inside the review call (ADR-0001).

```sql
-- V1 schema (spec section 9, adjusted for the Haiku judge, ADR-0001).
-- All timestamps are UTC ISO-8601 text with microseconds, so text order is time order.

CREATE TABLE sources (
    id              INTEGER PRIMARY KEY,
    kind            TEXT    NOT NULL
                    CHECK (kind IN ('feed', 'x_person', 'search', 'arxiv', 'github', 'hn')),
    name            TEXT    NOT NULL,
    config_json     TEXT    NOT NULL DEFAULT '{}',
    trust           TEXT    NOT NULL CHECK (trust IN ('primary', 'trusted', 'discovery')),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    weight          REAL    NOT NULL DEFAULT 1.0,
    last_run_at     TEXT,
    last_ok_at      TEXT,
    last_error      TEXT,
    last_count      INTEGER NOT NULL DEFAULT 0,
    last_duration_s REAL    NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    UNIQUE (kind, name)
);

CREATE TABLE stories (
    id              INTEGER PRIMARY KEY,
    headline        TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    score           REAL    NOT NULL DEFAULT 0,
    summary         TEXT,
    why_it_matters  TEXT,
    lead_item_id    INTEGER,
    needs_text      INTEGER NOT NULL DEFAULT 1 CHECK (needs_text IN (0, 1)),
    text_attempts   INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT    NOT NULL,
    last_updated_at TEXT    NOT NULL,
    feedback        INTEGER NOT NULL DEFAULT 0 CHECK (feedback IN (-1, 0, 1)),
    feedback_at     TEXT
);
CREATE INDEX ix_stories_last_updated ON stories (last_updated_at);

CREATE TABLE items (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    url             TEXT    NOT NULL,
    canonical_url   TEXT    NOT NULL UNIQUE,
    title           TEXT    NOT NULL,
    author          TEXT,
    published_at    TEXT,
    fetched_at      TEXT    NOT NULL,
    snippet         TEXT    NOT NULL DEFAULT '',
    body_excerpt    TEXT,
    hn_points       INTEGER,
    heuristic_score REAL    NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'dropped', 'queued', 'reviewed', 'review_failed',
                                      'expired')),
    review_attempts INTEGER NOT NULL DEFAULT 0,
    last_review_at  TEXT,
    judge_json      TEXT,
    signal          REAL,
    category        TEXT,
    story_id        INTEGER REFERENCES stories (id) ON DELETE SET NULL
);
CREATE INDEX ix_items_status ON items (status, heuristic_score);
CREATE INDEX ix_items_fetched ON items (fetched_at);
CREATE INDEX ix_items_story ON items (story_id);
CREATE INDEX ix_items_last_review ON items (last_review_at);

CREATE TABLE briefings (
    id             INTEGER PRIMARY KEY,
    date           TEXT NOT NULL UNIQUE,
    model          TEXT NOT NULL,
    body           TEXT NOT NULL,
    story_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL
);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    trigger     TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}',
    cost_usd    REAL NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE spend (
    id                 INTEGER PRIMARY KEY,
    at                 TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    provider           TEXT    NOT NULL CHECK (provider IN ('anthropic', 'typesafe')),
    purpose            TEXT    NOT NULL CHECK (purpose IN ('review', 'write', 'briefing')),
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL    NOT NULL
);
CREATE INDEX ix_spend_at ON spend (at);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
```

- [ ] **Step 6: Implement `src/ai_newsdesk/store/db.py`.**

```python
"""SQLite connection and numbered SQL migrations (V1-S1).

One aiosqlite connection per process, in autocommit mode (`isolation_level=None`): every
write transaction is opened explicitly with BEGIN IMMEDIATE by the Store.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


async def connect(path: str) -> aiosqlite.Connection:
    """Open `path` (parent directory created) with WAL, foreign keys and a busy timeout."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """(version, path) for every `NNNN_name.sql` in `directory`, in version order."""
    found: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        if path.suffix != ".sql":
            continue
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise ValueError(f"bad migration file name: {path.name} (want NNNN_name.sql)")
        found.append((int(match.group(1)), path))
    found.sort()
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError("two migration files share a version number")
    return found


async def applied_versions(conn: aiosqlite.Connection) -> set[int]:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    async with conn.execute("SELECT version FROM schema_migrations") as cur:
        return {row[0] for row in await cur.fetchall()}


async def apply_migrations(
    conn: aiosqlite.Connection, directory: Path = MIGRATIONS_DIR
) -> list[int]:
    """Apply each not-yet-applied migration in its own transaction; return the versions applied.

    A failing migration is rolled back completely and the error re-raised, so the database is
    left at the last good version.
    """
    done = await applied_versions(conn)
    applied: list[int] = []
    for version, path in migration_files(directory):
        if version in done:
            continue
        sql = path.read_text()
        try:
            # executescript runs BEGIN plus the file; the transaction stays open so the
            # bookkeeping row commits atomically with the schema change.
            await conn.executescript("BEGIN IMMEDIATE;\n" + sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, path.name, datetime.now(UTC).isoformat(timespec="microseconds")),
            )
            await conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
```

- [ ] **Step 7: Run the tests.** `uv run pytest tests/test_db.py -v`. Expected: `5 passed`. Then run `make test`. Expected: green, `47 passed`.

- [ ] **Step 8: Check that the migration ships in the wheel.** Run `uv build --wheel -o /tmp/ai-newsdesk-wheel && unzip -l /tmp/ai-newsdesk-wheel/*.whl | grep 0001_init.sql`. Expected: one line, `ai_newsdesk/store/migrations/0001_init.sql`.

- [ ] **Step 9: Commit.**

```bash
git add src/ai_newsdesk/store/__init__.py src/ai_newsdesk/store/db.py \
  src/ai_newsdesk/store/migrations/0001_init.sql tests/test_db.py
git commit -m "feat(store): SQLite WAL connection and numbered migrations (V1-S1)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `Store`: every contract query

**Goal:** Implement every `Store` method named in the master contract, plus the Contract additions. Every SQL query in the app lives here, and each method is atomic under one lock.

**Files:**
- Create: `src/ai_newsdesk/store/records.py`, `src/ai_newsdesk/store/store.py`
- Modify: `src/ai_newsdesk/store/__init__.py` (re-export `Store`), `tests/conftest.py` (add the `store` fixture)
- Test: `tests/test_store_items.py`, `tests/test_store_stories.py`, `tests/test_store_ledger.py`

**Acceptance Criteria:**
- [ ] Calling a method before `init()` raises `RuntimeError` naming `init`. Calling `init()` twice is harmless. `ai_newsdesk.store.Store` is `ai_newsdesk.store.store.Store`.
- [ ] `seed_sources_if_empty` loads the YAML once, then returns `0`. Disabled seeds are stored with `enabled=False`.
- [ ] `upsert_source` without `weight` keeps the stored weight on update. `set_source_weight` stores a weight in 0.3–2.0, and raises `ValueError` outside it and `KeyError` for an unknown source.
- [ ] `add_candidates` stores `https://www.lab.test/post/` and `https://lab.test/post#comments` once. Re-adding returns `[]`, and unknown source IDs are skipped.
- [ ] `items_for_enrichment` returns `(item_id, url, source kind, snippet)` for the known ids in id order, and `[]` for `[]`.
- [ ] `queued_review_inputs` orders by heuristic score and picks text in the order `body_excerpt`, `snippet`, then `"(no text, title only)"`. It returns no story candidates and ignores items older than 48 h. A `review_failed` item comes back exactly once more.
- [ ] `recompute_story_score` gives a single-source story with signal 6 a score of `6.0`. Adding a second source adds `0.5`. A better new lead flags the story for writing again; a weaker extra item does not.
- [ ] `today_stories(now, threshold=4)` leaves out stories below 4 and stories older than 24 h. It returns `StorySource`s from both sources, best signal first, and the stored feedback.
- [ ] `today_stories(now, threshold=4)` also returns a score-0 story whose item is `review_failed`, and stops returning it once that item is reviewed below the threshold; a stale one stays out.
- [ ] `month_spend` follows Europe/London months. Rows at 30 Sep 23:30 UTC (1 Oct in BST) and 31 Oct 23:59 UTC give $2.25 for October. The same data in UTC gives $0.25. December rolls over to January.
- [ ] `spend_by_day` returns every day from the 1st to today, with zero days included.
- [ ] `source_feedback` counts each rated story once per distinct source.
- [ ] `feedback_examples` returns one `(lead-item features, ±1)` per rated story, newest thumb first; a cleared thumb drops out. `story_features` returns the same dict for stories with items (an unjudged lead gives `signal=None` and the story's category) and leaves out stories without items.
- [ ] Fifty concurrent `set_setting` calls all persist. `backup` writes a readable copy and refuses to overwrite an existing file.

**Verify:** `uv run pytest tests/test_store_items.py tests/test_store_stories.py tests/test_store_ledger.py -v` → `32 passed`

**Steps:**

- [ ] **Step 1: Add the `store` fixture.** Replace `tests/conftest.py` with:

```python
"""Shared pytest fixtures."""

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.config import Settings, get_settings
from ai_newsdesk.store.store import Store
from tests.fakes import utc

ROOT = Path(__file__).resolve().parent.parent

# Fake secrets, built at runtime (nothing for gitleaks to flag) and long enough for the
# 32-character minimum.
TEST_SECRETS_KEY = "test-secrets-key-" + "0" * 32
TEST_SESSION_SECRET = "test-session-secret-" + "1" * 32
TEST_NOW = utc(2026, 10, 14, 12, 0)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """No Settings variable leaks in from the developer's shell (TZ is often set)."""
    monkeypatch.chdir(ROOT)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def settings_factory(clean_env: pytest.MonkeyPatch) -> Callable[..., Settings]:
    """Build a Settings from a clean environment plus the two required test secrets.

    Keyword overrides use field names (``tz="UTC"``); they are set as environment variables
    so they go through the same parsing and validation as production.
    """

    def make(**overrides: str) -> Settings:
        env = {"SECRETS_KEY": TEST_SECRETS_KEY, "SESSION_SECRET": TEST_SESSION_SECRET}
        env.update({k.upper(): v for k, v in overrides.items()})
        for k, v in env.items():
            clean_env.setenv(k, v)
        return Settings()  # type: ignore[call-arg]

    return make


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """A fresh SQLite path in pytest's tmp dir (the file does not exist yet)."""
    return str(tmp_path / "data" / "test.sqlite")


@pytest.fixture
def now() -> datetime:
    return TEST_NOW


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(TEST_NOW)


@pytest.fixture
async def store(db_path: str, clock: FixedClock) -> AsyncIterator[Store]:
    """A migrated Store on a fresh file, using the shared FixedClock."""
    s = Store(db_path, clock=clock)
    await s.init()
    try:
        yield s
    finally:
        await s.close()
```

- [ ] **Step 2: Write the failing tests.** `tests/test_store_items.py`:

```python
"""Store: lifecycle, sources and items (Task 6, part 1)."""

from datetime import timedelta
from pathlib import Path

import pytest

import ai_newsdesk.store as store_package
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import (
    Category,
    ItemStatus,
    SourceKind,
    SourceResult,
    Trust,
    Verdict,
)
from ai_newsdesk.store.store import NO_TEXT, Store, basic_canonical_url
from tests.fakes import make_candidate, utc

NOW = utc(2026, 10, 14, 12, 0)


async def _source(store: Store, name: str = "Lab", trust: Trust = Trust.PRIMARY) -> int:
    return await store.upsert_source(SourceKind.FEED, name, {"url": f"https://{name}.test"}, trust)


def test_package_reexports_store() -> None:
    assert store_package.Store is Store


async def test_init_is_idempotent_and_needed(db_path: str) -> None:
    s = Store(db_path)
    with pytest.raises(RuntimeError, match="init"):
        await s.list_sources()
    await s.init()
    await s.init()
    assert await s.list_sources() == []
    await s.close()


async def test_seed_sources_only_when_empty(store: Store, tmp_path: Path) -> None:
    seed = tmp_path / "sources.yaml"
    seed.write_text(
        "sources:\n"
        "  - {kind: feed, name: Lab blog, trust: primary, config: {url: 'https://lab.test/rss'}}\n"
        "  - kind: x_person\n"
        "    name: A Researcher\n"
        "    trust: trusted\n"
        "    config: {handle: researcher}\n"
        "    enabled: false\n"
    )
    assert await store.seed_sources_if_empty(seed) == 2
    assert await store.seed_sources_if_empty(seed) == 0
    enabled = await store.list_sources()
    assert [s.name for s in enabled] == ["Lab blog"]
    everything = await store.list_sources(enabled_only=False)
    person = next(s for s in everything if s.kind is SourceKind.X_PERSON)
    assert person.config == {"handle": "researcher"}
    assert (person.trust, person.enabled, person.weight) == (Trust.TRUSTED, False, 1.0)


async def test_upsert_get_delete_source(store: Store) -> None:
    sid = await _source(store)
    assert await _source(store) == sid  # same (kind, name) updates in place
    same = await store.upsert_source(
        SourceKind.FEED, "Lab renamed", {"url": "u"}, Trust.TRUSTED, weight=1.5, source_id=sid
    )
    assert same == sid
    got = await store.get_source(sid)
    assert got is not None
    assert (got.name, got.trust, got.weight) == ("Lab renamed", Trust.TRUSTED, 1.5)
    # no weight given: an update by id or by (kind, name) keeps the learned weight
    await store.upsert_source(
        SourceKind.FEED, "Lab renamed", {"url": "u2"}, Trust.TRUSTED, enabled=False, source_id=sid
    )
    await store.upsert_source(SourceKind.FEED, "Lab renamed", {"url": "u3"}, Trust.TRUSTED)
    kept = await store.get_source(sid)
    assert kept is not None
    assert (kept.config, kept.enabled, kept.weight) == ({"url": "u3"}, True, 1.5)
    with pytest.raises(ValueError, match="weight"):
        await store.upsert_source(SourceKind.FEED, "x", {}, Trust.PRIMARY, weight=2.5)
    with pytest.raises(KeyError):
        await store.upsert_source(SourceKind.FEED, "y", {}, Trust.PRIMARY, source_id=999)
    assert await store.delete_source(sid) is True
    assert await store.delete_source(sid) is False
    assert await store.get_source(sid) is None


async def test_set_source_weight(store: Store) -> None:
    sid = await _source(store)
    await store.set_source_weight(sid, 0.3)
    got = await store.get_source(sid)
    assert got is not None and got.weight == 0.3
    for bad in (0.29, 2.01):
        with pytest.raises(ValueError, match="weight"):
            await store.set_source_weight(sid, bad)
    with pytest.raises(KeyError):
        await store.set_source_weight(999, 1.0)


async def test_record_source_result_and_stats(store: Store) -> None:
    sid = await _source(store)
    ok = SourceResult(source_id=sid, candidates=[make_candidate(sid)], duration_s=1.5)
    await store.record_source_result(ok, NOW)
    await store.add_candidates([make_candidate(sid)], NOW)
    await store.record_source_result(SourceResult(source_id=sid, error="timeout"), NOW)
    [stats] = await store.source_stats()
    assert stats.last_ok_at == NOW
    assert stats.last_run_at == NOW
    assert stats.last_error == "timeout"
    assert (stats.last_count, stats.item_count) == (0, 1)


def test_basic_canonical_url() -> None:
    assert (
        basic_canonical_url("HTTPS://WWW.Example.com:443/a/b/?q=1#frag")
        == "https://example.com/a/b?q=1"
    )
    assert basic_canonical_url("http://example.com:8080/") == "http://example.com:8080"
    url = "https://example.com/x?y=2"
    assert basic_canonical_url(basic_canonical_url(url)) == basic_canonical_url(url)


async def test_add_candidates_dedupes_on_canonical_url(store: Store) -> None:
    sid = await _source(store)
    first = await store.add_candidates(
        [
            make_candidate(sid, url="https://www.lab.test/post/"),
            make_candidate(sid, url="https://lab.test/post#comments"),  # same canonical
            make_candidate(sid, url="https://lab.test/other", hn_points=150),
            make_candidate(999, url="https://lab.test/orphan"),  # unknown source: skipped
        ],
        NOW,
    )
    assert len(first) == 2
    again = await store.add_candidates([make_candidate(sid, url="https://lab.test/post")], NOW)
    assert again == []
    titles = await store.recent_titles(24)
    assert [t[0] for t in titles] == first
    assert all(story is None for _, _, story in titles)


async def test_recent_titles_uses_store_clock(store: Store, clock: FixedClock) -> None:
    sid = await _source(store)
    [old] = await store.add_candidates([make_candidate(sid, url="https://a.test/1")], NOW)
    clock.advance(timedelta(hours=73))
    [new] = await store.add_candidates([make_candidate(sid, url="https://a.test/2")], clock.now())
    assert [t[0] for t in await store.recent_titles(72)] == [new]
    assert {t[0] for t in await store.recent_titles(100)} == {old, new}


async def test_items_for_enrichment(store: Store) -> None:
    feed = await _source(store)
    hn = await store.upsert_source(SourceKind.HN, "HN", {}, Trust.DISCOVERY)
    a, b = await store.add_candidates(
        [
            make_candidate(feed, url="https://a.test/1", snippet="Short."),
            make_candidate(hn, url="https://b.test/2", snippet=""),
        ],
        NOW,
    )
    assert await store.items_for_enrichment([b, 999, a]) == [
        (a, "https://a.test/1", SourceKind.FEED, "Short."),
        (b, "https://b.test/2", SourceKind.HN, ""),
    ]
    assert await store.items_for_enrichment([]) == []


async def test_set_item_fields_whitelist(store: Store) -> None:
    sid = await _source(store)
    [item] = await store.add_candidates([make_candidate(sid)], NOW)
    await store.set_item_fields(item, heuristic_score=2.5, status=ItemStatus.QUEUED)
    with pytest.raises(ValueError, match="not settable"):
        await store.set_item_fields(item, canonical_url="x")
    with pytest.raises(ValueError, match="at least one"):
        await store.set_item_fields(item)
    with pytest.raises(KeyError):
        await store.set_item_fields(999, status=ItemStatus.DROPPED)


async def test_queued_review_inputs_text_order_and_window(store: Store) -> None:
    sid = await _source(store, "Lab", Trust.PRIMARY)
    a, b, c, d = await store.add_candidates(
        [
            make_candidate(sid, url="https://a.test/1", title="A", snippet="snip A"),
            make_candidate(sid, url="https://a.test/2", title="B", snippet="snip B"),
            make_candidate(sid, url="https://a.test/3", title="C", snippet=""),
            make_candidate(sid, url="https://a.test/4", title="D", snippet="new"),
        ],
        NOW - timedelta(hours=1),
    )
    for item, score in ((a, 1.0), (b, 3.0), (c, 2.0)):
        await store.set_item_fields(item, status=ItemStatus.QUEUED, heuristic_score=score)
    await store.set_item_fields(a, body_excerpt="Full article body")
    inputs = await store.queued_review_inputs(10, NOW)
    assert [i.item_id for i in inputs] == [b, c, a]  # d is still `new`
    by_id = {i.item_id: i for i in inputs}
    assert by_id[a].text == "Full article body"
    assert by_id[b].text == "snip B"
    assert by_id[c].text == NO_TEXT
    assert by_id[b].source_name == "Lab"
    assert (by_id[b].source_kind, by_id[b].trust) == (SourceKind.FEED, Trust.PRIMARY)
    assert by_id[b].story_candidates == []
    assert [i.item_id for i in await store.queued_review_inputs(1, NOW)] == [b]
    assert await store.queued_review_inputs(10, NOW + timedelta(hours=48)) == []
    assert d not in by_id


async def test_review_failed_gets_one_retry(store: Store) -> None:
    sid = await _source(store)
    [item] = await store.add_candidates([make_candidate(sid)], NOW)
    await store.mark_items([item], ItemStatus.QUEUED)
    assert await store.mark_items([item], ItemStatus.REVIEW_FAILED) == 1
    assert [i.item_id for i in await store.queued_review_inputs(10, NOW)] == [item]
    await store.mark_items([item], ItemStatus.REVIEW_FAILED)
    assert await store.queued_review_inputs(10, NOW) == []
    assert await store.mark_items([], ItemStatus.DROPPED) == 0


async def test_expire_old_queued(store: Store) -> None:
    sid = await _source(store)
    [old] = await store.add_candidates([make_candidate(sid, url="https://a.test/o")], NOW)
    later = NOW + timedelta(hours=49)
    [fresh] = await store.add_candidates([make_candidate(sid, url="https://a.test/f")], later)
    await store.mark_items([old, fresh], ItemStatus.QUEUED)
    assert await store.expire_old_queued(later) == 1
    assert [i.item_id for i in await store.queued_review_inputs(10, later)] == [fresh]


async def test_save_verdict_and_reviewed_count(store: Store) -> None:
    sid = await _source(store)
    a, b = await store.add_candidates(
        [make_candidate(sid, url="https://a.test/1"), make_candidate(sid, url="https://a.test/2")],
        NOW,
    )
    verdict = Verdict(item_id=a, signal=7, junk=False, category=Category.RESEARCH)
    await store.save_verdict(verdict, 8.4)
    await store.mark_items([b], ItemStatus.REVIEW_FAILED)
    assert await store.reviewed_count_today(NOW, "Europe/London") == 2
    assert await store.reviewed_count_today(NOW + timedelta(days=1), "Europe/London") == 0
    with pytest.raises(KeyError):
        await store.save_verdict(verdict.model_copy(update={"item_id": 999}), 1.0)
```

`tests/test_store_stories.py`:

```python
"""Store: stories, writing and briefings (Task 6, part 2)."""

from datetime import date, timedelta

import pytest

from ai_newsdesk.models import Category, ItemStatus, SourceKind, StoryText, Trust, Verdict
from ai_newsdesk.store.store import NO_TEXT, STORY_SOURCE_BONUS, Store
from tests.fakes import make_candidate, utc

NOW = utc(2026, 10, 14, 12, 0)


async def _judged_item(
    store: Store, source_id: int, url: str, signal: float, title: str = "T", snippet: str = "s"
) -> int:
    [item] = await store.add_candidates(
        [make_candidate(source_id, url=url, title=title, snippet=snippet)], NOW
    )
    verdict = Verdict(item_id=item, signal=int(signal), junk=False, category=Category.RESEARCH)
    await store.save_verdict(verdict, signal)
    return item


async def _two_sources(store: Store) -> tuple[int, int]:
    a = await store.upsert_source(SourceKind.FEED, "Lab", {"url": "u1"}, Trust.PRIMARY)
    b = await store.upsert_source(SourceKind.HN, "HN", {}, Trust.DISCOVERY)
    return a, b


async def test_create_open_and_attach(store: Store) -> None:
    lab, _ = await _two_sources(store)
    old = await store.create_story("Old", Category.OTHER, NOW - timedelta(hours=80))
    new = await store.create_story("New", Category.RESEARCH, NOW - timedelta(hours=1))
    assert [s.id for s in await store.open_stories(NOW)] == [new]
    assert [s.id for s in await store.open_stories(NOW, hours=100)] == [new, old]
    item = await _judged_item(store, lab, "https://a.test/1", 7)
    await store.attach_item(item, old, NOW)
    assert [s.id for s in await store.open_stories(NOW)] == [old, new]  # touched
    assert (await store.recent_titles(24))[0][2] == old
    with pytest.raises(KeyError):
        await store.attach_item(item, 999, NOW)
    with pytest.raises(KeyError):
        await store.attach_item(999, new, NOW)


async def test_score_lead_and_text_flag(store: Store) -> None:
    lab, hn = await _two_sources(store)
    story = await store.create_story("Model X", Category.MODEL_RELEASE, NOW)
    first = await _judged_item(store, lab, "https://a.test/1", 6, title="Lab post", snippet="")
    await store.set_item_fields(first, body_excerpt="Lab body")
    await store.attach_item(first, story, NOW)
    assert await store.recompute_story_score(story) == 6.0
    [needs] = await store.stories_needing_text(10, include_why=True)
    assert needs.story_id == story
    assert needs.texts == ["Lab post\n\nLab body"]
    assert needs.include_why is True

    await store.save_story_text(StoryText(story_id=story, summary="S1", why_it_matters="W1"))
    assert await store.stories_needing_text(10, include_why=True) == []

    # a second source with a lower signal: bonus only, lead unchanged, no rewrite
    second = await _judged_item(store, hn, "https://b.test/1", 5, title="HN thread", snippet="")
    await store.attach_item(second, story, NOW)
    assert await store.recompute_story_score(story) == 6.0 + STORY_SOURCE_BONUS
    assert await store.stories_needing_text(10, include_why=False) == []

    # a better item becomes lead: story needs text again, best two texts in signal order
    third = await _judged_item(store, hn, "https://b.test/2", 9, title="Better", snippet="Snip")
    await store.attach_item(third, story, NOW)
    assert await store.recompute_story_score(story) == 9.0 + STORY_SOURCE_BONUS
    [again] = await store.stories_needing_text(10, include_why=False)
    assert again.texts == ["Better\n\nSnip", "Lab post\n\nLab body"]
    with pytest.raises(KeyError):
        await store.recompute_story_score(999)


async def test_text_failures_get_one_retry(store: Store) -> None:
    lab, _ = await _two_sources(store)
    story = await store.create_story("H", Category.OTHER, NOW)
    item = await _judged_item(store, lab, "https://a.test/1", 5, snippet="")
    await store.attach_item(item, story, NOW)
    await store.recompute_story_score(story)
    [w] = await store.stories_needing_text(10, include_why=True)
    assert w.texts == [f"T\n\n{NO_TEXT}"]
    await store.mark_text_failed([story])
    assert len(await store.stories_needing_text(10, include_why=True)) == 1
    await store.mark_text_failed([story])
    assert await store.stories_needing_text(10, include_why=True) == []


async def test_save_story_text_keeps_why_when_none(store: Store) -> None:
    lab, _ = await _two_sources(store)
    story = await store.create_story("H", Category.OTHER, NOW)
    item = await _judged_item(store, lab, "https://a.test/1", 8)
    await store.attach_item(item, story, NOW)
    await store.recompute_story_score(story)
    await store.save_story_text(StoryText(story_id=story, summary="S1", why_it_matters="W1"))
    await store.save_story_text(StoryText(story_id=story, summary="S2", why_it_matters=None))
    [s] = await store.today_stories(NOW, threshold=0)
    assert (s.summary, s.why_it_matters) == ("S2", "W1")
    with pytest.raises(KeyError):
        await store.save_story_text(StoryText(story_id=999, summary="x"))


async def test_today_stories_threshold_window_sources_feedback(store: Store) -> None:
    lab, hn = await _two_sources(store)
    strong = await store.create_story("Strong", Category.MODEL_RELEASE, NOW - timedelta(hours=2))
    weak = await store.create_story("Weak", Category.OPINION, NOW - timedelta(hours=2))
    stale = await store.create_story("Stale", Category.RESEARCH, NOW - timedelta(hours=30))
    for story, signal, url in ((strong, 8, "s1"), (weak, 3, "w1"), (stale, 9, "x1")):
        item = await _judged_item(store, lab, f"https://a.test/{url}", signal)
        await store.attach_item(item, story, NOW - timedelta(hours=2 if story != stale else 30))
    hn_item = await _judged_item(store, hn, "https://b.test/s2", 7, title="HN take")
    await store.attach_item(hn_item, strong, NOW - timedelta(hours=1))
    for story in (strong, weak, stale):
        await store.recompute_story_score(story)
    await store.set_feedback(strong, 1)

    stories = await store.today_stories(NOW, threshold=4)
    assert [s.id for s in stories] == [strong]
    top = stories[0]
    assert top.score == 8.0 + STORY_SOURCE_BONUS
    assert top.category is Category.MODEL_RELEASE
    assert top.feedback == 1
    assert top.last_updated_at == NOW - timedelta(hours=1)
    assert [(src.source_name, src.source_kind) for src in top.sources] == [
        ("Lab", SourceKind.FEED),
        ("HN", SourceKind.HN),
    ]
    assert top.sources[1].title == "HN take"
    assert [s.id for s in await store.today_stories(NOW, threshold=0)] == [strong, weak]


async def test_today_stories_shows_review_failed_whatever_score(store: Store) -> None:
    lab, _ = await _two_sources(store)
    story = await store.create_story("Mystery lab teases agent", Category.OTHER, NOW)
    stale = await store.create_story("Old mystery", Category.OTHER, NOW - timedelta(hours=30))
    for story_id, url, at in (
        (story, "https://a.test/m", NOW),
        (stale, "https://a.test/o", NOW - timedelta(hours=30)),
    ):
        [item] = await store.add_candidates([make_candidate(lab, url=url)], at)
        await store.mark_items([item], ItemStatus.REVIEW_FAILED)
        await store.attach_item(item, story_id, at)
        assert await store.recompute_story_score(story_id) == 0.0
    [shown] = await store.today_stories(NOW, threshold=4)
    assert shown.id == story
    [failed] = [src.item_id for src in shown.sources]
    # once the item gets a verdict below the threshold, the story is hidden like any other
    verdict = Verdict(item_id=failed, signal=2, junk=False, category=Category.OTHER)
    await store.save_verdict(verdict, 2.0)
    await store.recompute_story_score(story)
    assert await store.today_stories(NOW, threshold=4) == []


async def test_story_features_use_lead_item(store: Store) -> None:
    lab, hn = await _two_sources(store)
    judged = await store.create_story("Judged", Category.RESEARCH, NOW)
    weak = await _judged_item(store, lab, "https://a.test/1", 5)
    lead = await _judged_item(store, hn, "https://b.test/1", 8)
    for item in (weak, lead):
        await store.attach_item(item, judged, NOW)  # no rescore needed: the lead is computed
    unjudged = await store.create_story("Unjudged", Category.POLICY, NOW)
    [failed] = await store.add_candidates([make_candidate(lab, url="https://a.test/2")], NOW)
    await store.mark_items([failed], ItemStatus.REVIEW_FAILED)
    await store.attach_item(failed, unjudged, NOW)
    empty = await store.create_story("No items", Category.OTHER, NOW)
    assert await store.story_features([judged, unjudged, empty, 999]) == {
        judged: {
            "signal": 8,
            "junk": False,
            "category": "research",
            "trust": "discovery",
            "source_kind": "hn",
        },
        unjudged: {
            "signal": None,
            "junk": False,
            "category": "policy",
            "trust": "primary",
            "source_kind": "feed",
        },
    }
    assert await store.story_features([]) == {}


async def test_top_stories_for_briefing(store: Store) -> None:
    lab, _ = await _two_sources(store)
    written = await store.create_story("Written", Category.POLICY, NOW)
    unwritten = await store.create_story("Unwritten", Category.POLICY, NOW)
    for story, url in ((written, "https://a.test/w"), (unwritten, "https://a.test/u")):
        item = await _judged_item(store, lab, url, 7)
        await store.attach_item(item, story, NOW)
        await store.recompute_story_score(story)
    await store.save_story_text(StoryText(story_id=written, summary="Sum"))
    [b] = await store.top_stories_for_briefing(NOW)
    assert (b.story_id, b.headline, b.category, b.summary) == (
        written,
        "Written",
        Category.POLICY,
        "Sum",
    )
    assert b.url == "https://a.test/w"
    assert await store.top_stories_for_briefing(NOW + timedelta(hours=25)) == []


async def test_briefings_save_replace_and_list(store: Store) -> None:
    assert await store.latest_briefing() is None
    await store.save_briefing(date(2026, 10, 13), "claude-haiku-5-5", "Mon", [1], NOW)
    await store.save_briefing(date(2026, 10, 14), "claude-haiku-5-5", "Tue v1", [1, 2], NOW)
    await store.save_briefing(date(2026, 10, 14), "claude-haiku-5-5", "Tue v2", [2], NOW)
    got = await store.get_briefing(date(2026, 10, 14))
    assert got is not None
    assert (got.body, got.story_ids, got.created_at) == ("Tue v2", [2], NOW)
    latest = await store.latest_briefing()
    assert latest is not None and latest.date == date(2026, 10, 14)
    assert await store.briefing_dates() == [date(2026, 10, 14), date(2026, 10, 13)]
    assert await store.get_briefing(date(2026, 1, 1)) is None
```

`tests/test_store_ledger.py`:

```python
"""Store: spend, runs, settings, feedback and backup (Task 6, part 3)."""

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import Category, Purpose, SourceKind, Trust, Usage, Verdict
from ai_newsdesk.store.store import Store
from tests.fakes import make_candidate, utc

LONDON = "Europe/London"


def _usage(cost_tokens: int = 1000) -> Usage:
    return Usage(
        provider="anthropic",
        model="claude-haiku-5-5",
        purpose=Purpose.REVIEW,
        input_tokens=cost_tokens,
        output_tokens=100,
    )


async def test_month_spend_uses_london_month(store: Store) -> None:
    # 1 Oct 00:00 BST is 30 Sep 23:00 UTC; 1 Nov 00:00 GMT is 1 Nov 00:00 UTC.
    await store.add_spend(_usage(), 1.00, utc(2026, 9, 30, 22, 30))  # September in London
    await store.add_spend(_usage(), 2.00, utc(2026, 9, 30, 23, 30))  # October in London
    await store.add_spend(_usage(), 0.25, utc(2026, 10, 31, 23, 59))  # October (GMT)
    await store.add_spend(_usage(), 4.00, utc(2026, 11, 1, 0, 0))  # November
    oct_now = utc(2026, 10, 14, 12)
    assert await store.month_spend(oct_now, LONDON) == pytest.approx(2.25)
    assert await store.month_spend(utc(2026, 9, 15), LONDON) == pytest.approx(1.00)
    assert await store.month_spend(utc(2026, 11, 2), LONDON) == pytest.approx(4.00)
    # in UTC the 23:30 spend belongs to September
    assert await store.month_spend(oct_now, "UTC") == pytest.approx(0.25)


async def test_month_spend_december_rollover(store: Store) -> None:
    await store.add_spend(_usage(), 0.5, utc(2026, 12, 31, 23, 0))
    await store.add_spend(_usage(), 0.7, utc(2027, 1, 1, 0, 0))
    assert await store.month_spend(utc(2026, 12, 20), LONDON) == pytest.approx(0.5)
    assert await store.month_spend(utc(2027, 1, 5), LONDON) == pytest.approx(0.7)


async def test_spend_by_day_fills_every_day(store: Store) -> None:
    await store.add_spend(_usage(), 0.10, utc(2026, 10, 1, 9))
    await store.add_spend(_usage(), 0.05, utc(2026, 10, 2, 23, 30))  # 3 Oct 00:30 BST
    await store.add_spend(_usage(), 0.02, utc(2026, 10, 3, 8))
    days = await store.spend_by_day(utc(2026, 10, 3, 12), LONDON)
    assert days == [("2026-10-01", 0.10), ("2026-10-02", 0.0), ("2026-10-03", 0.07)]


async def test_runs(store: Store) -> None:
    t0 = utc(2026, 10, 14, 6)
    first = await store.start_run("schedule", t0)
    second = await store.start_run("manual", t0 + timedelta(hours=1))
    await store.finish_run(first, t0 + timedelta(minutes=3), {"collected": 40}, 0.012, None)
    runs = await store.last_runs(5)
    assert [r.id for r in runs] == [second, first]
    assert runs[0].finished_at is None
    assert (runs[1].counts, runs[1].cost_usd, runs[1].error) == ({"collected": 40}, 0.012, None)
    assert runs[1].trigger == "schedule"
    assert [r.id for r in await store.last_runs(1)] == [second]
    with pytest.raises(KeyError):
        await store.finish_run(999, t0, {}, 0.0, "x")


async def test_settings_json_roundtrip(store: Store) -> None:
    assert await store.get_setting("runs_per_day") is None
    await store.set_setting("runs_per_day", 6)
    await store.set_setting("blob", {"a": [1, 2], "b": None})
    assert await store.get_setting("runs_per_day") == 6
    assert await store.get_setting("blob") == {"a": [1, 2], "b": None}
    await store.set_setting("runs_per_day", None)
    assert await store.get_setting("runs_per_day") is None


async def test_feedback_tally_and_examples(store: Store, clock: FixedClock) -> None:
    now = utc(2026, 10, 14, 12)
    lab = await store.upsert_source(SourceKind.FEED, "Lab", {}, Trust.PRIMARY)
    hn = await store.upsert_source(SourceKind.HN, "HN", {}, Trust.DISCOVERY)
    liked = await store.create_story("Liked", Category.RESEARCH, now)
    disliked = await store.create_story("Disliked", Category.OPINION, now)
    for story, src, url, signal in (
        (liked, lab, "https://a.test/1", 8),
        (liked, lab, "https://a.test/2", 7),  # same source twice: still one vote
        (liked, hn, "https://b.test/1", 6),
        (disliked, hn, "https://b.test/2", 4),
    ):
        [item] = await store.add_candidates([make_candidate(src, url=url)], now)
        verdict = Verdict(item_id=item, signal=signal, junk=False, category=Category.RESEARCH)
        await store.save_verdict(verdict, float(signal))
        await store.attach_item(item, story, now)
    unrated = await store.create_story("Unrated", Category.OTHER, now)
    [item] = await store.add_candidates([make_candidate(lab, url="https://a.test/3")], now)
    await store.attach_item(item, unrated, now)
    await store.set_feedback(liked, 1)
    clock.advance(timedelta(minutes=5))  # the thumb on `disliked` is the newer one
    await store.set_feedback(disliked, -1)
    assert await store.source_feedback(30, now) == {lab: (1, 0), hn: (1, 1)}
    assert await store.source_feedback(30, now + timedelta(days=31)) == {}
    # one example per rated story: its lead item's features, newest thumb first
    assert await store.feedback_examples() == [
        (
            {
                "signal": 4,
                "junk": False,
                "category": "research",
                "trust": "discovery",
                "source_kind": "hn",
            },
            -1,
        ),
        (
            {
                "signal": 8,
                "junk": False,
                "category": "research",
                "trust": "primary",
                "source_kind": "feed",
            },
            1,
        ),
    ]
    with pytest.raises(ValueError, match="-1, 0 or 1"):
        await store.set_feedback(liked, 2)
    with pytest.raises(KeyError):
        await store.set_feedback(999, 1)
    await store.set_feedback(liked, 0)
    assert await store.source_feedback(30, now) == {hn: (0, 1)}
    assert [label for _, label in await store.feedback_examples()] == [-1]


async def test_concurrent_writes_are_serialised(store: Store) -> None:
    await asyncio.gather(*(store.set_setting(f"k{i}", i) for i in range(50)))
    values = await asyncio.gather(*(store.get_setting(f"k{i}") for i in range(50)))
    assert values == list(range(50))


async def test_backup_is_a_complete_copy(store: Store, tmp_path: Path) -> None:
    await store.set_setting("marker", "present")
    dest = tmp_path / "backups" / "copy.sqlite"
    await store.backup(dest)
    with sqlite3.connect(dest) as copy:
        row = copy.execute("SELECT value_json FROM settings WHERE key = 'marker'").fetchone()
    assert row == ('"present"',)
    with pytest.raises(FileExistsError):
        await store.backup(dest)
```

- [ ] **Step 3: Run them.** `uv run pytest tests/test_store_items.py -v`. Expected: FAIL with `ImportError while loading conftest ... ModuleNotFoundError: No module named 'ai_newsdesk.store.store'`.

- [ ] **Step 4: Implement `src/ai_newsdesk/store/records.py`.**

```python
"""Read models the Store returns that are not part of the shared pipeline models (V1-S1)."""

import datetime as dt

from pydantic import BaseModel, Field

from ai_newsdesk.models import SourceKind, Trust


class SourceStats(BaseModel):
    """One row of the Sources page."""

    id: int
    kind: SourceKind
    name: str
    trust: Trust
    enabled: bool
    weight: float
    last_run_at: dt.datetime | None
    last_ok_at: dt.datetime | None
    last_error: str | None
    last_count: int
    last_duration_s: float
    item_count: int


class BriefingRecord(BaseModel):
    date: dt.date
    model: str
    body: str
    story_ids: list[int] = Field(default_factory=list)
    created_at: dt.datetime


class RunRecord(BaseModel):
    id: int
    trigger: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    counts: dict[str, int] = Field(default_factory=dict)
    cost_usd: float
    error: str | None
```

- [ ] **Step 5: Implement `src/ai_newsdesk/store/store.py`.**

```python
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
```

- [ ] **Step 6: Re-export `Store` from the package.** Steps 3–5 import `from ai_newsdesk.store import Store`. Replace `src/ai_newsdesk/store/__init__.py` with:

```python
"""SQLite storage: connection and migrations (db.py) and every query (store.py)."""

from ai_newsdesk.store.store import Store

__all__ = ["Store"]
```

- [ ] **Step 7: Run the tests.** `uv run pytest tests/test_store_items.py tests/test_store_stories.py tests/test_store_ledger.py -v`. Expected: `32 passed`. Then run `make test`. Expected: green, `79 passed`.

- [ ] **Step 8: Commit.**

```bash
git add src/ai_newsdesk/store/__init__.py src/ai_newsdesk/store/records.py \
  src/ai_newsdesk/store/store.py tests/conftest.py \
  tests/test_store_items.py tests/test_store_stories.py tests/test_store_ledger.py
git commit -m "feat(store): Store queries for sources, items, stories, spend and runs (V1-S1)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Secrets box and runtime settings

**Goal:**
- Encrypt API keys at rest with a Fernet key derived from `SECRETS_KEY`.
- Give the app a typed, validated `RuntimeSettings` stored in the `settings` table.
- Resolve each API key from the UI value, then `.env`, then `""`.
- Never expose a key value in a repr, a log or the database.

**Key derivation choice: HKDF-SHA256.** `SECRETS_KEY` is already a high-entropy random secret: at least 32 characters, validated in step 0, generated with `openssl rand -hex 32`. HKDF is the standard way to turn high-entropy key material into a key. PBKDF2 exists to slow down guessing of *low*-entropy passwords, so here it would add start-up cost and no security. The versioned `info` label separates this key from any other use of the same secret.

**Files:**
- Create: `src/ai_newsdesk/secrets_box.py`, `src/ai_newsdesk/runtime_settings.py`
- Test: `tests/test_secrets_box.py`, `tests/test_runtime_settings.py`

**Acceptance Criteria:**
- [ ] Encrypt then decrypt returns the plaintext. The token never contains the plaintext, and two encryptions of the same value differ.
- [ ] A wrong key or a garbage token raises `SecretsBoxError`, whose message never contains the token and whose context is suppressed. `repr(SecretsBox(...))` is `SecretsBox(<redacted>)`.
- [ ] `RuntimeSettings()` dumps exactly the contract defaults.
- [ ] Each invalid value is refused: `runs_per_day=5`, `briefing_time` of `7:00` or `24:00`, negative cap or budget, provider `openai`, empty model, `show_threshold=11`, negative `hn_min_points`, unknown key.
- [ ] Settings survive save and load. A stored `show_threshold=99` loads as `4`, and the warning names the key.
- [ ] `update_runtime_settings` with one bad value saves nothing.
- [ ] `resolve_api_key` returns the env value, then the UI value once one is set, then the env value again after `clear_api_key`, and `""` when neither is set.
- [ ] After set, resolve, status and clear at DEBUG logging, the UI key and `SECRETS_KEY` appear in none of these: `repr(ApiKeys)`, `str(ApiKeys)`, `repr(status)`, `status.model_dump_json()` and the captured logs. The raw `secret.anthropic` row doesn't contain the plaintext either.
- [ ] With a different `SECRETS_KEY` (rotation), `resolve_api_key` falls back to env and logs "could not be decrypted" without the key.

**Verify:** `uv run pytest tests/test_secrets_box.py tests/test_runtime_settings.py -v` → `28 passed`

**Steps:**

- [ ] **Step 1: Write the failing secrets-box tests** in `tests/test_secrets_box.py`.

```python
import pytest

from ai_newsdesk.secrets_box import SecretsBox, SecretsBoxError, derive_fernet_key

KEY_A = "a" * 64
KEY_B = "b" * 64
SECRET = "sk-test-" + "z" * 40


def test_roundtrip_and_ciphertext_hides_plaintext() -> None:
    box = SecretsBox(KEY_A)
    token = box.encrypt(SECRET)
    assert SECRET not in token
    assert box.decrypt(token) == SECRET
    assert box.encrypt(SECRET) != token  # Fernet uses a fresh IV each time


def test_key_derivation_is_deterministic_and_key_specific() -> None:
    assert derive_fernet_key(KEY_A) == derive_fernet_key(KEY_A)
    assert derive_fernet_key(KEY_A) != derive_fernet_key(KEY_B)
    assert len(derive_fernet_key(KEY_A)) == 44  # urlsafe base64 of 32 bytes


def test_wrong_key_raises_clean_error() -> None:
    token = SecretsBox(KEY_A).encrypt(SECRET)
    with pytest.raises(SecretsBoxError) as exc:
        SecretsBox(KEY_B).decrypt(token)
    assert token not in str(exc.value)
    assert exc.value.__suppress_context__ is True
    with pytest.raises(SecretsBoxError):
        SecretsBox(KEY_A).decrypt("not-a-token")


def test_repr_redacted_and_empty_key_refused() -> None:
    assert repr(SecretsBox(KEY_A)) == "SecretsBox(<redacted>)"
    assert KEY_A not in repr(SecretsBox(KEY_A))
    with pytest.raises(ValueError, match="SECRETS_KEY"):
        SecretsBox("")
```

- [ ] **Step 2: Run them.** `uv run pytest tests/test_secrets_box.py -v`. Expected: FAIL with `ModuleNotFoundError: No module named 'ai_newsdesk.secrets_box'`.

- [ ] **Step 3: Implement `src/ai_newsdesk/secrets_box.py`.**

```python
"""Encrypt API keys saved on the Settings page (V1-S1).

The Fernet key is derived from SECRETS_KEY with HKDF-SHA256. SECRETS_KEY is already a
high-entropy random secret (at least 32 characters, `openssl rand -hex 32`), so a slow
password KDF such as PBKDF2 would add start-up cost and no security; HKDF is the standard
extractor for key material like this. The `info` label separates this key from any other
use of the same secret, and versions it.
"""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HKDF_INFO = b"ai-newsdesk/secrets-box/v1"


class SecretsBoxError(Exception):
    """A token could not be decrypted (wrong SECRETS_KEY or corrupted). Never holds data."""


def derive_fernet_key(secrets_key: str) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO)
    return base64.urlsafe_b64encode(hkdf.derive(secrets_key.encode()))


class SecretsBox:
    def __init__(self, secrets_key: str) -> None:
        if not secrets_key:
            raise ValueError("SecretsBox needs a non-empty SECRETS_KEY")
        self._fernet = Fernet(derive_fernet_key(secrets_key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            # `from None`: the cause would carry the token into tracebacks and logs.
            raise SecretsBoxError(
                "stored secret could not be decrypted (was SECRETS_KEY changed?)"
            ) from None

    def __repr__(self) -> str:
        return "SecretsBox(<redacted>)"
```

- [ ] **Step 4: Run them.** `uv run pytest tests/test_secrets_box.py -v`. Expected: `4 passed`.

- [ ] **Step 5: Write the failing runtime-settings tests** in `tests/test_runtime_settings.py`.

```python
import logging
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from ai_newsdesk.config import Settings
from ai_newsdesk.runtime_settings import (
    ApiKeys,
    RuntimeSettings,
    load_runtime_settings,
    save_runtime_settings,
    update_runtime_settings,
)
from ai_newsdesk.secrets_box import SecretsBox
from ai_newsdesk.store.store import Store
from tests.fakes import utc

SettingsFactory = Callable[..., Settings]
UI_KEY = "sk-ui-" + "u" * 40
ENV_KEY = "sk-env-" + "e" * 40
NOW = utc(2026, 10, 14, 12)


def test_defaults_match_contract() -> None:
    rs = RuntimeSettings()
    assert rs.model_dump() == {
        "runs_per_day": 4,
        "briefing_time": "07:00",
        "daily_review_cap": 500,
        "monthly_budget_usd": 5.0,
        "judge_provider": "anthropic",
        "judge_model": "claude-haiku-5-5",
        "writer_model": "claude-haiku-5-5",
        "briefing_model": "claude-haiku-5-5",
        "show_threshold": 4,
        "why_it_matters": True,
        "hn_min_points": 100,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"runs_per_day": 5},
        {"briefing_time": "7:00"},
        {"briefing_time": "24:00"},
        {"daily_review_cap": -1},
        {"monthly_budget_usd": -0.01},
        {"judge_provider": "openai"},
        {"judge_model": ""},
        {"show_threshold": 11},
        {"hn_min_points": -5},
        {"unknown_key": 1},
    ],
)
def test_invalid_values_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(bad)


@pytest.mark.parametrize("runs", [1, 2, 4, 6, 12, 24])
def test_every_frequency_choice_allowed(runs: int) -> None:
    assert RuntimeSettings(runs_per_day=runs).runs_per_day == runs


async def test_load_defaults_save_roundtrip(store: Store) -> None:
    assert await load_runtime_settings(store) == RuntimeSettings()
    changed = RuntimeSettings(runs_per_day=6, why_it_matters=False, briefing_time="06:30")
    await save_runtime_settings(store, changed)
    assert await load_runtime_settings(store) == changed
    assert await store.get_setting("runs_per_day") == 6


async def test_invalid_stored_value_falls_back(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    await store.set_setting("show_threshold", 99)
    await store.set_setting("daily_review_cap", 50)
    with caplog.at_level(logging.WARNING):
        rs = await load_runtime_settings(store)
    assert (rs.show_threshold, rs.daily_review_cap) == (4, 50)
    assert "show_threshold" in caplog.text


async def test_update_validates_before_saving(store: Store) -> None:
    rs = await update_runtime_settings(store, monthly_budget_usd=3.5)
    assert rs.monthly_budget_usd == 3.5
    with pytest.raises(ValidationError):
        await update_runtime_settings(store, runs_per_day=7, monthly_budget_usd=1.0)
    assert (await load_runtime_settings(store)).monthly_budget_usd == 3.5


async def test_resolve_order_ui_then_env_then_empty(
    store: Store, settings_factory: SettingsFactory
) -> None:
    keys = ApiKeys(store, settings_factory(anthropic_api_key=ENV_KEY))
    assert await keys.resolve_api_key("anthropic") == ENV_KEY
    assert await keys.resolve_api_key("typesafe") == ""
    await keys.set_api_key("anthropic", f"  {UI_KEY}  ", NOW)
    assert await keys.resolve_api_key("anthropic") == UI_KEY
    status = await keys.api_key_status("anthropic")
    assert (status.set_in_ui, status.set_in_env, status.is_set) == (True, True, True)
    assert status.updated_at == NOW
    await keys.clear_api_key("anthropic")
    assert await keys.resolve_api_key("anthropic") == ENV_KEY
    empty = await keys.api_key_status("research_engine")
    assert (empty.is_set, empty.updated_at) == (False, None)


async def test_bad_input_refused(store: Store, settings_factory: SettingsFactory) -> None:
    keys = ApiKeys(store, settings_factory())
    with pytest.raises(ValueError, match="empty"):
        await keys.set_api_key("anthropic", "   ", NOW)
    with pytest.raises(ValueError, match="unknown API key"):
        await keys.resolve_api_key("openai")  # type: ignore[arg-type]


async def test_secret_never_leaks(
    store: Store, settings_factory: SettingsFactory, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_factory()
    keys = ApiKeys(store, settings)
    with caplog.at_level(logging.DEBUG):
        await keys.set_api_key("anthropic", UI_KEY, NOW)
        await keys.resolve_api_key("anthropic")
        status = await keys.api_key_status("anthropic")
        await keys.clear_api_key("anthropic")
    stored_texts = [repr(keys), str(keys), repr(status), status.model_dump_json(), caplog.text]
    for text in stored_texts:
        assert UI_KEY not in text
        assert settings.secrets_key not in text
    await keys.set_api_key("anthropic", UI_KEY, NOW)
    raw = await store.get_setting("secret.anthropic")
    assert UI_KEY not in str(raw)


async def test_rotated_secrets_key_falls_back_to_env(
    store: Store, settings_factory: SettingsFactory, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_factory(anthropic_api_key=ENV_KEY)
    await ApiKeys(store, settings).set_api_key("anthropic", UI_KEY, NOW)
    rotated = ApiKeys(store, settings, box=SecretsBox("r" * 64))
    with caplog.at_level(logging.WARNING):
        assert await rotated.resolve_api_key("anthropic") == ENV_KEY
    assert "could not be decrypted" in caplog.text
    assert UI_KEY not in caplog.text
```

- [ ] **Step 6: Run them.** `uv run pytest tests/test_runtime_settings.py -v`. Expected: FAIL with `ModuleNotFoundError: No module named 'ai_newsdesk.runtime_settings'`.

- [ ] **Step 7: Implement `src/ai_newsdesk/runtime_settings.py`.**

```python
"""Run-time settings and API keys, kept in the database `settings` table (V1-S1).

Each RuntimeSettings field is stored under its own key (the field name). API keys are
stored encrypted under `secret.<name>`; a key saved in the UI wins over the `.env` value.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai_newsdesk.config import Settings
from ai_newsdesk.secrets_box import SecretsBox, SecretsBoxError
from ai_newsdesk.store.store import Store

log = logging.getLogger(__name__)

RUNS_PER_DAY_CHOICES = (1, 2, 4, 6, 12, 24)
DEFAULT_MODEL = "claude-haiku-5-5"
ApiKeyName = Literal["anthropic", "research_engine", "typesafe"]
API_KEY_NAMES: tuple[ApiKeyName, ...] = get_args(ApiKeyName)
SECRET_PREFIX = "secret."  # noqa: S105  # a settings-key prefix, not a secret


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    runs_per_day: int = 4
    briefing_time: str = Field(default="07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    daily_review_cap: int = Field(default=500, ge=0)
    monthly_budget_usd: float = Field(default=5.0, ge=0)
    judge_provider: Literal["anthropic", "typesafe"] = "anthropic"
    judge_model: str = Field(default=DEFAULT_MODEL, min_length=1)
    writer_model: str = Field(default=DEFAULT_MODEL, min_length=1)
    briefing_model: str = Field(default=DEFAULT_MODEL, min_length=1)
    show_threshold: int = Field(default=4, ge=0, le=10)
    why_it_matters: bool = True
    hn_min_points: int = Field(default=100, ge=0)

    @field_validator("runs_per_day")
    @classmethod
    def _allowed_frequency(cls, v: int) -> int:
        if v not in RUNS_PER_DAY_CHOICES:
            raise ValueError(f"runs_per_day must be one of {RUNS_PER_DAY_CHOICES}")
        return v


async def load_runtime_settings(store: Store) -> RuntimeSettings:
    """Stored values over defaults. An invalid stored value is ignored (and logged by key)."""
    values: dict[str, Any] = {}
    for name in RuntimeSettings.model_fields:
        raw = await store.get_setting(name)
        if raw is None:
            continue
        try:
            RuntimeSettings.model_validate({name: raw})
        except ValidationError:
            log.warning("ignoring invalid stored setting %s; using the default", name)
            continue
        values[name] = raw
    return RuntimeSettings.model_validate(values)


async def save_runtime_settings(store: Store, rs: RuntimeSettings) -> None:
    for name, value in rs.model_dump(mode="json").items():
        await store.set_setting(name, value)


async def update_runtime_settings(store: Store, **changes: Any) -> RuntimeSettings:
    """Validate `changes` against the current settings, save and return the result.

    Raises pydantic.ValidationError (nothing saved) when any change is invalid.
    """
    current = await load_runtime_settings(store)
    updated = RuntimeSettings.model_validate({**current.model_dump(), **changes})
    await save_runtime_settings(store, updated)
    return updated


class ApiKeyStatus(BaseModel):
    """What the Settings page may show about a key: never the key itself."""

    name: ApiKeyName
    set_in_ui: bool
    set_in_env: bool
    updated_at: datetime | None = None

    @property
    def is_set(self) -> bool:
        return self.set_in_ui or self.set_in_env


class ApiKeys:
    """Write-only API key storage. Values never leave except through resolve_api_key()."""

    def __init__(self, store: Store, settings: Settings, box: SecretsBox | None = None) -> None:
        self._store = store
        self._settings = settings
        self._box = box or SecretsBox(settings.secrets_key)

    def __repr__(self) -> str:
        return "ApiKeys(<redacted>)"

    @staticmethod
    def _key(name: ApiKeyName) -> str:
        if name not in API_KEY_NAMES:
            raise ValueError(f"unknown API key name: {name!r}")
        return SECRET_PREFIX + name

    def _env_value(self, name: ApiKeyName) -> str:
        return getattr(self._settings, f"{name}_api_key")

    async def set_api_key(self, name: ApiKeyName, value: str, at: datetime) -> None:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("an API key cannot be empty; use clear_api_key to remove it")
        record = {
            "token": self._box.encrypt(cleaned),
            "updated_at": at.astimezone(UTC).isoformat(),
        }
        await self._store.set_setting(self._key(name), record)
        log.info("API key %s saved", name)

    async def clear_api_key(self, name: ApiKeyName) -> None:
        await self._store.set_setting(self._key(name), None)
        log.info("API key %s cleared; the .env value applies", name)

    async def api_key_status(self, name: ApiKeyName) -> ApiKeyStatus:
        record = await self._store.get_setting(self._key(name))
        return ApiKeyStatus(
            name=name,
            set_in_ui=record is not None,
            set_in_env=bool(self._env_value(name)),
            updated_at=datetime.fromisoformat(record["updated_at"]) if record else None,
        )

    async def resolve_api_key(self, name: ApiKeyName) -> str:
        """The UI value, else the `.env` value, else "". An undecryptable UI value is skipped."""
        record = await self._store.get_setting(self._key(name))
        if record is not None:
            try:
                return self._box.decrypt(record["token"])
            except SecretsBoxError:
                log.warning("stored %s API key could not be decrypted; using the .env value", name)
        return self._env_value(name)
```

- [ ] **Step 8: Run the tests.** `uv run pytest tests/test_secrets_box.py tests/test_runtime_settings.py -v`. Expected: `28 passed`. Then run `make test`. Expected: green, `107 passed`.

- [ ] **Step 9: Commit.**

```bash
git add src/ai_newsdesk/secrets_box.py src/ai_newsdesk/runtime_settings.py \
  tests/test_secrets_box.py tests/test_runtime_settings.py
git commit -m "feat(settings): encrypted API keys and typed runtime settings (V1-S1)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Pricing table

**Goal:** Make `config/pricing.yaml` the one source of model IDs and prices, so the budget, cost estimates and model pickers all use the same numbers.

**Files:**
- Create: `config/pricing.yaml`, `src/ai_newsdesk/pricing.py`
- Test: `tests/test_pricing.py`

**Acceptance Criteria:**
- [ ] `Pricing.load("config/pricing.yaml")` has `as_of == "2026-10-14"` and exactly four models:
  - `claude-haiku-5-5`: $0.10 in / $0.50 out
  - `claude-sonnet-5-5`: $2.00 / $10.00
  - `claude-opus-5-5`: $4.00 / $20.00
  - `jev-1.13.0`: $0.042 / $0, provider `typesafe`
- [ ] `models("anthropic")` lists the three Claude IDs, sorted. `models("typesafe") == ["jev-1.13.0"]`.
- [ ] `cost("claude-haiku-5-5", 1_000_000, 1_000_000) == 0.60`. Cache writes cost 1.25× the input price and cache reads 0.1×. `usage_cost(Usage)` uses all four token counts.
- [ ] A dated model ID (`claude-haiku-5-5-20261001`) raises `UnknownModelError`, which is a `KeyError`. Negative token counts raise `ValueError`.
- [ ] An unknown provider or an empty `models:` map in the YAML raises `ValidationError`.
- [ ] Every default model in `RuntimeSettings` is in the table.

**Verify:** `uv run pytest tests/test_pricing.py -v` → `11 passed`

**Steps:**

- [ ] **Step 1: Write the failing tests** in `tests/test_pricing.py`.

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_newsdesk.models import Purpose, Usage
from ai_newsdesk.pricing import Pricing, UnknownModelError
from ai_newsdesk.runtime_settings import RuntimeSettings
from tests.conftest import ROOT

PRICING = ROOT / "config" / "pricing.yaml"


@pytest.fixture
def pricing() -> Pricing:
    return Pricing.load(PRICING)


def test_shipped_table(pricing: Pricing) -> None:
    assert pricing.as_of == "2026-10-14"
    assert pricing.models() == [
        "claude-haiku-5-5",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "jev-1.13.0",
    ]
    assert pricing.models("anthropic") == [
        "claude-haiku-5-5",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]
    assert pricing.models("typesafe") == ["jev-1.13.0"]
    assert pricing.models("nobody") == []
    haiku = pricing.price("claude-haiku-5-5")
    assert (haiku.provider, haiku.input_per_mtok, haiku.output_per_mtok) == ("anthropic", 0.1, 0.5)


def test_runtime_defaults_are_priced(pricing: Pricing) -> None:
    rs = RuntimeSettings()
    for model in (rs.judge_model, rs.writer_model, rs.briefing_model):
        assert model in pricing.models("anthropic")


@pytest.mark.parametrize(
    ("model", "in_tok", "out_tok", "expected"),
    [
        ("claude-haiku-5-5", 1_000_000, 1_000_000, 0.60),
        ("claude-haiku-5-5", 2_000, 500, 0.00045),
        ("claude-sonnet-5-5", 1_000_000, 0, 2.00),
        ("claude-opus-5-5", 0, 1_000_000, 20.00),
        ("jev-1.13.0", 1_000_000, 1_000_000, 0.042),
        ("claude-haiku-5-5", 0, 0, 0.0),
    ],
)
def test_cost(pricing: Pricing, model: str, in_tok: int, out_tok: int, expected: float) -> None:
    assert pricing.cost(model, in_tok, out_tok) == pytest.approx(expected)


def test_cache_tokens_and_usage_cost(pricing: Pricing) -> None:
    # 1M cache writes at 1.25x and 1M cache reads at 0.1x of $0.10
    cost = pricing.cost(
        "claude-haiku-5-5", 0, 0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000
    )
    assert cost == pytest.approx(0.135)
    usage = Usage(
        provider="anthropic",
        model="claude-haiku-5-5",
        purpose=Purpose.REVIEW,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    assert pricing.usage_cost(usage) == pytest.approx(0.61)


def test_unknown_model_and_bad_counts(pricing: Pricing) -> None:
    with pytest.raises(UnknownModelError):
        pricing.cost("claude-haiku-5-5-20261001", 1, 1)
    with pytest.raises(KeyError):  # UnknownModelError is a KeyError
        pricing.price("gpt-x")
    with pytest.raises(ValueError, match="negative"):
        pricing.cost("claude-haiku-5-5", -1, 0)


def test_load_validates(tmp_path: Path) -> None:
    bad = tmp_path / "pricing.yaml"
    bad.write_text(
        "as_of: '2026-10-14'\n"
        "models:\n"
        "  m: {provider: other, input_per_mtok: 1, output_per_mtok: 1}\n"
    )
    with pytest.raises(ValidationError):
        Pricing.load(bad)
    bad.write_text("as_of: '2026-10-14'\nmodels: {}\n")
    with pytest.raises(ValidationError):
        Pricing.load(bad)
```

- [ ] **Step 2: Run them.** `uv run pytest tests/test_pricing.py -v`. Expected: FAIL with `ModuleNotFoundError: No module named 'ai_newsdesk.pricing'`.

- [ ] **Step 3: Create `config/pricing.yaml`.**

```yaml
# Model prices in USD per million tokens. Model IDs are exact API strings: never add dates.
# Checked against the providers' price pages on the date below; update both together.
as_of: "2026-10-14"
models:
  claude-haiku-5-5:
    provider: anthropic
    input_per_mtok: 0.10
    output_per_mtok: 0.50
  claude-sonnet-5-5:
    provider: anthropic
    input_per_mtok: 2.00
    output_per_mtok: 10.00
  claude-opus-5-5:
    provider: anthropic
    input_per_mtok: 4.00
    output_per_mtok: 20.00
  jev-1.13.0:
    provider: typesafe
    input_per_mtok: 0.042
    output_per_mtok: 0.0
```

- [ ] **Step 4: Implement `src/ai_newsdesk/pricing.py`.**

```python
"""Model price table from config/pricing.yaml (V1-S1). One table for every provider."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from ai_newsdesk.models import Usage

DEFAULT_PRICING_PATH = Path("config/pricing.yaml")
# Anthropic prompt caching (5-minute cache), as multiples of the input price.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10
_PER_TOKEN = 1_000_000


class UnknownModelError(KeyError):
    """The model ID is not in pricing.yaml."""


class ModelPrice(BaseModel):
    provider: Literal["anthropic", "typesafe"]
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class _PricingFile(BaseModel):
    as_of: str
    models: dict[str, ModelPrice] = Field(min_length=1)


class Pricing:
    def __init__(self, models: dict[str, ModelPrice], as_of: str) -> None:
        self._models = dict(models)
        self.as_of = as_of

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PRICING_PATH) -> "Pricing":
        data = _PricingFile.model_validate(yaml.safe_load(Path(path).read_text()))
        return cls(data.models, data.as_of)

    def price(self, model: str) -> ModelPrice:
        try:
            return self._models[model]
        except KeyError:
            raise UnknownModelError(model) from None

    def models(self, provider: str | None = None) -> list[str]:
        """Model IDs, sorted; only `provider`'s when given."""
        return sorted(
            name for name, p in self._models.items() if provider is None or p.provider == provider
        )

    def cost(
        self,
        model: str,
        in_tokens: int,
        out_tokens: int,
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """USD for one call. `in_tokens` excludes cached tokens, as the Anthropic API reports."""
        if min(in_tokens, out_tokens, cache_read_tokens, cache_write_tokens) < 0:
            raise ValueError("token counts cannot be negative")
        p = self.price(model)
        input_equiv = (
            in_tokens
            + cache_write_tokens * CACHE_WRITE_MULTIPLIER
            + cache_read_tokens * CACHE_READ_MULTIPLIER
        )
        return (input_equiv * p.input_per_mtok + out_tokens * p.output_per_mtok) / _PER_TOKEN

    def usage_cost(self, usage: Usage) -> float:
        return self.cost(
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
```

- [ ] **Step 5: Run the tests.** `uv run pytest tests/test_pricing.py -v`. Expected: `11 passed`. Then run `make test`. Expected: green, `118 passed`.

- [ ] **Step 6: Commit.**

```bash
git add config/pricing.yaml src/ai_newsdesk/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): model price table and cost calculation (V1-S1)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Step done when

- [ ] `make test` is green locally (ruff, ruff format, pyright `0 errors`, `118 passed`).
- [ ] `uv run pre-commit run --all-files` passes, including gitleaks. The test secrets are built at run time, so gitleaks has nothing to flag.
- [ ] The branch is pushed, and a PR is opened with `git push -u origin step-1-store && GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr create --base main --title "Step 1: store, secrets, runtime settings, pricing (V1-S1)" --body-file <body>`. The body lists Tasks 5–8 and the Contract additions, and ends with the attribution line `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- [ ] CI is green on the PR: the `lint-test` and `gitleaks` jobs. If hosted runners don't pick up jobs, the same SHA's push run is the evidence; rerun later.
- [ ] The PR is merged to `main` with `GH_TOKEN=... gh pr merge --merge --delete-branch`, and local `main` is fast-forwarded.
