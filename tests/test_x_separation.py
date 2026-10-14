"""X posts stay out of Today stories and get their own data (feedback-1, A3)."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_newsdesk.cluster import Judged, attach
from ai_newsdesk.models import (
    Category,
    ItemStatus,
    SourceConfig,
    SourceKind,
    StoryRef,
    StoryText,
    Trust,
    Verdict,
    XPost,
)
from ai_newsdesk.store.db import MIGRATIONS_DIR, apply_migrations, connect
from ai_newsdesk.store.store import STORY_SOURCE_BONUS, Store
from tests.fakes import make_candidate
from tests.pipeline_helpers import add_item, add_source, review_input

NOW = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)


async def x_source(store: Store, handle: str = "karpathy") -> SourceConfig:
    return await add_source(
        store,
        f"@{handle}",
        kind=SourceKind.X_PERSON,
        trust=Trust.TRUSTED,
        config={"handle": f"@{handle}"},
    )


async def judged(store: Store, item_id: int, signal: int, *, junk: bool = False) -> Verdict:
    v = Verdict(item_id=item_id, signal=signal, junk=junk, category=Category.MODEL_RELEASE)
    await store.save_verdict(v, 0 if junk else signal)
    return v


# ---- cluster.attach -----------------------------------------------------------------------------


async def test_attach_never_puts_x_posts_in_stories(store: Store) -> None:
    x = await x_source(store)
    lab = await add_source(store, "Lab blog")
    story = await store.create_story("OpenAI launches GPT-5.5", Category.MODEL_RELEASE, NOW)
    news = await add_item(store, lab.id, "OpenAI launches GPT-5.5", "https://example.com/a", NOW)
    await store.attach_item(news, story, NOW)
    post = await add_item(store, x.id, "GPT-5.5 is out", "https://x.com/karpathy/status/1", NOW)
    failed = await add_item(store, x.id, "Another post", "https://x.com/karpathy/status/2", NOW)
    v = await judged(store, post, 9)
    v = v.model_copy(update={"same_story_id": story})
    await store.mark_items([failed], ItemStatus.REVIEW_FAILED)
    ref = StoryRef(id=story, headline="OpenAI launches GPT-5.5")
    js = [
        Judged(
            review_input(post, "GPT-5.5 is out", kind=SourceKind.X_PERSON, candidates=[ref]), v, 9
        ),
        Judged(review_input(failed, "Another post", kind=SourceKind.X_PERSON), None, None),
    ]
    touched = await attach(store, js, {post: story}, show_threshold=4, at=NOW)
    assert touched == []
    assert [(i, s) for i, _, s in await store.recent_titles(24) if i in (post, failed)] == [
        (post, None),
        (failed, None),
    ]
    assert len(await store.open_stories(NOW)) == 1


# ---- migration 0002 -----------------------------------------------------------------------------


async def test_migration_detaches_x_items_and_drops_emptied_stories(
    db_path: str, tmp_path: Path
) -> None:
    v1 = tmp_path / "v1"
    v1.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_init.sql", v1)
    conn = await connect(db_path)
    try:
        assert await apply_migrations(conn, v1) == [1]
        await conn.executescript(
            """
            INSERT INTO sources (id, kind, name, trust, created_at) VALUES
              (1, 'feed', 'Lab', 'primary', 't'),
              (2, 'x_person', '@a', 'trusted', 't');
            INSERT INTO stories (id, headline, category, score, lead_item_id, needs_text,
                                 text_attempts, first_seen_at, last_updated_at) VALUES
              (10, 'X only', 'other', 9, 100, 0, 0, 't', 't'),
              (11, 'Mixed, X lead', 'other', 9.5, 101, 0, 1, 't', 't'),
              (12, 'News only', 'other', 6, 103, 0, 0, 't', 't'),
              (13, 'Empty before', 'other', 0, NULL, 1, 0, 't', 't');
            INSERT INTO items (id, source_id, url, canonical_url, title, fetched_at, status,
                               signal, story_id) VALUES
              (100, 2, 'u100', 'u100', 'x post', 't', 'reviewed', 9, 10),
              (101, 2, 'u101', 'u101', 'x lead', 't', 'reviewed', 9, 11),
              (102, 1, 'u102', 'u102', 'news', 't', 'reviewed', 5, 11),
              (103, 1, 'u103', 'u103', 'news2', 't', 'reviewed', 6, 12);
            """
        )
        assert await apply_migrations(conn) == [2]
        async with conn.execute("SELECT id, story_id FROM items ORDER BY id") as cur:
            assert [tuple(r) for r in await cur.fetchall()] == [
                (100, None),
                (101, None),
                (102, 11),
                (103, 12),
            ]
        async with conn.execute(
            "SELECT id, score, lead_item_id, needs_text, text_attempts FROM stories ORDER BY id"
        ) as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
        # 10 lost its only item; 13 was empty already and is left alone.
        assert rows == [
            (11, 5.0, 102, 1, 0),
            (12, 6.0, 103, 0, 0),
            (13, 0.0, None, 1, 0),
        ]
    finally:
        await conn.close()


async def test_migration_score_counts_distinct_remaining_sources(
    db_path: str, tmp_path: Path
) -> None:
    v1 = tmp_path / "v1"
    v1.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_init.sql", v1)
    conn = await connect(db_path)
    try:
        await apply_migrations(conn, v1)
        await conn.executescript(
            """
            INSERT INTO sources (id, kind, name, trust, created_at) VALUES
              (1, 'feed', 'Lab', 'primary', 't'),
              (2, 'hn', 'HN', 'discovery', 't'),
              (3, 'x_person', '@a', 'trusted', 't');
            INSERT INTO stories (id, headline, category, score, lead_item_id, needs_text,
                                 first_seen_at, last_updated_at) VALUES
              (20, 'Three sources', 'other', 8, 200, 0, 't', 't');
            INSERT INTO items (id, source_id, url, canonical_url, title, fetched_at, status,
                               signal, story_id) VALUES
              (200, 1, 'a', 'a', 'a', 't', 'reviewed', 7, 20),
              (201, 2, 'b', 'b', 'b', 't', 'reviewed', NULL, 20),
              (202, 3, 'c', 'c', 'c', 't', 'reviewed', 3, 20);
            """
        )
        await apply_migrations(conn)
        async with conn.execute(
            "SELECT score, lead_item_id, needs_text FROM stories WHERE id = 20"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert tuple(row) == (7 + STORY_SOURCE_BONUS, 200, 0)  # lead unchanged: no rewrite
    finally:
        await conn.close()


# ---- Today, briefing and writer queries ignore X items ------------------------------------------


async def mixed_story(store: Store) -> tuple[int, int, int]:
    """A news story with an X item forced into it (as before this change); returns ids."""
    x = await x_source(store)
    lab = await add_source(store, "Lab blog")
    story = await store.create_story("Gemini 4 Argon", Category.MODEL_RELEASE, NOW)
    news = await add_item(store, lab.id, "Gemini 4 Argon launches", "https://example.com/g", NOW)
    post = await add_item(store, x.id, "Argon is great", "https://x.com/karpathy/status/9", NOW)
    await judged(store, news, 5)
    await judged(store, post, 9)
    await store.attach_item(news, story, NOW)
    await store.attach_item(post, story, NOW)
    await store.recompute_story_score(story)
    return story, news, post


async def test_today_and_stories_by_ids_ignore_x_items(store: Store) -> None:
    story, news, _ = await mixed_story(store)
    [s] = await store.today_stories(NOW, 0)
    assert [src.item_id for src in s.sources] == [news]
    assert [src.item_id for src in (await store.stories_by_ids([story]))[story].sources] == [news]


async def test_today_hides_x_only_stories_and_x_review_failures(store: Store) -> None:
    x = await x_source(store)
    story = await store.create_story("X only", Category.OTHER, NOW)
    post = await add_item(store, x.id, "A post", "https://x.com/karpathy/status/5", NOW)
    await store.attach_item(post, story, NOW)
    await store.mark_items([post], ItemStatus.REVIEW_FAILED)
    assert await store.today_stories(NOW, 0) == []
    assert await store.today_stories(NOW, 99) == []


async def test_writer_texts_ignore_x_items(store: Store) -> None:
    story, _, _ = await mixed_story(store)
    [w] = await store.stories_needing_text(10, include_why=False)
    assert w.story_id == story
    assert w.texts == ["Gemini 4 Argon launches\n\nGemini 4 Argon launches. Details follow."]


async def test_scores_and_briefing_ignore_x_items(store: Store, db_path: str) -> None:
    story, news, _ = await mixed_story(store)  # the X post has the higher signal (9 vs 5)
    with sqlite3.connect(db_path) as conn:
        lead, score = conn.execute(
            "SELECT lead_item_id, score FROM stories WHERE id = ?", (story,)
        ).fetchone()
    assert (lead, score) == (news, 5.0)  # no X lead, no extra-source bonus for X
    await store.save_story_text(StoryText(story_id=story, summary="S"))
    assert [b.url for b in await store.top_stories_for_briefing(NOW)] == ["https://example.com/g"]


async def test_unscored_items_ignore_x_posts(store: Store) -> None:
    x = await x_source(store)
    lab = await add_source(store, "Lab blog")
    post = await add_item(store, x.id, "A post", "https://x.com/karpathy/status/6", NOW)
    news = await add_item(store, lab.id, "News", "https://example.com/n", NOW)
    await store.mark_items([post, news], ItemStatus.QUEUED)
    assert [u.item_id for u in await store.unscored_items(NOW)] == [news]


# ---- Store.x_posts ------------------------------------------------------------------------------


async def test_x_posts(store: Store) -> None:
    x = await x_source(store, "karpathy")
    lab = await add_source(store, "Lab blog")
    [reviewed] = await store.add_candidates(
        [
            make_candidate(
                x.id,
                url="https://x.com/karpathy/status/1",
                title="Karpathy on X: nanochat",
                author="karpathy",
                snippet="A new repo.",
                published_at=NOW - timedelta(hours=1),
            )
        ],
        NOW - timedelta(hours=2),
    )
    await judged(store, reviewed, 6)
    [queued] = await store.add_candidates(
        [make_candidate(x.id, url="https://x.com/karpathy/status/2", title="Fresh post")],
        NOW - timedelta(minutes=30),
    )
    await store.mark_items([queued], ItemStatus.QUEUED)
    [junk] = await store.add_candidates(
        [make_candidate(x.id, url="https://x.com/karpathy/status/3", title="Giveaway")], NOW
    )
    await judged(store, junk, 1, junk=True)
    [dropped] = await store.add_candidates(
        [make_candidate(x.id, url="https://x.com/karpathy/status/4", title="Dropped")], NOW
    )
    await store.mark_items([dropped], ItemStatus.DROPPED)
    [old] = await store.add_candidates(
        [make_candidate(x.id, url="https://x.com/karpathy/status/5", title="Old")],
        NOW - timedelta(days=8),
    )
    await add_item(store, lab.id, "Not X", "https://example.com/n", NOW)

    posts = await store.x_posts(NOW)
    assert [p.item_id for p in posts] == [queued, reviewed]
    fresh, first = posts
    assert first == XPost(
        item_id=reviewed,
        handle="karpathy",
        source_name="@karpathy",
        url="https://x.com/karpathy/status/1",
        title="Karpathy on X: nanochat",
        text="A new repo.",
        published_at=NOW - timedelta(hours=1),
        fetched_at=NOW - timedelta(hours=2),
        signal=6,
        status=ItemStatus.REVIEWED,
        junk=False,
    )
    assert fresh.handle == "karpathy"  # no author: the source's handle, without the @
    assert (fresh.signal, fresh.status, fresh.junk) == (None, ItemStatus.QUEUED, None)
    assert old not in {p.item_id for p in await store.x_posts(NOW, days=7)}
    assert old in {p.item_id for p in await store.x_posts(NOW, days=9)}
    assert len(await store.x_posts(NOW, limit=1)) == 1
