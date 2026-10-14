"""Process configuration from environment variables, read once at start-up (V1-S0).

Compose injects `.env`; nothing here reads a file. Run-time tunables live in the database
(`runtime_settings.py`), not here.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET = "change-me"  # noqa: S105  # the .env.example placeholder, refused below
MIN_SECRET_LENGTH = 32
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error must never echo a secret's value.
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    site_host: str = "ai-newsdesk.localhost"
    # repr=False keeps secrets out of repr()/str() and so out of logs and tracebacks.
    secrets_key: str = Field(repr=False)
    session_secret: str = Field(repr=False)
    owner_password_hash: str = Field(default="", repr=False)
    anthropic_api_key: str = Field(default="", repr=False)
    research_engine_api_key: str = Field(default="", repr=False)
    typesafe_api_key: str = Field(default="", repr=False)
    research_engine_url: str = "http://research-engine-app:8000"
    db_path: str = "data/ai-newsdesk.sqlite"
    tz: str = "Europe/London"
    log_level: str = "INFO"
    git_sha: str = "unknown"
    scheduler_enabled: bool = True

    @field_validator("secrets_key", "session_secret")
    @classmethod
    def _real_secret(cls, v: str, info: ValidationInfo) -> str:
        """Refuse the placeholder and short secrets; name the variable, never the value."""
        name = (info.field_name or "secret").upper()
        if v == PLACEHOLDER_SECRET or len(v) < MIN_SECRET_LENGTH:
            what = (
                "is the .env.example placeholder"
                if v == PLACEHOLDER_SECRET
                else f"is shorter than {MIN_SECRET_LENGTH} characters"
            )
            raise ValueError(
                f"{name} {what}; it must be a random secret of at least {MIN_SECRET_LENGTH} "
                "characters. Generate one with: openssl rand -hex 32"
            )
        return v

    @field_validator("owner_password_hash")
    @classmethod
    def _argon2_or_empty(cls, v: str) -> str:
        if v and not v.startswith("$argon2"):
            raise ValueError(
                "OWNER_PASSWORD_HASH must be an argon2 hash (run `make set-password`) or empty"
            )
        return v

    @field_validator("tz")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"TZ must be an IANA time zone name, e.g. Europe/London: {v}") from exc
        return v

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, v: str) -> str:
        level = v.upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(LOG_LEVELS)}")
        return level

    @property
    def zone(self) -> ZoneInfo:
        """The schedule and budget-month time zone."""
        return ZoneInfo(self.tz)

    @property
    def login_enabled(self) -> bool:
        return bool(self.owner_password_hash)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
