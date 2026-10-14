# Step 0: Scaffold (repo tooling, config, models, protocols, clock, fakes)

> Part of `docs/plans/2026-10-14-ai-newsdesk-v1.md`. Read that file first, for the Global Constraints and the shared contract, and also read the spec and ADR-0001. Steps use checkbox (`- [ ]`) syntax for tracking.

**Step goal:** By the end of this step, the repo is a working uv project. CI runs `make test` and gitleaks, and pre-commit runs gitleaks. The code holds the process `Settings`, every shared model and protocol from the contract, the clocks, and a set of protocol-true fakes and fixtures that later steps test against.

**Branch:** `step-0-scaffold`. **Depends on:** nothing; this is the first step. Every later step builds on it, and step 6 depends only on it.

**Task order:** 1 → 2 → 3 → 4. Each task is one commit.

**House patterns:** these copy research-engine's tooling: its `pyproject.toml` lint, type and pytest configuration, its gitleaks pre-commit, its CI layout, and its `Settings` secret validation. They are adapted to a single uv package at `src/ai_newsdesk`, not a workspace.

---

## Contract additions

Other steps may rely on these names, in addition to the master contract.

- **Package and tests layout.** Code imports as `ai_newsdesk.*`. `tests/` is a package (`tests/__init__.py`), so test files import shared helpers as `from tests.fakes import ...` and `from tests.conftest import ...`.
- **`ai_newsdesk.__version__`**: a `str` read from the package metadata (`0.1.0`).
- **`config.Settings`:**
  - Its attributes are the lower-case env var names, all plain `str` or `bool` as the contract table says: `site_host`, `secrets_key`, `session_secret`, `owner_password_hash`, `anthropic_api_key`, `research_engine_api_key`, `typesafe_api_key`, `research_engine_url`, `db_path`, `tz`, `log_level`, `git_sha`, `scheduler_enabled`.
  - Secret fields are declared `repr=False`, so `repr()` and `str()` never show them.
  - `env_file=None`: Compose injects `.env`, so nothing reads the file.
  - Validation: `SECRETS_KEY` and `SESSION_SECRET` must be at least 32 characters and not `change-me`. `OWNER_PASSWORD_HASH` must be empty or start with `$argon2`. `TZ` must be a valid IANA zone. `LOG_LEVEL` is upper-cased and must be a standard level. Errors never echo the input.
  - Extra members: property `Settings.zone -> zoneinfo.ZoneInfo`; property `Settings.login_enabled -> bool` (true when `owner_password_hash` is set); `config.get_settings() -> Settings`, cached with `lru_cache` (tests call `get_settings.cache_clear()`); constants `PLACEHOLDER_SECRET = "change-me"` and `MIN_SECRET_LENGTH = 32`.
- **`models.py`** is the contract verbatim. Long trailing comments are moved onto their own lines to fit ruff's 100-column limit, and `Story.model_rebuild()` sits at the end of the module.
- **`clock.py`:**
  - `SystemClock()`.
  - `FixedClock(at: datetime)`, with `.now()`, `.set(at: datetime) -> None` and `.advance(delta: timedelta) -> datetime`. It refuses naive datetimes (`ValueError`) and always returns UTC.
- **`tests/fakes.py`** (exact constructor signatures; every argument is optional):
  - `FAKE_MODEL = "claude-haiku-5-5"`.
  - `utc(year, month, day, hour=0, minute=0) -> datetime`: an aware UTC datetime.
  - `make_source_config(id=1, kind=SourceKind.FEED, name="Example Lab blog", config=None, trust=Trust.PRIMARY, enabled=True, weight=1.0) -> SourceConfig`. `config=None` means `{"url": "https://lab.example.com/feed.xml"}`.
  - `make_candidate(source_id=1, url="https://lab.example.com/news/model-x", title="Example Lab releases Model X", author=None, published_at=None, snippet="Model X is a new open-weights model.", hn_points=None) -> Candidate`.
  - `make_review_input(item_id=1, title=..., text=..., source_name="Example Lab blog", source_kind=SourceKind.FEED, trust=Trust.PRIMARY) -> ReviewInput`.
  - `FakeResearchEngine(search_results: dict[str, list[SearchHit]] | None = None, pages: dict[str, str] | None = None, healthy: bool = True, search_error: Exception | None = None)`.
    - It records `.search_calls: list[tuple[query, max_results, time_range]]` and `.fetch_calls: list[str]`.
    - `search` raises `search_error` when one is set; otherwise it returns `search_results.get(query, [])[:max_results]`.
    - `fetch_markdown` returns `pages.get(url)`.
  - `FakeSource(kind: SourceKind = SourceKind.FEED, candidates: list[Candidate] | None = None, results: dict[int, SourceResult] | None = None, error: str | None = None)`.
    - It records `.calls: list[tuple[source_id, since]]`.
    - Precedence: `results[src.id]` first; then an error result if `error` is set; otherwise `candidates` re-homed to `src.id`.
  - `FakeJudge(model=FAKE_MODEL, verdicts: dict[int, Verdict] | None = None, fail_ids: set[int] | None = None, default_signal: int = 6, default_category: Category = Category.OTHER, match_first_candidate: bool = False, cost_per_item: float = 0.001)`.
    - It records `.calls: list[list[ReviewInput]]`.
    - It returns one `Usage` per non-empty call: provider `anthropic`, purpose `review`, 100 input and 20 output tokens per item.
    - `worst_case_cost = cost_per_item × len(inputs)`.
  - `FakeWriter(model=FAKE_MODEL, fail_story_ids: set[int] | None = None, briefing_body: str | None = "Today's briefing.", cost_per_story: float = 0.001, briefing_cost: float = 0.002)`.
    - Summaries are `"Summary: <headline>"`. Why-it-matters is `"Why: <headline>"` when `include_why` is set, else `None`.
    - It records `.story_calls` and `.briefing_calls: list[tuple[list[BriefingStory], date_label]]`.
    - `write_briefing([])` returns `(None, [])`.
- **`tests/conftest.py` fixtures:**
  - `clean_env` (a monkeypatch with every Settings env var removed, cwd set to the repo root).
  - `settings_factory` (`(**field_overrides: str) -> Settings`; overrides go through the environment).
  - `db_path` (`str`, `tmp_path/"data"/"test.sqlite"`, not yet created).
  - `now` (`TEST_NOW`).
  - `clock` (`FixedClock(TEST_NOW)`).
  - Constants: `ROOT`, `TEST_SECRETS_KEY`, `TEST_SESSION_SECRET` and `TEST_NOW = utc(2026, 10, 14, 12, 0)`.
  - Step 1 adds a `store` fixture.
- **Dependencies:** `tzdata` is added to the Tech Stack list. `python:3.13-slim-trixie` ships no system zoneinfo database, and `Europe/London` must resolve in the container.

---

### Task 1: Repo tooling, CI and gitleaks

**Goal:** Make the repo a uv project with research-engine's lint, type and test settings. `make test` runs the full gate, CI runs it plus gitleaks, pre-commit runs gitleaks locally, and `.env.example` names every `Settings` key with placeholder values.

**Files:**
- Create: `pyproject.toml`, `uv.lock` (generated), `Makefile`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `.dockerignore`, `.env.example`, `src/ai_newsdesk/__init__.py`, `tests/__init__.py`
- Keep unchanged: `.gitignore`, which already exists and covers `.env`, data, backups and `*.local.md`.
- Test: `tests/test_repo.py`

**Acceptance Criteria:**
- [ ] `uv sync --locked` succeeds from a clean checkout. `uv.lock` is committed.
- [ ] `ai_newsdesk.__version__ == "0.1.0"`.
- [ ] `make test` runs `uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest` and exits 0.
- [ ] `.github/workflows/ci.yml` has a `lint-test` job (`uv sync --locked`, `make test`) and a `gitleaks` job. Workflow permissions are `contents: read`.
- [ ] `uv run pre-commit run --all-files` prints `Detect hardcoded secrets....Passed`.
- [ ] `.env.example` lists all 13 `Settings` variables. Secrets are `change-me` or empty, and no lab IPs, real hostnames or usernames appear.
- [ ] `.dockerignore` excludes `.env`, `.git`, `data`, `backups`, `*.sqlite` and `tests`.

**Verify:** `make test` → `6 passed`, and `uv run pre-commit run --all-files` → every hook `Passed`.

**Steps:**

- [ ] **Step 1: Create the branch.**

```bash
git switch main && git pull --ff-only && git switch -c step-0-scaffold
```

- [ ] **Step 2: Create `pyproject.toml`.** The dependencies are the master plan's Tech Stack, plus `tzdata`, with lower bounds at today's releases.

```toml
[project]
name = "ai-newsdesk"
version = "0.1.0"
description = "Self-hosted, high-signal AI news feed with a daily briefing"
readme = "README.md"
requires-python = ">=3.12"
license = "MIT"
dependencies = [
    "aiosqlite>=0.22.1",
    "anthropic>=1.13.0",
    "argon2-cffi>=25.1.0",
    "cryptography>=50.0.2",
    "fastapi>=0.142.2",
    "feedparser>=6.0.14",
    "httpx>=0.28.1",
    "itsdangerous>=2.2.0",
    "jinja2>=3.1.6",
    "pydantic>=2.13.5",
    "pydantic-settings>=2.15.0",
    "python-multipart>=0.0.32",
    "pyyaml>=6.0.3",
    # python:3.13-slim-trixie ships no system zoneinfo; Europe/London must resolve
    "tzdata>=2026.5",
    "uvicorn[standard]>=0.54.0",
]

[project.optional-dependencies]
jev = ["typesafe-sdk>=0.7.4"]

[dependency-groups]
dev = [
    "pytest>=9.1.1",
    "pytest-asyncio>=1.4.0",
    "respx>=0.23.1",
    "ruff>=0.16.10",
    "pyright>=1.1.414",
    "pre-commit>=4.6.2",
    "types-pyyaml>=6.0.12.20260906",
]

[build-system]
requires = ["uv_build>=0.12,<0.13"]
build-backend = "uv_build"

[tool.pytest]
minversion = "9.0"
faulthandler_timeout = "300"
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
markers = ["integration: makes real network calls (run with -m integration)"]
addopts = ["-m", "not integration", "--strict-markers", "-ra"]

[tool.ruff]
line-length = 100
target-version = "py312"
# docs/ holds plan/ADR markdown with illustrative snippets, not project source
extend-exclude = ["docs"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "ASYNC", "S", "SIM", "RUF"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "S105", "S106"]

[tool.pyright]
pythonVersion = "3.12"
typeCheckingMode = "standard"
include = ["src", "tests"]
venvPath = "."
venv = ".venv"
```

- [ ] **Step 3: Create the package and test-package markers, then lock and sync.** `uv_build` needs the module to exist before `uv sync` can install it in editable mode.

`src/ai_newsdesk/__init__.py`:

```python
"""ai-newsdesk: a self-hosted, high-signal AI news feed."""

from importlib.metadata import version

__version__ = version("ai-newsdesk")
```

`tests/__init__.py`: an empty file.

```bash
mkdir -p src/ai_newsdesk tests && touch tests/__init__.py
uv lock && uv sync --locked
```

Expected: `Resolved NN packages`, and the sync installs `ai-newsdesk==0.1.0` (editable).

- [ ] **Step 4: Write the failing test** `tests/test_repo.py`.

```python
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
```

- [ ] **Step 5: Run it.** `uv run pytest tests/test_repo.py -v`. Expected: FAIL. `test_dockerignore_keeps_secrets_and_data_out`, `test_precommit_runs_gitleaks`, `test_ci_runs_make_test_and_gitleaks` and `test_makefile_test_target_matches_global_constraint` each fail with `FileNotFoundError`. The version and `.gitignore` tests pass.

- [ ] **Step 6: Create `Makefile`.** Recipe lines start with a **tab**. Later steps (6 and 7) add the ops targets; this step adds only `help` and `test`.

```makefile
.PHONY: help test

help:            ## List make targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:[ ]*## /\t/'
test:            ## Lint, format check, type-check and unit tests
	@uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest
```

- [ ] **Step 7: Create `.pre-commit-config.yaml`.** Its `rev`s match research-engine's. The `exclude` covers the htmx file that step 5 vendors.

```yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.30.1
    hooks:
      - id: gitleaks
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: check-added-large-files
        args: ["--maxkb=2048"]
      - id: check-merge-conflict
      - id: end-of-file-fixer
        exclude: ^src/ai_newsdesk/web/static/htmx\.min\.js$  # byte-exact vendored file
      - id: trailing-whitespace
        exclude: ^src/ai_newsdesk/web/static/htmx\.min\.js$  # byte-exact vendored file
      - id: detect-private-key
```

- [ ] **Step 8: Create `.github/workflows/ci.yml`.**

```yaml
name: CI
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  lint-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10.2.0
        with:
          version: "0.12.23"
          enable-cache: true
      - run: uv sync --locked
      - run: make test
        env:
          PYTHONFAULTHANDLER: "1"
  gitleaks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - uses: gitleaks/gitleaks-action@v3
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GITLEAKS_VERSION: "8.30.1"
```

- [ ] **Step 9: Create `.dockerignore`.**

```gitignore
# Build context: only what the image needs (pyproject, uv.lock, src/, config/).
.git
.github
.venv
.env
.env.*
!.env.example
**/__pycache__
**/*.py[cod]
.pytest_cache
.ruff_cache
.coverage
htmlcov
dist
build
data
backups
logs
*.sqlite
*.sqlite-wal
*.sqlite-shm
*.log
.deploy
.claude
docs
tests
ops
*.local.md
```

- [ ] **Step 10: Create `.env.example`.** Every `Settings` key appears here with a placeholder. Step 6 appends `APP_MEM_LIMIT` and `APP_CPUS`. Task 2's tests guard this file.

```dotenv
# ai-newsdesk configuration. Copy to .env, chmod 600, and edit. Never commit .env.
# Run-time tunables (frequency, caps, models, thresholds) live in the Settings page, not here.

# ---- Core ----
# Public hostname served by the shared Caddy (example deployment: ai-newsdesk.toolbox.home.arpa)
SITE_HOST=ai-newsdesk.localhost
# Derives the key that encrypts API keys saved on the Settings page. openssl rand -hex 32
SECRETS_KEY=change-me
# Signs the session cookie. openssl rand -hex 32
SESSION_SECRET=change-me
# argon2 hash of the owner password, made by `make set-password`. Empty disables login.
OWNER_PASSWORD_HASH=

# ---- API keys (fallbacks: a key saved on the Settings page wins) ----
ANTHROPIC_API_KEY=
RESEARCH_ENGINE_API_KEY=
# Only needed when the optional Jev judge is selected
TYPESAFE_API_KEY=

# ---- Services and storage ----
RESEARCH_ENGINE_URL=http://research-engine-app:8000
# Compose sets /data/ai-newsdesk.sqlite on the named volume
DB_PATH=data/ai-newsdesk.sqlite

# ---- Runtime ----
# Schedules and the budget month use this zone
TZ=Europe/London
LOG_LEVEL=INFO
# Set by `make deploy`; shown on /health
GIT_SHA=unknown
SCHEDULER_ENABLED=true
```

- [ ] **Step 11: Run the tests and the full gate.** Run `uv run pytest tests/test_repo.py -v` (expected: `6 passed`), then `make test` (expected: ruff `All checks passed!`, pyright `0 errors`, pytest `6 passed`).

- [ ] **Step 12: Install and run the hooks.** Run `uv run pre-commit install`, then `git add -A && uv run pre-commit run --all-files`. Expected: every hook `Passed`. If `end-of-file-fixer` or `trailing-whitespace` rewrites a file, re-run it until every hook passes.

- [ ] **Step 13: Commit.**

```bash
git add pyproject.toml uv.lock Makefile .pre-commit-config.yaml .github/workflows/ci.yml \
  .dockerignore .env.example src/ai_newsdesk/__init__.py tests/__init__.py tests/test_repo.py
git commit -m "chore(repo): uv project, lint/type/test tooling, CI and gitleaks (V1-S0)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `Settings` (environment configuration)

**Goal:** Add `config.Settings`, read once from the environment, with every field in the contract table. It refuses weak or placeholder secrets, bad password hashes, unknown time zones and unknown log levels, and it never shows a secret in a repr or an error.

**Files:**
- Create: `src/ai_newsdesk/config.py`, `tests/conftest.py`
- Test: `tests/test_config.py`

**Acceptance Criteria:**
- [ ] With only `SECRETS_KEY` and `SESSION_SECRET` set, every field has the contract default. `settings.zone == ZoneInfo("Europe/London")` and `login_enabled is False`.
- [ ] A missing, short (31-character) or `change-me` value for `SECRETS_KEY` or `SESSION_SECRET` raises `ValidationError`. The message names the variable and never contains the value.
- [ ] `OWNER_PASSWORD_HASH=plaintext-password` is refused. A `$argon2id$...` value is accepted and sets `login_enabled`.
- [ ] `TZ=Mars/Olympus_Mons` and `LOG_LEVEL=LOUD` are refused. `LOG_LEVEL=debug` becomes `DEBUG`.
- [ ] `repr(settings)` and `str(settings)` contain no secret field values.
- [ ] `.env.example` names every `Settings` field. Any key containing KEY, SECRET, HASH, TOKEN or PASSWORD is `""` or `change-me`.

**Verify:** `uv run pytest tests/test_config.py -v` → `15 passed`

**Steps:**

- [ ] **Step 1: Write the shared fixtures** in `tests/conftest.py`. Task 4 extends this file.

```python
"""Shared pytest fixtures."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ai_newsdesk.config import Settings, get_settings

ROOT = Path(__file__).resolve().parent.parent

# Fake secrets, built at runtime (nothing for gitleaks to flag) and long enough for the
# 32-character minimum.
TEST_SECRETS_KEY = "test-secrets-key-" + "0" * 32
TEST_SESSION_SECRET = "test-session-secret-" + "1" * 32


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """No Settings variable leaks in from the developer's shell (TZ is often set)."""
    monkeypatch.chdir(ROOT)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def settings_factory(clean_env: pytest.MonkeyPatch) -> Callable[..., Settings]:
    """Build a Settings from a clean environment plus the two required test secrets.

    Keyword overrides use field names (``tz="UTC"``); they are set as environment variables
    so they go through the same parsing and validation as production.
    """

    def make(**overrides: str) -> Settings:
        env = {"SECRETS_KEY": TEST_SECRETS_KEY, "SESSION_SECRET": TEST_SESSION_SECRET}
        env.update({k.upper(): v for k, v in overrides.items()})
        for k, v in env.items():
            clean_env.setenv(k, v)
        return Settings()  # type: ignore[call-arg]

    return make
```

- [ ] **Step 2: Write the failing tests** in `tests/test_config.py`.

```python
from collections.abc import Callable
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ai_newsdesk.config import Settings, get_settings
from tests.conftest import ROOT, TEST_SECRETS_KEY, TEST_SESSION_SECRET

SettingsFactory = Callable[..., Settings]


def test_defaults(settings_factory: SettingsFactory) -> None:
    s = settings_factory()
    assert s.site_host == "ai-newsdesk.localhost"
    assert s.research_engine_url == "http://research-engine-app:8000"
    assert s.db_path == "data/ai-newsdesk.sqlite"
    assert s.tz == "Europe/London"
    assert s.zone == ZoneInfo("Europe/London")
    assert s.log_level == "INFO"
    assert s.git_sha == "unknown"
    assert s.scheduler_enabled is True
    assert s.owner_password_hash == ""
    assert s.login_enabled is False
    assert (s.anthropic_api_key, s.research_engine_api_key, s.typesafe_api_key) == ("", "", "")


def test_reads_environment(settings_factory: SettingsFactory) -> None:
    s = settings_factory(tz="UTC", log_level="debug", scheduler_enabled="false", git_sha="abc1234")
    assert s.tz == "UTC"
    assert s.log_level == "DEBUG"
    assert s.scheduler_enabled is False
    assert s.git_sha == "abc1234"


@pytest.mark.parametrize("field", ["SECRETS_KEY", "SESSION_SECRET"])
def test_required_secret_missing(clean_env: pytest.MonkeyPatch, field: str) -> None:
    clean_env.setenv("SECRETS_KEY", TEST_SECRETS_KEY)
    clean_env.setenv("SESSION_SECRET", TEST_SESSION_SECRET)
    clean_env.delenv(field)
    with pytest.raises(ValidationError, match=field.lower()):
        Settings()  # type: ignore[call-arg]


@pytest.mark.parametrize("field", ["secrets_key", "session_secret"])
@pytest.mark.parametrize("bad", ["change-me", "x" * 31])
def test_weak_secret_refused_without_echo(
    settings_factory: SettingsFactory, field: str, bad: str
) -> None:
    with pytest.raises(ValidationError) as exc:
        settings_factory(**{field: bad})
    message = str(exc.value)
    assert field.upper() in message
    assert bad not in message


def test_password_hash_must_be_argon2(settings_factory: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="argon2"):
        settings_factory(owner_password_hash="plaintext-password")
    s = settings_factory(owner_password_hash="$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")
    assert s.login_enabled is True


def test_unknown_time_zone_refused(settings_factory: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="IANA"):
        settings_factory(tz="Mars/Olympus_Mons")


def test_unknown_log_level_refused(settings_factory: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        settings_factory(log_level="LOUD")


def test_secrets_not_in_repr(settings_factory: SettingsFactory) -> None:
    s = settings_factory(anthropic_api_key="sk-test-" + "a" * 20)
    for text in (repr(s), str(s)):
        assert TEST_SECRETS_KEY not in text
        assert TEST_SESSION_SECRET not in text
        assert "sk-test-" not in text
    assert "ai-newsdesk.localhost" in repr(s)


def test_get_settings_is_cached(settings_factory: SettingsFactory) -> None:
    settings_factory()  # sets the environment
    assert get_settings() is get_settings()


def test_env_example_names_every_setting() -> None:
    text = (ROOT / ".env.example").read_text()
    keys = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = {name.upper() for name in Settings.model_fields} - keys
    assert not missing, f".env.example lacks {sorted(missing)}"


def test_env_example_secrets_are_placeholders() -> None:
    """Any key that names a secret ships empty or as the refused placeholder."""
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if any(word in key for word in ("KEY", "SECRET", "HASH", "TOKEN", "PASSWORD")):
                assert value in {"", "change-me"}, f"{key} must be a placeholder"
```

- [ ] **Step 3: Run them.** `uv run pytest tests/test_config.py -v`. Expected: FAIL with `ImportError while loading conftest ... ModuleNotFoundError: No module named 'ai_newsdesk.config'`.

- [ ] **Step 4: Implement `src/ai_newsdesk/config.py`.**

```python
"""Process configuration from environment variables, read once at start-up (V1-S0).

Compose injects `.env`; nothing here reads a file. Run-time tunables live in the database
(`runtime_settings.py`), not here.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET = "change-me"  # noqa: S105  # the .env.example placeholder, refused below
MIN_SECRET_LENGTH = 32
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error must never echo a secret's value.
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    site_host: str = "ai-newsdesk.localhost"
    # repr=False keeps secrets out of repr()/str() and so out of logs and tracebacks.
    secrets_key: str = Field(repr=False)
    session_secret: str = Field(repr=False)
    owner_password_hash: str = Field(default="", repr=False)
    anthropic_api_key: str = Field(default="", repr=False)
    research_engine_api_key: str = Field(default="", repr=False)
    typesafe_api_key: str = Field(default="", repr=False)
    research_engine_url: str = "http://research-engine-app:8000"
    db_path: str = "data/ai-newsdesk.sqlite"
    tz: str = "Europe/London"
    log_level: str = "INFO"
    git_sha: str = "unknown"
    scheduler_enabled: bool = True

    @field_validator("secrets_key", "session_secret")
    @classmethod
    def _real_secret(cls, v: str, info: ValidationInfo) -> str:
        """Refuse the placeholder and short secrets; name the variable, never the value."""
        name = (info.field_name or "secret").upper()
        if v == PLACEHOLDER_SECRET or len(v) < MIN_SECRET_LENGTH:
            what = (
                "is the .env.example placeholder"
                if v == PLACEHOLDER_SECRET
                else f"is shorter than {MIN_SECRET_LENGTH} characters"
            )
            raise ValueError(
                f"{name} {what}; it must be a random secret of at least {MIN_SECRET_LENGTH} "
                "characters. Generate one with: openssl rand -hex 32"
            )
        return v

    @field_validator("owner_password_hash")
    @classmethod
    def _argon2_or_empty(cls, v: str) -> str:
        if v and not v.startswith("$argon2"):
            raise ValueError(
                "OWNER_PASSWORD_HASH must be an argon2 hash (run `make set-password`) or empty"
            )
        return v

    @field_validator("tz")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"TZ must be an IANA time zone name, e.g. Europe/London: {v}") from exc
        return v

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, v: str) -> str:
        level = v.upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(LOG_LEVELS)}")
        return level

    @property
    def zone(self) -> ZoneInfo:
        """The schedule and budget-month time zone."""
        return ZoneInfo(self.tz)

    @property
    def login_enabled(self) -> bool:
        return bool(self.owner_password_hash)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
```

- [ ] **Step 5: Run the tests.** `uv run pytest tests/test_config.py -v`. Expected: `15 passed`. Then run `make test`. Expected: green, `21 passed`.

- [ ] **Step 6: Commit.**

```bash
git add src/ai_newsdesk/config.py tests/conftest.py tests/test_config.py
git commit -m "feat(config): environment Settings with secret validation (V1-S0)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Shared models, protocols and clocks

**Goal:** Put the contract's `models.py` and `protocols.py` in place exactly, and add `SystemClock` and `FixedClock`, so that every later step builds on the same types.

**Files:**
- Create: `src/ai_newsdesk/models.py`, `src/ai_newsdesk/protocols.py`, `src/ai_newsdesk/clock.py`
- Test: `tests/test_models.py`, `tests/test_clock.py`

**Acceptance Criteria:**
- [ ] The enum values are exactly the contract strings, in contract order. `f"{ItemStatus.QUEUED}" == "queued"`.
- [ ] `Verdict(signal=-1)` and `Verdict(signal=11)` raise `ValidationError`; 0, 4 and 10 are accepted. An unknown `category` is refused.
- [ ] `Story.model_validate(...)` with nested `sources` dicts yields `StorySource` objects (the forward reference resolves).
- [ ] `SystemClock().now()` and `FixedClock(...).now()` return aware UTC datetimes. `FixedClock` refuses naive datetimes and moves only through `set` and `advance`.
- [ ] pyright accepts `clock: Clock = SystemClock()` and `FixedClock(...)`, a static check that the protocol is satisfied.

**Verify:** `uv run pytest tests/test_models.py tests/test_clock.py -v` → `13 passed`; `uv run pyright` → `0 errors`

**Steps:**

- [ ] **Step 1: Write the failing tests** in `tests/test_models.py`:

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ai_newsdesk.models import (
    Candidate,
    Category,
    ItemStatus,
    Purpose,
    ReviewInput,
    SourceConfig,
    SourceKind,
    Story,
    StorySource,
    Trust,
    Usage,
    Verdict,
)


def test_enum_values_are_the_stored_strings() -> None:
    assert [t.value for t in Trust] == ["primary", "trusted", "discovery"]
    assert [k.value for k in SourceKind] == ["feed", "x_person", "search", "arxiv", "github", "hn"]
    assert [c.value for c in Category] == [
        "model_release",
        "research",
        "product",
        "policy",
        "industry",
        "opinion",
        "other",
    ]
    assert [s.value for s in ItemStatus] == [
        "new",
        "dropped",
        "queued",
        "reviewed",
        "review_failed",
        "expired",
    ]
    assert [p.value for p in Purpose] == ["review", "write", "briefing"]
    assert Category("model_release") is Category.MODEL_RELEASE
    assert f"{ItemStatus.QUEUED}" == "queued"


@pytest.mark.parametrize("signal", [0, 4, 10])
def test_verdict_signal_in_range(signal: int) -> None:
    v = Verdict(item_id=1, signal=signal, junk=False, category=Category.RESEARCH)
    assert v.signal == signal
    assert v.same_story_id is None


@pytest.mark.parametrize("signal", [-1, 11])
def test_verdict_signal_out_of_range(signal: int) -> None:
    with pytest.raises(ValidationError):
        Verdict(item_id=1, signal=signal, junk=False, category=Category.RESEARCH)


def test_verdict_category_must_be_known() -> None:
    with pytest.raises(ValidationError):
        Verdict.model_validate({"item_id": 1, "signal": 5, "junk": False, "category": "gossip"})


def test_defaults() -> None:
    src = SourceConfig.model_validate(
        {
            "id": 1,
            "kind": "feed",
            "name": "Lab",
            "config": {"url": "https://e.test/f"},
            "trust": "primary",
        }
    )
    assert (src.enabled, src.weight, src.trust) == (True, 1.0, Trust.PRIMARY)
    cand = Candidate(source_id=1, url="https://e.test/a", title="A")
    assert (cand.snippet, cand.author, cand.hn_points) == ("", None, None)
    usage = Usage(provider="anthropic", model="m", purpose=Purpose.REVIEW, input_tokens=5)
    assert (usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (0, 0, 0)
    ri = ReviewInput(
        item_id=1,
        source_name="Lab",
        source_kind=SourceKind.FEED,
        trust=Trust.PRIMARY,
        title="A",
        text="body",
    )
    assert ri.story_candidates == []


def test_story_holds_sources() -> None:
    at = datetime(2026, 10, 14, 12, tzinfo=UTC)
    story = Story.model_validate(
        {
            "id": 1,
            "headline": "H",
            "category": "research",
            "score": 7.5,
            "summary": None,
            "why_it_matters": None,
            "first_seen_at": at,
            "last_updated_at": at,
            "feedback": 0,
            "sources": [
                {
                    "item_id": 3,
                    "source_name": "Lab",
                    "source_kind": "feed",
                    "url": "https://e.test/a",
                    "title": "A",
                }
            ],
        }
    )
    assert isinstance(story.sources[0], StorySource)
    assert story.sources[0].published_at is None
```

and in `tests/test_clock.py`:

```python
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_newsdesk.clock import FixedClock, SystemClock
from ai_newsdesk.protocols import Clock


def test_system_clock_is_aware_utc() -> None:
    clock: Clock = SystemClock()
    now = clock.now()
    assert now.tzinfo is UTC
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5


def test_fixed_clock_normalises_to_utc() -> None:
    bst = timezone(timedelta(hours=1))
    clock: Clock = FixedClock(datetime(2026, 10, 14, 13, 0, tzinfo=bst))
    assert clock.now() == datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    assert clock.now().tzinfo is UTC


def test_fixed_clock_moves_only_when_told() -> None:
    clock = FixedClock(datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
    assert clock.now() == clock.now()
    assert clock.advance(timedelta(hours=6)) == datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
    clock.set(datetime(2026, 11, 1, tzinfo=UTC))
    assert clock.now() == datetime(2026, 11, 1, tzinfo=UTC)


def test_fixed_clock_refuses_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 10, 14, 12, 0))
    clock = FixedClock(datetime(2026, 10, 14, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 10, 14))
```

- [ ] **Step 2: Run them.** `uv run pytest tests/test_models.py tests/test_clock.py -v`. Expected: FAIL. Collection errors: `ModuleNotFoundError: No module named 'ai_newsdesk.models'` and `No module named 'ai_newsdesk.clock'`.

- [ ] **Step 3: Implement `src/ai_newsdesk/models.py`.** This is the contract verbatim, apart from the comment placement and `model_rebuild` noted in Contract additions.

```python
"""Shared pydantic models and enums: the V1 contract every step uses (V1-S0)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Trust(StrEnum):
    PRIMARY = "primary"
    TRUSTED = "trusted"
    DISCOVERY = "discovery"


class SourceKind(StrEnum):
    FEED = "feed"
    X_PERSON = "x_person"
    SEARCH = "search"
    ARXIV = "arxiv"
    GITHUB = "github"
    HN = "hn"


class Category(StrEnum):
    MODEL_RELEASE = "model_release"
    RESEARCH = "research"
    PRODUCT = "product"
    POLICY = "policy"
    INDUSTRY = "industry"
    OPINION = "opinion"
    OTHER = "other"


class ItemStatus(StrEnum):
    NEW = "new"
    DROPPED = "dropped"
    QUEUED = "queued"
    REVIEWED = "reviewed"
    REVIEW_FAILED = "review_failed"
    EXPIRED = "expired"


class Purpose(StrEnum):
    REVIEW = "review"
    WRITE = "write"
    BRIEFING = "briefing"


class SourceConfig(BaseModel):
    id: int
    kind: SourceKind
    name: str
    # e.g. {"url": ...} / {"handle": ...} / {"query": ...}
    config: dict[str, str | int | float | bool | list[str]]
    trust: Trust
    enabled: bool = True
    weight: float = 1.0  # feedback weight, 0.3..2.0


class Candidate(BaseModel):
    source_id: int
    url: str
    title: str
    author: str | None = None
    published_at: datetime | None = None
    snippet: str = ""
    hn_points: int | None = None


class SourceResult(BaseModel):
    source_id: int
    candidates: list[Candidate] = Field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0


class SearchHit(BaseModel):
    url: str
    title: str
    snippet: str = ""


class StoryRef(BaseModel):
    id: int
    headline: str


class ReviewInput(BaseModel):
    item_id: int
    source_name: str
    source_kind: SourceKind
    trust: Trust
    title: str
    author: str | None = None
    text: str  # body excerpt, else snippet, else "(no text, title only)"
    story_candidates: list[StoryRef] = Field(default_factory=list)


class Verdict(BaseModel):
    item_id: int
    signal: int = Field(ge=0, le=10)
    junk: bool
    category: Category
    same_story_id: int | None = None


class Usage(BaseModel):
    provider: str  # "anthropic" | "typesafe"
    model: str
    purpose: Purpose
    input_tokens: int
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class JudgeResult(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)
    failed_item_ids: list[int] = Field(default_factory=list)
    usage: list[Usage] = Field(default_factory=list)


class WriteInput(BaseModel):
    story_id: int
    headline: str
    texts: list[str]  # best 1-2 item texts ("title\n\ntext")
    include_why: bool


class StoryText(BaseModel):
    story_id: int
    summary: str
    why_it_matters: str | None = None


class BriefingStory(BaseModel):
    story_id: int
    headline: str
    category: Category
    summary: str
    url: str  # lead item's url


class Story(BaseModel):  # what the web layer renders
    id: int
    headline: str
    category: Category
    score: float
    summary: str | None
    why_it_matters: str | None
    first_seen_at: datetime
    last_updated_at: datetime
    feedback: int  # -1, 0, 1
    sources: list["StorySource"] = Field(default_factory=list)


class StorySource(BaseModel):
    item_id: int
    source_name: str
    source_kind: SourceKind
    url: str
    title: str
    published_at: datetime | None = None


Story.model_rebuild()  # resolve the forward reference to StorySource
```

- [ ] **Step 4: Implement `src/ai_newsdesk/protocols.py`.** This is the contract, formatted for ruff.

```python
"""The seams around every external dependency (V1-S0). Only app.py wires concrete classes."""

from datetime import datetime
from typing import Protocol

from ai_newsdesk.models import (
    BriefingStory,
    JudgeResult,
    ReviewInput,
    SearchHit,
    SourceConfig,
    SourceKind,
    SourceResult,
    StoryText,
    Usage,
    WriteInput,
)


class Clock(Protocol):
    def now(self) -> datetime: ...  # timezone-aware UTC


class ResearchEngine(Protocol):
    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]: ...

    async def fetch_markdown(self, url: str) -> str | None: ...  # None on any failure

    async def health(self) -> bool: ...


class Source(Protocol):
    kind: SourceKind

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult: ...  # never raises


class Judge(Protocol):
    model: str

    def worst_case_cost(self, inputs: list[ReviewInput]) -> float: ...  # USD, budget pre-check

    # never raises; failures -> failed_item_ids
    async def review(self, inputs: list[ReviewInput]) -> JudgeResult: ...


class Writer(Protocol):
    model: str

    def worst_case_cost_stories(self, inputs: list[WriteInput]) -> float: ...

    def worst_case_cost_briefing(self, stories: list[BriefingStory]) -> float: ...

    # never raises; missing stories simply absent
    async def write_stories(
        self, inputs: list[WriteInput]
    ) -> tuple[list[StoryText], list[Usage]]: ...

    async def write_briefing(
        self, stories: list[BriefingStory], date_label: str
    ) -> tuple[str | None, list[Usage]]: ...
```

- [ ] **Step 5: Implement `src/ai_newsdesk/clock.py`.**

```python
"""Clocks: the real one, and a settable one for tests (V1-S0). Both return aware UTC."""

from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that only moves when told to. Accepts any aware datetime; returns UTC."""

    def __init__(self, at: datetime) -> None:
        self._at = _aware_utc(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = _aware_utc(at)

    def advance(self, delta: timedelta) -> datetime:
        self._at = self._at + delta
        return self._at


def _aware_utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("FixedClock needs a timezone-aware datetime")
    return at.astimezone(UTC)
```

- [ ] **Step 6: Run the tests.** `uv run pytest tests/test_models.py tests/test_clock.py -v`. Expected: `13 passed`. Then run `make test`. Expected: green, `34 passed`.

- [ ] **Step 7: Commit.**

```bash
git add src/ai_newsdesk/models.py src/ai_newsdesk/protocols.py src/ai_newsdesk/clock.py \
  tests/test_models.py tests/test_clock.py
git commit -m "feat(core): shared models, protocols and clocks (V1-S0)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Fakes and shared fixtures

**Goal:** Give later steps protocol-true fakes for `ResearchEngine`, `Source`, `Judge` and `Writer` (FixedClock is the `Clock` fake), plus builders and fixtures. Pipeline, budget and web tests then need no network and no real model.

**Files:**
- Create: `tests/fakes.py`
- Modify: `tests/conftest.py` (add `db_path`, `now` and `clock`)
- Test: `tests/test_fakes.py`

**Acceptance Criteria:**
- [ ] pyright accepts `ResearchEngine = FakeResearchEngine()`, `Source = FakeSource()`, `Judge = FakeJudge()`, `Writer = FakeWriter()` and `Clock = FixedClock(...)`.
- [ ] Each fake behaves as described in Contract additions: recorded calls, canned answers, injected failures, usage and cost figures.
- [ ] The `clock` fixture is `FixedClock(TEST_NOW)`. `db_path` is a path under `tmp_path` whose file doesn't exist yet.

**Verify:** `uv run pytest tests/test_fakes.py -v` → `8 passed`; `make test` → `42 passed`

**Steps:**

- [ ] **Step 1: Write the failing tests** in `tests/test_fakes.py`.

```python
"""The fakes satisfy the protocols (checked by pyright) and behave as documented."""

from datetime import timedelta

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import (
    BriefingStory,
    Category,
    Purpose,
    SearchHit,
    SourceKind,
    SourceResult,
    StoryRef,
    Verdict,
    WriteInput,
)
from ai_newsdesk.protocols import Clock, Judge, ResearchEngine, Source, Writer
from tests.fakes import (
    FakeJudge,
    FakeResearchEngine,
    FakeSource,
    FakeWriter,
    make_candidate,
    make_review_input,
    make_source_config,
    utc,
)

# Static protocol conformance: pyright fails `make test` if a fake drifts from its protocol.
_engine: ResearchEngine = FakeResearchEngine()
_source: Source = FakeSource()
_judge: Judge = FakeJudge()
_writer: Writer = FakeWriter()
_clock: Clock = FixedClock(utc(2026, 10, 14))


def test_utc_helper() -> None:
    assert utc(2026, 10, 14, 7, 30).isoformat() == "2026-10-14T07:30:00+00:00"


async def test_fake_research_engine_records_and_answers() -> None:
    hit = SearchHit(url="https://x.com/someone/status/1", title="post")
    engine = FakeResearchEngine(
        search_results={"q": [hit, hit, hit]}, pages={"https://a.test": "# A"}
    )
    assert await engine.search("q", max_results=2) == [hit, hit]
    assert await engine.search("other", time_range=None) == []
    assert engine.search_calls == [("q", 2, "day"), ("other", 10, None)]
    assert await engine.fetch_markdown("https://a.test") == "# A"
    assert await engine.fetch_markdown("https://missing.test") is None
    assert engine.fetch_calls == ["https://a.test", "https://missing.test"]
    assert await engine.health() is True


async def test_fake_research_engine_can_raise() -> None:
    engine = FakeResearchEngine(search_error=RuntimeError("down"), healthy=False)
    with pytest.raises(RuntimeError, match="down"):
        await engine.search("q")
    assert await engine.health() is False


async def test_fake_source_rehomes_candidates_and_records() -> None:
    since = utc(2026, 10, 14) - timedelta(hours=6)
    source = FakeSource(kind=SourceKind.HN, candidates=[make_candidate(source_id=99)])
    result = await source.collect(make_source_config(id=7, kind=SourceKind.HN), since)
    assert result.source_id == 7
    assert [c.source_id for c in result.candidates] == [7]
    assert source.calls == [(7, since)]


async def test_fake_source_canned_result_and_error() -> None:
    canned = SourceResult(source_id=3, error="timeout")
    source = FakeSource(results={3: canned}, error="boom")
    assert await source.collect(make_source_config(id=3), utc(2026, 10, 14)) is canned
    other = await source.collect(make_source_config(id=4), utc(2026, 10, 14))
    assert (other.error, other.candidates) == ("boom", [])


async def test_fake_judge_defaults_overrides_failures() -> None:
    special = Verdict(item_id=2, signal=9, junk=False, category=Category.MODEL_RELEASE)
    judge = FakeJudge(verdicts={2: special}, fail_ids={3}, match_first_candidate=True)
    first = make_review_input(item_id=1).model_copy(
        update={"story_candidates": [StoryRef(id=40, headline="H")]}
    )
    inputs = [first, make_review_input(item_id=2), make_review_input(item_id=3)]
    result = await judge.review(inputs)
    assert [v.item_id for v in result.verdicts] == [1, 2]
    assert result.verdicts[0].signal == 6
    assert result.verdicts[0].same_story_id == 40
    assert result.verdicts[1] is special
    assert result.failed_item_ids == [3]
    assert result.usage[0].purpose is Purpose.REVIEW
    assert result.usage[0].input_tokens == 300
    assert judge.worst_case_cost(inputs) == pytest.approx(0.003)
    assert judge.calls == [inputs]


async def test_fake_judge_empty_batch_has_no_usage() -> None:
    result = await FakeJudge().review([])
    assert result.verdicts == [] and result.usage == []


async def test_fake_writer_stories_and_briefing() -> None:
    writer = FakeWriter(fail_story_ids={2})
    inputs = [
        WriteInput(story_id=1, headline="One", texts=["t"], include_why=True),
        WriteInput(story_id=2, headline="Two", texts=["t"], include_why=True),
        WriteInput(story_id=3, headline="Three", texts=["t"], include_why=False),
    ]
    texts, usage = await writer.write_stories(inputs)
    assert [(t.story_id, t.summary, t.why_it_matters) for t in texts] == [
        (1, "Summary: One", "Why: One"),
        (3, "Summary: Three", None),
    ]
    assert usage[0].purpose is Purpose.WRITE
    assert writer.worst_case_cost_stories(inputs) == pytest.approx(0.003)

    story = BriefingStory(
        story_id=1, headline="One", category=Category.RESEARCH, summary="s", url="https://a.test"
    )
    body, usage = await writer.write_briefing([story], "Wednesday 14 October")
    assert body == "Today's briefing."
    assert usage[0].purpose is Purpose.BRIEFING
    assert writer.briefing_calls == [([story], "Wednesday 14 October")]
    assert await writer.write_briefing([], "x") == (None, [])
    assert writer.worst_case_cost_briefing([]) == 0.0
```

- [ ] **Step 2: Run them.** `uv run pytest tests/test_fakes.py -v`. Expected: FAIL during collection with `ModuleNotFoundError: No module named 'tests.fakes'`.

- [ ] **Step 3: Implement `tests/fakes.py`.**

```python
"""In-memory fakes for every protocol, plus small builders, shared by all test files.

Each fake records its calls so tests can assert on what the code under test asked for.
"""

from datetime import UTC, datetime

from ai_newsdesk.models import (
    BriefingStory,
    Candidate,
    Category,
    JudgeResult,
    Purpose,
    ReviewInput,
    SearchHit,
    SourceConfig,
    SourceKind,
    SourceResult,
    StoryText,
    Trust,
    Usage,
    Verdict,
    WriteInput,
)

FAKE_MODEL = "claude-haiku-5-5"


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """An aware UTC datetime, for terse test data."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def make_source_config(
    id: int = 1,
    kind: SourceKind = SourceKind.FEED,
    name: str = "Example Lab blog",
    config: dict[str, str | int | float | bool | list[str]] | None = None,
    trust: Trust = Trust.PRIMARY,
    enabled: bool = True,
    weight: float = 1.0,
) -> SourceConfig:
    return SourceConfig(
        id=id,
        kind=kind,
        name=name,
        config=config if config is not None else {"url": "https://lab.example.com/feed.xml"},
        trust=trust,
        enabled=enabled,
        weight=weight,
    )


def make_candidate(
    source_id: int = 1,
    url: str = "https://lab.example.com/news/model-x",
    title: str = "Example Lab releases Model X",
    author: str | None = None,
    published_at: datetime | None = None,
    snippet: str = "Model X is a new open-weights model.",
    hn_points: int | None = None,
) -> Candidate:
    return Candidate(
        source_id=source_id,
        url=url,
        title=title,
        author=author,
        published_at=published_at,
        snippet=snippet,
        hn_points=hn_points,
    )


def make_review_input(
    item_id: int = 1,
    title: str = "Example Lab releases Model X",
    text: str = "Model X is a new open-weights model.",
    source_name: str = "Example Lab blog",
    source_kind: SourceKind = SourceKind.FEED,
    trust: Trust = Trust.PRIMARY,
) -> ReviewInput:
    return ReviewInput(
        item_id=item_id,
        source_name=source_name,
        source_kind=source_kind,
        trust=trust,
        title=title,
        text=text,
    )


class FakeResearchEngine:
    """`ResearchEngine` fake. Unknown queries return []; unknown URLs fetch as None."""

    def __init__(
        self,
        search_results: dict[str, list[SearchHit]] | None = None,
        pages: dict[str, str] | None = None,
        healthy: bool = True,
        search_error: Exception | None = None,
    ) -> None:
        self.search_results = search_results or {}
        self.pages = pages or {}
        self.healthy = healthy
        self.search_error = search_error
        self.search_calls: list[tuple[str, int, str | None]] = []
        self.fetch_calls: list[str] = []

    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]:
        self.search_calls.append((query, max_results, time_range))
        if self.search_error is not None:
            raise self.search_error
        return list(self.search_results.get(query, []))[:max_results]

    async def fetch_markdown(self, url: str) -> str | None:
        self.fetch_calls.append(url)
        return self.pages.get(url)

    async def health(self) -> bool:
        return self.healthy


class FakeSource:
    """`Source` fake. Returns `results[src.id]` if given, else `candidates` re-homed to src.id."""

    def __init__(
        self,
        kind: SourceKind = SourceKind.FEED,
        candidates: list[Candidate] | None = None,
        results: dict[int, SourceResult] | None = None,
        error: str | None = None,
    ) -> None:
        self.kind = kind
        self.candidates = candidates or []
        self.results = results or {}
        self.error = error
        self.calls: list[tuple[int, datetime]] = []

    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult:
        self.calls.append((src.id, since))
        if src.id in self.results:
            return self.results[src.id]
        if self.error is not None:
            return SourceResult(source_id=src.id, error=self.error)
        return SourceResult(
            source_id=src.id,
            candidates=[c.model_copy(update={"source_id": src.id}) for c in self.candidates],
        )


class FakeJudge:
    """`Judge` fake.

    Per input: an id in `fail_ids` fails; an id in `verdicts` gets that verdict; anything
    else gets `default_signal`, not junk, `default_category`, and `same_story_id` set to
    the first story candidate when `match_first_candidate` is true. Reports one `Usage`
    per call (100 input and 20 output tokens per item).
    """

    def __init__(
        self,
        model: str = FAKE_MODEL,
        verdicts: dict[int, Verdict] | None = None,
        fail_ids: set[int] | None = None,
        default_signal: int = 6,
        default_category: Category = Category.OTHER,
        match_first_candidate: bool = False,
        cost_per_item: float = 0.001,
    ) -> None:
        self.model = model
        self.verdicts = verdicts or {}
        self.fail_ids = fail_ids or set()
        self.default_signal = default_signal
        self.default_category = default_category
        self.match_first_candidate = match_first_candidate
        self.cost_per_item = cost_per_item
        self.calls: list[list[ReviewInput]] = []

    def worst_case_cost(self, inputs: list[ReviewInput]) -> float:
        return self.cost_per_item * len(inputs)

    async def review(self, inputs: list[ReviewInput]) -> JudgeResult:
        self.calls.append(list(inputs))
        result = JudgeResult()
        for inp in inputs:
            if inp.item_id in self.fail_ids:
                result.failed_item_ids.append(inp.item_id)
            elif inp.item_id in self.verdicts:
                result.verdicts.append(self.verdicts[inp.item_id])
            else:
                same = (
                    inp.story_candidates[0].id
                    if self.match_first_candidate and inp.story_candidates
                    else None
                )
                result.verdicts.append(
                    Verdict(
                        item_id=inp.item_id,
                        signal=self.default_signal,
                        junk=False,
                        category=self.default_category,
                        same_story_id=same,
                    )
                )
        if inputs:
            result.usage.append(
                Usage(
                    provider="anthropic",
                    model=self.model,
                    purpose=Purpose.REVIEW,
                    input_tokens=100 * len(inputs),
                    output_tokens=20 * len(inputs),
                )
            )
        return result


class FakeWriter:
    """`Writer` fake.

    Stories get summary "Summary: <headline>" and, when `include_why`, why-it-matters
    "Why: <headline>". Stories in `fail_story_ids` are left out. The briefing body is
    `briefing_body` (None simulates a failure). One `Usage` per call that did work.
    """

    def __init__(
        self,
        model: str = FAKE_MODEL,
        fail_story_ids: set[int] | None = None,
        briefing_body: str | None = "Today's briefing.",
        cost_per_story: float = 0.001,
        briefing_cost: float = 0.002,
    ) -> None:
        self.model = model
        self.fail_story_ids = fail_story_ids or set()
        self.briefing_body = briefing_body
        self.cost_per_story = cost_per_story
        self.briefing_cost = briefing_cost
        self.story_calls: list[list[WriteInput]] = []
        self.briefing_calls: list[tuple[list[BriefingStory], str]] = []

    def worst_case_cost_stories(self, inputs: list[WriteInput]) -> float:
        return self.cost_per_story * len(inputs)

    def worst_case_cost_briefing(self, stories: list[BriefingStory]) -> float:
        return self.briefing_cost if stories else 0.0

    async def write_stories(self, inputs: list[WriteInput]) -> tuple[list[StoryText], list[Usage]]:
        self.story_calls.append(list(inputs))
        texts = [
            StoryText(
                story_id=inp.story_id,
                summary=f"Summary: {inp.headline}",
                why_it_matters=f"Why: {inp.headline}" if inp.include_why else None,
            )
            for inp in inputs
            if inp.story_id not in self.fail_story_ids
        ]
        usage = [
            Usage(
                provider="anthropic",
                model=self.model,
                purpose=Purpose.WRITE,
                input_tokens=200 * len(inputs),
                output_tokens=60 * len(inputs),
            )
        ]
        return texts, usage if inputs else []

    async def write_briefing(
        self, stories: list[BriefingStory], date_label: str
    ) -> tuple[str | None, list[Usage]]:
        self.briefing_calls.append((list(stories), date_label))
        if not stories:
            return None, []
        usage = Usage(
            provider="anthropic",
            model=self.model,
            purpose=Purpose.BRIEFING,
            input_tokens=150 * len(stories),
            output_tokens=800,
        )
        return self.briefing_body, [usage]
```

- [ ] **Step 4: Replace `tests/conftest.py`** with the version below. It adds `TEST_NOW` and the `db_path`, `now` and `clock` fixtures.

```python
"""Shared pytest fixtures."""

from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.config import Settings, get_settings
from tests.fakes import utc

ROOT = Path(__file__).resolve().parent.parent

# Fake secrets, built at runtime (nothing for gitleaks to flag) and long enough for the
# 32-character minimum.
TEST_SECRETS_KEY = "test-secrets-key-" + "0" * 32
TEST_SESSION_SECRET = "test-session-secret-" + "1" * 32
TEST_NOW = utc(2026, 10, 14, 12, 0)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """No Settings variable leaks in from the developer's shell (TZ is often set)."""
    monkeypatch.chdir(ROOT)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def settings_factory(clean_env: pytest.MonkeyPatch) -> Callable[..., Settings]:
    """Build a Settings from a clean environment plus the two required test secrets.

    Keyword overrides use field names (``tz="UTC"``); they are set as environment variables
    so they go through the same parsing and validation as production.
    """

    def make(**overrides: str) -> Settings:
        env = {"SECRETS_KEY": TEST_SECRETS_KEY, "SESSION_SECRET": TEST_SESSION_SECRET}
        env.update({k.upper(): v for k, v in overrides.items()})
        for k, v in env.items():
            clean_env.setenv(k, v)
        return Settings()  # type: ignore[call-arg]

    return make


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """A fresh SQLite path in pytest's tmp dir (the file does not exist yet)."""
    return str(tmp_path / "data" / "test.sqlite")


@pytest.fixture
def now() -> datetime:
    return TEST_NOW


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(TEST_NOW)
```

- [ ] **Step 5: Run the tests.** `uv run pytest tests/test_fakes.py -v`. Expected: `8 passed`. Then run `make test`. Expected: green, `42 passed`.

- [ ] **Step 6: Commit.**

```bash
git add tests/fakes.py tests/conftest.py tests/test_fakes.py
git commit -m "test: protocol fakes, builders and shared fixtures (V1-S0)" \
  -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Step done when

- [ ] `make test` is green locally (ruff, ruff format, pyright `0 errors`, `42 passed`).
- [ ] `uv run pre-commit run --all-files` passes, including gitleaks.
- [ ] The branch is pushed, and a PR is opened with `git push -u origin step-0-scaffold && GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr create --base main --title "Step 0: scaffold (V1-S0)" --body-file <body>`. The body lists Tasks 1–4 and ends with the attribution line `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- [ ] CI is green on the PR: the `lint-test` and `gitleaks` jobs. If hosted runners don't pick up jobs, the same SHA's push run is the evidence; rerun later.
- [ ] The PR is merged to `main` with `GH_TOKEN=... gh pr merge --merge --delete-branch` (a merge commit keeps the one-commit-per-task history, as PRs #1 and #2 did), and local `main` is fast-forwarded.
