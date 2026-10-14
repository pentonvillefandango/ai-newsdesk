"""`make backup`: online backup inside the app container, copied out, retention (V1-S7)."""

import fcntl
import os
import stat
import time

from .conftest import FakeEnv, json_lines

TS = "20261015T033000Z"


def test_backup_writes_a_private_file(fake_env: FakeEnv) -> None:
    r = fake_env.run("backup", BACKUP_TS=TS)
    assert r.code == 0, (r.stdout, r.stderr)
    dest = fake_env.repo / "backups" / f"ai-newsdesk-{TS}.sqlite"
    assert r.last == {
        "bytes": dest.stat().st_size,
        "command": "backup",
        "file": str(dest),
        "ok": True,
        "pruned": [],
    }
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert stat.S_IMODE((fake_env.repo / "backups").stat().st_mode) == 0o700
    calls = fake_env.docker_calls()
    assert any(f"exec -T app ai-newsdesk db backup --out /data/.backup-{TS}-" in c for c in calls)
    assert any(f"cp app:/data/.backup-{TS}-" in c for c in calls)
    assert any("rm -f /data/.backup-" in c for c in calls)
    assert not list((fake_env.repo / "backups").glob("*.part"))


def test_in_container_failure_leaves_nothing(fake_env: FakeEnv) -> None:
    r = fake_env.run("backup", BACKUP_TS=TS, FAKE_DB_BACKUP_FAIL="1")
    assert r.code == 1 and r.last["ok"] is False and r.last["detail"]["ok"] is False
    assert not (fake_env.repo / "backups" / f"ai-newsdesk-{TS}.sqlite").exists()
    assert any("rm -f /data/.backup-" in c for c in fake_env.docker_calls())


def test_copy_failure_leaves_no_part_file(fake_env: FakeEnv) -> None:
    r = fake_env.run("backup", BACKUP_TS=TS, FAKE_CP_FAIL="1")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1
    assert not list((fake_env.repo / "backups").glob("*.part"))


def test_retention_prunes_only_old_ai_newsdesk_backups(fake_env: FakeEnv) -> None:
    bdir = fake_env.repo / "backups"
    old = bdir / "ai-newsdesk-20260901T033000Z.sqlite"
    other = bdir / "notes.txt"
    for p in (old, other):
        p.write_text("x")
        past = time.time() - 30 * 86400
        os.utime(p, (past, past))
    r = fake_env.run("backup", BACKUP_TS=TS, BACKUP_RETENTION_DAYS="14")
    assert r.code == 0 and r.last["pruned"] == [old.name]
    assert not old.exists() and other.exists()
    assert (bdir / "ai-newsdesk-20261014T033000Z.sqlite").exists()  # recent: kept


def test_existing_destination_is_exit_2(fake_env: FakeEnv) -> None:
    (fake_env.repo / "backups" / f"ai-newsdesk-{TS}.sqlite").write_text("keep")
    r = fake_env.run("backup", BACKUP_TS=TS)
    assert r.code == 2 and r.last["ok"] is False


def test_bad_inputs_are_exit_2(fake_env: FakeEnv) -> None:
    assert fake_env.run("backup", BACKUP_TS="yesterday").code == 2
    assert fake_env.run("backup", BACKUP_RETENTION_DAYS="0").code == 2
    assert fake_env.run("backup", "--extra").code == 2


def test_held_lock_is_exit_2(fake_env: FakeEnv) -> None:
    (fake_env.repo / ".deploy").mkdir()
    with (fake_env.repo / ".deploy" / "lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = fake_env.run("backup", BACKUP_TS=TS, BACKUP_LOCK_WAIT="0")
    assert r.code == 2 and "lock" in r.last["error"]
