-- multi-provider spec 4: the Compare page. A comparison runs 2-4 models over a sample of items
-- (judge) or stories (writer) and keeps every model's output here, never in items or stories.
-- models_json: [{"provider": ..., "model": ...}]; a model's key is "provider:model".
-- A result with latency_ms NULL was not run (the cap or the time limit stopped its model).

CREATE TABLE compare_runs (
    id              INTEGER PRIMARY KEY,
    job             TEXT    NOT NULL CHECK (job IN ('judge', 'writer')),
    models_json     TEXT    NOT NULL,
    sample_ids_json TEXT    NOT NULL DEFAULT '[]',
    cap_usd         REAL    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('running', 'done', 'capped', 'failed')),
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    cost_usd        REAL    NOT NULL DEFAULT 0,
    error           TEXT
);
CREATE INDEX ix_compare_runs_started ON compare_runs (started_at);

CREATE TABLE compare_results (
    run_id      INTEGER NOT NULL REFERENCES compare_runs (id) ON DELETE CASCADE,
    model_key   TEXT    NOT NULL,
    subject_id  INTEGER NOT NULL,
    output_json TEXT,
    ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
    latency_ms  INTEGER,
    cost_usd    REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, model_key, subject_id)
);

-- The owner's reusable label set: one current label per item.
CREATE TABLE labels (
    item_id INTEGER PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    label   TEXT    NOT NULL CHECK (label IN ('junk', 'show', 'important')),
    set_at  TEXT    NOT NULL
);

-- The owner's preferred summary per story in a writer comparison.
CREATE TABLE writer_prefs (
    run_id    INTEGER NOT NULL REFERENCES compare_runs (id) ON DELETE CASCADE,
    story_id  INTEGER NOT NULL,
    model_key TEXT    NOT NULL,
    set_at    TEXT    NOT NULL,
    PRIMARY KEY (run_id, story_id)
);
