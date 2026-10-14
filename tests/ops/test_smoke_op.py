"""`make smoke`: the probe runs inside the app container; the probe itself vs a stub app (V1-S7)."""

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import ROOT, FakeEnv, json_lines

# ---- smoke.sh with fake docker -----------------------------------------------------------------


def test_smoke_passes_through_probe_line(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke")
    assert r.code == 0, r.stderr
    assert len(json_lines(r.stdout)) == 1
    assert r.last["ok"] is True and r.last["command"] == "smoke"
    assert [c["name"] for c in r.last["checks"]] == ["health", "login", "auth_gate"]
    calls = fake_env.calls.read_text()
    assert "compose -p ai-newsdesk " in calls and "exec -T app python -c" in calls
    for secret in fake_env.secrets:
        assert secret not in r.stdout + r.stderr + calls


def test_smoke_failure_exits_1(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke", FAKE_SMOKE_FILE="exec_smoke_fail.json", FAKE_SMOKE_EXIT="1")
    assert r.code == 1 and r.last["ok"] is False
    assert r.last["checks"][2] == {"ms": 2, "name": "auth_gate", "ok": False, "status": 200}


def test_smoke_ok_line_but_nonzero_exit_fails(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke", FAKE_SMOKE_EXIT="1")
    assert r.code == 1 and r.last["ok"] is False


def test_smoke_garbage_from_container_fails_without_echo(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke", FAKE_SMOKE_FILE="exec_smoke_garbage.txt", FAKE_SMOKE_EXIT="1")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1 and r.last["error"]
    assert "Traceback" not in r.stdout


def test_smoke_docker_failure_one_json_line(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke", FAKE_FAIL="125")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1 and r.last["ok"] is False


# ---- smoke_probe.py against a stub app ---------------------------------------------------------


class _Stub(BaseHTTPRequestHandler):
    open_root = False  # True: "/" answers 200 without a session (a broken auth gate)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, b'{"status": "ok"}')
        elif self.path == "/login":
            self._send(200, b'<html><form method="post" action="/login"></form></html>')
        elif self.path == "/" and not self.open_root:
            self.send_response(303)
            self.send_header("Location", "/login")
            self.end_headers()
        else:
            self._send(200, b"<html>today</html>")

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def stub() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    _Stub.open_root = False


def _probe(server: ThreadingHTTPServer) -> subprocess.CompletedProcess[str]:
    port = server.server_address[1]
    return subprocess.run(
        [sys.executable, str(ROOT / "ops" / "smoke_probe.py")],
        env={"SMOKE_BASE_URL": f"http://127.0.0.1:{port}", "SITE_HOST": "news.example.test"},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_probe_passes_against_a_healthy_app(stub: ThreadingHTTPServer) -> None:
    p = _probe(stub)
    line = json.loads(p.stdout.strip().splitlines()[-1])
    assert p.returncode == 0 and line["ok"] is True and line["command"] == "smoke"
    assert {c["name"]: c["status"] for c in line["checks"]} == {
        "health": 200,
        "login": 200,
        "auth_gate": 303,
    }


def test_probe_fails_when_the_auth_gate_is_open(stub: ThreadingHTTPServer) -> None:
    _Stub.open_root = True
    p = _probe(stub)
    line = json.loads(p.stdout.strip().splitlines()[-1])
    assert p.returncode == 1 and line["ok"] is False
    assert [c["name"] for c in line["checks"] if not c["ok"]] == ["auth_gate"]


def test_probe_is_marked_for_the_fake_docker() -> None:
    assert "smoke-probe" in (ROOT / "ops" / "smoke_probe.py").read_text().splitlines()[0]
