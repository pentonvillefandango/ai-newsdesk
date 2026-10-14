#!/usr/bin/env bash
# Online SQLite backup of the live app to $BACKUP_DIR, then retention (V1-S7).
# The app keeps running: `ai-newsdesk db backup` uses SQLite's online backup API inside the
# container. Backups hold settings (API keys encrypted with SECRETS_KEY), stories and spend: the
# directory is 700 and every file 600. Takes the shared ops lock (.deploy/lock, as deploy and
# rollback do), waiting up to BACKUP_LOCK_WAIT seconds (default 600).
CMD=backup
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ "$#" -eq 0 ] || fail 2 backup "usage: ops/backup.sh (no arguments)"
days="${BACKUP_RETENTION_DAYS-14}"
[[ "$days" =~ ^[0-9]+$ ]] && [ "$days" -ge 1 ] ||
  fail 2 backup "BACKUP_RETENTION_DAYS must be a whole number of days >= 1"
lock_wait="${BACKUP_LOCK_WAIT-600}"
[[ "$lock_wait" =~ ^[0-9]+$ ]] || fail 2 backup "BACKUP_LOCK_WAIT must be a whole number of seconds"
require_cmd docker
require_cmd flock

mkdir -p "$REPO_DIR/.deploy"
exec 9>"$REPO_DIR/.deploy/lock"
flock -w "$lock_wait" 9 || fail 2 backup "a deploy, rollback or backup is running (.deploy/lock is held)"

umask 077
mkdir -p "$BACKUP_DIR" || fail 1 backup "cannot create $BACKUP_DIR"
chmod 700 "$BACKUP_DIR" || fail 1 backup "cannot set mode 700 on $BACKUP_DIR"

ts="${BACKUP_TS:-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$ts" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || fail 2 backup "BACKUP_TS must look like 20261014T033000Z"
inner="/data/.backup-$ts-$$.sqlite" # per run: a failed run can only ever remove its own file
dest="$BACKUP_DIR/ai-newsdesk-$ts.sqlite"
part="$dest.part" # never matches ai-newsdesk-*.sqlite: retention ignores it
[ ! -e "$dest" ] || fail 2 backup "a backup named $(basename "$dest") already exists"

INNER_LEFT=0
on_exit_hook() {
  if [ "$INNER_LEFT" = 1 ]; then dc exec -T app rm -f "$inner" >/dev/null 2>&1 || true; fi
  rm -f "$part"
}

log "backing up the app database"
INNER_LEFT=1
code=0
out="$(dc exec -T app ai-newsdesk db backup --out "$inner")" || code=$?
detail="$(LINE="$(printf '%s\n' "$out" | tail -n 1)" python3 -c '
import json, os
try:
    d = json.loads(os.environ["LINE"])
    if not isinstance(d, dict):
        raise ValueError
except ValueError:
    d = {"ok": False, "error": "no result line from ai-newsdesk db backup"}
print(json.dumps(d, sort_keys=True))')"
if [ "$code" -ne 0 ] || [ "$(printf '%s' "$detail" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ok") is True)')" != True ]; then
  fail 1 backup "in-container backup failed (exit $code)" detail:="$detail"
fi

log "copying it out to $dest"
dc cp "app:$inner" "$part" >&2 || fail 1 backup "docker compose cp failed"
dc exec -T app rm -f "$inner" >&2 || log "warning: could not remove $inner in the container"
INNER_LEFT=0
chmod 600 "$part" && mv -f "$part" "$dest" || fail 1 backup "cannot finalise $dest"
bytes="$(stat -c %s "$dest")"
[ "$bytes" -gt 0 ] || fail 1 backup "backup file is empty"

# Temps left by killed runs would stay in the volume forever: sweep any older than a day.
sweep='import os, sys, time; d = "/data"; cutoff = time.time() - 86400; '
sweep+='old = [n for n in os.listdir(d) if n.startswith(".backup-") and n.endswith(".sqlite") '
sweep+='and os.path.join(d, n) != sys.argv[2] and os.lstat(os.path.join(d, n)).st_mtime < cutoff]; '
sweep+='[os.unlink(os.path.join(d, n)) for n in old]; print(old)'
dc exec -T app python -c "$sweep" sweep-stale-backup-temps "$inner" >&2 || log "warning: could not sweep stale temp files in /data"

# Retention: only regular files named ai-newsdesk-<ts>.sqlite directly in $BACKUP_DIR.
pruned="$(python3 - "$BACKUP_DIR" "$days" "$dest" <<'PY'
import json, os, re, sys, time
bdir, days, newest = sys.argv[1], int(sys.argv[2]), os.path.basename(sys.argv[3])
cutoff = time.time() - days * 86400
pat = re.compile(r"ai-newsdesk-\d{8}T\d{6}Z\.sqlite")
pruned = []
for name in sorted(os.listdir(bdir)):
    path = os.path.join(bdir, name)
    if name == newest or not pat.fullmatch(name) or os.path.islink(path) or not os.path.isfile(path):
        continue
    if os.stat(path).st_mtime < cutoff:
        os.unlink(path)
        pruned.append(name)
print(json.dumps(pruned))
PY
)" || fail 1 backup "retention failed (the new backup was kept)" file="$dest"
[ "$pruned" = "[]" ] || log "pruned: $pruned"

json_out ok:=true command=backup file="$dest" bytes:="$bytes" pruned:="$pruned"
