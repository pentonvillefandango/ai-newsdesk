"""smoke-probe: run inside the app container by ops/smoke.sh (V1-S7). Prints one JSON line.

Checks the live app over loopback: /health is 200, /login is 200 with a form, and / without a
session is redirected or refused. Standard library only: it is sent as `python -c`.
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import IO

BASE = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000")
HOST = os.environ.get("SITE_HOST", "")
GATE_CODES = (302, 303, 307, 401)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None  # report the redirect itself; urllib then raises HTTPError with its code


def get(path: str) -> tuple[int, str, int]:
    headers = {"X-Forwarded-Proto": "https"}
    if HOST:
        headers["Host"] = HOST
    req = urllib.request.Request(BASE + path, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    start = time.monotonic()
    try:
        with opener.open(req, timeout=10) as resp:
            status, body = resp.status, resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, ""
    except OSError:
        status, body = 0, ""
    return status, body, int((time.monotonic() - start) * 1000)


def main() -> int:
    checks: list[dict[str, object]] = []
    status, _, ms = get("/health")
    checks.append({"name": "health", "ok": status == 200, "status": status, "ms": ms})
    status, body, ms = get("/login")
    login_ok = status == 200 and "<form" in body.lower()
    checks.append({"name": "login", "ok": login_ok, "status": status, "ms": ms})
    status, _, ms = get("/")
    checks.append({"name": "auth_gate", "ok": status in GATE_CODES, "status": status, "ms": ms})
    ok = all(c["ok"] is True for c in checks)
    print(json.dumps({"checks": checks, "command": "smoke", "ok": ok}, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
