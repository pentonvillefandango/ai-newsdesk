"""pipeline.run_narration: the one code path that makes a day's briefing MP3 (feedback-3)."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from pathlib import Path

import pytest

from ai_newsdesk.audio_files import AudioFiles
from ai_newsdesk.briefing import briefing_date_label
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import NarrationResult, VoiceInfo
from ai_newsdesk.narration import chunks, speech_text
from ai_newsdesk.pipeline import NarrationOutcome, PipelineContext, prune_audio, run_narration
from ai_newsdesk.runtime_settings import update_runtime_settings
from ai_newsdesk.store.store import Store
from ai_newsdesk.tts.catalogue import ElevenLabsCatalogue
from tests.fakes import FakeNarrator
from tests.pipeline_ctx import make_ctx

DAY = date(2026, 10, 14)
BODY = "## Labs\n\nA new model shipped [Lab](https://example.com/a).\n\nThat is all."
DANIEL = "onwK4e9ZLuTAKqWW03F9"


async def _yes() -> bool:
    return True


async def _no() -> bool:
    return False


def _ctx(
    store: Store,
    clock: FixedClock,
    tmp_path: Path,
    narrator: FakeNarrator | None,
    *,
    has_key: bool = True,
    catalogue: ElevenLabsCatalogue | None = None,
) -> PipelineContext:
    ctx = make_ctx(store, clock)
    ctx.narrator = narrator
    ctx.audio = AudioFiles(tmp_path / "audio")
    ctx.has_narration_key = _yes if has_key else _no
    ctx.tts_catalogue = catalogue
    return ctx


async def _brief(store: Store, clock: FixedClock, body: str = BODY) -> None:
    await store.save_briefing(DAY, "m", body, [], clock.now())


async def _usage(store: Store) -> list[tuple[str, int, int]]:
    rows = await store._all("SELECT model_id, characters, ok FROM tts_usage ORDER BY id")
    return [(r["model_id"], r["characters"], r["ok"]) for r in rows]


def _files(tmp_path: Path) -> set[str]:
    d = tmp_path / "audio"
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


async def test_done_writes_the_file_saves_the_row_and_records_usage(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    await _brief(store, clock)
    narrator = FakeNarrator(NarrationResult(audio=b"ID3-mp3", characters_billed=77))
    ctx = _ctx(store, clock, tmp_path, narrator)

    assert await run_narration(ctx, DAY) is NarrationOutcome.DONE

    text = speech_text(BODY, briefing_date_label(DAY))
    [call] = narrator.calls
    assert call.text_chunks == chunks(text)
    assert (call.voice_id, call.model_id, call.credit_factor) == (DANIEL, "eleven_v4", 1.0)
    rec = await store.get_briefing_audio(DAY)
    assert rec is not None
    assert (rec.voice_id, rec.voice_name, rec.model_id) == (DANIEL, "Daniel", "eleven_v4")
    assert (rec.characters, rec.bytes, rec.created_at) == (77, 7, clock.now())
    assert _files(tmp_path) == {rec.file}
    assert ctx.audio is not None
    assert ctx.audio.path(rec.file).read_bytes() == b"ID3-mp3"
    assert await _usage(store) == [("eleven_v4", 77, 1)]


async def test_a_suggested_voice_is_named_before_the_voice_list_loads(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    # Live on 2026-10-20 the first narration after a deploy ran before the voice list had
    # loaded, and the caption read "Read by onwK4e9ZLuTAKqWW03F9".
    await _brief(store, clock)
    catalogue = ElevenLabsCatalogue(store, FakeNarrator())  # nothing cached or fetched yet
    ctx = _ctx(store, clock, tmp_path, FakeNarrator(), catalogue=catalogue)
    assert await run_narration(ctx, DAY) is NarrationOutcome.DONE
    rec = await store.get_briefing_audio(DAY)
    assert rec is not None and rec.voice_name == "Daniel"


async def test_the_catalogue_gives_the_voice_name_and_the_factor(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    await _brief(store, clock)
    await update_runtime_settings(store, narration_model="eleven_flash_v2_5")
    catalogue = ElevenLabsCatalogue(
        store,
        FakeNarrator(
            voices_result=[VoiceInfo(voice_id=DANIEL, name="Daniel")],
            factors_result={"eleven_flash_v2_5": 0.5},
        ),
    )
    assert await catalogue.refresh()
    narrator = FakeNarrator()
    ctx = _ctx(store, clock, tmp_path, narrator, catalogue=catalogue)
    assert await run_narration(ctx, DAY) is NarrationOutcome.DONE
    assert narrator.calls[0].credit_factor == 0.5
    rec = await store.get_briefing_audio(DAY)
    assert rec is not None and rec.voice_name == "Daniel"


async def test_a_remake_deletes_the_old_file(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    await _brief(store, clock)
    ctx = _ctx(store, clock, tmp_path, FakeNarrator())
    assert await run_narration(ctx, DAY) is NarrationOutcome.DONE
    first = await store.get_briefing_audio(DAY)
    assert await run_narration(ctx, DAY) is NarrationOutcome.DONE
    second = await store.get_briefing_audio(DAY)
    assert first is not None and second is not None and first.file != second.file
    assert _files(tmp_path) == {second.file}


async def test_off_when_disabled_or_without_a_narrator(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    await _brief(store, clock)
    narrator = FakeNarrator()
    assert await run_narration(_ctx(store, clock, tmp_path, None), DAY) is NarrationOutcome.OFF
    await update_runtime_settings(store, narration_enabled=False)
    ctx = _ctx(store, clock, tmp_path, narrator)
    assert await run_narration(ctx, DAY) is NarrationOutcome.OFF
    assert narrator.calls == []


async def test_no_key_from_the_probe_sends_nothing(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    await _brief(store, clock)
    narrator = FakeNarrator()
    ctx = _ctx(store, clock, tmp_path, narrator, has_key=False)
    assert await run_narration(ctx, DAY) is NarrationOutcome.NO_KEY
    assert narrator.calls == []


async def test_no_key_from_the_narrator_records_nothing(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    """The key can vanish between the probe and the call: the narrator's own answer counts."""
    await _brief(store, clock)
    narrator = FakeNarrator(NarrationResult(error="No ElevenLabs key."))
    ctx = _ctx(store, clock, tmp_path, narrator)
    assert await run_narration(ctx, DAY) is NarrationOutcome.NO_KEY
    assert await _usage(store) == []
    assert await store.get_briefing_audio(DAY) is None


async def test_no_briefing(store: Store, clock: FixedClock, tmp_path: Path) -> None:
    narrator = FakeNarrator()
    ctx = _ctx(store, clock, tmp_path, narrator)
    assert await run_narration(ctx, DAY) is NarrationOutcome.NO_BRIEFING
    assert narrator.calls == []


async def test_over_cap_counts_this_months_usage_and_the_factor(
    store: Store, clock: FixedClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await _brief(store, clock)
    size = len(speech_text(BODY, briefing_date_label(DAY)))
    narrator = FakeNarrator()
    ctx = _ctx(store, clock, tmp_path, narrator)
    await store.record_tts_usage(clock.now(), DAY, "eleven_v4", 1000, ok=True)
    await update_runtime_settings(store, narration_monthly_chars=1000 + size - 1)
    with caplog.at_level(logging.INFO, logger="ai_newsdesk.pipeline"):
        assert await run_narration(ctx, DAY) is NarrationOutcome.OVER_CAP
    assert narrator.calls == []
    assert "allowance" in caplog.text
    await update_runtime_settings(store, narration_monthly_chars=1000 + size)
    assert await run_narration(ctx, DAY) is NarrationOutcome.DONE


async def test_a_clean_failure_records_no_usage(
    store: Store, clock: FixedClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await _brief(store, clock)
    narrator = FakeNarrator(NarrationResult(error="ElevenLabs said 422."))
    ctx = _ctx(store, clock, tmp_path, narrator)
    with caplog.at_level(logging.WARNING, logger="ai_newsdesk.pipeline"):
        assert await run_narration(ctx, DAY) is NarrationOutcome.FAILED
    assert "ElevenLabs said 422." in caplog.text
    assert await _usage(store) == []
    assert _files(tmp_path) == set()
    assert await store.get_briefing_audio(DAY) is None


@pytest.mark.parametrize(
    "result",
    [
        NarrationResult(characters_billed=300, maybe_billed=True, error="Timed out."),
        NarrationResult(characters_billed=120, maybe_billed=True, error="Chunk 2 failed (400)."),
    ],
    ids=["maybe-billed", "partial-chunks"],
)
async def test_billed_failures_record_usage_as_not_ok(
    store: Store, clock: FixedClock, tmp_path: Path, result: NarrationResult
) -> None:
    await _brief(store, clock)
    ctx = _ctx(store, clock, tmp_path, FakeNarrator(result))
    assert await run_narration(ctx, DAY) is NarrationOutcome.FAILED
    assert await _usage(store) == [("eleven_v4", result.characters_billed, 0)]
    assert _files(tmp_path) == set()


async def test_a_briefing_rewritten_mid_call_throws_the_audio_away(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    await _brief(store, clock)

    async def rewrite() -> None:
        await _brief(store, clock, body="## Labs\n\nSomething else entirely.")

    ctx = _ctx(store, clock, tmp_path, FakeNarrator(during=rewrite))
    assert await run_narration(ctx, DAY) is NarrationOutcome.FAILED
    assert _files(tmp_path) == set()
    assert await store.get_briefing_audio(DAY) is None
    assert await _usage(store) == [("eleven_v4", 100, 1)]  # it was billed all the same


async def test_prune_removes_old_audio_orphans_and_stale_temp_files_only(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    """Today is 2026-10-14 in London: days before 2026-09-14 go; other files are never touched."""
    ctx = _ctx(store, clock, tmp_path, FakeNarrator())
    assert ctx.audio is not None
    kept: dict[date, str] = {}
    for day in (date(2026, 9, 13), date(2026, 9, 14), DAY):
        await store.save_briefing(day, "m", BODY, [], clock.now())
        assert await run_narration(ctx, day) is NarrationOutcome.DONE
        rec = await store.get_briefing_audio(day)
        assert rec is not None
        kept[day] = rec.file
    orphan = ctx.audio.write(date(2026, 10, 1), b"orphan")
    d = tmp_path / "audio"
    stale_tmp = d / "briefing-2026-10-14-0123abcd.mp3.tmp"
    fresh_tmp = d / "briefing-2026-10-14-4567cdef.mp3.tmp"
    for p in (stale_tmp, fresh_tmp):
        p.write_bytes(b"half")
    old = clock.now().timestamp() - 3601
    os.utime(stale_tmp, (old, old))
    recent = clock.now().timestamp() - 600
    os.utime(fresh_tmp, (recent, recent))
    others = ["notes.txt", "briefing-notes.txt", "other.mp3", "other.tmp"]
    for name in others:
        (d / name).write_bytes(b"x")
        os.utime(d / name, (old, old))

    removed = await prune_audio(ctx)

    assert removed == 3  # the 13 September file, the orphan and the stale temp file
    assert await store.get_briefing_audio(date(2026, 9, 13)) is None
    assert await store.get_briefing_audio(date(2026, 9, 14)) is not None
    assert _files(tmp_path) == {
        kept[date(2026, 9, 14)],
        kept[DAY],
        fresh_tmp.name,
        *others,
    }
    assert orphan not in _files(tmp_path)


async def test_prune_without_an_audio_directory_is_a_no_op(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    ctx = _ctx(store, clock, tmp_path, FakeNarrator())
    assert await prune_audio(ctx) == 0
    ctx.audio = None
    assert await prune_audio(ctx) == 0


async def test_a_cancelled_narration_records_the_estimate_and_re_raises(
    store: Store, clock: FixedClock, tmp_path: Path
) -> None:
    """Shutdown mid-request: ElevenLabs may still bill, so the pre-check estimate is recorded
    (never under-count) and the cancellation propagates."""
    await _brief(store, clock)

    async def cancelled() -> None:
        raise asyncio.CancelledError

    ctx = _ctx(store, clock, tmp_path, FakeNarrator(during=cancelled))
    with pytest.raises(asyncio.CancelledError):
        await run_narration(ctx, DAY)
    size = len(speech_text(BODY, briefing_date_label(DAY)))
    assert await _usage(store) == [("eleven_v4", size, 0)]
    assert await store.get_briefing_audio(DAY) is None
