"""CI runs the checks that guard the house rules, without a Docker daemon (V1-S7)."""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW: dict[str, Any] = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def _steps() -> list[dict[str, Any]]:
    return [s for job in WORKFLOW["jobs"].values() for s in job.get("steps", [])]


def test_ci_runs_pytest() -> None:
    assert any(
        "pytest" in str(s.get("run", "")) or "make test" in str(s.get("run", "")) for s in _steps()
    )


def test_ci_syntax_checks_every_ops_script() -> None:
    assert any(
        "bash -n" in str(s.get("run", "")) and "ops/*.sh" in str(s.get("run", "")) for s in _steps()
    )


def test_ci_keeps_gitleaks() -> None:
    assert any("gitleaks" in str(s.get("uses", "")) + str(s.get("run", "")) for s in _steps())
