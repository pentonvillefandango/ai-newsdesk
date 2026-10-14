from collections.abc import Callable
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ai_newsdesk.config import Settings, get_settings
from tests.conftest import ROOT, TEST_SECRETS_KEY, TEST_SESSION_SECRET

SettingsFactory = Callable[..., Settings]


def test_defaults(settings_factory: SettingsFactory) -> None:
    s = settings_factory()
    assert s.site_host == "ai-newsdesk.localhost"
    assert s.research_engine_url == "http://research-engine-app:8000"
    assert s.db_path == "data/ai-newsdesk.sqlite"
    assert s.tz == "Europe/London"
    assert s.zone == ZoneInfo("Europe/London")
    assert s.log_level == "INFO"
    assert s.git_sha == "unknown"
    assert s.scheduler_enabled is True
    assert s.owner_password_hash == ""
    assert s.login_enabled is False
    assert (s.anthropic_api_key, s.research_engine_api_key, s.typesafe_api_key) == ("", "", "")


def test_reads_environment(settings_factory: SettingsFactory) -> None:
    s = settings_factory(tz="UTC", log_level="debug", scheduler_enabled="false", git_sha="abc1234")
    assert s.tz == "UTC"
    assert s.log_level == "DEBUG"
    assert s.scheduler_enabled is False
    assert s.git_sha == "abc1234"


@pytest.mark.parametrize("field", ["SECRETS_KEY", "SESSION_SECRET"])
def test_required_secret_missing(clean_env: pytest.MonkeyPatch, field: str) -> None:
    clean_env.setenv("SECRETS_KEY", TEST_SECRETS_KEY)
    clean_env.setenv("SESSION_SECRET", TEST_SESSION_SECRET)
    clean_env.delenv(field)
    with pytest.raises(ValidationError, match=field.lower()):
        Settings()  # type: ignore[call-arg]


@pytest.mark.parametrize("field", ["secrets_key", "session_secret"])
@pytest.mark.parametrize("bad", ["change-me", "x" * 31])
def test_weak_secret_refused_without_echo(
    settings_factory: SettingsFactory, field: str, bad: str
) -> None:
    with pytest.raises(ValidationError) as exc:
        settings_factory(**{field: bad})
    message = str(exc.value)
    assert field.upper() in message
    assert bad not in message


def test_password_hash_must_be_argon2(settings_factory: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="argon2"):
        settings_factory(owner_password_hash="plaintext-password")
    s = settings_factory(owner_password_hash="$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")
    assert s.login_enabled is True


def test_unknown_time_zone_refused(settings_factory: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="IANA"):
        settings_factory(tz="Mars/Olympus_Mons")


def test_unknown_log_level_refused(settings_factory: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        settings_factory(log_level="LOUD")


def test_secrets_not_in_repr(settings_factory: SettingsFactory) -> None:
    s = settings_factory(anthropic_api_key="sk-test-" + "a" * 20)
    for text in (repr(s), str(s)):
        assert TEST_SECRETS_KEY not in text
        assert TEST_SESSION_SECRET not in text
        assert "sk-test-" not in text
    assert "ai-newsdesk.localhost" in repr(s)


def test_get_settings_is_cached(settings_factory: SettingsFactory) -> None:
    settings_factory()  # sets the environment
    assert get_settings() is get_settings()


def test_env_example_names_every_setting() -> None:
    text = (ROOT / ".env.example").read_text()
    keys = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = {name.upper() for name in Settings.model_fields} - keys
    assert not missing, f".env.example lacks {sorted(missing)}"


def test_env_example_secrets_are_placeholders() -> None:
    """Any key that names a secret ships empty or as the refused placeholder."""
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if any(word in key for word in ("KEY", "SECRET", "HASH", "TOKEN", "PASSWORD")):
                assert value in {"", "change-me"}, f"{key} must be a placeholder"
