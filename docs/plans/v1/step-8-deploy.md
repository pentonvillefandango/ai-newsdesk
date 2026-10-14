# Step 8: Caddy site, index link, first deploy and handoff (tasks 39–42)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers-extended-cc:subagent-driven-development (or executing-plans). Steps use `- [ ]` checkboxes. Read the master plan `docs/plans/2026-10-14-ai-newsdesk-v1.md` (Global Constraints: the Caddy steps and the research-engine index PR are the **only** permitted touches outside `/opt/ai-newsdesk`), `docs/plans/v1/step-7-ops-docs.md` and `docs/OPERATIONS.md` ("Caddy site") first.

**Goal:** Put ai-newsdesk live at `https://ai-newsdesk.toolbox.home.arpa`, lab-only behind the shared Caddy, linked from the toolbox index page, verified end to end with real evidence, and leave a handoff for the next session.

**Branch:** `v1/step-8-deploy` (from `main` after step 7 is merged) for the repo changes (Task 39), merged before the deploy. In `/opt/research-engine`: branch `feat/index-ai-newsdesk` for the index link (Task 40). Tasks 41–42 are host operations from `main`.

**Depends on:** steps 0–7 merged into `main`.

**Order (owner asked for the Caddy parts to be done carefully):** repo site file and test (39) → index-link PR (40, independent of the deploy) → deploy the app and see it healthy (41 A–E) → only then install the site, add the host variable, validate, recreate Caddy (41 F–G) → end-to-end verification (41 H–L) → handoff (42).

**Never** print a secret value or a lab IP into the repo, the PR text or the plan. On the VM the lab IP is always `$(hostname -I | awk '{print $1}')`.

## Contract additions

- **Caddy site file:** `deploy/caddy/ai-newsdesk.caddy` (repo) → `/opt/caddy/sites/ai-newsdesk.caddy` (host, mode 644). Site address `{$AI_NEWSDESK_HOST}`, `tls internal`, `@lab remote_ip {$LAB_SUBNET}`, `reverse_proxy ai-newsdesk-app:8000`, `respond 403`. No `trusted_proxies`, no `flush_interval`.
- **Caddy host variable:** `AI_NEWSDESK_HOST=ai-newsdesk.toolbox.home.arpa`, one line appended to `/opt/caddy/.env` (never in research-engine's `deploy/caddy/.env.example`, whose keys its test pins to `SITE_HOST`, `LAB_SUBNET`, `TOOLBOX_HOST`).
- **Our `.env`:** `SITE_HOST=ai-newsdesk.toolbox.home.arpa` in production.
- **DNS:** UniFi CNAME `ai-newsdesk.toolbox.home.arpa` → `toolbox.home.arpa` (the owner adds it).
- **Index link** (research-engine `deploy/caddy/index/index.html`): `<li><a href="https://ai-newsdesk.toolbox.home.arpa/">AI Newsdesk</a></li>`, directly after the Research Engine item; research-engine's `tests/test_caddy_policy.py` allows the host `ai-newsdesk.toolbox.home.arpa` and asserts the link.
- **Index sync:** `DEPLOY_DIR=/opt/research-engine make bootstrap` in `/opt/research-engine`. Plain `make bootstrap` syncs from research-engine's `.deploy/current` (its live commit), which does not contain the link yet.
- **Test:** `tests/test_caddy_site.py`.

---

### Task 39: Caddy site file in the repo

**Goal:** A lab-only Caddy site file for ai-newsdesk, pinned by a test, merged to `main` before anything touches `/opt/caddy`.

**Files:**
- Create: `deploy/caddy/ai-newsdesk.caddy`
- Create: `tests/test_caddy_site.py`

**Acceptance Criteria:**
- [ ] The site uses `{$AI_NEWSDESK_HOST}` and `{$LAB_SUBNET}`, `tls internal`, `handle @lab { reverse_proxy ai-newsdesk-app:8000 }` and `respond 403`.
- [ ] No `trusted_proxies`, no `flush_interval`, no IP other than `127.0.0.1`, no `*.home.arpa` host name in the site file.
- [ ] `docs/OPERATIONS.md` documents install (with validation), reload vs recreate, bootstrap survival and rollback.
- [ ] `caddy validate` accepts the file with example values.
- [ ] Merged into `main` with CI green.

**Verify:** `uv run pytest tests/test_caddy_site.py -v` → 8 passed; the `caddy validate` command in step 5 prints `Valid configuration`.

**Steps:**

- [ ] **Step 1: Write the failing test** `tests/test_caddy_site.py`:

```python
"""The ai-newsdesk site file for the shared Caddy (V1-S8): lab-only, internal TLS, our alias."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE_PATH = ROOT / "deploy" / "caddy" / "ai-newsdesk.caddy"
OPERATIONS = ROOT / "docs" / "OPERATIONS.md"


def _site() -> str:
    return SITE_PATH.read_text()


def _code() -> str:
    """The site file with comments stripped."""
    return "\n".join(line.split("#", 1)[0] for line in _site().splitlines())


def test_site_address_is_our_host_variable() -> None:
    assert re.search(r"^\{\$AI_NEWSDESK_HOST\} \{$", _code(), re.MULTILINE)
    assert "SITE_HOST" not in _code(), "Caddy's SITE_HOST belongs to research-engine"


def test_tls_internal_and_lab_gate() -> None:
    code = _code()
    assert "tls internal" in code
    assert "@lab remote_ip {$LAB_SUBNET}" in code
    assert "handle @lab {" in code


def test_proxies_only_to_our_alias() -> None:
    assert re.findall(r"reverse_proxy\s+(\S+)", _code()) == ["ai-newsdesk-app:8000"]


def test_everyone_else_gets_403() -> None:
    assert re.search(r"^\s*respond 403\s*$", _code(), re.MULTILINE)


def test_no_trusted_proxies_and_no_streaming_options() -> None:
    code = _code()
    assert "trusted_proxies" not in code
    assert "flush_interval" not in code


def test_no_ips_or_real_hosts() -> None:
    text = _site()
    for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
        assert ip == "127.0.0.1", ip
    assert "home.arpa" not in text


def test_braces_balance() -> None:
    code = _code()
    assert code.count("{") == code.count("}")


def test_operations_documents_install_and_rollback() -> None:
    text = OPERATIONS.read_text()
    for needle in (
        "install -m 644 deploy/caddy/ai-newsdesk.caddy /opt/caddy/sites/ai-newsdesk.caddy",
        "grep -q '^AI_NEWSDESK_HOST=' /opt/caddy/.env ||",
        "caddy validate --config /etc/caddy/Caddyfile",
        "docker compose up -d --wait --force-recreate",
        "caddy reload --config /etc/caddy/Caddyfile",
        "rm -f /opt/caddy/sites/ai-newsdesk.caddy",
        "sed -i '/^AI_NEWSDESK_HOST=/d' /opt/caddy/.env",
        "make bootstrap",
    ):
        assert needle in text, needle
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_caddy_site.py -v`
Expected: 7 FAIL with `FileNotFoundError` for `deploy/caddy/ai-newsdesk.caddy`; `test_operations_documents_install_and_rollback` passes (step 7 wrote the section).

- [ ] **Step 3: Write `deploy/caddy/ai-newsdesk.caddy`** (indent with tabs, as `caddy fmt` does):

```caddyfile
# AI Newsdesk on the shared Caddy: copy to /opt/caddy/sites/ai-newsdesk.caddy (docs/OPERATIONS.md).
# Env (from /opt/caddy/.env): AI_NEWSDESK_HOST, LAB_SUBNET. Lab clients only; everyone else 403.
# Caddy sets X-Forwarded-* from the real client (never configure trusted proxies here).
# The app does not stream, so the default response buffering is fine.
{$AI_NEWSDESK_HOST} {
	tls internal

	@lab remote_ip {$LAB_SUBNET}
	handle @lab {
		reverse_proxy ai-newsdesk-app:8000
	}

	# everything outside the lab subnet
	respond 403
}
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `uv run pytest tests/test_caddy_site.py -v`
Expected: 8 passed.

- [ ] **Step 5: Validate the file with Caddy itself** (example values; no network, no ports, container removed afterwards; the image is the one the shared Caddy already uses):

```bash
docker run --rm --network none -e AI_NEWSDESK_HOST=ai-newsdesk.localhost -e LAB_SUBNET=127.0.0.1/32 \
  -v "$PWD/deploy/caddy/ai-newsdesk.caddy:/etc/caddy/Caddyfile:ro" caddy:2.11.6 \
  caddy validate --config /etc/caddy/Caddyfile 2>&1 | tail -n 1
```

Expected: a line ending in `Valid configuration`.

- [ ] **Step 6: Commit, push, PR, merge**

```bash
uv run ruff format tests && make test
git add deploy/caddy/ai-newsdesk.caddy tests/test_caddy_site.py
git commit -m "feat(deploy): lab-only Caddy site for ai-newsdesk (V1-S8)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git push -u origin v1/step-8-deploy
GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr create --base main \
  --title "V1 step 8: Caddy site file (V1-S8)" \
  --body "deploy/caddy/ai-newsdesk.caddy for the shared Caddy (AI_NEWSDESK_HOST, LAB_SUBNET gate, tls internal, reverse_proxy ai-newsdesk-app:8000, 403 otherwise) and its policy test. Host install steps are in docs/OPERATIONS.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr checks --watch
GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr merge --merge --delete-branch
git checkout main && git pull --ff-only
```

Expected: checks pass, PR merged, `git log --oneline -1` on `main` is the merge commit. (Run `requesting-code-review` on the diff before merging.)

---

### Task 40: "AI Newsdesk" link on the toolbox index page, and the DNS record

**Goal:** The toolbox index page links to ai-newsdesk, changed the house way (research-engine PR, then its bootstrap sync), and the owner is asked once for the DNS record.

**Files:** (in `/opt/research-engine`, through its PR flow only)
- Modify: `deploy/caddy/index/index.html`
- Modify: `tests/test_caddy_policy.py`

**Acceptance Criteria:**
- [ ] research-engine PR merged with CI green; its `tests/test_caddy_policy.py` allows `ai-newsdesk.toolbox.home.arpa` and asserts the link; `deploy/caddy/.env.example` is unchanged.
- [ ] `/opt/caddy/index/index.html` contains the link after the bootstrap sync, and the bootstrap changed nothing else (`changed == ["index/index.html"]`, no `.env` keys, no systemd change, no Caddy recreate).
- [ ] The owner has been asked, once, for the UniFi CNAME.

**Verify:** `curl -sk --resolve "$TB:443:$LAB_IP" "https://$TB/" | grep -c 'href="https://ai-newsdesk.toolbox.home.arpa/"'` → `1` (variables from step 9).

**Steps:**

- [ ] **Step 1: Ask the owner for the DNS record now** (it can propagate while the rest runs). One message, nothing else in it to answer:

  > Please add one DNS record in UniFi: **CNAME `ai-newsdesk.toolbox.home.arpa` → `toolbox.home.arpa`**. I'll carry on meanwhile and tell you when the site is ready to open.

- [ ] **Step 2: Start clean in research-engine.**

```bash
cd /opt/research-engine
git status --short
git branch --show-current
```

Expected: no output from `status`, branch `main`. If the tree isn't clean or the branch isn't `main`, stop: it is another session's work. Tell the owner and don't touch it.

```bash
git pull --ff-only
git checkout -b feat/index-ai-newsdesk
```

- [ ] **Step 3: Write the failing test change** in `/opt/research-engine/tests/test_caddy_policy.py` (Edit tool, two edits):
  1. In `test_no_hardcoded_ips_or_hosts`, the line
     `    allowed = {"toolbox.home.arpa", "research.toolbox.home.arpa"}` becomes
     `    allowed = {"toolbox.home.arpa", "research.toolbox.home.arpa", "ai-newsdesk.toolbox.home.arpa"}`
  2. In `test_env_example_and_index`, directly after
     `    assert "Tools on this host" in html and "<!--" in html` add
     `    assert '<li><a href="https://ai-newsdesk.toolbox.home.arpa/">AI Newsdesk</a></li>' in html`

  Do not touch `deploy/caddy/.env.example` (its keys are pinned to `SITE_HOST`, `LAB_SUBNET`, `TOOLBOX_HOST`).

- [ ] **Step 4: Run it and watch it fail**

Run: `cd /opt/research-engine && uv run pytest tests/test_caddy_policy.py -q`
Expected: `1 failed` (`test_env_example_and_index`: the link is missing), the rest pass.

- [ ] **Step 5: Add the link** in `/opt/research-engine/deploy/caddy/index/index.html`: directly after the line
`  <li><a href="https://research.toolbox.home.arpa/">Research Engine</a></li>` insert
`  <li><a href="https://ai-newsdesk.toolbox.home.arpa/">AI Newsdesk</a></li>`.
The `<h1>Tools on this host</h1>` heading and the `<!-- … -->` comment stay. The list then reads:

```html
<ul>
  <li><a href="https://research.toolbox.home.arpa/">Research Engine</a></li>
  <li><a href="https://ai-newsdesk.toolbox.home.arpa/">AI Newsdesk</a></li>
</ul>
```

- [ ] **Step 6: Run the tests and watch them pass**

```bash
cd /opt/research-engine
uv run pytest tests/test_caddy_policy.py -q
make test
```

Expected: all of `test_caddy_policy.py` passes; `make test` exits 0 (research-engine's full check, as its `CLAUDE.md` requires before a PR).

- [ ] **Step 7: Commit, push, PR, merge** (research-engine's convention: Conventional Commit plus its feature/decision IDs; Caddy work there uses `D15, V1-20`):

```bash
cd /opt/research-engine
git add deploy/caddy/index/index.html tests/test_caddy_policy.py
git commit -m "feat(deploy): link AI Newsdesk on the toolbox index page (D15, V1-20)

The index page lists a second tool on the shared Caddy. The policy test allows its
documented example host and pins the link.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git push -u origin feat/index-ai-newsdesk
GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr create --base main \
  --title "feat(deploy): link AI Newsdesk on the toolbox index page" \
  --body "Adds an AI Newsdesk link (https://ai-newsdesk.toolbox.home.arpa/) to deploy/caddy/index/index.html, after Research Engine. test_caddy_policy.py allows that documented example host and asserts the link. No change to deploy/caddy/.env.example, the Caddyfile or the research-engine site.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr checks --watch
GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr merge --merge --delete-branch
git checkout main && git pull --ff-only
```

Expected: checks pass; merged; `git status --short` empty on `main`.

- [ ] **Step 8: Dry-run the bootstrap sync from research-engine's `main`.** research-engine's ops resolve `DEPLOY_DIR` to `.deploy/current` (its live commit), which doesn't have the link; `DEPLOY_DIR=/opt/research-engine` makes it sync from the merged `main` instead. The dry run changes nothing and runs no sudo.

```bash
cd /opt/research-engine
DEPLOY_DIR=/opt/research-engine ops/bootstrap.sh --dry-run | tail -n 1 | python3 -c '
import json, sys
d = json.load(sys.stdin)
a = {x["step"]: x for x in d["actions"]}
print(d["ok"], a["caddy_files"]["changed"], a["caddy_env"]["action"], a["caddy"]["action"], a["systemd"]["action"])'
```

Expected: `True ['index/index.html'] keep up keep`. If `changed` lists anything else, `caddy_env` isn't `keep`, or `caddy` is `recreate`, stop and show the owner the line (other research-engine changes are waiting on `main`; don't sync them as a side effect). If `systemd` isn't `keep`, the real run needs sudo: ask the owner to run step 9's command themselves.

- [ ] **Step 9: Sync, then check the index page.**

```bash
cd /opt/research-engine
DEPLOY_DIR=/opt/research-engine make bootstrap | tail -n 1 | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["ok"])'
LAB_IP="$(hostname -I | awk '{print $1}')"
TB="$(grep -m1 '^TOOLBOX_HOST=' /opt/caddy/.env | cut -d= -f2-)"; TB="${TB:-toolbox.home.arpa}"
grep -c 'href="https://ai-newsdesk.toolbox.home.arpa/"' /opt/caddy/index/index.html
curl -sk --resolve "$TB:443:$LAB_IP" "https://$TB/" | grep -c 'href="https://ai-newsdesk.toolbox.home.arpa/"'
```

Expected: `True`, `1`, `1`.

- [ ] **Step 10: Note the bootstrap gotcha for the handoff (Task 42).** Until research-engine is redeployed at a commit that contains this link, a plain `make bootstrap` in `/opt/research-engine` (no `DEPLOY_DIR`) would copy its live commit's older `index.html` back and drop the link. The fix is the same command as step 9, or a research-engine deploy. This goes into our `HANDOFF.local.md` and the report to the owner; research-engine's own files are not edited.

---

### Task 41: First deploy, Caddy install and end-to-end verification

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** ai-newsdesk runs healthy as Compose project `ai-newsdesk`, Caddy serves it lab-only at `https://ai-newsdesk.toolbox.home.arpa`, every other site still works, and a real "Refresh now" run collected items and spent a little real money.

**Files:** none in the repo. Host: `/opt/ai-newsdesk/.env` (our own), `/opt/caddy/sites/ai-newsdesk.caddy` (new), `/opt/caddy/.env` (one appended line).

**Acceptance Criteria:**
- [ ] AC1 — `.env` is mode 600; `SECRETS_KEY` and `SESSION_SECRET` are ≥ 32 characters; `ANTHROPIC_API_KEY` is non-empty (or a key is saved on the Settings page); `SITE_HOST=ai-newsdesk.toolbox.home.arpa`; lengths only were printed.
- [ ] AC2 — `make set-password` (run by the owner) wrote one single-quoted `OWNER_PASSWORD_HASH`, and `docker compose config` resolves it to a value starting `$argon2id$`.
- [ ] AC3 — `make deploy` → `"ok": true`; `docker compose -p ai-newsdesk ps` shows `app running healthy`; `make smoke` → `"ok": true`.
- [ ] AC4 — `caddy validate` printed `Valid configuration` before Caddy was recreated; `docker compose -p caddy ps` shows `caddy running healthy` after.
- [ ] AC5 — Through Caddy via the lab IP: `/login` → 200, `/` → 302/303/307, `/health` → 200. Via `127.0.0.1` → 403.
- [ ] AC6 — research-engine still answers through Caddy (`/health` → 200 via its host) and `make health` in `/opt/research-engine` → `"ok": true`; the toolbox index → 200 with the AI Newsdesk link.
- [ ] AC7 — Our `make health` → `"ok": true` with `site_host_match: true` and `password_set: true`.
- [ ] AC8 — From inside the app container, `http://research-engine-app:8000/health` → 200 (and `/v1/engines` with the env key → 200 when the key is set in `.env`).
- [ ] AC9 — After "Refresh now": the newest `runs` row is finished, has no error, at least one count > 0, and `0 < cost_usd < 0.10`.
- [ ] AC10 — The owner confirms they can log in at `https://ai-newsdesk.toolbox.home.arpa` from their own browser and see stories.

**Verify:** each step below shows its exact command and expected output; record every output (redacted of nothing secret, because nothing secret is printed) in the report.

**Steps:**

- [ ] **Step A: Pre-flight (read-only).**

```bash
cd /opt/ai-newsdesk
git checkout main && git pull --ff-only && git status --short
git check-ignore .env HANDOFF.local.md
stat -c '%a' .env
docker network inspect proxy --format '{{.Name}}'
free -m | awk '/^Mem:/ {print $7 " MB available"}'
make test
```

Expected: `status` prints nothing; `.env` and `HANDOFF.local.md` both printed by `check-ignore`; `600`; `proxy`; at least `1000 MB available`; `make test` exits 0.

- [ ] **Step B: Set `SITE_HOST` and check secrets by length only (AC1).**

```bash
cd /opt/ai-newsdesk
[ -z "$(tail -c1 .env)" ] || echo >> .env
if grep -q '^SITE_HOST=' .env; then sed -i 's/^SITE_HOST=.*/SITE_HOST=ai-newsdesk.toolbox.home.arpa/' .env
else echo 'SITE_HOST=ai-newsdesk.toolbox.home.arpa' >> .env; fi
grep '^SITE_HOST=' .env
for k in SECRETS_KEY SESSION_SECRET ANTHROPIC_API_KEY RESEARCH_ENGINE_API_KEY TYPESAFE_API_KEY OWNER_PASSWORD_HASH; do
  v="$(grep -m1 "^$k=" .env | cut -d= -f2-)"; printf '%s %s\n' "$k" "${#v}"
done; v=""
stat -c '%a' .env
```

Expected: `SITE_HOST=ai-newsdesk.toolbox.home.arpa`; `SECRETS_KEY 64`, `SESSION_SECRET 64` (any value ≥ 32 passes); `ANTHROPIC_API_KEY` > 0; `RESEARCH_ENGINE_API_KEY` > 0; `TYPESAFE_API_KEY` any; `OWNER_PASSWORD_HASH 0` (not set yet); `600`. If `SECRETS_KEY` or `SESSION_SECRET` is under 32, replace it without printing: `sed -i "s/^SECRETS_KEY=.*/SECRETS_KEY=$(openssl rand -hex 32)/" .env`. If `ANTHROPIC_API_KEY` or `RESEARCH_ENGINE_API_KEY` is 0, ask the owner (they can paste it into `.env` themselves, or save it on the Settings page after step I).

- [ ] **Step C: Owner sets the password (AC2).** Ask the owner to run, in this Claude Code prompt (the `!` prefix runs it in their own terminal session, so the password never passes through Claude):

  > Please type: `! cd /opt/ai-newsdesk && make set-password` and choose your login password (at least 12 characters).

Then check, printing no value:

```bash
cd /opt/ai-newsdesk
grep -c "^OWNER_PASSWORD_HASH='\$argon2id\$" .env
stat -c '%a' .env
GIT_SHA=check docker compose -p ai-newsdesk -f compose.yaml --env-file .env config --format json | python3 -c '
import json, sys
v = json.load(sys.stdin)["services"]["app"]["environment"]["OWNER_PASSWORD_HASH"].replace("$$", "$")
print(v.startswith("$argon2id$"), len(v) > 60)'
```

Expected: `1`, `600`, `True True`.

- [ ] **Step D: First deploy (AC3).** Approved by the owner as part of this plan.

```bash
cd /opt/ai-newsdesk
make deploy | tail -n 1
docker compose -p ai-newsdesk ps --format '{{.Service}} {{.State}} {{.Health}}'
make smoke | tail -n 1
make version | tail -n 1
```

Expected: `{"command": "deploy", "from": "none", "ok": true, "smoke": "pass", "to": "<12-char sha of main>"}`; `app running healthy`; smoke `"ok": true` with `health` 200, `login` 200, `auth_gate` 303 (or 302/307/401); version `git_sha` equal to the deploy's `to`. If the deploy fails, it leaves the stack up: `make logs SINCE=10m`, fix forward with `superpowers-extended-cc:systematic-debugging`, commit, PR, `make deploy` again.

- [ ] **Step E: Our health before Caddy knows us (expected partial failure).**

```bash
cd /opt/ai-newsdesk && make health | tail -n 1
```

Expected: `"app_status": "up"`, `"password_set": true`, `"weak_secrets": []`, and `"site_host_match": false` with `"site_host_reason": "missing AI_NEWSDESK_HOST in caddy env"` (so `ok` is false). Any other problem: stop and fix it before touching Caddy.

- [ ] **Step F: Baseline the existing sites, then install the site file and host variable (no Caddy restart yet).**

```bash
LAB_IP="$(hostname -I | awk '{print $1}')"
RE_HOST="$(grep -m1 '^SITE_HOST=' /opt/caddy/.env | cut -d= -f2-)"
TB="$(grep -m1 '^TOOLBOX_HOST=' /opt/caddy/.env | cut -d= -f2-)"; TB="${TB:-toolbox.home.arpa}"
curl -sk -o /dev/null -w 'research %{http_code}\n' --resolve "$RE_HOST:443:$LAB_IP" "https://$RE_HOST/health"
curl -sk -o /dev/null -w 'index %{http_code}\n' --resolve "$TB:443:$LAB_IP" "https://$TB/"
docker compose -p caddy ps --format '{{.Service}} {{.State}} {{.Health}}'

cd /opt/ai-newsdesk
install -m 644 deploy/caddy/ai-newsdesk.caddy /opt/caddy/sites/ai-newsdesk.caddy
grep -q '^AI_NEWSDESK_HOST=' /opt/caddy/.env || {
  [ -z "$(tail -c1 /opt/caddy/.env)" ] || echo >> /opt/caddy/.env
  echo 'AI_NEWSDESK_HOST=ai-newsdesk.toolbox.home.arpa' >> /opt/caddy/.env
}
grep -c '^AI_NEWSDESK_HOST=' /opt/caddy/.env
grep -o '^[A-Z_]*=' /opt/caddy/.env
stat -c '%a' /opt/caddy/.env
cmp deploy/caddy/ai-newsdesk.caddy /opt/caddy/sites/ai-newsdesk.caddy && echo site-copied
```

Expected: `research 200`, `index 200`, `caddy running healthy` (the baseline); then `1`; the key names `SITE_HOST=`, `LAB_SUBNET=`, `TOOLBOX_HOST=`, `AI_NEWSDESK_HOST=` (names only, every original key still there); `600`; `site-copied`. Re-running this step never adds a second line.

- [ ] **Step G: Validate, and only then recreate Caddy (AC4).**

```bash
docker run --rm --network none --env-file /opt/caddy/.env -v /opt/caddy:/etc/caddy:ro caddy:2.11.6 \
  caddy validate --config /etc/caddy/Caddyfile 2>&1 | grep -c 'Valid configuration'
```

Expected: `1`. **If it prints `0`:** do not recreate. Undo our two changes and stop to investigate:

```bash
rm -f /opt/caddy/sites/ai-newsdesk.caddy
sed -i '/^AI_NEWSDESK_HOST=/d' /opt/caddy/.env
```

On `1`, recreate (the `.env` change needs it; a reload would not re-read the container env):

```bash
cd /opt/caddy && docker compose up -d --wait --force-recreate
docker compose -p caddy ps --format '{{.Service}} {{.State}} {{.Health}}'
```

Expected: the `up` exits 0; `caddy running healthy`.

- [ ] **Step H: Through Caddy (AC5, AC6).**

```bash
LAB_IP="$(hostname -I | awk '{print $1}')"
H=ai-newsdesk.toolbox.home.arpa
RE_HOST="$(grep -m1 '^SITE_HOST=' /opt/caddy/.env | cut -d= -f2-)"
TB="$(grep -m1 '^TOOLBOX_HOST=' /opt/caddy/.env | cut -d= -f2-)"; TB="${TB:-toolbox.home.arpa}"
for p in /login / /health; do
  curl -sk -o /dev/null -w "newsdesk $p %{http_code}\n" --resolve "$H:443:$LAB_IP" "https://$H$p"
done
curl -sk -o /dev/null -w 'newsdesk via loopback %{http_code}\n' --resolve "$H:443:127.0.0.1" "https://$H/login"
curl -sk -o /dev/null -w 'research %{http_code}\n' --resolve "$RE_HOST:443:$LAB_IP" "https://$RE_HOST/health"
curl -sk --resolve "$TB:443:$LAB_IP" "https://$TB/" | grep -c 'href="https://ai-newsdesk.toolbox.home.arpa/"'
cd /opt/research-engine && make health | tail -n 1
```

Expected: `newsdesk /login 200`, `newsdesk / 303` (302 or 307 also pass), `newsdesk /health 200`, `newsdesk via loopback 403`, `research 200`, `1`, and research-engine's health line with `"ok": true`. If research-engine fails here but passed in the step-F baseline, roll our Caddy changes back at once (`docs/OPERATIONS.md` → "Caddy site" → roll back), confirm research-engine recovers, then investigate.

- [ ] **Step I: Our full health and research-engine reachability (AC7, AC8).**

```bash
cd /opt/ai-newsdesk
make health | tail -n 1
docker compose -p ai-newsdesk exec -T app python -c "
import os, urllib.request, urllib.error
def code(url, headers=None):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=15).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__
base = os.environ['RESEARCH_ENGINE_URL']
print('research-engine /health', code(base + '/health'))
key = os.environ.get('RESEARCH_ENGINE_API_KEY', '')
print('research-engine /v1/engines', code(base + '/v1/engines', {'X-API-Key': key}) if key else 'no env key (Settings page key in use)')
"
```

Expected: `"ok": true`, `"site_host_match": true`, `"password_set": true`; `research-engine /health 200`; `research-engine /v1/engines 200` (a `401` means the key is wrong: ask the owner to set research-engine's API key on our Settings page).

- [ ] **Step J: DNS and the owner's browser (AC10).**

```bash
getent hosts ai-newsdesk.toolbox.home.arpa >/dev/null && echo resolves || echo "no DNS yet"
```

Expected: `resolves`. If `no DNS yet`, remind the owner of the CNAME from Task 40 step 1 and wait for it; the gate stays open. Then ask the owner:

  > ai-newsdesk is live at **https://ai-newsdesk.toolbox.home.arpa** (also linked from the toolbox index page). Please log in with the password you just set, open **Settings** and press **Refresh now**, then tell me when the Today page shows stories. If the page won't load in Chrome but I report it working, turn off Chrome's "Use secure DNS" (Settings → Privacy and security → Security) — it bypasses the lab DNS.

If the owner can't do it now, the next scheduled run (00:00, 06:00, 12:00 or 18:00 Europe/London) produces the run for step K; the owner's login confirmation (AC10) is still required before closing.

- [ ] **Step K: Check the run and the spend (AC9).** Read-only queries inside the container:

```bash
cd /opt/ai-newsdesk
docker compose -p ai-newsdesk exec -T app python -c "
import json, sqlite3
c = sqlite3.connect('/data/ai-newsdesk.sqlite')
r = c.execute('select id, trigger, started_at, finished_at, counts_json, cost_usd, error from runs order by id desc limit 1').fetchone()
counts = json.loads(r[4] or '{}')
print(json.dumps({'id': r[0], 'trigger': r[1], 'started_at': r[2], 'finished_at': r[3], 'counts': counts, 'cost_usd': r[5], 'error': r[6]}, sort_keys=True))
print('finished', r[3] is not None, '| no error', r[6] is None, '| some count > 0', any(isinstance(v, (int, float)) and v > 0 for v in counts.values()), '| 0 < cost < 0.10', r[5] is not None and 0 < r[5] < 0.10)
s = c.execute('select count(*), coalesce(sum(cost_usd), 0) from spend').fetchone()
print('spend rows', s[0], 'total usd', round(s[1], 4))
"
```

Expected: the run's JSON line, then `finished True | no error True | some count > 0 True | 0 < cost < 0.10 True`, and `spend rows` > 0 with a total under `0.10`. If the cost is 0, the Anthropic key is missing or wrong (Settings page shows it as not set, or the run's counts show review failures): fix the key and press Refresh now again.

- [ ] **Step L: Close the gate.** Re-run, in one go, the checks for AC3–AC8 (`make health`, `make smoke`, `docker compose -p ai-newsdesk ps`, `docker compose -p caddy ps`, the step-H curls, `cd /opt/research-engine && make health`) and capture the output; with AC9's output from step K and the owner's AC10 confirmation, the gate closes. Paste all of it into the Task 42 report.

---

### Task 42: Handoff and report

**Goal:** The next session can pick up from `HANDOFF.local.md`, and the owner gets a plain-English report with evidence.

**Files:**
- Create: `/opt/ai-newsdesk/HANDOFF.local.md` (git-ignored; via the `/handoff` skill; never committed)

**Acceptance Criteria:**
- [ ] `git check-ignore HANDOFF.local.md` prints the name and `git status --short` stays empty.
- [ ] The handoff records: live URL and commit, Compose project, Caddy site file and `AI_NEWSDESK_HOST`, volume `ai-newsdesk_app-data`, `backups/`, secret names (no values), approvals, gotchas and open items listed below.
- [ ] The owner has a short plain-English report with the gate evidence.

**Verify:** `git -C /opt/ai-newsdesk status --short` → empty; `grep -c "AI_NEWSDESK_HOST" /opt/ai-newsdesk/HANDOFF.local.md` → ≥ 1.

**Steps:**

- [ ] **Step 1: Run the `/handoff` skill** (Skill tool, skill `handoff`) from `/opt/ai-newsdesk`. It gathers `git log`, `make version`, `make health` and open PRs, and writes the file from its template with the Edit/Write tools. Make sure it includes:
  - **Current state:** V1 live at `https://ai-newsdesk.toolbox.home.arpa`, commit from `make version`, today's date; steps 0–8 merged; research-engine index PR merged.
  - **The host:** `/opt/ai-newsdesk`, Compose project `ai-newsdesk` (dev: `ai-newsdesk-dev`), alias `ai-newsdesk-app` on `proxy`, volume `ai-newsdesk_app-data`, `backups/`, `/opt/caddy/sites/ai-newsdesk.caddy`, `AI_NEWSDESK_HOST` in `/opt/caddy/.env`, secrets by name only (`SECRETS_KEY`, `SESSION_SECRET`, `OWNER_PASSWORD_HASH`, `ANTHROPIC_API_KEY`, `RESEARCH_ENGINE_API_KEY`, `TYPESAFE_API_KEY`).
  - **How to work here:** Superpowers; `make deploy`/`make rollback` need approval unless the owner grants a standing one (record it here if they do); the owner runs `make set-password`.
  - **Gotchas:** `OWNER_PASSWORD_HASH` must stay single-quoted (Compose eats `$`); `make health` compares `SITE_HOST` with Caddy's `AI_NEWSDESK_HOST`, not Caddy's `SITE_HOST`; `127.0.0.1` gets 403 by design, use `curl --resolve … $(hostname -I | awk '{print $1}')`; Chrome secure DNS; a `.env` change in `/opt/caddy` needs `--force-recreate`, a site-file change only a reload; rotating `SECRETS_KEY` makes saved API keys unreadable; research-engine's plain `make bootstrap` syncs from its live commit and would drop the index link until research-engine is redeployed — use `DEPLOY_DIR=/opt/research-engine make bootstrap` there.
  - **Open items:** no nightly backup timer yet (decide: a user crontab line for `make backup`, or a systemd timer with the owner's sudo); research-engine redeploy so its live commit carries the index link (its owner's call); DNS CNAME if still pending; first week: watch spend on the Settings page against the $5 cap.

- [ ] **Step 2: Check it stays local.**

```bash
cd /opt/ai-newsdesk
git check-ignore HANDOFF.local.md
git status --short
grep -c "AI_NEWSDESK_HOST" HANDOFF.local.md
```

Expected: `HANDOFF.local.md`, nothing, a number ≥ 1.

- [ ] **Step 3: Report to the owner** in plain English, short, with the evidence from Task 41 step L: the URL and that it's on the index page; deploy result and commit; health, smoke and Caddy status; that research-engine is still healthy; the "Refresh now" run's counts and its cost; what the handoff lists as open items (nightly backups, the research-engine bootstrap note). Ask nothing unless something is genuinely blocked; if the DNS record is still missing, repeat the one record.

---

## Step done when

- [ ] `tests/test_caddy_site.py` passes; the step-8 PR is merged; `/opt/caddy/sites/ai-newsdesk.caddy` equals `deploy/caddy/ai-newsdesk.caddy`.
- [ ] `/opt/caddy/.env` has exactly one `AI_NEWSDESK_HOST` line, mode 600, every original key intact; `caddy validate` passed before the recreate; `docker compose -p caddy ps` shows it healthy.
- [ ] The research-engine index-link PR is merged and the index page serves the AI Newsdesk link.
- [ ] The Task 41 gate is closed with captured output for AC1–AC9 and the owner's AC10 confirmation.
- [ ] research-engine's `make health` is `"ok": true` after every Caddy change.
- [ ] `HANDOFF.local.md` exists, is git-ignored, and the owner has the report.
