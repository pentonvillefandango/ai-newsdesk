"""`ai-newsdesk db backup --out PATH`: online backup of the live SQLite database (V1-S7).

Used by ops/backup.sh inside the running container. Prints one JSON line; exit 0 ok, 1 failed.
"""

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from ai_newsdesk.config import Settings
from ai_newsdesk.store.store import Store

COMMAND = "db backup"


def register(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:  # pyright: ignore[reportPrivateUsage]
    db = sub.add_parser("db", help="database maintenance")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    backup = db_sub.add_parser("backup", help="online backup of the live database to --out")
    backup.add_argument("--out", required=True, help="destination file (must not exist)")
    backup.set_defaults(func=cmd_backup)


def _emit(**fields: object) -> None:
    print(json.dumps({"command": COMMAND, **fields}, sort_keys=True))


async def _backup(src: Path, out: Path) -> None:
    store = Store(str(src))
    try:
        await store.init()  # opens the connection (migrations are already applied on a live DB)
        await store.backup(str(out))
    finally:
        await store.close()


def _integrity_ok(path: Path) -> bool:
    with sqlite3.connect(path) as conn:
        return conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def cmd_backup(args: argparse.Namespace) -> int:
    src = Path(Settings().db_path)  # pyright: ignore[reportCallIssue] - fields come from env
    out = Path(args.out)
    if not src.is_file():
        _emit(ok=False, error="database not found")
        return 1
    if out.exists():
        _emit(ok=False, error="output already exists")
        return 1
    try:
        asyncio.run(_backup(src, out))
        if not _integrity_ok(out):
            raise ValueError("integrity check failed")
    except Exception as exc:  # noqa: BLE001 - one JSON line, never a traceback
        out.unlink(missing_ok=True)
        _emit(ok=False, error=f"{type(exc).__name__}: {exc}")
        return 1
    _emit(ok=True, path=str(out), bytes=out.stat().st_size)
    return 0
