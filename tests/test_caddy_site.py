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
