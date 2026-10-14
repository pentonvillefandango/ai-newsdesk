import logging
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from ai_newsdesk.config import Settings
from ai_newsdesk.runtime_settings import (
    ApiKeys,
    RuntimeSettings,
    load_runtime_settings,
    save_runtime_settings,
    update_runtime_settings,
)
from ai_newsdesk.secrets_box import SecretsBox
from ai_newsdesk.store.store import Store
from tests.fakes import utc

SettingsFactory = Callable[..., Settings]
UI_KEY = "sk-ui-" + "u" * 40
ENV_KEY = "sk-env-" + "e" * 40
NOW = utc(2026, 10, 14, 12)


def test_defaults_match_contract() -> None:
    rs = RuntimeSettings()
    assert rs.model_dump() == {
        "runs_per_day": 4,
        "briefing_time": "07:00",
        "daily_review_cap": 500,
        "monthly_budget_usd": 5.0,
        "judge_provider": "anthropic",
        "judge_model": "claude-haiku-5-5",
        "writer_model": "claude-haiku-5-5",
        "briefing_model": "claude-haiku-5-5",
        "show_threshold": 4,
        "why_it_matters": True,
        "hn_min_points": 100,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"runs_per_day": 5},
        {"briefing_time": "7:00"},
        {"briefing_time": "24:00"},
        {"daily_review_cap": -1},
        {"monthly_budget_usd": -0.01},
        {"monthly_budget_usd": float("inf")},
        {"monthly_budget_usd": float("nan")},
        {"monthly_budget_usd": "inf"},
        {"monthly_budget_usd": 1000.01},
        {"judge_provider": "openai"},
        {"judge_model": ""},
        {"show_threshold": 11},
        {"hn_min_points": -5},
        {"unknown_key": 1},
    ],
)
def test_invalid_values_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(bad)


@pytest.mark.parametrize("runs", [1, 2, 4, 6, 12, 24])
def test_every_frequency_choice_allowed(runs: int) -> None:
    assert RuntimeSettings(runs_per_day=runs).runs_per_day == runs


async def test_load_defaults_save_roundtrip(store: Store) -> None:
    assert await load_runtime_settings(store) == RuntimeSettings()
    changed = RuntimeSettings(runs_per_day=6, why_it_matters=False, briefing_time="06:30")
    await save_runtime_settings(store, changed)
    assert await load_runtime_settings(store) == changed
    assert await store.get_setting("runs_per_day") == 6


async def test_invalid_stored_value_falls_back(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    await store.set_setting("show_threshold", 99)
    await store.set_setting("daily_review_cap", 50)
    with caplog.at_level(logging.WARNING):
        rs = await load_runtime_settings(store)
    assert (rs.show_threshold, rs.daily_review_cap) == (4, 50)
    assert "show_threshold" in caplog.text


async def test_update_validates_before_saving(store: Store) -> None:
    rs = await update_runtime_settings(store, monthly_budget_usd=3.5)
    assert rs.monthly_budget_usd == 3.5
    with pytest.raises(ValidationError):
        await update_runtime_settings(store, runs_per_day=7, monthly_budget_usd=1.0)
    assert (await load_runtime_settings(store)).monthly_budget_usd == 3.5


async def test_resolve_order_ui_then_env_then_empty(
    store: Store, settings_factory: SettingsFactory
) -> None:
    keys = ApiKeys(store, settings_factory(anthropic_api_key=ENV_KEY))
    assert await keys.resolve_api_key("anthropic") == ENV_KEY
    assert await keys.resolve_api_key("typesafe") == ""
    await keys.set_api_key("anthropic", f"  {UI_KEY}  ", NOW)
    assert await keys.resolve_api_key("anthropic") == UI_KEY
    status = await keys.api_key_status("anthropic")
    assert (status.set_in_ui, status.set_in_env, status.is_set) == (True, True, True)
    assert status.updated_at == NOW
    await keys.clear_api_key("anthropic")
    assert await keys.resolve_api_key("anthropic") == ENV_KEY
    empty = await keys.api_key_status("research_engine")
    assert (empty.is_set, empty.updated_at) == (False, None)


async def test_bad_input_refused(store: Store, settings_factory: SettingsFactory) -> None:
    keys = ApiKeys(store, settings_factory())
    with pytest.raises(ValueError, match="empty"):
        await keys.set_api_key("anthropic", "   ", NOW)
    with pytest.raises(ValueError, match="unknown API key"):
        await keys.resolve_api_key("openai")  # type: ignore[arg-type]


async def test_secret_never_leaks(
    store: Store, settings_factory: SettingsFactory, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_factory()
    keys = ApiKeys(store, settings)
    with caplog.at_level(logging.DEBUG):
        await keys.set_api_key("anthropic", UI_KEY, NOW)
        await keys.resolve_api_key("anthropic")
        status = await keys.api_key_status("anthropic")
        await keys.clear_api_key("anthropic")
    stored_texts = [repr(keys), str(keys), repr(status), status.model_dump_json(), caplog.text]
    for text in stored_texts:
        assert UI_KEY not in text
        assert settings.secrets_key not in text
    await keys.set_api_key("anthropic", UI_KEY, NOW)
    raw = await store.get_setting("secret.anthropic")
    assert UI_KEY not in str(raw)


async def test_rotated_secrets_key_falls_back_to_env(
    store: Store, settings_factory: SettingsFactory, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_factory(anthropic_api_key=ENV_KEY)
    await ApiKeys(store, settings).set_api_key("anthropic", UI_KEY, NOW)
    rotated = ApiKeys(store, settings, box=SecretsBox("r" * 64))
    with caplog.at_level(logging.WARNING):
        assert await rotated.resolve_api_key("anthropic") == ENV_KEY
    assert "could not be decrypted" in caplog.text
    assert UI_KEY not in caplog.text
