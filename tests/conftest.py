"""Shared pytest fixtures."""

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.config import Settings, get_settings
from ai_newsdesk.store.store import Store
from tests.fakes import utc

ROOT = Path(__file__).resolve().parent.parent

# Fake secrets, built at runtime (nothing for gitleaks to flag) and long enough for the
# 32-character minimum.
TEST_SECRETS_KEY = "test-secrets-key-" + "0" * 32
TEST_SESSION_SECRET = "test-session-secret-" + "1" * 32
TEST_NOW = utc(2026, 10, 14, 12, 0)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """No Settings variable leaks in from the developer's shell (TZ is often set)."""
    monkeypatch.chdir(ROOT)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def settings_factory(clean_env: pytest.MonkeyPatch) -> Callable[..., Settings]:
    """Build a Settings from a clean environment plus the two required test secrets.

    Keyword overrides use field names (``tz="UTC"``); they are set as environment variables
    so they go through the same parsing and validation as production.
    """

    def make(**overrides: str) -> Settings:
        env = {"SECRETS_KEY": TEST_SECRETS_KEY, "SESSION_SECRET": TEST_SESSION_SECRET}
        env.update({k.upper(): v for k, v in overrides.items()})
        for k, v in env.items():
            clean_env.setenv(k, v)
        return Settings()  # type: ignore[call-arg]

    return make


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """A fresh SQLite path in pytest's tmp dir (the file does not exist yet)."""
    return str(tmp_path / "data" / "test.sqlite")


@pytest.fixture
def now() -> datetime:
    return TEST_NOW


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(TEST_NOW)


@pytest.fixture
async def store(db_path: str, clock: FixedClock) -> AsyncIterator[Store]:
    """A migrated Store on a fresh file, using the shared FixedClock."""
    s = Store(db_path, clock=clock)
    await s.init()
    try:
        yield s
    finally:
        await s.close()
