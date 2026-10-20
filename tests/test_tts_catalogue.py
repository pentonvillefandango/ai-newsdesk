"""ElevenLabsCatalogue: the cached voice list and model credit factors (feedback-3)."""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import VoiceInfo
from ai_newsdesk.store.store import Store
from ai_newsdesk.tts.catalogue import SETTING_KEY, CatalogueData, ElevenLabsCatalogue
from tests.fakes import FakeNarrator

DANIEL = VoiceInfo(voice_id="onwK4e9ZLuTAKqWW03F9", name="Daniel", labels={"accent": "british"})
LILY = VoiceInfo(voice_id="pFZP5JQG7iQjIQuC4Bku", name="Lily")


def _narrator(**kw: object) -> FakeNarrator:
    return FakeNarrator(
        voices_result=kw.get("voices", [DANIEL, LILY]),  # type: ignore[arg-type]
        factors_result=kw.get("factors", {"eleven_v4": 1.0, "eleven_flash_v2_5": 0.5}),  # type: ignore[arg-type]
    )


async def test_nothing_cached_means_defaults(store: Store) -> None:
    cat = ElevenLabsCatalogue(store, _narrator())
    assert await cat.load() is None
    assert cat.cached() is None
    assert cat.factor("eleven_v4") == 1.0
    # Before the account's list loads, the suggested voices still have names; others keep the id.
    assert cat.voice_name("onwK4e9ZLuTAKqWW03F9") == "Daniel"
    assert cat.voice_name("XrExE9yKIg1WjnnlVkGX") == "Matilda"
    assert cat.voice_name("nobody") == "nobody"
    assert cat.is_stale() is True


async def test_refresh_stores_and_a_new_instance_loads_it(store: Store, clock: FixedClock) -> None:
    cat = ElevenLabsCatalogue(store, _narrator())
    assert await cat.refresh() is True
    data = cat.cached()
    assert data is not None
    assert data.fetched_at == clock.now()
    assert [v.name for v in data.voices] == ["Daniel", "Lily"]
    raw = await store.get_setting(SETTING_KEY)
    assert set(raw) == {"fetched_at", "voices", "model_factors"}

    again = ElevenLabsCatalogue(store, FakeNarrator())
    assert await again.load() == data
    assert again.factor("eleven_flash_v2_5") == 0.5
    assert again.factor("eleven_unknown") == 1.0
    assert again.voice_name("pFZP5JQG7iQjIQuC4Bku") == "Lily"
    assert again.voice_name("nobody") == "nobody"
    assert again.is_stale() is False
    clock.advance(timedelta(hours=24, seconds=1))
    assert again.is_stale() is True


@pytest.mark.parametrize("which", ["voices", "factors"])
async def test_a_failed_refresh_keeps_the_old_copy(store: Store, which: str) -> None:
    cat = ElevenLabsCatalogue(store, _narrator())
    assert await cat.refresh() is True
    before = cat.cached()
    cat.narrator = _narrator(**{which: None})
    assert await cat.refresh() is False
    assert cat.cached() == before
    assert CatalogueData.model_validate(await store.get_setting(SETTING_KEY)) == before


async def test_factors_are_clamped_and_non_finite_or_non_positive_ones_become_one(
    store: Store,
) -> None:
    factors = {
        "huge": 1e308, "neg": -2.0, "zero": 0.0, "nan": math.nan, "inf": math.inf, "ok": 0.3,
    }  # fmt: skip
    cat = ElevenLabsCatalogue(store, _narrator(factors=factors))
    assert await cat.refresh() is True
    assert cat.factor("huge") == 10.0
    assert cat.factor("neg") == 1.0  # a 0 or negative factor would record nothing
    assert cat.factor("zero") == 1.0
    assert cat.factor("nan") == 1.0
    assert cat.factor("inf") == 1.0
    assert cat.factor("ok") == 0.3


async def test_a_corrupt_stored_copy_is_ignored(store: Store) -> None:
    await store.set_setting(SETTING_KEY, {"voices": "nope"})
    cat = ElevenLabsCatalogue(store, _narrator())
    assert await cat.load() is None
    assert cat.factor("eleven_v4") == 1.0


async def test_a_raising_narrator_never_breaks_refresh(store: Store) -> None:
    class Boom(FakeNarrator):
        async def voices(self) -> list[VoiceInfo] | None:
            raise RuntimeError("down")

    cat = ElevenLabsCatalogue(store, Boom())
    assert await cat.refresh() is False
    assert cat.cached() is None
