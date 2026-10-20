# ADR-0003: ElevenLabs reads the briefing, made after it is written, with its own character meter

## Status

Accepted (2026-10-20). Source: the briefing audio design (`docs/superpowers/specs/2026-10-20-briefing-audio-design.md`), approved by the owner (feedback-3).

## Context

The owner listens in the office on a MacBook: the whole briefing as an overview, then uses the Today page to go deeper. So the briefing needs to be played, not just read. The V1 rules still hold: keys are write-only and never logged, fetched text is untrusted data, every external service sits behind a protocol, and spending has a hard limit.

ElevenLabs bills in credits (characters times a model factor), not dollars, so the dollar budget cannot guard it. The Creator plan allows 363,000 characters a month, as reported by the API. Measured on 2026-10-20: the real briefing came to 5,402 speech characters, about 165k a month at one briefing a day. The design estimated about 7k a day, about 217k.

## Decision

- **ElevenLabs, model `eleven_v4`** (credit factor 1.0), behind a `Narrator` protocol; only `app.py` wires `ElevenLabsNarrator`. Output is MP3 at 64 kbps, about 0.5 MB a minute. Long text is split into chunks and stitched with the API's request ids so the voice stays steady. `eleven_flash_v2_5` costs half the credits and can be chosen instead.
- **Audio is made after the scheduled briefing**, so it is ready when the owner sits down. The scheduler retries a missing one (3 tries a day, 30 minutes apart) under the same run lock; a failed narration never touches the briefing. Make audio / Remake audio and Write it now do it by hand.
- **Sources are dropped.** Citation links that end a sentence are not read; inline links are read as their text; markdown and bare URLs are removed.
- **A character meter separate from USD.** `tts_usage` records the characters (times the model factor) sent, and a monthly cap (default 300,000, below the 363,000 plan limit) is checked before every narration. It records requests that may have been billed (a timeout, a 5xx, chunks sent before a failure) and nothing on a clean refusal, so it may over-count and never under-counts.
- **Voices:** three suggested (Daniel, Lily, Matilda) plus a full dropdown of the account's voices, cached and refreshed nightly.
- **Samples are proxied** by the app: the preview files are free, the page stays same-origin with the CSP unchanged (`default-src 'self'`), and the key is sent only to `*.elevenlabs.io`. Other hosts are on a small allow-list and get no key.
- **Audio lives as files** in `/data/audio/` on the `app-data` volume, with a database row per day. A nightly upkeep deletes audio older than 30 days, orphaned MP3s, and temp files over 1 hour old.

## Alternatives considered

- **Generating audio on demand, at first Play.** Rejected by the owner: they want it ready when they sit down, and a page that spends money when opened is hard to cap. Making it after the briefing means it is ready and the spend happens once, at a known time.
- **Browser `speechSynthesis`.** Free and no key, but robotic and different on every device and browser, which is the wrong experience for a daily listen.
- **Storing the audio in SQLite.** Simple to serve, but 3 to 4 MB a day would bloat the database and so every backup (backups copy the database only). Files on the volume are pruned separately and keep backups small.
- **Counting characters against the USD budget.** Credits are not dollars, and plans differ; a separate meter in the unit ElevenLabs bills, with its own cap, is simpler and honest.

## Consequences

- At one briefing a day, about 165k of the 363,000 characters a month on Creator are used, well under the 300,000 cap; even the design's 217k estimate fits. Remaking audio by hand spends again.
- About 3 to 4 MB a day, about 100 MB at 30 days, on the `app-data` volume. Backups do not include audio; a restore leaves rows whose files are gone, and Make audio recreates them at a character cost.
- A new key (`ELEVENLABS_API_KEY` or saved in Settings) and a switch to turn the whole feature off. Without a key or when off, nothing is sent and no run lock is taken.
- The meter can over-count after a timeout, so the cap may stop audio slightly early; raising the cap or waiting for the month to roll over fixes it.
- Narrated text is built from the stored briefing, which is derived from untrusted news. It only ever goes to the voice API as text, never as instructions to us.
