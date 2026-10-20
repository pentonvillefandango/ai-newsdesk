"""Run-time settings and API keys, kept in the database `settings` table (V1-S1).

Each RuntimeSettings field is stored under its own key (the field name). API keys are
stored encrypted under `secret.<name>`; a key saved in the UI wins over the `.env` value.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai_newsdesk.config import Settings
from ai_newsdesk.secrets_box import SecretsBox, SecretsBoxError
from ai_newsdesk.store.store import Store

log = logging.getLogger(__name__)

RUNS_PER_DAY_CHOICES = (1, 2, 4, 6, 12, 24)
DEFAULT_MODEL = "claude-haiku-5-5"
ApiKeyName = Literal[
    "anthropic", "research_engine", "typesafe", "openai", "openrouter", "elevenlabs"
]
# Who can do each AI job: the judge alone may also be Jev (TypeSafe).
ModelProvider = Literal["anthropic", "openai", "openrouter"]
JudgeProvider = Literal["anthropic", "typesafe", "openai", "openrouter"]
API_KEY_NAMES: tuple[ApiKeyName, ...] = get_args(ApiKeyName)
SECRET_PREFIX = "secret."  # noqa: S105  # a settings-key prefix, not a secret


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    runs_per_day: int = 4
    briefing_time: str = Field(default="07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    daily_review_cap: int = Field(default=500, ge=0)
    monthly_budget_usd: float = Field(default=5.0, ge=0, le=1000, allow_inf_nan=False)
    judge_provider: JudgeProvider = "anthropic"
    judge_model: str = Field(default=DEFAULT_MODEL, min_length=1)
    writer_model: str = Field(default=DEFAULT_MODEL, min_length=1)
    briefing_model: str = Field(default=DEFAULT_MODEL, min_length=1)
    # Multi-provider: each job picks its provider. Missing keys in an older database take these
    # defaults, which match the single-provider behaviour.
    writer_provider: ModelProvider = "anthropic"
    briefing_provider: ModelProvider = "anthropic"
    consolidator_provider: ModelProvider = "anthropic"
    consolidator_model: str = Field(default=DEFAULT_MODEL, min_length=1)
    show_threshold: int = Field(default=4, ge=0, le=10)
    why_it_matters: bool = True
    hn_min_points: int = Field(default=100, ge=0)
    # Briefing read aloud (ElevenLabs).
    narration_enabled: bool = True
    narration_on_demand: bool = False  # True: only Make audio makes it (feedback-5)
    narration_voice_id: str = Field(default="pFZP5JQG7iQjIQuC4Bku", min_length=1, max_length=64)
    narration_model: str = Field(default="eleven_v4", min_length=1, max_length=64)
    narration_monthly_chars: int = Field(default=300_000, ge=0, le=10_000_000)

    def _jobs(self) -> list[tuple[str, str]]:
        """(provider, model) per AI job, writer first."""
        return [
            (self.writer_provider, self.writer_model),
            (self.judge_provider, self.judge_model),
            (self.briefing_provider, self.briefing_model),
            (self.consolidator_provider, self.consolidator_model),
        ]

    def anthropic_model(self) -> str:
        """A Claude model a job uses (the writer's when it is on Anthropic), else the default."""
        return next((m for p, m in self._jobs() if p == "anthropic"), DEFAULT_MODEL)

    def uses_model_list(self) -> bool:
        """True when a job is on OpenAI or OpenRouter, whose prices come from the model list."""
        return any(p in ("openai", "openrouter") for p, _ in self._jobs())

    @field_validator("runs_per_day")
    @classmethod
    def _allowed_frequency(cls, v: int) -> int:
        if v not in RUNS_PER_DAY_CHOICES:
            raise ValueError(f"runs_per_day must be one of {RUNS_PER_DAY_CHOICES}")
        return v


async def load_runtime_settings(store: Store) -> RuntimeSettings:
    """Stored values over defaults. An invalid stored value is ignored (and logged by key)."""
    values: dict[str, Any] = {}
    for name in RuntimeSettings.model_fields:
        raw = await store.get_setting(name)
        if raw is None:
            continue
        try:
            RuntimeSettings.model_validate({name: raw})
        except ValidationError:
            log.warning("ignoring invalid stored setting %s; using the default", name)
            continue
        values[name] = raw
    return RuntimeSettings.model_validate(values)


async def save_runtime_settings(store: Store, rs: RuntimeSettings) -> None:
    for name, value in rs.model_dump(mode="json").items():
        await store.set_setting(name, value)


async def update_runtime_settings(store: Store, **changes: Any) -> RuntimeSettings:
    """Validate `changes` against the current settings, save and return the result.

    Raises pydantic.ValidationError (nothing saved) when any change is invalid.
    """
    current = await load_runtime_settings(store)
    updated = RuntimeSettings.model_validate({**current.model_dump(), **changes})
    await save_runtime_settings(store, updated)
    return updated


class ApiKeyStatus(BaseModel):
    """What the Settings page may show about a key: never the key itself."""

    name: ApiKeyName
    set_in_ui: bool
    set_in_env: bool
    updated_at: datetime | None = None

    @property
    def is_set(self) -> bool:
        return self.set_in_ui or self.set_in_env


class ApiKeys:
    """Write-only API key storage. Values never leave except through resolve_api_key()."""

    def __init__(self, store: Store, settings: Settings, box: SecretsBox | None = None) -> None:
        self._store = store
        self._settings = settings
        self._box = box or SecretsBox(settings.secrets_key)

    def __repr__(self) -> str:
        return "ApiKeys(<redacted>)"

    @staticmethod
    def _key(name: ApiKeyName) -> str:
        if name not in API_KEY_NAMES:
            raise ValueError(f"unknown API key name: {name!r}")
        return SECRET_PREFIX + name

    def _env_value(self, name: ApiKeyName) -> str:
        return getattr(self._settings, f"{name}_api_key")

    async def set_api_key(self, name: ApiKeyName, value: str, at: datetime) -> None:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("an API key cannot be empty; use clear_api_key to remove it")
        record = {
            "token": self._box.encrypt(cleaned),
            "updated_at": at.astimezone(UTC).isoformat(),
        }
        await self._store.set_setting(self._key(name), record)
        log.info("API key %s saved", name)

    async def clear_api_key(self, name: ApiKeyName) -> None:
        await self._store.set_setting(self._key(name), None)
        log.info("API key %s cleared; the .env value applies", name)

    async def api_key_status(self, name: ApiKeyName) -> ApiKeyStatus:
        record = await self._store.get_setting(self._key(name))
        return ApiKeyStatus(
            name=name,
            set_in_ui=record is not None,
            set_in_env=bool(self._env_value(name)),
            updated_at=datetime.fromisoformat(record["updated_at"]) if record else None,
        )

    async def available(self) -> set[ApiKeyName]:
        """The names whose key resolves (UI or `.env`); never the values."""
        return {name for name in API_KEY_NAMES if await self.resolve_api_key(name)}

    async def resolve_api_key(self, name: ApiKeyName) -> str:
        """The UI value, else the `.env` value, else "". An undecryptable UI value is skipped."""
        record = await self._store.get_setting(self._key(name))
        if record is not None:
            try:
                return self._box.decrypt(record["token"])
            except SecretsBoxError:
                log.warning("stored %s API key could not be decrypted; using the .env value", name)
        return self._env_value(name)
