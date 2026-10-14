from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ai_newsdesk.models import ItemStatus, SourceKind, Trust
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
