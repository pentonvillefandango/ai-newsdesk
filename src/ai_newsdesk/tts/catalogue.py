"""The ElevenLabs catalogue: the account's voices and each model's credit factor, cached in the
settings table under `elevenlabs_catalogue` (feedback-3).

A refresh needs both lists; when either call fails the last good copy stays. Without a copy a
suggested voice keeps its name, any other is named by its id, and every factor is 1.0, which never
under-counts. Factors from the API are clamped to [0.0, 10.0] (non-finite ones become 1.0), so a
wild value can never overflow the billing arithmetic.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from pydantic import BaseModel, ValidationError, field_validator

from ai_newsdesk.models import VoiceInfo
from ai_newsdesk.protocols import Narrator
from ai_newsdesk.store.store import Store

log = logging.getLogger(__name__)

SETTING_KEY = "elevenlabs_catalogue"
STALE_AFTER = timedelta(hours=24)
MAX_FACTOR = 10.0
SUGGESTED_VOICES = (
    ("pFZP5JQG7iQjIQuC4Bku", "Lily", "British, news and narration"),  # the default
    ("onwK4e9ZLuTAKqWW03F9", "Daniel", "British, steady broadcaster"),
    ("XrExE9yKIg1WjnnlVkGX", "Matilda", "American, professional"),
)
# The voice setting's "Random (any voice)": a new voice each time audio is made (feedback-5).
RANDOM_VOICE = "random"
_SUGGESTED_NAMES = {vid: name for vid, name, _ in SUGGESTED_VOICES} | {RANDOM_VOICE: "Random voice"}


def fallback_voice_name(voice_id: str) -> str:
    """A suggested voice's name, else the id: used before the account's list has loaded."""
    return _SUGGESTED_NAMES.get(voice_id, voice_id)


def clamp_factor(value: float) -> float:
    """Non-finite or non-positive factors count as 1.0 (a 0 would record nothing); cap 10."""
    if not math.isfinite(value) or value <= 0:
        return 1.0
    return min(MAX_FACTOR, value)


class CatalogueData(BaseModel):
    fetched_at: datetime
    voices: list[VoiceInfo]
    model_factors: dict[str, float]

    @field_validator("model_factors")
    @classmethod
    def _clamp(cls, value: dict[str, float]) -> dict[str, float]:
        return {k: clamp_factor(v) for k, v in value.items()}


class ElevenLabsCatalogue:
    def __init__(self, store: Store, narrator: Narrator) -> None:
        self.store = store
        self.narrator = narrator
        self._data: CatalogueData | None = None

    async def load(self) -> CatalogueData | None:
        """Read the stored copy into memory (a corrupt one is ignored)."""
        raw = await self.store.get_setting(SETTING_KEY)
        if raw is None:
            return None
        try:
            self._data = CatalogueData.model_validate(raw)
        except ValidationError:
            log.warning("stored ElevenLabs catalogue is unreadable; ignored")
            return None
        return self._data

    def cached(self) -> CatalogueData | None:
        return self._data

    def is_stale(self) -> bool:
        data = self._data
        return data is None or self.store.clock.now() - data.fetched_at > STALE_AFTER

    async def refresh(self) -> bool:
        """Fetch both lists and store them. Never raises: on failure the old copy stays."""
        try:
            voices = await self.narrator.voices()
            factors = await self.narrator.model_factors()
            if voices is None or factors is None:
                raise ValueError("no answer")
            data = CatalogueData(
                fetched_at=self.store.clock.now(), voices=voices, model_factors=factors
            )
            await self.store.set_setting(SETTING_KEY, data.model_dump(mode="json"))
        except Exception as exc:  # failures are local: keep what we had
            log.warning(
                "ElevenLabs catalogue refresh failed (%s); kept the last good copy",
                type(exc).__name__,
            )
            return False
        self._data = data
        log.info("ElevenLabs catalogue refreshed: %d voices", len(voices))
        return True

    def voice_ids(self) -> list[str]:
        """The account's voices, or the suggested ones before the list has loaded."""
        data = self._data
        if data is not None and data.voices:
            return [voice.voice_id for voice in data.voices]
        return [vid for vid, _, _ in SUGGESTED_VOICES]

    def voice_name(self, voice_id: str) -> str:
        data = self._data
        if data is not None:
            for voice in data.voices:
                if voice.voice_id == voice_id:
                    return voice.name
        return fallback_voice_name(voice_id)

    def factor(self, model_id: str) -> float:
        data = self._data
        if data is None:
            return 1.0
        return data.model_factors.get(model_id, 1.0)
