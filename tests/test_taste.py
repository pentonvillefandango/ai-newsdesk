from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import Category, SourceConfig, SourceKind, Story, Trust, Verdict
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.store.store import Store
from ai_newsdesk.taste import (
    FEATURE_KEYS,
    FEATURE_NAMES,
    MAX_ADJUST,
    TASTE_KEY,
    TasteModel,
    adjustment,
    apply,
    featurize,
    fit,
    load_model,
    nightly,
    rank_today,
    refit,
)
from tests.feedback_stubs import StubStore
from tests.pipeline_helpers import add_item, add_source

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def feats(
    category: str, trust: str, signal: int, junk: bool = False, source_kind: str = "feed"
) -> dict[str, Any]:
    return {
        "signal": signal,
        "junk": junk,
        "category": category,
        "trust": trust,
        "source_kind": source_kind,
    }


LIKED = feats("research", "trusted", 7)
DISLIKED = feats("opinion", "discovery", 5)
LIKES = [(feats("research", "trusted", 6 + i % 3), 1) for i in range(20)]
DISLIKES = [(feats("opinion", "discovery", 5 + i % 3), -1) for i in range(20)]
EXAMPLES = LIKES + DISLIKES


def story(sid: int, category: Category, score: float, minutes_ago: int = 0) -> Story:
    at = NOW - timedelta(minutes=minutes_ago)
    return Story(
        id=sid,
        headline=f"Story {sid}",
        category=category,
        score=score,
        summary=None,
        why_it_matters=None,
        first_seen_at=at,
        last_updated_at=at,
        feedback=0,
    )


def test_featurize_layout() -> None:
    x = featurize(feats("research", "trusted", 7, junk=True, source_kind="x_person"))
    assert len(x) == len(FEATURE_NAMES) == 18
    assert x[FEATURE_NAMES.index("signal")] == 0.7
    assert x[FEATURE_NAMES.index("junk")] == 1.0
    assert x[FEATURE_NAMES.index("cat_research")] == 1.0
    assert x[FEATURE_NAMES.index("trust_trusted")] == 1.0
    assert x[FEATURE_NAMES.index("kind_x_person")] == 1.0
    assert sum(x) == 0.7 + 1.0 + 1.0 + 1.0 + 1.0


def test_featurize_tolerates_missing_and_unknown_values() -> None:
    unknown = {"signal": None, "category": "astrology", "trust": "???", "source_kind": "fax"}
    assert featurize(unknown) == [0.0] * 18


async def test_store_feature_dicts_match_featurize(store: Store) -> None:
    lab = await add_source(store, "Lab blog", trust=Trust.TRUSTED)
    item = await add_item(store, lab.id, "Lab paper on agents", "https://example.com/p", NOW)
    await store.save_verdict(
        Verdict(item_id=item, signal=7, junk=False, category=Category.RESEARCH), 7
    )
    story_id = await store.create_story("Lab paper on agents", Category.RESEARCH, NOW)
    await store.attach_item(item, story_id, NOW)
    await store.recompute_story_score(story_id)
    await store.set_feedback(story_id, 1)
    [(features, label)] = await store.feedback_examples()
    assert label == 1
    assert set(features) == set(FEATURE_KEYS)
    assert features == feats("research", "trusted", 7)
    assert (await store.story_features([story_id]))[story_id] == features
    assert featurize(features)[FEATURE_NAMES.index("kind_feed")] == 1.0


def test_fit_needs_thirty_labelled_examples_of_both_classes() -> None:
    assert fit(EXAMPLES[:29]) is None
    assert fit(LIKES + LIKES) is None
    assert fit(EXAMPLES[:29] + [(LIKED, 0)] * 10) is None  # cleared thumbs don't count
    assert fit(EXAMPLES[:30]) is not None


def test_learns_synthetic_preference_within_bounds() -> None:
    model = fit(EXAMPLES)
    assert model is not None
    assert model.n_examples == 40
    liked, disliked = adjustment(model, LIKED), adjustment(model, DISLIKED)
    assert 1.0 < liked <= MAX_ADJUST
    assert -MAX_ADJUST <= disliked < -1.0


def test_fit_is_deterministic() -> None:
    assert fit(EXAMPLES) == fit(EXAMPLES)


def test_adjustment_without_model_is_zero_and_always_bounded() -> None:
    assert adjustment(None, LIKED) == 0.0
    huge = TasteModel(weights=(100.0,) * 18, bias=100.0, n_examples=30)
    tiny = TasteModel(weights=(-100.0,) * 18, bias=-100.0, n_examples=30)
    assert adjustment(huge, LIKED) == 2.0
    assert adjustment(tiny, LIKED) == -2.0


def test_apply_reorders_by_adjusted_score() -> None:
    model = fit(EXAMPLES)
    stories = [
        story(1, Category.OPINION, 5.0),
        story(2, Category.RESEARCH, 4.5),
        story(3, Category.PRODUCT, 4.8),
    ]
    ranked = apply(stories, {1: DISLIKED, 2: LIKED}, model)
    assert [s.id for s in ranked] == [2, 3, 1]
    assert ranked[1].score == 4.8
    assert stories[0].score == 5.0  # inputs are not mutated


def test_apply_without_model_is_unchanged() -> None:
    stories = [story(1, Category.OPINION, 3.0), story(2, Category.RESEARCH, 9.0)]
    assert apply(stories, {1: DISLIKED}, None) == stories


def test_model_json_round_trip_and_validation() -> None:
    model = fit(EXAMPLES)
    assert model is not None
    assert TasteModel.from_json(model.to_json()) == model
    assert TasteModel.from_json(None) is None
    assert (
        TasteModel.from_json({"weights": [1.0], "bias": 0.0, "features": list(FEATURE_NAMES)})
        is None
    )
    stale = {**model.to_json(), "features": ["signal"]}
    assert TasteModel.from_json(stale) is None


async def test_refit_and_load_model() -> None:
    stub = StubStore(examples=EXAMPLES)
    model = await refit(cast(Store, stub))
    assert model is not None
    assert stub.settings[TASTE_KEY] == model.to_json()
    assert await load_model(cast(Store, stub)) == model
    small = StubStore(examples=EXAMPLES[:10])
    assert await refit(cast(Store, small)) is None
    assert TASTE_KEY in small.settings and small.settings[TASTE_KEY] is None
    assert await load_model(cast(Store, small)) is None


async def test_rank_today_uses_stored_model_and_features() -> None:
    stories = [story(1, Category.OPINION, 5.0), story(2, Category.RESEARCH, 4.5)]
    empty = StubStore(features={1: DISLIKED, 2: LIKED})
    assert await rank_today(cast(Store, empty), stories) == stories
    assert empty.feature_calls == 0
    stub = StubStore(examples=EXAMPLES, features={1: DISLIKED, 2: LIKED})
    await refit(cast(Store, stub))
    ranked = await rank_today(cast(Store, stub), stories)
    assert [s.id for s in ranked] == [2, 1]


async def test_nightly_updates_weights_and_refits() -> None:
    source = SourceConfig(
        id=1,
        kind=SourceKind.FEED,
        name="Lab",
        config={"url": "https://example.com"},
        trust=Trust.PRIMARY,
        weight=1.0,
    )
    stub = StubStore(sources=[source], feedback={1: (3, 0)}, examples=EXAMPLES)
    ctx = cast(PipelineContext, SimpleNamespace(store=stub, clock=FixedClock(NOW)))
    await nightly(ctx)
    assert stub.weights == {1: 1.3}
    assert stub.settings[TASTE_KEY] is not None


def test_no_numeric_libraries() -> None:
    import ai_newsdesk.taste as taste

    source = Path(taste.__file__).read_text(encoding="utf-8")
    assert "numpy" not in source and "sklearn" not in source


def test_from_json_rejects_malformed_values() -> None:
    model = fit(EXAMPLES)
    assert model is not None
    good = model.to_json()
    for bad in ("abc", [1], {}):
        assert TasteModel.from_json({**good, "n_examples": bad}) is None
    nan_weights = [float("nan"), *good["weights"][1:]]
    assert TasteModel.from_json({**good, "weights": nan_weights}) is None
    assert TasteModel.from_json({**good, "bias": float("inf")}) is None


async def test_rank_today_never_raises() -> None:
    stories = [story(1, Category.OPINION, 5.0), story(2, Category.RESEARCH, 4.5)]
    model = fit(EXAMPLES)
    assert model is not None
    bad = StubStore(features={1: DISLIKED, 2: LIKED})
    bad.settings[TASTE_KEY] = {**model.to_json(), "n_examples": "abc"}
    assert await rank_today(cast(Store, bad), stories) == stories

    class Broken(StubStore):
        async def story_features(self, story_ids: list[int]) -> dict[int, dict[str, Any]]:
            raise RuntimeError("db gone")

    broken = Broken(examples=EXAMPLES)
    await refit(cast(Store, broken))
    assert await rank_today(cast(Store, broken), stories) == stories
