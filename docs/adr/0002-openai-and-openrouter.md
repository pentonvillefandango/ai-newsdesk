# ADR-0002: OpenAI and OpenRouter via one OpenAI-compatible client; prices from the OpenRouter catalogue; structured-output-only models

## Status

Accepted (2026-10-14). Source: the multi-provider design (`docs/superpowers/specs/2026-10-14-multi-provider-design.md`), approved by the owner.

## Context

V1 used Anthropic for every AI job (ADR-0001). The owner wants to try other providers job by job: OpenAI directly with an OpenAI key, and OpenRouter, where one key reaches hundreds of models. The V1 rules still hold:

- a hard monthly budget, with every call checked against a true worst case first, so a model with no known price cannot be used;
- structured output only: answers are validated with the same pydantic models, and a refused or invalid answer is a failure;
- fetched text is untrusted data inside tags, under the same prompts for every provider;
- keys are write-only and tested before they are saved.

## Decision

- **One client contract.** A `ModelClient` protocol (`structured()` and `worst_case_usd()`) sits under the judge, summary writer, briefing writer and duplicate merger. Their prompts, batching and failure rules are unchanged.
- **Two implementations.** `AnthropicClient` wraps the existing Anthropic call. `OpenAICompatibleClient` uses the official `openai` SDK for both OpenAI (default base URL) and OpenRouter (`https://openrouter.ai/api/v1`), with a strict `json_schema` response format.
  - OpenRouter requests add `provider.require_parameters` and `provider.max_price` (the catalogue price), so a request is never routed to an endpoint without structured output or above the catalogue price, and `usage.include`, so the reported cost is recorded. They cap output with `max_tokens`; OpenAI direct uses `max_completion_tokens`.
- **Prices from the OpenRouter catalogue.** The public model list (no key) is fetched at start-up when stale, nightly and on demand, filtered and stored. OpenRouter models are priced from it; OpenAI models from `config/pricing.yaml` when listed, else from the catalogue's `openai/<id>` entry; Anthropic and TypeSafe from `pricing.yaml`. A failed fetch keeps the last good copy.
- **Structured-output-only models.** The catalogue keeps a model only if it is priced above zero, takes text, is not a variant id (`:free`, `:batch` or any other `:` suffix) and lists `structured_outputs`, `response_format` and `max_tokens`. Settings (now the Models page) validates every choice against this, and against the provider's key.
- **Per-job choice, Anthropic by default.** Each job has its own provider and model. Settings offers (since owner feedback 2, on its own Models page) a curated shortlist (`config/models.yaml`), manual entry with autocomplete from the catalogue, and a Test button that makes one tiny, budget-checked call.

## Alternatives considered

- **A separate client per provider, or a multi-provider library.** More code paths or a large dependency for two providers that speak the same API; OpenRouter is OpenAI-compatible.
- **Prices kept by hand in `pricing.yaml`.** Hundreds of models whose prices change; stale prices would break the worst-case guarantee. The catalogue is free, public and refreshed daily.
- **Allowing models without structured output (JSON mode plus retries).** Weakens the "schema-valid or failure" rule and makes worst cases depend on retries.

## Consequences

- Any job can move to OpenAI or OpenRouter from the Models page without code changes; Anthropic remains the default and needs no new key.
- The app makes one keyless request to openrouter.ai at start-up (when the list is stale) and nightly. If the list never loads, only `pricing.yaml` models are usable.
- Worst cases are conservative: the highest tier price, cached input at full price for OpenAI and OpenRouter, and input priced as a cache write. A call that may have been billed with no usage known is recorded once at its worst case, as a cost-only row: one we abandon (a Test or Compare time limit, or a pipeline call cut off by shutdown) and one that fails after the request may have reached the provider (an SDK timeout, a dropped connection, a 5xx or 408, an unexpected error). A failure that is never billed (any other 4xx, or a 529 overloaded reply, which was rejected and not run) records nothing. No SDK retries anywhere, so one call is one request; a failed item is offered again on the next run instead. Every call has a 120-second SDK timeout. The rule: the meter can over-count but never under-count; the one case logged instead of recorded is a Test cut off after the database has closed. (Corrected by owner feedback 2: earlier, timed-out and failed calls were not recorded, so the meter could understate spend.)
- Claude Haiku stays the tested judge. The Models page shows a warning whenever the judge is anything else: "Claude Haiku was chosen by testing on 623 labelled items; other judges are untested on your news and may filter differently." Other judges should be compared against the owner's labels before being trusted.
- A saved choice that becomes unusable (no price, no key) falls back to Claude Haiku at start-up, with a log line, instead of stopping the app.
