"""`ai-newsdesk db backup` and the hash-password stdin contract that ops relies on (V1-S7)."""

import asyncio
import io
import json
import sqlite3
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from ai_newsdesk import cli
from ai_newsdesk.store.store import Store

SECRET = "s" * 40


@pytest.fixture
def db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "live.sqlite"
    monkeypatch.setenv("SECRETS_KEY", SECRET)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DB_PATH", str(path))
    return path


def _make_db(path: Path) -> None:
    async def go() -> None:
        store = Store(str(path))
        await store.init()
        await store.set_setting("runs_per_day", 4)
        await store.close()

    asyncio.run(go())


def _last_json(out: str) -> dict[str, object]:
    return json.loads(out.strip().splitlines()[-1])


def test_db_backup_copies_the_live_database(
    db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_db(db_path)
    out = tmp_path / "copy.sqlite"
    code = cli.main(["db", "backup", "--out", str(out)])
    line = _last_json(capsys.readouterr().out)
    assert code == 0, line
    assert line == {"bytes": out.stat().st_size, "command": "db backup", "ok": True, "path": str(out)}
    with sqlite3.connect(out) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "items", "stories", "runs", "spend"} <= tables


def test_db_backup_refuses_a_missing_database(
    db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "copy.sqlite"
    code = cli.main(["db", "backup", "--out", str(out)])
    line = _last_json(capsys.readouterr().out)
    assert code == 1 and line == {"command": "db backup", "error": "database not found", "ok": False}
    assert not db_path.exists(), "a backup must never create an empty live database"
    assert not out.exists()


def test_db_backup_refuses_an_existing_output(
    db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_db(db_path)
    out = tmp_path / "copy.sqlite"
    out.write_text("keep me")
    code = cli.main(["db", "backup", "--out", str(out)])
    line = _last_json(capsys.readouterr().out)
    assert code == 1 and line["error"] == "output already exists"
    assert out.read_text() == "keep me"


def test_hash_password_reads_stdin_when_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ops/set_password.sh pipes the password on stdin (never argv) and reads one hash line."""
    password = "correct horse battery staple"
    monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
    code = cli.main(["hash-password"])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert code == 0 and len(lines) == 1
    assert lines[0].startswith("$argon2id$")
    assert PasswordHasher().verify(lines[0], password)
    assert password not in captured.out + captured.err
