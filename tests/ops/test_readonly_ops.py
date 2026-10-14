"""Read-only ops: status, health, version, logs and the shared lib (V1-S7)."""

import json
import os
import shutil
import subprocess

import pytest

from .conftest import HASH, FakeEnv, json_lines

BASH = shutil.which("bash") or "bash"


def lib(
    fake_env: FakeEnv, snippet: str, stdin: str = "", **env: str
) -> subprocess.CompletedProcess[str]:
    full = {**os.environ, "GIT_SHA": "abc1234", "REPO_DIR": str(fake_env.repo), **env}
    return subprocess.run(
        [BASH, "-c", f". {fake_env.repo}/ops/lib.sh; {snippet}"],
        input=stdin,
        env=full,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _no_secrets(fake_env: FakeEnv, *texts: str) -> None:
    joined = "".join(texts)
    for secret in fake_env.secrets:
        assert secret not in joined


# ---- status ------------------------------------------------------------------------------------


def test_status_shape_and_no_secrets(fake_env: FakeEnv) -> None:
    r = fake_env.run("status")
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["command"] == "status"
    [app] = r.last["containers"]
    assert app == {
        "cpu": "0.10%",
        "health": "healthy",
        "memory": "120MiB / 512MiB",
        "name": "ai-newsdesk-app-1",
        "restarts": 0,
        "state": "running",
    }
    assert [v["name"] for v in r.last["volumes"]] == ["ai-newsdesk_app-data"]
    assert r.last["image"] == "ai-newsdesk-app:abc1234"
    assert r.last["last_deploy"]["to"] == "abc1234abc12"
    assert r.last["last_backup"]["name"] == "ai-newsdesk-20261014T033000Z.sqlite"
    _no_secrets(fake_env, r.stdout, r.stderr)


def test_status_stopped_stack(fake_env: FakeEnv) -> None:
    r = fake_env.run("status", FAKE_PS_EMPTY="1")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1
    assert r.last["ok"] is False and r.last["not_running"] == ["app"]


# ---- health ------------------------------------------------------------------------------------


def test_health_ok(fake_env: FakeEnv) -> None:
    r = fake_env.run("health")
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["app_status"] == "up" and r.last["http_status"] == 200
    assert r.last["problems"] == [] and r.last["site_host_match"] is True
    assert r.last["password_set"] is True and r.last["weak_secrets"] == []
    assert "example.test" not in r.stdout + r.stderr
    _no_secrets(fake_env, r.stdout, r.stderr)


@pytest.mark.parametrize(
    ("caddy_text", "match", "reason"),
    [
        ("SITE_HOST=research.example.test\nAI_NEWSDESK_HOST=News.Example.Test\n", True, None),
        ("export AI_NEWSDESK_HOST='news.example.test'  # ours\r\n", True, None),
        ("AI_NEWSDESK_HOST=other.example.test\n", False, "mismatch"),
        # Caddy's SITE_HOST belongs to research-engine: equal to ours is NOT a match
        ("SITE_HOST=news.example.test\n", False, "missing AI_NEWSDESK_HOST in caddy env"),
    ],
)
def test_health_compares_with_caddy_ai_newsdesk_host(
    fake_env: FakeEnv, caddy_text: str, match: bool, reason: str | None
) -> None:
    fake_env.caddy_env.write_text(caddy_text)
    r = fake_env.run("health")
    assert r.last["site_host_match"] is match and r.last["site_host_reason"] == reason
    assert (r.code == 0) is match
    assert "example.test" not in (r.stdout + r.stderr).lower()


def test_health_no_caddy_env_is_null_not_failure(fake_env: FakeEnv) -> None:
    fake_env.caddy_env.unlink()
    r = fake_env.run("health")
    assert r.code == 0 and r.last["site_host_match"] is None
    assert r.last["site_host_reason"] == "no caddy env"


@pytest.mark.parametrize("name", ["SECRETS_KEY", "SESSION_SECRET"])
@pytest.mark.parametrize("bad", ["change-me", "short-but-unique-value", None])
def test_health_flags_weak_secret(fake_env: FakeEnv, name: str, bad: str | None) -> None:
    env = fake_env.repo / ".env"
    lines = [ln for ln in env.read_text().splitlines() if not ln.startswith(f"{name}=")]
    if bad is not None:
        lines.append(f"{name}={bad}")
    env.write_text("\n".join(lines) + "\n")
    r = fake_env.run("health")
    assert r.code == 1 and r.last["weak_secrets"] == [name]
    assert any(name in p and "openssl rand -hex 32" in p for p in r.last["problems"])
    if bad is not None:
        assert bad not in r.stdout + r.stderr


def test_health_flags_missing_owner_password(fake_env: FakeEnv) -> None:
    env = fake_env.repo / ".env"
    env.write_text(
        "\n".join(ln for ln in env.read_text().splitlines() if "OWNER_PASSWORD_HASH" not in ln)
        + "\n"
    )
    r = fake_env.run("health")
    assert r.code == 1 and r.last["password_set"] is False
    assert any("make set-password" in p for p in r.last["problems"])


@pytest.mark.parametrize(("code", "status"), [("503", "down"), ("000", "unreachable")])
def test_health_app_not_up_fails(fake_env: FakeEnv, code: str, status: str) -> None:
    r = fake_env.run("health", FAKE_HEALTH_CODE=code)
    assert r.code == 1 and r.last["app_status"] == status


@pytest.mark.parametrize("fixture", ["inspect_unhealthy.tsv", "inspect_stopped.tsv"])
def test_health_container_not_healthy_fails(fake_env: FakeEnv, fixture: str) -> None:
    r = fake_env.run("health", FAKE_INSPECT_FILE=fixture)
    assert r.code == 1 and r.last["problems"] == ["app: not running and healthy"]


# ---- version -----------------------------------------------------------------------------------


def test_version(fake_env: FakeEnv) -> None:
    r = fake_env.run("version")
    assert r.code == 0, r.stderr
    assert r.last["ok"] is True and r.last["git_sha"] == "abc1234"
    assert r.last["images"][0]["repository"] == "ai-newsdesk-app"
    assert r.last["deployed"] is None  # no .deploy/current in the fixture


# ---- logs --------------------------------------------------------------------------------------


def test_logs_passthrough(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "app", "5m")
    assert r.code == 0, r.stderr
    assert r.last == {"command": "logs", "lines": 2, "ok": True, "service": "app"}
    assert any("logs --no-color --since 5m --tail 500 app" in c for c in fake_env.docker_calls())


def test_logs_redacts_secrets(fake_env: FakeEnv) -> None:
    r = fake_env.run("logs", "app", "5m", FAKE_LOGS_FILE="logs_secrets.txt")
    assert r.code == 0 and "SEKRIT" not in r.stdout + r.stderr
    assert "visible-message" in r.stdout


@pytest.mark.parametrize(("args", "code"), [(("nope",), 2), (("app", "null"), 2)])
def test_logs_bad_arguments_exit_2(fake_env: FakeEnv, args: tuple[str, ...], code: int) -> None:
    r = fake_env.run("logs", *args)
    assert r.code == code and r.last["ok"] is False and len(json_lines(r.stdout)) == 1


# ---- contract ----------------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["status", "health", "version", "logs"])
def test_docker_failure_still_one_json_line(fake_env: FakeEnv, cmd: str) -> None:
    r = fake_env.run(cmd, FAKE_FAIL="125")
    assert r.code == 1
    assert len(json_lines(r.stdout)) == 1 and r.last["ok"] is False and r.last["command"] == cmd
    _no_secrets(fake_env, r.stdout, r.stderr)


def test_only_the_ai_newsdesk_project_is_addressed(fake_env: FakeEnv) -> None:
    for cmd in ("status", "health", "version", "logs"):
        fake_env.run(cmd)
    compose = [c for c in fake_env.docker_calls() if c.startswith("compose")]
    assert compose and all(c.startswith("compose -p ai-newsdesk ") for c in compose)
    assert not any("prune" in c.split() for c in fake_env.docker_calls())


def test_inspect_never_requests_config_env(fake_env: FakeEnv) -> None:
    for cmd in ("status", "health"):
        fake_env.run(cmd)
    inspects = [c for c in fake_env.docker_calls() if c.startswith("inspect")]
    assert inspects and all(" --format " in f" {c} " for c in inspects)


def test_app_port_adds_debug_compose_file(fake_env: FakeEnv) -> None:
    fake_env.run("version")
    assert not any("compose.debug.yaml" in c for c in fake_env.docker_calls())
    env = fake_env.repo / ".env"
    env.write_text(env.read_text() + "APP_PORT=8123\n")
    fake_env.run("version")
    assert any("compose.debug.yaml" in c for c in fake_env.docker_calls())


def test_env_get_reads_the_single_quoted_hash_literally(fake_env: FakeEnv) -> None:
    p = lib(fake_env, "env_get OWNER_PASSWORD_HASH")
    assert p.stdout.rstrip("\n") == HASH


def test_json_out_keeps_strings_unless_typed(fake_env: FakeEnv) -> None:
    p = lib(fake_env, 'json_out a=null b=123 c:=123 d:=true e="x=y"')
    assert json.loads(p.stdout) == {"a": "null", "b": "123", "c": 123, "d": True, "e": "x=y"}


@pytest.mark.parametrize(
    "line",
    [
        "SECRETS_KEY=SEKRIT-1 tail",
        "anthropic_api_key: SEKRIT-1",
        '{"api_key": "SEKRIT-1", "x": 1}',
        "X-API-Key: SEKRIT-1",
        "Cookie: nd_session=SEKRIT-1",
        "OWNER_PASSWORD_HASH='$argon2id$SEKRIT-1'",
    ],
)
def test_redact_masks(fake_env: FakeEnv, line: str) -> None:
    p = lib(fake_env, "redact", stdin=line + "\n")
    assert "SEKRIT" not in p.stdout and "***REDACTED***" in p.stdout, p.stdout
