"""Migration 0004: the spend table accepts OpenAI and OpenRouter calls (multi-provider)."""

from pathlib import Path

import aiosqlite
import pytest

from ai_newsdesk.store.db import MIGRATIONS_DIR, apply_migrations, connect

INSERT = (
    "INSERT INTO spend (at, model, provider, purpose, input_tokens, output_tokens,"
    " cache_read_tokens, cache_write_tokens, cost_usd) VALUES (?, ?, ?, ?, 1, 2, 3, 4, ?)"
)


async def test_existing_spend_survives_and_new_providers_are_accepted(
    db_path: str, tmp_path: Path
) -> None:
    before = tmp_path / "before"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("000[123]_*.sql")):
        (before / path.name).write_text(path.read_text())

    conn = await connect(db_path)
    try:
        assert await apply_migrations(conn, before) == [1, 2, 3]
        await conn.execute(
            INSERT, ("2026-10-01T00:00:00+00:00", "claude-haiku-5-5", "anthropic", "review", 0.5)
        )
        await conn.execute(
            INSERT, ("2026-10-02T00:00:00+00:00", "jev-1.13.0", "typesafe", "review", 0.25)
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                INSERT, ("2026-10-03T00:00:00+00:00", "gpt-x", "openrouter", "review", 0.1)
            )

        assert await apply_migrations(conn) == [4]

        async with conn.execute(
            "SELECT id, at, model, provider, purpose, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, cost_usd FROM spend ORDER BY id"
        ) as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
        assert rows == [
            (1, "2026-10-01T00:00:00+00:00", "claude-haiku-5-5", "anthropic", "review",
             1, 2, 3, 4, 0.5),
            (2, "2026-10-02T00:00:00+00:00", "jev-1.13.0", "typesafe", "review", 1, 2, 3, 4, 0.25),
        ]  # fmt: skip

        for provider in ("openai", "openrouter"):
            await conn.execute(
                INSERT, ("2026-10-03T00:00:00+00:00", "some-model", provider, "write", 0.1)
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                INSERT, ("2026-10-03T00:00:00+00:00", "some-model", "someone", "write", 0.1)
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                INSERT, ("2026-10-03T00:00:00+00:00", "some-model", "openai", "other", 0.1)
            )

        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'spend'"
        ) as cur:
            assert {r[0] for r in await cur.fetchall()} >= {"ix_spend_at"}
    finally:
        await conn.close()
