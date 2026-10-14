from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_newsdesk.models import (
    Category,
    ItemStatus,
    Purpose,
    SourceKind,
    SourceResult,
    Trust,
    Usage,
    UsageStats,
)
from ai_newsdesk.store import Store
from tests.web import seed

NOW = datetime(2026, 10, 14, 8, 30, tzinfo=UTC)


async def test_stories_by_ids_returns_stories_with_their_sources(store: Store) -> None:
    lab = await seed.add_source(store, name="Lab")
    hn = await seed.add_source(
        store, name="HN", kind=SourceKind.HN, trust=Trust.DISCOVERY, config={}
    )
    sid = await seed.add_story(
        store,
        at=NOW,
        headline="Lab ships a model",
        sources=[
            (lab, "https://lab.example/post", "Lab post"),
            (hn, "https://news.example/1", "HN thread"),
        ],
        signal=7,
        summary="It is faster.",
        why="Cheaper defaults.",
    )
    got = await store.stories_by_ids([sid, 999_999])
    assert list(got) == [sid]
    story = got[sid]
    assert story.headline == "Lab ships a model"
    assert story.summary == "It is faster."
    assert story.why_it_matters == "Cheaper defaults."
    assert story.feedback == 0
    assert {s.source_name for s in story.sources} == {"Lab", "HN"}
    assert {s.url for s in story.sources} == {"https://lab.example/post", "https://news.example/1"}


async def test_stories_by_ids_with_no_ids(store: Store) -> None:
    assert await store.stories_by_ids([]) == {}


async def test_unscored_items_are_recent_failed_or_queued(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    failed = await seed.add_item(
        store,
        source_id=src,
        url="https://lab.example/1",
        title="Failed",
        at=NOW,
        status=ItemStatus.REVIEW_FAILED,
    )
    queued = await seed.add_item(
        store,
        source_id=src,
        url="https://lab.example/2",
        title="Queued",
        at=NOW,
        status=ItemStatus.QUEUED,
    )
    await seed.add_item(
        store,
        source_id=src,
        url="https://lab.example/3",
        title="Reviewed",
        at=NOW,
        status=ItemStatus.REVIEWED,
    )
    await seed.add_item(
        store,
        source_id=src,
        url="https://lab.example/4",
        title="Old",
        at=NOW - timedelta(hours=30),
        status=ItemStatus.REVIEW_FAILED,
    )
    got = await store.unscored_items(NOW)
    assert {u.item_id for u in got} == {failed, queued}
    assert {u.status for u in got} == {ItemStatus.REVIEW_FAILED, ItemStatus.QUEUED}
    assert all(u.source_name == "Lab" and u.source_kind == SourceKind.FEED for u in got)
    assert len(await store.unscored_items(NOW, limit=1)) == 1


async def test_source_overview_counts_items_and_reports_health(store: Store) -> None:
    zeta = await seed.add_source(store, name="Zeta Lab")
    await seed.add_item(
        store,
        source_id=zeta,
        url="https://zeta.example/1",
        title="New",
        at=NOW - timedelta(hours=1),
    )
    await seed.add_item(
        store,
        source_id=zeta,
        url="https://zeta.example/2",
        title="Old",
        at=NOW - timedelta(days=10),
    )
    await seed.add_story(
        store, at=NOW, headline="Story", sources=[(zeta, "https://zeta.example/3", "In a story")]
    )
    await store.record_source_result(SourceResult(source_id=zeta, error="HTTP 503 from feed"), NOW)
    healthy = await seed.add_source(store, name="Healthy")
    await store.record_source_result(SourceResult(source_id=healthy), NOW)
    muted = await seed.add_source(store, name="Alpha", enabled=False)

    rows = {r.source.id: r for r in await store.source_overview(NOW)}
    assert rows[zeta].items_total == 3
    assert rows[zeta].items_7d == 2
    assert rows[zeta].stories_7d == 1
    assert rows[zeta].last_error == "HTTP 503 from feed"
    assert rows[healthy].last_ok_at == NOW
    assert rows[healthy].last_error is None
    assert rows[muted].source.enabled is False
    assert rows[muted].items_total == 0
    assert rows[muted].last_ok_at is None


async def test_usage_last_days(store: Store) -> None:
    for days_ago in (0, 1, 2):
        run_id = await store.start_run("schedule", NOW - timedelta(days=days_ago, hours=1))
        await store.finish_run(run_id, NOW - timedelta(days=days_ago), {}, 0.0, None)
    old = await store.start_run("schedule", NOW - timedelta(days=10))
    await store.finish_run(old, NOW - timedelta(days=10), {}, 0.0, None)
    src = await seed.add_source(store, name="Lab")
    for n in (1, 2):
        await seed.add_item(
            store,
            source_id=src,
            url=f"https://lab.example/{n}",
            title=f"T{n}",
            at=NOW - timedelta(hours=1),
            status=ItemStatus.REVIEWED,
        )
    await seed.add_item(
        store,
        source_id=src,
        url="https://lab.example/3",
        title="T3",
        at=NOW,
        status=ItemStatus.REVIEW_FAILED,
    )

    def usage(purpose: Purpose) -> Usage:
        return Usage(
            provider="anthropic", model="claude-haiku-5-5", purpose=purpose, input_tokens=10
        )

    await store.add_spend(usage(Purpose.REVIEW), 0.02, NOW - timedelta(hours=1))
    await store.add_spend(usage(Purpose.WRITE), 0.01, NOW - timedelta(hours=1))
    await store.add_spend(usage(Purpose.BRIEFING), 0.005, NOW - timedelta(hours=1))
    await store.add_spend(usage(Purpose.REVIEW), 1.0, NOW - timedelta(days=9))

    stats = await store.usage_last_days(NOW)
    assert isinstance(stats, UsageStats)
    assert (stats.days, stats.runs, stats.items_reviewed) == (7, 3, 2)
    assert stats.review_usd == pytest.approx(0.02)
    assert stats.write_usd == pytest.approx(0.01)
    assert stats.briefing_usd == pytest.approx(0.005)


async def _failed_story(store: Store, src: int) -> tuple[int, int]:
    """A story whose only item failed review (shown, never hidden)."""
    item = await seed.add_item(
        store,
        source_id=src,
        url="https://lab.example/failed-in-story",
        title="Failed in a story",
        at=NOW,
        status=ItemStatus.REVIEW_FAILED,
    )
    story = await store.create_story("Failed in a story", Category.OTHER, NOW)
    await store.attach_item(item, story, NOW)
    await store.recompute_story_score(story)
    return item, story


async def test_unscored_items_leave_out_items_already_in_a_story(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    loose = await seed.add_item(
        store,
        source_id=src,
        url="https://lab.example/loose",
        title="Loose",
        at=NOW,
        status=ItemStatus.REVIEW_FAILED,
    )
    await _failed_story(store, src)
    assert [u.item_id for u in await store.unscored_items(NOW)] == [loose]


async def test_stories_say_whether_any_item_was_scored(store: Store) -> None:
    src = await seed.add_source(store, name="Lab")
    _, failed = await _failed_story(store, src)
    reviewed = await seed.add_story(
        store, at=NOW, headline="Lab ships", sources=[(src, "https://lab.example/ok", "Lab ships")]
    )
    today = {s.id: s for s in await store.today_stories(NOW, 4)}
    assert (today[failed].scored, today[reviewed].scored) == (False, True)
    by_id = await store.stories_by_ids([failed, reviewed])
    assert (by_id[failed].scored, by_id[reviewed].scored) == (False, True)
