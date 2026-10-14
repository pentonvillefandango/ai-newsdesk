#!/usr/bin/env bash
# The running app image (its tag is the deployed short SHA), the commit .deploy/current points
# at, and the repo HEAD. Read-only.
CMD=version
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

images="$(dc images --format json)" || fail 1 version "docker compose images failed"
deployed="$(readlink "$REPO_DIR/.deploy/current" 2>/dev/null || true)"
head="$(git -C "$REPO_DIR" rev-parse --short="$SHORT_SHA_LEN" HEAD 2>/dev/null || true)"
result="$(IMAGES="$images" DEPLOYED="$deployed" HEAD_SHA="$head" PROJECT="$PROJECT" python3 - <<'PY'
import json, os
raw = os.environ["IMAGES"].strip()
try:
    rows = json.loads(raw) if raw.startswith("[") else [json.loads(x) for x in raw.splitlines() if x.strip()]
except ValueError:
    rows = []
images = [{"container": r.get("ContainerName"), "repository": r.get("Repository"),
           "tag": r.get("Tag"), "id": r.get("ID")} for r in rows]
app = next((i for i in images if i["repository"] == os.environ["PROJECT"] + "-app"), None)
print(json.dumps({"ok": app is not None, "command": "version",
                  "git_sha": app["tag"] if app else None,
                  "deployed": os.environ["DEPLOYED"] or None,
                  "repo_head": os.environ["HEAD_SHA"] or None, "images": images}, sort_keys=True))
PY
)"
emit_json_line "$result"
[ "$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])')" = "True" ] || exit 1
