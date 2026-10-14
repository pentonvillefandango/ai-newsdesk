"""Store: spend, runs, settings, feedback and backup (Task 6, part 3)."""

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import Category, Purpose, SourceKind, Trust, Usage, Verdict
from ai_newsdesk.store.store import Store
from tests.fakes import make_candidate, utc

LONDON = "Europe/London"


def _usage(cost_tokens: int = 1000) -> Usage:
    return Usage(
        provider="anthropic",
        model="claude-haiku-5-5",
        purpose=Purpose.REVIEW,
        input_tokens=cost_tokens,
        output_tokens=100,
    )


async def test_month_spend_uses_london_month(store: Store) -> None:
    # 1 Oct 00:00 BST is 30 Sep 23:00 UTC; 1 Nov 00:00 GMT is 1 Nov 00:00 UTC.
    await store.add_spend(_usage(), 1.00, utc(2026, 9, 30, 22, 30))  # September in London
    await store.add_spend(_usage(), 2.00, utc(2026, 9, 30, 23, 30))  # October in London
    await store.add_spend(_usage(), 0.25, utc(2026, 10, 31, 23, 59))  # October (GMT)
    await store.add_spend(_usage(), 4.00, utc(2026, 11, 1, 0, 0))  # November
    oct_now = utc(2026, 10, 14, 12)
    assert await store.month_spend(oct_now, LONDON) == pytest.approx(2.25)
    assert await store.month_spend(utc(2026, 9, 15), LONDON) == pytest.approx(1.00)
    assert await store.month_spend(utc(2026, 11, 2), LONDON) == pytest.approx(4.00)
    # in UTC the 23:30 spend belongs to September
    assert await store.month_spend(oct_now, "UTC") == pytest.approx(0.25)


async def test_month_spend_december_rollover(store: Store) -> None:
    await store.add_spend(_usage(), 0.5, utc(2026, 12, 31, 23, 0))
    await store.add_spend(_usage(), 0.7, utc(2027, 1, 1, 0, 0))
    assert await store.month_spend(utc(2026, 12, 20), LONDON) == pytest.approx(0.5)
    assert await store.month_spend(utc(2027, 1, 5), LONDON) == pytest.approx(0.7)


async def test_spend_by_day_fills_every_day(store: Store) -> None:
    await store.add_spend(_usage(), 0.10, utc(2026, 10, 1, 9))
    await store.add_spend(_usage(), 0.05, utc(2026, 10, 2, 23, 30))  # 3 Oct 00:30 BST
    await store.add_spend(_usage(), 0.02, utc(2026, 10, 3, 8))
    days = await store.spend_by_day(utc(2026, 10, 3, 12), LONDON)
    assert days == [("2026-10-01", 0.10), ("2026-10-02", 0.0), ("2026-10-03", 0.07)]


async def test_runs(store: Store) -> None:
    t0 = utc(2026, 10, 14, 6)
    first = await store.start_run("schedule", t0)
    second = await store.start_run("manual", t0 + timedelta(hours=1))
    await store.finish_run(first, t0 + timedelta(minutes=3), {"collected": 40}, 0.012, None)
    runs = await store.last_runs(5)
    assert [r.id for r in runs] == [second, first]
    assert runs[0].finished_at is None
    assert (runs[1].counts, runs[1].cost_usd, runs[1].error) == ({"collected": 40}, 0.012, None)
    assert runs[1].trigger == "schedule"
    assert [r.id for r in await store.last_runs(1)] == [second]
    with pytest.raises(KeyError):
        await store.finish_run(999, t0, {}, 0.0, "x")


async def test_settings_json_roundtrip(store: Store) -> None:
    assert await store.get_setting("runs_per_day") is None
    await store.set_setting("runs_per_day", 6)
    await store.set_setting("blob", {"a": [1, 2], "b": None})
    assert await store.get_setting("runs_per_day") == 6
    assert await store.get_setting("blob") == {"a": [1, 2], "b": None}
    await store.set_setting("runs_per_day", None)
    assert await store.get_setting("runs_per_day") is None


async def test_feedback_tally_and_examples(store: Store, clock: FixedClock) -> None:
    now = utc(2026, 10, 14, 12)
    lab = await store.upsert_source(SourceKind.FEED, "Lab", {}, Trust.PRIMARY)
    hn = await store.upsert_source(SourceKind.HN, "HN", {}, Trust.DISCOVERY)
    liked = await store.create_story("Liked", Category.RESEARCH, now)
    disliked = await store.create_story("Disliked", Category.OPINION, now)
    for story, src, url, signal in (
        (liked, lab, "https://a.test/1", 8),
        (liked, lab, "https://a.test/2", 7),  # same source twice: still one vote
        (liked, hn, "https://b.test/1", 6),
        (disliked, hn, "https://b.test/2", 4),
    ):
        [item] = await store.add_candidates([make_candidate(src, url=url)], now)
        verdict = Verdict(item_id=item, signal=signal, junk=False, category=Category.RESEARCH)
        await store.save_verdict(verdict, float(signal))
        await store.attach_item(item, story, now)
    unrated = await store.create_story("Unrated", Category.OTHER, now)
    [item] = await store.add_candidates([make_candidate(lab, url="https://a.test/3")], now)
    await store.attach_item(item, unrated, now)
    await store.set_feedback(liked, 1)
    clock.advance(timedelta(minutes=5))  # the thumb on `disliked` is the newer one
    await store.set_feedback(disliked, -1)
    assert await store.source_feedback(30, now) == {lab: (1, 0), hn: (1, 1)}
    assert await store.source_feedback(30, now + timedelta(days=31)) == {}
    # one example per rated story: its lead item's features, newest thumb first
    assert await store.feedback_examples() == [
        (
            {
                "signal": 4,
                "junk": False,
                "category": "research",
                "trust": "discovery",
                "source_kind": "hn",
            },
            -1,
        ),
        (
            {
                "signal": 8,
                "junk": False,
                "category": "research",
                "trust": "primary",
                "source_kind": "feed",
            },
            1,
        ),
    ]
    with pytest.raises(ValueError, match="-1, 0 or 1"):
        await store.set_feedback(liked, 2)
    with pytest.raises(KeyError):
        await store.set_feedback(999, 1)
    await store.set_feedback(liked, 0)
    assert await store.source_feedback(30, now) == {hn: (0, 1)}
    assert [label for _, label in await store.feedback_examples()] == [-1]


async def test_concurrent_writes_are_serialised(store: Store) -> None:
    await asyncio.gather(*(store.set_setting(f"k{i}", i) for i in range(50)))
    values = await asyncio.gather(*(store.get_setting(f"k{i}") for i in range(50)))
    assert values == list(range(50))


async def test_backup_is_a_complete_copy(store: Store, tmp_path: Path) -> None:
    await store.set_setting("marker", "present")
    dest = tmp_path / "backups" / "copy.sqlite"
    await store.backup(dest)
    with sqlite3.connect(dest) as copy:
        row = copy.execute("SELECT value_json FROM settings WHERE key = 'marker'").fetchone()
    assert row == ('"present"',)
    with pytest.raises(FileExistsError):
        await store.backup(dest)
