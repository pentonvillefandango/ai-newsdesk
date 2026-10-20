import datetime as dt

import aiosqlite
import pytest

from ai_newsdesk.store.db import MIGRATIONS_DIR, apply_migrations, connect
from ai_newsdesk.store.records import BriefingAudioRecord
from ai_newsdesk.store.store import Store

DAY = dt.date(2026, 10, 20)
AT = dt.datetime(2026, 10, 20, 8, 0, tzinfo=dt.UTC)


def _rec(day: dt.date = DAY, file: str = "briefing-2026-10-20-aaaaaaaa.mp3", at: dt.datetime = AT):
    return BriefingAudioRecord(
        date=day, voice_id="v1", voice_name="Daniel", model_id="eleven_v4",
        characters=1200, bytes=3456, file=file, created_at=at,
    )  # fmt: skip


async def _brief(store: Store, day: dt.date = DAY) -> None:
    await store.save_briefing(day, "m", "body", [1], AT)


async def test_save_get_and_previous_file(store: Store) -> None:
    await _brief(store)
    assert await store.get_briefing_audio(DAY) is None
    assert await store.save_briefing_audio(_rec()) is None
    assert await store.get_briefing_audio(DAY) == _rec()
    second = _rec(file="briefing-2026-10-20-bbbbbbbb.mp3")
    assert await store.save_briefing_audio(second) == "briefing-2026-10-20-aaaaaaaa.mp3"
    assert await store.get_briefing_audio(DAY) == second


async def test_audio_needs_a_briefing(store: Store) -> None:
    with pytest.raises(aiosqlite.IntegrityError):
        await store.save_briefing_audio(_rec())


async def test_delete_returns_file_name(store: Store) -> None:
    await _brief(store)
    assert await store.delete_briefing_audio(DAY) is None
    await store.save_briefing_audio(_rec())
    assert await store.delete_briefing_audio(DAY) == "briefing-2026-10-20-aaaaaaaa.mp3"
    assert await store.get_briefing_audio(DAY) is None


async def test_rewriting_a_briefing_drops_its_audio_row(store: Store) -> None:
    await _brief(store)
    await store.save_briefing_audio(_rec())
    await _brief(store)
    assert await store.get_briefing_audio(DAY) is None
    assert await store.get_briefing(DAY) is not None


async def test_rewriting_keeps_other_days_audio(store: Store) -> None:
    other = dt.date(2026, 10, 19)
    await _brief(store)
    await _brief(store, other)
    await store.save_briefing_audio(_rec(other, "briefing-2026-10-19-cccccccc.mp3"))
    await _brief(store)
    assert await store.get_briefing_audio(other) is not None


async def test_audio_older_than(store: Store) -> None:
    for d in (dt.date(2026, 9, 1), dt.date(2026, 9, 20), DAY):
        await _brief(store, d)
        await store.save_briefing_audio(_rec(d, f"briefing-{d}-dddddddd.mp3"))
    old = await store.audio_older_than(dt.date(2026, 9, 20))
    assert [r.date for r in old] == [dt.date(2026, 9, 1)]


async def test_month_tts_characters_uses_local_month(store: Store) -> None:
    tz = "Europe/London"
    rows = [
        (dt.datetime(2026, 9, 30, 22, 59, tzinfo=dt.UTC), 1000),  # 23:59 BST, September
        (dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.UTC), 10),  # 00:00 BST, October
        (dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC), 20),
        (dt.datetime(2026, 10, 31, 23, 59, tzinfo=dt.UTC), 40),  # 23:59 GMT, October
        (dt.datetime(2026, 11, 1, 0, 0, tzinfo=dt.UTC), 5000),  # November
    ]
    for at, n in rows:
        await store.record_tts_usage(at, DAY, "eleven_v4", n, ok=True)
    await store.record_tts_usage(AT, DAY, "eleven_v4", 7, ok=False)  # maybe billed: counts
    assert await store.month_tts_characters(AT, tz) == 10 + 20 + 40 + 7
    assert await store.month_tts_characters(dt.datetime(2026, 12, 1, tzinfo=dt.UTC), tz) == 0


async def test_migration_applies_on_database_from_0001_to_0005(db_path: str, tmp_path) -> None:
    before = tmp_path / "before"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("000[12345]_*.sql")):
        (before / path.name).write_text(path.read_text())
    conn = await connect(db_path)
    try:
        assert await apply_migrations(conn, before) == [1, 2, 3, 4, 5]
        await conn.execute(
            "INSERT INTO briefings (date, model, body, story_ids_json, created_at)"
            " VALUES ('2026-10-19', 'm', 'b', '[]', '2026-10-19T08:00:00+00:00')"
        )
        await conn.commit()
        assert await apply_migrations(conn) == [6]
        async with conn.execute("SELECT COUNT(*) FROM briefings") as cur:
            assert (await cur.fetchone())[0] == 1  # type: ignore[index]
        async with conn.execute("SELECT COUNT(*) FROM briefing_audio") as cur:
            assert (await cur.fetchone())[0] == 0  # type: ignore[index]
    finally:
        await conn.close()
