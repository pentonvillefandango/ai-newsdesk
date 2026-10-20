# ADR-0003: ElevenLabs reads the briefing, made after it is written, with its own character meter

## Status

Accepted (2026-10-20). Source: the briefing audio design (`docs/superpowers/specs/2026-10-20-briefing-audio-design.md`), approved by the owner (feedback-3). Amended the same day by feedback-4 and feedback-5; see **Amendments** below.

## Context

The owner listens in the office on a MacBook: the whole briefing as an overview, then uses the Today page to go deeper. So the briefing needs to be played, not just read. The V1 rules still hold: keys are write-only and never logged, fetched text is untrusted data, every external service sits behind a protocol, and spending has a hard limit.

ElevenLabs bills in credits (characters times a model factor), not dollars, so the dollar budget cannot guard it. The Creator plan allows 363,000 characters a month, as reported by the API. Measured on 2026-10-20: the real briefing came to 5,402 speech characters, about 165k a month at one briefing a day. The design estimated about 7k a day, about 217k.

## Decision

- **ElevenLabs, model `eleven_v4`** (credit factor 1.0), behind a `Narrator` protocol; only `app.py` wires `ElevenLabsNarrator`. Output is MP3 at 64 kbps, about 0.5 MB a minute. Long text is split into chunks and stitched with the API's request ids so the voice stays steady. `eleven_flash_v2_5` costs half the credits and can be chosen instead.
- **Audio is made after the scheduled briefing**, so it is ready when the owner sits down. The scheduler retries a missing one (3 tries a day, 30 minutes apart) under the same run lock; a failed narration never touches the briefing. Make audio / Remake audio and Write it now do it by hand.
- **Sources are dropped.** Citation links that end a sentence are not read; inline links are read as their text; markdown and bare URLs are removed.
- **A character meter separate from USD.** `tts_usage` records what ElevenLabs counted (its counter read before and after; feedback-4), else the characters sent times the model factor, and a monthly cap (default 300,000, below the 363,000 plan limit) is checked before every narration. It records requests that may have been billed (a timeout, a 5xx, chunks sent before a failure) and nothing on a clean refusal, so it may over-count and never under-counts.
- **Voices:** three suggested (Lily, the default since feedback-4, Daniel, Matilda) plus a full dropdown of the account's voices, cached and refreshed nightly.
- **Samples are proxied** by the app: the preview files are free, the page stays same-origin with the CSP unchanged (`default-src 'self'`), and the key is sent only to `*.elevenlabs.io`. Other hosts are on a small allow-list and get no key.
- **Audio lives as files** in `/data/audio/` on the `app-data` volume, with a database row per day. A nightly upkeep deletes audio older than 30 days, orphaned MP3s, and temp files over 1 hour old.

## Alternatives considered

- **Generating audio on demand, at first Play.** Rejected by the owner: they want it ready when they sit down, and a page that spends money when opened is hard to cap. Making it after the briefing means it is ready and the spend happens once, at a known time.
- **Browser `speechSynthesis`.** Free and no key, but robotic and different on every device and browser, which is the wrong experience for a daily listen.
- **Storing the audio in SQLite.** Simple to serve, but 3 to 4 MB a day would bloat the database and so every backup (backups copy the database only). Files on the volume are pruned separately and keep backups small.
- **Counting characters against the USD budget.** Credits are not dollars, and plans differ; a separate meter in the unit ElevenLabs bills, with its own cap, is simpler and honest.

## Consequences

- At one briefing a day, about 75k of the 363,000 characters a month on Creator are used (ElevenLabs counted about 2,400 per 5,400-character briefing), well under the 300,000 cap. Remaking audio by hand spends again.
- About 3 to 4 MB a day, about 100 MB at 30 days, on the `app-data` volume. Backups do not include audio; a restore leaves rows whose files are gone, and Make audio recreates them at a character cost.
- A new key (`ELEVENLABS_API_KEY` or saved in Settings) and a switch to turn the whole feature off. Without a key or when off, nothing is sent and the *scheduler* takes no run lock for audio; Write it now and Make audio take it briefly (narration then finds it off or keyless and returns at once).
- The meter can over-count after a timeout, so the cap may stop audio slightly early; raising the cap or waiting for the month to roll over fixes it.
- Narrated text is built from the stored briefing, which is derived from untrusted news. It only ever goes to the voice API as text, never as instructions to us.

## Amendments

- **Feedback-4 (2026-10-20).** The meter records ElevenLabs' own counter delta (read before, and twice after, 5 seconds apart), falling back to the estimate, because a 5,402-character briefing was counted as 2,377. Google-hosted voice previews served as `text/plain` are sniffed as MP3. The MP3 URL carries a version, so a remake isn't hidden by the browser cache. The spoken intro drops the date when the briefing opens with it. The default voice is Lily.
- **Feedback-5 (2026-10-20).** On-demand audio is now an owner-chosen option (`narration_on_demand`). Audio made after the briefing stays the default, and on demand only **Make audio** spends characters, so the "rejected" alternative above is available by choice. It still never spends when a page is merely opened. **Random (any voice)** picks from all the account's voices, never the previous recording's voice. The player has speeds from 1× to 1.5× and starts at 1.25×.
