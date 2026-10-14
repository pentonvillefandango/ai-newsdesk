"""`make deploy` and `make rollback`: worktrees, smoke gate, automatic rollback (V1-S7).

Each test builds a throwaway git repo in the fake env's repo directory. Docker is the fake from
conftest (`up` fails when FAKE_UP_FAIL_SHA equals the GIT_SHA it runs with); smoke is a fake
script (SMOKE_CMD) that fails for the sha in FAIL_SMOKE_SHA.
"""

import fcntl
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from .conftest import FakeEnv, Result, json_lines

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.test",
}
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

FAKE_SMOKE = """#!/usr/bin/env bash
printf '%s %s\\n' "$GIT_SHA" "$DEPLOY_DIR" >> "{log}"
if [ -n "${{FAIL_SMOKE_SHA:-}}" ] && [ "$GIT_SHA" = "$FAIL_SMOKE_SHA" ]; then
  echo '{{"ok": false, "command": "smoke", "error": "smoke failed"}}'
  exit 1
fi
echo '{{"ok": true, "command": "smoke"}}'
"""


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return p.stdout.strip()


def commit(repo: Path, content: str) -> str:
    (repo / "app.txt").write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", content)
    return git(repo, "rev-parse", "--short=12", "HEAD")


class Deployer:
    def __init__(self, fake_env: FakeEnv, tmp_path: Path) -> None:
        self.env = fake_env
        self.repo = fake_env.repo
        self.log = self.repo / "deploys.jsonl"
        self.log.unlink()
        (self.repo / ".gitignore").write_text(".env\n.deploy/\ndeploys.jsonl\nbackups/\n")
        git(self.repo, "init", "-q", "-b", "main")
        self.smoke_log = tmp_path / "smoke.log"
        script = tmp_path / "fake_smoke.sh"
        script.write_text(FAKE_SMOKE.format(log=self.smoke_log))
        script.chmod(0o755)
        self.smoke_cmd = str(script)

    def run(self, cmd: str, **env: str) -> Result:
        r = self.env.run(cmd, SMOKE_CMD=self.smoke_cmd, **env)
        assert len(json_lines(r.stdout)) == 1, (r.stdout, r.stderr)
        return r

    def entries(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        out = [json.loads(line) for line in self.log.read_text().splitlines()]
        assert all(TS.match(str(e["ts"])) for e in out)
        return out

    def current(self) -> str:
        link = self.repo / ".deploy" / "current"
        assert link.is_symlink()
        return git(link.resolve(), "rev-parse", "--short=12", "HEAD")

    def snapshot(self) -> tuple[str, str, str]:
        return (
            git(self.repo, "rev-parse", "HEAD"),
            git(self.repo, "status", "--porcelain"),
            (self.repo / "app.txt").read_text(),
        )

    def up_shas(self) -> list[str]:
        f = self.env.calls.with_name("calls.log.up")
        return f.read_text().split() if f.exists() else []


@pytest.fixture
def dep(fake_env: FakeEnv, tmp_path: Path) -> Deployer:
    return Deployer(fake_env, tmp_path)


# ---- preconditions -----------------------------------------------------------------------------


def test_deploy_refuses_dirty_tree(dep: Deployer) -> None:
    commit(dep.repo, "v1")
    (dep.repo / "app.txt").write_text("edited")
    r = dep.run("deploy")
    assert r.code == 2 and r.last["error"] == "working tree has uncommitted changes"
    assert dep.env.docker_calls() == [] and dep.entries() == []


@pytest.mark.parametrize("name", ["SECRETS_KEY", "SESSION_SECRET"])
def test_deploy_refuses_weak_secret(dep: Deployer, name: str) -> None:
    commit(dep.repo, "v1")
    env = dep.repo / ".env"
    lines = [ln for ln in env.read_text().splitlines() if not ln.startswith(f"{name}=")]
    env.write_text("\n".join([*lines, f"{name}=change-me"]) + "\n")
    r = dep.run("deploy")
    assert r.code == 2 and r.last["weak_secrets"] == [name]
    assert dep.env.docker_calls() == [] and dep.entries() == []


def test_deploy_and_rollback_refuse_while_locked(dep: Deployer) -> None:
    commit(dep.repo, "v1")
    (dep.repo / ".deploy").mkdir()
    with (dep.repo / ".deploy" / "lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for cmd in ("deploy", "rollback"):
            r = dep.run(cmd)
            assert r.code == 2 and "another deploy or rollback is running" in str(r.last["error"])
    assert dep.up_shas() == []


# ---- success -----------------------------------------------------------------------------------


def test_successful_deploy(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    before = dep.snapshot()
    r = dep.run("deploy")
    assert r.code == 0, (r.stdout, r.stderr)
    assert r.last == {"command": "deploy", "from": "none", "ok": True, "smoke": "pass", "to": c1}
    wt = dep.repo / ".deploy" / c1
    assert dep.current() == c1 and (wt / "app.txt").read_text() == "v1"
    [up] = [c for c in dep.env.docker_calls() if " up -d " in c]
    assert up.startswith("compose -p ai-newsdesk ") and f"--project-directory {wt}" in up
    assert up.endswith("up -d --build --wait --wait-timeout 300")
    assert dep.up_shas() == [c1]
    assert dep.smoke_log.read_text().split() == [c1, str(wt)]
    [entry] = dep.entries()
    assert (entry["action"], entry["to"], entry["result"], entry["smoke"]) == (
        "deploy", c1, "ok", "pass",
    )  # fmt: skip
    assert "sandbox" not in entry
    assert dep.snapshot() == before
    for secret in dep.env.secrets:
        assert secret not in r.stdout + r.stderr + dep.log.read_text()


def test_deploy_from_is_last_ok_to(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    c2 = commit(dep.repo, "v2")
    r = dep.run("deploy")
    assert r.code == 0 and (r.last["from"], r.last["to"]) == (c1, c2)


# ---- failure and automatic rollback -----------------------------------------------------------


@pytest.mark.parametrize(
    ("fail_env", "smoke"), [("FAIL_SMOKE_SHA", "fail"), ("FAKE_UP_FAIL_SHA", "skipped")]
)
def test_failed_deploy_rolls_back(dep: Deployer, fail_env: str, smoke: str) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    c2 = commit(dep.repo, "v2")
    before = dep.snapshot()
    r = dep.run("deploy", **{fail_env: c2})
    assert r.code == 1 and r.last["ok"] is False
    assert r.last["rolled_back_to"] == c1 and r.last["rollback_ok"] is True
    branch = str(r.last["rollback_branch"])
    assert re.fullmatch(r"rollback/\d{8}T\d{6}Z", branch)
    assert git(dep.repo, "rev-parse", "--short=12", branch) == c1
    assert dep.current() == c1 and dep.up_shas() == [c1, c2, c1]
    _, failed, rolled = dep.entries()
    assert (failed["result"], failed["smoke"]) == ("failed", smoke) and failed["detail"]
    assert (rolled["action"], rolled["from"], rolled["to"], rolled["result"]) == (
        "rollback", c2, c1, "ok",
    )  # fmt: skip
    assert dep.snapshot() == before


def test_failed_first_deploy_leaves_stack_up(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    r = dep.run("deploy", FAIL_SMOKE_SHA=c1)
    assert r.code == 1 and r.last["rolled_back_to"] is None and r.last["from"] == "none"
    assert dep.current() == c1 and dep.up_shas() == [c1]
    assert git(dep.repo, "branch", "--list", "rollback/*") == ""


def test_failed_redeploy_of_same_sha_does_not_roll_back_to_itself(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    r = dep.run("deploy", FAIL_SMOKE_SHA=c1)
    assert r.code == 1 and r.last["rolled_back_to"] is None
    assert dep.up_shas() == [c1, c1]


# ---- rollback.sh -------------------------------------------------------------------------------


def test_rollback_to_last_good_and_back(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    c2 = commit(dep.repo, "v2")
    assert dep.run("deploy").code == 0
    r = dep.run("rollback")
    assert r.code == 0, (r.stdout, r.stderr)
    assert (r.last["from"], r.last["to"], r.last["smoke"]) == (c2, c1, "pass")
    assert dep.current() == c1
    assert dep.run("rollback").last["to"] == c2 and dep.current() == c2


def test_rollback_to_explicit_sha_prefix(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    commit(dep.repo, "v2")
    assert dep.run("deploy").code == 0
    r = dep.run("rollback", SHA=c1[:7])
    assert r.code == 0 and r.last["to"] == c1 and dep.current() == c1


@pytest.mark.parametrize("sha", ["deadbee", "not-a-sha;id", "HEAD"])
def test_rollback_rejects_unknown_sha(dep: Deployer, sha: str) -> None:
    commit(dep.repo, "v1")
    r = dep.run("rollback", SHA=sha)
    assert r.code == 2 and dep.up_shas() == [] and dep.entries() == []


def test_rollback_without_candidate_is_exit_2(dep: Deployer) -> None:
    commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    r = dep.run("rollback")
    assert r.code == 2 and "no previous good deploy" in str(r.last["error"])


def test_failed_rollback_is_recorded(dep: Deployer) -> None:
    c1 = commit(dep.repo, "v1")
    assert dep.run("deploy").code == 0
    commit(dep.repo, "v2")
    assert dep.run("deploy").code == 0
    r = dep.run("rollback", FAIL_SMOKE_SHA=c1)
    assert r.code == 1 and r.last["to"] == c1
    assert dep.entries()[-1]["result"] == "failed"


# ---- retention ---------------------------------------------------------------------------------


def test_prunes_worktrees_and_images_by_explicit_tag_only(dep: Deployer) -> None:
    shas: list[str] = []
    for i in range(5):
        shas.append(commit(dep.repo, f"v{i}"))
        tags = " ".join([*shas, "dev", "latest"])
        r = dep.run("deploy", FAKE_IMAGE_TAGS=tags, FAKE_PS_IMAGES="caddy:2.11.6")
        assert r.code == 0, (r.stdout, r.stderr)
    kept = sorted(
        p.name for p in (dep.repo / ".deploy").iterdir() if p.is_dir() and not p.is_symlink()
    )
    assert kept == sorted(shas[1:])
    calls = dep.env.docker_calls()
    assert [c for c in calls if c.startswith("image rm")] == [f"image rm ai-newsdesk-app:{shas[0]}"]
    assert not any("prune" in c.split() for c in calls)
