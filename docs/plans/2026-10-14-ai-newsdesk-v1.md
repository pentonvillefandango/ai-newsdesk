# ai-newsdesk V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The detailed tasks, with code, live in one file per step under `docs/plans/v1/`. This file holds the shared contract that every step must match.

**Goal:** A self-hosted, lab-only AI news feed at `ai-newsdesk.toolbox.home.arpa`. It collects AI news from labs and trusted people (X via research-engine), filters out junk with Claude Haiku 5.5, groups duplicates into stories, summarises them and writes a daily briefing, all behind a polished web UI.

**Architecture:** One FastAPI container (Compose project `ai-newsdesk`), SQLite on a named volume, and an in-process asyncio scheduler. Pages are server-rendered with Jinja2, htmx and hand-written CSS. Every external dependency sits behind a protocol: `Source`, `ResearchEngine`, `Judge`, `Writer`, `Clock`. Only `app.py` wires up the concrete classes. The app joins the shared `proxy` network as `ai-newsdesk-app`, reaches research-engine at `http://research-engine-app:8000`, and is reached only through the shared Caddy.

**Tech Stack:** Python ≥3.12 (image `python:3.13.16-slim-trixie`), uv 0.12.23, FastAPI, uvicorn, Jinja2, htmx 2 (vendored static file), httpx, feedparser, pydantic v2 and pydantic-settings, aiosqlite, anthropic (official SDK), argon2-cffi, cryptography (Fernet), itsdangerous, PyYAML, python-multipart. Optional extra `jev`: typesafe-sdk. Dev: pytest, pytest-asyncio, respx, ruff, pyright, pre-commit (with gitleaks).

**Spec:** `docs/superpowers/specs/2026-10-14-v1-design.md` and `docs/adr/0001-claude-haiku-is-the-v1-judge.md`. Executors read both.

## Global Constraints

- **Co-tenancy (toolbox VM, non-negotiable):**
  - `name: ai-newsdesk` in `compose.yaml`;
  - no `ports:`, no `container_name`, no VM-wide prune;
  - address the stack only by Compose project `ai-newsdesk`;
  - named volumes only (binds only for read-only `./deploy/` config);
  - every service has a pinned tag, `restart: unless-stopped`, a healthcheck, json-file logging with `max-size`/`max-file`, and `deploy.resources.limits` (memory and cpus);
  - hardening: `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `read_only: true` with a `/tmp` tmpfs `noexec,nosuid,nodev`, and `user: "10001:10001"`;
  - only `app` joins `proxy`, which is declared as `proxy: {external: true, name: proxy}`, with alias `ai-newsdesk-app`.
- **Never touch another tool's files, containers, volumes or `.env`.** The exceptions are the explicitly listed Caddy steps (append `AI_NEWSDESK_HOST` to `/opt/caddy/.env`, add `/opt/caddy/sites/ai-newsdesk.caddy`, recreate the Caddy stack) and the research-engine index-link PR. Both are in step 8.
- **Secrets:**
  - they live only in `.env` (mode 600, git-ignored) or encrypted in the database;
  - never print, log or commit them; check lengths only;
  - the repo is **public**: commit no lab IPs, real hostnames (the documented example `*.toolbox.home.arpa` is allowed) or usernames;
  - gitleaks runs in pre-commit and CI.
- **AI calls:**
  - Default model `claude-haiku-5-5` for judge, writer and briefing.
  - Model IDs are exact strings from `config/pricing.yaml`; never add date suffixes.
  - Use `client.messages.parse(..., output_format=PydanticModel)` for structured output, with `output_config={"effort": "low"}` for judging.
  - No assistant prefill and no `temperature`.
  - Check `stop_reason` before reading output: a `refusal` or a `None` `parsed_output` counts as a failure for those items.
- **Untrusted content:**
  - all fetched text goes to the model inside `<item>` tags, with a system prompt saying it is data, never instructions;
  - in templates it goes through Jinja autoescape only (never `|safe` on fetched or model text);
  - outbound links get `rel="noopener noreferrer"`.
- **Budget:** a hard monthly cap (default $5.00, calendar month Europe/London) is checked before **every** AI call using the worst-case cost (input estimate plus `max_tokens` of output at output price). A call that doesn't fit is skipped, never sent.
- **research-engine politeness (ADR-0001):** at most 3 concurrent requests. Retry 429, 5xx without a JSON envelope, and transport errors up to 3 times, with 5 s and then 10 s backoff. A failed fetch falls back to the snippet. X pages are never fetched, because robots disallows them.
- **Failures are local:** one source, item or AI batch failing never fails a run. An item with no verdict is never hidden.
- **Time:** all stored timestamps are UTC ISO-8601. Schedules and the budget month are Europe/London (`TZ` setting).
- **TDD always:** a failing test first, watch it fail, then minimal code. `make test` = `uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest`.
- **Commits:**
  - small, one per task;
  - Conventional Commits with the step ID, e.g. `feat(store): items table and dedupe (V1-S1)`;
  - work on a branch per step, merged to `main` through a PR (`main` is protected);
  - end every commit message with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- **Docs honesty (`tests/test_docs.py`):**
  - `README.md`'s configuration table names every `.env.example` key;
  - `docs/OPERATIONS.md` names every make target;
  - update docs in the same commit as the code.

**User decisions (already made):**
- Name `ai-newsdesk`; our own code; public repo `pentonvillefandango/ai-newsdesk`; full process (Superpowers, TDD, reviews).
- Purpose: a high-signal AI news feed from labs and respected people, junk filtered out; research-engine is close to mandatory.
- **No paid X access, ever.** X comes in via research-engine `site:x.com/<handle>` searches, as snippets only.
- Claude API for AI. Model selectable in settings; monthly budget $5.
- Update frequency is a setting, default 4 runs a day (00:00, 06:00, 12:00, 18:00 Europe/London). There is also a daily review cap (default 500).
- "Why it matters" is an on/off setting. Models and API keys are entered in settings; keys are write-only.
- Simple owner-password login.
- Approach A: one FastAPI app, server-rendered, htmx, crafted CSS. The web UI must look beautiful.
- Agent-written deep-dive articles are V2, out of scope.
- **The judge is Claude Haiku 5.5** (bake-off, ADR-0001). TypeSafe Jev stays an optional `Judge` behind `judge_provider`.

---

## File structure

```
ai-newsdesk/
├── pyproject.toml  uv.lock  Makefile  Dockerfile  .dockerignore  .env.example  .gitignore
├── .pre-commit-config.yaml  .github/workflows/ci.yml
├── compose.yaml  compose.dev.yaml  compose.debug.yaml
├── config/
│   ├── pricing.yaml          # model id -> provider, input/output $ per Mtok
│   └── sources.yaml          # seed sources, loaded into DB on first run
├── deploy/caddy/ai-newsdesk.caddy
├── ops/  lib.sh _deploy_lib.sh deploy.sh rollback.sh health.sh status.sh logs.sh backup.sh smoke.sh version.sh set_password.sh
├── src/ai_newsdesk/
│   ├── __init__.py
│   ├── config.py             # Settings (pydantic-settings) - env only
│   ├── models.py             # shared pydantic models + enums (contract below)
│   ├── protocols.py          # Source, ResearchEngine, Judge, Writer, Clock
│   ├── clock.py              # SystemClock, FixedClock (tests)
│   ├── store/  __init__.py db.py migrations/0001_init.sql store.py
│   ├── secrets_box.py        # Fernet encrypt/decrypt of API keys in settings table
│   ├── runtime_settings.py   # typed view over the settings table (frequency, caps, models, toggles)
│   ├── research_engine.py    # HttpResearchEngine
│   ├── sources/  __init__.py base.py feed.py x_person.py search.py hn.py arxiv.py github.py registry.py
│   ├── normalise.py  dedupe.py  prefilter.py
│   ├── budget.py  pricing.py
│   ├── judges/  __init__.py haiku.py jev.py prompts.py
│   ├── writer.py  cluster.py  briefing.py  feedback.py  taste.py
│   ├── pipeline.py  scheduler.py
│   ├── web/  __init__.py app_routes.py auth.py csrf.py deps.py
│   │   ├── templates/  base.html today.html story_card.html briefing.html sources.html settings.html login.html partials/*.html
│   │   └── static/  app.css  htmx.min.js  icons.svg
│   ├── app.py                # create_app() factory: wiring, lifespan, scheduler start/stop
│   └── cli.py                # `ai-newsdesk` CLI: db backup, hash-password
├── tests/  conftest.py fakes.py fixtures/ test_*.py  ops/
└── docs/  OPERATIONS.md  adr/  plans/  superpowers/specs/
```

## Shared contract (every step must use these exact names)

### `src/ai_newsdesk/models.py`

```python
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
    config: dict[str, str | int | float | bool | list[str]]  # e.g. {"url": ...} / {"handle": ...} / {"query": ...}
    trust: Trust
    enabled: bool = True
    weight: float = 1.0                                       # feedback weight, 0.3..2.0


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
    text: str                                  # body excerpt, else snippet, else "(no text, title only)"
    story_candidates: list[StoryRef] = Field(default_factory=list)


class Verdict(BaseModel):
    item_id: int
    signal: int = Field(ge=0, le=10)
    junk: bool
    category: Category
    same_story_id: int | None = None


class Usage(BaseModel):
    provider: str                              # "anthropic" | "typesafe"
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
    texts: list[str]                           # best 1-2 item texts ("title\n\ntext")
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
    url: str                                   # lead item's url


class Story(BaseModel):                        # what the web layer renders
    id: int
    headline: str
    category: Category
    score: float
    summary: str | None
    why_it_matters: str | None
    first_seen_at: datetime
    last_updated_at: datetime
    feedback: int                              # -1, 0, 1
    sources: list["StorySource"] = Field(default_factory=list)


class StorySource(BaseModel):
    item_id: int
    source_name: str
    source_kind: SourceKind
    url: str
    title: str
    published_at: datetime | None = None
```

### `src/ai_newsdesk/protocols.py`

```python
from datetime import datetime
from typing import Protocol
from ai_newsdesk.models import (BriefingStory, JudgeResult, ReviewInput, SearchHit, SourceConfig,
                                SourceKind, SourceResult, StoryText, Usage, WriteInput)


class Clock(Protocol):
    def now(self) -> datetime: ...            # timezone-aware UTC


class ResearchEngine(Protocol):
    async def search(self, query: str, *, max_results: int = 10, time_range: str | None = "day") -> list[SearchHit]: ...
    async def fetch_markdown(self, url: str) -> str | None: ...   # None on any failure
    async def health(self) -> bool: ...


class Source(Protocol):
    kind: SourceKind
    async def collect(self, src: SourceConfig, since: datetime) -> SourceResult: ...   # never raises


class Judge(Protocol):
    model: str
    def worst_case_cost(self, inputs: list[ReviewInput]) -> float: ...   # USD, used by budget pre-check
    async def review(self, inputs: list[ReviewInput]) -> JudgeResult: ...  # never raises; failures -> failed_item_ids


class Writer(Protocol):
    model: str
    def worst_case_cost_stories(self, inputs: list[WriteInput]) -> float: ...
    def worst_case_cost_briefing(self, stories: list[BriefingStory]) -> float: ...
    async def write_stories(self, inputs: list[WriteInput]) -> tuple[list[StoryText], list[Usage]]: ...  # never raises; missing stories simply absent
    async def write_briefing(self, stories: list[BriefingStory], date_label: str) -> tuple[str | None, list[Usage]]: ...
```

### `config.py` — `Settings` (environment only, read once at start-up)

| Field (env var) | Type | Default |
|---|---|---|
| `SITE_HOST` | str | `ai-newsdesk.localhost` |
| `SECRETS_KEY` | str (required, ≥32 chars) | — |
| `SESSION_SECRET` | str (required, ≥32 chars) | — |
| `OWNER_PASSWORD_HASH` | str (argon2 hash, required in prod; empty → login disabled with a banner telling the owner to run `make set-password`) | `""` |
| `ANTHROPIC_API_KEY` | str | `""` |
| `RESEARCH_ENGINE_API_KEY` | str | `""` |
| `TYPESAFE_API_KEY` | str | `""` |
| `RESEARCH_ENGINE_URL` | str | `http://research-engine-app:8000` |
| `DB_PATH` | str | `data/ai-newsdesk.sqlite` (compose fixes it to `/data/ai-newsdesk.sqlite`) |
| `TZ` | str | `Europe/London` |
| `LOG_LEVEL` | str | `INFO` |
| `GIT_SHA` | str | `unknown` |
| `SCHEDULER_ENABLED` | bool | `true` |

### `runtime_settings.py` — `RuntimeSettings` (in the DB `settings` table, edited on the Settings page)

| Key | Type | Default |
|---|---|---|
| `runs_per_day` | int in {1,2,4,6,12,24} | 4 |
| `briefing_time` | "HH:MM" | "07:00" |
| `daily_review_cap` | int | 500 |
| `monthly_budget_usd` | float | 5.0 |
| `judge_provider` | "anthropic" \| "typesafe" | "anthropic" |
| `judge_model` / `writer_model` / `briefing_model` | str (key in pricing.yaml) | "claude-haiku-5-5" |
| `show_threshold` | int 0-10 | 4 |
| `why_it_matters` | bool | true |
| `hn_min_points` | int | 100 |

Encrypted API keys are stored under the keys `secret.anthropic`, `secret.research_engine` and `secret.typesafe`, via `secrets_box`. A key entered in the UI wins over the `.env` value. `resolve_api_key(name)` returns the UI value, then the env value, then `""`.

### Store method names used across steps (`store/store.py`, class `Store`, all `async`)

`init()` · `seed_sources_if_empty(path)` · `list_sources(enabled_only=True) -> list[SourceConfig]` · `get_source(id)` · `upsert_source(...)` · `delete_source(id)` · `record_source_result(SourceResult, at)` · `add_candidates(list[Candidate], at) -> list[int]` (new item IDs; existing canonical URLs skipped) · `recent_titles(hours) -> list[tuple[int, str, int | None]]` (item_id, title, story_id) · `set_item_fields(item_id, **fields)` · `queued_review_inputs(limit, now) -> list[ReviewInput]` · `expire_old_queued(now, hours=48) -> int` · `save_verdict(Verdict, signal_final)` · `mark_items(ids, ItemStatus)` · `open_stories(now, hours=72) -> list[StoryRef]` · `create_story(headline, category, at) -> int` · `attach_item(item_id, story_id, at)` · `recompute_story_score(story_id)` · `stories_needing_text(limit, include_why) -> list[WriteInput]` · `save_story_text(StoryText)` · `today_stories(now, threshold, hours=24) -> list[Story]` · `top_stories_for_briefing(now, limit=15) -> list[BriefingStory]` · `save_briefing(date, model, body, story_ids, at)` · `get_briefing(date) / latest_briefing() / briefing_dates()` · `add_spend(Usage, cost_usd, at)` · `month_spend(now, tz) -> float` · `spend_by_day(now, tz) -> list[tuple[str, float]]` · `reviewed_count_today(now, tz) -> int` · `start_run(trigger, at) -> int` · `finish_run(run_id, at, counts, cost, error)` · `last_runs(n)` · `get_setting(key) / set_setting(key, value)` · `set_feedback(story_id, value)` · `source_feedback(days, now) -> dict[int, tuple[int, int]]` · `feedback_examples() -> list[tuple[dict, int]]` · `backup(dest_path)`.

### Pipeline entry points

- `pipeline.run_once(ctx: PipelineContext, trigger: str) -> RunReport`. `PipelineContext` is a dataclass holding `store`, `sources: dict[SourceKind, Source]`, `research_engine`, `judge`, `writer`, `budget`, `clock` and `settings`.
- `pipeline.run_briefing(ctx, date) -> bool`.
- `scheduler.Scheduler(ctx).start() / .stop()`. It computes the next slot from `runs_per_day` and `briefing_time` in the `TZ` zone, re-reads settings every minute, and never overlaps runs (an `asyncio.Lock`).

## Steps

Each step is one branch and one PR. Tasks are numbered globally. The detail, with code and tests, is in the step file.

| Step | File | Tasks | Produces |
|---|---|---|---|
| 0 | `v1/step-0-scaffold.md` | 1–4 | Repo tooling, CI, config, models, protocols, clock, fakes |
| 1 | `v1/step-1-store.md` | 5–8 | SQLite schema and migrations, Store, secrets box, runtime settings, pricing |
| 2 | `v1/step-2-sources.md` | 9–14 | research-engine client, normalise, all source adapters, seed sources |
| 3 | `v1/step-3-pipeline.md` | 15–22 | dedupe, prefilter, budget, HaikuJudge (+ JevJudge), writer, cluster, briefing, pipeline, scheduler |
| 4 | `v1/step-4-feedback.md` | 23–24 | Feedback weights, taste model |
| 5 | `v1/step-5-web.md` | 25–31 | Auth, CSRF, layout and design system, Today, Briefing, Sources, Settings (keys, toggles, models, frequency, costs), health |
| 6 | `v1/step-6-compose.md` | 32–34 | Dockerfile, compose (prod, dev, debug), compose policy test |
| 7 | `v1/step-7-ops-docs.md` | 35–38 | Ops scripts, Makefile, OPERATIONS.md, README, CLAUDE.md, docs test |
| 8 | `v1/step-8-deploy.md` | 39–42 | Caddy site, index-link PR in research-engine, first deploy, end-to-end verification, handoff |

Dependencies: each step depends on all earlier steps (R5: step 5 needs step 4), except that step 6 depends only on step 0.

## Reconciliation decisions (binding; override any conflicting text in the step files)

The step files were written in parallel. Where they disagree, these rules win. The step files have been edited to match them.

- **R1 Store API.** Step 1 is the source of truth for every `Store` method signature. It also provides what the later steps need:
  - `items_for_enrichment(item_ids)` (step 2);
  - `today_stories` also returns any story that has an item in status `review_failed`, whatever its score (step 3);
  - `set_source_weight(source_id, weight)` and `story_features(story_ids)` (step 4);
  - `feedback_examples()` returns one entry per rated story, with the lead item's features (step 4);
  - `mark_text_failed(story_ids)`, which the step 3 pipeline calls for stories the writer left out.

  Later steps call `upsert_source` with step 1's keyword signature. Step 5's own Store additions (`stories_by_ids`, `unscored_items`, `source_overview`, `usage_last_days`) stay in step 5.
- **R2 Review retries.** `queued_review_inputs` returns `queued` items plus `review_failed` items with fewer than 2 attempts (step 1). The pipeline does not retry within a run. A failed item is retried on the next run, as spec section 8 says.
- **R3 API keys.** Step 1's `ApiKeys(store, settings, box)` is the only key store (`secret.<name>` → `{"token", "updated_at"}`), and `await api_keys.resolve_api_key(name)` is the only resolver. `HttpResearchEngine` takes `api_key_provider: Callable[[], Awaitable[str]]`, which `app.py` sets to `lambda: api_keys.resolve_api_key("research_engine")`. Step 5's key UI uses `ApiKeys.set_api_key` / `clear_api_key` / `api_key_status`.
- **R4 Briefing body format.** Step 3's format: plain text; `## ` starts a section heading; blank lines separate paragraphs; links are `[text](url)`, and `clean_briefing` keeps only links to the briefing's own story URLs. Step 5 escapes everything, then renders only those three constructs, and renders a link only if its URL is one of the briefing's story URLs.
- **R5 Step order.** Step 5 depends on step 4. Thumbs call `feedback.set_feedback(store, clock, story_id, value)`. "Never show stories like this" calls `feedback.add_blocklist_keyword`. The Today page ranks with `taste.rank_today`. `app.py` builds `Scheduler(ctx, nightly_fn=taste.nightly)`.
- **R6 Scheduler.** Step 3's API: `start()`, `async stop()`, `lock`, `alive`, `run_now(trigger)` (returns `None` if a run is already going). Refresh now calls `scheduler.run_now("manual")` as a background task.
- **R7 CLI.** Step 5 creates `cli.py` in the shape step 7 tests: argparse `main(argv=None) -> int`, subparsers with `set_defaults(func=...)`, and `hash-password` reads one stdin line when stdin is not a TTY. Step 5 also adds `[project.scripts] ai-newsdesk = "ai_newsdesk.cli:run"`, where `run()` calls `sys.exit(main())`. Step 7 adds `cli_db.register(sub)`.
- **R8 Wiring.** Step 5's `build_context` uses the real constructors:
  - `Store(db_path, clock=clock, canonicalise=normalise.canonical_url)`
  - `HttpResearchEngine(base_url, api_key_provider, client=http)`
  - `build_sources(http, research_engine, clock)`
  - `HaikuJudge(client, model, pricing)`
  - `JevJudge(api_key, pricing, model=...)`
  - `HaikuWriter(client, model, pricing, briefing_model=...)`
  - `Budget(store, pricing, clock, runtime, tz=settings.tz)`
  - `load_runtime_settings` / `save_runtime_settings` / `update_runtime_settings`
- **R9 `hn` sources.** When an `hn` source's config has no `min_points`, the pipeline injects `runtime.hn_min_points` (step 3).
