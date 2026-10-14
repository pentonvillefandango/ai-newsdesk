# tests/test_cluster.py
from datetime import UTC, datetime

from ai_newsdesk.cluster import (
    Judged,
    attach,
    dedupe_links,
    shortlist,
    story_score,
    with_candidates,
)
from ai_newsdesk.models import Category, ItemStatus, StoryRef, Trust, Verdict
from ai_newsdesk.store.store import Store
from tests.pipeline_helpers import add_item, add_source, review_input

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def test_shortlist_needs_two_shared_tokens() -> None:
    stories = [
        StoryRef(id=1, headline="OpenAI launches GPT-5.5 for developers"),
        StoryRef(id=2, headline="Google updates Gemini app"),
        StoryRef(id=3, headline="OpenAI hires new CFO"),
    ]
    assert [s.id for s in shortlist("GPT-5.5 launches at OpenAI DevDay", stories)] == [1]


def test_shortlist_caps_at_five_most_shared_then_newest() -> None:
    stories = [StoryRef(id=i, headline=f"Anthropic Claude update {i}") for i in range(1, 9)]
    stories.append(StoryRef(id=20, headline="Anthropic Claude Haiku update"))
    got = [s.id for s in shortlist("Anthropic Claude Haiku update notes", stories)]
    assert got == [20, 8, 7, 6, 5]


def test_with_candidates_adds_linked_story() -> None:
    stories = [
        StoryRef(id=1, headline="Mistral funding round closes"),
        StoryRef(id=2, headline="Other"),
    ]
    inputs = [review_input(10, "Totally different words here")]
    out = with_candidates(inputs, stories, {10: 2})
    assert [s.id for s in out[0].story_candidates] == [2]
    assert inputs[0].story_candidates == []  # inputs are not mutated


def test_dedupe_links() -> None:
    recent = [(1, "OpenAI launches GPT-5.5 for developers", 50)]
    inputs = [
        review_input(2, "OpenAI Launches GPT-5.5 For Developers!"),
        review_input(3, "Unrelated"),
    ]
    assert dedupe_links(inputs, recent) == {2: 50}


def test_story_score() -> None:
    assert story_score([6, 8], [1, 2]) == 8.5
    assert story_score([6, None], [1, 1]) == 6.0
    assert story_score([None], [1]) == 0.0
    assert story_score([5, 5, 5], [1, 2, 3]) == 6.0


async def test_attach_same_story_new_story_low_signal_and_failed(store: Store) -> None:
    lab = await add_source(store, "Lab blog", trust=Trust.PRIMARY)
    person = await add_source(store, "Researcher", trust=Trust.TRUSTED)
    i1 = await add_item(
        store, lab.id, "OpenAI launches GPT-5.5 for developers", "https://example.com/a", NOW
    )
    i2 = await add_item(
        store, person.id, "Hands-on with GPT-5.5 from OpenAI", "https://example.com/b", NOW
    )
    i3 = await add_item(store, person.id, "Ten prompts you must try", "https://example.com/c", NOW)
    i4 = await add_item(store, lab.id, "Mystery lab teases agent", "https://example.com/d", NOW)
    story = await store.create_story(
        "OpenAI launches GPT-5.5 for developers", Category.MODEL_RELEASE, NOW
    )
    await store.save_verdict(
        Verdict(item_id=i1, signal=8, junk=False, category=Category.MODEL_RELEASE), 8
    )
    await store.attach_item(i1, story, NOW)
    ref = StoryRef(id=story, headline="OpenAI launches GPT-5.5 for developers")
    v2 = Verdict(
        item_id=i2, signal=6, junk=False, category=Category.MODEL_RELEASE, same_story_id=story
    )
    v3 = Verdict(item_id=i3, signal=2, junk=False, category=Category.OTHER)
    await store.save_verdict(v2, 6)
    await store.save_verdict(v3, 2)
    await store.mark_items([i4], ItemStatus.REVIEW_FAILED)
    judged = [
        Judged(
            review_input(
                i2, "Hands-on with GPT-5.5 from OpenAI", source_name="Researcher", candidates=[ref]
            ),
            v2,
            6,
        ),
        Judged(review_input(i3, "Ten prompts you must try", source_name="Researcher"), v3, 2),
        Judged(review_input(i4, "Mystery lab teases agent"), None, None),
    ]
    touched = await attach(store, judged, {}, show_threshold=4, at=NOW)
    stories = {s.id: s for s in await store.today_stories(NOW, 0)}
    assert story in touched
    assert sorted(src.item_id for src in stories[story].sources) == [i1, i2]
    assert stories[story].score == 8.5
    failed_story = next(s for s in stories.values() if any(src.item_id == i4 for src in s.sources))
    assert failed_story.category is Category.OTHER
    assert failed_story.headline == "Mystery lab teases agent"
    assert all(src.item_id != i3 for s in stories.values() for src in s.sources)


async def test_attach_ignores_unoffered_story_and_uses_dedupe_link(store: Store) -> None:
    lab = await add_source(store, "Lab blog")
    i1 = await add_item(store, lab.id, "Gemini 4 arrives", "https://example.com/g", NOW)
    i2 = await add_item(store, lab.id, "Claude gets memory", "https://example.com/c", NOW)
    linked = await store.create_story("Claude gets memory", Category.PRODUCT, NOW)
    v1 = Verdict(
        item_id=i1, signal=7, junk=False, category=Category.MODEL_RELEASE, same_story_id=linked
    )
    v2 = Verdict(item_id=i2, signal=7, junk=False, category=Category.PRODUCT)
    await store.save_verdict(v1, 7)
    await store.save_verdict(v2, 7)
    judged = [
        Judged(review_input(i1, "Gemini 4 arrives"), v1, 7),
        Judged(review_input(i2, "Claude gets memory"), v2, 7),
    ]
    await attach(store, judged, {i2: linked}, show_threshold=4, at=NOW)
    stories = {s.id: s for s in await store.today_stories(NOW, 0)}
    assert [src.item_id for src in stories[linked].sources] == [i2]
    gemini = next(s for s in stories.values() if s.headline == "Gemini 4 arrives")
    assert gemini.id != linked
    assert gemini.category is Category.MODEL_RELEASE


async def test_attach_merges_near_duplicates_in_one_pass(store: Store) -> None:
    lab = await add_source(store, "Lab blog")
    other = await add_source(store, "Other blog", trust=Trust.TRUSTED)
    i1 = await add_item(
        store, lab.id, "Meta releases Llama 5 weights", "https://example.com/1", NOW
    )
    i2 = await add_item(
        store, other.id, "Meta Releases Llama 5 Weights!", "https://example.com/2", NOW
    )
    vs = [
        Verdict(item_id=i, signal=7, junk=False, category=Category.MODEL_RELEASE) for i in (i1, i2)
    ]
    for v in vs:
        await store.save_verdict(v, 7)
    judged = [
        Judged(review_input(i1, "Meta releases Llama 5 weights"), vs[0], 7),
        Judged(
            review_input(i2, "Meta Releases Llama 5 Weights!", source_name="Other blog"), vs[1], 7
        ),
    ]
    touched = await attach(store, judged, {}, show_threshold=4, at=NOW)
    assert len(touched) == 1
    stories = await store.today_stories(NOW, 0)
    assert len(stories) == 1
    assert stories[0].score == 7.5


async def test_attach_keeps_an_already_attached_item_in_its_story(store: Store) -> None:
    lab = await add_source(store, "Lab blog")
    i1 = await add_item(store, lab.id, "Mystery lab teases agent", "https://example.com/m", NOW)
    i2 = await add_item(store, lab.id, "Gemini 4 arrives", "https://example.com/g", NOW)
    shown = await store.create_story("Mystery lab teases agent", Category.OTHER, NOW)
    other = await store.create_story("Gemini 4 arrives", Category.MODEL_RELEASE, NOW)
    await store.attach_item(i1, shown, NOW)
    await store.attach_item(i2, other, NOW)
    # The retry succeeds: the judge points at another (offered) story and the signal is low.
    v1 = Verdict(item_id=i1, signal=2, junk=False, category=Category.PRODUCT, same_story_id=other)
    await store.save_verdict(v1, 2)
    ref = StoryRef(id=other, headline="Gemini 4 arrives")
    judged = [Judged(review_input(i1, "Mystery lab teases agent", candidates=[ref]), v1, 2)]
    touched = await attach(store, judged, {}, show_threshold=4, at=NOW, current={i1: shown})
    assert touched == [shown]
    stories = {s.id: s for s in await store.today_stories(NOW, 0)}
    assert [src.item_id for src in stories[shown].sources] == [i1]
    assert stories[shown].score == 2.0
    assert [src.item_id for src in stories[other].sources] == [i2]
