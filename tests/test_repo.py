"""Repo tooling guards: the package installs, and secrets stay out of git, images and CI."""

from importlib.metadata import version
from pathlib import Path

import yaml

import ai_newsdesk

ROOT = Path(__file__).resolve().parent.parent


def _lines(name: str) -> set[str]:
    return {line.strip() for line in (ROOT / name).read_text().splitlines()}


def test_package_version_matches_metadata() -> None:
    assert ai_newsdesk.__version__ == version("ai-newsdesk") == "0.1.0"


def test_gitignore_keeps_secrets_and_data_out() -> None:
    assert {
        ".env",
        ".env.*",
        "!.env.example",
        "data/",
        "backups/",
        "*.sqlite",
        "*.local.md",
    } <= _lines(".gitignore")


def test_dockerignore_keeps_secrets_and_data_out() -> None:
    assert {".env", ".env.*", ".git", "data", "backups", "*.sqlite", "tests"} <= _lines(
        ".dockerignore"
    )


def test_precommit_runs_gitleaks() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook_ids = {hook["id"] for repo in config["repos"] for hook in repo["hooks"]}
    assert {"gitleaks", "detect-private-key", "check-added-large-files"} <= hook_ids


def test_ci_runs_make_test_and_gitleaks() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    runs = [step.get("run") for step in ci["jobs"]["lint-test"]["steps"]]
    assert "uv sync --locked" in runs
    assert "make test" in runs
    uses = [step.get("uses", "") for step in ci["jobs"]["gitleaks"]["steps"]]
    assert any(u.startswith("gitleaks/gitleaks-action@") for u in uses)
    assert ci["permissions"] == {"contents": "read"}


def test_makefile_test_target_matches_global_constraint() -> None:
    text = (ROOT / "Makefile").read_text()
    assert (
        "uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest" in text
    )
