"""Store: lifecycle, sources and items (Task 6, part 1)."""

from datetime import timedelta
from pathlib import Path

import pytest

import ai_newsdesk.store as store_package
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import (
    Category,
    ItemStatus,
    SourceKind,
    SourceResult,
    Trust,
    Verdict,
)
from ai_newsdesk.store.store import NO_TEXT, Store, basic_canonical_url
from tests.fakes import make_candidate, utc

NOW = utc(2026, 10, 14, 12, 0)


async def _source(store: Store, name: str = "Lab", trust: Trust = Trust.PRIMARY) -> int:
    return await store.upsert_source(SourceKind.FEED, name, {"url": f"https://{name}.test"}, trust)


def test_package_reexports_store() -> None:
    assert store_package.Store is Store


async def test_init_is_idempotent_and_needed(db_path: str) -> None:
    s = Store(db_path)
    with pytest.raises(RuntimeError, match="init"):
        await s.list_sources()
    await s.init()
    await s.init()
    assert await s.list_sources() == []
    await s.close()


async def test_seed_sources_only_when_empty(store: Store, tmp_path: Path) -> None:
    seed = tmp_path / "sources.yaml"
    seed.write_text(
        "sources:\n"
        "  - {kind: feed, name: Lab blog, trust: primary, config: {url: 'https://lab.test/rss'}}\n"
        "  - kind: x_person\n"
        "    name: A Researcher\n"
        "    trust: trusted\n"
        "    config: {handle: researcher}\n"
        "    enabled: false\n"
    )
    assert await store.seed_sources_if_empty(seed) == 2
    assert await store.seed_sources_if_empty(seed) == 0
    enabled = await store.list_sources()
    assert [s.name for s in enabled] == ["Lab blog"]
    everything = await store.list_sources(enabled_only=False)
    person = next(s for s in everything if s.kind is SourceKind.X_PERSON)
    assert person.config == {"handle": "researcher"}
    assert (person.trust, person.enabled, person.weight) == (Trust.TRUSTED, False, 1.0)


async def test_upsert_get_delete_source(store: Store) -> None:
    sid = await _source(store)
    assert await _source(store) == sid  # same (kind, name) updates in place
    same = await store.upsert_source(
        SourceKind.FEED, "Lab renamed", {"url": "u"}, Trust.TRUSTED, weight=1.5, source_id=sid
    )
    assert same == sid
    got = await store.get_source(sid)
    assert got is not None
    assert (got.name, got.trust, got.weight) == ("Lab renamed", Trust.TRUSTED, 1.5)
    # no weight given: an update by id or by (kind, name) keeps the learned weight
    await store.upsert_source(
        SourceKind.FEED, "Lab renamed", {"url": "u2"}, Trust.TRUSTED, enabled=False, source_id=sid
    )
    await store.upsert_source(SourceKind.FEED, "Lab renamed", {"url": "u3"}, Trust.TRUSTED)
    kept = await store.get_source(sid)
    assert kept is not None
    assert (kept.config, kept.enabled, kept.weight) == ({"url": "u3"}, True, 1.5)
    with pytest.raises(ValueError, match="weight"):
        await store.upsert_source(SourceKind.FEED, "x", {}, Trust.PRIMARY, weight=2.5)
    with pytest.raises(KeyError):
        await store.upsert_source(SourceKind.FEED, "y", {}, Trust.PRIMARY, source_id=999)
    assert await store.delete_source(sid) is True
    assert await store.delete_source(sid) is False
    assert await store.get_source(sid) is None


async def test_set_source_weight(store: Store) -> None:
    sid = await _source(store)
    await store.set_source_weight(sid, 0.3)
    got = await store.get_source(sid)
    assert got is not None and got.weight == 0.3
    for bad in (0.29, 2.01):
        with pytest.raises(ValueError, match="weight"):
            await store.set_source_weight(sid, bad)
    with pytest.raises(KeyError):
        await store.set_source_weight(999, 1.0)


async def test_record_source_result_and_stats(store: Store) -> None:
    sid = await _source(store)
    ok = SourceResult(source_id=sid, candidates=[make_candidate(sid)], duration_s=1.5)
    await store.record_source_result(ok, NOW)
    await store.add_candidates([make_candidate(sid)], NOW)
    await store.record_source_result(SourceResult(source_id=sid, error="timeout"), NOW)
    [stats] = await store.source_stats()
    assert stats.last_ok_at == NOW
    assert stats.last_run_at == NOW
    assert stats.last_error == "timeout"
    assert (stats.last_count, stats.item_count) == (0, 1)


def test_basic_canonical_url() -> None:
    assert (
        basic_canonical_url("HTTPS://WWW.Example.com:443/a/b/?q=1#frag")
        == "https://example.com/a/b?q=1"
    )
    assert basic_canonical_url("http://example.com:8080/") == "http://example.com:8080"
    url = "https://example.com/x?y=2"
    assert basic_canonical_url(basic_canonical_url(url)) == basic_canonical_url(url)


async def test_add_candidates_dedupes_on_canonical_url(store: Store) -> None:
    sid = await _source(store)
    first = await store.add_candidates(
        [
            make_candidate(sid, url="https://www.lab.test/post/"),
            make_candidate(sid, url="https://lab.test/post#comments"),  # same canonical
            make_candidate(sid, url="https://lab.test/other", hn_points=150),
            make_candidate(999, url="https://lab.test/orphan"),  # unknown source: skipped
        ],
        NOW,
    )
    assert len(first) == 2
    again = await store.add_candidates([make_candidate(sid, url="https://lab.test/post")], NOW)
    assert again == []
    titles = await store.recent_titles(24)
    assert [t[0] for t in titles] == first
    assert all(story is None for _, _, story in titles)


async def test_recent_titles_uses_store_clock(store: Store, clock: FixedClock) -> None:
    sid = await _source(store)
    [old] = await store.add_candidates([make_candidate(sid, url="https://a.test/1")], NOW)
    clock.advance(timedelta(hours=73))
    [new] = await store.add_candidates([make_candidate(sid, url="https://a.test/2")], clock.now())
    assert [t[0] for t in await store.recent_titles(72)] == [new]
    assert {t[0] for t in await store.recent_titles(100)} == {old, new}


async def test_items_for_enrichment(store: Store) -> None:
    feed = await _source(store)
    hn = await store.upsert_source(SourceKind.HN, "HN", {}, Trust.DISCOVERY)
    a, b = await store.add_candidates(
        [
            make_candidate(feed, url="https://a.test/1", snippet="Short."),
            make_candidate(hn, url="https://b.test/2", snippet=""),
        ],
        NOW,
    )
    assert await store.items_for_enrichment([b, 999, a]) == [
        (a, "https://a.test/1", SourceKind.FEED, "Short."),
        (b, "https://b.test/2", SourceKind.HN, ""),
    ]
    assert await store.items_for_enrichment([]) == []


async def test_set_item_fields_whitelist(store: Store) -> None:
    sid = await _source(store)
    [item] = await store.add_candidates([make_candidate(sid)], NOW)
    await store.set_item_fields(item, heuristic_score=2.5, status=ItemStatus.QUEUED)
    with pytest.raises(ValueError, match="not settable"):
        await store.set_item_fields(item, canonical_url="x")
    with pytest.raises(ValueError, match="at least one"):
        await store.set_item_fields(item)
    with pytest.raises(KeyError):
        await store.set_item_fields(999, status=ItemStatus.DROPPED)


async def test_queued_review_inputs_text_order_and_window(store: Store) -> None:
    sid = await _source(store, "Lab", Trust.PRIMARY)
    a, b, c, d = await store.add_candidates(
        [
            make_candidate(sid, url="https://a.test/1", title="A", snippet="snip A"),
            make_candidate(sid, url="https://a.test/2", title="B", snippet="snip B"),
            make_candidate(sid, url="https://a.test/3", title="C", snippet=""),
            make_candidate(sid, url="https://a.test/4", title="D", snippet="new"),
        ],
        NOW - timedelta(hours=1),
    )
    for item, score in ((a, 1.0), (b, 3.0), (c, 2.0)):
        await store.set_item_fields(item, status=ItemStatus.QUEUED, heuristic_score=score)
    await store.set_item_fields(a, body_excerpt="Full article body")
    inputs = await store.queued_review_inputs(10, NOW)
    assert [i.item_id for i in inputs] == [b, c, a]  # d is still `new`
    by_id = {i.item_id: i for i in inputs}
    assert by_id[a].text == "Full article body"
    assert by_id[b].text == "snip B"
    assert by_id[c].text == NO_TEXT
    assert by_id[b].source_name == "Lab"
    assert (by_id[b].source_kind, by_id[b].trust) == (SourceKind.FEED, Trust.PRIMARY)
    assert by_id[b].story_candidates == []
    assert [i.item_id for i in await store.queued_review_inputs(1, NOW)] == [b]
    assert await store.queued_review_inputs(10, NOW + timedelta(hours=48)) == []
    assert d not in by_id


async def test_review_failed_gets_one_retry(store: Store) -> None:
    sid = await _source(store)
    [item] = await store.add_candidates([make_candidate(sid)], NOW)
    await store.mark_items([item], ItemStatus.QUEUED)
    assert await store.mark_items([item], ItemStatus.REVIEW_FAILED) == 1
    assert [i.item_id for i in await store.queued_review_inputs(10, NOW)] == [item]
    await store.mark_items([item], ItemStatus.REVIEW_FAILED)
    assert await store.queued_review_inputs(10, NOW) == []
    assert await store.mark_items([], ItemStatus.DROPPED) == 0


async def test_expire_old_queued(store: Store) -> None:
    sid = await _source(store)
    [old] = await store.add_candidates([make_candidate(sid, url="https://a.test/o")], NOW)
    later = NOW + timedelta(hours=49)
    [fresh] = await store.add_candidates([make_candidate(sid, url="https://a.test/f")], later)
    await store.mark_items([old, fresh], ItemStatus.QUEUED)
    assert await store.expire_old_queued(later) == 1
    assert [i.item_id for i in await store.queued_review_inputs(10, later)] == [fresh]


async def test_save_verdict_and_reviewed_count(store: Store) -> None:
    sid = await _source(store)
    a, b = await store.add_candidates(
        [make_candidate(sid, url="https://a.test/1"), make_candidate(sid, url="https://a.test/2")],
        NOW,
    )
    verdict = Verdict(item_id=a, signal=7, junk=False, category=Category.RESEARCH)
    await store.save_verdict(verdict, 8.4)
    await store.mark_items([b], ItemStatus.REVIEW_FAILED)
    assert await store.reviewed_count_today(NOW, "Europe/London") == 2
    assert await store.reviewed_count_today(NOW + timedelta(days=1), "Europe/London") == 0
    with pytest.raises(KeyError):
        await store.save_verdict(verdict.model_copy(update={"item_id": 999}), 1.0)
