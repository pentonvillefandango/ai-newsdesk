"""The briefing page's Listen bar, the MP3 route and the Make audio button (feedback-3)."""

from __future__ import annotations

import asyncio
from datetime import date
from html import unescape

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_newsdesk import pipeline
from ai_newsdesk.audio_files import AudioFiles
from ai_newsdesk.pipeline import NarrationOutcome
from ai_newsdesk.runtime_settings import update_runtime_settings
from ai_newsdesk.store.records import BriefingAudioRecord
from tests.web.support import NOW, csrf_token, run

DAY = date(2026, 10, 14)
MP3 = bytes(range(256)) * 40  # 10240 bytes: not a real MP3, the route does not care


@pytest.fixture
def audio_dir(app: FastAPI, tmp_path_factory: pytest.TempPathFactory) -> AudioFiles:
    files = AudioFiles(tmp_path_factory.mktemp("audio"))
    app.state.ctx.audio = files
    return files


def _briefing(client: TestClient, app: FastAPI, day: date = DAY) -> None:
    run(client, app.state.store.save_briefing, day, "claude-haiku-5-5", "Some news.", [], NOW)


def _audio(
    client: TestClient, app: FastAPI, files: AudioFiles, day: date = DAY, data: bytes = MP3
) -> str:
    _briefing(client, app, day)
    name = files.write(day, data)
    rec = BriefingAudioRecord(
        date=day,
        voice_id="v1",
        voice_name="Daniel",
        model_id="eleven_v4",
        characters=1000,
        bytes=len(data),
        file=name,
        created_at=NOW,
    )
    run(client, app.state.store.save_briefing_audio, rec)
    return name


def test_mp3_needs_a_session(app: FastAPI, anon: TestClient, audio_dir: AudioFiles) -> None:
    r = anon.get(f"/briefing/{DAY}/audio.mp3", follow_redirects=False)
    assert r.status_code in (303, 401)


def test_mp3_is_404_without_row_or_file(
    app: FastAPI, client: TestClient, audio_dir: AudioFiles
) -> None:
    assert client.get(f"/briefing/{DAY}/audio.mp3").status_code == 404
    name = _audio(client, app, audio_dir)
    audio_dir.delete(name)
    assert client.get(f"/briefing/{DAY}/audio.mp3").status_code == 404
    assert client.get("/briefing/not-a-day/audio.mp3").status_code == 404


def test_mp3_is_404_without_an_audio_directory(app: FastAPI, client: TestClient) -> None:
    assert client.get(f"/briefing/{DAY}/audio.mp3").status_code == 404


def test_mp3_is_served_with_cache_header_and_range(
    app: FastAPI, client: TestClient, audio_dir: AudioFiles
) -> None:
    _audio(client, app, audio_dir)
    r = client.get(f"/briefing/{DAY}/audio.mp3")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["cache-control"] == "private, max-age=86400"
    assert r.content == MP3
    part = client.get(f"/briefing/{DAY}/audio.mp3", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 0-9/{len(MP3)}"
    assert part.content == MP3[:10]


def test_page_with_audio_has_player_speeds_and_caption(
    app: FastAPI, client: TestClient, audio_dir: AudioFiles
) -> None:
    _briefing(client, app)
    _audio(client, app, audio_dir, data=b"x" * 960_000)  # 2 minutes at 64 kbps
    text = client.get("/briefing").text
    assert f'<audio controls preload="metadata" src="/briefing/{DAY}/audio.mp3">' in text
    for rate in ("1", "1.25", "1.5"):
        assert f'<button type="button" data-rate="{rate}"' in text
    assert "aria-pressed" in text
    assert "Read by Daniel · 2 min · made 09:30" in text  # 08:30 UTC is 09:30 in London
    assert "Make audio" not in text
    assert "/static/briefing-audio.js" in text


def test_short_audio_is_one_minute_at_least(
    app: FastAPI, client: TestClient, audio_dir: AudioFiles
) -> None:
    _briefing(client, app)
    _audio(client, app, audio_dir, data=b"x" * 100)
    assert "Read by Daniel · 1 min · made" in client.get("/briefing").text


def test_older_day_page_points_at_its_own_audio(
    app: FastAPI, client: TestClient, audio_dir: AudioFiles
) -> None:
    old = date(2026, 10, 13)
    _briefing(client, app, old)
    _briefing(client, app, DAY)
    _audio(client, app, audio_dir, old)
    assert f'src="/briefing/{old}/audio.mp3"' in client.get(f"/briefing/{old}").text
    assert "<audio" not in client.get("/briefing").text


def _set(client: TestClient, app: FastAPI, **values: object) -> None:
    run(client, lambda: update_runtime_settings(app.state.store, **values))


def test_reason_off_has_no_button(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app)
    _set(client, app, narration_enabled=False)
    text = client.get("/briefing").text
    assert "Audio is off in Settings." in text
    assert "Make audio" not in text


def test_reason_no_key_has_no_button(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app)

    async def no_key() -> bool:
        return False

    app.state.ctx.has_narration_key = no_key
    text = client.get("/briefing").text
    assert "Add an ElevenLabs key in Settings to hear this briefing." in text
    assert "Make audio" not in text


def test_reason_none_yet_and_making_and_busy(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app)
    text = client.get("/briefing").text
    assert "No audio yet." in text
    assert "Make audio" in text
    # ?audio=making alone is not enough: "Making audio" shows only while the day's audio is
    # being made (the day is in app.state.audio_in_flight), with or without the query.
    assert "No audio yet." in client.get("/briefing?audio=making").text
    app.state.audio_in_flight.add(DAY)
    try:
        for url in ("/briefing?audio=making", "/briefing", f"/briefing/{DAY}"):
            making = client.get(url).text
            assert "Making audio. This takes a minute or two; reload to listen." in making
            assert "Make audio" in making
    finally:
        app.state.audio_in_flight.discard(DAY)
    busy = client.get(f"/briefing/{DAY}?audio=busy").text
    assert "A run is in progress; try again in a few minutes." in busy
    assert "Make audio" in busy


def test_reason_over_cap_from_usage_and_from_status(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app)
    sched = app.state.scheduler
    sched._narration_try_day = DAY
    sched._narration_last_outcome = NarrationOutcome.OVER_CAP
    text = client.get("/briefing").text
    assert "This month's audio allowance is used up." in unescape(text)
    assert "will be retried" not in text
    assert "Make audio" in text
    sched._narration_last_outcome = None
    assert "No audio yet." in client.get("/briefing").text
    run(client, app.state.store.record_tts_usage, NOW, DAY, "eleven_v4", 300_000, ok=True)
    assert "This month's audio allowance is used up." in unescape(client.get("/briefing").text)


def test_reason_failed_will_retry_then_gave_up(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app)
    sched = app.state.scheduler
    sched._narration_try_day = DAY
    sched._narration_last_outcome = NarrationOutcome.FAILED
    sched._narration_tries = 1
    text = client.get("/briefing").text
    assert "Audio failed; it will be retried." in text
    assert "Make audio" in text
    sched._narration_tries = 3
    text = client.get("/briefing").text
    assert "Audio failed today." in text
    assert "will be retried" not in text


def test_failed_line_is_for_its_own_day_only(app: FastAPI, client: TestClient) -> None:
    old = date(2026, 10, 13)
    _briefing(client, app, old)
    _briefing(client, app, DAY)
    sched = app.state.scheduler
    sched._narration_try_day = DAY
    sched._narration_last_outcome = NarrationOutcome.FAILED
    sched._narration_tries = 1
    assert "Audio failed" in client.get("/briefing").text
    older = client.get(f"/briefing/{old}").text
    assert "Audio failed" not in older
    assert "No audio yet." in older


def test_no_listen_bar_without_a_briefing(client: TestClient) -> None:
    assert "Make audio" not in client.get("/briefing").text
    assert "Make audio" not in client.get("/briefing/2026-10-01").text


def test_make_audio_runs_narration_under_the_lock(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _briefing(client, app)
    lock: asyncio.Lock = app.state.scheduler.lock
    events: list[tuple[date, bool]] = []

    async def fake_run_narration(ctx: object, day: date) -> NarrationOutcome:
        events.append((day, lock.locked()))
        return NarrationOutcome.DONE

    monkeypatch.setattr(pipeline, "run_narration", fake_run_narration)
    r = client.post(
        f"/briefing/{DAY}/audio", data={"csrf_token": csrf_token(client)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/briefing?audio=making"

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)
    assert events == [(DAY, True)]
    assert not lock.locked()


def test_make_audio_for_an_older_day_redirects_to_that_day(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = date(2026, 10, 13)
    _briefing(client, app, old)
    _briefing(client, app, DAY)

    async def fake(ctx: object, day: date) -> NarrationOutcome:
        return NarrationOutcome.DONE

    monkeypatch.setattr(pipeline, "run_narration", fake)
    r = client.post(
        f"/briefing/{old}/audio", data={"csrf_token": csrf_token(client)}, follow_redirects=False
    )
    assert r.headers["location"] == f"/briefing/{old}?audio=making"

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)


def test_make_audio_swallows_a_failure(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _briefing(client, app)

    async def boom(ctx: object, day: date) -> NarrationOutcome:
        raise RuntimeError("tts down")

    monkeypatch.setattr(pipeline, "run_narration", boom)
    client.post(
        f"/briefing/{DAY}/audio", data={"csrf_token": csrf_token(client)}, follow_redirects=False
    )

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)
    assert not app.state.scheduler.lock.locked()


def test_make_audio_busy_when_the_lock_is_held(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _briefing(client, app)
    calls: list[date] = []

    async def fake(ctx: object, day: date) -> NarrationOutcome:
        calls.append(day)
        return NarrationOutcome.DONE

    monkeypatch.setattr(pipeline, "run_narration", fake)
    lock: asyncio.Lock = app.state.scheduler.lock
    run(client, lock.acquire)
    try:
        r = client.post(
            f"/briefing/{DAY}/audio",
            data={"csrf_token": csrf_token(client)},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/briefing?audio=busy"
    finally:
        lock.release()
    assert calls == []
    assert not [t for t in app.state.background if t.get_name() == "manual-audio"]


def test_make_audio_404_for_bad_day_or_missing_briefing(app: FastAPI, client: TestClient) -> None:
    token = {"csrf_token": csrf_token(client)}
    assert client.post("/briefing/nonsense/audio", data=token).status_code == 404
    assert client.post("/briefing/2026-10-01/audio", data=token).status_code == 404


def test_make_audio_needs_csrf(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app)
    assert client.post(f"/briefing/{DAY}/audio").status_code == 403


def test_make_audio_without_a_scheduler_is_503(app: FastAPI, client: TestClient) -> None:
    _briefing(client, app)
    app.state.scheduler = None
    r = client.post(f"/briefing/{DAY}/audio", data={"csrf_token": csrf_token(client)})
    assert r.status_code == 503
    assert "No audio yet." in client.get("/briefing").text


def test_making_ends_when_the_work_ends_and_the_failure_shows(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While Make audio runs the day is in flight; afterwards a FAILED run shows its reason
    even with ?audio=making still in the URL."""
    _briefing(client, app)
    seen: list[bool] = []
    sched = app.state.scheduler

    async def failing(ctx: object, day: date) -> NarrationOutcome:
        seen.append(day in app.state.audio_in_flight)
        sched._narration_try_day = DAY
        sched._narration_last_outcome = NarrationOutcome.FAILED
        sched._narration_tries = 1
        return NarrationOutcome.FAILED

    monkeypatch.setattr(pipeline, "run_narration", failing)
    r = client.post(
        f"/briefing/{DAY}/audio", data={"csrf_token": csrf_token(client)}, follow_redirects=False
    )

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)
    assert seen == [True]
    assert DAY not in app.state.audio_in_flight
    text = client.get(r.headers["location"]).text  # /briefing?audio=making
    assert "Making audio" not in text
    assert "Audio failed; it will be retried." in text


def test_making_ends_after_a_raising_or_over_cap_run(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _briefing(client, app)

    async def boom(ctx: object, day: date) -> NarrationOutcome:
        raise RuntimeError("tts down")

    monkeypatch.setattr(pipeline, "run_narration", boom)
    client.post(
        f"/briefing/{DAY}/audio", data={"csrf_token": csrf_token(client)}, follow_redirects=False
    )

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)
    assert DAY not in app.state.audio_in_flight
    assert "No audio yet." in client.get("/briefing?audio=making").text


def test_write_now_marks_the_day_in_flight_while_it_works(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def fake_run_briefing(ctx: object, day: date) -> bool:
        seen.append(f"brief {day in app.state.audio_in_flight}")
        await app.state.store.save_briefing(day, "m", "Some news.", [], NOW)
        return True

    async def over_cap(ctx: object, day: date) -> NarrationOutcome:
        seen.append(f"narrate {day in app.state.audio_in_flight}")
        return NarrationOutcome.OVER_CAP

    monkeypatch.setattr(pipeline, "run_briefing", fake_run_briefing)
    monkeypatch.setattr(pipeline, "run_narration", over_cap)
    client.post("/briefing/write", data={"csrf_token": csrf_token(client)}, follow_redirects=False)

    async def drain() -> None:
        await asyncio.gather(*app.state.background)

    run(client, drain)
    assert seen == ["brief True", "narrate True"]
    assert DAY not in app.state.audio_in_flight
    assert "Making audio" not in client.get("/briefing?audio=making").text
