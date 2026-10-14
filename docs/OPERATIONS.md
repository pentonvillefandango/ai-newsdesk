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
- API keys (`ANTHROPIC_API_KEY`, `RESEARCH_ENGINE_API_KEY`, `TYPESAFE_API_KEY`): change them in `.env` and `make deploy`, or save them on the Settings page (a saved key wins over `.env`).

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
- **Disk filling.** `df -h /`, `make status`, `du -sh backups .deploy`. Lower `BACKUP_RETENTION_DAYS`; only the owner deletes volumes or backups. Never run `docker system prune`.
