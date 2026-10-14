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
        assert await apply_migrations(conn) == [1, 2, 3, 4]
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
    assert [v for v, _ in migration_files()] == [1, 2, 3, 4]
    (tmp_path / "1_bad.sql").write_text("")
    with pytest.raises(ValueError, match=r"NNNN_name\.sql"):
        migration_files(tmp_path)
