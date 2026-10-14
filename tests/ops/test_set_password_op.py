"""`make set-password`: password on stdin only, single-quoted hash in .env, mode 600 (V1-S7)."""

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from .conftest import HASH, ROOT, FakeEnv

NEW_HASH = "$argon2id$v=19$m=65536,t=3,p=4$bmV3c2FsdG5ld3NhbHQ$bmV3aGFzaG5ld2hhc2huZXc"
PASSWORD = "correct horse battery"


@pytest.fixture
def hasher(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "hasher.stdin"
    script = tmp_path / "fake_hasher.sh"
    script.write_text(f"#!/usr/bin/env bash\ncat > '{log}'\nprintf '%s\\n' \"$HASH_OUT\"\n")
    script.chmod(0o755)
    return str(script), log


def _run(fake_env: FakeEnv, hasher: tuple[str, Path], stdin: str, out: str = NEW_HASH):
    return fake_env.run("set_password", stdin=stdin, HASH_CMD=hasher[0], HASH_OUT=out)


def _hash_lines(env: Path) -> list[str]:
    return [ln for ln in env.read_text().splitlines() if ln.startswith("OWNER_PASSWORD_HASH")]


def test_replaces_the_hash_single_quoted(fake_env: FakeEnv, hasher: tuple[str, Path]) -> None:
    env = fake_env.repo / ".env"
    others = [ln for ln in env.read_text().splitlines() if "OWNER_PASSWORD_HASH" not in ln]
    r = _run(fake_env, hasher, f"{PASSWORD}\n{PASSWORD}\n")
    assert r.code == 0, (r.stdout, r.stderr)
    assert r.last == {
        "command": "set-password",
        "env_mode": "600",
        "key": "OWNER_PASSWORD_HASH",
        "next": "make deploy",
        "ok": True,
    }
    assert _hash_lines(env) == [f"OWNER_PASSWORD_HASH='{NEW_HASH}'"]
    assert [ln for ln in env.read_text().splitlines() if "OWNER_PASSWORD_HASH" not in ln] == others
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert hasher[1].read_text() == PASSWORD + "\n"  # stdin, once
    for text in (r.stdout, r.stderr):
        assert PASSWORD not in text and NEW_HASH not in text and HASH not in text


def test_appends_when_absent(fake_env: FakeEnv, hasher: tuple[str, Path]) -> None:
    env = fake_env.repo / ".env"
    env.write_text("".join(ln + "\n" for ln in env.read_text().splitlines() if "OWNER" not in ln))
    r = _run(fake_env, hasher, f"{PASSWORD}\n{PASSWORD}\n")
    assert r.code == 0 and _hash_lines(env) == [f"OWNER_PASSWORD_HASH='{NEW_HASH}'"]


@pytest.mark.parametrize(
    ("stdin", "out", "code"),
    [
        (f"{PASSWORD}\nsomething else entirely\n", NEW_HASH, 2),  # mismatch
        ("short\nshort\n", NEW_HASH, 2),  # under 12 characters
        (f"{PASSWORD}\n{PASSWORD}\n", "not-a-hash", 1),  # hasher output rejected
        (f"{PASSWORD}\n{PASSWORD}\n", "$argon2id$v=19$m=1,t=1,p=1$a'b$c", 1),  # quote in hash
    ],
)
def test_nothing_changes_on_failure(
    fake_env: FakeEnv, hasher: tuple[str, Path], stdin: str, out: str, code: int
) -> None:
    env = fake_env.repo / ".env"
    before = env.read_bytes()
    r = _run(fake_env, hasher, stdin, out)
    assert r.code == code and r.last["ok"] is False and r.last["command"] == "set-password"
    assert env.read_bytes() == before
    assert PASSWORD not in r.stdout + r.stderr
    if code == 2:
        assert not hasher[1].exists(), "the hasher never runs on a rejected password"


def test_missing_env_is_exit_2(fake_env: FakeEnv, hasher: tuple[str, Path]) -> None:
    (fake_env.repo / ".env").unlink()
    r = _run(fake_env, hasher, f"{PASSWORD}\n{PASSWORD}\n")
    assert r.code == 2 and not (fake_env.repo / ".env").exists()


def test_docker_fallback_uses_a_one_off_container(fake_env: FakeEnv) -> None:
    r = fake_env.run("set_password", stdin=f"{PASSWORD}\n{PASSWORD}\n", HASH_VIA="docker")
    assert r.code == 0, (r.stdout, r.stderr)
    assert any(
        c.startswith("compose -p ai-newsdesk ")
        and c.endswith("run --rm --no-deps -T app ai-newsdesk hash-password")
        for c in fake_env.docker_calls()
    )
    assert Path(f"{fake_env.calls}.stdin").read_text() == PASSWORD + "\n"
    assert _hash_lines(fake_env.repo / ".env") == [f"OWNER_PASSWORD_HASH='{HASH}'"]  # \r stripped
    assert PASSWORD not in fake_env.calls.read_text()  # never on a command line


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker compose CLI")
def test_compose_reads_the_written_hash_literally(
    fake_env: FakeEnv, hasher: tuple[str, Path]
) -> None:
    """Real `docker compose config` (renders only; no daemon state): the single-quoted hash
    reaches the app's environment with every `$` intact (config shows a literal `$` as `$$`)."""
    assert _run(fake_env, hasher, f"{PASSWORD}\n{PASSWORD}\n").code == 0
    p = subprocess.run(
        ["docker", "compose", "-p", "ai-newsdesk-test", "-f", str(ROOT / "compose.yaml"),
         "--env-file", str(fake_env.repo / ".env"), "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )  # fmt: skip
    env = json.loads(p.stdout)["services"]["app"]["environment"]
    assert env["OWNER_PASSWORD_HASH"].replace("$$", "$") == NEW_HASH
    assert "variable is not set" not in p.stderr
