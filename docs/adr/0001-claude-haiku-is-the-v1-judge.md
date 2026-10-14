# ADR-0001: Claude Haiku 5.5 is the V1 judge; TypeSafe Jev stays optional

## Status

Accepted (2026-10-14). Source: the step 0 bake-off in the V1 spec, section 12.

## Context

The V1 design first split the AI work in two: TypeSafe Jev would judge suitability and same-story grouping, because it gives typed, calibrated answers at $0.042 per million input tokens with free output, and Claude Haiku 5.5 would write the summaries and the briefing. The spec required a bake-off before building on that split.

Both judges were given identical input: source, trust level, title and an article excerpt, read through research-engine where the feed text was thin. Each batch was labelled by hand (junk / fine / important) before either judge ran. There were four rounds:

1. **55 items.** Jev hid 54 of 55. Many items reached it with no article text, and the first "low value" question counted every lab announcement as promotional.
2. **The same 55, with full text and narrower questions.** Jev was level with Haiku.
3. **A 57-item hold-out from different sources.** Haiku was better: it caught 22 of 25 junk items against Jev's 19, and hid 1 of 8 important items against Jev's 2.
4. **623 items**, at the owner's request. Each judge is shown at its best threshold:

| Judge | Junk caught | Important hidden | Fine hidden | Cost (623 items) |
|---|---|---|---|---|
| Haiku 5.5, threshold 4 | 280/365 | 1/30 | 34/228 | $0.066 |
| Jev 1.13, best question set | 234/365 | 2/30 | 49/228 | $0.032 |

At 500 items a day, the cost difference is about $0.90 a month. Both judges fit easily under the $5 monthly cap. TypeSafe returned one `499` error during the runs; Anthropic returned none.

## Decision

V1 uses Claude Haiku 5.5 for judging (review and same-story check) as well as for writing. Judging uses structured output, effort `low`, about 10 items per request, and a show threshold of 4.

Judging sits behind a `Judge` protocol. `JevJudge` is an optional implementation, selected with the `judge_provider` setting. `TYPESAFE_API_KEY` is optional.

## Consequences

- One AI provider and one required key. Quality is better on the owner's kind of content, at about $2 a month.
- Jev can be re-evaluated later with the same harness, against the owner's thumbs data, without code changes outside `judge.py`.
- The bake-off's build lessons are binding on V1:
  - judge on article text, not headlines;
  - fetch through research-engine at 3 or fewer concurrent requests, retrying 429 and non-JSON replies with backoff;
  - never let one failed AI call fail a run or silently hide an item.
