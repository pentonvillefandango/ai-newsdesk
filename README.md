# ai-newsdesk

A self-hosted, high-signal AI news feed. It collects news from AI labs and from a curated list of respected people (X through [research-engine](https://github.com/pentonvillefandango/research-engine) searches, never paid X access), filters out the noise with Claude Haiku, merges duplicates into single stories, summarises them, and writes a daily briefing, behind a polished, server-rendered web UI.

- Spec: [docs/superpowers/specs/2026-10-14-v1-design.md](docs/superpowers/specs/2026-10-14-v1-design.md). Decisions: [docs/adr/](docs/adr/).
- Operations runbook: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Features (V1)

- Sources: lab feeds, trusted people on X (via research-engine `site:x.com/<handle>` searches), topic searches, Hacker News, arXiv and GitHub releases.
- An AI judge (Claude Haiku by default; optional TypeSafe Jev) scores signal and junk; duplicates become one story with every source linked (a consolidation pass merges stories that report the same event). Posts from people on X are kept out of news stories and shown on their own X page (last 7 days, newest first).
- Story summaries with an optional "why it matters" line, and a daily briefing.
- A hard monthly AI budget (default $5), checked before every call.
- Each AI job (judge, summary writer, briefing writer, duplicate merger) can use its own provider: Anthropic (default), OpenAI or OpenRouter. The Models page offers a priced shortlist (`config/models.yaml`), any other priced model with structured output by typing its id, and a Test button that makes one tiny, budget-checked call. It opens with a live diagram of the desk's end-to-end flow: when each AI model is used (with the provider, model and price chosen now) and when the tools run.
- A Compare page runs 2 to 4 models over the same recent news with the production prompts, under its own spend cap, and shows how they differ: agreement with the current judge, your thumbs and your labels, or summaries side by side. One click makes the winner the production model.
- Settings page: run frequency, caps, write-only API keys (each tested before it is saved), spend meter, cost estimates priced with the chosen models.
- Wide "lead + grid" layout on desktop, single column on phones; an Auto / Light / Dark theme toggle and a choice of six accent colours (Amber, Blue, Green, Violet, Rose, Teal), both remembered per browser.
- One owner password; lab-only behind a shared Caddy with HTTPS.

## Quick start

You need Docker with the Compose plugin, `uv` and `openssl`. This runs a local dev stack as its own Compose project (`ai-newsdesk-dev`), so it never touches a live `ai-newsdesk` stack on the same host.

```bash
git clone https://github.com/pentonvillefandango/ai-newsdesk.git
cd ai-newsdesk
uv sync
cp .env.example .env && chmod 600 .env
for k in SECRETS_KEY SESSION_SECRET; do
  sed -i "s/^$k=.*/$k=$(openssl rand -hex 32)/" .env
done
make set-password                 # prompts twice; writes OWNER_PASSWORD_HASH into .env
APP_PORT=8765 docker compose -p ai-newsdesk-dev -f compose.yaml -f compose.dev.yaml -f compose.debug.yaml --env-file .env up -d --build --wait
```

Open `http://localhost:8765/login` (use `localhost`: browsers accept the app's secure session cookie there) and log in. Add your Anthropic key on the Settings page (or as `ANTHROPIC_API_KEY` in `.env`). Stop it with the same command ending in `down` (add `-v` only to delete the dev data).

## Configuration

All settings come from `.env` (copy `.env.example`; mode 600; never commit it). Run-time tunables (frequency, caps, thresholds, "why it matters") live in the database and are edited on the Settings page; each job's model is chosen on the Models page.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SITE_HOST` | `ai-newsdesk.localhost` | Public host name. In production it must equal `AI_NEWSDESK_HOST` in `/opt/caddy/.env` (`make health` checks). |
| `SECRETS_KEY` | none (secret) | Encrypts API keys saved on the Settings page. At least 32 characters: `openssl rand -hex 32`. |
| `SESSION_SECRET` | none (secret) | Signs the session cookie. At least 32 characters. |
| `OWNER_PASSWORD_HASH` | empty | argon2id hash of the owner password, written single-quoted by `make set-password`. Empty disables login. |
| `ANTHROPIC_API_KEY` | empty (secret) | Fallback Claude key; a key saved on the Settings page wins. |
| `RESEARCH_ENGINE_API_KEY` | empty (secret) | Fallback research-engine key (its `API_KEY`). |
| `TYPESAFE_API_KEY` | empty (secret) | Only for the optional Jev judge. |
| `OPENAI_API_KEY` | empty (secret) | Fallback OpenAI key, only for jobs set to OpenAI. |
| `OPENROUTER_API_KEY` | empty (secret) | Fallback OpenRouter key, only for jobs set to OpenRouter. |
| `ELEVENLABS_API_KEY` | empty (secret) | Fallback ElevenLabs key, only for reading the briefing aloud. |
| `RESEARCH_ENGINE_URL` | `http://research-engine-app:8000` | research-engine over the shared `proxy` network. |
| `DB_PATH` | `data/ai-newsdesk.sqlite` | SQLite file for local runs. Compose fixes it to `/data/ai-newsdesk.sqlite` on the `app-data` volume. |
| `TZ` | `Europe/London` | Time zone for schedules and the budget month. |
| `LOG_LEVEL` | `INFO` | App log level. |
| `GIT_SHA` | `unknown` | Leave as is: `make deploy` sets it per run; it tags the image. |
| `SCHEDULER_ENABLED` | `true` | Turn the in-process scheduler off (`false`) for debugging. |
| `APP_MEM_LIMIT` | `512m` | Compose memory limit (Compose only). |
| `APP_CPUS` | `0.5` | Compose CPU limit (Compose only). |
| `APP_PORT` | empty | Publishes the app on `127.0.0.1:<port>` via `compose.debug.yaml`. Keep empty on the live host. |

## Deployment

One Docker Compose project (`ai-newsdesk`) behind the VM's shared Caddy, which serves HTTPS with its internal CA and admits only the lab subnet. The example deployment is `https://ai-newsdesk.toolbox.home.arpa`. Deploys, rollbacks, backups and the Caddy site are in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Development

```bash
uv sync
uv run pre-commit install         # gitleaks and file hygiene on every commit
make test                         # ruff, ruff format --check, pyright, pytest
```

Unit tests fake every external service. Integration tests (`-m integration`) are deselected by default; the ones that need research-engine run against a live instance through `RESEARCH_ENGINE_URL` and skip without it (see [docs/OPERATIONS.md](docs/OPERATIONS.md)).

## Licence

MIT. See [LICENSE](LICENSE).
