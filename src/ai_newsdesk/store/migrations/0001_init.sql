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
