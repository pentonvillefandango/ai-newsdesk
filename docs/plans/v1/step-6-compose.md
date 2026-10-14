# Step 6: Container image and Compose (tasks 32–34)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers-extended-cc:subagent-driven-development (or executing-plans). Steps use `- [ ]` checkboxes. Read the master plan `docs/plans/2026-10-14-ai-newsdesk-v1.md` (Global Constraints, Settings table) first.

**Goal:** A pinned, non-root, read-only container image for the app and a co-tenancy-safe Compose project `ai-newsdesk` (prod, dev and debug files), with policy tests that stop the house rules from regressing.

**Branch:** `v1/step-6-compose` (from `main`), one PR into `main`.

**Depends on:** step 0 only (`pyproject.toml`, `uv.lock`, `src/ai_newsdesk/config.py` with `Settings`, `.env.example`, `config/`). The image runs whatever app code exists; the app's `/health` (step 5) is only needed when the container is actually started (step 7 smoke, step 8 deploy). Every test in this step parses files only: no Docker daemon is needed, so CI runs them as-is.

## Contract additions

- **App image:** `ai-newsdesk-app:${GIT_SHA:-dev}`, built from `Dockerfile` (build arg `GIT_SHA`). Runs as uid/gid `10001`, `WORKDIR /app`, venv at `/app/.venv` (on `PATH`), seed config at `/app/config/` (so `config/pricing.yaml` and `config/sources.yaml` resolve relative to the working directory), `/data` owned by `10001`.
- **Dev image:** `ai-newsdesk-app-dev:local` (only in `compose.dev.yaml`). Dev Compose project name: `ai-newsdesk-dev`. Dev network: `ai-newsdesk-dev-proxy` (project-local, not the VM's `proxy`).
- **Compose:** project `ai-newsdesk`; one service `app`; named volume `app-data` mounted at `/data`; networks `internal` and external `proxy` (alias `ai-newsdesk-app`). `DB_PATH` is fixed to `/data/ai-newsdesk.sqlite` inside Compose.
- **Compose-only variables** (never reach the app): `APP_MEM_LIMIT` (default `512m`), `APP_CPUS` (default `0.5`), `APP_PORT` (debug override only; empty = no debug port).
- **`.env.example`** is authoritative as written in Task 33 (every `Settings` env var plus `APP_MEM_LIMIT`, `APP_CPUS`, `APP_PORT`). Secret placeholders are `change-me`; API keys and `OWNER_PASSWORD_HASH` are empty.
- **`Settings` field names** are the lower-case form of the env var names (`db_path` ↔ `DB_PATH`). The policy test relies on `{f.upper() for f in Settings.model_fields}` being exactly the env var set in the master plan's Settings table.
- **`tzdata`** is a runtime dependency (the slim image has no system zoneinfo; `Europe/London` must resolve).
- **Integration tests and research-engine:** the dev stack sits on a project-local network and cannot reach `research-engine-app`. Tests marked `integration` that need research-engine run on the host against the live research-engine through Caddy via `RESEARCH_ENGINE_URL` (see Task 33, step 8), and skip when `RESEARCH_ENGINE_URL` is unset.

---

### Task 32: Dockerfile and .dockerignore

**Goal:** A two-stage, pinned, non-root image that starts the uvicorn factory behind Caddy and reports its own health.

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `tests/test_dockerfile_policy.py`
- Modify (only if the tzdata test fails): `pyproject.toml`, `uv.lock` (via `uv add tzdata`)

**Acceptance Criteria:**
- [ ] Both stages use `python:3.13.16-slim-trixie`; uv is copied from `ghcr.io/astral-sh/uv:0.12.23`; every `uv sync` is `--locked`.
- [ ] Final stage: `USER 10001`, `/data` created and owned by `10001:10001`, a `HEALTHCHECK` against `http://127.0.0.1:8000/health`, `ARG GIT_SHA` exported as `ENV GIT_SHA`, `EXPOSE 8000`.
- [ ] `CMD` runs `uvicorn ai_newsdesk.app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"`.
- [ ] App code is root-owned (no `COPY --chown`); no `ADD http…`.
- [ ] `.dockerignore` keeps `.env*` (except `.env.example`), `.git`, `data`, `backups`, `.deploy`, `deploys.jsonl`, `.venv`, `tests`, `docs` out of the build context.
- [ ] `uv.lock` contains `tzdata`.
- [ ] `docker build` succeeds and `python -c "import zoneinfo; zoneinfo.ZoneInfo('Europe/London')"` works in the image.

**Verify:** `uv run pytest tests/test_dockerfile_policy.py -v` → all passed; `docker build -q --build-arg GIT_SHA=test -t ai-newsdesk-app-dev:local .` → prints an image id.

**Steps:**

- [ ] **Step 1: Write the failing test** `tests/test_dockerfile_policy.py`:

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_dockerfile_policy.py -v`
Expected: FAIL (`FileNotFoundError` for `Dockerfile` / `.dockerignore`; `test_tzdata_is_locked` fails if step 0 did not add tzdata).

- [ ] **Step 3: Write `Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1
# ai-newsdesk app image (V1-S6). Two stages: uv builds the venv, the final stage runs as uid 10001.
FROM python:3.13.16-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first (cached layer), then the project itself, installed non-editable.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

FROM python:3.13.16-slim-trixie
ARG GIT_SHA=unknown
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY config ./config
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 DB_PATH=/data/ai-newsdesk.sqlite GIT_SHA=${GIT_SHA}
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
CMD ["uvicorn", "ai_newsdesk.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "5"]
```

- [ ] **Step 4: Write `.dockerignore`**

```text
**/.env*
.env*
!.env.example
.git
.github
.claude
.superpowers
.deploy
data
backups
logs
deploys.jsonl
.venv
**/__pycache__
.pytest_cache
.ruff_cache
.coverage
tests
docs
*.local.md
```

- [ ] **Step 5: Make sure tzdata is locked.** Run `grep -c '^name = "tzdata"$' uv.lock`. If it prints `0`, run `uv add tzdata` (this edits `pyproject.toml` and `uv.lock`). Expected afterwards: `grep -c '^name = "tzdata"$' uv.lock` → `1`.

- [ ] **Step 6: Run the tests and watch them pass**

Run: `uv run pytest tests/test_dockerfile_policy.py -v`
Expected: 10 passed.

- [ ] **Step 7: Build the image once and check the timezone** (a dev tag, so no live tag is ever touched):

```bash
docker build -q --build-arg GIT_SHA=test -t ai-newsdesk-app-dev:local .
docker run --rm --network none --read-only --user 10001:10001 ai-newsdesk-app-dev:local \
  python -c "import zoneinfo, ai_newsdesk; print(zoneinfo.ZoneInfo('Europe/London'))"
```

Expected: an image id, then `Europe/London`.

- [ ] **Step 8: Full suite, then commit**

```bash
uv run ruff format tests && make test
git add Dockerfile .dockerignore tests/test_dockerfile_policy.py pyproject.toml uv.lock
git commit -m "build(docker): pinned non-root app image with healthcheck (V1-S6)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

Expected: `make test` exits 0; the commit succeeds (gitleaks pre-commit passes).

---

### Task 33: compose.yaml, compose.dev.yaml, compose.debug.yaml and .env.example

**Goal:** The production Compose project `ai-newsdesk` with every co-tenancy and hardening rule, a dev override that can never touch the live stack or the VM's `proxy` network, a loopback-only debug override, and an `.env.example` that documents every variable.

**Files:**
- Create: `compose.yaml`
- Create: `compose.dev.yaml`
- Create: `compose.debug.yaml`
- Replace: `.env.example` (authoritative content below)

**Acceptance Criteria:**
- [ ] `compose.yaml` starts with `name: ai-newsdesk` and has exactly one service, `app`.
- [ ] No `ports`, `container_name`, `env_file`, `cap_add`, `privileged`, host namespaces or bind mounts.
- [ ] `app`: image `ai-newsdesk-app:${GIT_SHA:-dev}`, `restart: unless-stopped`, healthcheck, json-file logging with `max-size`/`max-file`, limits `${APP_MEM_LIMIT:-512m}` / `${APP_CPUS:-0.5}`, `cap_drop: [ALL]`, `security_opt: ["no-new-privileges:true"]`, `read_only: true`, `/tmp` tmpfs `noexec,nosuid,nodev`, `user: "10001:10001"`, volume `app-data:/data`.
- [ ] `environment` maps every `Settings` field explicitly; secrets use `${VAR:?…}`; defaults equal the `Settings` defaults; `DB_PATH` is `/data/ai-newsdesk.sqlite`; `RESEARCH_ENGINE_URL` defaults to `http://research-engine-app:8000`.
- [ ] Networks: `internal: {}` and `proxy: {external: true, name: proxy}`; `app` joins `proxy` with alias `ai-newsdesk-app`.
- [ ] `compose.dev.yaml` only renames the app image (`ai-newsdesk-app-dev:local`) and replaces `proxy` with the project-local `ai-newsdesk-dev-proxy`.
- [ ] `compose.debug.yaml` only adds `127.0.0.1:${APP_PORT:?…}:8000` to `app`.
- [ ] `.env.example` holds every `Settings` env var plus `APP_MEM_LIMIT`, `APP_CPUS`, `APP_PORT`, with placeholders only.
- [ ] `docker compose -p ai-newsdesk-dev -f compose.yaml -f compose.dev.yaml config -q` succeeds with a dummy env file.

**Verify:** `uv run pytest tests/test_compose_policy.py -v` → all passed (the test is written in Task 34; this task's own check is the `config -q` command in step 5).

**Steps:**

- [ ] **Step 1: Write `compose.yaml`**

```yaml
name: ai-newsdesk

# Co-tenant stack on the shared toolbox VM: no host ports, no container_name, named volumes only,
# hard limits and log rotation. Only Caddy (project `caddy`) reaches the app, over the external
# `proxy` network, as `ai-newsdesk-app`. Policy-tested by tests/test_compose_policy.py.

x-logging: &logging
  driver: json-file
  options: { max-size: "10m", max-file: "5" }

services:
  app:
    build:
      context: .
      args: { GIT_SHA: "${GIT_SHA:-unknown}" }
    image: ai-newsdesk-app:${GIT_SHA:-dev}
    restart: unless-stopped
    # uvicorn graceful drain (5 s), then lifespan shutdown stops the scheduler; a run in flight
    # is abandoned and picked up again by the next run (items stay queued).
    stop_grace_period: 30s
    user: "10001:10001"
    read_only: true
    tmpfs: ["/tmp:size=64m,noexec,nosuid,nodev"]
    security_opt: ["no-new-privileges:true"]
    cap_drop: [ALL]
    # Explicit mapping, never env_file, so compose-only values (APP_MEM_LIMIT, APP_CPUS, APP_PORT)
    # never reach the app. Covers every Settings field (config.py); tests/test_compose_policy.py
    # fails on drift. Defaults equal the Settings defaults. Keep .env.example in sync.
    environment:
      # identity and secrets
      SITE_HOST: ${SITE_HOST:-ai-newsdesk.localhost}
      SECRETS_KEY: ${SECRETS_KEY:?set SECRETS_KEY in .env (openssl rand -hex 32)}
      SESSION_SECRET: ${SESSION_SECRET:?set SESSION_SECRET in .env (openssl rand -hex 32)}
      # argon2 hash written by `make set-password`, single-quoted in .env so `$` stays literal
      OWNER_PASSWORD_HASH: ${OWNER_PASSWORD_HASH:-}
      # API keys: fallbacks; keys saved on the Settings page win
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
      RESEARCH_ENGINE_API_KEY: ${RESEARCH_ENGINE_API_KEY:-}
      TYPESAFE_API_KEY: ${TYPESAFE_API_KEY:-}
      # research-engine over the shared proxy network (its alias there)
      RESEARCH_ENGINE_URL: ${RESEARCH_ENGINE_URL:-http://research-engine-app:8000}
      # store (fixed: the app-data volume; .env's DB_PATH is for local runs only)
      DB_PATH: /data/ai-newsdesk.sqlite
      # runtime
      TZ: ${TZ:-Europe/London}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      GIT_SHA: ${GIT_SHA:-unknown}
      SCHEDULER_ENABLED: ${SCHEDULER_ENABLED:-true}
    # the image's /data is owned 10001; a fresh named volume inherits that ownership
    volumes: ["app-data:/data"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    deploy:
      resources:
        limits: { memory: "${APP_MEM_LIMIT:-512m}", cpus: "${APP_CPUS:-0.5}" }
    logging: *logging
    networks:
      internal: {}
      proxy:
        aliases: [ai-newsdesk-app]

networks:
  internal: {}
  # VM-wide network owned by the shared Caddy project (/opt/caddy); never created here
  proxy:
    external: true
    name: proxy

volumes:
  app-data: {}
```

- [ ] **Step 2: Write `compose.dev.yaml`**

```yaml
# Dev/test override. ALWAYS use it with its own project name, never the live one:
#   docker compose -p ai-newsdesk-dev -f compose.yaml -f compose.dev.yaml up -d --build --wait
# A dev-only image name (no ${GIT_SHA}): a dev `up --build` can never overwrite a live or
# rollback `ai-newsdesk-app:<sha>` tag.
services:
  app:
    image: ai-newsdesk-app-dev:local

# Dev runs never join the VM-wide `proxy` network: a project-local one replaces it, so a dev
# container can't reach research-engine-app or be reached by Caddy. Integration tests that need
# research-engine run on the host with RESEARCH_ENGINE_URL pointing at the live one (README).
networks:
  proxy:
    external: false
    name: ai-newsdesk-dev-proxy
```

- [ ] **Step 3: Write `compose.debug.yaml`**

```yaml
# Debug override: publish the app on 127.0.0.1:${APP_PORT} only (loopback, never 0.0.0.0).
# The ops scripts add it automatically when APP_PORT is set (non-empty) in .env.
services:
  app:
    ports: ["127.0.0.1:${APP_PORT:?set APP_PORT in .env to use compose.debug.yaml}:8000"]
```

- [ ] **Step 4: Replace `.env.example`** with exactly:

```dotenv
# ai-newsdesk configuration. Copy to .env, then `chmod 600 .env`. Never commit .env.
# Secrets: generate each with `openssl rand -hex 32` (at least 32 characters, never change-me).

# --- identity and secrets ---
# Public host name Caddy serves; must equal AI_NEWSDESK_HOST in /opt/caddy/.env.
SITE_HOST=ai-newsdesk.localhost
SECRETS_KEY=change-me
SESSION_SECRET=change-me
# Set by `make set-password` (single-quoted: the argon2 hash contains `$`). Empty = login disabled.
OWNER_PASSWORD_HASH=

# --- API keys (fallbacks; a key saved on the Settings page wins) ---
ANTHROPIC_API_KEY=
RESEARCH_ENGINE_API_KEY=
TYPESAFE_API_KEY=

# --- services ---
RESEARCH_ENGINE_URL=http://research-engine-app:8000
# Local (non-Compose) runs only; Compose fixes it to /data/ai-newsdesk.sqlite.
DB_PATH=data/ai-newsdesk.sqlite

# --- runtime ---
TZ=Europe/London
LOG_LEVEL=INFO
# Leave as is: make deploy / make rollback set GIT_SHA per run.
GIT_SHA=unknown
SCHEDULER_ENABLED=true

# --- Compose only (never passed to the app) ---
APP_MEM_LIMIT=512m
APP_CPUS=0.5
# Set to a free local port to publish the app on 127.0.0.1 only (compose.debug.yaml). Empty = off.
APP_PORT=
```

- [ ] **Step 5: Check that Compose accepts all three files** (no daemon state changes; `config` only renders). Use a throwaway env file in the scratch dir so the real `.env` is never read or printed:

```bash
tmp="$(mktemp -d)"; sed 's/=change-me$/=0123456789abcdef0123456789abcdef/' .env.example > "$tmp/ci.env"
docker compose -p ai-newsdesk-dev -f compose.yaml -f compose.dev.yaml --env-file "$tmp/ci.env" config -q && echo dev-ok
APP_PORT=18080 docker compose -p ai-newsdesk-dev -f compose.yaml -f compose.dev.yaml -f compose.debug.yaml --env-file "$tmp/ci.env" config -q && echo debug-ok
docker compose -f compose.yaml --env-file "$tmp/ci.env" config --format json | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c["name"], sorted(c["services"]), "ports" in c["services"]["app"])'
rm -rf "$tmp"
```

Expected:
```
dev-ok
debug-ok
ai-newsdesk ['app'] False
```

- [ ] **Step 6: Commit** (the policy test that pins all of this follows in Task 34, in the same PR):

```bash
git add compose.yaml compose.dev.yaml compose.debug.yaml .env.example
git commit -m "build(compose): co-tenant ai-newsdesk project with dev and debug overrides (V1-S6)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 7: Note for later steps (no action).** Never run `docker compose up` against project `ai-newsdesk` by hand: step 7's `make deploy` runs it from `.deploy/current` with the right `GIT_SHA`. Dev runs always pass `-p ai-newsdesk-dev` with `compose.dev.yaml`; without `-p` the override would apply to project `ai-newsdesk` and replace the live stack.

- [ ] **Step 8: Note for integration tests (no action, documented in step 7's README/CLAUDE.md).** A dev container cannot reach research-engine. Integration tests that need it run on the host:

```bash
bundle="$(mktemp)"; cat "$(uv run python -m certifi)" /opt/caddy/root.crt > "$bundle"
RESEARCH_ENGINE_URL=https://research.toolbox.home.arpa SSL_CERT_FILE="$bundle" \
  uv run pytest -m integration
rm -f "$bundle"
```

The host name resolves to the VM's lab IP through the lab DNS, so Caddy's lab gate admits it; the combined CA bundle trusts both public CAs (Anthropic) and Caddy's internal CA. With `RESEARCH_ENGINE_URL` unset, those tests skip.

---

### Task 34: Compose policy test

**Goal:** A YAML-only test (no Docker needed) that fails if any co-tenancy, hardening, network, volume or environment rule regresses, or if `compose.yaml`, `Settings` and `.env.example` drift apart.

**Files:**
- Create: `tests/test_compose_policy.py`

**Acceptance Criteria:**
- [ ] The test checks: project name; single service; forbidden keys (incl. `env_file`, `cap_add`, `ports`, `container_name`); no Docker socket; good co-tenant (restart, healthcheck, logging, limits); pinned image with non-empty default tag; hardening; tmpfs; `security_opt` exactly `no-new-privileges:true`; networks (`proxy` external, only `app` on it, alias `ai-newsdesk-app`); named volumes only; env covers every `Settings` field with matching defaults; secrets use `:?`; `DB_PATH` fixed; `.env.example` in sync with `Settings` and every `${VAR}`; `.env.example` placeholders only; dev and debug overrides.
- [ ] It passes against Task 33's files and fails when, for example, `ports:` is added to `app` (checked by hand once in step 4).

**Verify:** `uv run pytest tests/test_compose_policy.py -v` → 21 passed.

**Steps:**

- [ ] **Step 1: Write the test** `tests/test_compose_policy.py`:

```python
"""Compose policy (V1-S6): co-tenancy on the shared toolbox VM, hardening, networks, volumes and
environment drift. Parses YAML only, so it needs no Docker daemon (CI runs it as-is)."""

import re
from pathlib import Path
from typing import Any

import yaml

from ai_newsdesk.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / name).read_text())


def _raw(name: str) -> str:
    """The file with comments stripped."""
    return "\n".join(line.split("#", 1)[0] for line in (ROOT / name).read_text().splitlines())


COMPOSE = _load("compose.yaml")
RAW = _raw("compose.yaml")
SERVICES: dict[str, Any] = COMPOSE["services"]
APP: dict[str, Any] = SERVICES["app"]
DEV = _load("compose.dev.yaml")
DEBUG = _load("compose.debug.yaml")

PROJECT = "ai-newsdesk"
ALIAS = "ai-newsdesk-app"
# Settings fields whose value compose fixes rather than taking from .env
COMPOSE_FIXED = {"DB_PATH": "/data/ai-newsdesk.sqlite"}
# Required secrets: compose refuses to start without them (${VAR:?message})
SECRETS = {"SECRETS_KEY", "SESSION_SECRET"}
# Compose-only variables: documented in .env.example, never passed to the app
COMPOSE_ONLY = {"APP_MEM_LIMIT", "APP_CPUS", "APP_PORT"}


def _settings_env_names() -> set[str]:
    return {f.upper() for f in Settings.model_fields}


def _env_example() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def _mounts(svc: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise short- and long-syntax volume entries to {type, source, target}."""
    out: list[dict[str, Any]] = []
    for v in svc.get("volumes", []):
        if isinstance(v, dict):
            out.append(
                {"type": v.get("type"), "source": str(v.get("source", "")), "target": v.get("target")}
            )
            continue
        parts = str(v).split(":")
        src, tgt = (parts[0], parts[1]) if len(parts) > 1 else ("", parts[0])
        bind = src.startswith((".", "/", "~", "$"))
        out.append(
            {"type": "bind" if bind else ("volume" if src else "anonymous"), "source": src, "target": tgt}
        )
    return out


def test_project_name() -> None:
    assert COMPOSE["name"] == PROJECT


def test_single_app_service() -> None:
    assert set(SERVICES) == {"app"}
    assert not APP.get("profiles")


def test_forbidden_settings() -> None:
    forbidden = (
        "container_name",
        "ports",
        "expose",
        "privileged",
        "pid",
        "devices",
        "network_mode",
        "volumes_from",
        "env_file",
        "cap_add",
        "extra_hosts",
    )
    for name, svc in SERVICES.items():
        for key in forbidden:
            assert key not in svc, f"{name} sets {key}"
        assert str(svc.get("ipc", "")) not in ("host",), f"{name} shares ipc"
        assert not str(svc.get("ipc", "")).startswith(("container:", "service:")), name
        assert svc.get("userns_mode") != "host", name
        assert svc.get("cgroup") != "host", name
    for word in ("SYS_ADMIN", "NET_ADMIN", "docker.sock", "privileged"):
        assert word not in RAW, word


def test_every_service_is_a_good_cotenant() -> None:
    for name, svc in SERVICES.items():
        assert svc["restart"] == "unless-stopped", name
        assert "healthcheck" in svc, name
        assert svc["logging"]["driver"] == "json-file", name
        opts = svc["logging"]["options"]
        assert "max-size" in opts and "max-file" in opts, name
        limits = svc["deploy"]["resources"]["limits"]
        assert "memory" in limits and "cpus" in limits, name


def test_limits_have_documented_defaults() -> None:
    limits = APP["deploy"]["resources"]["limits"]
    assert limits["memory"] == "${APP_MEM_LIMIT:-512m}"
    assert str(limits["cpus"]) == "${APP_CPUS:-0.5}"


def test_healthcheck_hits_health_endpoint() -> None:
    test = " ".join(APP["healthcheck"]["test"])
    assert "http://127.0.0.1:8000/health" in test


def test_image_pinned_and_built_locally() -> None:
    image = str(APP["image"])
    assert image == "ai-newsdesk-app:${GIT_SHA:-dev}"
    tag = image.partition(":")[2]
    assert "latest" not in tag.lower()
    for default in re.findall(r"\$\{\w+(?::?-([^}]*))?\}", tag):
        assert default.strip(), "an interpolated tag needs a non-empty default"
    assert APP["build"]["context"] == "."
    assert APP["build"]["args"]["GIT_SHA"] == "${GIT_SHA:-unknown}"


def test_hardening() -> None:
    assert APP["cap_drop"] == ["ALL"]
    assert APP["security_opt"] == ["no-new-privileges:true"]
    assert APP["read_only"] is True
    assert APP["user"] == "10001:10001"
    assert APP["stop_grace_period"] == "30s"
    assert not any("cap_add" in s for s in SERVICES.values())


def test_app_tmpfs() -> None:
    tmpfs = APP["tmpfs"]
    tmpfs = [tmpfs] if isinstance(tmpfs, str) else tmpfs
    assert len(tmpfs) == 1
    target, _, opts = str(tmpfs[0]).partition(":")
    assert target == "/tmp"  # noqa: S108 - a tmpfs mount target
    assert {"noexec", "nosuid", "nodev"} <= set(opts.split(","))


def test_networks() -> None:
    assert COMPOSE["networks"] == {"internal": {}, "proxy": {"external": True, "name": "proxy"}}
    on_proxy = [n for n, s in SERVICES.items() if "proxy" in (s.get("networks") or {})]
    assert on_proxy == ["app"]
    assert APP["networks"]["proxy"]["aliases"] == [ALIAS]
    assert "internal" in APP["networks"]


def test_volumes_named_only() -> None:
    assert COMPOSE["volumes"] == {"app-data": {}}
    for name, svc in SERVICES.items():
        mounts = _mounts(svc)
        assert all(m["type"] == "volume" for m in mounts), f"{name}: {mounts}"
        assert all(m["source"] in COMPOSE["volumes"] for m in mounts), f"{name}: {mounts}"
    assert APP["volumes"] == ["app-data:/data"]


def test_app_env_covers_every_setting() -> None:
    """Every Settings field reaches the app through an explicit mapping, so they can't drift."""
    env: dict[str, str] = APP["environment"]
    assert set(env) == _settings_env_names()
    for key in _settings_env_names() - set(COMPOSE_FIXED):
        value = str(env[key])
        if key in SECRETS:
            assert re.fullmatch(rf"\$\{{{key}:\?[^}}]+\}}", value), f"{key}: {value}"
        else:
            assert re.fullmatch(rf"\$\{{{key}:-[^}}]*\}}", value), f"{key}: {value}"


def test_compose_fixed_values() -> None:
    env: dict[str, str] = APP["environment"]
    for key, value in COMPOSE_FIXED.items():
        assert env[key] == value, key


def test_app_env_defaults_match_settings() -> None:
    env: dict[str, str] = APP["environment"]
    for name, field in Settings.model_fields.items():
        key = name.upper()
        if key in COMPOSE_FIXED or key in SECRETS:
            continue
        m = re.fullmatch(rf"\$\{{{key}:-([^}}]*)\}}", str(env[key]))
        assert m is not None, key
        default = field.default
        want = str(default).lower() if isinstance(default, bool) else str(default)
        assert m.group(1) == want, f"{key}: compose {m.group(1)!r} != Settings {default!r}"


def test_research_engine_reached_over_proxy_alias() -> None:
    assert APP["environment"]["RESEARCH_ENGINE_URL"] == (
        "${RESEARCH_ENGINE_URL:-http://research-engine-app:8000}"
    )


def test_compose_only_vars_stay_out_of_app() -> None:
    assert not (set(APP["environment"]) & COMPOSE_ONLY)


def test_env_example_in_sync() -> None:
    keys = set(_env_example())
    assert _settings_env_names() <= keys
    compose_vars = set(re.findall(r"\$\{(\w+)[:}]", RAW))
    assert compose_vars <= keys, "every ${VAR} in compose.yaml is documented in .env.example"
    assert COMPOSE_ONLY <= keys
    assert keys <= _settings_env_names() | COMPOSE_ONLY, "no undocumented extras"


def test_env_example_holds_placeholders_only() -> None:
    env = _env_example()
    for key in SECRETS:
        assert env[key] == "change-me", key
    empty = ("OWNER_PASSWORD_HASH", "ANTHROPIC_API_KEY", "RESEARCH_ENGINE_API_KEY")
    for key in (*empty, "TYPESAFE_API_KEY", "APP_PORT"):
        assert env[key] == "", key
    assert env["SITE_HOST"].endswith(".localhost")


def test_dev_override_renames_image_and_isolates_network() -> None:
    """A dev `up --build` must never overwrite a live or rollback `ai-newsdesk-app:<sha>` tag,
    and a dev container must never join the VM-wide proxy network."""
    assert set(DEV) == {"services", "networks"}
    assert set(DEV["services"]) == {"app"}
    app = DEV["services"]["app"]
    assert set(app) == {"image"}
    image = app["image"]
    assert image == "ai-newsdesk-app-dev:local" and "$" not in image
    assert image.partition(":")[0] != APP["image"].partition(":")[0]
    assert DEV["networks"] == {"proxy": {"external": False, "name": "ai-newsdesk-dev-proxy"}}


def test_dev_override_publishes_nothing() -> None:
    assert "ports" not in DEV["services"]["app"]


def test_debug_override_app_loopback_only() -> None:
    assert set(DEBUG) == {"services"}
    assert set(DEBUG["services"]) == {"app"}
    app = DEBUG["services"]["app"]
    assert set(app) == {"ports"}
    assert len(app["ports"]) == 1
    assert re.fullmatch(r"127\.0\.0\.1:\$\{APP_PORT:\?[^}]+\}:8000", app["ports"][0])
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_compose_policy.py -v`
Expected: 21 passed. (Task 33 is already committed, so this test goes straight to green. To prove it bites, do step 3.)

- [ ] **Step 3: Prove it fails on a regression** (temporary edit, never committed):

```bash
sed -i 's/^    restart: unless-stopped$/    restart: unless-stopped\n    ports: ["8000:8000"]/' compose.yaml
uv run pytest tests/test_compose_policy.py -q -k "forbidden"; git checkout -- compose.yaml
```

Expected: `1 failed` with `app sets ports`, then `git status --short compose.yaml` prints nothing.

- [ ] **Step 4: Full suite**

Run: `uv run ruff format tests && make test`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add tests/test_compose_policy.py
git commit -m "test(compose): co-tenancy, hardening and env drift policy (V1-S6)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 6: Push and open the PR** (main is protected):

```bash
git push -u origin v1/step-6-compose
GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr create --base main \
  --title "V1 step 6: container image and Compose (V1-S6)" \
  --body "Dockerfile, .dockerignore, compose.yaml (+dev, +debug), .env.example and the Dockerfile/Compose policy tests. YAML-only tests: no Docker needed in CI.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Then run `requesting-code-review`, fix findings, wait for CI green (`GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr checks --watch`), and merge with `GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr merge --merge --delete-branch`.

---

## Step done when

- [ ] `uv run pytest tests/test_dockerfile_policy.py tests/test_compose_policy.py` passes (31 tests) and `make test` is green.
- [ ] `docker build` of the dev tag works and `Europe/London` resolves inside the image.
- [ ] `docker compose … config -q` accepts prod, dev and debug combinations with a throwaway env file.
- [ ] No `ports`, `container_name`, `env_file`, `cap_add` or bind mount anywhere in `compose.yaml`; dev override uses project `ai-newsdesk-dev` and network `ai-newsdesk-dev-proxy`.
- [ ] `.env.example` lists every `Settings` env var plus `APP_MEM_LIMIT`, `APP_CPUS`, `APP_PORT`, placeholders only.
- [ ] PR merged into `main` with CI green.
