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
