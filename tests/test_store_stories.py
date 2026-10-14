"""Store: stories, writing and briefings (Task 6, part 2)."""

from datetime import date, timedelta

import pytest

from ai_newsdesk.models import Category, ItemStatus, SourceKind, StoryText, Trust, Verdict
from ai_newsdesk.store.store import NO_TEXT, STORY_SOURCE_BONUS, Store
from tests.fakes import make_candidate, utc

NOW = utc(2026, 10, 14, 12, 0)


async def _judged_item(
    store: Store, source_id: int, url: str, signal: float, title: str = "T", snippet: str = "s"
) -> int:
    [item] = await store.add_candidates(
        [make_candidate(source_id, url=url, title=title, snippet=snippet)], NOW
    )
    verdict = Verdict(item_id=item, signal=int(signal), junk=False, category=Category.RESEARCH)
    await store.save_verdict(verdict, signal)
    return item


async def _two_sources(store: Store) -> tuple[int, int]:
    a = await store.upsert_source(SourceKind.FEED, "Lab", {"url": "u1"}, Trust.PRIMARY)
    b = await store.upsert_source(SourceKind.HN, "HN", {}, Trust.DISCOVERY)
    return a, b


async def test_create_open_and_attach(store: Store) -> None:
    lab, _ = await _two_sources(store)
    old = await store.create_story("Old", Category.OTHER, NOW - timedelta(hours=80))
    new = await store.create_story("New", Category.RESEARCH, NOW - timedelta(hours=1))
    assert [s.id for s in await store.open_stories(NOW)] == [new]
    assert [s.id for s in await store.open_stories(NOW, hours=100)] == [new, old]
    item = await _judged_item(store, lab, "https://a.test/1", 7)
    await store.attach_item(item, old, NOW)
    assert [s.id for s in await store.open_stories(NOW)] == [old, new]  # touched
    assert (await store.recent_titles(24))[0][2] == old
    with pytest.raises(KeyError):
        await store.attach_item(item, 999, NOW)
    with pytest.raises(KeyError):
        await store.attach_item(999, new, NOW)


async def test_score_lead_and_text_flag(store: Store) -> None:
    lab, hn = await _two_sources(store)
    story = await store.create_story("Model X", Category.MODEL_RELEASE, NOW)
    first = await _judged_item(store, lab, "https://a.test/1", 6, title="Lab post", snippet="")
    await store.set_item_fields(first, body_excerpt="Lab body")
    await store.attach_item(first, story, NOW)
    assert await store.recompute_story_score(story) == 6.0
    [needs] = await store.stories_needing_text(10, include_why=True)
    assert needs.story_id == story
    assert needs.texts == ["Lab post\n\nLab body"]
    assert needs.include_why is True

    await store.save_story_text(StoryText(story_id=story, summary="S1", why_it_matters="W1"))
    assert await store.stories_needing_text(10, include_why=True) == []

    # a second source with a lower signal: bonus only, lead unchanged, no rewrite
    second = await _judged_item(store, hn, "https://b.test/1", 5, title="HN thread", snippet="")
    await store.attach_item(second, story, NOW)
    assert await store.recompute_story_score(story) == 6.0 + STORY_SOURCE_BONUS
    assert await store.stories_needing_text(10, include_why=False) == []

    # a better item becomes lead: story needs text again, best two texts in signal order
    third = await _judged_item(store, hn, "https://b.test/2", 9, title="Better", snippet="Snip")
    await store.attach_item(third, story, NOW)
    assert await store.recompute_story_score(story) == 9.0 + STORY_SOURCE_BONUS
    [again] = await store.stories_needing_text(10, include_why=False)
    assert again.texts == ["Better\n\nSnip", "Lab post\n\nLab body"]
    with pytest.raises(KeyError):
        await store.recompute_story_score(999)


async def test_text_failures_get_one_retry(store: Store) -> None:
    lab, _ = await _two_sources(store)
    story = await store.create_story("H", Category.OTHER, NOW)
    item = await _judged_item(store, lab, "https://a.test/1", 5, snippet="")
    await store.attach_item(item, story, NOW)
    await store.recompute_story_score(story)
    [w] = await store.stories_needing_text(10, include_why=True)
    assert w.texts == [f"T\n\n{NO_TEXT}"]
    await store.mark_text_failed([story])
    assert len(await store.stories_needing_text(10, include_why=True)) == 1
    await store.mark_text_failed([story])
    assert await store.stories_needing_text(10, include_why=True) == []


async def test_save_story_text_keeps_why_when_none(store: Store) -> None:
    lab, _ = await _two_sources(store)
    story = await store.create_story("H", Category.OTHER, NOW)
    item = await _judged_item(store, lab, "https://a.test/1", 8)
    await store.attach_item(item, story, NOW)
    await store.recompute_story_score(story)
    await store.save_story_text(StoryText(story_id=story, summary="S1", why_it_matters="W1"))
    await store.save_story_text(StoryText(story_id=story, summary="S2", why_it_matters=None))
    [s] = await store.today_stories(NOW, threshold=0)
    assert (s.summary, s.why_it_matters) == ("S2", "W1")
    with pytest.raises(KeyError):
        await store.save_story_text(StoryText(story_id=999, summary="x"))


async def test_today_stories_threshold_window_sources_feedback(store: Store) -> None:
    lab, hn = await _two_sources(store)
    strong = await store.create_story("Strong", Category.MODEL_RELEASE, NOW - timedelta(hours=2))
    weak = await store.create_story("Weak", Category.OPINION, NOW - timedelta(hours=2))
    stale = await store.create_story("Stale", Category.RESEARCH, NOW - timedelta(hours=30))
    for story, signal, url in ((strong, 8, "s1"), (weak, 3, "w1"), (stale, 9, "x1")):
        item = await _judged_item(store, lab, f"https://a.test/{url}", signal)
        await store.attach_item(item, story, NOW - timedelta(hours=2 if story != stale else 30))
    hn_item = await _judged_item(store, hn, "https://b.test/s2", 7, title="HN take")
    await store.attach_item(hn_item, strong, NOW - timedelta(hours=1))
    for story in (strong, weak, stale):
        await store.recompute_story_score(story)
    await store.set_feedback(strong, 1)

    stories = await store.today_stories(NOW, threshold=4)
    assert [s.id for s in stories] == [strong]
    top = stories[0]
    assert top.score == 8.0 + STORY_SOURCE_BONUS
    assert top.category is Category.MODEL_RELEASE
    assert top.feedback == 1
    assert top.last_updated_at == NOW - timedelta(hours=1)
    assert [(src.source_name, src.source_kind) for src in top.sources] == [
        ("Lab", SourceKind.FEED),
        ("HN", SourceKind.HN),
    ]
    assert top.sources[1].title == "HN take"
    assert [s.id for s in await store.today_stories(NOW, threshold=0)] == [strong, weak]


async def test_today_stories_shows_review_failed_whatever_score(store: Store) -> None:
    lab, _ = await _two_sources(store)
    story = await store.create_story("Mystery lab teases agent", Category.OTHER, NOW)
    stale = await store.create_story("Old mystery", Category.OTHER, NOW - timedelta(hours=30))
    for story_id, url, at in (
        (story, "https://a.test/m", NOW),
        (stale, "https://a.test/o", NOW - timedelta(hours=30)),
    ):
        [item] = await store.add_candidates([make_candidate(lab, url=url)], at)
        await store.mark_items([item], ItemStatus.REVIEW_FAILED)
        await store.attach_item(item, story_id, at)
        assert await store.recompute_story_score(story_id) == 0.0
    [shown] = await store.today_stories(NOW, threshold=4)
    assert shown.id == story
    [failed] = [src.item_id for src in shown.sources]
    # once the item gets a verdict below the threshold, the story is hidden like any other
    verdict = Verdict(item_id=failed, signal=2, junk=False, category=Category.OTHER)
    await store.save_verdict(verdict, 2.0)
    await store.recompute_story_score(story)
    assert await store.today_stories(NOW, threshold=4) == []


async def test_story_features_use_lead_item(store: Store) -> None:
    lab, hn = await _two_sources(store)
    judged = await store.create_story("Judged", Category.RESEARCH, NOW)
    weak = await _judged_item(store, lab, "https://a.test/1", 5)
    lead = await _judged_item(store, hn, "https://b.test/1", 8)
    for item in (weak, lead):
        await store.attach_item(item, judged, NOW)  # no rescore needed: the lead is computed
    unjudged = await store.create_story("Unjudged", Category.POLICY, NOW)
    [failed] = await store.add_candidates([make_candidate(lab, url="https://a.test/2")], NOW)
    await store.mark_items([failed], ItemStatus.REVIEW_FAILED)
    await store.attach_item(failed, unjudged, NOW)
    empty = await store.create_story("No items", Category.OTHER, NOW)
    assert await store.story_features([judged, unjudged, empty, 999]) == {
        judged: {
            "signal": 8,
            "junk": False,
            "category": "research",
            "trust": "discovery",
            "source_kind": "hn",
        },
        unjudged: {
            "signal": None,
            "junk": False,
            "category": "policy",
            "trust": "primary",
            "source_kind": "feed",
        },
    }
    assert await store.story_features([]) == {}


async def test_top_stories_for_briefing(store: Store) -> None:
    lab, _ = await _two_sources(store)
    written = await store.create_story("Written", Category.POLICY, NOW)
    unwritten = await store.create_story("Unwritten", Category.POLICY, NOW)
    for story, url in ((written, "https://a.test/w"), (unwritten, "https://a.test/u")):
        item = await _judged_item(store, lab, url, 7)
        await store.attach_item(item, story, NOW)
        await store.recompute_story_score(story)
    await store.save_story_text(StoryText(story_id=written, summary="Sum"))
    [b] = await store.top_stories_for_briefing(NOW)
    assert (b.story_id, b.headline, b.category, b.summary) == (
        written,
        "Written",
        Category.POLICY,
        "Sum",
    )
    assert b.url == "https://a.test/w"
    assert await store.top_stories_for_briefing(NOW + timedelta(hours=25)) == []


async def test_briefings_save_replace_and_list(store: Store) -> None:
    assert await store.latest_briefing() is None
    await store.save_briefing(date(2026, 10, 13), "claude-haiku-5-5", "Mon", [1], NOW)
    await store.save_briefing(date(2026, 10, 14), "claude-haiku-5-5", "Tue v1", [1, 2], NOW)
    await store.save_briefing(date(2026, 10, 14), "claude-haiku-5-5", "Tue v2", [2], NOW)
    got = await store.get_briefing(date(2026, 10, 14))
    assert got is not None
    assert (got.body, got.story_ids, got.created_at) == ("Tue v2", [2], NOW)
    latest = await store.latest_briefing()
    assert latest is not None and latest.date == date(2026, 10, 14)
    assert await store.briefing_dates() == [date(2026, 10, 14), date(2026, 10, 13)]
    assert await store.get_briefing(date(2026, 1, 1)) is None
