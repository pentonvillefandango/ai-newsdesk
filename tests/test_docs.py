"""Docs honesty (V1-S7): the docs name what the code really has and leak no lab values."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
OPERATIONS = ROOT / "docs" / "OPERATIONS.md"
CLAUDE = ROOT / "CLAUDE.md"
ADR_DIR = ROOT / "docs" / "adr"

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}  # noqa: S104 - a text allow-list, not a bind address
HOME_PATH = re.compile(r"/home/[\w.-]+")
ARPA = re.compile(r"[\w-]+(?:\.[\w-]+)*\.home\.arpa")
ALLOWED_HOSTS = {
    "toolbox.home.arpa",
    "ai-newsdesk.toolbox.home.arpa",
    "research.toolbox.home.arpa",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _make_targets() -> list[str]:
    return re.findall(r"^([a-z][a-z0-9-]*):", _read(ROOT / "Makefile"), flags=re.MULTILINE)


def _env_example_keys() -> list[str]:
    return re.findall(r"^([A-Z][A-Z0-9_]*)=", _read(ROOT / ".env.example"), flags=re.MULTILINE)


def _scanned_docs() -> list[Path]:
    return [README, CLAUDE, OPERATIONS, *sorted(ADR_DIR.glob("*.md"))]


# ---- README ------------------------------------------------------------------------------------


def test_readme_config_table_lists_every_env_example_key() -> None:
    table = "\n".join(ln for ln in _read(README).splitlines() if ln.lstrip().startswith("|"))
    keys = _env_example_keys()
    assert len(keys) >= 16  # the parse saw the file
    missing = [k for k in keys if f"`{k}`" not in table]
    assert not missing, missing


def test_readme_has_quick_start_and_links_operations() -> None:
    text = _read(README)
    assert re.search(r"^## Quick start$", text, re.MULTILINE)
    assert "(docs/OPERATIONS.md)" in text


# ---- OPERATIONS --------------------------------------------------------------------------------


def test_operations_mentions_every_make_target() -> None:
    text = _read(OPERATIONS)
    targets = _make_targets()
    assert {"deploy", "rollback", "backup", "set-password", "health"} <= set(targets)
    missing = [t for t in targets if f"make {t}" not in text]
    assert not missing, missing


def test_operations_covers_contract_approvals_and_troubleshooting() -> None:
    text = _read(OPERATIONS)
    for needle in (
        "one JSON object",  # the JSON-line contract
        "exit **0**",
        "**2**",  # exit codes
        "Approval required",
        "secure DNS",  # Chrome's secure DNS bypasses the lab resolver
        "403 by design",
        "AI_NEWSDESK_HOST",
        "-p ai-newsdesk-dev",
        "SECRETS_KEY",
    ):
        assert needle in text, needle


# ---- CLAUDE.md ---------------------------------------------------------------------------------


def test_claude_md_rules() -> None:
    text = _read(CLAUDE)
    for needle in (
        "HANDOFF.local.md",
        "Compose project `ai-newsdesk`",
        "public",
        "No paid X",
        "superpowers-extended-cc",
        "docs/OPERATIONS.md",
    ):
        assert needle in text, needle


# ---- lab values --------------------------------------------------------------------------------


def test_scan_helpers_catch_lab_values() -> None:
    assert IPV4.findall("host 10.1.2.3 here") == ["10.1.2.3"]
    assert IPV4.findall("python 3.13.16 and uv 0.12.23") == []
    assert HOME_PATH.search("see /home/someone/x")
    assert ARPA.findall("x.toolbox.home.arpa") == ["x.toolbox.home.arpa"]


def test_no_lab_ips_home_paths_or_real_hosts_in_docs() -> None:
    problems: list[str] = []
    for path in _scanned_docs():
        text = _read(path)
        rel = path.relative_to(ROOT)
        problems += [f"{rel}: {ip}" for ip in IPV4.findall(text) if ip not in ALLOWED_IPS]
        problems += [f"{rel}: {m}" for m in HOME_PATH.findall(text)]
        problems += [f"{rel}: {h}" for h in ARPA.findall(text) if h not in ALLOWED_HOSTS]
    assert not problems, problems
