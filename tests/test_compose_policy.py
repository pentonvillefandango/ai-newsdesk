"""Compose policy (V1-S6): co-tenancy on the shared toolbox VM, hardening, networks, volumes and
environment drift. Parses YAML only, so it needs no Docker daemon (CI runs it as-is)."""

import re
from pathlib import Path
from typing import Any

import yaml

from ai_newsdesk.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / name).read_text())


def _raw(name: str) -> str:
    """The file with comments stripped."""
    return "\n".join(line.split("#", 1)[0] for line in (ROOT / name).read_text().splitlines())


COMPOSE = _load("compose.yaml")
RAW = _raw("compose.yaml")
SERVICES: dict[str, Any] = COMPOSE["services"]
APP: dict[str, Any] = SERVICES["app"]
DEV = _load("compose.dev.yaml")
DEBUG = _load("compose.debug.yaml")

PROJECT = "ai-newsdesk"
ALIAS = "ai-newsdesk-app"
# Settings fields whose value compose fixes rather than taking from .env
COMPOSE_FIXED = {"DB_PATH": "/data/ai-newsdesk.sqlite"}
# Required secrets: compose refuses to start without them (${VAR:?message})
SECRETS = {"SECRETS_KEY", "SESSION_SECRET"}
# Compose-only variables: documented in .env.example, never passed to the app
COMPOSE_ONLY = {"APP_MEM_LIMIT", "APP_CPUS", "APP_PORT"}


def _settings_env_names() -> set[str]:
    return {f.upper() for f in Settings.model_fields}


def _env_example() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def _mounts(svc: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise short- and long-syntax volume entries to {type, source, target}."""
    out: list[dict[str, Any]] = []
    for v in svc.get("volumes", []):
        if isinstance(v, dict):
            out.append(
                {
                    "type": v.get("type"),
                    "source": str(v.get("source", "")),
                    "target": v.get("target"),
                }
            )
            continue
        parts = str(v).split(":")
        src, tgt = (parts[0], parts[1]) if len(parts) > 1 else ("", parts[0])
        bind = src.startswith((".", "/", "~", "$"))
        out.append(
            {
                "type": "bind" if bind else ("volume" if src else "anonymous"),
                "source": src,
                "target": tgt,
            }
        )
    return out


def test_project_name() -> None:
    assert COMPOSE["name"] == PROJECT


def test_single_app_service() -> None:
    assert set(SERVICES) == {"app"}
    assert not APP.get("profiles")


def test_forbidden_settings() -> None:
    forbidden = (
        "container_name",
        "ports",
        "expose",
        "privileged",
        "pid",
        "devices",
        "network_mode",
        "volumes_from",
        "env_file",
        "cap_add",
        "extra_hosts",
    )
    for name, svc in SERVICES.items():
        for key in forbidden:
            assert key not in svc, f"{name} sets {key}"
        assert str(svc.get("ipc", "")) not in ("host",), f"{name} shares ipc"
        assert not str(svc.get("ipc", "")).startswith(("container:", "service:")), name
        assert svc.get("userns_mode") != "host", name
        assert svc.get("cgroup") != "host", name
    for word in ("SYS_ADMIN", "NET_ADMIN", "docker.sock", "privileged"):
        assert word not in RAW, word


def test_every_service_is_a_good_cotenant() -> None:
    for name, svc in SERVICES.items():
        assert svc["restart"] == "unless-stopped", name
        assert "healthcheck" in svc, name
        assert svc["logging"]["driver"] == "json-file", name
        opts = svc["logging"]["options"]
        assert "max-size" in opts and "max-file" in opts, name
        limits = svc["deploy"]["resources"]["limits"]
        assert "memory" in limits and "cpus" in limits, name


def test_limits_have_documented_defaults() -> None:
    limits = APP["deploy"]["resources"]["limits"]
    assert limits["memory"] == "${APP_MEM_LIMIT:-512m}"
    assert str(limits["cpus"]) == "${APP_CPUS:-0.5}"


def test_healthcheck_hits_health_endpoint() -> None:
    test = " ".join(APP["healthcheck"]["test"])
    assert "http://127.0.0.1:8000/health" in test


def test_image_pinned_and_built_locally() -> None:
    image = str(APP["image"])
    assert image == "ai-newsdesk-app:${GIT_SHA:-dev}"
    tag = image.partition(":")[2]
    assert "latest" not in tag.lower()
    for default in re.findall(r"\$\{\w+(?::?-([^}]*))?\}", tag):
        assert default.strip(), "an interpolated tag needs a non-empty default"
    assert APP["build"]["context"] == "."
    assert APP["build"]["args"]["GIT_SHA"] == "${GIT_SHA:-unknown}"


def test_hardening() -> None:
    assert APP["cap_drop"] == ["ALL"]
    assert APP["security_opt"] == ["no-new-privileges:true"]
    assert APP["read_only"] is True
    assert APP["user"] == "10001:10001"
    assert APP["stop_grace_period"] == "30s"
    assert not any("cap_add" in s for s in SERVICES.values())


def test_app_tmpfs() -> None:
    tmpfs = APP["tmpfs"]
    tmpfs = [tmpfs] if isinstance(tmpfs, str) else tmpfs
    assert len(tmpfs) == 1
    target, _, opts = str(tmpfs[0]).partition(":")
    assert target == "/tmp"  # noqa: S108 - a tmpfs mount target
    assert {"noexec", "nosuid", "nodev"} <= set(opts.split(","))


def test_networks() -> None:
    assert COMPOSE["networks"] == {"internal": {}, "proxy": {"external": True, "name": "proxy"}}
    on_proxy = [n for n, s in SERVICES.items() if "proxy" in (s.get("networks") or {})]
    assert on_proxy == ["app"]
    assert APP["networks"]["proxy"]["aliases"] == [ALIAS]
    assert "internal" in APP["networks"]


def test_volumes_named_only() -> None:
    assert COMPOSE["volumes"] == {"app-data": {}}
    for name, svc in SERVICES.items():
        mounts = _mounts(svc)
        assert all(m["type"] == "volume" for m in mounts), f"{name}: {mounts}"
        assert all(m["source"] in COMPOSE["volumes"] for m in mounts), f"{name}: {mounts}"
    assert APP["volumes"] == ["app-data:/data"]


def test_app_env_covers_every_setting() -> None:
    """Every Settings field reaches the app through an explicit mapping, so they can't drift."""
    env: dict[str, str] = APP["environment"]
    assert set(env) == _settings_env_names()
    for key in _settings_env_names() - set(COMPOSE_FIXED):
        value = str(env[key])
        if key in SECRETS:
            assert re.fullmatch(rf"\$\{{{key}:\?[^}}]+\}}", value), f"{key}: {value}"
        else:
            assert re.fullmatch(rf"\$\{{{key}:-[^}}]*\}}", value), f"{key}: {value}"


def test_compose_fixed_values() -> None:
    env: dict[str, str] = APP["environment"]
    for key, value in COMPOSE_FIXED.items():
        assert env[key] == value, key


def test_app_env_defaults_match_settings() -> None:
    env: dict[str, str] = APP["environment"]
    for name, field in Settings.model_fields.items():
        key = name.upper()
        if key in COMPOSE_FIXED or key in SECRETS:
            continue
        m = re.fullmatch(rf"\$\{{{key}:-([^}}]*)\}}", str(env[key]))
        assert m is not None, key
        default = field.default
        want = str(default).lower() if isinstance(default, bool) else str(default)
        assert m.group(1) == want, f"{key}: compose {m.group(1)!r} != Settings {default!r}"


def test_research_engine_reached_over_proxy_alias() -> None:
    assert APP["environment"]["RESEARCH_ENGINE_URL"] == (
        "${RESEARCH_ENGINE_URL:-http://research-engine-app:8000}"
    )


def test_compose_only_vars_stay_out_of_app() -> None:
    assert not (set(APP["environment"]) & COMPOSE_ONLY)


def test_env_example_in_sync() -> None:
    keys = set(_env_example())
    assert _settings_env_names() <= keys
    compose_vars = set(re.findall(r"\$\{(\w+)[:}]", RAW))
    assert compose_vars <= keys, "every ${VAR} in compose.yaml is documented in .env.example"
    assert keys >= COMPOSE_ONLY
    assert keys <= _settings_env_names() | COMPOSE_ONLY, "no undocumented extras"


def test_env_example_holds_placeholders_only() -> None:
    env = _env_example()
    for key in SECRETS:
        assert env[key] == "change-me", key
    empty = ("OWNER_PASSWORD_HASH", "ANTHROPIC_API_KEY", "RESEARCH_ENGINE_API_KEY")
    for key in (*empty, "TYPESAFE_API_KEY", "APP_PORT"):
        assert env[key] == "", key
    assert env["SITE_HOST"].endswith(".localhost")


def test_dev_override_renames_image_and_isolates_network() -> None:
    """A dev `up --build` must never overwrite a live or rollback `ai-newsdesk-app:<sha>` tag,
    and a dev container must never join the VM-wide proxy network."""
    assert set(DEV) == {"services", "networks"}
    assert set(DEV["services"]) == {"app"}
    app = DEV["services"]["app"]
    assert set(app) == {"image"}
    image = app["image"]
    assert image == "ai-newsdesk-app-dev:local" and "$" not in image
    assert image.partition(":")[0] != APP["image"].partition(":")[0]
    assert DEV["networks"] == {"proxy": {"external": False, "name": "ai-newsdesk-dev-proxy"}}


def test_dev_override_publishes_nothing() -> None:
    assert "ports" not in DEV["services"]["app"]


def test_debug_override_app_loopback_only() -> None:
    assert set(DEBUG) == {"services"}
    assert set(DEBUG["services"]) == {"app"}
    app = DEBUG["services"]["app"]
    assert set(app) == {"ports"}
    assert len(app["ports"]) == 1
    assert re.fullmatch(r"127\.0\.0\.1:\$\{APP_PORT:\?[^}]+\}:8000", app["ports"][0])
