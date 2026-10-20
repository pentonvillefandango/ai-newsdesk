# Briefing read aloud (ElevenLabs) and a "briefing failed" note: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- When today's briefing is missing, the briefing page says so and offers **Write it now**.
- After each briefing is written, it is turned into an MP3 with ElevenLabs and played on the briefing page. Settings chooses the voice (three suggestions plus every account voice, with free samples), the model and the monthly character cap.

**Architecture:**
- A pure `narration.py` turns the stored markdown-lite briefing into speech text and splits it into chunks.
- A `Narrator` protocol has one concrete class, `ElevenLabsNarrator` (httpx).
- `pipeline.run_narration` is the single code path for an audio job: build the text, check the quota, narrate, record the characters used, then save the file and its row. The scheduler, the "Write it now" task and the "Make audio" button all call it.
- Audio files live beside the SQLite database on the `app-data` volume. A `briefing_audio` row describes each file, and a `tts_usage` ledger is the character meter.

**Tech stack:** Python 3.13, FastAPI, Starlette `FileResponse` (Range support), Jinja2 with autoescape, htmx, aiosqlite, httpx, pydantic, pytest and respx/`httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-10-20-briefing-audio-design.md`. Read it fully. It is binding, and this plan argues from it.

## Global constraints

- **TDD.** Write the failing test first and watch it fail. `make test` (ruff, ruff format --check, pyright, pytest) must be green before every commit.
- **Commits.** Conventional Commits with scope `(feedback-3)`, for example `feat(narration): speech text from the briefing (feedback-3)`. Trailer: `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- **Tests never call real APIs.** Use `httpx.MockTransport` or a fake `Narrator`. Never read the real `.env`.
- **Follow the existing patterns:**
  - store access through `_all`, `_one` and `_tx`; aware UTC datetimes;
  - router-level session and CSRF on POSTs; the background-task-plus-303 pattern of `routes_settings.refresh_now`;
  - htmx partial plus a no-JS fallback; Jinja autoescape, and never `|safe` on fetched or model text;
  - CSP `default-src 'self'; script-src 'self'; style-src 'self'`: no inline script or style, and no third-party media origins.
- **Keys.** Never log, print, return or commit key values. Scrub every error (`keys.scrub`, `describe_error`). The ElevenLabs key goes only to `api.elevenlabs.io`, and to `*.elevenlabs.io` for previews.
- **Protocols.** Only `app.py` wires concrete classes (`ElevenLabsNarrator`).
- **Docs in the same commit as the code.** `tests/test_docs.py` and `tests/test_compose_policy.py` must stay green: the README config table lists every `.env.example` key, and Compose maps keys explicitly.
- **Do not** push, open PRs, deploy, touch Compose project `ai-newsdesk`, `/opt/caddy`, other tools or the real `.env`, or dispatch subagents.

**Owner decisions (already made, 2026-10-20):**
- "the whole briefing", listened to "in my office, on macbook"; the Today page is for going deeper.
- Audio is made **ready after the scheduled briefing**, not on first Play.
- "yes, drop the sources": a citation link that ends a sentence is not read.
- "those voices are fine to test, but put a full drop down in settings": Daniel, Lily and Matilda are suggested first, then every account voice.
- The account is ElevenLabs Creator, with the key already in `.env` as `ELEVENLABS_API_KEY`. Plan limit: 363,000 characters a month.
- Failure note: "Yes, add it", done before the audio work.

---

### Task 1: The briefing failure note and "Write it now"

**Goal:** `GET /briefing` shows a notice when today's briefing is missing after `briefing_time`, with the scheduler's attempt state and a **Write it now** button that writes it in the background.

**Files:**
- Modify: `src/ai_newsdesk/scheduler.py` (read-only status: `briefing_status(day) -> BriefingStatus`)
- Modify: `src/ai_newsdesk/web/routes_briefing.py` (the notice context; `POST /briefing/write`)
- Modify: `src/ai_newsdesk/web/templates/briefing.html` (the notice block)
- Modify: `src/ai_newsdesk/static/*.css` (only if needed, reusing existing notice styles)
- Test: `tests/test_scheduler.py`, `tests/web/test_briefing_page.py` (or the existing briefing web test file)

**Acceptance criteria:**
- [ ] The notice shows only on `/briefing` (never on `/briefing/{day}`), when local today has no briefing and local time ≥ `briefing_time`.
- [ ] The notice reads "Today's briefing isn't ready. Showing the one from {Weekday d Month}." The "Showing…" sentence is left out when there's no briefing at all.
- [ ] While attempts remain the notice says "Tried {n} of 6 times; the last try failed." (only when n ≥ 1). After 6 attempts it says "The scheduler has stopped trying today."
- [ ] `POST /briefing/write` (CSRF) behaves like this:
  - when the scheduler lock is free, it starts a background task and redirects with 303 to `/briefing?writing=1`, which shows "Writing today's briefing. This takes about a minute; reload to see it.";
  - when the lock is held, it redirects to `/briefing?writing=busy`, which shows "A run is in progress; try again in a few minutes.", and starts nothing.
- [ ] The task runs `pipeline.run_briefing(ctx, today)` under the scheduler lock, and logs and swallows exceptions. Task 6 adds narration after it.
- [ ] Scheduler status is read-only and never changes the scheduler's retry state.

**Verify:** `uv run pytest tests/test_scheduler.py tests/web -q -k "briefing"` → all pass; then `make test` → green.

**Steps:**

- [ ] **Step 1: Write the failing scheduler test.**

```python
async def test_briefing_status_reports_tries_and_give_up(...):
    # Drive tick() with a failing briefing_fn past briefing_time, as the existing
    # retry tests do. Then:
    st = sched.briefing_status(day)
    assert (st.tries, st.max_tries, st.gave_up) == (1, 6, False)
    # ...advance the clock 30 min five more times...
    assert sched.briefing_status(day).gave_up is True
    assert sched.briefing_status(other_day).tries == 0  # another day: no attempts
```

- [ ] **Step 2: Run it and watch it fail** (`AttributeError: briefing_status`).
- [ ] **Step 3: Implement it.**

```python
@dataclass(frozen=True)
class BriefingStatus:
    tries: int
    max_tries: int
    gave_up: bool

def briefing_status(self, day: date) -> BriefingStatus:
    tries = self._briefing_tries if day == self._briefing_try_day else 0
    return BriefingStatus(tries, BRIEFING_MAX_ATTEMPTS, tries >= BRIEFING_MAX_ATTEMPTS)
```

- [ ] **Step 4: Write the failing web tests.**
  - The notice shows when it should, and not on `/briefing/2026-10-19`.
  - No notice before `briefing_time`, and none when today's briefing exists.
  - The tries and gave-up wording.
  - A POST with the lock free returns 303 to `?writing=1`, and a fake `run_briefing` is called once with today.
  - A POST with the lock held returns 303 to `?writing=busy` and nothing is called.
  - A POST without the CSRF token returns 403.
  - Use the existing web test fixtures (`tests/web/`, `dev_seed.py` patterns), and a fixed clock with `TZ=Europe/London`.
- [ ] **Step 5: Implement the route and template.**
  - Compute `today = clock.now().astimezone(tz).date()` and `due = local time ≥ parse_hhmm(rs.briefing_time)`.
  - `notice` is shown when `due` and there is no `get_briefing(today)`.
  - Pass `briefing_status` from `request.app.state.scheduler`. It may be `None` in tests: then show no tries line.
  - In the template, the button is a `<form method="post" action="/briefing/write">` with the CSRF field, as other forms have.
- [ ] **Step 6: Run `make test` until it is green, then commit:** `feat(briefing): say when today's briefing failed, with Write it now (feedback-3)`.

---

### Task 2: Speech text (`src/ai_newsdesk/narration.py`)

**Goal:** Pure functions that turn a stored briefing body into speech text and chunks of at most 9,000 characters, following spec 4.1.

**Files:**
- Create: `src/ai_newsdesk/narration.py`
- Test: `tests/test_narration.py`

**Acceptance criteria:**
- [ ] `speech_text(body, day_label)` starts with `"AI Newsdesk briefing for {day_label}."` and a blank line.
- [ ] `## X` becomes the paragraph `X.`, and is not doubled when X already ends in `.`, `!` or `?`.
- [ ] Citation links are dropped together with their leading whitespace. The cases: a single link before `.`; two or more links in a row before `.`; a link before `)`; a link at the end of a paragraph; a link before `!` or `?`.
- [ ] Inline links become their text: "[A](u1) and [B](u2) agreed." becomes "A and B agreed."
- [ ] Leftover `*`, `_`, backticks and leading `#` are removed. Bare `http(s)://` URLs are removed. Spaces collapse, and paragraphs are separated by exactly one blank line.
- [ ] `chunks(text, limit=9000)` returns `[text]` when it fits. Otherwise it splits at paragraph breaks, then at sentence ends, then with a hard cut. Every chunk is ≤ limit, no chunk is empty, and `"".join` of the stripped chunks keeps every word in order.

**Verify:** `uv run pytest tests/test_narration.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** (parametrised examples).

```python
from ai_newsdesk.narration import chunks, speech_text

LABEL = "Tuesday 20 October 2026"

@pytest.mark.parametrize(("body", "want"), [
    ("Quiet day.", "AI Newsdesk briefing for Tuesday 20 October 2026.\n\nQuiet day."),
    ("## Research\n\nA result.", "...\n\nResearch.\n\nA result."),
    ("Sued in court [CTV News](https://x.example/a).", "...\n\nSued in court."),
    ("A launch [Verge](https://a.example) [Ars](https://b.example).", "...\n\nA launch."),
    ("It shipped (see [Blog](https://a.example)).", "...\n\nIt shipped (see)."),
    ("[A](https://a.example) and [B](https://b.example) agreed.", "...\n\nA and B agreed."),
    ("Ends with a source [Wired](https://w.example)", "...\n\nEnds with a source"),
    ("**Bold** and `code` at https://raw.example/x now.", "...\n\nBold and code at now."),
])
def test_speech_text(body, want): ...
```

  Write the full expected strings, with the `...` prefix filled in. Note the `(see)` case: the spec only promises that citations before `)` are dropped. If the implementer prefers to drop an empty `( )`, record that under Rulings and adjust the test.
- [ ] **Step 2: Run them and watch them fail** (ImportError).
- [ ] **Step 3: Implement it**, reusing `briefing.LINK_RE` (or `web.briefing_text.LINK_RE`, which is the same pattern; import the one in `briefing.py` to avoid a web dependency). The citation rule: one regex for `(\s*\[[^\]\n]+\]\([^)\s]+\))+(?=\s*(?:[.!?)]|$))`, applied per paragraph with `re.MULTILINE` semantics, replaced with `""`. Then replace the remaining links with their text.
- [ ] **Step 4: Write the chunk tests:** a 20,000-character text of 200-character paragraphs gives 3 chunks, each ≤ 9,000; a single 12,000-character paragraph of sentences splits at a sentence end; a 10,000-character string with no spaces is hard cut.
- [ ] **Step 5: Implement `chunks`, run `make test`, then commit:** `feat(narration): speech text and chunks from the briefing (feedback-3)`.

---

### Task 3: ElevenLabs key, config and runtime settings

**Goal:** The key `elevenlabs` exists end to end (env, Compose, `ApiKeys`, a Settings key box with a free key test), along with the narration runtime settings.

**Files:**
- Modify: `src/ai_newsdesk/config.py` (`elevenlabs_api_key: str = Field(default="", repr=False)`)
- Modify: `src/ai_newsdesk/runtime_settings.py`:
  - `ApiKeyName` gains `"elevenlabs"`;
  - new fields `narration_enabled: bool = True`, `narration_voice_id: str = Field(default="onwK4e9ZLuTAKqWW03F9", min_length=1, max_length=64)`, `narration_model: str = Field(default="eleven_v4", min_length=1, max_length=64)`, `narration_monthly_chars: int = Field(default=300_000, ge=0, le=10_000_000)`.
- Modify: `src/ai_newsdesk/web/keys.py` (`KEY_LABELS["elevenlabs"] = "ElevenLabs"`; `elevenlabs_key_checker(http)` sends `GET https://api.elevenlabs.io/v1/user/subscription` with the `xi-api-key` header; added to `default_key_testers`)
- Modify: `compose.yaml` (`ELEVENLABS_API_KEY: ${ELEVENLABS_API_KEY:-}`), `.env.example` (`ELEVENLABS_API_KEY=`), and the `README.md` config table row
- Test: `tests/test_runtime_settings.py`, `tests/test_config.py`, `tests/web/test_keys*.py` (wherever key testers are tested), `tests/test_compose_policy.py` and `tests/test_docs.py` (these should pass, not change)

**Acceptance criteria:**
- [ ] `RuntimeSettings()` has the four defaults, and invalid values are rejected: a cap of -1, a cap of 10,000,001, an empty voice.
- [ ] `ApiKeys.resolve_api_key("elevenlabs")` falls back to `settings.elevenlabs_api_key`.
- [ ] The key checker returns `None` on 200, "ElevenLabs rejected the key (401)." on 401, "ElevenLabs answered {code}." otherwise, and "Could not reach ElevenLabs (…)." on transport errors. The key never appears in a message.
- [ ] The Settings page shows an ElevenLabs key box like the others (the existing key template loop should pick it up from `KEY_LABELS`).
- [ ] `make test` is green, including the docs and compose policy tests.

**Verify:** `make test` → green.

**Steps:**

- [ ] **Step 1: Write the failing tests:** runtime defaults and validation, the env fallback, and the checker with `MockTransport` (200, 401, 500, ConnectError), asserting the key string is absent from each result.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement it**, mirroring `bearer_checker`. ElevenLabs uses the `xi-api-key` header, not `Authorization`.
- [ ] **Step 4: Add the env, Compose and README lines.** Run `make test`, then commit: `feat(keys): ElevenLabs key and read-aloud settings (feedback-3)`.

---

### Task 4: `Narrator` protocol and `ElevenLabsNarrator`

**Goal:** A tested ElevenLabs client behind a protocol, following spec 4.2: narrate with chunk stitching, list voices with pagination, read the quota and the model credit factors.

**Files:**
- Modify: `src/ai_newsdesk/protocols.py` (`Narrator`)
- Modify: `src/ai_newsdesk/models.py` (`NarrationResult`, `VoiceInfo`, `QuotaInfo`, `TtsModelInfo`)
- Create: `src/ai_newsdesk/tts/__init__.py`, `src/ai_newsdesk/tts/elevenlabs.py`
- Test: `tests/test_tts_elevenlabs.py`

**Interfaces (exact):**

```python
class NarrationResult(BaseModel):
    audio: bytes | None = None
    characters_billed: int = 0      # characters sent x the model's credit factor, rounded up
    maybe_billed: bool = False      # True when the outcome is unknown or chunks succeeded
    error: str | None = None        # short, scrubbed

class VoiceInfo(BaseModel):
    voice_id: str
    name: str
    category: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    preview_url: str | None = None

class QuotaInfo(BaseModel):
    tier: str
    character_count: int
    character_limit: int
    resets_at: datetime | None = None

class Narrator(Protocol):
    async def narrate(self, text_chunks: list[str], *, voice_id: str, model_id: str,
                      credit_factor: float) -> NarrationResult: ...      # never raises
    async def voices(self) -> list[VoiceInfo] | None: ...                # None on failure
    async def model_factors(self) -> dict[str, float] | None: ...        # TTS models only
    async def quota(self) -> QuotaInfo | None: ...
```

`ElevenLabsNarrator(http: httpx.AsyncClient, key: Callable[[], Awaitable[str]], *, base_url="https://api.elevenlabs.io", timeout_s=180.0)`

**Acceptance criteria:**
- [ ] `narrate` sends one request per chunk to `POST {base}/v1/text-to-speech/{voice_id}?output_format=mp3_44100_64`:
  - headers `xi-api-key` and `Accept: audio/mpeg`;
  - JSON body `{"text": chunk, "model_id": model_id}`, plus from the second chunk on `previous_request_ids: [up to 3 previous request-id header values]`, or `previous_text` (the last 500 characters) when no header was returned;
  - `voice_id` is URL-quoted, and a `voice_id` that doesn't match `^[A-Za-z0-9]{1,64}$` returns an error without any request.
- [ ] On success, `audio` is the concatenated bytes, `characters_billed = ceil(sum(len(chunk)) * credit_factor)`, `maybe_billed=False` and `error=None`.
- [ ] A 4xx on chunk 1 gives `audio=None`, `characters_billed=0` and `error="ElevenLabs answered 401."` (or 422 or 429, with no body echo).
- [ ] A timeout, a transport error or a 5xx on chunk k gives `audio=None`, `maybe_billed=True` and `characters_billed` counting chunks 1..k.
- [ ] A 4xx on chunk k > 1 gives `maybe_billed=True` and counts chunks 1..k-1.
- [ ] An empty key gives `error="No ElevenLabs key."` and makes no request.
- [ ] `voices()` follows `next_page_token` while `has_more`, and maps the fields. `model_factors()` maps `model_rates.character_cost_multiplier` for `can_do_text_to_speech` models. `quota()` maps the subscription. Each returns `None` on any error.
- [ ] The key value never appears in `error` or in captured logs (assert with `caplog`).

**Verify:** `uv run pytest tests/test_tts_elevenlabs.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** with `httpx.MockTransport` handlers that record requests and return `httpx.Response(200, content=b"ID3a", headers={"request-id": "r1"})`, and so on. Cover each criterion above as its own test.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement it.** Catch `httpx.TimeoutException` and `httpx.TransportError` as maybe-billed. Use `response.is_success`. Never include `exc` text that might hold headers: use `type(exc).__name__`.
- [ ] **Step 4: Run `make test`, then commit:** `feat(tts): ElevenLabs narrator behind a Narrator protocol (feedback-3)`.

---

### Task 5: Store: migration 0006, audio files and the character ledger

**Goal:** Persist audio metadata and the character usage, manage MP3 files atomically on disk, drop stale audio when a briefing is rewritten, and prune after 30 days (spec 4.3).

**Files:**
- Create: `src/ai_newsdesk/store/migrations/0006_briefing_audio.sql`
- Create: `src/ai_newsdesk/audio_files.py` (`AudioFiles(dir: Path)`: `write(day, data) -> str` (returns the file name), `path(name) -> Path`, `delete(name)`, `names() -> set[str]`)
- Modify: `src/ai_newsdesk/store/store.py` and `src/ai_newsdesk/store/records.py` (`BriefingAudioRecord`; `save_briefing_audio`, `get_briefing_audio`, `delete_briefing_audio`, `audio_older_than`, `record_tts_usage`, `month_tts_characters(now, tz)`)
- Test: `tests/test_store_audio.py`, `tests/test_audio_files.py`, `tests/test_db.py` (migration count, if it asserts one)

**Schema (exact):**

```sql
CREATE TABLE briefing_audio (
  date TEXT PRIMARY KEY REFERENCES briefings(date) ON DELETE CASCADE,
  voice_id TEXT NOT NULL,
  voice_name TEXT NOT NULL,
  model_id TEXT NOT NULL,
  characters INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  file TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE tts_usage (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  date TEXT NOT NULL,
  model_id TEXT NOT NULL,
  characters INTEGER NOT NULL,
  ok INTEGER NOT NULL
);
CREATE INDEX tts_usage_at ON tts_usage(at);
```

Check that `briefings.date` is unique and suitable as an FK target. If it isn't, drop the FK and handle the case in `save_briefing`; record that under Rulings.

**Acceptance criteria:**
- [ ] `AudioFiles.write` creates the directory (mode 0o750), writes `briefing-<YYYY-MM-DD>-<8 hex>.mp3` through a temp file plus `os.replace`, and returns the name. `path()` refuses any name not matching `^briefing-\d{4}-\d{2}-\d{2}-[0-9a-f]{8}\.mp3$` (ValueError).
- [ ] `save_briefing_audio(rec)` upserts and returns the previous file name (or None) so the caller can delete it.
- [ ] `save_briefing` on a date that already has audio deletes the audio row and returns or exposes the old file name. The pipeline deletes the file (Task 6).
- [ ] `month_tts_characters` sums `characters` for local-month rows, the same month logic as `month_spend`.
- [ ] `audio_older_than(cutoff_date)` returns rows to prune. The nightly prune (Task 6) also deletes files on disk with no row.
- [ ] The migration applies on a copy of a database made with 0001–0005 (follow `tests/test_migration_spend_providers.py`).

**Verify:** `uv run pytest tests/test_store_audio.py tests/test_audio_files.py tests/test_db.py -q` → pass; `make test` → green.

**Steps:**

- [ ] **Step 1: Write the failing tests** for each criterion, with `tmp_path` for the files.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement the migration, records, store methods and `AudioFiles`.**
- [ ] **Step 4: Run `make test`, then commit:** `feat(store): briefing audio rows, files and the character ledger (feedback-3)`.

---

### Task 6: `run_narration`, the scheduler, Write-it-now narration, nightly prune and app wiring

**Goal:** One code path makes a day's audio. It runs after the scheduled briefing (3 tries a day, 30 minutes apart), after Write it now, and from the button (Task 7). Nightly upkeep prunes, and `app.py` wires the narrator.

**Files:**
- Modify: `src/ai_newsdesk/pipeline.py` (`PipelineContext` gains `narrator: Narrator | None = None` and `audio: AudioFiles | None = None`; new `async def run_narration(ctx, day) -> NarrationOutcome`)
- Create: `src/ai_newsdesk/tts/catalogue.py` (`ElevenLabsCatalogue(store, narrator)`: `cached() -> CatalogueData | None`, stored under settings key `elevenlabs_catalogue` as `{fetched_at, voices: [...], model_factors: {...}}`; `refresh() -> bool`; `voice_name(voice_id) -> str` and `factor(model_id) -> float`, 1.0 when unknown)
- Modify: `src/ai_newsdesk/scheduler.py` (narration state and `_maybe_narrate(day, now)`, `NARRATION_MAX_ATTEMPTS = 3`, `NARRATION_RETRY_AFTER = 30 min`; `narration_status(day)`)
- Modify: `src/ai_newsdesk/web/routes_briefing.py` (the Write-it-now task narrates after a True briefing)
- Modify: `src/ai_newsdesk/app.py`:
  - `build_context` creates `ElevenLabsNarrator(deps.http, lambda: keys.resolve_api_key("elevenlabs"))` and `AudioFiles(Path(settings.db_path).parent / "audio")`;
  - the nightly chain prunes audio older than 30 days and orphans, and refreshes the ElevenLabs catalogue in the background when a key exists.
- Test: `tests/test_pipeline_narration.py`, `tests/test_scheduler.py`, `tests/test_tts_catalogue.py`, `tests/web/...` (Write-it-now narrates)

**`run_narration` contract:**

```python
class NarrationOutcome(StrEnum):
    DONE = "done"; OFF = "off"; NO_KEY = "no_key"; NO_BRIEFING = "no_briefing"
    OVER_CAP = "over_cap"; FAILED = "failed"; BUSY = "busy"

async def run_narration(ctx: PipelineContext, day: date) -> NarrationOutcome:
    # 1. rs = load_runtime_settings; OFF if not rs.narration_enabled or ctx.narrator is None.
    # 2. NO_KEY if not await keys.resolve_api_key("elevenlabs") (inject a key probe via ctx).
    # 3. briefing = get_briefing(day) -> NO_BRIEFING if missing.
    # 4. text = speech_text(briefing.body, briefing_date_label(day)); parts = chunks(text)
    # 5. factor = catalogue.factor(rs.narration_model); est = ceil(len(text) * factor)
    #    OVER_CAP if month_tts_characters + est > rs.narration_monthly_chars (log it).
    # 6. result = await ctx.narrator.narrate(parts, voice_id=..., model_id=..., credit_factor=factor)
    # 7. if result.characters_billed: record_tts_usage(at, day, model, chars, ok=result.audio is not None)
    # 8. FAILED if result.audio is None (log the scrubbed error).
    # 9. name = audio.write(day, result.audio); old = save_briefing_audio(...); delete old file.
    #    If the briefing was rewritten meanwhile (body changed between 3 and 9), delete the
    #    new file and return FAILED.
    # 10. DONE
```

Ruling allowed: how the key presence check reaches the pipeline. Prefer a `has_key: Callable[[], Awaitable[bool]]` on the context, or let `narrate` return the `"No ElevenLabs key."` error mapped to NO_KEY. Pick the simplest and record it under Rulings.

**Acceptance criteria:**
- [ ] `run_narration` returns each outcome under its condition, with a fake `Narrator` and an in-memory store. Usage is recorded on success, on maybe-billed and on partial chunks, and never on a clean 4xx. The file is written and the old file deleted on a remake.
- [ ] Scheduler:
  - after a successful `_maybe_brief(D)`, and on later ticks while D has a briefing and no audio, it calls `run_narration(D)` under the lock;
  - attempts are counted only for FAILED (up to 3 a day, ≥ 30 minutes apart);
  - OFF, NO_KEY and OVER_CAP don't use up attempts and stop that day's tries, except that OFF and NO_KEY are re-checked next tick, so turning the feature on works the same day;
  - DONE marks the day done;
  - a narration exception never changes the briefing state.
- [ ] `narration_status(day)` gives tries, max and gave-up, for the page.
- [ ] The Write-it-now task calls `run_narration(today)` after `run_briefing` returns True.
- [ ] Nightly removes rows and files older than 30 days and orphan files, and never touches files without the `briefing-` prefix.
- [ ] The app starts with no ElevenLabs key: `narrator` is still built, and narration returns NO_KEY.

**Verify:** `uv run pytest tests/test_pipeline_narration.py tests/test_scheduler.py tests/test_tts_catalogue.py -q` → pass; `make test` → green.

**Steps:**

- [ ] **Step 1: Write the failing `run_narration` tests**, one per outcome, plus usage and file assertions.
- [ ] **Step 2: Run them, watch them fail, implement, and make them pass.**
- [ ] **Step 3: Write the failing scheduler tests**, following the existing briefing retry tests in `tests/test_scheduler.py`: inject a `narration_fn` the same way `briefing_fn` is injected. Implement and pass.
- [ ] **Step 4: Write the catalogue tests:** cache round trip, refresh failure keeps the old copy, factor defaults. Implement and pass.
- [ ] **Step 5: Wire it in `app.py`, add the nightly prune with a test, run `make test`, then commit:** `feat(narration): make the briefing audio after each briefing (feedback-3)`.

---

### Task 7: The briefing page player, the audio route and Make audio

**Goal:** The briefing page plays the day's MP3 (with seeking and speed), explains why audio is missing, and offers **Make audio** (spec 4.6).

**Files:**
- Modify: `src/ai_newsdesk/web/routes_briefing.py`:
  - `GET /briefing/{day}/audio.mp3`;
  - `POST /briefing/{day}/audio`;
  - the audio context in `_render`. Note that `/briefing/{day}` exists: register the `.mp3` route so `{day}` parsing doesn't swallow it, and test it.
- Modify: `src/ai_newsdesk/web/templates/briefing.html` (the "Listen" bar)
- Create: `src/ai_newsdesk/static/briefing-audio.js` (the speed buttons; `localStorage` key `ai-newsdesk-audio-rate` in try/catch)
- Modify: CSS (the existing stylesheet; follow the "First light" tokens and accent variable)
- Test: `tests/web/test_briefing_audio.py`

**Acceptance criteria:**
- [ ] `GET /briefing/{day}/audio.mp3`:
  - needs a session (it redirects or returns 401 like the other pages: match the existing behaviour);
  - returns 404 when there's no row or the file is missing;
  - returns 200 `audio/mpeg` with `Cache-Control: private, max-age=86400`;
  - `Range: bytes=0-9` returns 206 with a `Content-Range` header and 10 bytes.
- [ ] With audio, the page has:
  - `<audio controls preload="metadata" src="/briefing/{day}/audio.mp3">`;
  - three speed buttons (1×, 1.25×, 1.5×) as `<button type="button" data-rate="...">`, with `aria-pressed`;
  - the caption "Read by {voice_name} · {m} min · made {HH:MM}". Minutes are `round(bytes / 8000 / 60)`, with a minimum of 1.
- [ ] Without audio, the reason comes from the outcome state:
  - OFF gives "Audio is off in Settings." (no button);
  - NO_KEY gives "Add an ElevenLabs key in Settings to hear this briefing." (no button);
  - OVER_CAP gives "This month's audio allowance is used up.";
  - FAILED gives "Audio failed; it will be retried." or "Audio failed today." after 3 tries;
  - `?audio=making` gives "Making audio. This takes a minute or two; reload to listen.";
  - otherwise "No audio yet.";
  - each of these, except OFF and NO_KEY, shows a **Make audio** button.
- [ ] `POST /briefing/{day}/audio` (CSRF):
  - returns 404 for a bad day or a missing briefing;
  - when the lock is busy, redirects to `?audio=busy` ("A run is in progress; try again in a few minutes.");
  - otherwise starts a background `run_narration(day)` under the lock and redirects with 303 to `/briefing/{day}?audio=making` (or `/briefing?audio=making` when the day is the latest).
- [ ] No inline script or style. The JS is served from `/static`, and the page works without JS: the player is native.

**Verify:** `uv run pytest tests/web/test_briefing_audio.py -q` → pass; `make test` → green.

**Steps:**

- [ ] **Step 1: Write the failing tests** (route, Range, 404s, page states, button, CSRF). Write test MP3 bytes into the tmp audio directory through `AudioFiles`.
- [ ] **Step 2: Run them, watch them fail, implement, and make them pass.** Use `FileResponse(path, media_type="audio/mpeg", headers=...)`; Starlette 1.8 serves Range.
- [ ] **Step 3: Visual check** (the handoff gotcha "Visual checks"): Playwright headless shell on loopback with a scratch database from `tests/web/dev_seed.py`, at 1440 px and 390 px, light and dark. Save screenshots to `.superpowers/fb3/shots/`.
- [ ] **Step 4: Run `make test`, then commit:** `feat(briefing): listen to the briefing, with speed and Make audio (feedback-3)`.

---

### Task 8: Settings "Read aloud", voice samples and the Models page step

**Goal:** Settings lets the owner switch read-aloud on or off and choose a voice (suggested group plus all account voices, with free samples), the model and the cap, and shows character usage. The Models page flow diagram shows the step (spec 4.7, 4.8).

**Files:**
- Modify: `src/ai_newsdesk/web/routes_settings.py`:
  - the Read aloud form fields in the existing settings POST;
  - `POST /settings/voices/refresh`;
  - `GET /settings/voice-sample/{voice_id}`.
- Modify: `src/ai_newsdesk/web/forms.py` (validation for the four fields)
- Modify: `src/ai_newsdesk/web/templates/settings.html` (the "Read aloud" section, `id="read-aloud"`)
- Create: `src/ai_newsdesk/static/voice-sample.js` (Play sample: sets an `<audio>` `src` to `/settings/voice-sample/{selected id}` and plays it)
- Modify: `src/ai_newsdesk/web/flow.py` and its template or tests (the "Read aloud" step)
- Test: `tests/web/test_settings_read_aloud.py`, `tests/web/test_flow*.py`

**Constants (exact):**

```python
SUGGESTED_VOICES = (
    ("onwK4e9ZLuTAKqWW03F9", "Daniel", "British, steady broadcaster"),
    ("pFZP5JQG7iQjIQuC4Bku", "Lily", "British, news and narration"),
    ("XrExE9yKIg1WjnnlVkGX", "Matilda", "American, professional"),
)
NARRATION_MODELS = (
    ("eleven_v4", "Eleven v4 (best)"),
    ("eleven_multilingual_v2", "Multilingual v2 (most stable long-form)"),
    ("eleven_flash_v2_5", "Flash v2.5 (half the characters)"),
)
SAMPLE_MAX_BYTES = 2 * 1024 * 1024
SAMPLE_TIMEOUT_S = 15.0
```

**Acceptance criteria:**
- [ ] The section has:
  - an on/off checkbox;
  - a voice `<select>` with `<optgroup label="Suggested">` (the three, always present) and `<optgroup label="All voices on your account">` (the cached list, minus the suggested ids, sorted by name, each "Name (accent, gender)" with missing labels left out);
  - the current voice selected, even if it's in neither list (shown as its id);
  - a model `<select>` from `NARRATION_MODELS`;
  - a cap number input;
  - the usage line.
- [ ] Saving validates: a voice id matching `^[A-Za-z0-9]{1,64}$`, a model in `NARRATION_MODELS`, and a cap from 0 to 10,000,000. Errors show inline like the other fields.
- [ ] The usage line reads "This month: {n} of {cap} characters used." plus " ElevenLabs says {limit - count} of {limit} left on your {tier} plan." when the quota is known. The quota is cached in memory on `app.state` for 10 minutes, and a failure leaves only the first sentence.
- [ ] The catalogue is refreshed in the background when Settings opens, the copy is older than 24 h and a key exists. `POST /settings/voices/refresh` refreshes now and redirects to `#read-aloud` with a saved or kept message.
- [ ] `GET /settings/voice-sample/{voice_id}`:
  - returns 404 unless the id is in the cached list with a `preview_url`;
  - fetches only `https` URLs whose host is `storage.googleapis.com` with a path starting `/eleven-public-prod/`, or a host that is `elevenlabs.io` or ends in `.elevenlabs.io`. Anything else gives 404, and is tested with `storage.googleapis.com/evil/...`, `elevenlabs.io.evil.example` and `http://` URLs;
  - sends `xi-api-key` only to `*.elevenlabs.io`, which is tested;
  - streams at most 2 MB (more gives 502), with a 15 s timeout giving 504;
  - responds `audio/mpeg` (or the upstream audio content type) with `Cache-Control: private, max-age=86400`;
  - needs a login.
- [ ] Models page: the flow has a "Read aloud" step after the briefing showing "ElevenLabs · {voice name} · {model}" or "Off". The existing flow tests are updated.

**Verify:** `uv run pytest tests/web -q -k "read_aloud or voice or flow"` → pass; `make test` → green.

**Steps:**

- [ ] **Step 1: Write the failing tests** for each criterion (fake catalogue in settings, `MockTransport` for the sample proxy).
- [ ] **Step 2: Run them, watch them fail, implement, and make them pass.**
- [ ] **Step 3: Visual check of Settings and Models** (as in Task 7, Step 3).
- [ ] **Step 4: Run `make test`, then commit:** `feat(settings): choose the read-aloud voice with samples (feedback-3)`.

---

### Task 9: Docs: OPERATIONS, README and ADR-0003

**Goal:** The docs describe the feature, its files, its quota and its recovery (spec 5).

**Files:**
- Modify: `docs/OPERATIONS.md`:
  - a "Read aloud (ElevenLabs)" section: where the audio lives (`/data/audio/` on `app-data`), 30-day retention, backups being database-only, the character cap versus the plan limit, how to remake audio (the button), how to switch it off, and the key;
  - the briefing failure note and Write it now.
- Modify: `README.md` (a feature line, and the key row added in Task 3)
- Create: `docs/adr/0003-elevenlabs-reads-the-briefing.md`
- Test: `tests/test_docs.py` (must pass, with no lab values)

**Acceptance criteria:**
- [ ] The ADR states the context, the decision (ElevenLabs `eleven_v4`, audio made after the briefing, a character meter separate from USD, proxied samples, dropped citations), the alternatives (on-demand generation, browser speech synthesis, storing audio in SQLite) and the consequences (about 217k of 363k characters a month on Creator; about 3–4 MB a day, about 100 MB at 30 days).
- [ ] `make test` is green.

**Verify:** `uv run pytest tests/test_docs.py -q` → pass; `make test` → green.

**Steps:**

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Run `make test`, then commit:** `docs: read aloud and the briefing failure note (feedback-3)`.

---

## After the tasks (controller, not implementer)

1. Final whole-branch review, then fixes.
2. PR, CI green, merge. Then `make backup` and `make deploy`; the owner has approved deploys for this feature in chat.
3. **Live check:**
   - one real narration of today's briefing through the **Make audio** button, about 7,000 characters;
   - confirm `tts_usage` and the ElevenLabs quota went up by about the same amount;
   - play it in the browser and seek;
   - play one sample on Settings.
4. Update `HANDOFF.local.md`.
