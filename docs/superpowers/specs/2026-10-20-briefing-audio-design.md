# Briefing read aloud (ElevenLabs) and a "briefing failed" note (design)

> **Amended after approval** (feedback-4 and feedback-5): on-demand audio, a random voice, speeds 1× to 1.5× starting at 1.25×, Lily as default, and a meter that records ElevenLabs' own count. ADR-0003's Amendments and `docs/OPERATIONS.md` describe the current behaviour; this spec is kept as approved.

Date: 2026-10-20. Status: design approved by the owner in chat. Decisions: the whole briefing, audio ready after the scheduled briefing, sources dropped, three suggested voices plus a full voice dropdown, the failure note first.

## 1. Goal

The owner listens to the daily briefing in the office, on a MacBook, as an overview. The Today page is still where they go deeper.

- **Part 1, failure note.** When today's briefing is missing, the briefing page says so and offers **Write it now**. On 2026-10-20 the page silently showed Monday's briefing after six failed attempts (fixed in PR #19); next time the owner should see it.
- **Part 2, read aloud.**
  - After the scheduled briefing is written, the app turns it into speech with ElevenLabs and saves an MP3.
  - The briefing page gets an audio player with play/pause, seeking and 1×, 1.25× and 1.5× speed.
  - Settings chooses the voice, from three suggestions or any voice on the account, with free samples. Settings also chooses the model, switches the feature on or off and shows the month's character use.

## 2. Non-negotiables (inherited)

- **Keys are write-only.**
  - `ELEVENLABS_API_KEY` comes from `.env`, or from the encrypted `ApiKeys` store, which wins.
  - A key is tested before it is saved.
  - Keys never appear in logs, responses or error text (`keys.scrub`).
- **Behind a protocol.** ElevenLabs sits behind a new `Narrator` protocol, and only `app.py` wires `ElevenLabsNarrator`.
- **A quota pre-check before every call.** ElevenLabs bills in characters (credits), not dollars, so this is a separate monthly **character cap**, not the USD budget. A call that might have been billed but whose outcome is unknown (a timeout, a dropped connection) is recorded at its full character count: the meter may over-count, never under-count.
- **Fetched text is data.**
  - The speech text is built only from the stored briefing body, which `clean_briefing` already filtered.
  - Voice names and labels from ElevenLabs are rendered with autoescape.
  - CSP stays `script-src 'self'` / `style-src 'self'`: no inline script or style, and no third-party media origins (samples are proxied).
- **Failures are local.** A failed narration never affects the text briefing, the runs or the scheduler.
- **Tests never call real APIs.** They use httpx `MockTransport` or a fake `Narrator`.
- **Co-tenancy.** Audio lives on our own `app-data` volume. No new host paths, ports or containers.

## 3. Part 1: the briefing failure note

- **When it shows.** The latest briefing page (`GET /briefing`) shows a notice when today (local `TZ`) has no briefing and the local time is past `briefing_time`. The notice reads:
  - "Today's briefing isn't ready. Showing the one from {Weekday d Month}." (no "Showing…" part when there is none);
  - plus the scheduler's state for today: "Tried {n} of 6 times; the last try failed." while attempts remain, or "The scheduler has stopped trying today." once all six have failed;
  - plus a **Write it now** button (`POST /briefing/write`, CSRF-protected like every POST).
- **What the button does.**
  - It follows the Settings "Refresh now" pattern (`routes_settings._run_now`): it starts a background task and redirects with 303 to `/briefing?writing=1`, which shows "Writing today's briefing. This takes about a minute; reload to see it."
  - The task takes the scheduler lock without waiting. If the lock is held, it does nothing, and the redirect says "A run is in progress; try again in a few minutes."
  - The task calls `pipeline.run_briefing(ctx, today)`. When that returns True, the task then runs narration for today (Part 2), outside the briefing's lock hold.
- **Scheduler state.** The scheduler exposes today's attempt count and whether it has given up (read-only properties). A briefing written by the button is picked up by the scheduler's existing `get_briefing` check, so it won't write a second one.
- An earlier day's page (`/briefing/{day}`) never shows the notice.

## 4. Part 2: read aloud

### 4.1 Speech text (`src/ai_newsdesk/narration.py`, pure functions)

`speech_text(body: str, day_label: str) -> str` turns the stored markdown-lite body (master plan R4) into plain speech text:

- It opens with "AI Newsdesk briefing for {day_label}." followed by a paragraph break.
- `## Heading` becomes `Heading.` as its own paragraph.
- **Links.**
  - A link that is a citation is dropped together with the whitespace before it. A citation is one or more `[text](url)` links that end a sentence, followed by `.`, `!`, `?` or `)` or the end of a paragraph. The sentence's own punctuation is kept.
  - Any other link becomes its text.
  - Examples:
    - "… in court [CTV News](https://…)." becomes "… in court."
    - "[A](u1) and [B](u2) agreed." becomes "A and B agreed."
    - "… launch [Verge](u1) [Ars](u2)." becomes "… launch."
- Leftover markdown punctuation (`*`, `_`, backticks, a leading `#`) is removed, URLs that aren't links are dropped, runs of spaces are collapsed and paragraphs are kept, separated by blank lines.
- `chunks(text, limit=9000) -> list[str]` splits at paragraph breaks, falling back to sentence ends and then a hard cut, so every chunk is at most `limit` characters. A normal briefing (about 7,000 characters) is one chunk.

### 4.2 Narrator protocol and ElevenLabs client

```python
class Narrator(Protocol):
    async def narrate(self, text_chunks: list[str], *, voice_id: str, model_id: str,
                      credit_factor: float) -> NarrationResult: ...  # never raises
    async def voices(self) -> list[VoiceInfo] | None: ...  # None on failure
    async def model_factors(self) -> dict[str, float] | None: ...  # None on failure
    async def quota(self) -> QuotaInfo | None: ...  # None on failure
```

The plan (`docs/plans/2026-10-20-briefing-audio.md`, Task 4) gives the exact field lists.

- **`NarrationResult`** has:
  - `audio: bytes | None`;
  - `characters_billed: int`, characters sent, times the model's credit factor;
  - `maybe_billed: bool`;
  - `error: str | None`, a short message, scrubbed.
- **`ElevenLabsNarrator(http, key_resolver)`** (`src/ai_newsdesk/tts/elevenlabs.py`).
  - **Requests.** `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_64`, with headers `xi-api-key` and `Accept: audio/mpeg`, and the JSON body `{text, model_id}`.
  - **Multiple chunks.** Each chunk after the first passes `previous_request_ids` (the `request-id` response header of the previous chunk, up to 3) for seamless joins. If the header is missing, `previous_text` (the last 500 characters of the previous chunk) is sent instead. The MP3 bytes are concatenated.
  - **Timeouts and retries.** 180 s per narration request (15 s for the free GETs: voices, models, quota), and no retries inside a call: the scheduler retries.
  - **Billing outcomes.**
    - A 4xx response means nothing was billed.
    - A timeout, a dropped connection or a 5xx means `maybe_billed=True`, so the characters count.
    - Any chunk failing fails the whole narration. Chunks already produced are counted as billed.
  - **`voices()`** reads `GET /v2/voices?page_size=100`, following `next_page_token`. It returns `voice_id`, `name`, `labels` (accent, gender, age, use case), `category` and `preview_url`.
  - **`quota()`** reads `GET /v1/user/subscription` and returns `tier`, `character_count`, `character_limit` and `next_character_count_reset_unix`.
  - **Credit factors.** `GET /v1/models` gives `model_rates.character_cost_multiplier` per text-to-speech model. These are cached together with the voice list. Without a cache the factor is 1.0, which never under-counts.
- **Key test** (`keys.py`): `GET /v1/user/subscription` with the candidate key. It is free, and a 401 means rejected.

### 4.3 Storage (migration `0006_briefing_audio.sql`)

- **`briefing_audio`**:
  - `date TEXT PRIMARY KEY` (the briefing date);
  - `voice_id`, `voice_name`, `model_id`;
  - `characters INTEGER`;
  - `bytes INTEGER`;
  - `file TEXT`, a file name, not a path;
  - `created_at`.
- **`tts_usage`**: `id`, `at`, `date` (the briefing date), `model_id`, `characters` (billed credits), `ok INTEGER`. The month's sum, by local month as in the budget, is the meter. Samples use free preview files, so they never appear here.
- **Audio files.**
  - Files live in `<dir of DB_PATH>/audio/`, named `briefing-<date>-<8 hex>.mp3`. A random suffix is used so a regenerated file never collides.
  - Writes go to a temp file, which is then renamed. After that the row is upserted and the old file deleted.
  - Paths are built only from validated dates and generated names, never from request input.
- **When a day's briefing is rewritten** (`save_briefing` on an existing date), its audio row and file are removed, because the audio no longer matches.
- **Nightly upkeep** (in the existing nightly chain) deletes audio rows and files older than 30 days, and orphaned files.
- `make backup` stays database-only: audio can always be made again. This is documented.

### 4.4 Runtime settings and the quota check

- **New `RuntimeSettings` fields:**
  - `narration_enabled: bool = True`;
  - `narration_voice_id: str = "onwK4e9ZLuTAKqWW03F9"` (Daniel);
  - `narration_model: str = "eleven_v4"`;
  - `narration_monthly_chars: int = 300_000` (from 0 to 10,000,000).
- **The key.** `ApiKeyName` gains `"elevenlabs"`, `config.Settings` gains `elevenlabs_api_key`, and `compose.yaml` maps `ELEVENLABS_API_KEY`. `.env.example` and the README config table list it.
- **Pre-check.** The estimate is `len(text) × credit factor`. The narration is skipped, with a log line and the reason shown on the briefing page, when the month's `tts_usage` plus the estimate would exceed `narration_monthly_chars`.
- **Narration needs** `narration_enabled`, a resolvable key and a briefing for the day.

### 4.5 When narration runs

- **Scheduler.** After `_maybe_brief` succeeds for day D, or on any later tick while D's briefing exists and D has no audio, the scheduler narrates D:
  - only when narration is possible (4.4);
  - under the scheduler lock;
  - retrying every 30 min, at most 3 attempts per local day, with its own counters separate from the briefing's.
- **The "Write it now" task** (Part 1) narrates after it writes.
- **Make audio.** `POST /briefing/{day}/audio` (the button) makes or remakes any stored day's audio, in the background, with the same lock rule as Part 1. It redirects to `/briefing/{day}?audio=making`.
- **One code path,** `pipeline.run_narration(ctx, day) -> bool`, does it all: build the text, pre-check, narrate, record usage (always when billed or maybe billed), save the file and row. It returns True on success.

### 4.6 Briefing page

- **When audio exists for the day,** a "Listen" bar sits above the article:
  - `<audio controls preload="metadata">` with the source `/briefing/{day}/audio.mp3`;
  - speed buttons for 1×, 1.25× and 1.5× (`static/briefing-audio.js`, which sets `playbackRate`; per browser, through `localStorage` wrapped in try/except);
  - a caption with the voice name, length in minutes (from bytes at 64 kbps) and when it was made.
- **When no audio exists,** the bar shows the reason ("Audio is off in Settings", "No ElevenLabs key", "This month's audio allowance is used up", "Making audio…", or "Audio failed; it will be retried") and a **Make audio** button, except when the feature is off or there's no key.
- **Serving.** `GET /briefing/{day}/audio.mp3` needs a login. It returns `audio/mpeg` and supports HTTP Range (Starlette `FileResponse`), which Safari needs to seek. It also sends `Cache-Control: private, max-age=86400`. It returns 404 when there's no audio.
- The page works without JavaScript: only the speed buttons need it.

### 4.7 Settings: "Read aloud" section

- **Choices.**
  - An on/off switch.
  - The **voice dropdown**: a "Suggested" group first (Daniel, British, steady broadcaster; Lily, British, news and narration; Matilda, American, professional), then "All voices on your account" sorted by name. Each entry is labelled "Name (accent, gender)".
  - The model dropdown: `eleven_v4` (default, best), `eleven_multilingual_v2` (most stable long-form) and `eleven_flash_v2_5` (half the credits).
  - The monthly character cap.
- **The voice list** is cached in settings key `elevenlabs_catalogue` (`{fetched_at, voices, model_factors}`). It refreshes when Settings opens and the copy is older than a day, and through a **Refresh voices** button. Without a key or a list, the dropdown shows only the three suggestions.
- **Samples.** A **Play sample** button sits next to the dropdown. `GET /settings/voice-sample/{voice_id}`:
  - proxies the voice's ElevenLabs `preview_url`, which is free and spends no credits;
  - only for a `voice_id` in the cached list, and only for an `https` URL on the allow-list, with a 2 MB cap and a 15 s timeout;
  - the allow-list is `storage.googleapis.com` with a path under `/eleven-public-prod/`, or a host that is `elevenlabs.io` or ends in `.elevenlabs.io`. Both were seen on the owner's account, 2026-10-20;
  - only an `*.elevenlabs.io` request carries `xi-api-key` (the account's own voices preview through the API). The key is never sent to any other host;
  - makes the browser's media requests same-origin, so CSP is unchanged.
- **Usage line.** "This month: {n} of {cap} characters used. ElevenLabs says {x} of {limit} left on your {tier} plan." The quota is fetched when Settings opens and cached for 10 minutes. If the call fails, only our own meter is shown.
- **The key.** A key box for ElevenLabs sits with the other keys (`KEY_LABELS`), tested before it's saved.

### 4.8 Models page

The flow diagram (`web/flow.py`) gains a "Read aloud" step after the briefing, showing "ElevenLabs, {voice name}, {model}" or "Off".

## 5. Docs

- **OPERATIONS.md:** the audio files and 30-day retention, backups not including audio, the character cap, the key, and how to regenerate.
- **README:** the feature, and the config table row.
- **ADR-0003:** "ElevenLabs reads the briefing". It covers why ElevenLabs, why a character meter separate from the USD budget, why audio is made after the briefing (owner choice: ready when they sit down, about 217k of 363k characters a month on Creator), and why samples are proxied.
- `tests/test_docs.py` must stay green.

## 6. Testing

- **`narration.py`:** headings; citations dropped (single, double, before `)`, at paragraph end); inline links kept as text; markdown and URL stripping; the opening line; `chunks` limits and boundaries.
- **`ElevenLabsNarrator`** with `MockTransport`:
  - request shape (URL, headers, body, output format);
  - multi-chunk stitching via `previous_request_ids`;
  - 401, 422 and 429 mean nothing billed; a timeout and a 5xx mean `maybe_billed`;
  - the key is never in errors or logs;
  - voices pagination; quota parsing.
- **Store:** migration 0006; upsert replaces the file; a rewritten briefing drops its audio; nightly pruning; the monthly usage sum.
- **`run_narration`:** the pre-check skips over the cap; usage is recorded on success, maybe-billed and partial chunks; nothing is recorded on a 4xx; the file is written atomically.
- **Scheduler:** narration follows the briefing; 3 tries a day at 30-minute spacing; it never runs without a key or when off; a narration failure never touches briefing state; the attempt counters.
- **Web:**
  - the failure notice's conditions, the earlier-day page and the button (the busy path, the background run);
  - the player markup and Range on the audio route (a 206 with `Content-Range`);
  - 404 and login required;
  - the Make audio button;
  - Settings: the dropdown groups, the sample proxy allow-list and size cap, an unknown voice gives 404, the usage line;
  - CSP has no inline script.
- **Live check after deploy** (owner-approved cost): one real narration of today's briefing, about 7,000 characters (about 2% of the month), played back in the browser, with seeking checked.
