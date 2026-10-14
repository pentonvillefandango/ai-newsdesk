from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk.app import create_app
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.config import Settings
from ai_newsdesk.runtime_settings import API_KEY_NAMES, RuntimeSettings
from ai_newsdesk.web.keys import KeyTester
from tests.web.fakes import fake_ctx_factory
from tests.web.support import BASE_URL, CONFIG_DIR, NOW, login, make_settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def key_results() -> dict[str, str | None]:
    """What each fake key tester answers: None = the key works, a string = the provider's error."""
    results: dict[str, str | None] = {name: None for name in API_KEY_NAMES}
    return results


def _tester(results: dict[str, str | None], name: str) -> KeyTester:
    async def check(key: str, runtime: RuntimeSettings) -> str | None:
        return results[name]

    return check


@pytest.fixture
def app(settings: Settings, key_results: dict[str, str | None]) -> FastAPI:
    return create_app(
        settings,
        ctx_factory=fake_ctx_factory,
        key_testers={name: _tester(key_results, name) for name in API_KEY_NAMES},
        config_dir=CONFIG_DIR,
        clock=FixedClock(NOW),
    )


@pytest.fixture
def anon(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def client(anon: TestClient) -> TestClient:
    login(anon)
    return anon
