# Step 5: Web UI (auth, design system, Today, Briefing, Sources, Settings, health)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking. The shared contract is in `docs/plans/2026-10-14-ai-newsdesk-v1.md`; read its Global Constraints before starting.

**Goal:** The owner logs in with one password and reads a calm, beautiful, server-rendered newsroom: Today (stories by signal or by section, thumbs, every source), Briefing (today plus an archive), Sources (health, add, edit, mute, delete) and Settings (frequency with cost estimates, caps, models, write-only API keys tested on save, toggles, Refresh now, spend meter and per-day chart). `/health` reports the database, scheduler and last run.

**Branch:** `step-5-web` (from `main` once steps 0–4 are merged). One PR, merged through GitHub.

**Depends on:** steps 0–4 (master plan R5). Thumbs call step 4's `feedback.set_feedback(store, clock, story_id, value)` so source weights move at once; "Never show stories like this" calls `feedback.add_blocklist_keyword(store, keyword)`; Today ranks with `taste.rank_today`; the scheduler is built as `Scheduler(ctx, nightly_fn=taste.nightly)`.

**Architecture:** `app.py` is the composition root (`create_app`). `web/security.py` installs three pure-ASGI middlewares (security headers, CSRF cookie, auth gate) and the login routes. Each page has its own router module; all of them are combined in `web/app_routes.py`. Templates are Jinja2 with autoescape on and **no `|safe` anywhere**. CSS is one hand-written file. The only script is the vendored htmx 2.0.11, and every page works without it (forms post and redirect).

**Formatting note:** code blocks are laid out for reading. Run `uv run ruff format` before each commit; if a lint rule the repo enables (for example `E501` on a long string, or `S608` on the placeholder-only f-strings in the Store queries) objects, wrap the string or add a targeted `# noqa` with the rule code. Do not change behaviour to satisfy a linter.

**Tasks:** 25 auth + CSRF + login + `hash-password` · 26 `create_app`, lifespan, `/health` · 27 design system · 28 Today (thumbs, blocklist) · 29 Briefing · 30 Sources · 31 Settings.

---

## Contract additions

New cross-step names introduced by this step (other steps must use these exact names):

```python
# src/ai_newsdesk/app.py
@dataclass
class AppDeps:
    settings: Settings
    store: Store                     # Store(db_path, clock=clock, canonicalise=normalise.canonical_url)
    pricing: Pricing
    http: httpx.AsyncClient          # shared client, closed by the lifespan
    clock: Clock                     # SystemClock() in production; tests pass FixedClock
    api_keys: ApiKeys                # step 1's ApiKeys(store, settings): the only key store

CtxFactory = Callable[[AppDeps], Awaitable[PipelineContext]]

async def build_context(deps: AppDeps) -> PipelineContext: ...   # default factory: the only place concrete classes are wired

def create_app(
    settings: Settings | None = None,       # None -> Settings() from the environment
    *,
    ctx_factory: CtxFactory | None = None,  # tests inject fakes
    key_testers: Mapping[str, KeyTester] | None = None,  # tests inject fakes
    config_dir: Path = Path("config"),      # holds pricing.yaml and sources.yaml (cwd-relative; the image's WORKDIR must contain config/)
    clock: Clock | None = None,             # None -> SystemClock(); shared by the Store and the pipeline context
) -> FastAPI: ...
# uvicorn entry point: `uvicorn --factory ai_newsdesk.app:create_app`

# app.state after start-up: settings, store, pricing, http, api_keys (ApiKeys), deps, ctx (PipelineContext),
# scheduler (Scheduler: always built as Scheduler(ctx, nightly_fn=taste.nightly); start() only when
#   SCHEDULER_ENABLED, so Refresh now works either way), run_lock (the scheduler's own lock),
# background (set[asyncio.Task[None]]), key_testers (dict[str, KeyTester]),
# rebuild_context (async () -> None: rebuilds ctx after a settings or key change and hands it to the scheduler).
```

```python
# src/ai_newsdesk/web/keys.py  (provider testers only; storage is step 1's ApiKeys, master plan R3)
KEY_LABELS: dict[ApiKeyName, str]
KeyTester = Callable[[str, RuntimeSettings], Awaitable[str | None]]   # None = key works, else a message (scrubbed before display)
REDACTED = "[key removed]"; MAX_KEY_CHARS = 500; scrub(message, key) -> str
check_anthropic_key, research_engine_checker(base_url, http), check_typesafe_key, default_key_testers(settings, http)
```

```python
# src/ai_newsdesk/cli.py  (master plan R7; step 7 adds `cli_db.register(sub)`)
def main(argv: list[str] | None = None) -> int: ...  # argparse, prog="ai-newsdesk", sub = add_subparsers(dest="command", required=True),
                                                     # every command uses set_defaults(func=...), returns args.func(args)
def run() -> None: ...                               # console script: sys.exit(main())
# pyproject.toml: [project.scripts] ai-newsdesk = "ai_newsdesk.cli:run"
```

```python
# src/ai_newsdesk/models.py (appended by Task 28/30/31)
class UnscoredItem(BaseModel):      # an item with no verdict that must still be visible
    item_id: int
    source_name: str
    source_kind: SourceKind
    url: str
    title: str
    published_at: datetime | None = None
    status: ItemStatus

class SourceOverview(BaseModel):    # one row of the Sources page
    source: SourceConfig
    last_ok_at: datetime | None = None
    last_error: str | None = None
    items_7d: int = 0
    stories_7d: int = 0
    items_total: int = 0

class UsageStats(BaseModel):        # input to the Settings cost estimate
    days: int
    runs: int
    items_reviewed: int
    review_usd: float
    write_usd: float
    briefing_usd: float
```

New `Store` methods (all `async`, added by this step; they use step 1's `_all` / `_one` / `_ts` / `_dt` / `_req_dt` helpers, so they take the store lock and read the same timestamp format):
`stories_by_ids(ids: list[int]) -> dict[int, Story]` · `unscored_items(now, hours=24, limit=30) -> list[UnscoredItem]` · `source_overview(now) -> list[SourceOverview]` (built on step 1's `list_sources(enabled_only=False)` and `source_stats()`, adding only the 7-day counts) · `usage_last_days(now, days=7) -> UsageStats`. None of them duplicates a step 1 method.

**Briefing body format (step 3's "markdown-lite", master plan R4):** plain text; a line starting `## ` is a section heading; blank lines separate paragraphs (single newlines inside a paragraph fold into spaces); links are `[text](url)`. `web/briefing_text.py` escapes everything (autoescape; it never builds HTML) and renders exactly those three constructs. A link renders only when its URL is one of the briefing's story URLs (the item URLs of `briefing.story_ids`, read with `stories_by_ids`) and passes `safe_url`; any other link shows as its text.

**HTTP surface:** `GET /login`, `POST /login`, `POST /logout`, `GET /health` (open) · `GET /` (`?view=top|category`), `POST /stories/{id}/feedback`, `POST /stories/{id}/block` · `GET /briefing`, `GET /briefing/{yyyy-mm-dd}` · `GET /sources`, `POST /sources`, `GET /sources/{id}/edit`, `GET /sources/{id}/row`, `POST /sources/{id}`, `POST /sources/{id}/mute`, `POST /sources/{id}/delete` · `GET /settings`, `POST /settings`, `POST /settings/keys/{name}`, `POST /settings/keys/{name}/clear`, `POST /settings/refresh` · `/static/*` (open).

**Cookies and CSRF:** session cookie `nd_session` (signed with `SESSION_SECRET`, `Secure; HttpOnly; SameSite=Lax`, 14 days; invalidated when `OWNER_PASSWORD_HASH` changes). CSRF cookie `nd_csrf` (`Secure; HttpOnly; SameSite=Strict`), echoed in the form field `csrf_token` or the header `X-CSRF-Token` (htmx sends it from `hx-headers` on `<body>`).

**CLI:** `ai-newsdesk hash-password` prints the argon2id hash on one stdout line. On a TTY it reads the password twice with `getpass`; when stdin is not a TTY (step 7's `make set-password` pipes it in) it reads exactly one line from stdin. Note for step 7: the hash contains `$`; `make set-password` must write it to `.env` single-quoted so Compose does not interpolate it.

### Names used from steps 0–4 (real names; Task 26 Step 1 confirms them)

These come from the Contract additions of steps 0–4 and the master plan's Reconciliation decisions (R1–R8). Task 26 Step 1 greps for each one. If a name has drifted on `main`, fix the call site listed and record the difference in the PR description; do not change earlier steps' code from this branch.

| Name (step) | Used in |
|---|---|
| `config.Settings` (0): attributes are the lower-case env names (`owner_password_hash`, `session_secret`, `secrets_key`, `db_path`, `tz`, `git_sha`, `scheduler_enabled`, `research_engine_url`, `anthropic_api_key`, `research_engine_api_key`, `typesafe_api_key`, `site_host`, `log_level`), plain `str`/`bool`, constructible with keyword arguments | everywhere |
| `clock.SystemClock()`, `clock.FixedClock(at)` (0) | `app.py`, `tests/web` |
| `from ai_newsdesk.store import Store` (1, re-exported from `store/store.py`): `Store(db_path, *, clock=None, canonicalise=basic_canonical_url)`, `init()`, `close()`; locked private helpers `_all(sql, params)`, `_one(sql, params)`, `_tx()` (step-5 additions use these, never raw `self._db.execute`), module helpers `_ts(dt)`, `_dt(str \| None)`, `_req_dt(str)` | `app.py`, the four new Store methods |
| `upsert_source(kind, name, config, trust, *, enabled=True, weight=None, source_id=None) -> int` (1; `weight=None` keeps the learned weight on update, so the Sources page never passes one); `get_source(source_id)`; `list_sources(enabled_only=...)`; `delete_source(source_id)`; `source_stats() -> list[SourceStats]` | `routes_sources.py`, `source_overview`, `tests/web/seed.py` |
| `get_setting(key)` / `set_setting(key, value)` (1; `None` deletes) | `routes_health.py` |
| `month_spend(now, tz)`, `spend_by_day(now, tz) -> list[tuple[str, float]]` (1; `tz` is the zone name, pass `settings.tz`) | `deps.py`, `routes_settings.py` |
| `save_briefing(date: datetime.date, model, body, story_ids, at)`; `get_briefing(date) / latest_briefing() -> BriefingRecord \| None` with `.date: date`, `.model`, `.body`, `.story_ids: list[int]`, `.created_at`; `briefing_dates() -> list[date]` newest first (1) | `routes_briefing.py` |
| `last_runs(n) -> list[RunRecord]` with `trigger`, `started_at`, `finished_at`, `cost_usd`, `error` (1) | `web/runs.run_dict` |
| `runtime_settings` (1): `RuntimeSettings`, `RUNS_PER_DAY_CHOICES`, `load_runtime_settings(store)`, `save_runtime_settings(store, rs)`, `update_runtime_settings(store, **changes)` (raises `ValidationError`, saves nothing); `ApiKeyName`, `API_KEY_NAMES`, `ApiKeys(store, settings, box=None)` with `set_api_key(name, value, at)`, `clear_api_key(name)`, `api_key_status(name) -> ApiKeyStatus(name, set_in_ui, set_in_env, updated_at, is_set)`, `resolve_api_key(name)` | `deps.py`, `routes_settings.py`, `app.py`, tests |
| `pricing.Pricing.load(path)`, `.models(provider=None) -> list[str]` (sorted ids), `.price(model) -> ModelPrice(provider, input_per_mtok, output_per_mtok)` (1) | `app.py`, `routes_settings.py` |
| `normalise.canonical_url` (2); `research_engine.HttpResearchEngine(base_url, api_key_provider, client=http)` with an async provider (R3); `sources.registry.build_sources(http, research_engine, clock)` (2) | `app.build_context` |
| `budget.Budget(store, pricing, clock, runtime, tz=settings.tz)`; `judges.haiku.HaikuJudge(client, model, pricing)`; `judges.jev.JevJudge(api_key, pricing, model=...)`; `writer.HaikuWriter(client, model, pricing, briefing_model=...)`; `pipeline.PipelineContext(store=, sources=, research_engine=, judge=, writer=, budget=, clock=, settings=)` (3) | `app.build_context`, `tests/web/fakes.py` |
| `scheduler.Scheduler(ctx, *, run_fn=None, briefing_fn=None, nightly_fn=None, tick_seconds=60.0)` with `start()`, `async stop()`, `async run_now(trigger="manual") -> RunReport \| None`, `lock`, `alive`, assignable `ctx` (3, R6) | `app.py`, `routes_health.py`, `routes_settings.py` |
| `feedback.set_feedback(store, clock, story_id, value)`, `feedback.add_blocklist_keyword(store, keyword) -> list[str]` (raises `ValueError` for an empty or over-long keyword); `taste.rank_today(store, stories)`, `taste.nightly` (4, R5) | `routes_today.py`, `app.py` |
| `tests/__init__.py` exists and `pythonpath = ["."]`; `asyncio_mode = "auto"`; `tests/conftest.py` gives the `store` fixture (0, 1) | tests |

## Design direction: "First light"

Chosen with the frontend-design guidance, then checked against its list of generated-looking defaults.

- **Subject:** a private morning paper about AI, read once or twice a day, often on a phone before the house is up. The job: show what matters, with proof (sources) and a sense of how strong each story is.
- **Palette (light / dark):** paper `#EDF1F4` / `#121A25` (cool pre-dawn mist, deliberately *not* warm cream), surface `#F7F9FB` / `#18222F`, ink `#172230` / `#E4E9EF` (blue-black, not tinted near-black), secondary ink `#46546A` / `#B3BECB`, muted `#5E6B7D` / `#8D9AAA` (both ≥ 4.5:1 on paper), rules `#CFD8E0` / `#2A3646`. **One accent: first-light amber** `#A65F00` / `#E2A63C`, used only for signal (the gauge, the badge, the active nav underline, focus rings, the spend bars). Validated with the dataviz palette script: light `#A65F00` passes every check on `#F7F9FB`; dark passes contrast (≥ 3:1) on `#18222F`.
- **Type:** headlines in the system's best book serif (`"Iowan Old Style", "Charter", "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif`), body and UI in `system-ui`. No web fonts, no CDN, no layout shift from font swaps. Scale ratio about 1.2 from a 16 px base (17 px from 768 px), headlines balanced with `text-wrap: balance`, section names in serif italic in sentence case. No all-caps labels, no monospace, no middle-dot meta strings, no arrows on links.
- **Layout:** one reading column (`40rem`, about 65 characters), left-aligned under the masthead; on wide screens the "By section" view gains a sticky section index on the left. Stories are not boxed cards: they sit on the paper, separated by space and a hairline, so the page reads like a column of print rather than a SaaS card kit.
- **The one bold thing — the signal gauge:** every story carries a 4 px vertical gauge down its left edge, filled from the bottom in amber to its signal score (0–10), like a tide gauge at dawn. The wordmark repeats it in miniature. Everything else stays quiet: ink buttons, hairlines, no shadows, no entrance animations; motion only answers the owner's own clicks and is removed under `prefers-reduced-motion`.

```
 390 px                              1280 px (By section)
 ┌───────────────────────────┐       ┌───────────────────────────────────────────────────────┐
 │ ▮Newsdesk        Log out  │       │ ▮Newsdesk          Today  Briefing  Sources  Settings │
 │ Today Briefing Sources …  │       ├───────────────────────────────────────────────────────┤
 ├───────────────────────────┤       │ Wednesday 14 October                                  │
 │ Wednesday 14 October      │       │ 14 stories worth your time …   (Top | By section)     │
 │ 14 stories worth your …   │       │                                                       │
 │ (Top | By section)        │       │ Model releases 3  │ Model releases               3    │
 │                           │       │ Research       5  │ ┃ Signal 9  Research  2 h ago     │
 │ ┃ Signal 9  2 h ago       │       │ Products       2  │ ┃ Headline in book serif         │
 │ ┃ Headline set large in   │       │ (sticky index)    │ ┃ Summary, why it matters …      │
 │ ┃ the book serif          │       │                   │ ┃ (chip)(chip)(chip)      👍 👎   │
 │ ┃ Summary …               │       │                   │ ─────────────────────────────     │
 │ ┃ (chip)(chip)     👍 👎  │       │                   │ ┃ …                              │
 └───────────────────────────┘       └───────────────────────────────────────────────────────┘
```

---
### Task 25: Auth, CSRF, login page and `hash-password`

**Goal:** Only the owner gets in. Login checks an argon2 hash, issues a signed session cookie, rate-limits guessing, and every state-changing request carries a CSRF token. With no password hash configured, login is impossible and the page says how to fix it.

**Files:**
- Create: `src/ai_newsdesk/web/__init__.py`
- Create: `src/ai_newsdesk/web/auth.py`
- Create: `src/ai_newsdesk/web/csrf.py`
- Create: `src/ai_newsdesk/web/security.py`
- Create: `src/ai_newsdesk/web/routes_auth.py`
- Create: `src/ai_newsdesk/web/deps.py` (minimal; replaced in Task 27)
- Create: `src/ai_newsdesk/web/templates/base.html` (minimal; replaced in Task 27)
- Create: `src/ai_newsdesk/web/templates/login.html`
- Create: `src/ai_newsdesk/web/static/htmx.min.js` (vendored, byte-exact)
- Create or modify: `src/ai_newsdesk/cli.py`
- Create: `tests/web/__init__.py`, `tests/web/support.py`, `tests/web/test_auth.py`, `tests/test_cli.py`

**Acceptance Criteria:**
- [ ] Any path other than `/login`, `/health` and `/static/*` without a valid session: `GET`/`HEAD` → 303 to `/login?next=<path>`; htmx request → 401 with `HX-Redirect`; other methods → 401.
- [ ] Right password + CSRF token → 303 to a same-site `next` (open redirects fall back to `/`) and a session cookie with `HttpOnly`, `Secure`, `SameSite=Lax`, `Max-Age=1209600`.
- [ ] Wrong password → 401 "Wrong password. Try again."; the 6th attempt after 5 failures within 15 minutes from one IP → 429 with `Retry-After`, even with the right password.
- [ ] `POST` without a matching `csrf_token` field or `X-CSRF-Token` header → 403 (login and logout included).
- [ ] Empty `OWNER_PASSWORD_HASH` → login page shows "run make set-password", has no password field, and `POST /login` → 403.
- [ ] A tampered cookie, or one issued before the password hash changed, is refused.
- [ ] Every response carries the CSP (`script-src 'self'`, `style-src 'self'`), `X-Frame-Options: DENY`, `nosniff` and `Referrer-Policy`.
- [ ] `ai-newsdesk hash-password` on a TTY reads the password twice via `getpass`; with stdin not a TTY it reads one line from stdin. Either way it prints one verifiable `$argon2id$` hash line; mismatch or fewer than 12 characters → exit 2, nothing on stdout. `cli.py` has the argparse shape step 7 tests (`main(argv=None) -> int`, subparsers with `set_defaults(func=...)`) and `pyproject.toml` has `[project.scripts] ai-newsdesk = "ai_newsdesk.cli:run"`.
- [ ] `htmx.min.js` is htmx 2.0.11, sha256 `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`.

**Verify:** `uv run pytest tests/web/test_auth.py tests/test_cli.py -q` → all pass (24 tests).

**Steps:**

- [ ] **Step 1: Vendor htmx 2.0.11 (pinned, verified)**

```bash
mkdir -p src/ai_newsdesk/web/static
curl -sSfL -o src/ai_newsdesk/web/static/htmx.min.js \
  https://cdn.jsdelivr.net/npm/htmx.org@2.0.11/dist/htmx.min.js
echo "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717  src/ai_newsdesk/web/static/htmx.min.js" | sha256sum -c -
```

Expected: `src/ai_newsdesk/web/static/htmx.min.js: OK` (52,182 bytes). If the check fails, delete the file and stop: do not commit an unverified script. The same bytes are served at `https://unpkg.com/htmx.org@2.0.11/dist/htmx.min.js`. Make sure the pre-commit whitespace hooks skip it: add `exclude: ^src/ai_newsdesk/web/static/htmx\.min\.js$` to the `end-of-file-fixer` and `trailing-whitespace` hooks in `.pre-commit-config.yaml` if they would touch it (run `uv run pre-commit run --files src/ai_newsdesk/web/static/htmx.min.js` and re-run the sha256 check afterwards: it must still say `OK`).

- [ ] **Step 2: Write the shared test helpers**

`tests/web/__init__.py`:

```python
```

(empty file)

`tests/web/support.py`:

```python
"""Shared helpers for the web tests: settings, login, CSRF and running coroutines on the app loop."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from ai_newsdesk.config import Settings
from ai_newsdesk.web.csrf import CSRF_COOKIE

T = TypeVar("T")

NOW = datetime(2026, 10, 14, 8, 30, tzinfo=UTC)
TEST_PASSWORD = "correct horse battery staple"
# Cheap parameters keep the tests fast; verify() reads the parameters from the hash itself.
TEST_HASH = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(TEST_PASSWORD)
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
BASE_URL = "https://testserver"  # https so the Secure cookies are sent back
HX = {"HX-Request": "true"}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "site_host": "ai-newsdesk.localhost",
        "secrets_key": "s" * 44,
        "session_secret": "k" * 44,
        "owner_password_hash": TEST_HASH,
        "anthropic_api_key": "",
        "research_engine_api_key": "",
        "typesafe_api_key": "",
        "research_engine_url": "http://research-engine.invalid:8000",
        "db_path": str(tmp_path / "web.sqlite"),
        "tz": "Europe/London",
        "log_level": "INFO",
        "git_sha": "abc1234",
        "scheduler_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def csrf_token(client: TestClient) -> str:
    """The CSRF cookie value (fetching /login first if the client has none yet)."""
    if client.cookies.get(CSRF_COOKIE) is None:
        client.get("/login")
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return token


def login(client: TestClient, password: str = TEST_PASSWORD) -> None:
    response = client.post(
        "/login",
        data={"password": password, "next": "/", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def api_routes(app: FastAPI) -> list[APIRoute]:
    """Every APIRoute, including those of included routers.

    FastAPI >= 0.142 keeps an included router as one lazy entry in `app.routes` (its routes are
    reached through `original_router`), so walk the tree instead of reading `app.routes` flat.
    """
    found: list[APIRoute] = []

    def walk(routes: list[Any]) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif (inner := getattr(route, "original_router", None)) is not None:
                walk(list(inner.routes))

    walk(list(app.routes))
    return found


def run(client: TestClient, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """Run an async callable on the app's event loop (the Store's aiosqlite connection lives there)."""
    portal = client.portal
    assert portal is not None, "use the TestClient inside its `with` block"
    return portal.call(functools.partial(fn, *args, **kwargs))
```

- [ ] **Step 3: Write the failing auth tests**

`tests/web/test_auth.py`:

```python
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.config import Settings
from ai_newsdesk.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    LoginRateLimiter,
    SessionCodec,
    is_open_path,
    safe_next,
    verify_password_sync,
)
from ai_newsdesk.web.csrf import CSRF_COOKIE, csrf_protect
from ai_newsdesk.web.security import install_security
from tests.web.support import BASE_URL, TEST_HASH, TEST_PASSWORD, csrf_token, login, make_settings


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    install_security(app, settings)

    @app.get("/private")
    async def private() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/private", dependencies=[Depends(csrf_protect)])
    async def private_post() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(_app(make_settings(tmp_path)), base_url=BASE_URL) as c:
        yield c


def test_private_page_redirects_to_login(client: TestClient) -> None:
    r = client.get("/private?x=1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fprivate%3Fx%3D1"


def test_htmx_request_without_session_gets_hx_redirect(client: TestClient) -> None:
    r = client.get("/private", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["HX-Redirect"].startswith("/login?next=")


def test_post_without_session_is_401(client: TestClient) -> None:
    assert client.post("/private", follow_redirects=False).status_code == 401


def test_login_page_shows_form_and_sets_csrf_cookie(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert client.cookies.get(CSRF_COOKIE)
    cookie = next(v for v in r.headers.get_list("set-cookie") if v.startswith(f"{CSRF_COOKIE}="))
    for part in ("httponly", "secure", "samesite=strict"):
        assert part in cookie.lower()


def test_wrong_password_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"password": "nope", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Wrong password. Try again." in r.text
    assert client.get("/private", follow_redirects=False).status_code == 303


def test_login_sets_hardened_session_cookie(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"password": TEST_PASSWORD, "next": "/private", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/private"
    cookie = next(v for v in r.headers.get_list("set-cookie") if v.startswith(f"{SESSION_COOKIE}="))
    lowered = cookie.lower()
    for part in ("httponly", "secure", "samesite=lax", f"max-age={SESSION_MAX_AGE_S}"):
        assert part in lowered
    assert client.get("/private").json() == {"ok": True}


def test_login_needs_csrf_token(client: TestClient) -> None:
    client.get("/login")
    r = client.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert r.status_code == 403


def test_open_redirect_falls_back_to_root(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"password": TEST_PASSWORD, "next": "//evil.example/x", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_five_failures_lock_the_client_out(client: TestClient) -> None:
    token = csrf_token(client)
    for _ in range(5):
        r = client.post("/login", data={"password": "nope", "csrf_token": token}, follow_redirects=False)
        assert r.status_code == 401
    r = client.post("/login", data={"password": TEST_PASSWORD, "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert "Too many wrong passwords" in r.text


def test_rate_limiter_forgets_after_the_window() -> None:
    now = [0.0]
    limiter = LoginRateLimiter(clock=lambda: now[0])
    for _ in range(5):
        limiter.fail("192.0.2.1")
    assert limiter.blocked("192.0.2.1")
    assert not limiter.blocked("192.0.2.2")
    assert limiter.retry_after("192.0.2.1") == 900
    now[0] = 15 * 60 + 1
    assert not limiter.blocked("192.0.2.1")
    limiter.fail("192.0.2.3")
    limiter.reset("192.0.2.3")
    assert not limiter.blocked("192.0.2.3")
    assert len(limiter) == 0


def test_tampered_session_cookie_is_refused(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "forged.value.sig", domain="testserver")
    assert client.get("/private", follow_redirects=False).status_code == 303


def test_changing_the_password_hash_ends_sessions() -> None:
    a = SessionCodec("k" * 44, "hash-a")
    b = SessionCodec("k" * 44, "hash-b")
    assert a.valid(a.issue())
    assert not b.valid(a.issue())
    disabled = SessionCodec("k" * 44, "")
    assert not disabled.valid(disabled.issue())


def test_logout_ends_the_session(client: TestClient) -> None:
    login(client)
    r = client.post("/logout", data={"csrf_token": csrf_token(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert client.get("/private", follow_redirects=False).status_code == 303


def test_csrf_header_is_accepted_and_wrong_tokens_refused(client: TestClient) -> None:
    login(client)
    assert client.post("/private", headers={"X-CSRF-Token": csrf_token(client)}).status_code == 200
    assert client.post("/private", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/private").status_code == 403


def test_empty_password_hash_disables_login(tmp_path: Path) -> None:
    with TestClient(_app(make_settings(tmp_path, owner_password_hash="")), base_url=BASE_URL) as c:
        page = c.get("/login")
        assert "make set-password" in page.text
        assert 'name="password"' not in page.text
        r = c.post("/login", data={"password": "anything", "csrf_token": csrf_token(c)}, follow_redirects=False)
        assert r.status_code == 403


def test_open_paths() -> None:
    for path in ("/login", "/health", "/static/app.css"):
        assert is_open_path(path)
    for path in ("/", "/loginx", "/settings", "/healthz", "/static"):
        assert not is_open_path(path)


def test_safe_next() -> None:
    assert safe_next("/settings?x=1") == "/settings?x=1"
    for bad in ("//evil.example", "https://evil.example/", "/\\evil", "", None, "/%2F%2Fevil", "settings"):
        assert safe_next(bad) == "/"


def test_verify_password() -> None:
    assert verify_password_sync(TEST_HASH, TEST_PASSWORD)
    assert not verify_password_sync(TEST_HASH, "nope")
    assert not verify_password_sync("not-a-hash", TEST_PASSWORD)
    assert not verify_password_sync("", TEST_PASSWORD)


def test_security_headers(client: TestClient) -> None:
    r = client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
```

- [ ] **Step 4: Write the failing CLI tests**

`tests/test_cli.py`:

```python
from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from argon2 import PasswordHasher

from ai_newsdesk import cli
from ai_newsdesk.cli import main


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _answers(monkeypatch: pytest.MonkeyPatch, *values: str) -> None:
    """Pretend to be a terminal, so hash-password prompts twice with getpass."""
    it: Iterator[str] = iter(values)
    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(it))


def test_hash_password_reads_one_line_when_stdin_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough password\n"))
    assert main(["hash-password"]) == 0
    captured = capsys.readouterr()
    [line] = captured.out.strip().splitlines()
    assert PasswordHasher().verify(line, "a long enough password")
    assert "a long enough password" not in captured.out + captured.err


def test_run_exits_with_mains_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["ai-newsdesk", "hash-password"])
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit) as exit_info:
        cli.run()
    assert exit_info.value.code == 2


def test_hash_password_prints_a_verifiable_hash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, "a long enough password", "a long enough password")
    assert main(["hash-password"]) == 0
    captured = capsys.readouterr()
    out = captured.out.strip()
    assert out.startswith("$argon2id$")
    assert PasswordHasher().verify(out, "a long enough password")
    assert "a long enough password" not in captured.out + captured.err


def test_hash_password_refuses_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, "a long enough password", "a different password")
    assert main(["hash-password"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "don't match" in captured.err


def test_hash_password_refuses_a_short_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, "short", "short")
    assert main(["hash-password"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "at least 12" in captured.err
```

- [ ] **Step 5: Run the tests to see them fail**

Run: `uv run pytest tests/web/test_auth.py tests/test_cli.py -q`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ai_newsdesk.web'` (and `ai_newsdesk.cli` has no `main`/`hash-password` yet).

- [ ] **Step 6: Implement auth**

`src/ai_newsdesk/web/__init__.py`:

```python
"""Web layer: routes, templates, static assets, auth and CSRF."""
```

`src/ai_newsdesk/web/auth.py`:

```python
"""Owner login: argon2 password check, signed session cookie, login rate limit and the auth gate."""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from urllib.parse import quote, unquote, urlsplit

import anyio.to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, TimestampSigner
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

SESSION_COOKIE = "nd_session"
SESSION_MAX_AGE_S = 14 * 24 * 3600
_SALT = "ai-newsdesk-session"
OPEN_PATHS = frozenset({"/login", "/health"})
OPEN_PREFIXES = ("/static/",)
MAX_PASSWORD_CHARS = 1024  # argon2 cost is per byte of input; refuse absurd inputs outright

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password_sync(stored_hash: str, password: str) -> bool:
    if not stored_hash or not password or len(password) > MAX_PASSWORD_CHARS:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def verify_password(stored_hash: str, password: str) -> bool:
    """argon2 is deliberately slow, so it runs off the event loop."""
    return await anyio.to_thread.run_sync(verify_password_sync, stored_hash, password)


class SessionCodec:
    """The cookie value is ``TimestampSigner(secret).sign(fingerprint of OWNER_PASSWORD_HASH)``.

    Changing the password hash changes the fingerprint, so every older session stops working.
    With no hash configured, nothing is ever valid.
    """

    def __init__(self, secret: str, password_hash: str) -> None:
        self._signer = TimestampSigner(secret, salt=_SALT)
        self._enabled = bool(password_hash)
        self._payload = hashlib.sha256(password_hash.encode()).hexdigest()[:32].encode()

    def issue(self) -> str:
        return self._signer.sign(self._payload).decode()

    def valid(self, value: str | None) -> bool:
        if not self._enabled or not value:
            return False
        try:
            return self._signer.unsign(value, max_age=SESSION_MAX_AGE_S) == self._payload
        except (BadSignature, UnicodeError):  # SignatureExpired is a BadSignature
            return False


class LoginRateLimiter:
    """At most ``max_failures`` failed logins per client in a sliding ``window_s``.

    Memory is bounded by ``max_clients`` (the least recently failing client is dropped first).
    """

    def __init__(
        self,
        max_failures: int = 5,
        window_s: float = 15 * 60,
        *,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_failures
        self._window = window_s
        self._cap = max_clients
        self._clock = clock
        self._fails: OrderedDict[str, deque[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._fails)

    def _recent(self, client: str) -> deque[float] | None:
        q = self._fails.get(client)
        if q is None:
            return None
        cutoff = self._clock() - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            del self._fails[client]
            return None
        return q

    def blocked(self, client: str) -> bool:
        q = self._recent(client)
        return q is not None and len(q) >= self._max

    def retry_after(self, client: str) -> int:
        """Whole seconds until ``client`` may try again (0 when not blocked)."""
        q = self._recent(client)
        if q is None or len(q) < self._max:
            return 0
        return max(1, math.ceil(q[0] + self._window - self._clock()))

    def fail(self, client: str) -> None:
        if self.blocked(client):
            return  # attempts during a lockout neither count nor extend it
        q = self._fails.get(client)
        if q is None:
            q = self._fails[client] = deque(maxlen=self._max)
        else:
            self._fails.move_to_end(client)
        q.append(self._clock())
        while len(self._fails) > self._cap:
            self._fails.popitem(last=False)

    def reset(self, client: str) -> None:
        self._fails.pop(client, None)


def _bad_path(value: str) -> bool:
    return (
        not value.startswith("/")
        or value.startswith(("//", "/\\"))
        or "\\" in value
        or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value)
    )


def safe_next(value: str | None) -> str:
    """``value`` if it is a same-site path (also once percent-decoded), otherwise ``/``."""
    if not value or len(value) > 2048 or _bad_path(value) or _bad_path(unquote(value)):
        return "/"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    return value


def is_open_path(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def login_required_response(conn: HTTPConnection, method: str) -> Response:
    target = conn.url.path + (f"?{conn.url.query}" if conn.url.query else "")
    login_url = "/login?next=" + quote(safe_next(target), safe="")
    if conn.headers.get("hx-request") == "true":
        # htmx follows HX-Redirect before it looks at the status code.
        return Response(status_code=401, headers={"HX-Redirect": login_url})
    if method in ("GET", "HEAD"):
        return RedirectResponse(login_url, status_code=303)
    return PlainTextResponse("Log in first.", status_code=401)


class AuthMiddleware:
    """Pure ASGI gate: every HTTP path except the open ones needs a valid session cookie.

    Fail-closed: a new route is protected without anyone remembering to protect it.
    """

    def __init__(self, app: ASGIApp, codec: SessionCodec) -> None:
        self.app = app
        self.codec = codec

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_open_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        if self.codec.valid(conn.cookies.get(SESSION_COOKIE)):
            await self.app(scope, receive, send)
            return
        response = login_required_response(conn, scope["method"])
        await response(scope, receive, send)
```

`src/ai_newsdesk/web/csrf.py`:

```python
"""Double-submit CSRF protection.

A random token lives in the ``nd_csrf`` cookie (set on the first response to any browser). Every
unsafe request must echo it in the ``csrf_token`` form field or the ``X-CSRF-Token`` header.
"""

from __future__ import annotations

import hmac
import re
import secrets

from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_COOKIE = "nd_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
CSRF_MAX_AGE_S = 14 * 24 * 3600
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def new_token() -> str:
    return secrets.token_urlsafe(32)  # 43 characters


class CSRFCookieMiddleware:
    """Makes sure every browser holds a CSRF token and exposes it as ``request.state.csrf_token``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = HTTPConnection(scope).cookies.get(CSRF_COOKIE, "")
        fresh = not _TOKEN_RE.match(token)
        if fresh:
            token = new_token()
        scope.setdefault("state", {})["csrf_token"] = token
        if not fresh:
            await self.app(scope, receive, send)
            return
        cookie = (
            f"{CSRF_COOKIE}={token}; Path=/; Max-Age={CSRF_MAX_AGE_S}; HttpOnly; Secure; SameSite=Strict"
        ).encode()

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"set-cookie", cookie))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_cookie)


async def csrf_protect(request: Request) -> None:
    """FastAPI dependency: refuse an unsafe request whose token does not match the cookie."""
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE, "")
    supplied = request.headers.get(CSRF_HEADER, "")
    if not supplied and request.headers.get("content-type", "").startswith(_FORM_TYPES):
        value = (await request.form()).get(CSRF_FIELD)
        supplied = value if isinstance(value, str) else ""
    if not cookie or not supplied or not hmac.compare_digest(cookie.encode(), supplied.encode()):
        raise HTTPException(
            status_code=403,
            detail="The form expired or came from somewhere else. Reload the page and try again.",
        )
```

`src/ai_newsdesk/web/deps.py` (minimal for now; Task 27 replaces it):

```python
"""Template environment shared by every route module."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
NO_STORE = {"Cache-Control": "no-store"}

templates = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(WEB_DIR / "templates"), autoescape=True)
)
```

`src/ai_newsdesk/web/routes_auth.py`:

```python
"""Login and logout."""

from __future__ import annotations

import math
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_newsdesk.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    LoginRateLimiter,
    SessionCodec,
    safe_next,
    verify_password,
)
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import NO_STORE, templates

router = APIRouter(include_in_schema=False)


def _client_ip(request: Request) -> str:
    # Behind Caddy, uvicorn --proxy-headers has already put the forwarded client address here.
    return request.client.host if request.client else "unknown"


def _login_page(request: Request, next_path: str, error: str | None, status_code: int) -> HTMLResponse:
    enabled = bool(request.app.state.settings.owner_password_hash)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": next_path,
            "error": error,
            "login_enabled": enabled,
            "csrf_token": request.state.csrf_token,
        },
        status_code=status_code,
        headers=NO_STORE,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> Response:
    target = safe_next(next)
    codec: SessionCodec = request.app.state.session_codec
    if codec.valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(target, status_code=303)
    return _login_page(request, target, None, 200)


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login(
    request: Request,
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    target = safe_next(next)
    stored_hash: str = request.app.state.settings.owner_password_hash
    if not stored_hash:
        return _login_page(request, target, None, 403)
    limiter: LoginRateLimiter = request.app.state.login_limiter
    client = _client_ip(request)
    if limiter.blocked(client):
        wait_s = limiter.retry_after(client)
        minutes = max(1, math.ceil(wait_s / 60))
        response = _login_page(
            request, target, f"Too many wrong passwords. Try again in {minutes} min.", 429
        )
        response.headers["Retry-After"] = str(wait_s)
        return response
    if not await verify_password(stored_hash, password):
        limiter.fail(client)
        return _login_page(request, target, "Wrong password. Try again.", 401)
    limiter.reset(client)
    codec: SessionCodec = request.app.state.session_codec
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        codec.issue(),
        max_age=SESSION_MAX_AGE_S,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.post("/logout", dependencies=[Depends(csrf_protect)])
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="lax")
    return response
```

`src/ai_newsdesk/web/security.py`:

```python
"""Installs the security middlewares, auth state and login routes on an app."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_newsdesk.config import Settings
from ai_newsdesk.web import routes_auth
from ai_newsdesk.web.auth import AuthMiddleware, LoginRateLimiter, SessionCodec
from ai_newsdesk.web.csrf import CSRFCookieMiddleware

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_SECURITY_HEADERS = (
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
)


class SecurityHeadersMiddleware:
    """Adds the CSP and hardening headers to every HTTP response (headers only, body untouched)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def install_security(app: FastAPI, settings: Settings) -> None:
    """Session codec, login limiter, middlewares and the /login + /logout routes.

    Middleware order (the last added is outermost): security headers wrap everything, so
    redirects and 401s carry the CSP; the CSRF cookie is issued even on the login redirect; the
    auth gate is innermost.
    """
    app.state.session_codec = SessionCodec(settings.session_secret, settings.owner_password_hash)
    app.state.login_limiter = LoginRateLimiter()
    app.add_middleware(AuthMiddleware, codec=app.state.session_codec)
    app.add_middleware(CSRFCookieMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(routes_auth.router)
```

`src/ai_newsdesk/web/templates/base.html` (minimal; Task 27 replaces it):

```html
<!doctype html>
<html lang="en-GB">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Newsdesk{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
  {% block masthead %}{% endblock %}
  <main id="main" class="page">{% block content %}{% endblock %}</main>
</body>
</html>
```

`src/ai_newsdesk/web/templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}Log in to Newsdesk{% endblock %}
{% block masthead %}{% endblock %}
{% block content %}
<section class="login" aria-labelledby="login-title">
  <div class="login__card">
    <p class="wordmark wordmark--large">Newsdesk</p>
    <h1 id="login-title" class="login__title">Log in</h1>
    {% if not login_enabled %}
    <div class="notice notice--error" role="alert">
      <p>Login is switched off because no owner password is set. On the server, run <code>make set-password</code>, then restart the app.</p>
    </div>
    {% else %}
    <form class="form" method="post" action="/login">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="hidden" name="next" value="{{ next }}">
      <div class="field">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required autofocus{% if error %} aria-invalid="true" aria-describedby="login-error"{% endif %}>
        {% if error %}<p class="field__error" id="login-error" role="alert">{{ error }}</p>{% endif %}
      </div>
      <div class="form__actions">
        <button class="button" type="submit">Log in</button>
      </div>
    </form>
    {% endif %}
  </div>
</section>
{% endblock %}
```

Login pages need no-store too; the template response already sends `Cache-Control: no-store` (the test checks it).

`src/ai_newsdesk/cli.py` (master plan R7: this is the shape step 7 tests and extends with `cli_db.register(sub)`; create the file exactly as below):

```python
"""The `ai-newsdesk` command line."""

from __future__ import annotations

import argparse
import getpass
import sys

from ai_newsdesk.web.auth import hash_password

MIN_PASSWORD_LENGTH = 12


def _too_short(password: str) -> bool:
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Use at least {MIN_PASSWORD_LENGTH} characters. Nothing was changed.", file=sys.stderr)
        return True
    return False


def cmd_hash_password(args: argparse.Namespace) -> int:
    """Print the argon2id hash for OWNER_PASSWORD_HASH.

    On a terminal: prompt twice without echo. Otherwise (ops/set_password.sh pipes it in, never
    via argv): read exactly one line from stdin. Only the hash ever reaches stdout.
    """
    if not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\r\n")
        if _too_short(password):
            return 2
    else:
        password = getpass.getpass("New owner password: ")
        if _too_short(password):
            return 2
        if getpass.getpass("Type it again: ") != password:
            print("The two passwords don't match. Nothing was changed.", file=sys.stderr)
            return 2
    print(hash_password(password))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-newsdesk")
    sub = parser.add_subparsers(dest="command", required=True)
    hash_cmd = sub.add_parser(
        "hash-password",
        help="Read the owner password (twice on a terminal, one stdin line otherwise) and print its argon2 hash.",
    )
    hash_cmd.set_defaults(func=cmd_hash_password)
    args = parser.parse_args(argv)
    return int(args.func(args))


def run() -> None:
    """Console-script entry point (`ai-newsdesk`)."""
    sys.exit(main())


if __name__ == "__main__":
    run()
```

Add the console script to `pyproject.toml` (step 0 does not define one):

```toml
[project.scripts]
ai-newsdesk = "ai_newsdesk.cli:run"
```

Then run `uv sync` so `uv run ai-newsdesk hash-password` resolves.

- [ ] **Step 7: Run the tests to see them pass**

Run: `uv run pytest tests/web/test_auth.py tests/test_cli.py -q`
Expected: `24 passed`.
Run: `uv run ruff check && uv run ruff format --check && uv run pyright`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add src/ai_newsdesk/web src/ai_newsdesk/cli.py tests/web tests/test_cli.py .pre-commit-config.yaml pyproject.toml
git commit -m "feat(web): owner login, session cookie, CSRF and hash-password (V1-S5)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 26: `create_app`, lifespan, key testers and `/health`

**Goal:** One factory builds the whole app: it opens the store, seeds sources, loads prices, wires the pipeline context (fakes in tests), starts and stops the scheduler, and serves `/health`. A route-policy test makes sure every page added later needs a session and every POST needs CSRF.

**Files:**
- Create: `src/ai_newsdesk/app.py`
- Create: `src/ai_newsdesk/web/app_routes.py`
- Create: `src/ai_newsdesk/web/routes_health.py`
- Create: `src/ai_newsdesk/web/runs.py`
- Create: `src/ai_newsdesk/web/keys.py` (provider key testers only; key storage is step 1's `ApiKeys`)
- Create: `tests/web/fakes.py`, `tests/web/conftest.py`
- Create: `tests/web/test_app.py`, `tests/web/test_route_policy.py`, `tests/web/test_key_testers.py`

**Acceptance Criteria:**
- [ ] `GET /health` without login → 200 `{"status":"up","db":true,"scheduler":<bool>,"last_run":<object|null>,"git_sha":"<sha>"}`; `last_run` has `trigger`, `started_at`, `finished_at`, `cost_usd`, `error`.
- [ ] If the database query fails → 503 with `"status":"down","db":false`.
- [ ] Start-up seeds sources from `config/sources.yaml` into an empty DB; `ctx_factory` replaces the real wiring; the Store gets the app clock and `normalise.canonical_url`; the scheduler is always built as `Scheduler(ctx, nightly_fn=taste.nightly)`; `SCHEDULER_ENABLED=true` → `start()` at start-up, `await stop()` at shutdown; `false` → never started (`/health` says `"scheduler": false`); `app.state.run_lock` is the scheduler's lock either way.
- [ ] `/health` reports `"scheduler": scheduler.alive`.
- [ ] `/docs`, `/redoc` and `/openapi.json` don't exist.
- [ ] Route policy: every `GET` route except `/login` and `/health` → 303 to login without a session; every `POST` except `/login` → 401 without a session; every `POST` → 403 without a CSRF token.
- [ ] Key testers: Anthropic `count_tokens` 401 → "Anthropic rejected the key (401)."; research-engine `GET /v1/engines` with `X-API-Key` 401 → "research-engine rejected the key (401)."; unreachable → "Could not reach …"; TypeSafe SDK missing → "isn't installed".

**Verify:** `uv run pytest tests/web -q` → all pass.

**Steps:**

- [ ] **Step 1: Confirm the names this step uses from steps 0–4**

```bash
grep -n "owner_password_hash\|session_secret\|secrets_key\|scheduler_enabled\|git_sha\|db_path" src/ai_newsdesk/config.py
grep -n "class FixedClock\|class SystemClock" src/ai_newsdesk/clock.py
grep -n "def __init__\|canonicalise\|async def init\|async def close\|def _all\|def _one\|^def _ts\|^def _dt\|^def _req_dt" src/ai_newsdesk/store/store.py
grep -nE "async def (upsert_source|get_source|list_sources|delete_source|source_stats|get_setting|set_setting|month_spend|spend_by_day|save_briefing|get_briefing|latest_briefing|briefing_dates|last_runs|record_source_result)\(" src/ai_newsdesk/store/store.py
grep -n "class RuntimeSettings\|RUNS_PER_DAY_CHOICES\|def load_runtime_settings\|def save_runtime_settings\|def update_runtime_settings\|class ApiKeys\|class ApiKeyStatus\|API_KEY_NAMES\|def set_api_key\|def clear_api_key\|def api_key_status\|def resolve_api_key" src/ai_newsdesk/runtime_settings.py
grep -n "class ModelPrice\|def load\|def models\|def price" src/ai_newsdesk/pricing.py
grep -n "def canonical_url" src/ai_newsdesk/normalise.py
grep -n "class HttpResearchEngine" -A8 src/ai_newsdesk/research_engine.py
grep -n "def build_sources" src/ai_newsdesk/sources/registry.py
grep -n "class Budget" -A8 src/ai_newsdesk/budget.py
grep -n "class HaikuJudge\|class JevJudge\|def __init__" -A6 src/ai_newsdesk/judges/haiku.py src/ai_newsdesk/judges/jev.py
grep -n "class HaikuWriter\|def __init__" -A6 src/ai_newsdesk/writer.py
grep -n "class PipelineContext" -A12 src/ai_newsdesk/pipeline.py
grep -n "class Scheduler\|def __init__\|def start\|async def stop\|def alive\|async def run_now\|self\.lock\|self\.ctx" src/ai_newsdesk/scheduler.py
grep -n "async def set_feedback\|async def add_blocklist_keyword" src/ai_newsdesk/feedback.py
grep -n "async def rank_today\|async def nightly" src/ai_newsdesk/taste.py
grep -n "asyncio_mode\|pythonpath" pyproject.toml; ls tests/__init__.py
```

Expected: every name in the "Names used from steps 0–4" table at the top of this file is found with the shape described. In particular `HttpResearchEngine` must **await** its `api_key_provider` (master plan R3); if step 2 still calls it synchronously, stop and raise it with the plan owner rather than passing a coroutine into a header. Where any other name differs, adapt **only** the call site that table names (for example the constructor calls in `build_context`), and list each difference in the PR description. Do not change earlier steps' code from this branch.

- [ ] **Step 2: Write the test fakes and fixtures**

`tests/web/fakes.py`:

```python
"""Minimal protocol fakes so the web tests never touch the network or spend money."""

from __future__ import annotations

from ai_newsdesk.app import AppDeps
from ai_newsdesk.budget import Budget
from ai_newsdesk.models import (
    BriefingStory,
    JudgeResult,
    ReviewInput,
    SearchHit,
    StoryText,
    Usage,
    WriteInput,
)
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.runtime_settings import load_runtime_settings


class FakeResearchEngine:
    async def search(
        self, query: str, *, max_results: int = 10, time_range: str | None = "day"
    ) -> list[SearchHit]:
        return []

    async def fetch_markdown(self, url: str) -> str | None:
        return None

    async def health(self) -> bool:
        return True


class FakeJudge:
    model = "claude-haiku-5-5"

    def worst_case_cost(self, inputs: list[ReviewInput]) -> float:
        return 0.0

    async def review(self, inputs: list[ReviewInput]) -> JudgeResult:
        return JudgeResult(failed_item_ids=[i.item_id for i in inputs])


class FakeWriter:
    model = "claude-haiku-5-5"

    def worst_case_cost_stories(self, inputs: list[WriteInput]) -> float:
        return 0.0

    def worst_case_cost_briefing(self, stories: list[BriefingStory]) -> float:
        return 0.0

    async def write_stories(self, inputs: list[WriteInput]) -> tuple[list[StoryText], list[Usage]]:
        return [], []

    async def write_briefing(
        self, stories: list[BriefingStory], date_label: str
    ) -> tuple[str | None, list[Usage]]:
        return None, []


async def fake_ctx_factory(deps: AppDeps) -> PipelineContext:
    """Same shape as `app.build_context`, with fakes for everything that would leave the process."""
    runtime = await load_runtime_settings(deps.store)
    return PipelineContext(
        store=deps.store,
        sources={},
        research_engine=FakeResearchEngine(),
        judge=FakeJudge(),
        writer=FakeWriter(),
        budget=Budget(deps.store, deps.pricing, deps.clock, runtime, tz=deps.settings.tz),
        clock=deps.clock,
        settings=deps.settings,
    )
```

(The clock comes from `create_app(clock=FixedClock(NOW))` in `tests/web/conftest.py`, so the Store, the pipeline context and the pages all agree on "now".)

`tests/web/conftest.py`:

```python
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.app import create_app
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.config import Settings
from ai_newsdesk.runtime_settings import API_KEY_NAMES, RuntimeSettings
from ai_newsdesk.web.keys import KeyTester
from tests.web.fakes import fake_ctx_factory
from tests.web.support import BASE_URL, CONFIG_DIR, NOW, login, make_settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def key_results() -> dict[str, str | None]:
    """What each fake key tester answers: None = the key works, a string = the provider's error."""
    results: dict[str, str | None] = {name: None for name in API_KEY_NAMES}
    return results


def _tester(results: dict[str, str | None], name: str) -> KeyTester:
    async def check(key: str, runtime: RuntimeSettings) -> str | None:
        return results[name]

    return check


@pytest.fixture
def app(settings: Settings, key_results: dict[str, str | None]) -> FastAPI:
    return create_app(
        settings,
        ctx_factory=fake_ctx_factory,
        key_testers={name: _tester(key_results, name) for name in API_KEY_NAMES},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
    )


@pytest.fixture
def anon(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def client(anon: TestClient) -> TestClient:
    login(anon)
    return anon
```

- [ ] **Step 3: Write the failing app, policy and key-tester tests**

`tests/web/test_app.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk import taste
from ai_newsdesk.app import create_app
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.normalise import canonical_url
from tests.web.fakes import fake_ctx_factory
from tests.web.support import BASE_URL, CONFIG_DIR, NOW, make_settings, run


class FakeScheduler:
    """Stands in for step 3's Scheduler: same constructor, start(), async stop(), lock, alive, ctx."""

    events: list[str] = []

    def __init__(self, ctx: object, **kwargs: Any) -> None:
        self.ctx = ctx
        self.kwargs = kwargs
        self.lock = asyncio.Lock()
        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive

    def start(self) -> None:
        self._alive = True
        FakeScheduler.events.append("start")

    async def stop(self) -> None:
        self._alive = False
        FakeScheduler.events.append("stop")


def test_health_is_open_and_reports_state(anon: TestClient) -> None:
    r = anon.get("/health")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "status": "up",
        "db": True,
        "scheduler": False,
        "last_run": None,
        "git_sha": "abc1234",
    }


def test_health_reports_the_last_run(app: FastAPI, anon: TestClient) -> None:
    store = app.state.store
    run_id = run(anon, store.start_run, "manual", NOW)
    run(anon, store.finish_run, run_id, NOW, {"collected": 3}, 0.01, None)
    last = anon.get("/health").json()["last_run"]
    assert last["trigger"] == "manual"
    assert last["error"] is None
    assert set(last) == {"trigger", "started_at", "finished_at", "cost_usd", "error"}


def test_health_is_503_when_the_database_fails(
    app: FastAPI, anon: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk gone")

    monkeypatch.setattr(app.state.store, "get_setting", boom)
    r = anon.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "down"
    assert r.json()["db"] is False


def test_start_up_seeds_sources(app: FastAPI, anon: TestClient) -> None:
    sources = run(anon, app.state.store.list_sources, enabled_only=False)
    assert len(sources) > 0


def test_ctx_factory_replaces_the_real_wiring(app: FastAPI, anon: TestClient) -> None:
    assert app.state.ctx.clock.now() == NOW
    assert app.state.store.clock is app.state.ctx.clock
    assert app.state.store.canonicalise is canonical_url
    # Built but never started: SCHEDULER_ENABLED is false in the test settings.
    assert app.state.scheduler.alive is False
    assert app.state.scheduler.ctx is app.state.ctx
    assert app.state.run_lock is app.state.scheduler.lock


def test_api_docs_are_off(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_scheduler_starts_and_stops_with_the_nightly_taste_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeScheduler.events = []
    monkeypatch.setattr("ai_newsdesk.app.Scheduler", FakeScheduler)
    app = create_app(
        make_settings(tmp_path, scheduler_enabled=True),
        ctx_factory=fake_ctx_factory,
        key_testers={},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
    )
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/health").json()["scheduler"] is True
        assert app.state.run_lock is app.state.scheduler.lock
        assert app.state.scheduler.kwargs == {"nightly_fn": taste.nightly}
    assert FakeScheduler.events == ["start", "stop"]


def test_disabled_scheduler_is_never_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeScheduler.events = []
    monkeypatch.setattr("ai_newsdesk.app.Scheduler", FakeScheduler)
    app = create_app(
        make_settings(tmp_path, scheduler_enabled=False),
        ctx_factory=fake_ctx_factory,
        key_testers={},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
    )
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/health").json()["scheduler"] is False
    assert FakeScheduler.events == ["stop"]


def test_rebuild_context_hands_the_new_context_to_the_scheduler(app: FastAPI, anon: TestClient) -> None:
    before = app.state.ctx
    run(anon, app.state.rebuild_context)
    assert app.state.ctx is not before
    assert app.state.scheduler.ctx is app.state.ctx
```

`tests/web/test_route_policy.py`:

```python
"""Guards every route added in later tasks: session required, CSRF required on POST."""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.web.support import api_routes

OPEN_GET = {"/login", "/health"}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _paths(app: FastAPI, method: str) -> list[str]:
    paths = [r.path for r in api_routes(app) if method in r.methods]
    assert paths, "no routes found: the policy tests would pass vacuously"
    return paths


def test_every_page_needs_a_session(app: FastAPI, anon: TestClient) -> None:
    for path in _paths(app, "GET"):
        if path in OPEN_GET:
            continue
        r = anon.get(_concrete(path), follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"].startswith("/login?next="), path


def test_every_post_needs_a_session(app: FastAPI, anon: TestClient) -> None:
    for path in _paths(app, "POST"):
        if path == "/login":
            continue
        assert anon.post(_concrete(path), follow_redirects=False).status_code == 401, path


def test_every_post_needs_a_csrf_token(app: FastAPI, client: TestClient) -> None:
    posts = _paths(app, "POST")
    assert "/login" in posts and "/logout" in posts
    for path in posts:
        r = client.post(_concrete(path), data={"x": "1"}, follow_redirects=False)
        assert r.status_code == 403, path
```

`tests/web/test_key_testers.py`:

```python
from __future__ import annotations

import sys
from typing import Any

import httpx
import httpx2  # installed with anthropic >= 1.13, which sends through it
import pytest
import respx

from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.web.keys import check_anthropic_key, check_typesafe_key, research_engine_checker

RE_URL = "http://research-engine.invalid:8000"
COUNT_TOKENS = "https://api.anthropic.com/v1/messages/count_tokens"


def _anthropic_answering(status: int, body: dict[str, Any], seen: list[str]) -> Any:
    """The anthropic SDK uses httpx2 (respx cannot see it), so answer through a MockTransport."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url} key={request.headers.get('x-api-key')}")
        return httpx2.Response(status, json=body)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_anthropic_key_rejected() -> None:
    seen: list[str] = []
    error = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    async with _anthropic_answering(401, error, seen) as http:
        message = await check_anthropic_key("sk-bad", RuntimeSettings(), http_client=http)
    assert message == "Anthropic rejected the key (401)."
    assert seen == [f"POST {COUNT_TOKENS} key=sk-bad"]


async def test_anthropic_key_accepted() -> None:
    seen: list[str] = []
    async with _anthropic_answering(200, {"input_tokens": 5}, seen) as http:
        assert await check_anthropic_key("sk-good", RuntimeSettings(), http_client=http) is None
    assert len(seen) == 1


@respx.mock
async def test_research_engine_key_checked_on_an_authenticated_endpoint() -> None:
    route = respx.get(f"{RE_URL}/v1/engines").mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as http:
        check = research_engine_checker(RE_URL, http)
        assert await check("bad", RuntimeSettings()) == "research-engine rejected the key (401)."
        assert route.calls.last.request.headers["X-API-Key"] == "bad"
        route.mock(return_value=httpx.Response(200, json={"data": {}}))
        assert await check("good", RuntimeSettings()) is None
        route.mock(side_effect=httpx.ConnectError("refused"))
        message = await check("good", RuntimeSettings())
        assert message is not None and message.startswith("Could not reach research-engine")


async def test_typesafe_without_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    message = await check_typesafe_key("ts-key", RuntimeSettings())
    assert message is not None and "isn't installed" in message
```

- [ ] **Step 4: Run the tests to see them fail**

Run: `uv run pytest tests/web/test_app.py tests/web/test_route_policy.py tests/web/test_key_testers.py -q`
Expected: FAIL at collection: `ModuleNotFoundError: No module named 'ai_newsdesk.app'` (and `ai_newsdesk.web.keys`).

- [ ] **Step 5: Implement**

`src/ai_newsdesk/web/runs.py`:

```python
"""Run records as plain JSON-able dicts (for /health and the Settings page)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel

RUN_FIELDS = ("trigger", "started_at", "finished_at", "cost_usd", "error")


def _jsonable(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


def run_dict(run: object) -> dict[str, Any]:
    """Accepts whatever `Store.last_runs` returns per row: a pydantic model or a mapping."""
    if isinstance(run, BaseModel):
        data: Mapping[str, Any] = run.model_dump(mode="json")
    else:
        mapping = cast(Mapping[str, Any], run)
        data = {key: mapping[key] for key in mapping.keys()}
    return {key: _jsonable(data.get(key)) for key in RUN_FIELDS}
```

`src/ai_newsdesk/web/routes_health.py`:

```python
"""GET /health: open, JSON, never cached. 200 while the database answers, 503 otherwise."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ai_newsdesk.web.deps import NO_STORE
from ai_newsdesk.web.runs import run_dict

router = APIRouter(include_in_schema=False)
log = logging.getLogger("ai_newsdesk.web.health")


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    state = request.app.state
    scheduler = getattr(state, "scheduler", None)
    body: dict[str, object] = {
        "status": "up",
        "db": True,
        "scheduler": bool(scheduler is not None and scheduler.alive),
        "last_run": None,
        "git_sha": state.settings.git_sha,
    }
    try:
        await state.store.get_setting("runs_per_day")
        runs = await state.store.last_runs(1)
    except Exception:
        log.exception("health check: database query failed")
        body.update(status="down", db=False)
        return JSONResponse(body, status_code=503, headers=NO_STORE)
    if runs:
        body["last_run"] = run_dict(runs[0])
    return JSONResponse(body, headers=NO_STORE)
```

`src/ai_newsdesk/web/app_routes.py`:

```python
"""All page routers in one place (login and logout are installed by `web.security`)."""

from __future__ import annotations

from fastapi import APIRouter

from ai_newsdesk.web import routes_health


def build_router() -> APIRouter:
    router = APIRouter()
    for sub in (routes_health.router,):
        router.include_router(sub)
    return router
```

`src/ai_newsdesk/web/keys.py`:

```python
"""Provider tests for API keys entered on the Settings page.

Storage is not here: step 1's `ApiKeys` (runtime_settings.py) is the only key store (master plan
R3). Testers make one cheap, side-effect-free call with the candidate key and return None when
it works, else a short message. Callers scrub the key out of any message before showing it.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic
import httpx

from ai_newsdesk.config import Settings
from ai_newsdesk.runtime_settings import ApiKeyName, RuntimeSettings

KEY_LABELS: dict[ApiKeyName, str] = {
    "anthropic": "Anthropic",
    "research_engine": "research-engine",
    "typesafe": "TypeSafe",
}
KeyTester = Callable[[str, RuntimeSettings], Awaitable[str | None]]
TEST_TIMEOUT_S = 15.0
MAX_KEY_CHARS = 500
REDACTED = "[key removed]"
_KEY_LIKE = re.compile(r"\b(?:sk|ts)-[A-Za-z0-9_\-]{4,}|[A-Za-z0-9_\-]{32,}")


def scrub(message: str, key: str) -> str:
    """Remove the key (and anything key-shaped) from a provider message before showing it."""
    out = message.replace(key, REDACTED) if key else message
    return _KEY_LIKE.sub(REDACTED, out)[:300]


async def check_anthropic_key(
    key: str, runtime: RuntimeSettings, *, http_client: Any | None = None
) -> str | None:
    """`count_tokens` costs nothing and proves the key and the writer model are usable.

    `http_client` is for tests: the anthropic SDK (>= 1.13) sends through `httpx2`, which respx
    does not patch, so tests pass an `httpx2.AsyncClient` with a `MockTransport`.
    """
    client = anthropic.AsyncAnthropic(
        api_key=key, max_retries=0, timeout=TEST_TIMEOUT_S, http_client=http_client
    )
    try:
        await client.messages.count_tokens(
            model=runtime.writer_model,
            messages=[{"role": "user", "content": "ping"}],
        )
    except anthropic.AuthenticationError:
        return "Anthropic rejected the key (401)."
    except anthropic.PermissionDeniedError:
        return "Anthropic accepted the key but refused access (403)."
    except anthropic.APIStatusError as exc:
        return f"Anthropic answered {exc.status_code}: {exc.message}"
    except anthropic.APIConnectionError:
        return "Could not reach the Anthropic API. Check the network and try again."
    finally:
        await client.close()
    return None


def research_engine_checker(base_url: str, http: httpx.AsyncClient) -> KeyTester:
    """research-engine's /health is open, so it cannot prove a key. GET /v1/engines needs the
    key and has no side effects."""
    url = base_url.rstrip("/") + "/v1/engines"

    async def check(key: str, runtime: RuntimeSettings) -> str | None:
        try:
            response = await http.get(url, headers={"X-API-Key": key}, timeout=TEST_TIMEOUT_S)
        except httpx.HTTPError as exc:
            return f"Could not reach research-engine ({type(exc).__name__})."
        if response.status_code == 401:
            return "research-engine rejected the key (401)."
        if response.status_code != 200:
            return f"research-engine answered {response.status_code}."
        return None

    return check


async def check_typesafe_key(key: str, runtime: RuntimeSettings) -> str | None:
    """One single-question Noul request (optional `jev` extra)."""
    try:
        sdk: Any = importlib.import_module("typesafe_sdk")
    except ImportError:
        return "The TypeSafe SDK isn't installed. Rebuild the image with the jev extra."
    try:
        async with sdk.AsyncTypeSafeClient(api_key=key) as client:
            await client.system_one(
                state={"document": "ping"},
                questions={"ping": sdk.Noul(instructions="Is this text a test message?")},
            )
    except Exception as exc:
        return f"TypeSafe call failed: {type(exc).__name__}: {exc}"
    return None


def default_key_testers(settings: Settings, http: httpx.AsyncClient) -> dict[ApiKeyName, KeyTester]:
    return {
        "anthropic": check_anthropic_key,
        "research_engine": research_engine_checker(settings.research_engine_url, http),
        "typesafe": check_typesafe_key,
    }
```

`src/ai_newsdesk/app.py`:

```python
"""Composition root: the only module that wires concrete classes to the protocols."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anthropic
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_newsdesk import taste
from ai_newsdesk.budget import Budget
from ai_newsdesk.clock import SystemClock
from ai_newsdesk.config import Settings
from ai_newsdesk.judges.haiku import HaikuJudge
from ai_newsdesk.judges.jev import JevJudge
from ai_newsdesk.normalise import canonical_url
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.pricing import Pricing
from ai_newsdesk.protocols import Clock, Judge
from ai_newsdesk.research_engine import HttpResearchEngine
from ai_newsdesk.runtime_settings import ApiKeys, load_runtime_settings
from ai_newsdesk.scheduler import Scheduler
from ai_newsdesk.sources.registry import build_sources
from ai_newsdesk.store import Store
from ai_newsdesk.web.app_routes import build_router
from ai_newsdesk.web.deps import STATIC_DIR
from ai_newsdesk.web.keys import KeyTester, default_key_testers
from ai_newsdesk.web.security import install_security
from ai_newsdesk.writer import HaikuWriter

log = logging.getLogger("ai_newsdesk.app")


@dataclass
class AppDeps:
    settings: Settings
    store: Store
    pricing: Pricing
    http: httpx.AsyncClient
    clock: Clock
    api_keys: ApiKeys


CtxFactory = Callable[[AppDeps], Awaitable[PipelineContext]]


async def build_context(deps: AppDeps) -> PipelineContext:
    """Real wiring (master plan R8). Called at start-up and again after any settings or key change."""
    settings, store, keys = deps.settings, deps.store, deps.api_keys
    runtime = await load_runtime_settings(store)
    # The provider is async and read per request, so a key saved in the UI applies at once (R3).
    research = HttpResearchEngine(
        settings.research_engine_url,
        lambda: keys.resolve_api_key("research_engine"),
        client=deps.http,
    )
    # An empty key still builds a client; calls then fail with 401 and items stay unscored.
    client = anthropic.AsyncAnthropic(api_key=(await keys.resolve_api_key("anthropic")) or "not-set")
    judge: Judge
    if runtime.judge_provider == "typesafe":
        # judges.jev imports without the optional `jev` extra; the SDK is loaded only on use.
        judge = JevJudge(await keys.resolve_api_key("typesafe"), deps.pricing, model=runtime.judge_model)
    else:
        judge = HaikuJudge(client, runtime.judge_model, deps.pricing)
    return PipelineContext(
        store=store,
        sources=build_sources(deps.http, research, deps.clock),
        research_engine=research,
        judge=judge,
        writer=HaikuWriter(client, runtime.writer_model, deps.pricing, briefing_model=runtime.briefing_model),
        budget=Budget(store, deps.pricing, deps.clock, runtime, tz=settings.tz),
        clock=deps.clock,
        settings=settings,
    )


async def _http_error(request: Request, exc: Exception) -> Response:
    if isinstance(exc, StarletteHTTPException):
        return PlainTextResponse(
            f"{exc.status_code}: {exc.detail}", status_code=exc.status_code, headers=exc.headers
        )
    raise exc


def create_app(
    settings: Settings | None = None,
    *,
    ctx_factory: CtxFactory | None = None,
    key_testers: Mapping[str, KeyTester] | None = None,
    config_dir: Path = Path("config"),
    clock: Clock | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings()
    factory: CtxFactory = ctx_factory if ctx_factory is not None else build_context
    app_clock: Clock = clock if clock is not None else SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.db_path, clock=app_clock, canonicalise=canonical_url)
        await store.init()
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": f"ai-newsdesk/{settings.git_sha}"},
        )
        scheduler: Scheduler | None = None
        background: set[asyncio.Task[None]] = set()
        app.state.background = background
        try:
            await store.seed_sources_if_empty(config_dir / "sources.yaml")
            pricing = Pricing.load(config_dir / "pricing.yaml")
            api_keys = ApiKeys(store, settings)
            deps = AppDeps(
                settings=settings, store=store, pricing=pricing, http=http, clock=app_clock, api_keys=api_keys
            )
            app.state.store = store
            app.state.pricing = pricing
            app.state.http = http
            app.state.api_keys = api_keys
            app.state.deps = deps
            app.state.ctx = await factory(deps)
            app.state.key_testers = (
                dict(key_testers) if key_testers is not None else default_key_testers(settings, http)
            )
            # Always built, so Refresh now can use run_now() even with the schedule switched off.
            scheduler = Scheduler(app.state.ctx, nightly_fn=taste.nightly)
            if settings.scheduler_enabled:
                scheduler.start()
            app.state.scheduler = scheduler
            app.state.run_lock = scheduler.lock
            log.info("ai-newsdesk started (scheduler %s)", "on" if settings.scheduler_enabled else "off")
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            for task in list(background):
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await http.aclose()
            await store.close()

    app = FastAPI(
        title="ai-newsdesk",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def rebuild_context() -> None:
        ctx = await factory(app.state.deps)
        app.state.ctx = ctx
        if app.state.scheduler is not None:
            app.state.scheduler.ctx = ctx

    app.state.settings = settings
    app.state.rebuild_context = rebuild_context
    install_security(app, settings)
    app.include_router(build_router())
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_exception_handler(StarletteHTTPException, _http_error)
    return app
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `uv run pytest tests/web -q`
Expected: all pass (`test_auth.py` 19, `test_app.py` 9, `test_route_policy.py` 3, `test_key_testers.py` 4).
Run: `uv run ruff check && uv run ruff format --check && uv run pyright`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/ai_newsdesk/app.py src/ai_newsdesk/web tests/web
git commit -m "feat(web): create_app, lifespan, key testers and /health (V1-S5)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 27: Design system ("First light"): layout, CSS, icons, view helpers

**Goal:** The shared look: base layout with masthead and nav, the budget banner, one complete stylesheet with light and dark tokens, an SVG icon sprite, and the view helpers every page uses. Pages load nothing but local CSS and the vendored htmx.

**Files:**
- Replace: `src/ai_newsdesk/web/deps.py`
- Replace: `src/ai_newsdesk/web/templates/base.html`
- Create: `src/ai_newsdesk/web/static/app.css`
- Create: `src/ai_newsdesk/web/static/icons.svg`
- Create: `tests/web/test_design_system.py`, `tests/web/test_view_helpers.py`

**Acceptance Criteria:**
- [ ] `base.html` has the masthead (wordmark, nav Today / Briefing / Sources / Settings with `aria-current` on the active page, Log out as a CSRF-protected POST form), a budget-reached banner slot, a skip link, `color-scheme: light dark`, and htmx config that works under the CSP (`includeIndicatorStyles:false`, `allowEval:false`, `selfRequestsOnly:true`, 422 responses swapped).
- [ ] Every HTML page has exactly one script, `/static/htmx.min.js`, no inline scripts, no `<style>` elements, no `style=` attributes, and only `/static/` stylesheets.
- [ ] `app.css` defines colour tokens for light and dark (`prefers-color-scheme`), the type and spacing scales, and styles for masthead, story, gauge, chip, badge, tag, button, form, notice, meter, chart, Sources and Settings layouts, `:focus-visible`, `prefers-reduced-motion` and `forced-colors`.
- [ ] `icons.svg` has symbols `i-thumbs-up`, `i-thumbs-down`, `i-external-link`, `i-refresh`, `i-x-logo`, `i-rss`, `i-hn`; static files are served without login.
- [ ] View helpers: `ago`, `day_label`, `short_date`, `usd`, `safe_url` (only `http`/`https`, else `#`), `signal_of` (0–10), `unique_sources`, labels and `source_detail` behave as tested.

**Verify:** `uv run pytest tests/web/test_design_system.py tests/web/test_view_helpers.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

`tests/web/test_view_helpers.py`:

```python
from __future__ import annotations

from datetime import date, timedelta

from ai_newsdesk.models import Category, SourceConfig, SourceKind, StorySource, Trust
from ai_newsdesk.web.deps import (
    ago,
    category_label,
    day_label,
    icon_for,
    kind_label,
    safe_url,
    short_date,
    signal_of,
    source_detail,
    trust_label,
    unique_sources,
    usd,
)
from tests.web.support import NOW


def test_ago() -> None:
    assert ago(NOW - timedelta(seconds=20), NOW) == "just now"
    assert ago(NOW - timedelta(minutes=7), NOW) == "7 min ago"
    assert ago(NOW - timedelta(hours=3, minutes=5), NOW) == "3 h ago"
    assert ago(NOW - timedelta(days=1, hours=2), NOW) == "yesterday"
    assert ago(NOW - timedelta(days=3), NOW) == "3 days ago"
    assert ago(NOW - timedelta(days=40), NOW) == "4 Sep 2026"
    assert ago(NOW.replace(tzinfo=None) - timedelta(hours=1), NOW) == "1 h ago"
    assert ago(None, NOW) == "never"


def test_dates() -> None:
    assert day_label(date(2026, 10, 14)) == "Wednesday 14 October"
    assert short_date(date(2026, 10, 3)) == "Sat 3 Oct"


def test_usd() -> None:
    assert usd(0) == "$0.00"
    assert usd(0.004) == "under $0.01"
    assert usd(2.07) == "$2.07"
    assert usd(1234.5) == "$1,234.50"


def test_safe_url_allows_only_web_links() -> None:
    assert safe_url("https://lab.example/a?b=1") == "https://lab.example/a?b=1"
    assert safe_url(" http://lab.example/ ") == "http://lab.example/"
    for bad in ("javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,x", "//evil.example", "/local", "", "https://"):
        assert safe_url(bad) == "#"


def test_signal_of_clamps_and_rounds() -> None:
    assert signal_of(7.6) == 8
    assert signal_of(12.4) == 10
    assert signal_of(-1) == 0


def test_unique_sources_keeps_first_per_name() -> None:
    a = StorySource(item_id=1, source_name="Lab", source_kind=SourceKind.FEED, url="https://a.example", title="A")
    b = StorySource(item_id=2, source_name="Lab", source_kind=SourceKind.FEED, url="https://b.example", title="B")
    c = StorySource(item_id=3, source_name="HN", source_kind=SourceKind.HN, url="https://c.example", title="C")
    assert [s.item_id for s in unique_sources([a, b, c])] == [1, 3]


def test_labels_and_icons() -> None:
    assert category_label(Category.MODEL_RELEASE) == "Model releases"
    assert category_label("research") == "Research"
    assert kind_label(SourceKind.X_PERSON) == "People on X"
    assert trust_label(Trust.PRIMARY) == "Primary"
    assert icon_for(SourceKind.X_PERSON) == "i-x-logo"
    assert icon_for(SourceKind.FEED) == "i-rss"
    assert icon_for(SourceKind.HN) == "i-hn"
    assert icon_for(SourceKind.SEARCH) is None


def test_source_detail() -> None:
    def src(kind: SourceKind, config: dict[str, str | int | float | bool | list[str]]) -> SourceConfig:
        return SourceConfig(id=1, kind=kind, name="n", config=config, trust=Trust.TRUSTED)

    assert source_detail(src(SourceKind.FEED, {"url": "https://a.example/feed"})) == "https://a.example/feed"
    assert source_detail(src(SourceKind.X_PERSON, {"handle": "karpathy"})) == "@karpathy"
    assert source_detail(src(SourceKind.SEARCH, {"query": "new AI model"})) == "Search: new AI model"
    assert source_detail(src(SourceKind.GITHUB, {"repo": "org/repo"})) == "github.com/org/repo"
    assert source_detail(src(SourceKind.ARXIV, {"categories": ["cs.AI", "cs.CL"]})) == "cs.AI, cs.CL"
    assert source_detail(src(SourceKind.HN, {})) == "Front-page stories above the points threshold"
```

`tests/web/test_design_system.py`:

```python
from __future__ import annotations

import hashlib
from html.parser import HTMLParser

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.web.deps import STATIC_DIR, templates
from tests.web.support import api_routes, csrf_token

HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
ICONS = ("thumbs-up", "thumbs-down", "external-link", "refresh", "x-logo", "rss", "hn")


class _Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str | None] = []
        self.inline_script = False
        self.stylesheets: list[str] = []
        self.style_elements = 0
        self.style_attrs = 0
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if "style" in a:
            self.style_attrs += 1
        if tag == "script":
            self.scripts.append(a.get("src"))
            self._in_script = True
        elif tag == "style":
            self.style_elements += 1
        elif tag == "link" and a.get("rel") == "stylesheet":
            self.stylesheets.append(a.get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_script = True


def _check_page(html: str, where: str) -> None:
    p = _Assets()
    p.feed(html)
    assert p.scripts == ["/static/htmx.min.js"], where
    assert not p.inline_script, where
    assert p.style_elements == 0, where
    assert p.style_attrs == 0, where
    assert p.stylesheets and all(href.startswith("/static/") for href in p.stylesheets), where


def test_vendored_htmx_matches_the_pin() -> None:
    assert hashlib.sha256((STATIC_DIR / "htmx.min.js").read_bytes()).hexdigest() == HTMX_SHA256


def test_icon_sprite_has_every_icon() -> None:
    sprite = (STATIC_DIR / "icons.svg").read_text()
    for name in ICONS:
        assert f'id="i-{name}"' in sprite


def test_stylesheet_covers_both_themes_and_motion() -> None:
    css = (STATIC_DIR / "app.css").read_text()
    for needle in (
        "prefers-color-scheme: dark",
        "prefers-reduced-motion: reduce",
        "forced-colors: active",
        ":focus-visible",
        "--accent:",
        "--font-display:",
        ".gauge",
        ".chip",
        ".meter",
        ".chart__bar",
    ):
        assert needle in css, needle


def test_static_files_are_served_without_login(anon: TestClient) -> None:
    for path, kind in (
        ("/static/app.css", "text/css"),
        ("/static/icons.svg", "image/svg+xml"),
        ("/static/htmx.min.js", "javascript"),
    ):
        r = anon.get(path)
        assert r.status_code == 200, path
        assert kind in r.headers["content-type"], path


def test_base_template_has_masthead_banner_and_only_local_assets() -> None:
    html = templates.get_template("base.html").render(csrf_token="tok", active="sources", budget_reached=True)
    _check_page(html, "base.html")
    assert '<a href="/sources" aria-current="page">Sources</a>' in html
    assert "budget is spent" in html
    assert 'hx-headers=\'{"X-CSRF-Token": "tok"}\'' in html
    quiet = templates.get_template("base.html").render(csrf_token="tok", active="today", budget_reached=False)
    assert "budget is spent" not in quiet


def test_every_page_loads_only_local_assets(app: FastAPI, client: TestClient) -> None:
    pages = [r.path for r in api_routes(app) if "GET" in r.methods and "{" not in r.path and r.path != "/health"]
    assert "/" in pages and "/login" in pages
    for path in pages:
        r = client.get(path)
        if r.headers.get("content-type", "").startswith("text/html"):
            _check_page(r.text, path)
    client.post("/logout", data={"csrf_token": csrf_token(client)})
    login_page = client.get("/login")
    assert login_page.status_code == 200
    _check_page(login_page.text, "/login")
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/web/test_design_system.py tests/web/test_view_helpers.py -q`
Expected: FAIL: `ImportError: cannot import name 'ago' from 'ai_newsdesk.web.deps'`.

- [ ] **Step 3: Implement**

`src/ai_newsdesk/web/deps.py` (replace the whole file):

```python
"""Template environment, view helpers and page rendering shared by every route module."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from ai_newsdesk.models import Category, SourceConfig, SourceKind, StorySource, Trust
from ai_newsdesk.runtime_settings import ApiKeys, load_runtime_settings
from ai_newsdesk.store import Store

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
NO_STORE = {"Cache-Control": "no-store"}

CATEGORY_LABELS: dict[Category, str] = {
    Category.MODEL_RELEASE: "Model releases",
    Category.RESEARCH: "Research",
    Category.PRODUCT: "Products",
    Category.POLICY: "Policy",
    Category.INDUSTRY: "Industry",
    Category.OPINION: "Opinion",
    Category.OTHER: "Other",
}
KIND_LABELS: dict[SourceKind, str] = {
    SourceKind.FEED: "Feeds",
    SourceKind.X_PERSON: "People on X",
    SourceKind.SEARCH: "Searches",
    SourceKind.GITHUB: "GitHub releases",
    SourceKind.ARXIV: "arXiv",
    SourceKind.HN: "Hacker News",
}
TRUST_LABELS: dict[Trust, str] = {
    Trust.PRIMARY: "Primary",
    Trust.TRUSTED: "Trusted",
    Trust.DISCOVERY: "Discovery",
}
KIND_ICONS: dict[SourceKind, str] = {
    SourceKind.FEED: "i-rss",
    SourceKind.X_PERSON: "i-x-logo",
    SourceKind.HN: "i-hn",
}


def ago(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    seconds = (now - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return f"{dt.day} {dt:%b %Y}"


def day_label(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B}"


def short_date(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def usd(value: float) -> str:
    if value <= 0:
        return "$0.00"
    if value < 0.01:
        return "under $0.01"
    return f"${value:,.2f}"


def safe_url(url: str) -> str:
    """Outbound links come from untrusted feeds: only absolute http(s) URLs survive."""
    candidate = url.strip()
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return "#"
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "#"
    return candidate


def signal_of(score: float) -> int:
    return max(0, min(10, round(score)))


def unique_sources(sources: Iterable[StorySource]) -> list[StorySource]:
    seen: set[str] = set()
    out: list[StorySource] = []
    for src in sources:
        if src.source_name not in seen:
            seen.add(src.source_name)
            out.append(src)
    return out


def category_label(category: Category | str) -> str:
    try:
        return CATEGORY_LABELS[Category(category)]
    except ValueError:
        return str(category)


def kind_label(kind: SourceKind | str) -> str:
    return KIND_LABELS.get(SourceKind(kind), str(kind))


def trust_label(trust: Trust | str) -> str:
    return TRUST_LABELS.get(Trust(trust), str(trust))


def icon_for(kind: SourceKind | str) -> str | None:
    return KIND_ICONS.get(SourceKind(kind))


def source_detail(src: SourceConfig) -> str:
    cfg = src.config
    match src.kind:
        case SourceKind.FEED:
            return str(cfg.get("url", ""))
        case SourceKind.X_PERSON:
            return f"@{cfg.get('handle', '')}"
        case SourceKind.SEARCH:
            return f"Search: {cfg.get('query', '')}"
        case SourceKind.GITHUB:
            return f"github.com/{cfg.get('repo', '')}"
        case SourceKind.ARXIV:
            cats = cfg.get("categories", [])
            return ", ".join(cats) if isinstance(cats, list) else str(cats)
        case SourceKind.HN:
            return "Front-page stories above the points threshold"


templates = Jinja2Templates(
    env=Environment(
        loader=FileSystemLoader(WEB_DIR / "templates"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
)
templates.env.globals.update(
    ago=ago,
    day_label=day_label,
    short_date=short_date,
    usd=usd,
    signal_of=signal_of,
    unique_sources=unique_sources,
    category_label=category_label,
    kind_label=kind_label,
    trust_label=trust_label,
    icon_for=icon_for,
    source_detail=source_detail,
)
templates.env.filters["safe_url"] = safe_url


def store_of(request: Request) -> Store:
    return request.app.state.store


def api_keys_of(request: Request) -> ApiKeys:
    return request.app.state.api_keys


def now_of(request: Request) -> datetime:
    return request.app.state.ctx.clock.now()


def zone_of(request: Request) -> ZoneInfo:
    return ZoneInfo(request.app.state.settings.tz)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


async def page(
    request: Request, name: str, context: Mapping[str, Any], *, status_code: int = 200
) -> HTMLResponse:
    """A full page: adds the CSRF token, clock, zone, runtime settings and the budget banner flag."""
    store = store_of(request)
    now = now_of(request)
    runtime = await load_runtime_settings(store)
    spent = await store.month_spend(now, request.app.state.settings.tz)
    full: dict[str, Any] = {
        "csrf_token": request.state.csrf_token,
        "now": now,
        "zone": zone_of(request),
        "runtime": runtime,
        "budget_reached": spent >= runtime.monthly_budget_usd,
        "active": "",
    }
    full.update(context)
    return templates.TemplateResponse(request, name, full, status_code=status_code, headers=NO_STORE)


def partial(
    request: Request, name: str, context: Mapping[str, Any], *, status_code: int = 200
) -> HTMLResponse:
    """An htmx fragment: no layout, same escaping rules."""
    full: dict[str, Any] = {
        "csrf_token": request.state.csrf_token,
        "now": now_of(request),
        "zone": zone_of(request),
    }
    full.update(context)
    return templates.TemplateResponse(request, name, full, status_code=status_code, headers=NO_STORE)
```

`src/ai_newsdesk/web/templates/base.html` (replace the whole file):

```html
<!doctype html>
<html lang="en-GB">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light dark">
  <meta name="theme-color" content="#edf1f4" media="(prefers-color-scheme: light)">
  <meta name="theme-color" content="#121a25" media="(prefers-color-scheme: dark)">
  <meta name="htmx-config" content='{"includeIndicatorStyles":false,"allowEval":false,"allowScriptTags":false,"selfRequestsOnly":true,"historyCacheSize":0,"responseHandling":[{"code":"204","swap":false},{"code":"[23]..","swap":true},{"code":"422","swap":true},{"code":"[45]..","swap":false,"error":true}]}'>
  <title>{% block title %}Newsdesk{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="/static/htmx.min.js" defer></script>
</head>
<body hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'>
  <a class="skip" href="#main">Skip to content</a>
  {% block masthead %}
  <header class="masthead">
    <div class="masthead__inner">
      <a class="wordmark" href="/">Newsdesk</a>
      <nav class="nav" aria-label="Main">
        <a href="/"{% if active == "today" %} aria-current="page"{% endif %}>Today</a>
        <a href="/briefing"{% if active == "briefing" %} aria-current="page"{% endif %}>Briefing</a>
        <a href="/sources"{% if active == "sources" %} aria-current="page"{% endif %}>Sources</a>
        <a href="/settings"{% if active == "settings" %} aria-current="page"{% endif %}>Settings</a>
      </nav>
      <form class="logout" method="post" action="/logout">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button class="link-button" type="submit">Log out</button>
      </form>
    </div>
  </header>
  {% if budget_reached %}
  <div class="banner" role="status">
    <p><strong>This month's budget is spent.</strong> Collection carries on, but new items wait unscored until the budget resets on the 1st. <a href="/settings#spending">Change the budget</a></p>
  </div>
  {% endif %}
  {% endblock %}
  <main id="main" class="page">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

`src/ai_newsdesk/web/static/icons.svg`:

```html
<svg xmlns="http://www.w3.org/2000/svg">
  <symbol id="i-thumbs-up" viewBox="0 0 24 24">
    <g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
      <path d="M7 11v9H4.5a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1H7Z"/>
      <path d="M7 11l3.6-7.2a2 2 0 0 1 3.4 1.4V9h5a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.8 20H7"/>
    </g>
  </symbol>
  <symbol id="i-thumbs-down" viewBox="0 0 24 24">
    <g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" transform="rotate(180 12 12)">
      <path d="M7 11v9H4.5a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1H7Z"/>
      <path d="M7 11l3.6-7.2a2 2 0 0 1 3.4 1.4V9h5a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.8 20H7"/>
    </g>
  </symbol>
  <symbol id="i-external-link" viewBox="0 0 24 24">
    <g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
      <path d="M14 4h6v6"/>
      <path d="M20 4l-9 9"/>
      <path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>
    </g>
  </symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24">
    <g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
      <path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/>
      <path d="M4 4v4h4"/>
      <path d="M4 13a8 8 0 0 0 14.3 4.9L20 16"/>
      <path d="M20 20v-4h-4"/>
    </g>
  </symbol>
  <symbol id="i-x-logo" viewBox="0 0 24 24">
    <path fill="currentColor" d="M17.8 3h3.1l-6.8 7.7L22 21h-6.2l-4.9-6.4L5.3 21H2.2l7.2-8.3L1.8 3h6.4l4.4 5.8L17.8 3Zm-1.1 16.2h1.7L7.4 4.7H5.5l11.2 14.5Z"/>
  </symbol>
  <symbol id="i-rss" viewBox="0 0 24 24">
    <g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
      <path d="M5 5a14 14 0 0 1 14 14"/>
      <path d="M5 11a8 8 0 0 1 8 8"/>
    </g>
    <circle cx="6" cy="18" r="1.6" fill="currentColor"/>
  </symbol>
  <symbol id="i-hn" viewBox="0 0 24 24">
    <g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3.5" y="3.5" width="17" height="17" rx="3"/>
      <path d="M8.5 7.5 12 13l3.5-5.5"/>
      <path d="M12 13v4"/>
    </g>
  </symbol>
</svg>
```

`src/ai_newsdesk/web/static/app.css`:

```css
/* ai-newsdesk: "First light".
   A cool, quiet morning paper. Book-serif headlines, system sans for everything else.
   One accent (first-light amber) and it means one thing: signal. */

/* ---------- Tokens ---------- */
:root {
  color-scheme: light dark;

  /* colour: light */
  --paper: #edf1f4;
  --surface: #f7f9fb;
  --ink: #172230;
  --ink-2: #46546a;
  --ink-3: #5e6b7d;
  --rule: #cfd8e0;
  --rule-strong: #aab6c2;
  --track: #dce3e9;
  --accent: #a65f00;
  --accent-wash: #f5e6cd;
  --on-ink: #f7f9fb;
  --danger: #a3322a;
  --danger-wash: #f7e1de;
  --ok: #2f6b4f;
  --ok-wash: #dcefe5;

  /* type */
  --font-display: "Iowan Old Style", "Charter", "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif;
  --font-body: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --step--2: 0.75rem;
  --step--1: 0.875rem;
  --step-0: 1rem;
  --step-1: 1.1875rem;
  --step-2: 1.4375rem;
  --step-3: clamp(1.625rem, 1.35rem + 1.2vw, 2.0625rem);
  --step-4: clamp(2rem, 1.55rem + 2.2vw, 2.875rem);
  --leading-tight: 1.12;
  --leading-head: 1.22;
  --leading-body: 1.55;
  --leading-read: 1.7;

  /* space */
  --space-1: 0.25rem;
  --space-2: 0.5rem;
  --space-3: 0.75rem;
  --space-4: 1rem;
  --space-5: 1.5rem;
  --space-6: 2rem;
  --space-7: 3rem;
  --space-8: 4.5rem;
  --gutter: 1rem;

  /* shape and layout */
  --measure: 40rem;
  --wide: 70rem;
  --radius-s: 4px;
  --radius-m: 8px;
  --radius-pill: 999px;
  --gauge-w: 4px;
  --dur: 160ms;
}

@media (prefers-color-scheme: dark) {
  :root {
    --paper: #121a25;
    --surface: #18222f;
    --ink: #e4e9ef;
    --ink-2: #b3becb;
    --ink-3: #8d9aaa;
    --rule: #2a3646;
    --rule-strong: #3e4c5e;
    --track: #263243;
    --accent: #e2a63c;
    --accent-wash: #2f2817;
    --on-ink: #121a25;
    --danger: #f08b80;
    --danger-wash: #3a1f1e;
    --ok: #7cc4a0;
    --ok-wash: #183026;
  }
}

/* ---------- Base ---------- */
*,
*::before,
*::after {
  box-sizing: border-box;
}

html {
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}

@media (min-width: 48rem) {
  html {
    font-size: 106.25%; /* 17px */
  }
  :root {
    --gutter: 2rem;
  }
}

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font: 400 var(--step-0) / var(--leading-body) var(--font-body);
  font-kerning: normal;
  text-rendering: optimizeLegibility;
  overflow-wrap: break-word;
  -webkit-font-smoothing: antialiased;
}

h1,
h2,
h3 {
  margin: 0;
  font-family: var(--font-display);
  font-weight: 600;
  line-height: var(--leading-head);
  letter-spacing: -0.005em;
  text-wrap: balance;
}

p,
ul,
ol,
dl,
figure {
  margin: 0;
}

p {
  text-wrap: pretty;
}

a {
  color: inherit;
  text-decoration-line: underline;
  text-decoration-color: var(--rule-strong);
  text-decoration-thickness: 1px;
  text-underline-offset: 0.18em;
}

a:hover {
  text-decoration-color: var(--accent);
}

code {
  font-size: 0.92em;
  padding: 0.05em 0.35em;
  border-radius: var(--radius-s);
  background: var(--track);
}

:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: 2px;
}

.icon {
  width: 1em;
  height: 1em;
  flex: none;
  vertical-align: -0.125em;
}

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.skip {
  position: absolute;
  left: var(--gutter);
  top: -4rem;
  z-index: 10;
  padding: var(--space-2) var(--space-4);
  background: var(--ink);
  color: var(--on-ink);
  border-radius: var(--radius-s);
}

.skip:focus {
  top: var(--space-2);
}

/* ---------- Masthead ---------- */
.masthead {
  border-bottom: 1px solid var(--rule);
}

.masthead__inner {
  max-width: var(--wide);
  margin: 0 auto;
  padding: var(--space-3) var(--gutter);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2) var(--space-5);
}

.wordmark {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-family: var(--font-display);
  font-size: var(--step-2);
  font-style: italic;
  font-weight: 700;
  letter-spacing: -0.01em;
  text-decoration: none;
  color: var(--ink);
}

/* The gauge in miniature: the one signature mark, repeated. */
.wordmark::before {
  content: "";
  width: 5px;
  height: 1.05em;
  border-radius: 3px;
  background: linear-gradient(to top, var(--accent) 0 68%, var(--track) 68% 100%);
}

.wordmark--large {
  font-size: var(--step-3);
}

.nav {
  display: flex;
  gap: var(--space-5);
  margin-left: auto;
  font-size: var(--step--1);
}

.nav a {
  padding-block: var(--space-2);
  color: var(--ink-2);
  text-decoration: none;
  border-bottom: 2px solid transparent;
}

.nav a:hover {
  color: var(--ink);
}

.nav a[aria-current="page"] {
  color: var(--ink);
  font-weight: 600;
  border-bottom-color: var(--accent);
}

.logout {
  margin: 0;
  font-size: var(--step--1);
}

@media (max-width: 40rem) {
  .masthead__inner {
    justify-content: space-between;
  }
  .nav {
    order: 3;
    width: 100%;
    margin-left: 0;
    justify-content: space-between;
  }
  .logout {
    order: 2;
  }
}

.banner {
  background: var(--accent-wash);
  border-bottom: 1px solid var(--rule);
}

.banner p {
  max-width: var(--wide);
  margin: 0 auto;
  padding: var(--space-3) var(--gutter);
  font-size: var(--step--1);
}

/* ---------- Page scaffolding ---------- */
.page {
  max-width: var(--wide);
  margin: 0 auto;
  padding: var(--space-6) var(--gutter) var(--space-8);
}

.page-head {
  display: grid;
  gap: var(--space-3);
  max-width: var(--measure);
  margin-bottom: var(--space-6);
}

.page-head h1 {
  font-size: var(--step-4);
  line-height: var(--leading-tight);
  letter-spacing: -0.015em;
}

.lede {
  font-family: var(--font-display);
  font-size: var(--step-1);
  color: var(--ink-2);
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-2);
}

.switch {
  display: inline-flex;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--rule);
  border-radius: var(--radius-pill);
  background: var(--surface);
  font-size: var(--step--1);
}

.switch a {
  padding: 0.35rem 0.95rem;
  border-radius: var(--radius-pill);
  color: var(--ink-2);
  text-decoration: none;
}

.switch a[aria-current="true"] {
  background: var(--ink);
  color: var(--on-ink);
}

.notice {
  max-width: var(--measure);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--rule);
  border-radius: var(--radius-m);
  background: var(--surface);
  font-size: var(--step--1);
}

.notice + .notice,
.notice + * {
  margin-top: var(--space-5);
}

.notice--ok {
  border-color: var(--ok);
  background: var(--ok-wash);
}

.notice--error {
  border-color: var(--danger);
  background: var(--danger-wash);
}

.empty {
  max-width: var(--measure);
  padding-block: var(--space-6);
}

.empty h2 {
  font-size: var(--step-3);
  font-style: italic;
}

.empty p {
  margin-top: var(--space-3);
  color: var(--ink-2);
}

/* ---------- Sections ---------- */
.section {
  scroll-margin-top: var(--space-5);
}

.section + .section {
  margin-top: var(--space-7);
}

.section__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--rule-strong);
}

.section__head h2 {
  font-size: var(--step-2);
  font-style: italic;
}

.section__count {
  color: var(--ink-3);
  font-size: var(--step--1);
  font-variant-numeric: tabular-nums;
}

.section__note {
  margin-top: var(--space-3);
  color: var(--ink-2);
  font-size: var(--step--1);
  max-width: var(--measure);
}

/* ---------- Today ---------- */
.today {
  display: grid;
  gap: var(--space-6);
}

.stream {
  max-width: var(--measure);
  min-width: 0;
}

.index {
  display: none;
}

@media (min-width: 64rem) {
  .today--indexed {
    grid-template-columns: 11rem minmax(0, var(--measure));
    column-gap: var(--space-8);
  }
  .today--indexed .index {
    display: block;
    position: sticky;
    top: var(--space-5);
    align-self: start;
  }
}

.index ul {
  list-style: none;
  padding: 0;
  display: grid;
  gap: var(--space-1);
  font-size: var(--step--1);
}

.index a {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-1) 0;
  color: var(--ink-2);
  text-decoration: none;
}

.index a:hover {
  color: var(--ink);
}

.index .count {
  color: var(--ink-3);
  font-variant-numeric: tabular-nums;
}

/* Story: sits on the paper, no box. The gauge on the left is the signal. */
.story {
  --signal: 0;
  display: grid;
  grid-template-columns: var(--gauge-w) minmax(0, 1fr);
  column-gap: var(--space-4);
  padding-block: var(--space-5);
  border-bottom: 1px solid var(--rule);
  scroll-margin-top: var(--space-5);
}

.story:last-child {
  border-bottom: 0;
}

.story[data-signal="0"] { --signal: 0.03; }
.story[data-signal="1"] { --signal: 0.1; }
.story[data-signal="2"] { --signal: 0.2; }
.story[data-signal="3"] { --signal: 0.3; }
.story[data-signal="4"] { --signal: 0.4; }
.story[data-signal="5"] { --signal: 0.5; }
.story[data-signal="6"] { --signal: 0.6; }
.story[data-signal="7"] { --signal: 0.7; }
.story[data-signal="8"] { --signal: 0.8; }
.story[data-signal="9"] { --signal: 0.9; }
.story[data-signal="10"] { --signal: 1; }

.gauge {
  width: var(--gauge-w);
  border-radius: 2px;
  background: linear-gradient(
    to top,
    var(--accent) 0 calc(var(--signal) * 100%),
    var(--track) calc(var(--signal) * 100%) 100%
  );
}

.story__body {
  min-width: 0;
}

.story__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1) var(--space-3);
  margin-bottom: var(--space-2);
  color: var(--ink-3);
  font-size: var(--step--1);
}

.badge {
  color: var(--accent);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.badge--unscored {
  padding: 0 0.45rem;
  border: 1px dashed var(--rule-strong);
  border-radius: var(--radius-pill);
  color: var(--ink-2);
  font-weight: 500;
}

.story__headline {
  font-size: var(--step-2);
}

.story--lead .story__headline {
  font-size: var(--step-3);
  line-height: var(--leading-tight);
}

.story__headline a {
  text-decoration: none;
}

.story__headline a:hover {
  text-decoration: underline;
  text-decoration-color: var(--accent);
}

.story__summary {
  margin-top: var(--space-2);
  max-width: 38em;
  line-height: 1.6;
}

.story__summary--pending {
  color: var(--ink-3);
  font-style: italic;
}

.story__why {
  margin-top: var(--space-3);
  max-width: 38em;
  font-family: var(--font-display);
  font-size: var(--step-1);
  font-style: italic;
  line-height: 1.5;
  color: var(--ink-2);
}

.story__why strong {
  font-family: var(--font-body);
  font-size: var(--step--1);
  font-style: normal;
  font-weight: 600;
  color: var(--ink);
}

.story__foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-4);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  list-style: none;
  padding: 0;
  min-width: 0;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  max-width: 15rem;
  padding: 0.2rem 0.65rem;
  border: 1px solid var(--rule);
  border-radius: var(--radius-pill);
  background: var(--surface);
  color: var(--ink-2);
  font-size: var(--step--1);
  text-decoration: none;
}

.chip span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chip:hover {
  border-color: var(--rule-strong);
  color: var(--ink);
}

.thumbs {
  display: flex;
  gap: var(--space-1);
  margin: 0 0 0 auto;
}

.thumb {
  appearance: none;
  display: grid;
  place-items: center;
  width: 2.375rem;
  height: 2.375rem;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--ink-3);
  cursor: pointer;
}

.thumb .icon {
  width: 1.15rem;
  height: 1.15rem;
}

.thumb:hover {
  border-color: var(--rule);
  color: var(--ink);
}

.thumb[aria-pressed="true"] {
  border-color: var(--accent);
  background: var(--accent-wash);
  color: var(--accent);
}

@media (pointer: coarse) {
  .thumb {
    width: 2.75rem;
    height: 2.75rem;
  }
}

.more {
  margin-top: var(--space-3);
  font-size: var(--step--1);
}

.more summary {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  color: var(--ink-2);
  cursor: pointer;
  list-style: none;
}

.more summary::-webkit-details-marker {
  display: none;
}

.more summary::before {
  content: "+";
  width: 1em;
  text-align: center;
  color: var(--accent);
  font-weight: 700;
}

.more[open] summary::before {
  content: "\2212";
}

.more ul {
  list-style: none;
  display: grid;
  gap: var(--space-2);
  margin-top: var(--space-3);
  padding: 0 0 0 var(--space-3);
  border-left: 1px solid var(--rule);
}

.more li {
  display: grid;
  gap: 0.1rem;
}

.more .src {
  color: var(--ink-3);
}

.story__note {
  margin-top: var(--space-3);
  font-size: var(--step--1);
  color: var(--ink-2);
}

.block__form {
  display: grid;
  gap: var(--space-2);
  max-width: 24rem;
  margin-top: var(--space-3);
}

.block__form label {
  font-weight: 600;
}

.unscored {
  list-style: none;
  padding: 0;
}

.unscored li {
  padding-block: var(--space-4);
  border-bottom: 1px solid var(--rule);
}

.unscored li:last-child {
  border-bottom: 0;
}

.unscored__title {
  font-family: var(--font-display);
  font-size: var(--step-1);
  font-weight: 600;
  line-height: var(--leading-head);
}

/* ---------- Briefing ---------- */
.briefing {
  display: grid;
  gap: var(--space-7);
}

@media (min-width: 64rem) {
  .briefing {
    grid-template-columns: minmax(0, var(--measure)) 13rem;
    column-gap: var(--space-8);
  }
}

.read {
  max-width: 36em;
  font-family: var(--font-display);
  font-size: var(--step-1);
  line-height: var(--leading-read);
}

.read h2 {
  margin: var(--space-6) 0 var(--space-3);
  font-size: var(--step-2);
  font-style: italic;
}

.read h2:first-child {
  margin-top: 0;
}

.read p + p {
  margin-top: var(--space-4);
}

.ref {
  font-family: var(--font-body);
  font-size: var(--step--1);
  color: var(--ink-2);
  text-decoration-color: var(--accent);
  white-space: nowrap;
}

.archive h2 {
  font-size: var(--step-1);
  font-style: italic;
  margin-bottom: var(--space-3);
}

.archive ul {
  list-style: none;
  padding: 0;
  display: grid;
  gap: var(--space-2);
  font-size: var(--step--1);
}

.archive a {
  color: var(--ink-2);
  text-decoration: none;
}

.archive a:hover {
  color: var(--ink);
  text-decoration: underline;
}

.archive a[aria-current="page"] {
  color: var(--ink);
  font-weight: 600;
}

/* ---------- Buttons ---------- */
.button {
  appearance: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  min-height: 2.5rem;
  padding: 0.6rem 1.1rem;
  border: 1px solid var(--ink);
  border-radius: var(--radius-m);
  background: var(--ink);
  color: var(--on-ink);
  font: 600 var(--step--1) / 1.1 var(--font-body);
  text-decoration: none;
  cursor: pointer;
}

.button:hover {
  background: color-mix(in oklab, var(--ink) 86%, var(--accent));
}

.button--quiet {
  border-color: var(--rule-strong);
  background: transparent;
  color: var(--ink);
}

.button--quiet:hover {
  border-color: var(--ink);
  background: var(--surface);
}

.button--danger {
  border-color: color-mix(in oklab, var(--danger) 55%, var(--rule));
  background: transparent;
  color: var(--danger);
}

.button--danger:hover {
  border-color: var(--danger);
  background: var(--danger-wash);
}

.button--small {
  min-height: 2.125rem;
  padding: 0.4rem 0.8rem;
}

.button[disabled],
.button.htmx-request,
.htmx-request .button {
  opacity: 0.55;
  cursor: progress;
}

.link-button {
  appearance: none;
  padding: 0;
  border: 0;
  background: none;
  color: var(--ink-2);
  font: inherit;
  text-decoration: underline;
  text-decoration-color: var(--rule-strong);
  text-underline-offset: 0.18em;
  cursor: pointer;
}

.link-button:hover {
  color: var(--ink);
  text-decoration-color: var(--accent);
}

/* ---------- Forms ---------- */
.form {
  display: grid;
  gap: var(--space-5);
}

.form__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
}

.field {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
}

.field > label,
.field > .field__label {
  font-size: var(--step--1);
  font-weight: 600;
}

.field__hint {
  color: var(--ink-3);
  font-size: var(--step--1);
}

.field__error {
  color: var(--danger);
  font-size: var(--step--1);
  font-weight: 600;
}

input[type="text"],
input[type="password"],
input[type="url"],
input[type="number"],
input[type="time"],
select {
  width: 100%;
  max-width: 30rem;
  min-height: 2.75rem;
  padding: 0.55rem 0.75rem;
  border: 1px solid var(--rule-strong);
  border-radius: var(--radius-s);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
}

input[type="number"],
input[type="time"] {
  max-width: 11rem;
  font-variant-numeric: tabular-nums;
}

input:focus-visible,
select:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
  border-color: var(--accent);
}

[aria-invalid="true"] {
  border-color: var(--danger);
}

.check {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
}

.check input {
  width: 1.15rem;
  height: 1.15rem;
  margin: 0.2rem 0 0;
  accent-color: var(--accent);
}

.check label {
  font-weight: 600;
  font-size: var(--step--1);
}

.grid-2 {
  display: grid;
  gap: var(--space-5);
}

@media (min-width: 40rem) {
  .grid-2 {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.tag {
  display: inline-block;
  padding: 0.05rem 0.5rem;
  border-radius: var(--radius-pill);
  background: var(--track);
  color: var(--ink-2);
  font-family: var(--font-body);
  font-size: var(--step--2);
  font-style: normal;
  font-weight: 600;
  vertical-align: 0.15em;
  white-space: nowrap;
}

.tag--primary {
  background: var(--ink);
  color: var(--on-ink);
}

.tag--ok {
  background: var(--ok-wash);
  color: var(--ok);
}

.tag--muted {
  background: transparent;
  border: 1px dashed var(--rule-strong);
}

/* ---------- Sources ---------- */
.add-source {
  max-width: var(--measure);
  margin-bottom: var(--space-7);
  padding: var(--space-4) var(--space-5);
  border: 1px solid var(--rule);
  border-radius: var(--radius-m);
  background: var(--surface);
}

.add-source summary {
  font-weight: 600;
  cursor: pointer;
}

.add-source[open] summary {
  margin-bottom: var(--space-5);
}

.kind {
  scroll-margin-top: var(--space-5);
}

.kind + .kind {
  margin-top: var(--space-7);
}

.sources {
  list-style: none;
  padding: 0;
}

.source {
  display: grid;
  gap: var(--space-3);
  padding-block: var(--space-4);
  border-bottom: 1px solid var(--rule);
  scroll-margin-top: var(--space-5);
}

.source:last-child {
  border-bottom: 0;
}

@media (min-width: 56rem) {
  .source {
    grid-template-columns: minmax(0, 2fr) minmax(0, 2fr) auto;
    align-items: start;
    column-gap: var(--space-6);
  }
  .source--editing {
    grid-template-columns: minmax(0, 1fr);
  }
}

.source__name {
  font-family: var(--font-display);
  font-size: var(--step-1);
  font-weight: 600;
  line-height: var(--leading-head);
}

.source__detail {
  margin-top: var(--space-1);
  color: var(--ink-3);
  font-size: var(--step--1);
  overflow-wrap: anywhere;
}

.source--muted .source__name,
.source--muted .source__detail {
  color: var(--ink-3);
}

.source__stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1) var(--space-4);
  font-size: var(--step--1);
}

.source__stats div {
  display: flex;
  gap: 0.35rem;
}

.source__stats dt {
  color: var(--ink-3);
}

.source__stats dd {
  margin: 0;
  color: var(--ink);
  font-variant-numeric: tabular-nums;
}

.source__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.source__actions form {
  margin: 0;
}

.source__error {
  grid-column: 1 / -1;
  color: var(--danger);
  font-size: var(--step--1);
  overflow-wrap: anywhere;
}

/* ---------- Settings ---------- */
.settings {
  display: grid;
  gap: var(--space-7);
  max-width: 54rem;
}

.panel {
  display: grid;
  gap: var(--space-5);
  padding-top: var(--space-5);
  border-top: 1px solid var(--rule-strong);
  scroll-margin-top: var(--space-5);
}

@media (min-width: 56rem) {
  .panel {
    grid-template-columns: 13rem minmax(0, 1fr);
    column-gap: var(--space-7);
  }
}

.panel__head h2 {
  font-size: var(--step-2);
  font-style: italic;
}

.panel__head p {
  margin-top: var(--space-2);
  color: var(--ink-2);
  font-size: var(--step--1);
}

.panel__body {
  display: grid;
  gap: var(--space-5);
  min-width: 0;
}

.meter {
  display: grid;
  gap: var(--space-2);
}

.meter__figures {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
  font-variant-numeric: tabular-nums;
}

.meter__spent {
  font-family: var(--font-display);
  font-size: var(--step-3);
  font-weight: 600;
  line-height: 1;
}

.meter__of {
  color: var(--ink-2);
  font-size: var(--step--1);
}

.meter__bar {
  display: block;
  width: 100%;
  height: 10px;
  border-radius: 5px;
  overflow: hidden;
}

.meter__track {
  fill: var(--track);
}

.meter__fill {
  fill: var(--accent);
}

.meter--over .meter__fill {
  fill: var(--danger);
}

.chart-figure {
  display: grid;
  gap: var(--space-2);
}

.chart-caption {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: var(--space-2);
  font-size: var(--step--1);
  font-weight: 600;
}

.chart-caption span + span {
  color: var(--ink-3);
  font-weight: 400;
}

.chart {
  display: block;
  width: 100%;
  height: auto;
  max-height: 12rem;
}

.chart__bar {
  fill: var(--accent);
}

.chart__bar:hover {
  fill: var(--ink);
}

.chart__zero {
  fill: var(--rule-strong);
}

.chart__axis {
  stroke: var(--rule-strong);
  stroke-width: 1;
}

.chart__grid {
  stroke: var(--rule);
  stroke-width: 1;
  stroke-dasharray: 2 3;
}

.chart-axis {
  display: flex;
  justify-content: space-between;
  color: var(--ink-3);
  font-size: var(--step--2);
  font-variant-numeric: tabular-nums;
}

.table {
  width: 100%;
  margin-top: var(--space-3);
  border-collapse: collapse;
  font-size: var(--step--1);
  font-variant-numeric: tabular-nums;
}

.table th,
.table td {
  padding: var(--space-1) var(--space-2);
  border-bottom: 1px solid var(--rule);
  text-align: left;
}

.table td:last-child,
.table th:last-child {
  text-align: right;
}

.key {
  display: grid;
  gap: var(--space-2);
  padding-bottom: var(--space-5);
  border-bottom: 1px solid var(--rule);
}

.key:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}

.key__head {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}

.key__head h3 {
  font-size: var(--step-1);
}

.key__form {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin: 0;
}

.key__form input {
  flex: 1 1 14rem;
}

.runs {
  list-style: none;
  padding: 0;
  display: grid;
  gap: var(--space-2);
  font-size: var(--step--1);
}

.runs li {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1) var(--space-3);
  color: var(--ink-2);
}

.runs .error {
  color: var(--danger);
}

/* ---------- Login ---------- */
.login {
  min-height: 80svh;
  display: grid;
  place-items: center;
}

.login__card {
  width: 100%;
  max-width: 24rem;
  display: grid;
  gap: var(--space-5);
}

.login__title {
  font-size: var(--step-4);
  line-height: var(--leading-tight);
}

/* ---------- Motion ---------- */
@media (prefers-reduced-motion: no-preference) {
  .thumb,
  .button,
  .chip,
  .nav a,
  .switch a,
  .link-button {
    transition:
      color var(--dur) ease-out,
      background-color var(--dur) ease-out,
      border-color var(--dur) ease-out;
  }
  .gauge {
    transition: background var(--dur) ease-out;
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}

/* ---------- Forced colours (Windows high contrast) ---------- */
@media (forced-colors: active) {
  .gauge,
  .wordmark::before {
    forced-color-adjust: none;
    background: linear-gradient(
      to top,
      Highlight 0 calc(var(--signal, 0.68) * 100%),
      CanvasText calc(var(--signal, 0.68) * 100%) 100%
    );
  }
  .thumb[aria-pressed="true"] {
    outline: 2px solid Highlight;
  }
  .meter__fill,
  .chart__bar {
    fill: Highlight;
  }
}
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/web/test_design_system.py tests/web/test_view_helpers.py -q`
Expected: all pass (`test_view_helpers.py` 8, `test_design_system.py` 6).
Run: `uv run pytest tests/web -q && uv run pyright`
Expected: all pass, no type errors.

- [ ] **Step 5: Commit**

```bash
git add src/ai_newsdesk/web tests/web
git commit -m "feat(web): First light design system, base layout and icons (V1-S5)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 28: Today page

**Goal:** The front page: today's stories as "Top" (by score, lead story larger) or "By section" (grouped by category with a section index on wide screens). Each story shows signal, category, time, headline, summary, why-it-matters (only when the toggle is on and the text exists), source chips, thumbs via htmx (through step 4's `feedback.set_feedback`, so source weights move at once), a "Never show stories like this" form that adds a blocklist keyword (step 4's `feedback.add_blocklist_keyword`), and every source link one click away. The order comes from `taste.rank_today`. Items without a verdict stay visible as "Unscored".

**Files:**
- Modify: `src/ai_newsdesk/models.py` (append `UnscoredItem`)
- Modify: `src/ai_newsdesk/store/store.py` (add `stories_by_ids`, `unscored_items`)
- Create: `src/ai_newsdesk/web/routes_today.py`
- Create: `src/ai_newsdesk/web/templates/today.html`
- Create: `src/ai_newsdesk/web/templates/partials/story_card.html`
- Modify: `src/ai_newsdesk/web/app_routes.py`
- Create: `tests/web/seed.py`, `tests/web/test_today.py`, `tests/test_store_web_queries.py`

**Acceptance Criteria:**
- [ ] `GET /` (default `view=top`) lists `today_stories(now, show_threshold)` by score, then `await taste.rank_today(store, stories)` (a no-op until the taste model is fitted); the first is the lead (`story--lead`); `?view=category` groups them in `Category` order with headings "Model releases", "Research", … and a section index; any other `view` value means Top.
- [ ] A card shows "Signal N" (rounded, 0–10) and a matching `data-signal` gauge, category, relative time, headline linked to the lead source, summary (or "The summary is written on the next run."), "Why it matters." only when the setting is on and text exists, up to 4 source chips (icon by kind), thumbs, and "All N sources" when there is more than one.
- [ ] `POST /stories/{id}/feedback` with `value` in {-1, 0, 1} calls `feedback.set_feedback(store, ctx.clock, story_id, value)` (stores the thumb and updates source weights): htmx → 200 with only the updated `<article>` (pressed state shown, lead kept); plain form → 303 to `/#story-{id}`; bad value → 422; unknown story → 404; CSRF required.
- [ ] Each card has a collapsed "Never show stories like this" form (`POST /stories/{id}/block`, field `keyword`) that calls `feedback.add_blocklist_keyword(store, keyword)`: htmx → 200 with the card and "Added to the blocklist" confirmation; plain → 303 to `/?blocked=1#story-{id}` with a notice; empty or over-long keyword → 422 with the error by the field, nothing saved; unknown story → 404; CSRF required.
- [ ] `review_failed`/`queued` items from the last 24 h appear under "Waiting for review" with an "Unscored" marker ("Unscored: budget reached" when the month's budget is spent); the budget banner shows in that case.
- [ ] Fetched and model text is escaped (a `<script>` headline renders as text); every outbound link has `rel="noopener noreferrer"`.
- [ ] Empty day → "Nothing above the bar yet" with links to run now and lower the bar.

**Verify:** `uv run pytest tests/web/test_today.py tests/test_store_web_queries.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the seed helpers (public Store API only)**

`tests/web/seed.py`:

```python
"""Seed data through the public Store API (shared by web tests, store tests and the dev seed)."""

from __future__ import annotations

from datetime import datetime

from ai_newsdesk.models import Candidate, Category, ItemStatus, SourceKind, StoryText, Trust, Verdict
from ai_newsdesk.store import Store

ConfigValue = str | int | float | bool | list[str]


async def add_source(
    store: Store,
    *,
    name: str,
    kind: SourceKind = SourceKind.FEED,
    trust: Trust = Trust.PRIMARY,
    config: dict[str, ConfigValue] | None = None,
    enabled: bool = True,
) -> int:
    slug = "-".join(name.lower().split())
    cfg = config if config is not None else {"url": f"https://{slug}.example/feed.xml"}
    return await store.upsert_source(kind=kind, name=name, config=cfg, trust=trust, enabled=enabled)


async def add_item(
    store: Store,
    *,
    source_id: int,
    url: str,
    title: str,
    at: datetime,
    status: ItemStatus | None = None,
) -> int:
    ids = await store.add_candidates(
        [Candidate(source_id=source_id, url=url, title=title, published_at=at)], at
    )
    assert len(ids) == 1, f"{url} was already stored"
    if status is not None:
        await store.mark_items(ids, status)
    return ids[0]


async def add_story(
    store: Store,
    *,
    at: datetime,
    headline: str,
    sources: list[tuple[int, str, str]],
    category: Category = Category.RESEARCH,
    signal: int = 8,
    summary: str | None = "A short summary.",
    why: str | None = "It matters because it changes the defaults.",
) -> int:
    """A reviewed story with one item per (source_id, url, title)."""
    story_id = await store.create_story(headline, category, at)
    for source_id, url, title in sources:
        item_id = await add_item(store, source_id=source_id, url=url, title=title, at=at)
        await store.save_verdict(
            Verdict(item_id=item_id, signal=signal, junk=False, category=category), float(signal)
        )
        await store.attach_item(item_id, story_id, at)
    await store.recompute_story_score(story_id)
    if summary is not None:
        await store.save_story_text(StoryText(story_id=story_id, summary=summary, why_it_matters=why))
    return story_id
```

- [ ] **Step 2: Write the failing store tests**

`tests/test_store_web_queries.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ai_newsdesk.models import ItemStatus, SourceKind, Trust
from ai_newsdesk.store import Store
from tests.web import seed

NOW = datetime(2026, 10, 14, 8, 30, tzinfo=UTC)


async def test_stories_by_ids_returns_stories_with_their_sources(store: Store) -> None:
    lab = await seed.add_source(store, name="Lab")
    hn = await seed.add_source(store, name="HN", kind=SourceKind.HN, trust=Trust.DISCOVERY, config={})
    sid = await seed.add_story(
        store,
        at=NOW,
        headline="Lab ships a model",
        sources=[(lab, "https://lab.example/post", "Lab post"), (hn, "https://news.example/1", "HN thread")],
        signal=7,
        summary="It is faster.",
        why="Cheaper defaults.",
    )
    got = await store.stories_by_ids([sid, 999_999])
    assert list(got) == [sid]
    story = got[sid]
    assert story.headline == "Lab ships a model"
    assert story.summary == "It is faster."
    assert story.why_it_matters == "Cheaper defaults."
    assert story.feedback == 0
    assert {s.source_name for s in story.sources} == {"Lab", "HN"}
    assert {s.url for s in story.sources} == {"https://lab.example/post", "https://news.example/1"}


async def test_stories_by_ids_with_no_ids(store: Store) -> None:
    assert await store.stories_by_ids([]) == {}


async def test_unscored_items_are_recent_failed_or_queued(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    failed = await seed.add_item(store, source_id=src, url="https://lab.example/1", title="Failed", at=NOW, status=ItemStatus.REVIEW_FAILED)
    queued = await seed.add_item(store, source_id=src, url="https://lab.example/2", title="Queued", at=NOW, status=ItemStatus.QUEUED)
    await seed.add_item(store, source_id=src, url="https://lab.example/3", title="Reviewed", at=NOW, status=ItemStatus.REVIEWED)
    await seed.add_item(
        store, source_id=src, url="https://lab.example/4", title="Old", at=NOW - timedelta(hours=30), status=ItemStatus.REVIEW_FAILED
    )
    got = await store.unscored_items(NOW)
    assert {u.item_id for u in got} == {failed, queued}
    assert {u.status for u in got} == {ItemStatus.REVIEW_FAILED, ItemStatus.QUEUED}
    assert all(u.source_name == "Lab" and u.source_kind == SourceKind.FEED for u in got)
    assert len(await store.unscored_items(NOW, limit=1)) == 1
```

- [ ] **Step 3: Write the failing page tests**

`tests/web/test_today.py`:

```python
from __future__ import annotations

from datetime import timedelta
from html.parser import HTMLParser
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.models import Category, ItemStatus, Purpose, Usage
from ai_newsdesk.runtime_settings import update_runtime_settings
from tests.web import seed
from tests.web.support import HX, NOW, csrf_token, run

AT = NOW - timedelta(hours=2)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            a = dict(attrs)
            self.links.append((a.get("href") or "", a.get("rel") or ""))


def _source(client: TestClient, app: FastAPI, name: str = "Example Lab") -> int:
    return run(client, seed.add_source, app.state.store, name=name)


def _story(client: TestClient, app: FastAPI, src: int, headline: str, url: str, **kw: Any) -> int:
    return run(
        client, seed.add_story, app.state.store, at=AT, headline=headline, sources=[(src, url, headline)], **kw
    )


def _runtime(client: TestClient, app: FastAPI, **changes: Any) -> None:
    run(client, update_runtime_settings, app.state.store, **changes)


def test_today_shows_the_story_card(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(
        client, app, src, "Example Lab ships a new model", "https://lab.example/news/model",
        signal=8, summary="The model is faster.", why="It changes default choices.",
    )
    r = client.get("/")
    assert r.status_code == 200
    for text in (
        "Wednesday 14 October",
        "Example Lab ships a new model",
        "The model is faster.",
        "Why it matters.",
        "It changes default choices.",
        "Signal 8",
        'data-signal="8"',
        "2 h ago",
        "Example Lab",
        'href="/static/icons.svg#i-rss"',
        'aria-current="page">Today',
    ):
        assert text in r.text, text


def test_why_it_matters_is_hidden_when_switched_off(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(client, app, src, "A story", "https://lab.example/a", why="Hidden reason.")
    _runtime(client, app, why_it_matters=False)
    r = client.get("/")
    assert "A story" in r.text
    assert "Hidden reason." not in r.text
    assert "Why it matters." not in r.text


def test_top_view_orders_by_score_with_a_lead(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    second = _story(client, app, src, "Second best", "https://lab.example/2", signal=6)
    first = _story(client, app, src, "Best of the day", "https://lab.example/1", signal=9)
    r = client.get("/?view=nonsense")
    assert r.text.index("Best of the day") < r.text.index("Second best")
    assert r.text.count("story--lead") == 1
    assert f'class="story story--lead" id="story-{first}"' in r.text
    assert f'class="story" id="story-{second}"' in r.text


def test_section_view_groups_by_category(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(client, app, src, "A research result", "https://lab.example/r", category=Category.RESEARCH, signal=9)
    _story(client, app, src, "A model release", "https://lab.example/m", category=Category.MODEL_RELEASE, signal=6)
    r = client.get("/?view=category")
    text = r.text
    assert text.index(">Model releases</h2>") < text.index(">Research</h2>")
    assert text.index("A model release") < text.index("A research result")
    assert 'class="index"' in text
    assert 'href="#section-model_release"' in text
    assert "story--lead" not in text


def test_untrusted_text_is_escaped(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    _story(
        client, app, src, "<script>alert(1)</script> Model", "https://lab.example/x",
        summary="<img src=x onerror=alert(2)>", why="<b>bold</b>",
    )
    r = client.get("/")
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Model" in r.text
    assert "<script>alert(1)" not in r.text
    assert "<img src=x" not in r.text
    assert "<b>bold</b>" not in r.text


def test_outbound_links_get_noopener_noreferrer(app: FastAPI, client: TestClient) -> None:
    lab = _source(client, app, "Lab")
    blog = _source(client, app, "Blog")
    run(
        client, seed.add_story, app.state.store, at=AT, headline="Two sources",
        sources=[(lab, "https://lab.example/p", "Lab post"), (blog, "https://blog.example/p", "Blog post")],
    )
    parser = _Links()
    parser.feed(client.get("/").text)
    outbound = [(href, rel) for href, rel in parser.links if href.startswith("http")]
    assert len(outbound) >= 5  # headline, 2 chips, 2 links in "All 2 sources"
    for href, rel in outbound:
        assert {"noopener", "noreferrer"} <= set(rel.split()), href


def test_every_source_is_one_click_away(app: FastAPI, client: TestClient) -> None:
    lab = _source(client, app, "Lab")
    blog = _source(client, app, "Blog")
    run(
        client, seed.add_story, app.state.store, at=AT, headline="Two sources",
        sources=[(lab, "https://lab.example/p", "Lab post"), (blog, "https://blog.example/p", "Blog post")],
    )
    r = client.get("/")
    assert "All 2 sources" in r.text
    assert "Lab post" in r.text and "Blog post" in r.text


def test_thumbs_via_htmx_return_the_updated_card(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Thumb me", "https://lab.example/t")
    headers = {**HX, "X-CSRF-Token": csrf_token(client)}
    r = client.post(f"/stories/{sid}/feedback", data={"value": "1", "lead": "1"}, headers=headers)
    assert r.status_code == 200
    assert r.text.lstrip().startswith(f'<article class="story story--lead" id="story-{sid}"')
    assert 'aria-pressed="true"' in r.text
    assert "<html" not in r.text
    assert run(client, app.state.store.stories_by_ids, [sid])[sid].feedback == 1
    r = client.post(f"/stories/{sid}/feedback", data={"value": "0"}, headers=headers)
    assert r.text.lstrip().startswith(f'<article class="story" id="story-{sid}"')
    assert 'aria-pressed="true"' not in r.text
    assert run(client, app.state.store.stories_by_ids, [sid])[sid].feedback == 0


def test_thumbs_without_htmx_redirect_back(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Thumb me", "https://lab.example/t")
    r = client.post(
        f"/stories/{sid}/feedback",
        data={"value": "-1", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/#story-{sid}"
    assert run(client, app.state.store.stories_by_ids, [sid])[sid].feedback == -1


def test_thumbs_go_through_step_4_and_move_source_weights(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = _source(client, app)
    sid = _story(client, app, src, "Thumb me", "https://lab.example/t")
    before = run(client, store.get_source, src)
    assert before is not None
    client.post(f"/stories/{sid}/feedback", data={"value": "1"}, headers={**HX, "X-CSRF-Token": csrf_token(client)})
    after = run(client, store.get_source, src)
    assert after is not None and after.weight > before.weight


def test_never_show_stories_like_this_adds_a_blocklist_keyword(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = _source(client, app)
    sid = _story(client, app, src, "Crypto token launches an AI agent", "https://lab.example/c")
    page = client.get("/").text
    assert "Never show stories like this" in page
    assert f'action="/stories/{sid}/block"' in page
    r = client.post(
        f"/stories/{sid}/block",
        data={"keyword": "  Crypto   Token "},
        headers={**HX, "X-CSRF-Token": csrf_token(client)},
    )
    assert r.status_code == 200
    assert r.text.lstrip().startswith(f'<article class="story" id="story-{sid}"')
    assert "Added to the blocklist" in r.text
    assert run(client, store.get_setting, "blocklist") == ["crypto token"]
    r = client.post(
        f"/stories/{sid}/block",
        data={"keyword": "agents", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/?blocked=1#story-{sid}"
    assert "Added to the blocklist." in client.get(r.headers["location"]).text
    assert run(client, store.get_setting, "blocklist") == ["crypto token", "agents"]


def test_blocklist_rejects_bad_input(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Block me", "https://lab.example/b")
    headers = {**HX, "X-CSRF-Token": csrf_token(client)}
    r = client.post(f"/stories/{sid}/block", data={"keyword": "   "}, headers=headers)
    assert r.status_code == 422
    assert "Enter a short word or phrase" in r.text
    assert client.post(f"/stories/{sid}/block", data={"keyword": "x" * 61}, headers=headers).status_code == 422
    assert run(client, app.state.store.get_setting, "blocklist") is None
    assert client.post("/stories/999999/block", data={"keyword": "x"}, headers=headers).status_code == 404
    assert client.post(f"/stories/{sid}/block", data={"keyword": "x"}, headers=HX).status_code == 403


def test_feedback_rejects_bad_input(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    sid = _story(client, app, src, "Thumb me", "https://lab.example/t")
    headers = {**HX, "X-CSRF-Token": csrf_token(client)}
    assert client.post(f"/stories/{sid}/feedback", data={"value": "5"}, headers=headers).status_code == 422
    assert client.post("/stories/999999/feedback", data={"value": "1"}, headers=headers).status_code == 404
    assert client.post(f"/stories/{sid}/feedback", data={"value": "1"}, headers=HX).status_code == 403


def test_empty_day(client: TestClient) -> None:
    r = client.get("/")
    assert "Nothing above the bar yet" in r.text
    assert 'href="/settings#run"' in r.text


def test_unscored_items_stay_visible(app: FastAPI, client: TestClient) -> None:
    src = _source(client, app)
    run(
        client, seed.add_item, app.state.store, source_id=src, url="https://lab.example/u",
        title="Not reviewed <yet>", at=AT, status=ItemStatus.REVIEW_FAILED,
    )
    r = client.get("/")
    assert "Waiting for review" in r.text
    assert ">Unscored</span>" in r.text
    assert "Not reviewed &lt;yet&gt;" in r.text
    assert "Nothing above the bar yet" not in r.text


def test_budget_reached_shows_banner_and_marker(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    _runtime(client, app, monthly_budget_usd=0.01)
    usage = Usage(provider="anthropic", model="claude-haiku-5-5", purpose=Purpose.REVIEW, input_tokens=100)
    run(client, store.add_spend, usage, 0.02, NOW - timedelta(hours=1))
    src = _source(client, app)
    run(
        client, seed.add_item, store, source_id=src, url="https://lab.example/q",
        title="Queued item", at=AT, status=ItemStatus.QUEUED,
    )
    r = client.get("/")
    assert "budget is spent" in r.text
    assert "Unscored: budget reached" in r.text
```

- [ ] **Step 4: Run the tests to see them fail**

Run: `uv run pytest tests/web/test_today.py tests/test_store_web_queries.py -q`
Expected: FAIL: `AttributeError: 'Store' object has no attribute 'stories_by_ids'` in the store tests, and `GET /` returns 404 in the page tests.

- [ ] **Step 5: Implement**

Append to `src/ai_newsdesk/models.py`:

```python
class UnscoredItem(BaseModel):
    """An item with no verdict (review failed, or still queued); never hidden from the owner."""

    item_id: int
    source_name: str
    source_kind: SourceKind
    url: str
    title: str
    published_at: datetime | None = None
    status: ItemStatus
```

In `src/ai_newsdesk/store/store.py`: add `UnscoredItem` to the `ai_newsdesk.models` import (`Category`, `ItemStatus`, `SourceKind`, `Story` and `StorySource` are already there), and add these methods to `class Store`. They use step 1's locked helpers (`self._all`) and timestamp helpers (`_ts`, `_dt`, `_req_dt`), never `self._db.execute` directly:

```python
    # --- web queries (step 5) -------------------------------------------------------------

    async def stories_by_ids(self, ids: list[int]) -> dict[int, Story]:
        """Stories by ID with their sources, lead (highest signal, then lowest id) first."""
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = await self._all(
            "SELECT id, headline, category, score, summary, why_it_matters, first_seen_at,"
            f" last_updated_at, feedback FROM stories WHERE id IN ({marks})",  # noqa: S608
            ids,
        )
        stories: dict[int, Story] = {
            int(r["id"]): Story(
                id=int(r["id"]),
                headline=r["headline"],
                category=Category(r["category"]),
                score=float(r["score"] or 0.0),
                summary=r["summary"],
                why_it_matters=r["why_it_matters"],
                first_seen_at=_req_dt(r["first_seen_at"]),
                last_updated_at=_req_dt(r["last_updated_at"]),
                feedback=int(r["feedback"] or 0),
            )
            for r in rows
        }
        items = await self._all(
            "SELECT i.id AS item_id, i.story_id, i.url, i.title, i.published_at,"
            " s.name AS source_name, s.kind AS source_kind"
            " FROM items i JOIN sources s ON s.id = i.source_id"
            f" WHERE i.story_id IN ({marks})"  # noqa: S608
            " ORDER BY i.story_id, COALESCE(i.signal, -1) DESC, i.id",
            ids,
        )
        for r in items:
            story = stories.get(int(r["story_id"]))
            if story is not None:
                story.sources.append(
                    StorySource(
                        item_id=int(r["item_id"]),
                        source_name=r["source_name"],
                        source_kind=SourceKind(r["source_kind"]),
                        url=r["url"],
                        title=r["title"],
                        published_at=_dt(r["published_at"]),
                    )
                )
        return stories

    async def unscored_items(
        self, now: dt.datetime, hours: int = 24, limit: int = 30
    ) -> list[UnscoredItem]:
        """Recent items with no verdict, best heuristic score first."""
        rows = await self._all(
            "SELECT i.id AS item_id, i.url, i.title, i.published_at, i.status,"
            " s.name AS source_name, s.kind AS source_kind"
            " FROM items i JOIN sources s ON s.id = i.source_id"
            " WHERE i.status IN ('review_failed', 'queued') AND i.fetched_at >= ?"
            " ORDER BY i.heuristic_score DESC, i.fetched_at DESC, i.id DESC LIMIT ?",
            (_ts(now - dt.timedelta(hours=hours)), limit),
        )
        return [
            UnscoredItem(
                item_id=int(r["item_id"]),
                source_name=r["source_name"],
                source_kind=SourceKind(r["source_kind"]),
                url=r["url"],
                title=r["title"],
                published_at=_dt(r["published_at"]),
                status=ItemStatus(r["status"]),
            )
            for r in rows
        ]
```

(The f-strings only splice `?` placeholders, never values; hence the targeted `# noqa: S608`.)

`src/ai_newsdesk/web/routes_today.py`:

```python
"""Today: the front page, thumbs on a story, and "never show stories like this"."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_newsdesk import feedback, taste
from ai_newsdesk.models import Category, Story
from ai_newsdesk.runtime_settings import load_runtime_settings
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import category_label, is_htmx, now_of, page, partial, store_of, zone_of

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])

BLOCK_ERROR = "Enter a short word or phrase (up to 60 characters)."


@dataclass(frozen=True)
class Group:
    category: Category
    label: str
    slug: str
    stories: list[Story]


def group_by_category(stories: list[Story]) -> list[Group]:
    groups: list[Group] = []
    for category in Category:
        members = [s for s in stories if s.category == category]
        if members:
            groups.append(Group(category, category_label(category), f"section-{category.value}", members))
    return groups


@router.get("/", response_class=HTMLResponse)
async def today(request: Request, view: str = "top", blocked: int = 0) -> HTMLResponse:
    store = store_of(request)
    now = now_of(request)
    runtime = await load_runtime_settings(store)
    by_score = sorted(
        await store.today_stories(now, runtime.show_threshold),
        key=lambda s: (-s.score, -s.last_updated_at.timestamp()),
    )
    # Step 4's taste model nudges scores by up to +-2 once it has enough thumbs (else a no-op).
    stories = await taste.rank_today(store, by_score)
    return await page(
        request,
        "today.html",
        {
            "active": "today",
            "view": "category" if view == "category" else "top",
            "stories": stories,
            "groups": group_by_category(stories),
            "unscored": await store.unscored_items(now),
            "source_count": len({src.source_name for s in stories for src in s.sources}),
            "local_today": now.astimezone(zone_of(request)).date(),
            "show_why": runtime.why_it_matters,
            "blocked": blocked == 1,
        },
    )


async def _story(request: Request, story_id: int) -> Story:
    story = (await store_of(request).stories_by_ids([story_id])).get(story_id)
    if story is None:
        raise HTTPException(status_code=404, detail="There is no such story.")
    return story


async def _card(
    request: Request,
    story_id: int,
    *,
    lead: bool,
    blocked: str | None = None,
    block_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    story = await _story(request, story_id)
    runtime = await load_runtime_settings(store_of(request))
    return partial(
        request,
        "partials/story_card.html",
        {
            "story": story,
            "show_why": runtime.why_it_matters,
            "lead": lead,
            "blocked": blocked,
            "block_error": block_error,
        },
        status_code=status_code,
    )


@router.post("/stories/{story_id}/feedback")
async def thumbs(
    request: Request,
    story_id: int,
    value: Annotated[int, Form()],
    lead: Annotated[int, Form()] = 0,
) -> Response:
    if value not in (-1, 0, 1):
        raise HTTPException(status_code=422, detail="Feedback must be -1, 0 or 1.")
    await _story(request, story_id)
    # Step 4 (R5): stores the thumb and moves the source weights straight away.
    await feedback.set_feedback(store_of(request), request.app.state.ctx.clock, story_id, value)
    if not is_htmx(request):
        return RedirectResponse(f"/#story-{story_id}", status_code=303)
    return await _card(request, story_id, lead=lead == 1)


@router.post("/stories/{story_id}/block")
async def never_show_like_this(
    request: Request,
    story_id: int,
    keyword: Annotated[str, Form()] = "",
    lead: Annotated[int, Form()] = 0,
) -> Response:
    await _story(request, story_id)
    try:
        # Step 4 (R5): lower-cases, collapses spaces, validates, and appends to the `blocklist`
        # setting that step 3's pre-filter reads on the next run.
        words = await feedback.add_blocklist_keyword(store_of(request), keyword)
    except ValueError:
        if not is_htmx(request):
            raise HTTPException(status_code=422, detail=BLOCK_ERROR) from None
        return await _card(request, story_id, lead=lead == 1, block_error=BLOCK_ERROR, status_code=422)
    if not is_htmx(request):
        return RedirectResponse(f"/?blocked=1#story-{story_id}", status_code=303)
    added = " ".join(keyword.lower().split())
    return await _card(request, story_id, lead=lead == 1, blocked=added if added in words else None)
```

`src/ai_newsdesk/web/templates/partials/story_card.html`:

```html
{% set signal = signal_of(story.score) %}
{% set lead_src = story.sources[0] if story.sources else none %}
<article class="story{% if lead %} story--lead{% endif %}" id="story-{{ story.id }}" data-signal="{{ signal }}" aria-labelledby="story-{{ story.id }}-title">
  <span class="gauge" aria-hidden="true"></span>
  <div class="story__body">
    <p class="story__meta">
      <span class="badge">Signal {{ signal }}</span>
      <span>{{ category_label(story.category) }}</span>
      <time datetime="{{ story.last_updated_at.isoformat() }}">{{ ago(story.last_updated_at, now) }}</time>
    </p>
    <h3 class="story__headline" id="story-{{ story.id }}-title">
      {% if lead_src %}<a href="{{ lead_src.url|safe_url }}" rel="noopener noreferrer" target="_blank">{{ story.headline }}</a>{% else %}{{ story.headline }}{% endif %}
    </h3>
    {% if story.summary %}
    <p class="story__summary">{{ story.summary }}</p>
    {% else %}
    <p class="story__summary story__summary--pending">The summary is written on the next run.</p>
    {% endif %}
    {% if show_why and story.why_it_matters %}
    <p class="story__why"><strong>Why it matters.</strong> {{ story.why_it_matters }}</p>
    {% endif %}
    <div class="story__foot">
      <ul class="chips" aria-label="Sources">
        {% for src in unique_sources(story.sources)[:4] %}
        {% set icon = icon_for(src.source_kind) %}
        <li><a class="chip" href="{{ src.url|safe_url }}" rel="noopener noreferrer" target="_blank">{% if icon %}<svg class="icon" aria-hidden="true"><use href="/static/icons.svg#{{ icon }}"></use></svg>{% endif %}<span>{{ src.source_name }}</span></a></li>
        {% endfor %}
      </ul>
      <form class="thumbs" method="post" action="/stories/{{ story.id }}/feedback" hx-post="/stories/{{ story.id }}/feedback" hx-target="#story-{{ story.id }}" hx-swap="outerHTML">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        {% if lead %}
        <input type="hidden" name="lead" value="1">
        {% endif %}
        <button class="thumb" type="submit" name="value" value="{{ 0 if story.feedback == 1 else 1 }}" aria-pressed="{{ 'true' if story.feedback == 1 else 'false' }}" title="More like this">
          <svg class="icon" aria-hidden="true"><use href="/static/icons.svg#i-thumbs-up"></use></svg><span class="visually-hidden">More like this</span>
        </button>
        <button class="thumb" type="submit" name="value" value="{{ 0 if story.feedback == -1 else -1 }}" aria-pressed="{{ 'true' if story.feedback == -1 else 'false' }}" title="Less like this">
          <svg class="icon" aria-hidden="true"><use href="/static/icons.svg#i-thumbs-down"></use></svg><span class="visually-hidden">Less like this</span>
        </button>
      </form>
    </div>
    {% if story.sources|length > 1 %}
    <details class="more">
      <summary>All {{ story.sources|length }} sources</summary>
      <ul>
        {% for src in story.sources %}
        <li><a href="{{ src.url|safe_url }}" rel="noopener noreferrer" target="_blank">{{ src.title }}</a><span class="src">{{ src.source_name }}{% if src.published_at %}, {{ ago(src.published_at, now) }}{% endif %}</span></li>
        {% endfor %}
      </ul>
    </details>
    {% endif %}
    {% if blocked %}
    <p class="story__note" role="status">Added to the blocklist: new items mentioning “{{ blocked }}” are dropped from the next run.</p>
    {% endif %}
    <details class="more block"{% if block_error %} open{% endif %}>
      <summary>Never show stories like this</summary>
      <form class="block__form" method="post" action="/stories/{{ story.id }}/block" hx-post="/stories/{{ story.id }}/block" hx-target="#story-{{ story.id }}" hx-swap="outerHTML">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        {% if lead %}
        <input type="hidden" name="lead" value="1">
        {% endif %}
        <label for="block-{{ story.id }}">Word or phrase to block</label>
        <input id="block-{{ story.id }}" name="keyword" type="text" maxlength="60" required autocomplete="off" aria-describedby="block-{{ story.id }}-hint{% if block_error %} block-{{ story.id }}-error{% endif %}"{% if block_error %} aria-invalid="true"{% endif %}>
        <p class="field__hint" id="block-{{ story.id }}-hint">New items whose title or snippet contains it are dropped before review.</p>
        {% if block_error %}<p class="field__error" id="block-{{ story.id }}-error" role="alert">{{ block_error }}</p>{% endif %}
        <button class="button button--quiet button--small" type="submit">Block</button>
      </form>
    </details>
  </div>
</article>
```

`src/ai_newsdesk/web/templates/today.html`:

```html
{% extends "base.html" %}
{% block title %}Today on Newsdesk{% endblock %}
{% block content %}
<header class="page-head">
  <h1><time datetime="{{ local_today.isoformat() }}">{{ day_label(local_today) }}</time></h1>
  {% if stories %}
  <p class="lede">{{ stories|length }} {{ "story" if stories|length == 1 else "stories" }} worth your time from the last 24 hours, found by {{ source_count }} {{ "source" if source_count == 1 else "sources" }}.</p>
  {% endif %}
  <div class="toolbar">
    <nav class="switch" aria-label="Arrange stories">
      <a href="/?view=top"{% if view == "top" %} aria-current="true"{% endif %}>Top</a>
      <a href="/?view=category"{% if view == "category" %} aria-current="true"{% endif %}>By section</a>
    </nav>
  </div>
</header>
{% if blocked %}
<div class="notice notice--ok" role="status"><p>Added to the blocklist. New items that mention it are dropped from the next run.</p></div>
{% endif %}
<div class="today{% if view == 'category' and groups %} today--indexed{% endif %}">
  {% if view == "category" and groups %}
  <nav class="index" aria-label="Sections">
    <ul>
      {% for group in groups %}
      <li><a href="#{{ group.slug }}"><span>{{ group.label }}</span><span class="count">{{ group.stories|length }}</span></a></li>
      {% endfor %}
    </ul>
  </nav>
  {% endif %}
  <div class="stream">
    {% if not stories and not unscored %}
    <section class="empty">
      <h2>Nothing above the bar yet</h2>
      <p>The desk checks your sources {{ runtime.runs_per_day }} {{ "time" if runtime.runs_per_day == 1 else "times" }} a day and shows stories that score {{ runtime.show_threshold }} or more. <a href="/settings#run">Run it now</a>, or <a href="/settings#reading">lower the bar</a>.</p>
    </section>
    {% elif view == "category" %}
    {% for group in groups %}
    <section class="section" id="{{ group.slug }}" aria-labelledby="{{ group.slug }}-title">
      <div class="section__head">
        <h2 id="{{ group.slug }}-title">{{ group.label }}</h2>
        <span class="section__count">{{ group.stories|length }}</span>
      </div>
      {% for story in group.stories %}
      {% with lead = false %}{% include "partials/story_card.html" %}{% endwith %}
      {% endfor %}
    </section>
    {% endfor %}
    {% elif stories %}
    <section aria-labelledby="top-title">
      <h2 id="top-title" class="visually-hidden">Top stories</h2>
      {% for story in stories %}
      {% with lead = loop.first %}{% include "partials/story_card.html" %}{% endwith %}
      {% endfor %}
    </section>
    {% endif %}
    {% if unscored %}
    <section class="section" id="unscored" aria-labelledby="unscored-title">
      <div class="section__head">
        <h2 id="unscored-title">Waiting for review</h2>
        <span class="section__count">{{ unscored|length }}</span>
      </div>
      <p class="section__note">{% if budget_reached %}This month's budget is spent, so these items were not reviewed. They are listed by source rank only.{% else %}These items could not be reviewed yet. The next run tries again.{% endif %}</p>
      <ul class="unscored">
        {% for item in unscored %}
        <li>
          <p class="story__meta"><span class="badge badge--unscored">Unscored{% if budget_reached %}: budget reached{% endif %}</span><span>{{ item.source_name }}</span>{% if item.published_at %}<time datetime="{{ item.published_at.isoformat() }}">{{ ago(item.published_at, now) }}</time>{% endif %}</p>
          <p class="unscored__title"><a href="{{ item.url|safe_url }}" rel="noopener noreferrer" target="_blank">{{ item.title }}</a></p>
        </li>
        {% endfor %}
      </ul>
    </section>
    {% endif %}
  </div>
</div>
{% endblock %}
```

`src/ai_newsdesk/web/app_routes.py`: change the import and the tuple to:

```python
from ai_newsdesk.web import routes_health, routes_today


def build_router() -> APIRouter:
    router = APIRouter()
    for sub in (routes_health.router, routes_today.router):
        router.include_router(sub)
    return router
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `uv run pytest tests/web tests/test_store_web_queries.py -q`
Expected: all pass, including the route-policy and design-system tests, which now also cover `/`, `/stories/{story_id}/feedback` and `/stories/{story_id}/block`.
Run: `uv run ruff check && uv run ruff format --check && uv run pyright`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/ai_newsdesk/models.py src/ai_newsdesk/store/store.py src/ai_newsdesk/web tests/web tests/test_store_web_queries.py
git commit -m "feat(web): Today page with signal gauge, sources and thumbs (V1-S5)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 29: Briefing page

**Goal:** The morning briefing as a long-form read in the book serif, with an archive by date. The body is model text in step 3's "markdown-lite" format (master plan R4), so the page escapes everything and renders only three constructs: `## ` headings, blank-line paragraphs and `[text](url)` links, the last only when the URL belongs to one of the briefing's own stories.

**Files:**
- Create: `src/ai_newsdesk/web/briefing_text.py`
- Create: `src/ai_newsdesk/web/routes_briefing.py`
- Create: `src/ai_newsdesk/web/templates/briefing.html`
- Modify: `src/ai_newsdesk/web/app_routes.py`
- Create: `tests/web/test_briefing_text.py`, `tests/web/test_briefing.py`

**Acceptance Criteria:**
- [ ] `GET /briefing` shows the latest briefing: date as the title, when and by which model it was written, each line starting `## ` as a heading, blank-line-separated text as paragraphs (single newlines folded into spaces).
- [ ] `[text](url)` renders as a link (`class="ref"`, `rel="noopener noreferrer"`, `target="_blank"`) only when `url` is one of the briefing's story URLs (every item URL of `briefing.story_ids`) and passes `safe_url`; any other link shows as its plain text; links inside headings show as text.
- [ ] Any HTML in the body is shown as text (escaped), never interpreted; nothing in the page is marked safe.
- [ ] The archive lists every briefing date, newest first, links to `/briefing/YYYY-MM-DD`, and marks the open one with `aria-current`.
- [ ] `/briefing/<date>` with an unknown, impossible or malformed date → 404 page "No briefing for that day"; no briefings at all → "No briefing yet" with the configured briefing time.

**Verify:** `uv run pytest tests/web/test_briefing_text.py tests/web/test_briefing.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

`tests/web/test_briefing_text.py`:

```python
from __future__ import annotations

from ai_newsdesk.web.briefing_text import Block, Segment, parse_briefing

LAB = "https://lab.example/model"
PAPER = "https://paper.example/abs/1"


def test_headings_and_paragraphs() -> None:
    body = "## Model releases\n\nFirst line\nsame paragraph.\n\n\n## Research\nA heading with text under it."
    assert parse_briefing(body, set()) == [
        Block("heading", (Segment(text="Model releases"),)),
        Block("para", (Segment(text="First line same paragraph."),)),
        Block("heading", (Segment(text="Research"),)),
        Block("para", (Segment(text="A heading with text under it."),)),
    ]


def test_links_to_story_urls_become_link_segments() -> None:
    [block] = parse_briefing(f"Lab A [shipped a model]({LAB}) and a [paper]({PAPER}) followed.", {LAB, PAPER})
    assert block.segments == (
        Segment(text="Lab A "),
        Segment(text="shipped a model", url=LAB),
        Segment(text=" and a "),
        Segment(text="paper", url=PAPER),
        Segment(text=" followed."),
    )


def test_other_links_become_plain_text() -> None:
    body = f"See [this](https://evil.example/x) and [that](javascript:void) and [ok]({LAB})."
    [block] = parse_briefing(body, {LAB})
    assert block.segments == (
        Segment(text="See this and that and "),
        Segment(text="ok", url=LAB),
        Segment(text="."),
    )


def test_unsafe_urls_never_link_even_if_listed() -> None:
    [block] = parse_briefing("[x](javascript:void)", {"javascript:void"})
    assert block.segments == (Segment(text="x"),)


def test_links_in_headings_and_html_stay_text() -> None:
    blocks = parse_briefing(f"## Policy [news]({LAB})\r\n\r\n<b>raw</b>", {LAB})
    assert blocks == [
        Block("heading", (Segment(text="Policy news"),)),
        Block("para", (Segment(text="<b>raw</b>"),)),
    ]


def test_empty_body() -> None:
    assert parse_briefing("  \n\n ", set()) == []
```

`tests/web/test_briefing.py`:

```python
from __future__ import annotations

from datetime import date, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.web import seed
from tests.web.support import NOW, run


def _briefing(client: TestClient, app: FastAPI, day: date, body: str, story_ids: list[int]) -> None:
    run(client, app.state.store.save_briefing, day, "claude-haiku-5-5", body, story_ids, NOW)


def test_latest_briefing_renders_headings_paragraphs_and_story_links(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Example Lab")
    sid = run(
        client, seed.add_story, store, at=NOW - timedelta(hours=3), headline="Example Lab ships a model",
        sources=[(src, "https://lab.example/model", "Model post")],
    )
    body = (
        "## Model releases\n\n"
        "Example Lab [shipped a model](https://lab.example/model). "
        "A [stray link](https://evil.example/x) stays text.\n\n"
        "<b>not bold</b> stays text."
    )
    _briefing(client, app, date(2026, 10, 14), body, [sid])
    r = client.get("/briefing")
    assert r.status_code == 200
    text = r.text
    assert "Wednesday 14 October" in text
    assert "<h2>Model releases</h2>" in text
    assert '<a class="ref" href="https://lab.example/model" rel="noopener noreferrer" target="_blank">shipped a model</a>' in text
    assert "evil.example" not in text
    assert "A stray link stays text." in text
    assert "](" not in text
    assert "&lt;b&gt;not bold&lt;/b&gt; stays text." in text
    assert "<b>not bold" not in text
    assert "claude-haiku-5-5" in text
    assert 'aria-current="page">Briefing' in text


def test_archive_lists_dates_and_opens_older_briefings(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app, date(2026, 10, 13), "Tuesday news.", [])
    _briefing(client, app, date(2026, 10, 14), "Wednesday news.", [])
    latest = client.get("/briefing").text
    assert "Wednesday news." in latest
    assert 'href="/briefing/2026-10-13">Tue 13 Oct</a>' in latest
    assert 'href="/briefing/2026-10-14" aria-current="page">Wed 14 Oct</a>' in latest
    older = client.get("/briefing/2026-10-13")
    assert older.status_code == 200
    assert "Tuesday news." in older.text
    assert "Wednesday news." not in older.text
    assert 'href="/briefing/2026-10-13" aria-current="page"' in older.text


def test_unknown_impossible_or_malformed_dates_are_404(client: TestClient) -> None:
    for path in ("/briefing/2026-10-01", "/briefing/2026-02-30", "/briefing/latest", "/briefing/20261014"):
        r = client.get(path)
        assert r.status_code == 404, path
        assert "No briefing for that day" in r.text


def test_no_briefing_yet(client: TestClient) -> None:
    r = client.get("/briefing")
    assert r.status_code == 200
    assert "No briefing yet" in r.text
    assert "07:00" in r.text
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/web/test_briefing_text.py tests/web/test_briefing.py -q`
Expected: FAIL: `ModuleNotFoundError: No module named 'ai_newsdesk.web.briefing_text'`.

- [ ] **Step 3: Implement**

`src/ai_newsdesk/web/briefing_text.py`:

```python
"""Turns a stored briefing body (step 3's "markdown-lite") into blocks for the template.

Format (master plan R4): plain text; a line starting "## " is a section heading; blank lines
separate paragraphs; links are [text](url). Nothing here produces HTML: the template renders
every piece with autoescape. A link survives only if its URL is one of the briefing's story
URLs (passed in) and is a plain http(s) URL; anything else becomes its text.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from ai_newsdesk.web.deps import safe_url

# The same pattern as step 3's briefing.clean_briefing, so both sides agree on what a link is.
LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")


@dataclass(frozen=True)
class Segment:
    text: str = ""
    url: str | None = None


@dataclass(frozen=True)
class Block:
    kind: Literal["heading", "para"]
    segments: tuple[Segment, ...]


def _segments(text: str, allowed: Collection[str]) -> tuple[Segment, ...]:
    """Split one flattened line into text and link segments; adjacent text is merged."""
    out: list[Segment] = []

    def add_text(piece: str) -> None:
        if not piece:
            return
        if out and out[-1].url is None:
            out[-1] = Segment(text=out[-1].text + piece)
        else:
            out.append(Segment(text=piece))

    pos = 0
    for match in LINK_RE.finditer(text):
        add_text(text[pos : match.start()])
        label, url = match.group(1), match.group(2)
        if url in allowed and safe_url(url) == url:
            out.append(Segment(text=label, url=url))
        else:
            add_text(label)
        pos = match.end()
    add_text(text[pos:])
    return tuple(out)


def _flat(lines: list[str]) -> str:
    return " ".join(" ".join(lines).split())


def parse_briefing(body: str, allowed_urls: Collection[str]) -> list[Block]:
    blocks: list[Block] = []
    for chunk in _BLANK_LINE.split(body.replace("\r\n", "\n").replace("\r", "\n").strip()):
        para: list[str] = []
        for line in chunk.split("\n"):
            if line.startswith("## "):
                if para:
                    blocks.append(Block("para", _segments(_flat(para), allowed_urls)))
                    para = []
                # Headings are plain text: a link inside one keeps only its label.
                heading = _flat([LINK_RE.sub(r"\1", line[3:])])
                if heading:
                    blocks.append(Block("heading", (Segment(text=heading),)))
            elif line.strip():
                para.append(line)
        if para:
            blocks.append(Block("para", _segments(_flat(para), allowed_urls)))
    return blocks
```

`src/ai_newsdesk/web/routes_briefing.py`:

```python
"""Briefing: the latest one, any earlier one by date, and the archive."""

from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ai_newsdesk.store.records import BriefingRecord
from ai_newsdesk.web.briefing_text import parse_briefing
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import page, store_of

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_day(value: str) -> date | None:
    if not _DAY_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def _render(
    request: Request, briefing: BriefingRecord | None, *, status_code: int = 200
) -> HTMLResponse:
    store = store_of(request)
    allowed: set[str] = set()
    if briefing is not None and briefing.story_ids:
        for story in (await store.stories_by_ids(briefing.story_ids)).values():
            allowed.update(src.url for src in story.sources)
    return await page(
        request,
        "briefing.html",
        {
            "active": "briefing",
            "briefing": briefing,
            "blocks": parse_briefing(briefing.body, allowed) if briefing is not None else [],
            "archive": await store.briefing_dates(),
            "missing": status_code == 404,
        },
        status_code=status_code,
    )


@router.get("/briefing", response_class=HTMLResponse)
async def latest_briefing(request: Request) -> HTMLResponse:
    return await _render(request, await store_of(request).latest_briefing())


@router.get("/briefing/{day}", response_class=HTMLResponse)
async def briefing_for_day(request: Request, day: str) -> HTMLResponse:
    parsed = _parse_day(day)
    briefing = await store_of(request).get_briefing(parsed) if parsed is not None else None
    if briefing is None:
        return await _render(request, None, status_code=404)
    return await _render(request, briefing)
```

`src/ai_newsdesk/web/templates/briefing.html`:

```html
{% extends "base.html" %}
{% block title %}Briefing on Newsdesk{% endblock %}
{% block content %}
<div class="briefing">
  <article aria-labelledby="briefing-title">
    <header class="page-head">
      {% if briefing %}
      <h1 id="briefing-title"><time datetime="{{ briefing.date.isoformat() }}">{{ day_label(briefing.date) }}</time></h1>
      <p class="lede">The morning briefing, written at {{ briefing.created_at.astimezone(zone).strftime("%H:%M") }} by {{ briefing.model }} from {{ briefing.story_ids|length }} {{ "story" if briefing.story_ids|length == 1 else "stories" }}.</p>
      {% elif missing %}
      <h1 id="briefing-title">No briefing for that day</h1>
      <p class="lede">Pick a day from the list, or read <a href="/briefing">the latest briefing</a>.</p>
      {% else %}
      <h1 id="briefing-title">No briefing yet</h1>
      <p class="lede">The first one is written at {{ runtime.briefing_time }} from the day's top stories. You can change the time in <a href="/settings#schedule">Settings</a>.</p>
      {% endif %}
    </header>
    {% if blocks %}
    <div class="read">
      {% for block in blocks %}
      {% if block.kind == "heading" %}
      <h2>{{ block.segments[0].text }}</h2>
      {% else %}
      <p>{% for seg in block.segments %}{% if seg.url %}<a class="ref" href="{{ seg.url|safe_url }}" rel="noopener noreferrer" target="_blank">{{ seg.text }}</a>{% else %}{{ seg.text }}{% endif %}{% endfor %}</p>
      {% endif %}
      {% endfor %}
    </div>
    {% endif %}
  </article>
  {% if archive %}
  <nav class="archive" aria-labelledby="archive-title">
    <h2 id="archive-title">All briefings</h2>
    <ul>
      {% for day in archive %}
      <li><a href="/briefing/{{ day.isoformat() }}"{% if briefing and briefing.date == day %} aria-current="page"{% endif %}>{{ short_date(day) }}{% if day.year != now.year %} {{ day.year }}{% endif %}</a></li>
      {% endfor %}
    </ul>
  </nav>
  {% endif %}
</div>
{% endblock %}
```

`src/ai_newsdesk/web/app_routes.py`: import `routes_briefing` and add it to the tuple:

```python
from ai_newsdesk.web import routes_briefing, routes_health, routes_today


def build_router() -> APIRouter:
    router = APIRouter()
    for sub in (routes_health.router, routes_today.router, routes_briefing.router):
        router.include_router(sub)
    return router
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/web -q`
Expected: all pass (the route-policy and design-system tests now cover `/briefing` and `/briefing/{day}` too).
Run: `uv run ruff check && uv run ruff format --check && uv run pyright`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add src/ai_newsdesk/web tests/web
git commit -m "feat(web): Briefing page with archive and story links (V1-S5)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 30: Sources page

**Goal:** See every source's health at a glance, grouped by kind (trust, weight, last success, last error, item counts), and add, edit, mute or delete sources inline with htmx, with plain-form fallbacks and strict validation.

**Files:**
- Modify: `src/ai_newsdesk/models.py` (append `SourceOverview`)
- Modify: `src/ai_newsdesk/store/store.py` (add `source_overview`)
- Create: `src/ai_newsdesk/web/forms.py`
- Create: `src/ai_newsdesk/web/routes_sources.py`
- Create: `src/ai_newsdesk/web/templates/sources.html`
- Create: `src/ai_newsdesk/web/templates/partials/source_row.html`
- Create: `src/ai_newsdesk/web/templates/partials/source_edit.html`
- Modify: `src/ai_newsdesk/web/app_routes.py`
- Create: `tests/web/test_source_form.py`, `tests/web/test_sources.py`
- Modify: `tests/test_store_web_queries.py` (append one test)

**Acceptance Criteria:**
- [ ] `GET /sources` groups sources in the order Feeds, People on X, Searches, GitHub releases, arXiv, Hacker News; each row shows name, trust tag, "Muted" tag when disabled, detail (URL, `@handle`, query, repo, categories), weight, items this week, items in stories this week, all-time items, last success (relative) and the last error (escaped, truncated).
- [ ] Add (`POST /sources`): kinds feed / x_person / search / github; name 1–80 characters; feed URL must be `http(s)://` with a host; handle must match `^[A-Za-z0-9_]{1,15}$` (a leading `@` is stripped); query 1–200 characters; repo `owner/name`. Invalid → 422 with the form open, values kept and the error next to the field. Valid → 303 to `/sources?added=<id>#source-<id>` and the "Source added" notice.
- [ ] Edit: `GET /sources/{id}/edit` (htmx → the edit form fragment; plain → the full page with that row in edit mode); `POST /sources/{id}` validates the same way (htmx 200 row / 422 form; plain 303 / 422 page); `GET /sources/{id}/row` restores the row (Cancel).
- [ ] Mute toggles `enabled` (htmx → updated row; plain → 303); Delete removes the source (htmx → empty 200 so the row disappears; plain → 303; `hx-confirm` asks first).
- [ ] Unknown source ID → 404 everywhere; every POST needs CSRF.

**Verify:** `uv run pytest tests/web/test_source_form.py tests/web/test_sources.py tests/test_store_web_queries.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

`tests/web/test_source_form.py`:

```python
from __future__ import annotations

from ai_newsdesk.models import SourceConfig, SourceKind, Trust
from ai_newsdesk.web.forms import parse_source_form


def test_valid_feed() -> None:
    form = parse_source_form(kind="feed", name="  Example   Blog ", trust="trusted", value=" https://blog.example/feed.xml ")
    assert form.ok
    assert form.kind == SourceKind.FEED
    assert form.trust == Trust.TRUSTED
    assert form.name == "Example Blog"
    assert form.config == {"url": "https://blog.example/feed.xml"}


def test_feed_must_be_http_or_https() -> None:
    for bad in ("ftp://blog.example/feed", "blog.example/feed", "javascript:alert(1)", "https://", ""):
        form = parse_source_form(kind="feed", name="Blog", trust="trusted", value=bad)
        assert form.errors == {"value": "Use a feed address that starts with http:// or https://."}, bad


def test_x_handle_rules() -> None:
    assert parse_source_form(kind="x_person", name="A", trust="trusted", value="@karpathy").config == {"handle": "karpathy"}
    assert parse_source_form(kind="x_person", name="A", trust="trusted", value="a_b_123456789012").errors == {
        "value": "An X handle is 1 to 15 letters, digits or underscores."
    }
    for bad in ("", "has space", "dash-name", "toolonghandle_123"):
        assert "value" in parse_source_form(kind="x_person", name="A", trust="trusted", value=bad).errors, bad


def test_search_and_github() -> None:
    assert parse_source_form(kind="search", name="S", trust="discovery", value=" new  AI model ").config == {
        "query": "new AI model"
    }
    assert "value" in parse_source_form(kind="search", name="S", trust="discovery", value=" ").errors
    assert parse_source_form(kind="github", name="G", trust="trusted", value="https://github.com/org/repo/").config == {
        "repo": "org/repo"
    }
    assert "value" in parse_source_form(kind="github", name="G", trust="trusted", value="not a repo").errors


def test_kind_trust_and_name_are_checked() -> None:
    form = parse_source_form(kind="hn", name="", trust="best", value="x")
    assert set(form.errors) == {"kind", "trust", "name"}
    assert "value" not in form.errors
    assert parse_source_form(kind="feed", name="n" * 81, trust="primary", value="https://a.example").errors == {
        "name": "Give the source a name of 1 to 80 characters."
    }


def test_editing_keeps_kind_and_unedited_config() -> None:
    hn = SourceConfig(id=4, kind=SourceKind.HN, name="Hacker News", config={"min_points": 100}, trust=Trust.DISCOVERY)
    form = parse_source_form(kind="feed", name="HN front page", trust="discovery", value="", existing=hn)
    assert form.ok
    assert form.kind == SourceKind.HN
    assert form.config == {"min_points": 100}
```

`tests/web/test_sources.py`:

```python
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.models import SourceKind, SourceResult, Trust
from tests.web import seed
from tests.web.support import HX, NOW, csrf_token, run


def _form(client: TestClient, **values: str) -> dict[str, str]:
    return {"csrf_token": csrf_token(client), **values}


def _hx(client: TestClient) -> dict[str, str]:
    return {**HX, "X-CSRF-Token": csrf_token(client)}


def test_sources_page_groups_by_kind_with_health(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    feed = run(client, seed.add_source, store, name="Zeta Lab")
    run(
        client, seed.add_source, store, name="Ada", kind=SourceKind.X_PERSON, trust=Trust.TRUSTED,
        config={"handle": "ada_l"},
    )
    run(client, store.record_source_result, SourceResult(source_id=feed, error="HTTP 503 <from> feed"), NOW)
    r = client.get("/sources")
    assert r.status_code == 200
    text = r.text
    assert text.index(">Feeds</h2>") < text.index(">People on X</h2>")
    assert "Zeta Lab" in text
    assert "@ada_l" in text
    assert "Last error: HTTP 503 &lt;from&gt; feed" in text
    assert "<dt>Items this week</dt>" in text
    assert 'aria-current="page">Sources' in text


def test_add_a_feed(app: FastAPI, client: TestClient) -> None:
    r = client.post(
        "/sources",
        data=_form(client, kind="feed", name="Example Blog", trust="trusted", value="https://blog.example/feed.xml"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/sources?added=")
    sources = run(client, app.state.store.list_sources, enabled_only=False)
    [added] = [s for s in sources if s.name == "Example Blog"]
    assert added.kind == SourceKind.FEED
    assert added.trust == Trust.TRUSTED
    assert added.config == {"url": "https://blog.example/feed.xml"}
    page = client.get(r.headers["location"])
    assert "Source added." in page.text


def test_add_rejects_bad_values_and_keeps_the_form(client: TestClient) -> None:
    r = client.post("/sources", data=_form(client, kind="feed", name="Blog", trust="trusted", value="ftp://blog.example/feed"))
    assert r.status_code == 422
    assert "starts with http:// or https://" in r.text
    assert 'value="ftp://blog.example/feed"' in r.text
    assert '<details class="add-source" open>' in r.text
    r = client.post("/sources", data=_form(client, kind="x_person", name="A", trust="trusted", value="not a handle!"))
    assert r.status_code == 422
    assert "1 to 15 letters, digits or underscores" in r.text
    r = client.post("/sources", data=_form(client, kind="hn", name="HN", trust="trusted", value="x"))
    assert r.status_code == 422
    assert "Choose what kind of source this is." in r.text
    r = client.post("/sources", data=_form(client, kind="feed", name="", trust="trusted", value="https://a.example/f"))
    assert r.status_code == 422
    assert "name of 1 to 80 characters" in r.text


def test_add_an_x_person_strips_the_at(app: FastAPI, client: TestClient) -> None:
    client.post("/sources", data=_form(client, kind="x_person", name="Andrej", trust="trusted", value="@karpathy"))
    sources = run(client, app.state.store.list_sources, enabled_only=False)
    [added] = [s for s in sources if s.name == "Andrej"]
    assert added.config == {"handle": "karpathy"}


def test_edit_inline_with_htmx(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Old name")
    form = client.get(f"/sources/{src}/edit", headers=HX)
    assert form.status_code == 200
    assert form.text.lstrip().startswith('<li class="source source--editing"')
    assert 'value="Old name"' in form.text
    r = client.post(
        f"/sources/{src}",
        data={"name": "New name", "trust": "discovery", "value": "https://new.example/feed.xml"},
        headers=_hx(client),
    )
    assert r.status_code == 200
    assert r.text.lstrip().startswith(f'<li class="source" id="source-{src}"')
    assert "New name" in r.text and "Discovery" in r.text
    stored = run(client, store.get_source, src)
    assert stored is not None
    assert stored.name == "New name"
    assert stored.trust == Trust.DISCOVERY
    assert stored.config == {"url": "https://new.example/feed.xml"}
    bad = client.post(f"/sources/{src}", data={"name": "New name", "trust": "discovery", "value": "nope"}, headers=_hx(client))
    assert bad.status_code == 422
    assert "source--editing" in bad.text
    assert "starts with http:// or https://" in bad.text
    row = client.get(f"/sources/{src}/row", headers=HX)
    assert "New name" in row.text and "source--editing" not in row.text


def test_edit_without_htmx_renders_the_full_page(app: FastAPI, client: TestClient) -> None:
    src = run(client, seed.add_source, app.state.store, name="Plain")
    r = client.get(f"/sources/{src}/edit")
    assert r.status_code == 200
    assert "<html" in r.text
    assert f'<li class="source source--editing" id="source-{src}">' in r.text
    r = client.post(
        f"/sources/{src}",
        data=_form(client, name="Plain renamed", trust="primary", value="https://plain.example/feed"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/sources#source-{src}"


def test_edit_and_mute_keep_the_learned_weight(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Weighted")
    run(client, store.set_source_weight, src, 1.5)
    client.post(
        f"/sources/{src}",
        data={"name": "Weighted, renamed", "trust": "trusted", "value": "https://w.example/feed"},
        headers=_hx(client),
    )
    client.post(f"/sources/{src}/mute", headers=_hx(client))
    stored = run(client, store.get_source, src)
    assert stored is not None
    assert (stored.name, stored.enabled, stored.weight) == ("Weighted, renamed", False, 1.5)


def test_mute_toggles(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Noisy")
    r = client.post(f"/sources/{src}/mute", headers=_hx(client))
    assert r.status_code == 200
    assert ">Unmute</button>" in r.text and "source--muted" in r.text
    stored = run(client, store.get_source, src)
    assert stored is not None and stored.enabled is False
    r = client.post(f"/sources/{src}/mute", data=_form(client), follow_redirects=False)
    assert r.status_code == 303
    stored = run(client, store.get_source, src)
    assert stored is not None and stored.enabled is True


def test_delete(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    src = run(client, seed.add_source, store, name="Gone soon")
    r = client.post(f"/sources/{src}/delete", headers=_hx(client))
    assert r.status_code == 200
    assert r.text == ""
    assert run(client, store.get_source, src) is None
    other = run(client, seed.add_source, store, name="Gone too")
    r = client.post(f"/sources/{other}/delete", data=_form(client), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sources"


def test_unknown_source_is_404(client: TestClient) -> None:
    assert client.get("/sources/999999/edit").status_code == 404
    assert client.get("/sources/999999/row", headers=HX).status_code == 404
    assert client.post("/sources/999999/mute", headers=_hx(client)).status_code == 404
    assert client.post("/sources/999999/delete", headers=_hx(client)).status_code == 404
```

Append to `tests/test_store_web_queries.py` (add `SourceResult` to its `ai_newsdesk.models` import):

```python
async def test_source_overview_counts_items_and_reports_health(store: Store) -> None:
    zeta = await seed.add_source(store, name="Zeta Lab")
    await seed.add_item(store, source_id=zeta, url="https://zeta.example/1", title="New", at=NOW - timedelta(hours=1))
    await seed.add_item(store, source_id=zeta, url="https://zeta.example/2", title="Old", at=NOW - timedelta(days=10))
    await seed.add_story(store, at=NOW, headline="Story", sources=[(zeta, "https://zeta.example/3", "In a story")])
    await store.record_source_result(SourceResult(source_id=zeta, error="HTTP 503 from feed"), NOW)
    healthy = await seed.add_source(store, name="Healthy")
    await store.record_source_result(SourceResult(source_id=healthy), NOW)
    muted = await seed.add_source(store, name="Alpha", enabled=False)

    rows = {r.source.id: r for r in await store.source_overview(NOW)}
    assert rows[zeta].items_total == 3
    assert rows[zeta].items_7d == 2
    assert rows[zeta].stories_7d == 1
    assert rows[zeta].last_error == "HTTP 503 from feed"
    assert rows[healthy].last_ok_at == NOW
    assert rows[healthy].last_error is None
    assert rows[muted].source.enabled is False
    assert rows[muted].items_total == 0
    assert rows[muted].last_ok_at is None
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/web/test_source_form.py tests/web/test_sources.py tests/test_store_web_queries.py -q`
Expected: FAIL: `ModuleNotFoundError: No module named 'ai_newsdesk.web.forms'`, and `AttributeError: 'Store' object has no attribute 'source_overview'`.

- [ ] **Step 3: Implement**

Append to `src/ai_newsdesk/models.py`:

```python
class SourceOverview(BaseModel):
    """One row of the Sources page: the source plus its health and volume."""

    source: SourceConfig
    last_ok_at: datetime | None = None
    last_error: str | None = None
    items_7d: int = 0
    stories_7d: int = 0
    items_total: int = 0
```

In `src/ai_newsdesk/store/store.py`, add `SourceOverview` to the models import and add to `class Store`. It reuses step 1's `list_sources` (for the config) and `source_stats()` (for last success, last error and the all-time item count) and only adds the two 7-day counts:

```python
    async def source_overview(self, now: dt.datetime) -> list[SourceOverview]:
        """Every source (muted ones too) with step 1's health stats plus 7-day item counts."""
        configs = {s.id: s for s in await self.list_sources(enabled_only=False)}
        recent = {
            int(r["source_id"]): (int(r["items_7d"]), int(r["stories_7d"]))
            for r in await self._all(
                "SELECT source_id, COUNT(*) AS items_7d,"
                " COUNT(story_id) AS stories_7d"
                " FROM items WHERE fetched_at >= ? GROUP BY source_id",
                (_ts(now - dt.timedelta(days=7)),),
            )
        }
        rows: list[SourceOverview] = []
        for stats in await self.source_stats():
            src = configs.get(stats.id)
            if src is None:  # deleted between the two reads
                continue
            items_7d, stories_7d = recent.get(stats.id, (0, 0))
            rows.append(
                SourceOverview(
                    source=src,
                    last_ok_at=stats.last_ok_at,
                    last_error=stats.last_error or None,
                    items_7d=items_7d,
                    stories_7d=stories_7d,
                    items_total=stats.item_count,
                )
            )
        return rows
```

`src/ai_newsdesk/web/forms.py`:

```python
"""Validation for the Sources forms. Returns clean values plus field errors; never raises."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ai_newsdesk.models import SourceConfig, SourceKind, Trust

ConfigValue = str | int | float | bool | list[str]

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
NAME_MAX = 80
QUERY_MAX = 200
URL_MAX = 2000

# kind -> (config key, field label, hint). arXiv and HN are singletons: name and trust only.
EDITABLE_KINDS: dict[SourceKind, tuple[str, str, str]] = {
    SourceKind.FEED: ("url", "Feed address", "The RSS or Atom address, starting with https://."),
    SourceKind.X_PERSON: (
        "handle",
        "X handle",
        "Without the @. Posts are found through research-engine search; X itself is never fetched.",
    ),
    SourceKind.SEARCH: ("query", "Search words", "A topic search run through research-engine."),
    SourceKind.GITHUB: ("repo", "Repository", "owner/name. New releases are collected."),
}

FEED_ERROR = "Use a feed address that starts with http:// or https://."
HANDLE_ERROR = "An X handle is 1 to 15 letters, digits or underscores."
QUERY_ERROR = f"Enter 1 to {QUERY_MAX} characters to search for."
REPO_ERROR = "Use the form owner/name, for example huggingface/transformers."
NAME_ERROR = f"Give the source a name of 1 to {NAME_MAX} characters."


@dataclass
class SourceForm:
    kind: SourceKind | None
    name: str
    trust: Trust | None
    value: str
    config: dict[str, ConfigValue] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _value(kind: SourceKind, raw: str) -> tuple[ConfigValue | None, str | None]:
    value = raw.strip()
    if kind == SourceKind.FEED:
        try:
            parts = urlsplit(value)
        except ValueError:
            return None, FEED_ERROR
        if parts.scheme not in ("http", "https") or not parts.netloc or len(value) > URL_MAX:
            return None, FEED_ERROR
        return value, None
    if kind == SourceKind.X_PERSON:
        handle = value.removeprefix("@")
        return (handle, None) if HANDLE_RE.match(handle) else (None, HANDLE_ERROR)
    if kind == SourceKind.SEARCH:
        query = " ".join(value.split())
        return (query, None) if 0 < len(query) <= QUERY_MAX else (None, QUERY_ERROR)
    if kind == SourceKind.GITHUB:
        repo = value.removeprefix("https://github.com/").strip("/")
        return (repo, None) if REPO_RE.match(repo) else (None, REPO_ERROR)
    return None, None


def parse_source_form(
    *, kind: str, name: str, trust: str, value: str, existing: SourceConfig | None = None
) -> SourceForm:
    """Validate an add form, or an edit form when ``existing`` is given (its kind is kept)."""
    errors: dict[str, str] = {}
    source_kind: SourceKind | None
    if existing is not None:
        source_kind = existing.kind
    else:
        try:
            source_kind = SourceKind(kind)
        except ValueError:
            source_kind = None
        if source_kind not in EDITABLE_KINDS:
            source_kind = None
            errors["kind"] = "Choose what kind of source this is."
    try:
        source_trust: Trust | None = Trust(trust)
    except ValueError:
        source_trust = None
        errors["trust"] = "Choose a trust level."
    clean_name = " ".join(name.split())
    if not 0 < len(clean_name) <= NAME_MAX:
        errors["name"] = NAME_ERROR
    config: dict[str, ConfigValue] = dict(existing.config) if existing is not None else {}
    if source_kind in EDITABLE_KINDS:
        key = EDITABLE_KINDS[source_kind][0]
        parsed, error = _value(source_kind, value)
        if error is not None:
            errors["value"] = error
        elif parsed is not None:
            config[key] = parsed
    return SourceForm(
        kind=source_kind,
        name=clean_name,
        trust=source_trust,
        value=value.strip(),
        config=config,
        errors=errors,
    )
```

`src/ai_newsdesk/web/routes_sources.py`:

```python
"""Sources: health overview, add, inline edit, mute and delete."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_newsdesk.models import SourceConfig, SourceKind, SourceOverview, Trust
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import NO_STORE, is_htmx, kind_label, now_of, page, partial, store_of
from ai_newsdesk.web.forms import EDITABLE_KINDS, parse_source_form

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])

KIND_ORDER = (
    SourceKind.FEED,
    SourceKind.X_PERSON,
    SourceKind.SEARCH,
    SourceKind.GITHUB,
    SourceKind.ARXIV,
    SourceKind.HN,
)
KIND_NOTES: dict[SourceKind, str] = {
    SourceKind.FEED: "Blogs and lab news, read directly from RSS or Atom.",
    SourceKind.X_PERSON: "Posts found through research-engine search. Snippets only; X itself is never fetched.",
    SourceKind.SEARCH: "Topic searches run through research-engine.",
    SourceKind.GITHUB: "New releases of these repositories.",
    SourceKind.ARXIV: "New papers, filtered hard before review.",
    SourceKind.HN: "Hacker News stories above the points threshold.",
}
_EMPTY_ADD = {"kind": "feed", "name": "", "trust": "trusted", "value": ""}


def _form_values(src: SourceConfig) -> dict[str, str]:
    spec = EDITABLE_KINDS.get(src.kind)
    return {
        "name": src.name,
        "trust": src.trust.value,
        "value": str(src.config.get(spec[0], "")) if spec else "",
    }


def _shared() -> dict[str, Any]:
    return {"editable_kinds": EDITABLE_KINDS, "trusts": list(Trust)}


async def _row(request: Request, source_id: int) -> SourceOverview:
    for row in await store_of(request).source_overview(now_of(request)):
        if row.source.id == source_id:
            return row
    raise HTTPException(status_code=404, detail="There is no such source.")


async def _source(request: Request, source_id: int) -> SourceConfig:
    src = await store_of(request).get_source(source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="There is no such source.")
    return src


async def _page(
    request: Request,
    *,
    add: dict[str, str] | None = None,
    add_errors: dict[str, str] | None = None,
    editing: int | None = None,
    edit_form: dict[str, str] | None = None,
    edit_errors: dict[str, str] | None = None,
    added: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    rows = await store_of(request).source_overview(now_of(request))
    groups = [
        (kind, kind_label(kind), KIND_NOTES[kind], [r for r in rows if r.source.kind == kind])
        for kind in KIND_ORDER
    ]
    return await page(
        request,
        "sources.html",
        {
            "active": "sources",
            "groups": [g for g in groups if g[3]],
            "enabled_count": sum(1 for r in rows if r.source.enabled),
            "muted_count": sum(1 for r in rows if not r.source.enabled),
            "add": add or _EMPTY_ADD,
            "add_errors": add_errors or {},
            "editing": editing,
            "edit_form": edit_form or {},
            "edit_errors": edit_errors or {},
            "added": added,
            **_shared(),
        },
        status_code=status_code,
    )


def _row_partial(request: Request, row: SourceOverview) -> HTMLResponse:
    return partial(request, "partials/source_row.html", {"row": row})


def _edit_partial(
    request: Request, row: SourceOverview, form: dict[str, str], errors: dict[str, str], status_code: int = 200
) -> HTMLResponse:
    return partial(
        request,
        "partials/source_edit.html",
        {"row": row, "form": form, "errors": errors, **_shared()},
        status_code=status_code,
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request, added: int | None = None) -> HTMLResponse:
    return await _page(request, added=added is not None)


@router.post("/sources")
async def add_source(
    request: Request,
    kind: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    trust: Annotated[str, Form()] = "",
    value: Annotated[str, Form()] = "",
) -> Response:
    form = parse_source_form(kind=kind, name=name, trust=trust, value=value)
    if not form.ok or form.kind is None or form.trust is None:
        submitted = {"kind": kind, "name": name, "trust": trust, "value": value}
        return await _page(request, add=submitted, add_errors=form.errors, status_code=422)
    source_id = await store_of(request).upsert_source(form.kind, form.name, form.config, form.trust)
    return RedirectResponse(f"/sources?added={source_id}#source-{source_id}", status_code=303)


@router.get("/sources/{source_id}/edit", response_class=HTMLResponse)
async def edit_form(request: Request, source_id: int) -> HTMLResponse:
    src = await _source(request, source_id)
    if is_htmx(request):
        return _edit_partial(request, await _row(request, source_id), _form_values(src), {})
    return await _page(request, editing=source_id, edit_form=_form_values(src))


@router.get("/sources/{source_id}/row", response_class=HTMLResponse)
async def source_row(request: Request, source_id: int) -> HTMLResponse:
    return _row_partial(request, await _row(request, source_id))


@router.post("/sources/{source_id}")
async def edit_source(
    request: Request,
    source_id: int,
    name: Annotated[str, Form()] = "",
    trust: Annotated[str, Form()] = "",
    value: Annotated[str, Form()] = "",
) -> Response:
    existing = await _source(request, source_id)
    form = parse_source_form(kind=existing.kind.value, name=name, trust=trust, value=value, existing=existing)
    if not form.ok or form.trust is None:
        submitted = {"name": name, "trust": trust, "value": value}
        if is_htmx(request):
            return _edit_partial(request, await _row(request, source_id), submitted, form.errors, 422)
        return await _page(
            request, editing=source_id, edit_form=submitted, edit_errors=form.errors, status_code=422
        )
    # weight is left out (None), so step 1 keeps the weight learned from thumbs.
    await store_of(request).upsert_source(
        existing.kind,
        form.name,
        form.config,
        form.trust,
        enabled=existing.enabled,
        source_id=source_id,
    )
    if is_htmx(request):
        return _row_partial(request, await _row(request, source_id))
    return RedirectResponse(f"/sources#source-{source_id}", status_code=303)


@router.post("/sources/{source_id}/mute")
async def toggle_mute(request: Request, source_id: int) -> Response:
    src = await _source(request, source_id)
    await store_of(request).upsert_source(
        src.kind, src.name, src.config, src.trust, enabled=not src.enabled, source_id=source_id
    )
    if is_htmx(request):
        return _row_partial(request, await _row(request, source_id))
    return RedirectResponse(f"/sources#source-{source_id}", status_code=303)


@router.post("/sources/{source_id}/delete")
async def delete_source(request: Request, source_id: int) -> Response:
    await _source(request, source_id)
    await store_of(request).delete_source(source_id)
    if is_htmx(request):
        return HTMLResponse("", headers=NO_STORE)
    return RedirectResponse("/sources", status_code=303)
```

`src/ai_newsdesk/web/templates/partials/source_row.html`:

```html
{% set s = row.source %}
<li class="source{% if not s.enabled %} source--muted{% endif %}" id="source-{{ s.id }}">
  <div class="source__main">
    <p class="source__name">{{ s.name }} <span class="tag{% if s.trust == 'primary' %} tag--primary{% endif %}">{{ trust_label(s.trust) }}</span>{% if not s.enabled %} <span class="tag tag--muted">Muted</span>{% endif %}</p>
    <p class="source__detail">{{ source_detail(s) }}</p>
  </div>
  <dl class="source__stats">
    <div><dt>Weight</dt><dd>{{ "%.2f"|format(s.weight) }}</dd></div>
    <div><dt>Items this week</dt><dd>{{ row.items_7d }}</dd></div>
    <div><dt>In stories</dt><dd>{{ row.stories_7d }}</dd></div>
    <div><dt>All time</dt><dd>{{ row.items_total }}</dd></div>
    <div><dt>Last success</dt><dd>{% if row.last_ok_at %}<time datetime="{{ row.last_ok_at.isoformat() }}">{{ ago(row.last_ok_at, now) }}</time>{% else %}never{% endif %}</dd></div>
  </dl>
  <div class="source__actions">
    <a class="button button--quiet button--small" href="/sources/{{ s.id }}/edit" hx-get="/sources/{{ s.id }}/edit" hx-target="#source-{{ s.id }}" hx-swap="outerHTML">Edit</a>
    <form method="post" action="/sources/{{ s.id }}/mute" hx-post="/sources/{{ s.id }}/mute" hx-target="#source-{{ s.id }}" hx-swap="outerHTML">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="button button--quiet button--small" type="submit">{{ "Unmute" if not s.enabled else "Mute" }}</button>
    </form>
    <form method="post" action="/sources/{{ s.id }}/delete" hx-post="/sources/{{ s.id }}/delete" hx-target="#source-{{ s.id }}" hx-swap="outerHTML" hx-confirm="Delete {{ s.name }}? It will not be collected again.">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="button button--danger button--small" type="submit">Delete</button>
    </form>
  </div>
  {% if row.last_error %}
  <p class="source__error">Last error: {{ row.last_error|truncate(200, true) }}</p>
  {% endif %}
</li>
```

`src/ai_newsdesk/web/templates/partials/source_edit.html`:

```html
{% set s = row.source %}
{% set spec = editable_kinds.get(s.kind) %}
<li class="source source--editing" id="source-{{ s.id }}">
  <form class="form" method="post" action="/sources/{{ s.id }}" hx-post="/sources/{{ s.id }}" hx-target="#source-{{ s.id }}" hx-swap="outerHTML">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <p class="source__name">Edit {{ s.name }}</p>
    <div class="grid-2">
      <div class="field">
        <label for="name-{{ s.id }}">Name</label>
        <input id="name-{{ s.id }}" name="name" type="text" value="{{ form.name }}" required maxlength="80"{% if errors.name %} aria-invalid="true" aria-describedby="name-{{ s.id }}-error"{% endif %}>
        {% if errors.name %}<p class="field__error" id="name-{{ s.id }}-error">{{ errors.name }}</p>{% endif %}
      </div>
      <div class="field">
        <label for="trust-{{ s.id }}">Trust</label>
        <select id="trust-{{ s.id }}" name="trust"{% if errors.trust %} aria-invalid="true"{% endif %}>
          {% for t in trusts %}
          <option value="{{ t.value }}"{% if form.trust == t.value %} selected{% endif %}>{{ trust_label(t) }}</option>
          {% endfor %}
        </select>
        {% if errors.trust %}<p class="field__error">{{ errors.trust }}</p>{% endif %}
      </div>
    </div>
    {% if spec %}
    <div class="field">
      <label for="value-{{ s.id }}">{{ spec[1] }}</label>
      <input id="value-{{ s.id }}" name="value" type="text" value="{{ form.value }}" required aria-describedby="value-{{ s.id }}-hint{% if errors.value %} value-{{ s.id }}-error{% endif %}"{% if errors.value %} aria-invalid="true"{% endif %}>
      <p class="field__hint" id="value-{{ s.id }}-hint">{{ spec[2] }}</p>
      {% if errors.value %}<p class="field__error" id="value-{{ s.id }}-error">{{ errors.value }}</p>{% endif %}
    </div>
    {% endif %}
    <div class="form__actions">
      <button class="button" type="submit">Save changes</button>
      <a class="button button--quiet" href="/sources#source-{{ s.id }}" hx-get="/sources/{{ s.id }}/row" hx-target="#source-{{ s.id }}" hx-swap="outerHTML">Cancel</a>
    </div>
  </form>
</li>
```

`src/ai_newsdesk/web/templates/sources.html`:

```html
{% extends "base.html" %}
{% block title %}Sources on Newsdesk{% endblock %}
{% block content %}
<header class="page-head">
  <h1>Sources</h1>
  <p class="lede">{{ enabled_count }} active{% if muted_count %}, {{ muted_count }} muted{% endif %}. Thumbs on stories move each source's weight between 0.3 and 2.</p>
</header>
{% if added %}
<div class="notice notice--ok" role="status"><p>Source added. It is collected on the next run.</p></div>
{% endif %}
<details class="add-source"{% if add_errors %} open{% endif %}>
  <summary>Add a source</summary>
  <form class="form" method="post" action="/sources">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <div class="grid-2">
      <div class="field">
        <label for="add-kind">Kind</label>
        <select id="add-kind" name="kind"{% if add_errors.kind %} aria-invalid="true" aria-describedby="add-kind-error"{% endif %}>
          {% for kind in editable_kinds %}
          <option value="{{ kind.value }}"{% if add.kind == kind.value %} selected{% endif %}>{{ kind_label(kind) }}</option>
          {% endfor %}
        </select>
        {% if add_errors.kind %}<p class="field__error" id="add-kind-error">{{ add_errors.kind }}</p>{% endif %}
      </div>
      <div class="field">
        <label for="add-trust">Trust</label>
        <select id="add-trust" name="trust"{% if add_errors.trust %} aria-invalid="true" aria-describedby="add-trust-error"{% endif %}>
          {% for t in trusts %}
          <option value="{{ t.value }}"{% if add.trust == t.value %} selected{% endif %}>{{ trust_label(t) }}</option>
          {% endfor %}
        </select>
        {% if add_errors.trust %}<p class="field__error" id="add-trust-error">{{ add_errors.trust }}</p>{% endif %}
      </div>
    </div>
    <div class="field">
      <label for="add-name">Name</label>
      <input id="add-name" name="name" type="text" value="{{ add.name }}" required maxlength="80"{% if add_errors.name %} aria-invalid="true" aria-describedby="add-name-error"{% endif %}>
      {% if add_errors.name %}<p class="field__error" id="add-name-error">{{ add_errors.name }}</p>{% endif %}
    </div>
    <div class="field">
      <label for="add-value">Address, handle, search words or repository</label>
      <input id="add-value" name="value" type="text" value="{{ add.value }}" required aria-describedby="add-value-hint{% if add_errors.value %} add-value-error{% endif %}"{% if add_errors.value %} aria-invalid="true"{% endif %}>
      <p class="field__hint" id="add-value-hint">Feeds need a full https:// address. X handles go without the @. GitHub repositories are owner/name.</p>
      {% if add_errors.value %}<p class="field__error" id="add-value-error">{{ add_errors.value }}</p>{% endif %}
    </div>
    <div class="form__actions">
      <button class="button" type="submit">Add source</button>
    </div>
  </form>
</details>
{% for kind, label, note, rows in groups %}
<section class="kind" id="kind-{{ kind.value }}" aria-labelledby="kind-{{ kind.value }}-title">
  <div class="section__head">
    <h2 id="kind-{{ kind.value }}-title">{{ label }}</h2>
    <span class="section__count">{{ rows|length }}</span>
  </div>
  <p class="section__note">{{ note }}</p>
  <ul class="sources">
    {% for row in rows %}
    {% if editing == row.source.id %}
    {% with form = edit_form, errors = edit_errors %}{% include "partials/source_edit.html" %}{% endwith %}
    {% else %}
    {% include "partials/source_row.html" %}
    {% endif %}
    {% endfor %}
  </ul>
</section>
{% endfor %}
{% endblock %}
```

`src/ai_newsdesk/web/app_routes.py`: import `routes_sources` and add it to the tuple:

```python
from ai_newsdesk.web import routes_briefing, routes_health, routes_sources, routes_today


def build_router() -> APIRouter:
    router = APIRouter()
    for sub in (routes_health.router, routes_today.router, routes_briefing.router, routes_sources.router):
        router.include_router(sub)
    return router
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/web tests/test_store_web_queries.py -q`
Expected: all pass (route policy now also covers the seven Sources routes).
Run: `uv run ruff check && uv run ruff format --check && uv run pyright`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add src/ai_newsdesk/models.py src/ai_newsdesk/store/store.py src/ai_newsdesk/web tests/web tests/test_store_web_queries.py
git commit -m "feat(web): Sources page with health, add, edit, mute and delete (V1-S5)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 31: Settings page

**Goal:** One page to run the desk: update frequency (each option priced), daily review cap, monthly budget with a spend meter and per-day chart, judge/writer/briefing models with prices, the "why it matters" toggle, show threshold, briefing time, HN points, write-only API keys that are tested before they are saved, and Refresh now.

**Files:**
- Modify: `src/ai_newsdesk/models.py` (append `UsageStats`)
- Modify: `src/ai_newsdesk/store/store.py` (add `usage_last_days`)
- Create: `src/ai_newsdesk/web/estimate.py`
- Create: `src/ai_newsdesk/web/chart.py`
- Modify: `src/ai_newsdesk/web/forms.py` (append `parse_settings_form`)
- Create: `src/ai_newsdesk/web/routes_settings.py`
- Create: `src/ai_newsdesk/web/templates/settings.html`
- Create: `src/ai_newsdesk/web/templates/partials/spend_chart.html`
- Modify: `src/ai_newsdesk/web/app_routes.py`
- Create: `tests/web/test_estimate.py`, `tests/web/test_chart.py`, `tests/web/test_settings_form.py`, `tests/web/test_keys.py`, `tests/web/test_settings.py`
- Create: `tests/web/dev_seed.py` (demo data for the visual check; not a test)
- Modify: `tests/test_store_web_queries.py` (append one test)

**Acceptance Criteria:**
- [ ] Frequency select offers 1, 2, 4, 6, 12, 24 runs a day, each with "about $X a month" = 30 × (min(items/run × runs/day, daily cap) × (review/item + writer/item × (1, or 0.6 with why-it-matters off)) + briefing/day). Rates come from the last 7 days when there are ≥ 3 finished runs and ≥ 50 reviewed items; otherwise 125 items/run, $0.000106 review/item, $0.000022 writer/item, $0.005 briefing/day (so 4 a day ≈ $2.07). Options over the budget say "(over budget)".
- [ ] `POST /settings` validates every field, then saves with `update_runtime_settings(store, **values)` so keys this page does not edit (for example `blocklist`) are untouched (frequency in the list, `HH:MM`, cap 0–5000, budget 0–100, threshold 0–10, HN points 0–10000, provider anthropic/typesafe, judge model offered by that provider, writer/briefing models are Claude models from `pricing.yaml`); invalid → 422, nothing saved, errors by the fields and values kept; valid → saved, pipeline context rebuilt, 303 with "Settings saved.".
- [ ] Keys go through step 1's `ApiKeys` only (master plan R3): each key shows only "Set"/"Not set" and where it comes from (`api_key_status`: saved here with `updated_at`, or `.env`); the input is always empty. Saving runs the provider test first; failure → 422 with the provider's message, the key replaced by `[key removed]`, nothing stored; success → `set_api_key(name, key, now)` (encrypted `secret.<name>` record), context rebuilt. "Clear and use .env" calls `clear_api_key(name)`, so `resolve_api_key` falls back to the env value. The TypeSafe row appears only when the Jev judge is chosen or a TypeSafe key is saved. A saved key never appears in any response body.
- [ ] Refresh now: `POST /settings/refresh` starts `scheduler.run_now("manual")` (master plan R6) as a background task; if the scheduler's lock is held → "A run is already going". The last 5 runs are listed with time, trigger, cost and any error (escaped).
- [ ] Spend meter (month spend / budget, red when reached) and an inline SVG bar chart per day of this month: `role="img"` with title and description, a `<title>` per bar, and a "Show as a table" fallback.
- [ ] Every Settings POST needs CSRF.

**Verify:** `uv run pytest tests/web tests/test_store_web_queries.py -q` → all pass; then `make test` → green.

**Steps:**

- [ ] **Step 1: Write the failing unit tests**

`tests/web/test_estimate.py`:

```python
from __future__ import annotations

import pytest

from ai_newsdesk.models import UsageStats
from ai_newsdesk.web.estimate import (
    DEFAULT_RATES,
    RUNS_PER_DAY_OPTIONS,
    frequency_options,
    monthly_estimate,
    rates_from,
)


def test_defaults_match_the_spec() -> None:
    assert monthly_estimate(DEFAULT_RATES, runs_per_day=4, daily_cap=500, why_it_matters=True) == pytest.approx(2.07)
    assert monthly_estimate(DEFAULT_RATES, runs_per_day=1, daily_cap=500, why_it_matters=True) == pytest.approx(0.63)


def test_daily_cap_limits_the_estimate() -> None:
    four = monthly_estimate(DEFAULT_RATES, runs_per_day=4, daily_cap=500, why_it_matters=True)
    assert monthly_estimate(DEFAULT_RATES, runs_per_day=24, daily_cap=500, why_it_matters=True) == pytest.approx(four)
    assert monthly_estimate(DEFAULT_RATES, runs_per_day=24, daily_cap=0, why_it_matters=True) == pytest.approx(0.15)


def test_why_it_matters_off_is_cheaper() -> None:
    assert monthly_estimate(DEFAULT_RATES, runs_per_day=4, daily_cap=500, why_it_matters=False) == pytest.approx(1.938)


def test_history_replaces_the_defaults() -> None:
    stats = UsageStats(days=7, runs=28, items_reviewed=2800, review_usd=0.28, write_usd=0.07, briefing_usd=0.035)
    rates = rates_from(stats)
    assert rates.from_history
    assert rates.items_per_run == pytest.approx(100)
    assert monthly_estimate(rates, runs_per_day=4, daily_cap=500, why_it_matters=True) == pytest.approx(1.65)


def test_thin_history_keeps_the_defaults() -> None:
    assert rates_from(UsageStats(days=7, runs=2, items_reviewed=900, review_usd=1, write_usd=1, briefing_usd=1)) == DEFAULT_RATES
    assert rates_from(UsageStats(days=7, runs=9, items_reviewed=10, review_usd=1, write_usd=1, briefing_usd=1)) == DEFAULT_RATES


def test_frequency_options() -> None:
    options = frequency_options(DEFAULT_RATES, daily_cap=500, why_it_matters=True, budget=1.0)
    assert [o.runs for o in options] == list(RUNS_PER_DAY_OPTIONS)
    assert [o.label for o in options[:3]] == ["Once a day", "Twice a day", "4 times a day"]
    assert not options[0].over_budget
    assert options[2].over_budget
```

`tests/web/test_chart.py`:

```python
from __future__ import annotations

from datetime import date

import pytest

from ai_newsdesk.web.chart import CHART_HEIGHT, nice_ceiling, spend_chart


def test_nice_ceiling() -> None:
    assert nice_ceiling(0) == 0.01
    assert nice_ceiling(0.083) == pytest.approx(0.1)
    assert nice_ceiling(0.1) == pytest.approx(0.1)
    assert nice_ceiling(0.13) == pytest.approx(0.2)
    assert nice_ceiling(0.24) == pytest.approx(0.25)
    assert nice_ceiling(3.1) == pytest.approx(5)


def test_spend_chart_has_one_bar_per_day_so_far() -> None:
    rows = [("2026-09-30", 9.0), ("2026-10-01", 0.05), ("2026-10-03", 0.1), ("2026-10-03", 0.0)]
    chart = spend_chart(rows, date(2026, 10, 3))
    assert [b.day.day for b in chart.bars] == [1, 2, 3]
    assert chart.scale_max == pytest.approx(0.1)
    assert chart.total == pytest.approx(0.15)
    assert chart.bars[1].height == 0
    assert chart.bars[2].height == pytest.approx(CHART_HEIGHT - 4)
    assert chart.bars[0].height == pytest.approx((CHART_HEIGHT - 4) / 2)
    assert chart.bars[2].y == pytest.approx(4)
    assert [b.x for b in chart.bars] == [2, 14, 26]
    assert chart.width == 7 * 12
    assert chart.peak is not None and chart.peak.day == date(2026, 10, 3)


def test_tiny_values_stay_visible_and_empty_months_have_no_peak() -> None:
    chart = spend_chart([("2026-10-01", 0.001), ("2026-10-02", 0.1)], date(2026, 10, 2))
    assert chart.bars[0].height == 2.0
    empty = spend_chart([], date(2026, 10, 14))
    assert len(empty.bars) == 14
    assert empty.peak is None
    assert all(b.height == 0 for b in empty.bars)
```

`tests/web/test_settings_form.py`:

```python
from __future__ import annotations

from ai_newsdesk.web.forms import parse_settings_form

MODELS = {"anthropic": {"claude-haiku-5-5", "claude-sonnet-5-5"}, "typesafe": {"jev-1-13"}}
VALID = {
    "runs_per_day": "4",
    "briefing_time": "07:00",
    "daily_review_cap": "500",
    "monthly_budget_usd": "5.00",
    "judge_provider": "anthropic",
    "judge_model": "claude-haiku-5-5",
    "writer_model": "claude-haiku-5-5",
    "briefing_model": "claude-sonnet-5-5",
    "show_threshold": "4",
    "why_it_matters": "on",
    "hn_min_points": "100",
}


def test_valid_form() -> None:
    form = parse_settings_form(VALID, models_by_provider=MODELS)
    assert form.ok
    assert form.values == {
        "runs_per_day": 4,
        "briefing_time": "07:00",
        "daily_review_cap": 500,
        "monthly_budget_usd": 5.0,
        "judge_provider": "anthropic",
        "judge_model": "claude-haiku-5-5",
        "writer_model": "claude-haiku-5-5",
        "briefing_model": "claude-sonnet-5-5",
        "show_threshold": 4,
        "why_it_matters": True,
        "hn_min_points": 100,
    }


def test_unchecked_box_means_off() -> None:
    form = parse_settings_form({k: v for k, v in VALID.items() if k != "why_it_matters"}, models_by_provider=MODELS)
    assert form.values["why_it_matters"] is False


def test_each_field_is_validated() -> None:
    cases = {
        "runs_per_day": ("5", "Choose one of the listed frequencies."),
        "briefing_time": ("25:00", "Use the 24-hour form HH:MM, for example 07:00."),
        "daily_review_cap": ("5001", "Enter a whole number from 0 to 5000."),
        "monthly_budget_usd": ("nan", "Enter an amount from 0 to 100 dollars."),
        "show_threshold": ("11", "Enter a whole number from 0 to 10."),
        "hn_min_points": ("-1", "Enter a whole number from 0 to 10000."),
        "judge_provider": ("openai", "Choose a judge provider."),
        "writer_model": ("jev-1-13", "Choose a Claude model from the list."),
        "briefing_model": ("gpt-5", "Choose a Claude model from the list."),
    }
    for field, (bad, message) in cases.items():
        form = parse_settings_form({**VALID, field: bad}, models_by_provider=MODELS)
        assert form.errors == {field: message}, field


def test_judge_model_must_match_the_provider() -> None:
    form = parse_settings_form({**VALID, "judge_provider": "typesafe"}, models_by_provider=MODELS)
    assert form.errors == {"judge_model": "Choose a model that the selected judge provider offers."}
    form = parse_settings_form({**VALID, "judge_provider": "typesafe", "judge_model": "jev-1-13"}, models_by_provider=MODELS)
    assert form.ok
```

`tests/web/test_keys.py`:

```python
from __future__ import annotations

from ai_newsdesk.web.keys import REDACTED, scrub


def test_scrub_removes_the_key_and_key_like_tokens() -> None:
    key = "sk-test-SECRET-123"
    assert scrub(f"invalid x-api-key {key} rejected", key) == f"invalid x-api-key {REDACTED} rejected"
    assert key not in scrub(f"{key}{key}", key)
    assert REDACTED in scrub("echoed sk-ant-api03-abcdefgh back", "other")
    assert REDACTED in scrub("token " + "A" * 40, "other")
    assert scrub("plain message", "sk-x") == "plain message"
    assert len(scrub("x" * 1000, "k")) <= 300
```

Append to `tests/test_store_web_queries.py` (add `Purpose`, `Usage` and `UsageStats` to its `ai_newsdesk.models` import and `import pytest`):

```python
async def test_usage_last_days(store: Store) -> None:
    for days_ago in (0, 1, 2):
        run_id = await store.start_run("schedule", NOW - timedelta(days=days_ago, hours=1))
        await store.finish_run(run_id, NOW - timedelta(days=days_ago), {}, 0.0, None)
    old = await store.start_run("schedule", NOW - timedelta(days=10))
    await store.finish_run(old, NOW - timedelta(days=10), {}, 0.0, None)
    src = await seed.add_source(store, name="Lab")
    for n in (1, 2):
        await seed.add_item(store, source_id=src, url=f"https://lab.example/{n}", title=f"T{n}", at=NOW - timedelta(hours=1), status=ItemStatus.REVIEWED)
    await seed.add_item(store, source_id=src, url="https://lab.example/3", title="T3", at=NOW, status=ItemStatus.REVIEW_FAILED)

    def usage(purpose: Purpose) -> Usage:
        return Usage(provider="anthropic", model="claude-haiku-5-5", purpose=purpose, input_tokens=10)

    await store.add_spend(usage(Purpose.REVIEW), 0.02, NOW - timedelta(hours=1))
    await store.add_spend(usage(Purpose.WRITE), 0.01, NOW - timedelta(hours=1))
    await store.add_spend(usage(Purpose.BRIEFING), 0.005, NOW - timedelta(hours=1))
    await store.add_spend(usage(Purpose.REVIEW), 1.0, NOW - timedelta(days=9))

    stats = await store.usage_last_days(NOW)
    assert isinstance(stats, UsageStats)
    assert (stats.days, stats.runs, stats.items_reviewed) == (7, 3, 2)
    assert stats.review_usd == pytest.approx(0.02)
    assert stats.write_usd == pytest.approx(0.01)
    assert stats.briefing_usd == pytest.approx(0.005)
```

- [ ] **Step 2: Write the failing page tests**

`tests/web/test_settings.py`:

```python
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.app import create_app
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import Purpose, Usage
from ai_newsdesk.runtime_settings import (
    API_KEY_NAMES,
    RuntimeSettings,
    load_runtime_settings,
    update_runtime_settings,
)
from tests.web.fakes import fake_ctx_factory
from tests.web.support import BASE_URL, CONFIG_DIR, NOW, csrf_token, login, make_settings, run

SECRET = "sk-test-SECRET-123"
VALID = {
    "runs_per_day": "4",
    "briefing_time": "07:00",
    "daily_review_cap": "500",
    "monthly_budget_usd": "5.00",
    "judge_provider": "anthropic",
    "judge_model": "claude-haiku-5-5",
    "writer_model": "claude-haiku-5-5",
    "briefing_model": "claude-haiku-5-5",
    "show_threshold": "4",
    "why_it_matters": "on",
    "hn_min_points": "100",
}


def _post(client: TestClient, path: str, data: dict[str, str]) -> object:
    return client.post(path, data={"csrf_token": csrf_token(client), **data}, follow_redirects=False)


async def _drain(app: FastAPI) -> None:
    await asyncio.gather(*list(app.state.background))


async def _release(lock: asyncio.Lock) -> None:
    lock.release()


def _resolve(client: TestClient, app: FastAPI, name: str) -> str:
    return run(client, app.state.api_keys.resolve_api_key, name)


def test_settings_page_shows_estimates_models_and_spend(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    for text in (
        "4 times a day, about $2.07 a month",
        "Once a day, about $0.63 a month",
        "claude-haiku-5-5 ($0.1 in, $0.5 out per million tokens)",
        "of $5.00 spent in October",
        'role="img"',
        "Show as a table",
        "Refresh now",
        'aria-current="page">Settings',
        'name="why_it_matters" type="checkbox" checked',
    ):
        assert text in r.text, text


def test_saving_valid_settings(app: FastAPI, client: TestClient) -> None:
    before = app.state.ctx
    data = {**VALID, "runs_per_day": "6", "briefing_time": "06:30", "monthly_budget_usd": "7.5", "show_threshold": "5"}
    del data["why_it_matters"]
    r = client.post("/settings", data={"csrf_token": csrf_token(client), **data}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=settings"
    rt = run(client, load_runtime_settings, app.state.store)
    assert (rt.runs_per_day, rt.briefing_time, rt.monthly_budget_usd, rt.show_threshold) == (6, "06:30", 7.5, 5)
    assert rt.why_it_matters is False
    assert app.state.ctx is not before
    assert "Settings saved." in client.get(r.headers["location"]).text


def test_invalid_frequency_is_rejected(app: FastAPI, client: TestClient) -> None:
    r = client.post("/settings", data={"csrf_token": csrf_token(client), **VALID, "runs_per_day": "5"})
    assert r.status_code == 422
    assert "Choose one of the listed frequencies." in r.text
    assert "Nothing was saved." in r.text
    assert run(client, load_runtime_settings, app.state.store).runs_per_day == 4


def test_other_invalid_values_are_rejected(client: TestClient) -> None:
    for field, bad, message in (
        ("briefing_time", "25:00", "Use the 24-hour form HH:MM, for example 07:00."),
        ("monthly_budget_usd", "-1", "Enter an amount from 0 to 100 dollars."),
        ("writer_model", "gpt-5", "Choose a Claude model from the list."),
    ):
        r = client.post("/settings", data={"csrf_token": csrf_token(client), **VALID, field: bad})
        assert r.status_code == 422, field
        assert message in r.text, field
        assert f'value="{bad}"' in r.text or field == "writer_model", field


def test_settings_posts_need_csrf(client: TestClient) -> None:
    assert client.post("/settings", data=VALID).status_code == 403
    assert client.post("/settings/keys/anthropic", data={"key": SECRET}).status_code == 403
    assert client.post("/settings/keys/anthropic/clear").status_code == 403
    assert client.post("/settings/refresh").status_code == 403


def test_api_key_is_write_only(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    r = client.post(
        "/settings/keys/anthropic",
        data={"csrf_token": csrf_token(client), "key": SECRET},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=key#key-anthropic"
    page = client.get(r.headers["location"]).text
    assert SECRET not in page
    assert "SECRET" not in page
    assert "Key tested and saved." in page
    assert "Saved here" in page
    stored = run(client, store.get_setting, "secret.anthropic")  # step 1's {"token", "updated_at"}
    assert isinstance(stored, dict) and stored["token"] and SECRET not in json.dumps(stored)
    assert _resolve(client, app, "anthropic") == SECRET
    for path in ("/", "/sources", "/briefing", "/health", "/settings?saved=key"):
        assert SECRET not in client.get(path).text, path


def test_failed_key_test_refuses_to_save_and_hides_the_key(
    app: FastAPI, client: TestClient, key_results: dict[str, str | None]
) -> None:
    key_results["anthropic"] = f"Anthropic answered 401: invalid x-api-key {SECRET}"
    r = client.post("/settings/keys/anthropic", data={"csrf_token": csrf_token(client), "key": SECRET})
    assert r.status_code == 422
    assert SECRET not in r.text
    assert "SECRET" not in r.text
    assert "Anthropic answered 401: invalid x-api-key [key removed]" in r.text
    assert run(client, app.state.store.get_setting, "secret.anthropic") is None


def test_empty_key_and_unknown_key_name(client: TestClient) -> None:
    r = client.post("/settings/keys/anthropic", data={"csrf_token": csrf_token(client), "key": "  "})
    assert r.status_code == 422
    assert "Paste a key first." in r.text
    r = client.post("/settings/keys/openai", data={"csrf_token": csrf_token(client), "key": "x"})
    assert r.status_code == 404


def test_clear_key_falls_back_to_env(tmp_path: Path) -> None:
    async def ok(key: str, runtime: RuntimeSettings) -> str | None:
        return None

    app = create_app(
        make_settings(tmp_path, anthropic_api_key="sk-env-fallback-key"),
        ctx_factory=fake_ctx_factory,
        key_testers={name: ok for name in API_KEY_NAMES},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
    )
    with TestClient(app, base_url=BASE_URL) as c:
        login(c)
        c.post("/settings/keys/anthropic", data={"csrf_token": csrf_token(c), "key": SECRET})
        assert _resolve(c, app, "anthropic") == SECRET
        r = c.post("/settings/keys/anthropic/clear", data={"csrf_token": csrf_token(c)}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/settings?saved=cleared#key-anthropic"
        assert _resolve(c, app, "anthropic") == "sk-env-fallback-key"
        page = c.get("/settings").text
        assert "Using the value from .env." in page
        assert "sk-env-fallback-key" not in page
        assert SECRET not in page


def test_typesafe_key_appears_only_with_the_jev_judge(app: FastAPI, client: TestClient) -> None:
    assert 'id="key-typesafe"' not in client.get("/settings").text
    run(client, update_runtime_settings, app.state.store, judge_provider="typesafe")
    assert 'id="key-typesafe"' in client.get("/settings").text


def test_refresh_now_runs_the_scheduler_in_the_background(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_run_now(trigger: str = "manual") -> None:
        calls.append(trigger)

    monkeypatch.setattr(app.state.scheduler, "run_now", fake_run_now)
    r = client.post("/settings/refresh", data={"csrf_token": csrf_token(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?run=started#run"
    run(client, _drain, app)
    assert calls == ["manual"]
    assert "Run started." in client.get(r.headers["location"]).text


def test_refresh_now_waits_for_a_running_run(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_run_now(trigger: str = "manual") -> None:
        calls.append(trigger)

    monkeypatch.setattr(app.state.scheduler, "run_now", fake_run_now)
    lock: asyncio.Lock = app.state.scheduler.lock
    run(client, lock.acquire)
    try:
        r = client.post("/settings/refresh", data={"csrf_token": csrf_token(client)}, follow_redirects=False)
        assert r.headers["location"] == "/settings?run=busy#run"
        assert "A run is already going." in client.get(r.headers["location"]).text
    finally:
        run(client, _release, lock)
    assert calls == []


def test_spend_meter_chart_and_last_runs(app: FastAPI, client: TestClient) -> None:
    store = app.state.store
    usage = Usage(provider="anthropic", model="claude-haiku-5-5", purpose=Purpose.REVIEW, input_tokens=100)
    run(client, store.add_spend, usage, 0.12, NOW - timedelta(hours=1))
    run_id = run(client, store.start_run, "manual", NOW - timedelta(minutes=30))
    run(client, store.finish_run, run_id, NOW - timedelta(minutes=29), {}, 0.12, "boom <x>")
    text = client.get("/settings").text
    assert '<span class="meter__spent">$0.12</span>' in text
    assert 'class="chart__bar"' in text
    assert "Wed 14 Oct: $0.12" in text
    assert "boom &lt;x&gt;" in text
    assert "30 min ago" in text
```

- [ ] **Step 3: Run the tests to see them fail**

Run: `uv run pytest tests/web/test_estimate.py tests/web/test_chart.py tests/web/test_settings_form.py tests/web/test_keys.py tests/web/test_settings.py tests/test_store_web_queries.py -q`
Expected: FAIL at collection: `ModuleNotFoundError: No module named 'ai_newsdesk.web.estimate'` (then `chart`, `parse_settings_form`, `scrub`, `usage_last_days` missing).

- [ ] **Step 4: Implement the model, store query, estimate and chart**

Append to `src/ai_newsdesk/models.py`:

```python
class UsageStats(BaseModel):
    """The last few days of real activity, used for the Settings cost estimate."""

    days: int
    runs: int
    items_reviewed: int
    review_usd: float
    write_usd: float
    briefing_usd: float
```

In `src/ai_newsdesk/store/store.py`, add `UsageStats` to the models import and add to `class Store`:

```python
    async def usage_last_days(self, now: dt.datetime, days: int = 7) -> UsageStats:
        """Finished runs, reviewed items and spend by purpose over the last ``days`` days."""
        since = _ts(now - dt.timedelta(days=days))
        runs = await self._one(
            "SELECT COUNT(*) AS n FROM runs WHERE started_at >= ? AND finished_at IS NOT NULL",
            (since,),
        )
        reviewed = await self._one(
            "SELECT COUNT(*) AS n FROM items WHERE status = 'reviewed' AND fetched_at >= ?",
            (since,),
        )
        by_purpose = {
            str(r["purpose"]): float(r["usd"])
            for r in await self._all(
                "SELECT purpose, COALESCE(SUM(cost_usd), 0) AS usd FROM spend"
                " WHERE at >= ? GROUP BY purpose",
                (since,),
            )
        }
        return UsageStats(
            days=days,
            runs=int(runs["n"]) if runs is not None else 0,
            items_reviewed=int(reviewed["n"]) if reviewed is not None else 0,
            review_usd=by_purpose.get("review", 0.0),
            write_usd=by_purpose.get("write", 0.0),
            briefing_usd=by_purpose.get("briefing", 0.0),
        )
```

`src/ai_newsdesk/web/estimate.py`:

```python
"""Estimated monthly cost for each update frequency (shown next to each option on Settings).

estimate = 30 x (min(items per run x runs per day, daily review cap)
                 x (review cost per item + writer cost per item [x 0.6 with why-it-matters off])
                 + briefing cost per day)
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_newsdesk.models import UsageStats
from ai_newsdesk.runtime_settings import RUNS_PER_DAY_CHOICES

RUNS_PER_DAY_OPTIONS: tuple[int, ...] = RUNS_PER_DAY_CHOICES  # step 1 owns the allowed values
DAYS_PER_MONTH = 30
# Spec section 5 (bake-off actuals): 500 reviews/day about $0.053, writing about $0.011/day for
# those items, briefing about $0.005/day.
DEFAULT_ITEMS_PER_RUN = 125.0
DEFAULT_REVIEW_PER_ITEM = 0.000106
DEFAULT_WRITE_PER_ITEM = 0.000022
DEFAULT_BRIEFING_PER_DAY = 0.005
WHY_OFF_WRITE_FACTOR = 0.6  # summary only: roughly 40 % fewer output tokens
MIN_RUNS_FOR_HISTORY = 3
MIN_ITEMS_FOR_HISTORY = 50


@dataclass(frozen=True)
class Rates:
    items_per_run: float
    review_per_item: float
    write_per_item: float
    briefing_per_day: float
    from_history: bool


DEFAULT_RATES = Rates(
    items_per_run=DEFAULT_ITEMS_PER_RUN,
    review_per_item=DEFAULT_REVIEW_PER_ITEM,
    write_per_item=DEFAULT_WRITE_PER_ITEM,
    briefing_per_day=DEFAULT_BRIEFING_PER_DAY,
    from_history=False,
)


def rates_from(stats: UsageStats) -> Rates:
    if stats.runs < MIN_RUNS_FOR_HISTORY or stats.items_reviewed < MIN_ITEMS_FOR_HISTORY:
        return DEFAULT_RATES
    items = stats.items_reviewed
    return Rates(
        items_per_run=items / stats.runs,
        review_per_item=stats.review_usd / items,
        write_per_item=stats.write_usd / items,
        briefing_per_day=stats.briefing_usd / max(stats.days, 1),
        from_history=True,
    )


def monthly_estimate(rates: Rates, *, runs_per_day: int, daily_cap: int, why_it_matters: bool) -> float:
    items_per_day = min(rates.items_per_run * runs_per_day, float(daily_cap))
    write = rates.write_per_item * (1.0 if why_it_matters else WHY_OFF_WRITE_FACTOR)
    return DAYS_PER_MONTH * (items_per_day * (rates.review_per_item + write) + rates.briefing_per_day)


@dataclass(frozen=True)
class FrequencyOption:
    runs: int
    label: str
    monthly: float
    over_budget: bool


def frequency_label(runs: int) -> str:
    return {1: "Once a day", 2: "Twice a day"}.get(runs, f"{runs} times a day")


def frequency_options(
    rates: Rates, *, daily_cap: int, why_it_matters: bool, budget: float
) -> list[FrequencyOption]:
    options: list[FrequencyOption] = []
    for runs in RUNS_PER_DAY_OPTIONS:
        monthly = monthly_estimate(rates, runs_per_day=runs, daily_cap=daily_cap, why_it_matters=why_it_matters)
        options.append(FrequencyOption(runs, frequency_label(runs), monthly, monthly > budget))
    return options
```

`src/ai_newsdesk/web/chart.py`:

```python
"""Geometry for the per-day spend chart, drawn as inline SVG by the template (no chart script)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

CHART_HEIGHT = 120
BAND = 12
BAR_WIDTH = 8
MIN_BAR = 2.0
TOP_PAD = 4


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    width: float
    height: float
    day: date
    value: float


@dataclass(frozen=True)
class SpendChart:
    bars: list[Bar]
    width: int
    height: int
    scale_max: float
    total: float
    peak: Bar | None


def nice_ceiling(value: float) -> float:
    """The smallest of 1, 2, 2.5, 5 x 10^k that is >= value (0.01 for nothing)."""
    if value <= 0:
        return 0.01
    base = 10.0 ** math.floor(math.log10(value))
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= step * base * (1 + 1e-9):
            return round(step * base, 10)
    return round(10 * base, 10)


def spend_chart(rows: list[tuple[str, float]], today: date) -> SpendChart:
    """One bar per day from the 1st of ``today``'s month to ``today``; other days are ignored."""
    by_day: dict[str, float] = {}
    for day, usd in rows:
        by_day[day] = by_day.get(day, 0.0) + usd
    days = [today.replace(day=d) for d in range(1, today.day + 1)]
    values = [by_day.get(d.isoformat(), 0.0) for d in days]
    scale = nice_ceiling(max(values, default=0.0))
    usable = CHART_HEIGHT - TOP_PAD
    bars: list[Bar] = []
    for i, (day, value) in enumerate(zip(days, values, strict=True)):
        height = 0.0 if value <= 0 else max(MIN_BAR, value / scale * usable)
        bars.append(
            Bar(
                x=i * BAND + (BAND - BAR_WIDTH) / 2,
                y=CHART_HEIGHT - height,
                width=BAR_WIDTH,
                height=height,
                day=day,
                value=value,
            )
        )
    peak = max((b for b in bars if b.value > 0), key=lambda b: b.value, default=None)
    return SpendChart(
        bars=bars,
        width=max(len(days), 7) * BAND,
        height=CHART_HEIGHT,
        scale_max=scale,
        total=sum(values),
        peak=peak,
    )
```

- [ ] **Step 5: Implement settings validation**

At the top of `src/ai_newsdesk/web/forms.py`, add these imports next to the existing ones:

```python
import math
from collections.abc import Mapping

from ai_newsdesk.web.estimate import RUNS_PER_DAY_OPTIONS
```

and append:

```python
BRIEFING_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
JUDGE_PROVIDERS = ("anthropic", "typesafe")


@dataclass
class SettingsForm:
    values: dict[str, object] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_settings_form(
    form: Mapping[str, str], *, models_by_provider: Mapping[str, set[str]]
) -> SettingsForm:
    """Validate the runtime-settings form. ``values`` holds typed values for valid fields only."""
    result = SettingsForm()
    values, errors = result.values, result.errors

    def integer(key: str, low: int, high: int, message: str) -> None:
        try:
            number = int(form.get(key, "").strip())
        except ValueError:
            errors[key] = message
            return
        if low <= number <= high:
            values[key] = number
        else:
            errors[key] = message

    try:
        runs = int(form.get("runs_per_day", ""))
    except ValueError:
        runs = -1
    if runs in RUNS_PER_DAY_OPTIONS:
        values["runs_per_day"] = runs
    else:
        errors["runs_per_day"] = "Choose one of the listed frequencies."

    briefing_time = form.get("briefing_time", "").strip()
    if BRIEFING_TIME_RE.match(briefing_time):
        values["briefing_time"] = briefing_time
    else:
        errors["briefing_time"] = "Use the 24-hour form HH:MM, for example 07:00."

    integer("daily_review_cap", 0, 5000, "Enter a whole number from 0 to 5000.")

    try:
        budget = float(form.get("monthly_budget_usd", "").strip())
    except ValueError:
        budget = math.nan
    if math.isfinite(budget) and 0 <= budget <= 100:
        values["monthly_budget_usd"] = round(budget, 2)
    else:
        errors["monthly_budget_usd"] = "Enter an amount from 0 to 100 dollars."

    integer("show_threshold", 0, 10, "Enter a whole number from 0 to 10.")
    integer("hn_min_points", 0, 10000, "Enter a whole number from 0 to 10000.")

    provider = form.get("judge_provider", "")
    if provider in JUDGE_PROVIDERS:
        values["judge_provider"] = provider
        judge_model = form.get("judge_model", "")
        if judge_model in models_by_provider.get(provider, set()):
            values["judge_model"] = judge_model
        else:
            errors["judge_model"] = "Choose a model that the selected judge provider offers."
    else:
        errors["judge_provider"] = "Choose a judge provider."

    claude_models = models_by_provider.get("anthropic", set())
    for key in ("writer_model", "briefing_model"):
        model = form.get(key, "")
        if model in claude_models:
            values[key] = model
        else:
            errors[key] = "Choose a Claude model from the list."

    values["why_it_matters"] = form.get("why_it_matters") == "on"
    return result
```

Key storage needs no code here: the routes call step 1's `ApiKeys` (`app.state.api_keys`) directly, and `scrub` already lives in `web/keys.py` (Task 26).

- [ ] **Step 6: Implement the routes and templates**

`src/ai_newsdesk/web/routes_settings.py`:

```python
"""Settings: runtime settings, write-only API keys, Refresh now, spend meter and chart."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from ai_newsdesk.pricing import Pricing
from ai_newsdesk.runtime_settings import (
    API_KEY_NAMES,
    ApiKeyName,
    RuntimeSettings,
    load_runtime_settings,
    update_runtime_settings,
)
from ai_newsdesk.scheduler import Scheduler
from ai_newsdesk.web.chart import spend_chart
from ai_newsdesk.web.csrf import csrf_protect
from ai_newsdesk.web.deps import api_keys_of, now_of, page, store_of, zone_of
from ai_newsdesk.web.estimate import frequency_options, rates_from
from ai_newsdesk.web.forms import parse_settings_form
from ai_newsdesk.web.keys import KEY_LABELS, MAX_KEY_CHARS, scrub
from ai_newsdesk.web.runs import run_dict

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])
log = logging.getLogger("ai_newsdesk.web.settings")

SAVED = {
    "settings": "Settings saved.",
    "key": "Key tested and saved.",
    "cleared": "Key cleared. The .env value applies now, if there is one.",
}
RUN = {
    "started": "Run started. New stories appear on Today when it finishes.",
    "busy": "A run is already going. Try again when it has finished.",
}
FIELDS = (
    "runs_per_day",
    "briefing_time",
    "daily_review_cap",
    "monthly_budget_usd",
    "judge_provider",
    "judge_model",
    "writer_model",
    "briefing_model",
    "show_threshold",
    "hn_min_points",
)


def _models_by_provider(pricing: Pricing) -> dict[str, set[str]]:
    return {provider: set(pricing.models(provider)) for provider in ("anthropic", "typesafe")}


def _model_options(pricing: Pricing) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for model_id in pricing.models():
        price = pricing.price(model_id)
        options.append(
            {
                "id": model_id,
                "provider": price.provider,
                "label": f"{model_id} (${price.input_per_mtok:g} in, ${price.output_per_mtok:g} out per million tokens)",
            }
        )
    return options


def _values_from(runtime: RuntimeSettings) -> dict[str, str]:
    values = {key: str(getattr(runtime, key)) for key in FIELDS}
    values["monthly_budget_usd"] = f"{runtime.monthly_budget_usd:.2f}"
    values["why_it_matters"] = "on" if runtime.why_it_matters else ""
    return values


def _values_echo(form: dict[str, str]) -> dict[str, str]:
    values = {key: form.get(key, "") for key in FIELDS}
    values["why_it_matters"] = "on" if form.get("why_it_matters") == "on" else ""
    return values


def _parse_iso(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def _key_name(name: str) -> ApiKeyName:
    if name not in API_KEY_NAMES:
        raise HTTPException(status_code=404, detail="There is no such key.")
    return cast(ApiKeyName, name)


async def _render(
    request: Request,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    key_errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    store = store_of(request)
    settings = request.app.state.settings
    pricing: Pricing = request.app.state.pricing
    now = now_of(request)
    today = now.astimezone(zone_of(request)).date()
    runtime = await load_runtime_settings(store)
    form = values if values is not None else _values_from(runtime)
    rates = rates_from(await store.usage_last_days(now))
    spent = await store.month_spend(now, settings.tz)
    budget = runtime.monthly_budget_usd
    show_typesafe = runtime.judge_provider == "typesafe" or form.get("judge_provider") == "typesafe"
    api_keys = api_keys_of(request)
    # Step 1's ApiKeyStatus: name, set_in_ui, set_in_env, updated_at, is_set. Never the key.
    statuses = [await api_keys.api_key_status(name) for name in API_KEY_NAMES]
    keys = [k for k in statuses if k.name != "typesafe" or show_typesafe or k.set_in_ui]
    runs: list[dict[str, Any]] = []
    for record in await store.last_runs(5):
        data = run_dict(record)
        data["started"] = _parse_iso(data["started_at"])
        runs.append(data)
    return await page(
        request,
        "settings.html",
        {
            "active": "settings",
            "values": form,
            "errors": errors or {},
            "key_errors": key_errors or {},
            "frequency_options": frequency_options(
                rates,
                daily_cap=runtime.daily_review_cap,
                why_it_matters=runtime.why_it_matters,
                budget=budget,
            ),
            "rates": rates,
            "models": _model_options(pricing),
            "spent": spent,
            "spent_pct": 100.0 if budget <= 0 else min(100.0, 100.0 * spent / budget),
            "over_budget": spent >= budget,
            "month_label": f"{today:%B}",
            "chart": spend_chart(await store.spend_by_day(now, settings.tz), today),
            "keys": keys,
            "key_labels": KEY_LABELS,
            "runs": runs,
            "flash": SAVED.get(request.query_params.get("saved", "")),
            "run_flash": RUN.get(request.query_params.get("run", "")),
        },
        status_code=status_code,
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return await _render(request)


@router.post("/settings")
async def save_settings(request: Request) -> Response:
    raw = await request.form()
    form = {key: value for key, value in raw.items() if isinstance(value, str)}
    parsed = parse_settings_form(form, models_by_provider=_models_by_provider(request.app.state.pricing))
    if not parsed.ok:
        return await _render(request, values=_values_echo(form), errors=parsed.errors, status_code=422)
    try:
        # Step 1 validates everything again and saves nothing on error.
        await update_runtime_settings(store_of(request), **parsed.values)
    except ValidationError as exc:
        errors = {str(e["loc"][0]) if e["loc"] else "form": "This value was refused." for e in exc.errors()}
        return await _render(request, values=_values_echo(form), errors=errors, status_code=422)
    await request.app.state.rebuild_context()
    return RedirectResponse("/settings?saved=settings", status_code=303)


@router.post("/settings/keys/{name}")
async def save_api_key(request: Request, name: str, key: Annotated[str, Form()] = "") -> Response:
    key_name = _key_name(name)
    candidate = key.strip()
    error: str | None
    if not candidate:
        error = "Paste a key first."
    elif len(candidate) > MAX_KEY_CHARS or any(ch.isspace() for ch in candidate):
        error = "That does not look like an API key."
    else:
        tester = request.app.state.key_testers.get(key_name)
        if tester is None:
            error = "No test is configured for this key, so it was not saved."
        else:
            try:
                result = await tester(candidate, await load_runtime_settings(store_of(request)))
            except Exception as exc:  # a tester must never leak the key through a traceback
                result = f"The test call failed ({type(exc).__name__})."
            error = None if result is None else scrub(result, candidate)
    if error is not None:
        log.info("API key not saved: test failed for %s", key_name)
        return await _render(request, key_errors={key_name: error}, status_code=422)
    await api_keys_of(request).set_api_key(key_name, candidate, now_of(request))
    await request.app.state.rebuild_context()
    return RedirectResponse(f"/settings?saved=key#key-{key_name}", status_code=303)


@router.post("/settings/keys/{name}/clear")
async def clear_api_key(request: Request, name: str) -> Response:
    key_name = _key_name(name)
    await api_keys_of(request).clear_api_key(key_name)
    await request.app.state.rebuild_context()
    return RedirectResponse(f"/settings?saved=cleared#key-{key_name}", status_code=303)


async def _run_now(scheduler: Scheduler) -> None:
    try:
        # Step 3 (R6): returns None without running if a run started in the meantime.
        if await scheduler.run_now("manual") is None:
            log.info("manual run skipped or produced no report")
    except Exception:
        log.exception("manual run failed")


@router.post("/settings/refresh")
async def refresh_now(request: Request) -> Response:
    app: FastAPI = request.app
    scheduler: Scheduler = app.state.scheduler
    if scheduler.lock.locked():
        return RedirectResponse("/settings?run=busy#run", status_code=303)
    task = asyncio.create_task(_run_now(scheduler), name="manual-run")
    app.state.background.add(task)
    task.add_done_callback(app.state.background.discard)
    return RedirectResponse("/settings?run=started#run", status_code=303)
```

`src/ai_newsdesk/web/templates/partials/spend_chart.html`:

```html
<figure class="chart-figure" aria-labelledby="spend-caption">
  <figcaption class="chart-caption" id="spend-caption"><span>Spend per day in {{ month_label }}</span><span>Bars scale to {{ usd(chart.scale_max) }} a day</span></figcaption>
  <svg class="chart" viewBox="0 0 {{ chart.width }} {{ chart.height }}" role="img" aria-labelledby="spend-title spend-desc">
    <title id="spend-title">Spend per day in {{ month_label }}</title>
    <desc id="spend-desc">{% if chart.peak %}{{ usd(chart.total) }} so far this month. Highest day: {{ short_date(chart.peak.day) }}, {{ usd(chart.peak.value) }}.{% else %}Nothing spent yet this month.{% endif %}</desc>
    <line class="chart__grid" x1="0" x2="{{ chart.width }}" y1="0.5" y2="0.5"></line>
    {% for bar in chart.bars %}
    {% if bar.height > 0 %}
    <rect class="chart__bar" x="{{ bar.x }}" y="{{ '%.2f'|format(bar.y) }}" width="{{ bar.width }}" height="{{ '%.2f'|format(bar.height) }}" rx="2"><title>{{ short_date(bar.day) }}: {{ usd(bar.value) }}</title></rect>
    {% else %}
    <rect class="chart__zero" x="{{ bar.x }}" y="{{ chart.height - 1 }}" width="{{ bar.width }}" height="1"><title>{{ short_date(bar.day) }}: nothing spent</title></rect>
    {% endif %}
    {% endfor %}
    <line class="chart__axis" x1="0" x2="{{ chart.width }}" y1="{{ chart.height - 0.5 }}" y2="{{ chart.height - 0.5 }}"></line>
  </svg>
  <div class="chart-axis" aria-hidden="true"><span>{{ short_date(chart.bars[0].day) }}</span><span>{{ short_date(chart.bars[-1].day) }}</span></div>
  <details class="more">
    <summary>Show as a table</summary>
    <table class="table">
      <thead><tr><th scope="col">Day</th><th scope="col">Spend</th></tr></thead>
      <tbody>
        {% for bar in chart.bars %}
        <tr><td>{{ short_date(bar.day) }}</td><td>{{ usd(bar.value) }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </details>
</figure>
```

`src/ai_newsdesk/web/templates/settings.html`:

```html
{% extends "base.html" %}
{% block title %}Settings on Newsdesk{% endblock %}
{% block content %}
{% macro aria(name, hint=false) %}{% if hint or errors.get(name) %} aria-describedby="{% if hint %}{{ name }}-hint{% endif %}{% if hint and errors.get(name) %} {% endif %}{% if errors.get(name) %}{{ name }}-error{% endif %}"{% endif %}{% if errors.get(name) %} aria-invalid="true"{% endif %}{% endmacro %}
{% macro error(name) %}{% if errors.get(name) %}<p class="field__error" id="{{ name }}-error">{{ errors[name] }}</p>{% endif %}{% endmacro %}
<header class="page-head">
  <h1>Settings</h1>
  <p class="lede">How often the desk reads, how much it may spend, and which models do the work.</p>
</header>
{% if flash %}
<div class="notice notice--ok" role="status"><p>{{ flash }}</p></div>
{% endif %}
{% if errors %}
<div class="notice notice--error" role="alert"><p>Nothing was saved. Fix the {{ "field" if errors|length == 1 else errors|length ~ " fields" }} marked below.</p></div>
{% endif %}
<div class="settings">
  <form class="settings" method="post" action="/settings">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">

    <section class="panel" id="schedule" aria-labelledby="schedule-title">
      <div class="panel__head">
        <h2 id="schedule-title">Schedule</h2>
        <p>Each run collects every source, reviews what is new and writes the summaries.</p>
      </div>
      <div class="panel__body">
        <div class="field">
          <label for="runs_per_day">Update frequency</label>
          <select id="runs_per_day" name="runs_per_day"{{ aria("runs_per_day", true) }}>
            {% for opt in frequency_options %}
            <option value="{{ opt.runs }}"{% if values.runs_per_day == opt.runs|string %} selected{% endif %}>{{ opt.label }}, about {{ usd(opt.monthly) }} a month{% if opt.over_budget %} (over budget){% endif %}</option>
            {% endfor %}
          </select>
          <p class="field__hint" id="runs_per_day-hint">{% if rates.from_history %}Estimates use the last 7 days: about {{ rates.items_per_run|round|int }} items reviewed a run.{% else %}Estimates use starting figures until there are a few days of history.{% endif %}</p>
          {{ error("runs_per_day") }}
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="briefing_time">Briefing time</label>
            <input id="briefing_time" name="briefing_time" type="time" value="{{ values.briefing_time }}" required{{ aria("briefing_time", true) }}>
            <p class="field__hint" id="briefing_time-hint">Local time, every morning.</p>
            {{ error("briefing_time") }}
          </div>
          <div class="field">
            <label for="daily_review_cap">Daily review cap</label>
            <input id="daily_review_cap" name="daily_review_cap" type="number" min="0" max="5000" step="1" inputmode="numeric" value="{{ values.daily_review_cap }}" required{{ aria("daily_review_cap", true) }}>
            <p class="field__hint" id="daily_review_cap-hint">Most items the model reviews in a day.</p>
            {{ error("daily_review_cap") }}
          </div>
        </div>
      </div>
    </section>

    <section class="panel" id="spending" aria-labelledby="spending-title">
      <div class="panel__head">
        <h2 id="spending-title">Spending</h2>
        <p>A hard cap. Every model call is checked against it first; a call that might not fit is skipped.</p>
      </div>
      <div class="panel__body">
        <div class="meter{% if over_budget %} meter--over{% endif %}">
          <p class="meter__figures"><span class="meter__spent">{{ usd(spent) }}</span><span class="meter__of">of {{ usd(runtime.monthly_budget_usd) }} spent in {{ month_label }}</span></p>
          <svg class="meter__bar" viewBox="0 0 100 4" preserveAspectRatio="none" role="img" aria-label="{{ spent_pct|round|int }} percent of this month's budget used">
            <rect class="meter__track" width="100" height="4"></rect>
            <rect class="meter__fill" width="{{ '%.2f'|format(spent_pct) }}" height="4"></rect>
          </svg>
        </div>
        <div class="field">
          <label for="monthly_budget_usd">Monthly budget in dollars</label>
          <input id="monthly_budget_usd" name="monthly_budget_usd" type="number" min="0" max="100" step="0.01" inputmode="decimal" value="{{ values.monthly_budget_usd }}" required{{ aria("monthly_budget_usd", true) }}>
          <p class="field__hint" id="monthly_budget_usd-hint">Covers every provider. Resets on the 1st, UK time.</p>
          {{ error("monthly_budget_usd") }}
        </div>
        {% include "partials/spend_chart.html" %}
      </div>
    </section>

    <section class="panel" id="models" aria-labelledby="models-title">
      <div class="panel__head">
        <h2 id="models-title">Models</h2>
        <p>Prices come from config/pricing.yaml. Changes apply from the next run.</p>
      </div>
      <div class="panel__body">
        <div class="field">
          <label for="judge_provider">Judge</label>
          <select id="judge_provider" name="judge_provider"{{ aria("judge_provider") }}>
            <option value="anthropic"{% if values.judge_provider == "anthropic" %} selected{% endif %}>Claude (Anthropic)</option>
            <option value="typesafe"{% if values.judge_provider == "typesafe" %} selected{% endif %}>Jev (TypeSafe)</option>
          </select>
          {{ error("judge_provider") }}
        </div>
        <div class="field">
          <label for="judge_model">Judge model</label>
          <select id="judge_model" name="judge_model"{{ aria("judge_model") }}>
            {% for m in models %}
            <option value="{{ m.id }}"{% if values.judge_model == m.id %} selected{% endif %}>{{ m.label }}</option>
            {% endfor %}
          </select>
          {{ error("judge_model") }}
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="writer_model">Summary writer</label>
            <select id="writer_model" name="writer_model"{{ aria("writer_model") }}>
              {% for m in models if m.provider == "anthropic" %}
              <option value="{{ m.id }}"{% if values.writer_model == m.id %} selected{% endif %}>{{ m.label }}</option>
              {% endfor %}
            </select>
            {{ error("writer_model") }}
          </div>
          <div class="field">
            <label for="briefing_model">Briefing writer</label>
            <select id="briefing_model" name="briefing_model"{{ aria("briefing_model") }}>
              {% for m in models if m.provider == "anthropic" %}
              <option value="{{ m.id }}"{% if values.briefing_model == m.id %} selected{% endif %}>{{ m.label }}</option>
              {% endfor %}
            </select>
            {{ error("briefing_model") }}
          </div>
        </div>
      </div>
    </section>

    <section class="panel" id="reading" aria-labelledby="reading-title">
      <div class="panel__head">
        <h2 id="reading-title">Reading</h2>
        <p>What reaches the Today page.</p>
      </div>
      <div class="panel__body">
        <div class="check">
          <input id="why_it_matters" name="why_it_matters" type="checkbox"{% if values.why_it_matters == "on" %} checked{% endif %} aria-describedby="why_it_matters-hint">
          <div>
            <label for="why_it_matters">Write "why it matters"</label>
            <p class="field__hint" id="why_it_matters-hint">One extra sentence per story. Off saves output tokens; lines already written come back if you turn it on again.</p>
          </div>
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="show_threshold">Show stories scoring at least</label>
            <input id="show_threshold" name="show_threshold" type="number" min="0" max="10" step="1" inputmode="numeric" value="{{ values.show_threshold }}" required{{ aria("show_threshold", true) }}>
            <p class="field__hint" id="show_threshold-hint">Out of 10. 4 was the best balance in testing.</p>
            {{ error("show_threshold") }}
          </div>
          <div class="field">
            <label for="hn_min_points">Hacker News points needed</label>
            <input id="hn_min_points" name="hn_min_points" type="number" min="0" max="10000" step="1" inputmode="numeric" value="{{ values.hn_min_points }}" required{{ aria("hn_min_points") }}>
            {{ error("hn_min_points") }}
          </div>
        </div>
      </div>
    </section>

    <div class="form__actions">
      <button class="button" type="submit">Save settings</button>
    </div>
  </form>

  <section class="panel" id="keys" aria-labelledby="keys-title">
    <div class="panel__head">
      <h2 id="keys-title">API keys</h2>
      <p>Keys are tested before they are saved, stored encrypted, and never shown again. A key saved here wins over the one in .env.</p>
    </div>
    <div class="panel__body">
      {% for k in keys %}
      {% set label = key_labels[k.name] %}
      <div class="key" id="key-{{ k.name }}">
        <div class="key__head">
          <h3>{{ label }}</h3>
          <span class="tag{% if k.is_set %} tag--ok{% endif %}">{{ "Set" if k.is_set else "Not set" }}</span>
        </div>
        <p class="field__hint">{% if k.set_in_ui %}Saved here {{ ago(k.updated_at, now) }}.{% elif k.set_in_env %}Using the value from .env.{% else %}No key here or in .env.{% endif %}</p>
        <form class="key__form" method="post" action="/settings/keys/{{ k.name }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <label class="visually-hidden" for="key-input-{{ k.name }}">New {{ label }} key</label>
          <input id="key-input-{{ k.name }}" name="key" type="password" autocomplete="off" spellcheck="false" placeholder="Paste a new key"{% if key_errors.get(k.name) %} aria-invalid="true" aria-describedby="key-error-{{ k.name }}"{% endif %}>
          <button class="button" type="submit">Test and save</button>
        </form>
        {% if key_errors.get(k.name) %}
        <p class="field__error" id="key-error-{{ k.name }}" role="alert">Not saved. {{ key_errors[k.name] }}</p>
        {% endif %}
        {% if k.set_in_ui %}
        <form method="post" action="/settings/keys/{{ k.name }}/clear">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <button class="link-button" type="submit">Clear and use .env</button>
        </form>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </section>

  <section class="panel" id="run" aria-labelledby="run-title">
    <div class="panel__head">
      <h2 id="run-title">Run now</h2>
      <p>Collect and review straight away instead of waiting for the next slot. Runs never overlap.</p>
    </div>
    <div class="panel__body">
      {% if run_flash %}
      <div class="notice notice--ok" role="status"><p>{{ run_flash }}</p></div>
      {% endif %}
      <form method="post" action="/settings/refresh">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button class="button" type="submit"><svg class="icon" aria-hidden="true"><use href="/static/icons.svg#i-refresh"></use></svg>Refresh now</button>
      </form>
      {% if runs %}
      <ul class="runs" aria-label="Recent runs">
        {% for r in runs %}
        <li>
          <time datetime="{{ r.started_at }}">{{ ago(r.started, now) }}</time>
          <span>{{ r.trigger }}</span>
          <span>{{ usd(r.cost_usd or 0) }}</span>
          {% if r.error %}<span class="error">{{ r.error|truncate(160, true) }}</span>{% elif not r.finished_at %}<span>running</span>{% else %}<span>finished</span>{% endif %}
        </li>
        {% endfor %}
      </ul>
      {% endif %}
    </div>
  </section>
</div>
{% endblock %}
```

`src/ai_newsdesk/web/app_routes.py`: import `routes_settings` and add it to the tuple (final form of the file):

```python
"""All page routers in one place (login and logout are installed by `web.security`)."""

from __future__ import annotations

from fastapi import APIRouter

from ai_newsdesk.web import routes_briefing, routes_health, routes_settings, routes_sources, routes_today


def build_router() -> APIRouter:
    router = APIRouter()
    for sub in (
        routes_health.router,
        routes_today.router,
        routes_briefing.router,
        routes_sources.router,
        routes_settings.router,
    ):
        router.include_router(sub)
    return router
```

`tests/web/dev_seed.py` (demo content for the visual check; fictional names only):

```python
"""Fill a development database with fictional stories, a briefing, spend and runs.

Usage: uv run python -m tests.web.dev_seed data/dev.sqlite
Never point it at the production database.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_newsdesk.models import Category, ItemStatus, Purpose, SourceKind, Trust, Usage
from ai_newsdesk.store import Store
from tests.web import seed

STORIES = [
    (Category.MODEL_RELEASE, 9, "Northwind AI releases a small model that matches last year's flagship",
     "The new model runs on a single consumer GPU and scores within two points of the larger one on common reasoning tests. Weights are available under a research licence.",
     "Strong local models change what is worth paying an API for."),
    (Category.RESEARCH, 8, "Halcyon Research shows long-context recall drops sharply after 200k tokens",
     "A new benchmark hides facts at many depths in long documents. Every tested model loses accuracy well before its advertised limit.",
     "Context-window numbers on spec sheets overstate what you can rely on."),
    (Category.PRODUCT, 7, "Pinecrest Labs adds offline mode to its coding assistant",
     "The assistant can now index and answer questions about a repository without a network connection, using a bundled small model.",
     None),
    (Category.POLICY, 6, "Regulators publish draft guidance on labelling AI-generated images",
     "The draft asks large platforms to attach provenance metadata and to show a visible label when an image is mostly machine-made.",
     "Labelling rules will shape how image tools are built and shipped."),
    (Category.INDUSTRY, 5, "Two inference start-ups merge to share data-centre capacity",
     "The combined company will run one scheduling layer across both fleets and keep both brands for now.",
     None),
    (Category.RESEARCH, 6, "A simple trick cuts hallucinated citations in summaries by half",
     "Asking the model to quote the supporting sentence before writing each claim reduced invented sources in a controlled study.",
     "Cheap prompt-level fixes still move the needle."),
    (Category.OPINION, 4, "An essay argues evals should be published with their failure cases",
     "The author proposes that every benchmark result ship with a sample of the items the model got wrong.",
     None),
]


async def main(db_path: str) -> None:
    now = datetime.now(UTC)
    store = Store(db_path)
    await store.init()
    try:
        await store.seed_sources_if_empty(Path("config/sources.yaml"))
        lab = await seed.add_source(store, name="Northwind AI blog")
        paper = await seed.add_source(store, name="Halcyon Research", trust=Trust.TRUSTED)
        person = await seed.add_source(
            store, name="A respected researcher", kind=SourceKind.X_PERSON, trust=Trust.TRUSTED,
            config={"handle": "example_researcher"},
        )
        ids: list[int] = []
        for n, (category, signal, headline, summary, why) in enumerate(STORIES):
            sources = [(lab, f"https://northwind.example/post-{n}", headline)]
            if n < 2:
                sources.append((paper, f"https://halcyon.example/note-{n}", f"Notes on: {headline}"))
                sources.append((person, f"https://x.com/example_researcher/status/{1000 + n}", "Thread with a summary"))
            ids.append(
                await seed.add_story(
                    store, at=now - timedelta(hours=1 + n * 2), headline=headline, sources=sources,
                    category=category, signal=signal, summary=summary, why=why,
                )
            )
        await seed.add_item(
            store, source_id=lab, url="https://northwind.example/unreviewed", title="An item the judge could not review",
            at=now - timedelta(hours=1), status=ItemStatus.REVIEW_FAILED,
        )
        # Step 3's markdown-lite: "## " headings, blank-line paragraphs, [text](story url) links.
        def url(n: int) -> str:
            return f"https://northwind.example/post-{n}"

        body = (
            "Small models and honest benchmarks lead the day.\n\n"
            "## Model releases\n"
            f"Northwind AI [released a small model]({url(0)}) that comes close to last year's flagship. "
            "It runs on one consumer GPU.\n\n"
            "## Research\n"
            f"[Long-context recall falls off]({url(1)}) well before advertised limits, and "
            f"[quoting sources before writing claims]({url(5)}) halves invented citations.\n\n"
            "## Policy\n"
            f"[Draft guidance on labelling AI images]({url(3)}) asks platforms for provenance metadata."
        )
        local_day = now.astimezone(ZoneInfo("Europe/London")).date()
        await store.save_briefing(local_day, "claude-haiku-5-5", body, ids[:6], now - timedelta(hours=2))
        usage = Usage(provider="anthropic", model="claude-haiku-5-5", purpose=Purpose.REVIEW, input_tokens=1000)
        for day in range(1, now.day + 1):
            at = now.replace(day=day, hour=6, minute=0, second=0, microsecond=0)
            if at <= now:
                await store.add_spend(usage, 0.04 + (day % 5) * 0.012, at)
        for hours_ago, error in ((18, None), (12, None), (6, "research-engine timed out for 2 sources")):
            run_id = await store.start_run("schedule", now - timedelta(hours=hours_ago))
            await store.finish_run(run_id, now - timedelta(hours=hours_ago) + timedelta(minutes=3), {}, 0.05, error)
    finally:
        await store.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].endswith(".sqlite"):
        raise SystemExit("usage: python -m tests.web.dev_seed data/dev.sqlite")
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 7: Run the tests to see them pass**

Run: `uv run pytest tests/web tests/test_store_web_queries.py tests/test_cli.py -q`
Expected: all pass. The route-policy test now covers the four Settings POST routes, and the design-system test checks `/settings`.
Run: `make test`
Expected: ruff, ruff format check, pyright and the whole pytest suite pass.

- [ ] **Step 8: Commit**

```bash
git add src/ai_newsdesk/models.py src/ai_newsdesk/store/store.py src/ai_newsdesk/web tests/web tests/test_store_web_queries.py
git commit -m "feat(web): Settings with cost estimates, write-only keys, Refresh now and spend chart (V1-S5)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
## Step done when

- [ ] All seven tasks are committed on `step-5-web`, one commit each, every message ending with the `Co-Authored-By` trailer.
- [ ] `make test` is green: ruff, ruff format check, pyright and the full pytest suite (including `tests/web/test_route_policy.py`, which proves every page needs a session and every POST needs CSRF).
- [ ] `grep -rn "|safe" src/ai_newsdesk/web/templates` prints nothing, and `grep -rn "style=" src/ai_newsdesk/web/templates` prints nothing.
- [ ] `sha256sum src/ai_newsdesk/web/static/htmx.min.js` prints `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`.
- [ ] No lab IPs, real hostnames or usernames in any new file (`git diff main --stat` reviewed; gitleaks passes in pre-commit).
- [ ] **Manual visual check (light and dark, 390 px and 1280 px).** On the VM, from the repo root, with a throwaway dev database (never the production one):

  ```bash
  mkdir -p data
  export DB_PATH=data/dev.sqlite SCHEDULER_ENABLED=false GIT_SHA=dev TZ=Europe/London
  export SECRETS_KEY="$(openssl rand -base64 48)" SESSION_SECRET="$(openssl rand -base64 48)"
  export OWNER_PASSWORD_HASH="$(uv run python -c 'from ai_newsdesk.web.auth import hash_password; print(hash_password("dev-only-password"))')"
  uv run python -m tests.web.dev_seed data/dev.sqlite
  uv run uvicorn --factory ai_newsdesk.app:create_app --host 127.0.0.1 --port 8077
  ```

  From the laptop: `ssh -L 8077:127.0.0.1:8077 <the VM>` and open `http://localhost:8077` in Chrome (browsers treat `localhost` as secure, so the `Secure` cookies work over plain http there). Log in with `dev-only-password`. In DevTools, use the device toolbar for **390 × 844** and a **1280 × 800** window, and Rendering → "Emulate CSS media feature prefers-color-scheme" for **light** and **dark**. Take screenshots of Today (Top and By section), Briefing, Sources (with one row in edit mode) and Settings (with the chart table open) in all four combinations (16 images), and attach them to the PR. Check by eye:
  - the amber gauge on each story matches its "Signal N"; the lead story's headline is larger; nothing overflows horizontally at 390 px; the nav sits on its own row at 390 px;
  - thumbs toggle in place with no layout shift; "All N sources" expands; the section index is sticky at 1280 px in By section;
  - dark mode has no unreadable text or invisible borders; the focus ring (Tab through a page) is visible in both themes;
  - with "Reduce motion" emulated, nothing animates.
  Stop uvicorn and delete `data/dev.sqlite` afterwards.
- [ ] PR opened from `step-5-web` with any drift from the "Names used from steps 0–4" table listed, the screenshots attached, and the description ending with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
