"""Compare storage (multi-provider spec 4): migration 0005, runs, results, labels, writer
preferences and the comparison samples."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from ai_newsdesk.models import (
    Candidate,
    Category,
    ItemStatus,
    SourceKind,
    StoryText,
    Trust,
    Verdict,
)
from ai_newsdesk.store import Store
from ai_newsdesk.store.db import apply_migrations, connect
from ai_newsdesk.store.records import CompareModel, CompareResult
from tests.web import seed

NOW = datetime(2026, 10, 14, 8, 30, tzinfo=UTC)
MODELS = [
    CompareModel(provider="anthropic", model="claude-haiku-5-5"),
    CompareModel(provider="openrouter", model="google/gemini-3.8-flash"),
]


async def test_migration_adds_the_compare_tables(db_path: str) -> None:
    conn = await connect(db_path)
    try:
        assert await apply_migrations(conn) == [1, 2, 3, 4, 5]
        assert await apply_migrations(conn) == []
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            names = {row[0] for row in await cur.fetchall()}
        assert {"compare_runs", "compare_results", "labels", "writer_prefs"} <= names
        await conn.execute(
            "INSERT INTO compare_runs (job, models_json, sample_ids_json, cap_usd, status,"
            " started_at) VALUES ('judge', '[]', '[]', 0.25, 'running', 'x')"
        )
        for bad in (
            "INSERT INTO compare_runs (job, models_json, sample_ids_json, cap_usd, status,"
            " started_at) VALUES ('briefing', '[]', '[]', 0.25, 'running', 'x')",
            "INSERT INTO compare_runs (job, models_json, sample_ids_json, cap_usd, status,"
            " started_at) VALUES ('judge', '[]', '[]', 0.25, 'lost', 'x')",
            "INSERT INTO labels (item_id, label, set_at) VALUES (1, 'maybe', 'x')",
        ):
            with pytest.raises(aiosqlite.IntegrityError):
                await conn.execute(bad)
    finally:
        await conn.close()


def test_model_key_names_provider_and_model() -> None:
    assert MODELS[1].key == "openrouter:google/gemini-3.8-flash"


async def test_a_run_is_created_finished_and_listed_newest_first(store: Store) -> None:
    first = await store.create_compare_run("judge", MODELS, [3, 1, 2], 0.25, NOW)
    second = await store.create_compare_run("writer", MODELS[:1], [9], 0.5, NOW)
    third = await store.create_compare_run("judge", MODELS, [], 1.0, NOW + timedelta(minutes=5))
    run = await store.compare_run(first)
    assert run is not None
    assert (run.job, run.models, run.sample_ids, run.cap_usd) == ("judge", MODELS, [3, 1, 2], 0.25)
    assert (run.status, run.finished_at, run.cost_usd, run.error) == ("running", None, 0.0, None)

    await store.finish_compare_run(first, "capped", NOW + timedelta(minutes=1), 0.2, None)
    run = await store.compare_run(first)
    assert run is not None
    assert (run.status, run.cost_usd) == ("capped", 0.2)
    assert run.finished_at == NOW + timedelta(minutes=1)

    assert [r.id for r in await store.compare_runs()] == [third, second, first]
    assert [r.id for r in await store.compare_runs(limit=1)] == [third]
    assert await store.compare_run(999) is None


async def test_runs_left_running_are_failed_at_start_up(store: Store) -> None:
    left = await store.create_compare_run("judge", MODELS, [], 0.25, NOW)
    done = await store.create_compare_run("judge", MODELS, [], 0.25, NOW)
    await store.finish_compare_run(done, "done", NOW, 0.1, None)
    assert await store.fail_running_compare_runs(NOW, "The app restarted during the run.") == 1
    run = await store.compare_run(left)
    assert run is not None
    assert (run.status, run.error) == ("failed", "The app restarted during the run.")
    kept = await store.compare_run(done)
    assert kept is not None and kept.status == "done"


async def test_results_round_trip(store: Store) -> None:
    run_id = await store.create_compare_run("judge", MODELS, [1, 2], 0.25, NOW)
    rows = [
        CompareResult(
            model_key=MODELS[0].key,
            subject_id=1,
            output={"item_id": 1, "signal": 7},
            ok=True,
            latency_ms=900,
            cost_usd=0.001,
        ),
        CompareResult(model_key=MODELS[0].key, subject_id=2, ok=False, latency_ms=900),
        CompareResult(model_key=MODELS[1].key, subject_id=1, ok=False),  # not run
    ]
    await store.add_compare_results(run_id, rows)
    assert await store.compare_results(run_id) == rows
    assert rows[2].ran is False and rows[1].ran is True


async def test_labels_are_one_per_item_and_can_be_cleared(store: Store) -> None:
    src = await seed.add_source(store, name="Lab blog")
    a = await seed.add_item(store, source_id=src, url="https://lab.example/a", title="A", at=NOW)
    b = await seed.add_item(store, source_id=src, url="https://lab.example/b", title="B", at=NOW)
    await store.set_label(a, "junk", NOW)
    await store.set_label(a, "important", NOW)
    await store.set_label(b, "show", NOW)
    assert await store.labels([a, b, 99]) == {a: "important", b: "show"}
    await store.set_label(b, None, NOW)
    assert await store.labels([a, b]) == {a: "important"}
    with pytest.raises(ValueError):
        await store.set_label(a, "maybe", NOW)  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        await store.set_label(99, "show", NOW)


async def test_writer_preferences_one_per_story(store: Store) -> None:
    run_id = await store.create_compare_run("writer", MODELS, [5, 6], 0.25, NOW)
    await store.set_writer_pref(run_id, 5, MODELS[0].key, NOW)
    await store.set_writer_pref(run_id, 5, MODELS[1].key, NOW)
    await store.set_writer_pref(run_id, 6, MODELS[1].key, NOW)
    assert await store.writer_prefs(run_id) == {5: MODELS[1].key, 6: MODELS[1].key}


async def _x_item(store: Store, n: int, at: datetime) -> int:
    src = await seed.add_source(
        store,
        name=f"@person{n}",
        kind=SourceKind.X_PERSON,
        trust=Trust.TRUSTED,
        config={"handle": f"person{n}"},
    )
    [item_id] = await store.add_candidates(
        [Candidate(source_id=src, url=f"https://x.com/p/status/{n}", title=f"Post {n}")], at
    )
    await store.mark_items([item_id], ItemStatus.QUEUED)
    return item_id


async def test_judge_sample_is_recent_reviewed_or_queued_news_newest_first(store: Store) -> None:
    src = await seed.add_source(store, name="Lab blog")

    async def item(slug: str, hours: float, status: ItemStatus | None) -> int:
        return await seed.add_item(
            store,
            source_id=src,
            url=f"https://lab.example/{slug}",
            title=slug,
            at=NOW - timedelta(hours=hours),
            status=status,
        )

    queued = await item("queued", 1, ItemStatus.QUEUED)
    reviewed = await item("reviewed", 2, None)
    await store.save_verdict(
        Verdict(item_id=reviewed, signal=6, junk=False, category=Category.RESEARCH), 6.0
    )
    await store.set_item_fields(reviewed, body_excerpt="The full article body.")
    await item("dropped", 3, ItemStatus.DROPPED)
    await item("failed", 3, ItemStatus.REVIEW_FAILED)
    await item("old", 24 * 7 + 1, ItemStatus.QUEUED)
    await _x_item(store, 1, NOW - timedelta(hours=1))

    sample = await store.compare_judge_inputs(NOW, 10)
    assert [s.item_id for s in sample] == [queued, reviewed]
    assert sample[1].text == "The full article body."  # what the production judge reads
    assert sample[0].text == "(no text, title only)"
    assert [s.item_id for s in await store.compare_judge_inputs(NOW, 1)] == [queued]


async def test_writer_sample_is_top_stories_of_48_hours(store: Store) -> None:
    src = await seed.add_source(store, name="Lab blog")

    async def story(headline: str, hours: float, signal: int) -> int:
        slug = "-".join(headline.lower().split())
        return await seed.add_story(
            store,
            at=NOW - timedelta(hours=hours),
            headline=headline,
            sources=[(src, f"https://lab.example/{slug}", headline)],
            signal=signal,
        )

    low = await story("Low story", 2, 5)
    high = await story("High story", 30, 9)
    await story("Old story", 49, 10)
    sample = await store.compare_writer_inputs(NOW, 5, include_why=True)
    assert [w.story_id for w in sample] == [high, low]
    assert sample[0].texts == ["High story\n\n(no text, title only)"]
    assert sample[0].include_why is True
    assert [w.story_id for w in await store.compare_writer_inputs(NOW, 1, False)] == [high]


async def test_compare_items_carry_production_verdict_thumb_and_label(store: Store) -> None:
    src = await seed.add_source(store, name="Lab blog")
    await store.set_source_weight(src, 1.5)
    story = await seed.add_story(
        store,
        at=NOW,
        headline="Story",
        sources=[(src, "https://lab.example/s", "Story")],
        signal=7,
    )
    await store.set_feedback(story, -1)
    [item] = [s.item_id for s in (await store.stories_by_ids([story]))[story].sources]
    plain = await seed.add_item(
        store, source_id=src, url="https://lab.example/p", title="Plain", at=NOW
    )
    await store.set_label(plain, "junk", NOW)
    got = await store.compare_items([item, plain, 999])
    assert set(got) == {item, plain}
    verdict = got[item].verdict
    assert verdict is not None and verdict.signal == 7
    assert (got[item].feedback, got[item].weight, got[item].label) == (-1, 1.5, None)
    assert got[item].url == "https://lab.example/s"
    assert (got[plain].verdict, got[plain].feedback, got[plain].label) == (None, 0, "junk")


async def test_nothing_in_items_or_stories_changes_through_compare_storage(store: Store) -> None:
    src = await seed.add_source(store, name="Lab blog")
    story = await seed.add_story(
        store, at=NOW, headline="Story", sources=[(src, "https://lab.example/s", "Story")]
    )
    await store.save_story_text(StoryText(story_id=story, summary="Kept."))

    async def snapshot() -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
        items = [tuple(r) for r in await store._all("SELECT * FROM items ORDER BY id")]
        stories = [tuple(r) for r in await store._all("SELECT * FROM stories ORDER BY id")]
        return items, stories

    before = await snapshot()
    run_id = await store.create_compare_run("writer", MODELS, [story], 0.25, NOW)
    await store.add_compare_results(
        run_id, [CompareResult(model_key=MODELS[0].key, subject_id=story, ok=True)]
    )
    await store.set_writer_pref(run_id, story, MODELS[0].key, NOW)
    await store.compare_writer_inputs(NOW, 5, True)
    await store.compare_judge_inputs(NOW, 5)
    await store.finish_compare_run(run_id, "done", NOW, 0.0, None)
    assert await snapshot() == before
