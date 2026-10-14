"""Dockerfile and .dockerignore policy (V1-S6): pinned, locked, non-root, healthy, lean context."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "python:3.13.16-slim-trixie"


def _directives() -> list[str]:
    """Dockerfile lines with comments dropped and continuations joined."""
    text = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _final_stage(lines: list[str]) -> list[str]:
    last = max(i for i, ln in enumerate(lines) if re.match(r"FROM\s", ln))
    return lines[last:]


def test_base_images_pinned() -> None:
    froms = [ln.split()[1] for ln in _directives() if re.match(r"FROM\s", ln)]
    assert froms == [BASE, BASE], froms


def test_uv_pinned_on_copy_from() -> None:
    assert any(
        ln.startswith("COPY") and "--from=ghcr.io/astral-sh/uv:0.12.23 " in ln
        for ln in _directives()
    )


def test_both_uv_syncs_locked() -> None:
    syncs = [ln for ln in _directives() if "uv sync" in ln]
    assert len(syncs) == 2
    assert all("uv sync --locked" in ln for ln in syncs), syncs


def test_final_stage_non_root_with_healthcheck() -> None:
    final = _final_stage(_directives())
    assert any(re.fullmatch(r"USER\s+10001", ln) for ln in final)
    assert not any(re.match(r"USER\s+(root|0)\b", ln) for ln in final)
    hc = [ln for ln in final if ln.startswith("HEALTHCHECK")]
    assert len(hc) == 1 and "http://127.0.0.1:8000/health" in hc[0]
    assert "EXPOSE 8000" in final


def test_data_dir_owned_by_app_user() -> None:
    final = " ".join(_final_stage(_directives()))
    assert "mkdir -p /data" in final and "chown 10001:10001 /data" in final
    assert "useradd --uid 10001 --gid 10001" in final


def test_git_sha_arg_reaches_env() -> None:
    final = _final_stage(_directives())
    assert "ARG GIT_SHA=unknown" in final
    assert any(ln.startswith("ENV") and "GIT_SHA=${GIT_SHA}" in ln for ln in final)


def test_cmd_runs_uvicorn_factory_behind_proxy() -> None:
    cmd = [ln for ln in _final_stage(_directives()) if ln.startswith("CMD")]
    assert len(cmd) == 1
    for part in (
        '"uvicorn"',
        '"ai_newsdesk.app:create_app"',
        '"--factory"',
        '"--host", "0.0.0.0"',
        '"--port", "8000"',
        '"--proxy-headers"',
        '"--forwarded-allow-ips", "*"',
    ):
        assert part in cmd[0], part


def test_no_unsafe_directives() -> None:
    lines = _directives()
    assert not any(re.match(r"ADD\s+https?://", ln) for ln in lines)
    assert not any(ln.startswith("COPY") and "--chown" in ln for ln in _final_stage(lines))


def test_tzdata_is_locked() -> None:
    """The slim image has no system zoneinfo: Europe/London needs the tzdata package."""
    assert re.search(r'^name = "tzdata"$', (ROOT / "uv.lock").read_text(), re.MULTILINE)


def test_dockerignore() -> None:
    ignore = [
        ln.strip()
        for ln in (ROOT / ".dockerignore").read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    env = [i for i, ln in enumerate(ignore) if ln.endswith(".env*")]
    assert env and "!.env.example" in ignore
    assert ignore.index("!.env.example") > max(env)
    for entry in (".git", "data", "backups", ".deploy", "deploys.jsonl", ".venv", "tests", "docs"):
        assert entry in ignore, entry
