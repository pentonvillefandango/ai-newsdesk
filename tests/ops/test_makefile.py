"""Makefile: every ops target, help, and no injectable variables (V1-S7)."""

import os
import re
import subprocess

import pytest

from .conftest import ROOT

TARGETS = {
    "help", "deploy", "rollback", "status", "health", "smoke", "logs", "backup", "version",
    "set-password", "test",
}  # fmt: skip


def _make(*args: str) -> str:
    # Drop make's own variables so a run under `make test` is not treated as a sub-make.
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MAKELEVEL", "MFLAGS"}}
    return subprocess.run(
        ["make", *args], cwd=ROOT, capture_output=True, text=True, check=True, env=env
    ).stdout


def test_phony_lists_every_target() -> None:
    text = (ROOT / "Makefile").read_text()
    phony = re.search(r"^\.PHONY:(.*)$", text, re.M)
    assert phony and set(phony.group(1).split()) == TARGETS
    assert set(re.findall(r"^([a-z][a-z0-9-]*):", text, re.M)) == TARGETS


def test_help_lists_every_target() -> None:
    out = _make("help")
    for t in TARGETS:
        assert re.search(rf"^{re.escape(t)}\t", out, re.M), t


@pytest.mark.parametrize(
    ("target", "script"),
    [
        ("deploy", "ops/deploy.sh"),
        ("rollback", "ops/rollback.sh"),
        ("status", "ops/status.sh"),
        ("health", "ops/health.sh"),
        ("smoke", "ops/smoke.sh"),
        ("backup", "ops/backup.sh"),
        ("version", "ops/version.sh"),
        ("set-password", "ops/set_password.sh"),
    ],
)
def test_target_runs_its_script(target: str, script: str) -> None:
    assert _make("-n", target).strip() == script


def test_test_target_is_the_house_check() -> None:
    assert _make("-n", "test").strip() == (
        "uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest"
    )


@pytest.mark.parametrize(
    ("target", "var"), [("logs", "SERVICE"), ("logs", "SINCE"), ("rollback", "SHA")]
)
def test_vars_are_not_injectable(target: str, var: str) -> None:
    evil = 'a"; echo INJECTED; "$(id)`id`'
    out = _make("-n", target, f"{var}={evil}")
    assert "INJECTED" not in out and "uid=" not in out
    text = (ROOT / "Makefile").read_text()
    assert re.search(rf"^export .*\b{var}\b", text, re.M)
