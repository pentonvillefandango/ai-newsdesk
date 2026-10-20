# Operations runbook

How to run, check, deploy, back up and repair ai-newsdesk on the shared `toolbox` VM. Every command works the same for the owner and for Claude Code. Run them from the repository root (`/opt/ai-newsdesk`).

## Quick check

```bash
make health             # expect "ok": true, "app_status": "up", "site_host_match": true, "password_set": true
make version            # the running image tag (= the deployed commit)
tail -n 1 deploys.jsonl # the last deploy: "result": "ok", and its "to" matches make version
```

If any of these looks wrong, go to [Troubleshooting](#troubleshooting). For a deeper check run `make smoke`.

## Conventions

- Every operation is `make <target>`, a thin wrapper around `ops/<script>.sh`.
- Progress goes to stderr. The **last line on stdout is one JSON object**, always with `ok` (true or false) and `command`. A failure adds `error`.
- The scripts exit **0** on success, **1** when the check or command failed, and **2** on a usage or precondition error (bad argument, dirty tree, weak secret, or the shared lock is held). `make` reports a failing script as `make: *** [Makefile:NN: <target>] Error 1` and then itself exits 2: read the JSON `ok`, or run `ops/<script>.sh` directly for the exact code.
- No script prints a secret. Logs and command output pass through a redactor that masks key, token, secret and password values, plus `X-API-Key`, `Authorization`, `Cookie` and `Set-Cookie` headers.
- The scripts address the stack only as Compose project `ai-newsdesk`, with `--env-file .env`. They never touch other projects or VM-wide Docker state, and never prune.
- Environment overrides: `BACKUP_DIR` (default `backups/`), `BACKUP_RETENTION_DAYS` (14), `BACKUP_LOCK_WAIT` (600 seconds), `CADDY_DIR` (`/opt/caddy`), `ENV_FILE` (`.env`), `DEPLOYS_LOG` (`deploys.jsonl`), `TAIL` (500 lines, for logs), `HASH_VIA` (`auto`, `uv` or `docker`, for set-password).

## Commands

| Command | What it does | Final JSON line |
| --- | --- | --- |
| `make help` | Lists the targets. | (plain text) |
| `make status` | Container state, health, restarts, CPU and memory, this project's volumes, the running image, last deploy and last backup. Read-only. Fails if `app` isn't running. | `{ok, command, containers, not_running, volumes, image, last_deploy, last_backup}` |
| `make health` | `/health` must answer 200; `app` must be running and healthy; `SITE_HOST` in `.env` must equal `AI_NEWSDESK_HOST` in `/opt/caddy/.env` (compared, never printed); `SECRETS_KEY` and `SESSION_SECRET` must be at least 32 characters and not `change-me`; `OWNER_PASSWORD_HASH` must be set. Read-only. | `{ok, command, app_status, http_status, containers, problems, site_host_match, site_host_reason, weak_secrets, password_set}` |
| `make smoke` | From inside the app container: `GET /health` is 200, `GET /login` is 200 with a form, `GET /` without a session is redirected (302/303/307) or refused (401). A few seconds. Read-only. | `{ok, command, checks: [{name, ok, status, ms}]}` |
| `make logs SERVICE=app SINCE=30m` | Recent app logs, redacted. `SINCE` is a number plus `s`, `m`, `h` or `d`. | log lines, then `{ok, command, service, lines}` |
| `make version` | The running image tag (the deployed short SHA), `.deploy/current` and the repo `HEAD`. Read-only. | `{ok, command, git_sha, deployed, repo_head, images}` |
| `make backup` | Online SQLite backup into `backups/`, then retention. The app keeps running. | `{ok, command, file, bytes, pruned}` |
| `make set-password` | Prompts twice for the owner password, hashes it (argon2id) and writes `OWNER_PASSWORD_HASH` into `.env`. Then run `make deploy`. | `{ok, command, key, env_mode, next}` |
| `make deploy` | Deploys the committed `HEAD`: worktree, build, `up --wait`, smoke; rolls back automatically on failure. **Approval required.** | ok: `{ok, command, from, to, smoke}`; failed: adds `error`, `detail`, `stack_changed`, `rolled_back_to`, `rollback_target`, `rollback_ok`, `rollback_branch`, `current` |
| `make rollback` or `make rollback SHA=<sha>` | Redeploys the last good commit (or the given one) on a new `rollback/<ts>` branch, then smoke. **Approval required.** | ok: `{ok, command, from, to, branch, smoke}`; failed: adds `error`, `switched`, `current`, `detail` |
| `make test` | Lint, format check, type check and unit tests. Touches nothing live. | (tool output) |

## Approvals and guardrails

- **Run freely (read-only or additive):** `make status`, `make health`, `make smoke`, `make logs`, `make version`, `make backup`, `make test`, `make help`.
- **Approval required** (they restart the live service): `make deploy` and `make rollback`. The owner approved the first deploy as part of the V1 plan; after that, ask, unless `HANDOFF.local.md` records a standing approval. Report every run.
- `make set-password` is interactive: the owner runs it (their password never passes through Claude Code).
- **Never, unless the owner explicitly asks:** `docker compose down -v`, deleting volumes or backups, `docker system prune` or any VM-wide clean-up, force-pushing.
- Never touch another tool's files, containers, volumes or `.env`. The only shared files this tool owns are `/opt/caddy/sites/ai-newsdesk.caddy` and the `AI_NEWSDESK_HOST` line in `/opt/caddy/.env`.

## Deploy and rollback

The live stack always runs a **committed** version: commit, then `make deploy`.

```text
/opt/ai-newsdesk/                main working tree (never reset or checked out by ops)
├── .deploy/
│   ├── <short-sha>/             one git worktree per deployed commit (12-character SHA)
│   ├── current -> <short-sha>   the live one; Compose runs from here
│   └── lock                     one deploy, rollback or backup at a time
├── deploys.jsonl                append-only history (git-ignored)
├── backups/                     mode 700, files mode 600 (git-ignored)
└── .env                         mode 600 (git-ignored)
```

`make deploy`:

1. Refuses (exit 2, nothing changed) on uncommitted changes, a weak `SECRETS_KEY`/`SESSION_SECRET`, or a held lock.
2. Creates (or reuses, after checking it) the worktree `.deploy/<short-sha>` and points `current` at it.
3. Runs `docker compose -p ai-newsdesk up -d --build --wait` from the worktree with `GIT_SHA` set, so the image is `ai-newsdesk-app:<short-sha>`.
4. Runs `make smoke`.
5. On success: appends an `ok` entry to `deploys.jsonl`, keeps the current worktree plus the 3 most recent others, and removes older `ai-newsdesk-app:<sha>` images by explicit tag (never one a container uses, never a prune).
6. On failure: appends a `failed` entry and rolls back to the last good commit. With none (the first deploy), it leaves the failed stack up for diagnosis.

`deploys.jsonl` holds one JSON object per line: `ts` (UTC), `action` (`deploy` or `rollback`), `from`, `to`, `result` (`ok` or `failed`), `smoke` (`pass`, `fail` or `skipped`), and when relevant `detail` and `branch`. Read it with `tail -n 5 deploys.jsonl`.

Recovery: an interrupted deploy leaves `current` on the new commit with no log entry; run `make deploy` again or `make rollback SHA=<last ok to>`. A registered worktree whose directory is gone: `git worktree prune`. Old `rollback/<ts>` branches: `git branch -d rollback/<ts>`.

## Backups and restore

- `make backup` runs `ai-newsdesk db backup` inside `app` (SQLite's online backup API, then `PRAGMA integrity_check`) and copies the result to `backups/ai-newsdesk-<UTC-timestamp>.sqlite` (mode 600). Retention deletes `ai-newsdesk-*.sqlite` files older than `BACKUP_RETENTION_DAYS` (14) and nothing else. Backups take the same lock as deploys.
- A backup holds the encrypted API keys. They decrypt only with the same `SECRETS_KEY`, so keep `.env` too (it is not in the backup).
- No nightly timer is installed in V1. Run `make backup` before risky changes.
- **Restore (manual, approval required: it stops the app).** From the repo root, with `F` the backup to restore:

  ```bash
  make backup                                       # safety copy of the current database
  F=backups/ai-newsdesk-<ts>.sqlite
  export GIT_SHA="$(readlink .deploy/current)"
  stage="$(mktemp -d backups/.restore.XXXXXX)" && install -m 644 "$F" "$stage/restore.sqlite"
  P=(docker compose -p ai-newsdesk --project-directory .deploy/current -f .deploy/current/compose.yaml --env-file .env)
  "${P[@]}" stop app
  "${P[@]}" run --rm --no-deps -T -v "$PWD/$stage/restore.sqlite:/restore.sqlite:ro" app \
    python -c "import os, shutil; [os.remove(p) for p in ('/data/ai-newsdesk.sqlite-wal', '/data/ai-newsdesk.sqlite-shm') if os.path.exists(p)]; shutil.copyfile('/restore.sqlite', '/data/ai-newsdesk.sqlite'); print('restored')"
  "${P[@]}" start app
  rm -rf "$stage"
  make health
  ```

## Model catalogue and prices

- Anthropic and TypeSafe prices come from `config/pricing.yaml`. OpenRouter prices (and OpenAI prices not listed in `pricing.yaml`) come from OpenRouter's public model list (`https://openrouter.ai/api/v1/models`, no key), filtered to priced models with structured output (variant ids such as `:free` or `:batch` are dropped) and stored in the `settings` table as `model_catalogue`.
- The list is refreshed at start-up when it is older than 24 hours (in the background) and nightly after 03:30. A failed fetch is logged (`model catalogue refresh failed`) and the last good copy is kept; with no copy at all, only `pricing.yaml` models are usable.
- Spend from OpenRouter is recorded at the cost OpenRouter reports for each call.
- **A call that may have been billed with no usage known is recorded at its worst case**: one row with no tokens, only the cost, and each call is recorded once.
  - That covers a call we abandon: a Test or Compare call that hits its time limit, and a judge, duplicate-merger, summary or briefing call cut off when the app shuts down mid-run.
  - It also covers a call that fails after the request may have reached the provider: the SDK timeout (120 seconds per call), a dropped connection, a 5xx or 408 reply, or any unexpected error. A TypeSafe Jev item that fails counts its worst case too.
  - A failure the provider never bills records nothing: any other 4xx (a bad key, a bad request, a rate limit) or a 529 (overloaded: rejected, not run).
  - The Anthropic and OpenAI SDKs never retry, so one call is one request (the optional TypeSafe SDK keeps its own defaults). A failed item or story is offered again on the next run instead. A refused or unparseable answer is recorded at the usage the provider reported.
  - So the spend meter can over-count but never under-count. The one exception: a Test cut off at shutdown after the database has closed is logged (`a cancelled test's worst case was not recorded`) instead of recorded.
- The **Models** page (in the nav after Settings) holds each job's provider and model, the model list and the Test buttons. It opens with a diagram of the end-to-end flow, built from the saved settings: AI steps (accent-coloured) show the provider, model and price chosen now; tool and code steps are neutral.
- The Models page shows the list's age ("Model list updated 3 h ago") and a **Refresh model list** button. A failed refresh says so and keeps the previous list.
- The shortlist offered on the Models page is `config/models.yaml`: model ids per provider (`anthropic`, `openai`, `openrouter`), in display order. Edit it in the repo and deploy; an id must be unique within its provider. A listed id with no known price shows as "no price yet" and can't be chosen. Any other usable model can be typed into a job's "Other model" box.
- The Models page's **Test** button makes one structured call to the chosen model (at most 512 output tokens), checked against the budget first and recorded as review spend. It is refused, with nothing sent, when the model is unpriced, its key is missing, the budget can't cover it or a run is in progress (a test holds the run lock, so its spend always counts against the cap). The call gets one attempt and 30 seconds; a test that times out may still be billed, so it is recorded at its worst case. A scheduled run that falls due while the lock is held waits for it, up to 20 minutes, and is skipped with a log line (`the lock was held for 1200 s; schedule run skipped`) only after that; Refresh now still says a run is already going. If a collection run (Refresh now, say) finished while it waited, the scheduled run is skipped instead (`a run finished while waiting; schedule run skipped`); after a Test it runs as normal.
- Choosing a judge other than Claude Haiku on Anthropic shows a warning: Haiku is the judge tested on 623 labelled items (ADR-0001, ADR-0002).
- Each AI job (judge, summary writer, briefing writer, duplicate-merger) has its own provider and model. A saved choice that can't be used at start-up (its price is unknown, or its OpenAI or OpenRouter key is missing) never stops the app: that job uses Claude Haiku and the log says `<job> model <provider>:<model> is unusable (<reason>)`. After each successful list refresh the jobs are rebuilt when any job uses OpenAI or OpenRouter, so a model that was waiting for the list is picked up.
- The duplicate-merger has its own model setting (default Claude Haiku). Before multi-provider it reused the judge's Claude model (or the writer's), so an install whose judge was on Sonnet or Opus merges with Haiku after the upgrade until the merger's model is changed on the Models page.

## Compare page

- **Compare** (after Models) runs 2 to 4 models over the same sample with the production prompts and batches: the judge over the last reviewed or queued news items of 7 days (default 30, up to 100), the summary writer over the top stories of 48 hours (default 5, up to 15). Every provider needs its key here, Anthropic included.
- **Check cost** shows the maximum cost (every call's worst case); **Run comparison** stays disabled until it fits the comparison's cap (default $0.25, $0.01 to $5.00, never more than the money left this month). Each call is checked against both the cap and the monthly budget before it is sent and recorded as review or write spend; a model whose next call doesn't fit stops there (status "Stopped at the cap", the rest "Not run").
- A comparison holds the run lock, like a collection run. Starting one while a run is going says so at once and creates nothing. Each call gets 120 seconds and no SDK retry (a retried request could be billed twice), and the whole comparison 15 minutes; past that it stops as failed and frees the lock (a scheduled run waiting for the lock then goes ahead). A call that times out, or is still waiting when the 15 minutes run out, is recorded at its worst case, in the month's spend, the comparison's cost and that model's cost. A comparison cut off by a restart is marked failed at start-up.
- Results live in `compare_runs` and `compare_results`; nothing is written to items or stories. Labels (Junk / Show / Important, one per item, in `labels`) are kept and reused by later comparisons; preferred summaries are in `writer_prefs`. **Use this model** applies the same checks as the Models page and switches the production judge or summary writer from the next run.

## The briefing and Read aloud (ElevenLabs)

**If today's briefing is missing.** After the briefing time the Briefing page shows "Today's briefing isn't ready", with the latest earlier briefing below it. While the scheduler is still retrying it says "Tried N of M times; the last try failed"; when it has stopped (6 tries a day, 30 minutes apart) it says so. The notice has a **Write it now** button: it writes today's briefing in the background under the same run lock as the scheduler, then makes its audio. If a run holds the lock the page says "A run is in progress; try again in a few minutes" and starts nothing. Reload after about a minute.

**What it does.** After each briefing is saved, the app turns it into an MP3 with ElevenLabs so it is ready to play when the owner sits down. The Briefing page shows a player with speed buttons. The text read is the briefing with markdown, URLs and citation links that end a sentence dropped, under a spoken opening line.

- **Where the audio lives.** `/data/audio/` on the `app-data` volume, one file per briefing day, named `briefing-<date>-<random>.mp3` (64 kbps MP3, about 0.5 MB a minute: a 6 to 7 minute briefing is 3 to 4 MB). A row in the database (`briefing_audio`) names the voice, model, characters and file. The audio route is login-protected and supports seeking.
- **Retention.** The nightly upkeep (after 03:30, under the run lock) deletes audio for briefing days more than 30 days old, any `briefing-*.mp3` with no database row, and `.mp3.tmp` leftovers older than 1 hour. It touches no other file. At 30 days expect about 100 MB.
- **Backups do not include audio.** `make backup` copies the SQLite database only. After a restore, the rows of missing files make the Briefing page offer **Make audio** again; it costs characters, so remake only the days you want.
- **Retries.** If a day's briefing has no audio, the scheduler tries to make it: at most 3 failed tries a day, 30 minutes apart. A run that holds the lock is skipped until the next tick. Narration never changes the briefing's own retry state.
- **Make audio / Remake audio.** The button on a briefing page (any day) makes the audio in the background and returns to the page ("Making audio. This takes a minute or two; reload to listen."). It replaces the day's old audio and spends characters again. If a run is in progress it says so and starts nothing. A briefing rewritten while its audio is being made discards that audio.
- **Character cap versus the plan limit.** ElevenLabs bills in credits (characters times the model's factor), so the meter is kept apart from the dollar budget. It counts characters actually sent, including requests that may have been billed (a timeout, a 5xx, or chunks sent before a failure), so it may over-count and never under-counts. A clean refusal (401, 422, 429) records nothing. Before each narration the run checks `used this month + this text` against **Monthly character cap** (default 300,000); over it, no audio is made and the page says "This month's audio allowance is used up." The Creator plan allows 363,000 characters a month (Settings shows what ElevenLabs reports). The default model `eleven_v4` has factor 1.0; `eleven_flash_v2_5` costs half the credits. A real briefing is about 5,400 characters, so about 165k a month at one a day.
- **The key.** Add the ElevenLabs key on Settings (write-only, like the others) or as `ELEVENLABS_API_KEY` in `.env`; the saved key wins and applies at once. Without a key nothing is sent and the Briefing page says "Add an ElevenLabs key in Settings to hear this briefing."
- **Voices and samples.** Settings, Read aloud, offers three suggested voices (Daniel, Lily, Matilda) and a dropdown of the account's voices, cached for 24 hours and refreshed nightly or with **Refresh voices** (if ElevenLabs cannot be reached the old list stays). Samples play through the app (`/settings/voice-sample/<id>`): previews are free, the page stays on its own origin, and the key is sent only to `*.elevenlabs.io`.
- **Switching it off.** Untick **Read the briefing aloud** in Settings (Read aloud section) (stored as `narration_enabled`) and save. No more audio is made, scheduled or manual, and no key is used; existing files stay until the 30-day upkeep. Removing the key has the same effect on spending. Neither needs a deploy.
- **Quota recovery.** If the cap is reached, raise **Monthly character cap** (within the plan limit) and press **Make audio**, or wait for the month to roll over; the scheduler stops trying that day once the cap blocks it. If ElevenLabs refuses (401, 422, 429), check the key and plan on the ElevenLabs site; nothing was billed.

## Known limitations

- **Global login lockout.** The app runs uvicorn with `--no-proxy-headers`, so it never trusts `X-Forwarded-*` (any container on the shared `proxy` network could forge them). Because it cannot tell clients apart, the login limiter is global: 5 wrong passwords in 15 minutes, from anyone, lock the login for everyone until the oldest failure is 15 minutes old (the page says how long to wait). Redirects are relative paths and the `__Host-` cookies are always `Secure`, so neither depends on the forwarded scheme; a trailing-slash URL (such as `/settings/`) is a 404, never an `http://` redirect. Trade-off, accepted for a single-owner lab tool: a co-tenant container on the `proxy` network (or anyone in the lab subnet) can lock the owner out for up to 15 minutes by guessing wrong.
- **Plain `make bootstrap` in research-engine syncs from its deployed commit.** Until research-engine is redeployed with the AI Newsdesk index link, run `DEPLOY_DIR=/opt/research-engine make bootstrap` there (it then syncs from the working tree) to get the link onto the index page. A plain `make bootstrap` can put the old index back.

## Owner password

`make set-password` prompts twice (at least 12 characters), sends the password to `ai-newsdesk hash-password` on stdin (never on a command line), and writes `OWNER_PASSWORD_HASH='<hash>'` into `.env`. The single quotes are required: an argon2 hash contains `$`, and Docker Compose would otherwise treat each `$name` as a variable and silently break the hash. The script never echoes the password or the hash, and keeps every other line and mode 600. It hashes with `uv run` when `uv` is installed, otherwise in a one-off app container; set `HASH_VIA=uv` or `HASH_VIA=docker` to choose. Then `make deploy` (the app reads `.env` only at start-up). Login uses the cookies `__Host-nd_session` and `__Host-nd_csrf`. Browsers only accept the `__Host-` prefix on HTTPS (or on `localhost`), so log in through the Caddy URL, not plain HTTP. Without a hash the login page shows a banner and nobody can log in; `make health` reports `password_set: false`.

## Rotating secrets

```bash
sed -i "s/^SESSION_SECRET=.*/SESSION_SECRET=$(openssl rand -hex 32)/" .env && make deploy
```

- `SESSION_SECRET`: everyone is logged out.
- `SECRETS_KEY`: it encrypts the API keys saved on the Settings page. After rotating it those saved keys can't be decrypted: the app falls back to the `.env` keys, and you re-enter the keys on the Settings page.
- API keys (`ANTHROPIC_API_KEY`, `RESEARCH_ENGINE_API_KEY`, `TYPESAFE_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `ELEVENLABS_API_KEY`): change them in `.env` and `make deploy`, or save them on the Settings page (a saved key wins over `.env`).

Never print or commit `.env`. Check values by length only, e.g. `v="$(grep -m1 '^SECRETS_KEY=' .env | cut -d= -f2-)"; echo "${#v}"; v=`.

## Caddy site

The shared Caddy (`/opt/caddy`, Compose project `caddy`) is the only way in. Our part of it:

- **Site file:** `deploy/caddy/ai-newsdesk.caddy` in this repo (added in V1 step 8), installed as `/opt/caddy/sites/ai-newsdesk.caddy`. Lab-only (`@lab remote_ip {$LAB_SUBNET}`), `tls internal`, `reverse_proxy ai-newsdesk-app:8000` over the shared `proxy` network, `respond 403` for everyone else. No `trusted_proxies` (Caddy sets `X-Forwarded-*` from the real client; the app ignores them anyway, see Known limitations) and no `flush_interval` (the app doesn't stream).
- **Host variable:** `AI_NEWSDESK_HOST=ai-newsdesk.toolbox.home.arpa` in `/opt/caddy/.env` (Caddy's own `SITE_HOST` there belongs to research-engine). DNS: a UniFi CNAME `ai-newsdesk.toolbox.home.arpa` → `toolbox.home.arpa`.
- **research-engine's `make bootstrap`** syncs its `deploy/caddy/` into `/opt/caddy`, copying only changed files, never deleting anything and never copying a `.env`, and in `/opt/caddy/.env` it rewrites only `SITE_HOST`, `LAB_SUBNET` and `TOOLBOX_HOST`, keeping every other line. So our site file and our `AI_NEWSDESK_HOST` line survive every bootstrap. The index page (`toolbox.home.arpa`) and its "AI Newsdesk" link live in research-engine's `deploy/caddy/index/index.html`: change them there, by PR, never in `/opt/caddy/index`.
- **Reload or recreate:** `sites/` is a directory mount, so a changed site file needs only `cd /opt/caddy && docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`. A change to `/opt/caddy/.env` needs `cd /opt/caddy && docker compose up -d --wait --force-recreate` (the container reads its env only at creation).

Install or update (validate before recreating; nothing from the env file is printed):

```bash
install -m 644 deploy/caddy/ai-newsdesk.caddy /opt/caddy/sites/ai-newsdesk.caddy
grep -q '^AI_NEWSDESK_HOST=' /opt/caddy/.env || {
  [ -z "$(tail -c1 /opt/caddy/.env)" ] || echo >> /opt/caddy/.env
  echo 'AI_NEWSDESK_HOST=ai-newsdesk.toolbox.home.arpa' >> /opt/caddy/.env
}
docker run --rm --network none --env-file /opt/caddy/.env -v /opt/caddy:/etc/caddy:ro caddy:2.11.6 \
  caddy validate --config /etc/caddy/Caddyfile 2>&1 | grep -c 'Valid configuration'   # must print 1
cd /opt/caddy && docker compose up -d --wait --force-recreate
```

If validation prints `0`, don't recreate: roll back (below) and fix the site file first.

Roll back our Caddy changes (touches only our file and our one line):

```bash
rm -f /opt/caddy/sites/ai-newsdesk.caddy
sed -i '/^AI_NEWSDESK_HOST=/d' /opt/caddy/.env
cd /opt/caddy && docker compose up -d --wait --force-recreate
cd /opt/research-engine && make health   # research-engine must still be ok
```

## Host-side checks

From the VM itself, requests through `127.0.0.1` get **403 by design**: Caddy sees a Docker bridge address, outside `LAB_SUBNET`. Use the VM's lab IP with `--resolve`:

```bash
LAB_IP="$(hostname -I | awk '{print $1}')"
curl -sk -o /dev/null -w '%{http_code}\n' --resolve "ai-newsdesk.toolbox.home.arpa:443:$LAB_IP" https://ai-newsdesk.toolbox.home.arpa/login
```

`make health` and `make smoke` need neither: they run inside the container.

## Dev stack (never replaces the live one)

```bash
APP_PORT=8765 docker compose -p ai-newsdesk-dev -f compose.yaml -f compose.dev.yaml -f compose.debug.yaml --env-file .env up -d --build --wait
# http://localhost:8765/login
APP_PORT=8765 docker compose -p ai-newsdesk-dev -f compose.yaml -f compose.dev.yaml -f compose.debug.yaml --env-file .env down
```

Always pass `-p ai-newsdesk-dev` with `compose.dev.yaml`; without it the override would apply to project `ai-newsdesk` and replace the live stack. Set `APP_PORT` in the shell, not in `.env`: a non-empty `APP_PORT` in `.env` makes the ops scripts publish the **live** app on loopback too. The dev stack uses its own network (`ai-newsdesk-dev-proxy`), so it cannot reach research-engine; integration tests that need research-engine run on the host against the live one:

```bash
bundle="$(mktemp)"; cat "$(uv run python -m certifi)" /opt/caddy/root.crt > "$bundle"
RESEARCH_ENGINE_URL=https://research.toolbox.home.arpa SSL_CERT_FILE="$bundle" uv run pytest -m integration
rm -f "$bundle"
```

Without `RESEARCH_ENGINE_URL` those tests skip.

## Troubleshooting

Start with `make status`, `make health` and `tail -n 5 deploys.jsonl`.

- **The site doesn't load in the owner's browser, but `curl --resolve` from the VM works.** Suspect Chrome's "secure DNS" first: it asks a public DNS-over-HTTPS resolver, which doesn't know `*.toolbox.home.arpa`. Turn it off (Settings → Privacy and security → Security → Use secure DNS) or use the lab resolver. Also check the UniFi CNAME `ai-newsdesk.toolbox.home.arpa` → `toolbox.home.arpa` exists.
- **403 from Caddy.** Expected from `127.0.0.1` and from outside `LAB_SUBNET`. From a lab client it means the client's address isn't in `LAB_SUBNET`.
- **`site_host_match: false`.** `SITE_HOST` in `.env` differs from `AI_NEWSDESK_HOST` in `/opt/caddy/.env` (`site_host_reason` says which is missing). Fix `.env` and `make deploy`, or fix the Caddy line (see Caddy site) and recreate Caddy.
- **`password_set: false` or the login banner.** Run `make set-password`, then `make deploy`.
- **`weak_secrets` names a key.** Replace it with `openssl rand -hex 32`, then `make deploy`.
- **App unhealthy or restarting.** `make logs SINCE=1h`. Out of memory: raise `APP_MEM_LIMIT` in `.env` and `make deploy`. Started after a deploy: `make rollback`.
- **No new stories.** Check the Settings page: the monthly budget may be used up (AI calls are skipped, never sent over the cap), the daily review cap reached, or an API key missing. research-engine down: X and search sources skip with a recorded reason; check `cd /opt/research-engine && make health` (read-only).
- **A briefing has no audio.** The Briefing page says why (off, no key, allowance used up, failed and will be retried, or failed today). See [The briefing and Read aloud](#the-briefing-and-read-aloud-elevenlabs); **Make audio** retries by hand. `make logs SINCE=1h` shows `audio for <date>` lines (no key values are ever logged).
- **Disk filling.** `df -h /`, `make status`, `du -sh backups .deploy`. Lower `BACKUP_RETENTION_DAYS`; only the owner deletes volumes or backups. Never run `docker system prune`.
