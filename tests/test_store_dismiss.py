"""Dismissing cards (feedback-1 dismiss): hides a Today story or an X post, nothing else."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from ai_newsdesk.models import Candidate, ItemStatus, SourceKind, Trust
from ai_newsdesk.store import Store
from ai_newsdesk.store.db import apply_migrations, connect
from tests.web import seed

NOW = datetime(2026, 10, 14, 8, 30, tzinfo=UTC)
AT = NOW - timedelta(hours=2)


async def _story(store: Store, src: int, headline: str, **kw: object) -> int:
    slug = "-".join(headline.lower().split())
    return await seed.add_story(
        store,
        at=AT,
        headline=headline,
        sources=[(src, f"https://lab.example/{slug}", headline)],
        **kw,  # type: ignore[arg-type]
    )


async def _x_source(store: Store, handle: str = "example_researcher") -> int:
    return await seed.add_source(
        store,
        name=f"@{handle}",
        kind=SourceKind.X_PERSON,
        trust=Trust.TRUSTED,
        config={"handle": handle},
    )


async def _x_post(store: Store, src: int, n: int) -> int:
    ids = await store.add_candidates(
        [
            Candidate(
                source_id=src,
                url=f"https://x.com/example_researcher/status/{n}",
                title=f"Post {n}",
                snippet=f"Text {n}",
                published_at=AT,
            )
        ],
        AT,
    )
    return ids[0]


async def _dismissed_at(store: Store, table: str, row_id: int) -> str | None:
    row = await store._one(f"SELECT dismissed_at FROM {table} WHERE id = ?", (row_id,))  # noqa: S608
    assert row is not None
    return row["dismissed_at"]


async def test_migration_adds_dismissed_at_to_stories_and_items(db_path: str) -> None:
    conn = await connect(db_path)
    try:
        assert await apply_migrations(conn) == [1, 2, 3, 4]
        assert await apply_migrations(conn) == []
        for table in ("stories", "items"):
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                cols = {r["name"]: r for r in await cur.fetchall()}
            assert "dismissed_at" in cols, table
            assert cols["dismissed_at"]["notnull"] == 0
            assert cols["dismissed_at"]["dflt_value"] is None
    finally:
        await conn.close()


async def test_migration_keeps_existing_rows_visible(db_path: str) -> None:
    conn = await connect(db_path)
    try:
        await apply_migrations(conn)
        await conn.execute(
            "INSERT INTO stories (headline, category, first_seen_at, last_updated_at)"
            " VALUES ('h', 'research', '2026-10-14T00:00:00+00:00', '2026-10-14T00:00:00+00:00')"
        )
        async with conn.execute("SELECT dismissed_at FROM stories") as cur:
            assert [r[0] for r in await cur.fetchall()] == [None]
    finally:
        await conn.close()


async def test_dismiss_story_hides_it_from_today(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    keep = await _story(store, src, "Kept story")
    gone = await _story(store, src, "Gone story")
    assert await store.dismiss_story(gone, NOW) is True
    assert [s.id for s in await store.today_stories(NOW, 0)] == [keep]
    hidden = await store.today_stories(NOW, 0, dismissed=True)
    assert [s.id for s in hidden] == [gone]
    assert hidden[0].dismissed is True
    assert await store.count_dismissed_stories(NOW, 0) == 1
    assert await _dismissed_at(store, "stories", gone) == NOW.isoformat(timespec="microseconds")


async def test_dismiss_story_is_idempotent_and_keeps_the_first_time(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    sid = await _story(store, src, "A story")
    assert await store.dismiss_story(sid, NOW) is True
    assert await store.dismiss_story(sid, NOW + timedelta(hours=1)) is True
    assert await _dismissed_at(store, "stories", sid) == NOW.isoformat(timespec="microseconds")
    assert await store.count_dismissed_stories(NOW, 0) == 1


async def test_undismiss_story_brings_it_back(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    sid = await _story(store, src, "A story")
    await store.dismiss_story(sid, NOW)
    assert await store.undismiss_story(sid) is True
    assert await store.undismiss_story(sid) is True  # idempotent
    assert [s.id for s in await store.today_stories(NOW, 0)] == [sid]
    assert await store.count_dismissed_stories(NOW, 0) == 0
    assert await _dismissed_at(store, "stories", sid) is None


async def test_unknown_story_ids(store: Store) -> None:
    assert await store.dismiss_story(999, NOW) is False
    assert await store.undismiss_story(999) is False


async def test_dismiss_needs_an_aware_time(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    sid = await _story(store, src, "A story")
    with pytest.raises(ValueError, match="timezone-aware"):
        await store.dismiss_story(sid, datetime(2026, 10, 14))


async def test_dismissed_count_uses_the_same_window_as_today(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    low = await _story(store, src, "Low story", signal=3)
    old = await seed.add_story(
        store,
        at=NOW - timedelta(hours=30),
        headline="Old story",
        sources=[(src, "https://lab.example/old", "Old")],
    )
    for sid in (low, old):
        await store.dismiss_story(sid, NOW)
    assert await store.count_dismissed_stories(NOW, 5) == 0
    assert await store.count_dismissed_stories(NOW, 0) == 1
    assert await store.count_dismissed_stories(NOW, 0, hours=48) == 2


async def test_stories_by_ids_still_returns_dismissed_stories_and_says_so(store: Store) -> None:
    """The briefing and the Undo/Restore card refresh read dismissed stories by id."""
    src = await seed.add_source(store, name="Lab")
    sid = await _story(store, src, "A story")
    await store.dismiss_story(sid, NOW)
    got = await store.stories_by_ids([sid])
    assert got[sid].dismissed is True
    assert (await store.top_stories_for_briefing(NOW))[0].story_id == sid


async def test_dismissed_story_stays_dismissed_when_new_sources_join(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    other = await seed.add_source(store, name="Other Lab")
    sid = await _story(store, src, "A story")
    await store.dismiss_story(sid, NOW)
    item = await seed.add_item(
        store, source_id=other, url="https://other.example/a", title="A", at=NOW
    )
    await store.attach_item(item, sid, NOW)
    await store.recompute_story_score(sid)
    assert await store.today_stories(NOW, 0) == []
    assert await store.count_dismissed_stories(NOW, 0) == 1


async def test_merge_keeps_the_kept_storys_own_dismissal(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    keep = await _story(store, src, "Keep me")
    merged = await _story(store, src, "Merge me")
    await store.dismiss_story(merged, NOW)
    await store.merge_stories(keep, [merged])
    assert [s.id for s in await store.today_stories(NOW, 0)] == [keep]

    gone = await _story(store, src, "Dismissed keeper")
    visible = await _story(store, src, "Visible one")
    await store.dismiss_story(gone, NOW)
    await store.merge_stories(gone, [visible])
    assert gone not in [s.id for s in await store.today_stories(NOW, 0)]
    assert [s.id for s in await store.today_stories(NOW, 0, dismissed=True)] == [gone]


async def test_dismiss_does_not_touch_feedback_or_weights(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    sid = await _story(store, src, "A story")
    await store.set_feedback(sid, 1)
    before_sources = await store.list_sources(enabled_only=False)
    before_examples = await store.feedback_examples()
    await store.dismiss_story(sid, NOW)
    await store.undismiss_story(sid)
    await store.dismiss_story(sid, NOW)
    assert await store.list_sources(enabled_only=False) == before_sources
    assert await store.feedback_examples() == before_examples
    assert (await store.stories_by_ids([sid]))[sid].feedback == 1


async def test_dismiss_x_post_hides_it_from_the_x_page(store: Store) -> None:
    src = await _x_source(store)
    keep = await _x_post(store, src, 1)
    gone = await _x_post(store, src, 2)
    assert await store.dismiss_item(gone, NOW) is True
    assert await store.dismiss_item(gone, NOW + timedelta(hours=1)) is True
    assert [p.item_id for p in await store.x_posts(NOW)] == [keep]
    hidden = await store.x_posts(NOW, dismissed=True)
    assert [p.item_id for p in hidden] == [gone]
    assert hidden[0].dismissed is True
    assert await store.count_dismissed_x_posts(NOW) == 1
    assert await _dismissed_at(store, "items", gone) == NOW.isoformat(timespec="microseconds")
    assert await store.undismiss_item(gone) is True
    assert await store.count_dismissed_x_posts(NOW) == 0
    assert {p.item_id for p in await store.x_posts(NOW)} == {keep, gone}


async def test_dismissed_x_count_uses_the_x_page_window(store: Store) -> None:
    src = await _x_source(store)
    ids = await store.add_candidates(
        [Candidate(source_id=src, url="https://x.com/example_researcher/status/9", title="Old")],
        NOW - timedelta(days=8),
    )
    await store.dismiss_item(ids[0], NOW)
    assert await store.count_dismissed_x_posts(NOW) == 0
    assert await store.count_dismissed_x_posts(NOW, days=10) == 1


async def test_dismiss_item_only_works_on_x_posts(store: Store) -> None:
    lab = await seed.add_source(store, name="Lab")
    item = await seed.add_item(store, source_id=lab, url="https://lab.example/1", title="T", at=AT)
    assert await store.dismiss_item(item, NOW) is False
    assert await store.undismiss_item(item) is False
    assert await store.dismiss_item(999, NOW) is False
    assert await store.undismiss_item(999) is False
    assert await _dismissed_at(store, "items", item) is None


async def test_unscored_items_leave_out_dismissed_items(store: Store) -> None:
    lab = await seed.add_source(store, name="Lab")
    shown = await seed.add_item(
        store,
        source_id=lab,
        url="https://lab.example/1",
        title="Shown",
        at=AT,
        status=ItemStatus.QUEUED,
    )
    hidden = await seed.add_item(
        store,
        source_id=lab,
        url="https://lab.example/2",
        title="Hidden",
        at=AT,
        status=ItemStatus.QUEUED,
    )
    # Only X posts can be dismissed through the API today; the filter guards the list anyway.
    async with aiosqlite.connect(store.db_path) as raw:
        await raw.execute(
            "UPDATE items SET dismissed_at = ? WHERE id = ?",
            (NOW.isoformat(timespec="microseconds"), hidden),
        )
        await raw.commit()
    assert [i.item_id for i in await store.unscored_items(NOW)] == [shown]


async def test_sources_stats_still_count_dismissed_items(store: Store) -> None:
    src = await _x_source(store)
    post = await _x_post(store, src, 1)
    await store.dismiss_item(post, NOW)
    overview = {o.source.id: o for o in await store.source_overview(NOW)}
    assert overview[src].items_7d == 1
    assert overview[src].items_total == 1
