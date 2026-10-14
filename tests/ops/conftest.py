"""Fake-bin fixture for ops script tests: a fake `docker` on PATH, canned output, argv recorded."""

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

BASH = shutil.which("bash") or "bash"
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "docker"
HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaA"

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker: records argv, prints canned output chosen by subcommand.
F="$FAKE_FIXTURES"
printf '%s\n' "$*" >> "$FAKE_CALLS"
printf '%s\n' "${GIT_SHA-unset}" >> "$FAKE_CALLS.sha"
if [ -n "${FAKE_FAIL:-}" ]; then echo "fake docker: simulated failure" >&2; exit "$FAKE_FAIL"; fi
args=" $* "
case "$1" in
  compose)
    case "$args" in
      *" run --rm --no-deps -T app ai-newsdesk hash-password "*)
        cat > "$FAKE_CALLS.stdin"
        printf '%s\r\n' "$FAKE_HASH_OUT" ;;
      *" cp app:"*)
        [ -z "${FAKE_CP_FAIL:-}" ] || { echo "fake docker: cp failed" >&2; exit 1; }
        printf 'SQLite format 3 fake backup' > "${*: -1}" ;;
      *" ps -a -q "*) [ -n "${FAKE_PS_EMPTY:-}" ] || cat "$F/ps_q.txt" ;;
      *" config --services "*)
        [ -z "${FAKE_CONFIG_FAIL:-}" ] || exit 1
        cat "$F/config_services.txt" ;;
      *" images "*) cat "$F/images.json" ;;
      *" logs "*) [ -z "${FAKE_LOGS_FAIL:-}" ] || exit 1; cat "$F/${FAKE_LOGS_FILE:-logs.txt}" ;;
      *" exec "*)
        case "$args" in
          *"smoke-probe"*)
            cat "$F/${FAKE_SMOKE_FILE:-exec_smoke_ok.json}"; exit "${FAKE_SMOKE_EXIT:-0}" ;;
          *" ai-newsdesk db backup "*)
            if [ -n "${FAKE_DB_BACKUP_FAIL:-}" ]; then
              echo '{"command": "db backup", "error": "database not found", "ok": false}'; exit 1
            fi
            printf '{"bytes": 27, "command": "db backup", "ok": true, "path": "%s"}\n' "${*: -1}" ;;
          *" rm -f /data/.backup-"*) : ;;
          *" sweep-stale-backup-temps "*) echo '[]' ;;
          *"/health"*) echo "${FAKE_HEALTH_CODE:-200}" ;;
          *) exit 1 ;;
        esac ;;
      *" up -d "*)
        printf '%s\n' "${GIT_SHA-unset}" >> "$FAKE_CALLS.up"
        if [ -n "${FAKE_UP_FAIL_SHA:-}" ] && [ "${GIT_SHA-}" = "$FAKE_UP_FAIL_SHA" ]; then
          echo "fake docker: up failed" >&2; exit 1
        fi
        echo "fake docker: up ok" >&2 ;;
      *) echo "fake docker: unhandled: $*" >&2; exit 99 ;;
    esac ;;
  image)
    case "$2" in
      ls) for t in ${FAKE_IMAGE_TAGS:-}; do echo "$t"; done ;;
      rm) [ -z "${FAKE_IMAGE_RM_FAIL:-}" ] || exit 1; echo "Untagged: $3" ;;
      *) echo "fake docker: unhandled: $*" >&2; exit 99 ;;
    esac ;;
  ps) for i in ${FAKE_PS_IMAGES:-}; do echo "$i"; done ;;
  inspect)
    case "$args" in
      *" --format "*) cat "$F/${FAKE_INSPECT_FILE:-inspect.tsv}" ;;
      *) echo "fake docker: inspect without --format would expose Config.Env" >&2; exit 98 ;;
    esac ;;
  stats) cat "$F/stats.ndjson" ;;
  system) cat "$F/df.json" ;;
  *) echo "fake docker: unhandled: $*" >&2; exit 99 ;;
esac
"""


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    last: dict[str, Any]


@dataclass
class FakeEnv:
    repo: Path
    calls: Path
    caddy_env: Path
    secrets: list[str]
    run: Callable[..., Result]

    def docker_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


def json_lines(stdout: str) -> list[str]:
    return [ln for ln in stdout.splitlines() if ln.startswith("{") and ln.endswith("}")]


@pytest.fixture
def fake_env(tmp_path: Path) -> FakeEnv:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(ROOT / "ops", repo / "ops")
    if (ROOT / "Makefile").exists():
        shutil.copy(ROOT / "Makefile", repo / "Makefile")
    pad = "0" * 32
    values = {
        "SECRETS_KEY": f"SEKRIT-SK-{pad}",
        "SESSION_SECRET": f"SEKRIT-SS-{pad}",
        "ANTHROPIC_API_KEY": f"SEKRIT-AN-{pad}",
        "RESEARCH_ENGINE_API_KEY": f"SEKRIT-RE-{pad}",
    }
    env_file = repo / ".env"
    env_file.write_text(
        "# test env\n"
        + "".join(f"{k}={v}\n" for k, v in values.items())
        + f"OWNER_PASSWORD_HASH='{HASH}'\n"
        + "SITE_HOST=news.example.test\n"
    )
    env_file.chmod(0o600)
    caddy = tmp_path / "caddy"
    caddy.mkdir()
    caddy_env = caddy / ".env"
    caddy_env.write_text("SITE_HOST=research.example.test\nAI_NEWSDESK_HOST=news.example.test\n")
    (repo / "backups").mkdir()
    (repo / "backups" / "ai-newsdesk-20261014T033000Z.sqlite").write_bytes(b"x" * 10)
    (repo / "deploys.jsonl").write_text(
        '{"action": "deploy", "from": "none", "result": "ok", "smoke": "pass", '
        '"to": "abc1234abc12", "ts": "2026-10-14T10:00:00Z"}\n'
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    calls = tmp_path / "calls.log"

    def run(
        cmd: str, *args: str, script: str | None = None, stdin: str | None = None, **env: str
    ) -> Result:
        full = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "FAKE_FIXTURES": str(FIXTURES),
            "FAKE_CALLS": str(calls),
            "FAKE_HASH_OUT": HASH,
            "GIT_SHA": "abc1234",
            "CADDY_DIR": str(caddy),
            **env,
        }
        p = subprocess.run(
            [BASH, script or f"ops/{cmd}.sh", *args],
            cwd=repo,
            env=full,
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = p.stdout.strip().splitlines()
        try:
            last = json.loads(lines[-1]) if lines else {}
        except ValueError:
            last = {}
        return Result(p.returncode, p.stdout, p.stderr, last)

    return FakeEnv(
        repo=repo,
        calls=calls,
        caddy_env=caddy_env,
        secrets=[*values.values(), HASH],
        run=run,
    )
