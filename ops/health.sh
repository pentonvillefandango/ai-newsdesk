#!/usr/bin/env bash
# /health answers 200, every compose service is running and healthy, SITE_HOST equals the shared
# Caddy's AI_NEWSDESK_HOST, no weak secrets, and an owner password is set. Names only, never
# values. Read-only.
CMD=health
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

expected="$(dc config --services)" || fail 1 health "cannot list compose services"
ids="$(dc ps -a -q)"
inspect=""
[ -n "$ids" ] && inspect="$(inspect_fields "$ids")"
code="$(app_http_code /health)"

# Our SITE_HOST must equal AI_NEWSDESK_HOST in the shared Caddy's env (Caddy's own SITE_HOST is
# research-engine's). Compared case-insensitively, never printed. No Caddy env file: null + reason.
site_match=null
site_reason=null
if [ ! -e "$CADDY_DIR/.env" ]; then
  site_reason='"no caddy env"'
else
  mine="$(env_get SITE_HOST || true)"
  if [ ! -r "$CADDY_DIR/.env" ]; then
    site_match=false; site_reason='"unreadable caddy env"'
  elif [ -z "$mine" ]; then
    site_match=false; site_reason='"missing SITE_HOST in env"'
  elif ! theirs="$(env_get AI_NEWSDESK_HOST "$CADDY_DIR/.env")" || [ -z "$theirs" ]; then
    site_match=false; site_reason='"missing AI_NEWSDESK_HOST in caddy env"'
  elif [ "${mine,,}" = "${theirs,,}" ]; then
    site_match=true
  else
    site_match=false; site_reason='"mismatch"'
  fi
fi
mine="" theirs=""

weak="$(weak_secrets)"
password_set=false
if [ -n "$(env_get OWNER_PASSWORD_HASH || true)" ]; then password_set=true; fi

result="$(WEAK="$weak" SECRET_HINT="$SECRET_HINT" INSPECT="$inspect" EXPECTED="$expected" \
  CODE="$code" SITE_MATCH="$site_match" SITE_REASON="$site_reason" PASSWORD_SET="$password_set" \
  python3 - <<'PY'
import json, os
containers, good = {}, set()
for line in os.environ["INSPECT"].splitlines():
    if not line.strip():
        continue
    name, state, health, _restarts, service = line.split("\t")
    containers[name.lstrip("/")] = health if state == "running" else state
    if state == "running" and health == "healthy":
        good.add(service)
problems = [f"{svc}: not running and healthy" for svc in os.environ["EXPECTED"].split() if svc not in good]
weak = os.environ["WEAK"].split()
if weak:
    problems.append(f"weak secrets in .env: {' '.join(weak)} ({os.environ['SECRET_HINT']})")
password_set = os.environ["PASSWORD_SET"] == "true"
if not password_set:
    problems.append("OWNER_PASSWORD_HASH is not set (login is disabled): run make set-password, then make deploy")
code = os.environ["CODE"].strip()
http_status = int(code) if code.isdigit() else 0
app_status = "up" if http_status == 200 else ("unreachable" if http_status == 0 else "down")
site = json.loads(os.environ["SITE_MATCH"])
ok = app_status == "up" and not problems and site is not False
print(json.dumps({"ok": ok, "command": "health", "app_status": app_status,
                  "http_status": http_status, "containers": containers, "problems": problems,
                  "site_host_match": site, "site_host_reason": json.loads(os.environ["SITE_REASON"]),
                  "weak_secrets": weak, "password_set": password_set}, sort_keys=True))
PY
)"
emit_json_line "$result"
[ "$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])')" = "True" ] || exit 1
