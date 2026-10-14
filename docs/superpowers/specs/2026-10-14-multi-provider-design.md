# Multi-provider AI: OpenAI and OpenRouter (design)

Date: 2026-10-14. Status: design approved by the owner in chat (scope, shortlist, manual entry, judge warning).

## 1. Goal

Today every AI job uses Anthropic's API. Add two more providers:

- **OpenAI**, called directly with the owner's OpenAI key.
- **OpenRouter**, which gives one key access to hundreds of models.

Each of the four AI jobs (judge, summary writer, briefing writer, duplicate-merger) chooses its provider and model **independently**. Anthropic stays the default for everything.

For OpenAI and OpenRouter, the Settings page offers:

- a curated shortlist of 25 models;
- a **manual model entry**, autocompleted and validated against the models that are priced and support structured output;
- a **Test** button that makes one tiny, budget-checked real call.

Switching the judge away from Claude Haiku shows a warning. The reason is that Haiku won the bake-off on 623 labelled items (ADR-0001), and other judges are untested on the owner's news.

## 2. Non-negotiables (inherited)

- **Hard monthly budget cap.** Every call is pre-checked against a true worst-case cost. A model whose price is unknown cannot be selected or used.
- **Structured output only.** A model must guarantee schema-valid output; for OpenRouter that means `structured_outputs` is in `supported_parameters`. The response is parsed and validated with the same pydantic models used today. Invalid or refused output counts as a failure for those items.
- **Untrusted content stays tagged.** Fetched text goes inside tags, under a system prompt that says the tagged text is data, never instructions. The prompts are the existing ones, shared by every provider.
- **Failures are local.**
- **Keys are write-only.**
  - Keys come from the encrypted `ApiKeys` store, falling back to `.env` (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`).
  - Each key is tested before it is saved.
  - Keys never appear in logs or responses, and error messages are scrubbed.
- **Tests never call real APIs.**

## 3. Architecture

### 3.1 Model client abstraction (new `src/ai_newsdesk/llm/`)

```python
class StructuredResult(Generic[T]):
    parsed: T | None
    usage: Usage | None
    refused: bool
    error: str | None   # short, scrubbed

class ModelClient(Protocol):
    provider: str            # "anthropic" | "openai" | "openrouter"
    model: str
    async def structured(self, *, system: str, user: str, schema: type[T],
                         max_tokens: int, purpose: Purpose) -> StructuredResult[T]: ...
    def worst_case_usd(self, *, system: str, user: str, max_tokens: int) -> float: ...
```

The client classes:

- **`AnthropicClient`** wraps today's code: `messages.parse`, effort low, a cached system block, the `stop_reason` and refusal checks, and the existing usage extraction.
- **`OpenAICompatibleClient`** uses the official `openai` Python SDK (new dependency, pinned).
  - **OpenAI direct:** `base_url` is the default.
  - **OpenRouter:** `base_url="https://openrouter.ai/api/v1"`, plus `HTTP-Referer` / `X-Title` headers naming the app (no lab hostnames).
  - **Structured output:** `response_format={"type": "json_schema", "json_schema": {"name": ..., "strict": true, "schema": <pydantic JSON schema>}}`. Use the SDK's parse helper if it supports this; otherwise build the request explicitly and validate with pydantic.
  - **OpenRouter request extras:**
    - `provider: {"require_parameters": true, "max_price": {"prompt": <catalogue $/Mtok>, "completion": <catalogue $/Mtok>}}`, so a request is never routed to a provider that lacks structured output or charges more than the catalogue price;
    - `usage: {"include": true}`, so the actual cost comes back and is recorded.
  - **Token limit:** use `max_completion_tokens` where the API accepts it, so reasoning tokens are bounded too. Do not send `temperature`.
  - **Refusals:** a `refusal` field or `finish_reason` of `content_filter` → `refused=True`. Hitting `length` → failure.
- **Judge, Writer, Briefing and Consolidator** are refactored to take a `ModelClient` instead of an Anthropic client.
  - Their prompts, batching, validation and failure rules stay exactly as they are; only the transport changes.
  - `HaikuJudge`, `HaikuWriter` and `HaikuConsolidator` become provider-neutral classes (e.g. `LLMJudge`). Keep the old names as thin aliases only if tests or imports need them.
  - `JevJudge` is unchanged.

### 3.2 Model catalogue and prices (new `src/ai_newsdesk/llm/catalogue.py`)

- Fetch `https://openrouter.ai/api/v1/models`, which is public and needs no key. Keep a model only if all of these hold:
  - a numeric `pricing.prompt` and `pricing.completion` greater than 0;
  - `structured_outputs` in `supported_parameters`;
  - text input;
  - an id that does not end in `:free`.
- Store the filtered catalogue (`id`, `name`, prices per million tokens, context length) in the settings table (`model_catalogue`, with a `fetched_at` timestamp).
- **Refresh** at start-up if older than 24 h, nightly (scheduler `nightly_fn` chain), and from a "Refresh model list" button on Settings.
- **If a fetch fails,** keep the last good copy. If none exists, only the curated models priced in `config/pricing.yaml` are usable.
- **Prices by provider:**
  - **OpenRouter model `x/y`:** the catalogue price.
  - **OpenAI direct model `m`:** `pricing.yaml` if listed, else the catalogue price of `openai/m`.
  - **Anthropic:** `pricing.yaml`, as today.
  - A model with no price from these sources is **unusable**.
- **Worst case per call:** `estimate_tokens(system + user)` (UTF-8 bytes) × input price × 1.25, plus `max_tokens` × output price.
  - The 1.25 covers a possible cache write.
  - The byte count is an upper bound for byte-pair tokenisers.
  - `max_price` routing stops OpenRouter charging more than the catalogue price.

### 3.3 Curated shortlist (`config/models.yaml`, owner-editable)

The file lists ids per provider. The OpenRouter ids below were verified against the live catalogue on 2026-10-14; all are priced and support structured output.

```yaml
anthropic: [claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5]
openai: [gpt-6-luna, gpt-5.4-nano, gpt-5.6-luna, gpt-6.1-sol]
openrouter:
  - anthropic/claude-haiku-5.5
  - anthropic/claude-sonnet-5.5
  - anthropic/claude-opus-5.5
  - openai/gpt-6-luna
  - openai/gpt-5.4-nano
  - openai/gpt-5.6-luna
  - openai/gpt-6.1-sol
  - google/gemini-3.8-flash
  - google/gemini-4-argon
  - deepseek/deepseek-v4.1-flash
  - deepseek/deepseek-v4-pro-0813
  - qwen/qwen3.8-flash
  - qwen/qwen3.8-max-0902
  - mistralai/mistral-small-2603
  - mistralai/mistral-large-4-0
  - mistralai/mistral-medium-3-5
  - x-ai/grok-4.7
  - meta-llama/llama-4-maverick
  - z-ai/glm-5.3-flash
  - z-ai/glm-5.3
  - moonshotai/kimi-k2.6
  - minimax/minimax-m3
  - nvidia/nemotron-3.5-lightning
  - nvidia/nemotron-3-super-120b-a12b
  - cohere/command-a-plus
```

OpenAI direct model ids are the ids without the `openai/` prefix. OpenAI's own API is the source of truth. If an id is rejected, the Test button shows it.

### 3.4 Runtime settings

The `judge_provider` values become `anthropic | typesafe | openai | openrouter`. New provider fields:

- `writer_provider`, `briefing_provider` and `consolidator_provider`, each `anthropic | openai | openrouter`, default `anthropic`;
- `consolidator_model`, default `claude-haiku-5-5`. Until now the consolidator reused the judge or writer model.

Model fields stay free strings, but saving validates them:

- **Anthropic:** must be in `pricing.yaml`.
- **OpenAI:** must be priced, by `pricing.yaml` or by the catalogue entry for `openai/<id>`.
- **OpenRouter:** must be in the filtered catalogue.

Saving also checks that the matching provider key resolves (UI or `.env`); if not, it shows a form error naming the missing key. `build_context` builds one `ModelClient` per job.

### 3.5 Settings page

- **Models section:** one row per job, with a Provider select and a Model field.
  - For curated models, a `<select>` lists the shortlist with prices.
  - For **manual entry**, a text input with a `<datalist>` autocompletes from the filtered catalogue for that provider. The OpenAI datalist lists catalogue `openai/*` ids without the prefix.
  - Each row has a **Test** button: one tiny structured call (a fixed 1-item schema such as `{"ok": true}` with a ~10-token answer), budget-checked and recorded as spend. It shows ok or error (scrubbed), latency, and the price per million tokens.
  - A **judge warning** appears when the judge is not the Anthropic Claude Haiku default: "Claude Haiku was chosen by testing on 623 labelled items; other judges are untested on your news and may filter differently."
  - The model list's age is shown, with a "Refresh model list" button.
- **API keys section:** adds OpenAI and OpenRouter rows, write-only, tested before saving:
  - **OpenAI:** `GET /v1/models` with the key.
  - **OpenRouter:** `GET /api/v1/key`, which validates the key without spending.
- **Cost estimates** per update frequency use the chosen models' prices.

### 3.6 Ops and docs

- Add `OPENAI_API_KEY` and `OPENROUTER_API_KEY` to `.env.example`, the README configuration table and the compose env mapping. The compose policy test and the docs test must stay green.
- OPERATIONS.md: note that model prices come from the OpenRouter catalogue, refreshed daily.
- New ADR-0002: "OpenAI and OpenRouter via one OpenAI-compatible client; prices from the OpenRouter catalogue; structured-output-only models".

## 4. Compare page (owner request, same build)

This is the bake-off from ADR-0001, built into the app.

- **New page `/compare`, with a "Compare" nav item after Settings.**
  - **Setup form:**
    - **Job:** Judge or Summary writer.
    - **Models:** 2–4, each a provider plus a model (shortlist or manual entry, validated as in §3.4).
    - **Sample:**
      - Judge: the last N reviewed or queued non-X items from the last 7 days. N defaults to 30, maximum 100. The items get the same body text the real judge sees.
      - Writer: the top N stories by score from the last 48 h. N defaults to 5, maximum 15.
    - **Spend cap for this comparison:** default **$0.25**, editable from $0.01 to $5.00, never more than the money left in the month.
  - **Before running:** the page shows the **maximum cost**. That is the sum of every call's worst case, worked out the same way as the real calls. Run is disabled if the maximum is over the cap.
- **Running.**
  - A comparison is a background task (like Refresh now). It shares the pipeline lock, so it never overlaps a collection run. If a run is in progress, the page says "busy".
  - It calls each model with exactly the production prompts and batching, through `ModelClient`.
  - Every call is pre-checked against both the comparison cap and the monthly cap, and recorded in `spend`. Use `Purpose.REVIEW` for judge comparisons and `Purpose.WRITE` for writer comparisons.
  - Nothing it produces touches real items or stories: no verdicts are saved and no summaries are replaced.
- **Storage (new migration):**
  - `compare_runs` (id, job, models_json, sample_ids_json, cap_usd, status, started_at, finished_at, cost_usd, error);
  - `compare_results` (run_id, model_key, subject_id, output_json, ok, latency_ms, cost_usd);
  - `labels` (item_id PRIMARY KEY, label in {junk, show, important}, set_at). This is the owner's reusable label set; there is one current label per item.
  - `writer_prefs` (run_id, story_id, model_key, set_at). The owner's preferred summary per story.
- **Judge results view.**
  - **Summary table, one row per model:**
    - shown and hidden counts at the current show threshold;
    - junk count and average signal;
    - **agreement with the current production judge**, as % of items with the same show or hide decision;
    - **agreement with your thumbs**, where the item's story has a thumb: thumbs up counts as show, thumbs down as hide;
    - **accuracy against your labels**, where the item has one: junk means hide, show or important means show, and important items count double;
    - failures, total cost and average latency.
  - **Per-item list:** the title (escaped, with a link through `safe_url`) and each model's signal, junk flag and category, with **disagreements highlighted**.
  - **Labelling:** each item has three buttons, Junk / Show / Important (htmx POST, session + CSRF). They save to `labels` and the accuracy updates.
  - **"Use this model":** a button per model sets it as the production judge. It runs the same validation and shows the same judge warning as Settings.
- **Writer results view:**
  - for each story, the summaries side by side (escaped, plain text);
  - a "Prefer this" button per model, saved to `writer_prefs`;
  - a tally per model;
  - "Use this model" for the writer.
- **History:** the page lists past comparisons, newest first, with their status, cost and best model. Each opens its results view.
- **Safety:** model output is shown as escaped plain text only. Outbound links go through `safe_url` with `rel="noopener noreferrer"`. Errors are scrubbed. Keys are never shown.

## 5. Testing

- **Fake transports only:** `httpx.MockTransport` or respx for the openai SDK. The anthropic SDK uses httpx2, so follow the existing fake client.
- `ModelClient` contract tests run for each implementation. They cover:
  - success;
  - refusal;
  - invalid JSON;
  - `length`;
  - transport error;
  - usage and cost extraction (including OpenRouter's `usage.cost`);
  - the OpenRouter extras sent (`require_parameters`, `max_price`, `usage.include`, no `temperature`).
- Catalogue tests: filtering, persistence, refresh failure keeps the last good copy, price resolution by provider, unknown model unusable.
- Judge, writer and consolidator tests run against a fake `ModelClient`. The existing behaviour tests keep passing.
- Settings tests:
  - validation per provider;
  - missing key error;
  - datalist contents;
  - the Test button (budget-checked, scrubbed);
  - the judge warning;
  - keys write-only.
- Budget tests: the worst case covers the catalogue price × 1.25 plus `max_tokens` output, and a call that doesn't fit is never sent.
- Compare tests:
  - maximum-cost estimate, and refusal over the cap;
  - the cap enforced per call;
  - no production data changed;
  - result metrics, checked by hand against a fixed fixture;
  - labels stored and reused for accuracy;
  - htmx labelling (session + CSRF);
  - "Use this model" validation;
  - "busy" when the pipeline lock is held;
  - escaping of model output.
