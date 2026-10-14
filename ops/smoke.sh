#!/usr/bin/env bash
# Smoke test of the live app from INSIDE its container (no host port, no TLS trust needed), in a
# few seconds: /health is 200, /login is 200 with a form, / without a session is redirected or
# refused. The probe is ops/smoke_probe.py, sent as `python -c`. Read-only apart from those GETs.
# Prints the probe's JSON line (string values redacted); exit 0 only if it ran and reported ok.
CMD=smoke
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

probe="$(cat "$(dirname "${BASH_SOURCE[0]}")/smoke_probe.py")" || fail 2 smoke "ops/smoke_probe.py is missing"
log "smoke: probing the app inside its container"
code=0
out="$(dc exec -T app python -c "$probe" 2>/dev/null)" || code=$?

# The probe's result is its last stdout line; anything else is reported only as a generic
# failure, never echoed.
line="$(printf '%s\n' "$out" | tail -n 1 | redact_json)"
result="$(LINE="$line" CODE="$code" python3 - <<'PY'
import json, os
try:
    d = json.loads(os.environ["LINE"])
    if not isinstance(d, dict) or not isinstance(d.get("ok"), bool):
        raise ValueError
except ValueError:
    d = {"ok": False, "error": f"no smoke result from the app container (exit {os.environ['CODE']})"}
d["command"] = "smoke"
if os.environ["CODE"] != "0":
    d["ok"] = False
print(json.dumps(d, sort_keys=True))
PY
)"
emit_json_line "$result"
[ "$code" -eq 0 ] && [ "$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])')" = "True" ] || exit 1
