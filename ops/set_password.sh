#!/usr/bin/env bash
# Set the owner login password (V1-S7). Prompts twice on a terminal (or reads two lines from stdin
# otherwise), hashes it with `ai-newsdesk hash-password` (argon2id; the password reaches it on
# stdin only, never on a command line) and writes OWNER_PASSWORD_HASH='<hash>' into .env.
# The single quotes matter: an argon2 hash contains `$`, which Docker Compose would otherwise
# interpolate. .env stays mode 600 and every other line is kept. Nothing secret is printed.
# The app reads .env only at start-up: run `make deploy` afterwards.
# HASH_VIA=auto (default: local `uv run` when uv is installed, else a one-off app container),
# uv or docker. HASH_CMD (tests only) replaces the hasher.
CMD=set-password
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MIN_LEN=12
[ -f "$ENV_FILE" ] || fail 2 set-password "no .env: copy .env.example to .env (mode 600) first"
via="${HASH_VIA:-auto}"
case "$via" in
  auto | uv | docker) ;;
  *) fail 2 set-password "HASH_VIA must be auto, uv or docker" ;;
esac
if [ "$via" = auto ]; then
  if command -v uv >/dev/null 2>&1 && [ -f "$REPO_DIR/pyproject.toml" ]; then via=uv; else via=docker; fi
fi
if [ -z "${HASH_CMD:-}" ] && [ "$via" = docker ]; then require_cmd docker; fi

pw1="" pw2=""
if [ -t 0 ]; then
  read -r -s -p "New owner password (at least $MIN_LEN characters): " pw1
  printf '\n' >&2
  read -r -s -p "Repeat it: " pw2
  printf '\n' >&2
else
  IFS= read -r pw1 || true
  IFS= read -r pw2 || true
fi
if [ "$pw1" != "$pw2" ]; then
  pw1="" pw2=""
  fail 2 set-password "the two passwords differ; nothing was changed"
fi
pw2=""
if [ "${#pw1}" -lt "$MIN_LEN" ]; then
  pw1=""
  fail 2 set-password "the password must be at least $MIN_LEN characters; nothing was changed"
fi

hash_password() {
  if [ -n "${HASH_CMD:-}" ]; then
    "$HASH_CMD"
  elif [ "$via" = uv ]; then
    uv run --project "$REPO_DIR" --quiet ai-newsdesk hash-password
  else
    dc run --rm --no-deps -T app ai-newsdesk hash-password
  fi
}

log "hashing the password (via $via)"
code=0
hash="$(printf '%s\n' "$pw1" | hash_password | tr -d '\r' | tail -n 1)" || code=$?
pw1=""
[ "$code" -eq 0 ] || fail 1 set-password "hash-password failed (exit $code); nothing was changed"
re='^\$argon2id\$v=[0-9]+\$m=[0-9]+,t=[0-9]+,p=[0-9]+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$'
if ! [[ "$hash" =~ $re ]]; then
  hash=""
  fail 1 set-password "hash-password did not return an argon2id hash; nothing was changed"
fi

# Values reach python only through its environment, never a command line or stdout.
if ! NEW_HASH="$hash" python3 - "$ENV_FILE" <<'PY'
import os, re, sys
path = sys.argv[1]
new = "OWNER_PASSWORD_HASH='" + os.environ["NEW_HASH"] + "'"
lines = open(path, encoding="utf-8").read().splitlines()
out, done = [], False
for line in lines:
    if re.match(r"\s*(?:export\s+)?OWNER_PASSWORD_HASH\s*=", line):
        if not done:  # first occurrence replaced in place, duplicates dropped
            out.append(new)
            done = True
        continue
    out.append(line)
if not done:
    out.append(new)
tmp = path + ".set-password-tmp"
if os.path.lexists(tmp):
    os.unlink(tmp)
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
os.replace(tmp, path)
PY
then
  hash=""
  fail 1 set-password "could not write .env; nothing was changed"
fi
hash=""
chmod 600 "$ENV_FILE"
log "OWNER_PASSWORD_HASH written to .env (mode 600); run make deploy to apply it"
json_out ok:=true command=set-password key=OWNER_PASSWORD_HASH env_mode=600 next="make deploy"
