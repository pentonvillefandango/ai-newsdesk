-- multi-provider: spend rows may come from OpenAI and OpenRouter calls too. SQLite cannot alter
-- a CHECK constraint, so the table is rebuilt with every row and id kept. Nothing references
-- spend, so the rebuild is safe with foreign keys on.

CREATE TABLE spend_new (
    id                 INTEGER PRIMARY KEY,
    at                 TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    provider           TEXT    NOT NULL
        CHECK (provider IN ('anthropic', 'typesafe', 'openai', 'openrouter')),
    purpose            TEXT    NOT NULL CHECK (purpose IN ('review', 'write', 'briefing')),
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL    NOT NULL
);

INSERT INTO spend_new (id, at, model, provider, purpose, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, cost_usd)
    SELECT id, at, model, provider, purpose, input_tokens, output_tokens,
           cache_read_tokens, cache_write_tokens, cost_usd
    FROM spend;

DROP TABLE spend;
ALTER TABLE spend_new RENAME TO spend;
CREATE INDEX ix_spend_at ON spend (at);
