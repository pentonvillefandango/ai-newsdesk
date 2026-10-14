# Step 4: Feedback and taste (tasks 23–24)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this step task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read the master plan `docs/plans/2026-10-14-ai-newsdesk-v1.md` (Global Constraints and Shared contract) and the "Contract additions" of `docs/plans/v1/step-3-pipeline.md` first.

**Goal:** Learn from the owner's thumbs in code, not prompts. Thumbs move each source's weight (which feeds the pre-filter), and once there are at least 30 thumbs a tiny logistic regression adjusts story ranking by up to ±2 points.

**Branch:** `step-4-feedback` (from `main`, merged through a PR).

**Depends on:** steps 0–3 (merged to `main`). Uses `tests/pipeline_helpers.py` (step 3, task 21), `PipelineContext` (step 3, task 22) and the scheduler's `nightly_fn` hook.

**Before task 23:**

```bash
cd /opt/ai-newsdesk
git switch main && git pull --ff-only
git switch -c step-4-feedback
```

Run `uv run ruff format src tests` before every commit; it may rewrap long lines in the code below. `make test` must pass at every commit.

## Assumed from earlier steps

- Everything listed under "Assumed from earlier steps" in step 3, plus `tests/pipeline_helpers.py` (`add_source`, `add_item`), the `store` and `clock` fixtures (`tests/conftest.py`) and `ai_newsdesk.clock.FixedClock(at)` (step 0; there is no `FakeClock`).
- Step 1 is the source of truth for every Store signature (master plan R1). This step adds no Store methods.

## Contract additions

**Store methods this step uses (all provided by step 1, R1; signatures as pinned there):**
- `list_sources(enabled_only=False) -> list[SourceConfig]`.
- `set_source_weight(source_id: int, weight: float) -> None`: sets `sources.weight` (0.3–2.0).
- `set_feedback(story_id: int, value: int) -> None`: value in {-1, 0, 1}; stamps `feedback_at`.
- `source_feedback(days: int, now: datetime) -> dict[int, tuple[int, int]]`: per source ID, `(ups, downs)` over stories with feedback `1` / `-1` whose `feedback_at` is within `days` of `now`, crediting every distinct source that has an item on the story (each story counts once per source).
- `feedback_examples() -> list[tuple[dict[str, Any], int]]`: one entry per rated story, from the story's **lead item**. Features: `{"signal": int | None, "junk": bool, "category": str, "trust": str, "source_kind": str}` (`signal` is the judge's raw 0–10, `None` for an unjudged lead); the label is the feedback (`1` or `-1`), newest thumb first.
- `story_features(story_ids: list[int]) -> dict[int, dict[str, Any]]`: the same feature dict for each requested story that has at least one item.
- `get_setting(key) -> Any` (`None` when absent) and `set_setting(key, value)` (`None` deletes the key).

`taste.featurize` reads exactly these five keys (`taste.FEATURE_KEYS`); `tests/test_taste.py::test_store_feature_dicts_match_featurize` checks it against the real Store.

**New settings keys:** `taste_model` (JSON written by `taste.refit`, `null` when there isn't enough data). `blocklist` comes from step 3.

**New names:**
- `feedback.py`: `FEEDBACK_DAYS = 30`, `WEIGHT_MIN = 0.3`, `WEIGHT_MAX = 2.0`, `weight_for(ups, downs) -> float`, `async update_source_weights(store, now, *, days=30) -> dict[int, float]` (changed weights only), `async set_feedback(store, clock, story_id, value) -> dict[int, float]` (value in {-1, 0, 1}; raises `ValueError` otherwise), `async add_blocklist_keyword(store, keyword) -> list[str]`.
- `taste.py`: `TASTE_KEY`, `MIN_EXAMPLES = 30`, `MAX_ADJUST = 2.0`, `FEATURE_KEYS`, `FEATURE_NAMES` (18), `featurize`, `TasteModel(weights, bias, n_examples)` with `to_json()` / `from_json()`, `fit(examples) -> TasteModel | None`, `probability`, `adjustment(model, features) -> float` in [−2, +2], `apply(stories, features_by_id, model) -> list[Story]`, `async refit(store)`, `async load_model(store)`, `async rank_today(store, stories) -> list[Story]`, `async nightly(ctx)`.

**Requirements on step 5 (web and wiring):**
- Thumbs endpoints call `feedback.set_feedback(store, clock, story_id, value)` (not `Store.set_feedback` directly), so source weights update immediately.
- "Never show stories like this" calls `feedback.add_blocklist_keyword(store, keyword)`.
- The Today page ranks with `await taste.rank_today(store, stories)` after `Store.today_stories(...)`.
- `app.py` builds the scheduler as `Scheduler(ctx, nightly_fn=taste.nightly)`.

---

### Task 23: Feedback and source weights (`feedback.py`)

**Goal:** Thumbs on a story credit each of its sources; a source's weight is `clamp(1 + 0.1 × (ups − downs over 30 days), 0.3, 2.0)`, written to the `sources` table on every thumb and nightly.

**Files:**
- Create: `src/ai_newsdesk/feedback.py`
- Create: `tests/feedback_stubs.py`
- Test: `tests/test_feedback.py`

**Acceptance Criteria:**
- [ ] `weight_for(ups, downs)` equals `clamp(1 + 0.1 × (ups − downs), 0.3, 2.0)`, rounded to 3 decimals.
- [ ] `update_source_weights` asks the store for 30 days of feedback, covers every source (including disabled ones), writes only weights that changed, and returns them.
- [ ] `set_feedback` rejects values outside {-1, 0, 1} without writing anything; otherwise it stores the thumb and updates weights at once.
- [ ] `add_blocklist_keyword` lower-cases and collapses whitespace, ignores duplicates, and rejects empty or over-60-character keywords.
- [ ] With the real Store, a thumb up on a two-source story makes both weights 1.1; a thumb down makes them 0.9; clearing it makes them 1.0.

**Verify:** `uv run pytest tests/test_feedback.py -q` → `13 passed`

**Steps:**

- [ ] **Step 1: Write the stubs and the failing test**

```python
# tests/feedback_stubs.py
"""An in-memory stand-in for the Store methods that feedback.py and taste.py use."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ai_newsdesk.models import SourceConfig


class StubStore:
    def __init__(
        self,
        *,
        sources: list[SourceConfig] | None = None,
        feedback: dict[int, tuple[int, int]] | None = None,
        examples: list[tuple[dict[str, Any], int]] | None = None,
        features: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.sources = sources or []
        self.feedback = feedback or {}
        self.examples = examples or []
        self.features = features or {}
        self.settings: dict[str, Any] = {}
        self.weights: dict[int, float] = {}
        self.story_feedback: dict[int, int] = {}
        self.feedback_asked: list[tuple[int, datetime]] = []
        self.feature_calls = 0

    async def list_sources(self, enabled_only: bool = True) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled or not enabled_only]

    async def source_feedback(self, days: int, now: datetime) -> dict[int, tuple[int, int]]:
        self.feedback_asked.append((days, now))
        return self.feedback

    async def set_source_weight(self, source_id: int, weight: float) -> None:
        self.weights[source_id] = weight

    async def set_feedback(self, story_id: int, value: int) -> None:
        self.story_feedback[story_id] = value

    async def get_setting(self, key: str) -> Any:
        return self.settings.get(key)

    async def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value

    async def feedback_examples(self) -> list[tuple[dict[str, Any], int]]:
        return self.examples

    async def story_features(self, story_ids: list[int]) -> dict[int, dict[str, Any]]:
        self.feature_calls += 1
        return {i: self.features[i] for i in story_ids if i in self.features}
```

```python
# tests/test_feedback.py
from datetime import UTC, datetime
from typing import cast

import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.feedback import (
    FEEDBACK_DAYS,
    add_blocklist_keyword,
    set_feedback,
    update_source_weights,
    weight_for,
)
from ai_newsdesk.models import Category, SourceConfig, SourceKind, Trust
from ai_newsdesk.store.store import Store
from tests.feedback_stubs import StubStore
from tests.pipeline_helpers import add_item, add_source

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def src(sid: int, weight: float = 1.0, enabled: bool = True) -> SourceConfig:
    return SourceConfig(
        id=sid,
        kind=SourceKind.FEED,
        name=f"s{sid}",
        config={"url": "https://example.com"},
        trust=Trust.TRUSTED,
        enabled=enabled,
        weight=weight,
    )


@pytest.mark.parametrize(
    ("ups", "downs", "expected"),
    [(0, 0, 1.0), (1, 0, 1.1), (0, 1, 0.9), (25, 0, 2.0), (0, 20, 0.3), (12, 2, 2.0), (3, 10, 0.3)],
)
def test_weight_for(ups: int, downs: int, expected: float) -> None:
    assert weight_for(ups, downs) == expected


async def test_update_source_weights_writes_only_changes() -> None:
    stub = StubStore(
        sources=[
            src(1),
            src(2, weight=1.5),
            src(3, enabled=False),
            src(4, weight=1.0, enabled=False),
        ],
        feedback={1: (2, 0), 2: (0, 0), 4: (0, 3)},
    )
    changed = await update_source_weights(cast(Store, stub), NOW)
    assert changed == {1: 1.2, 2: 1.0, 4: 0.7}
    assert stub.weights == changed
    assert stub.feedback_asked == [(FEEDBACK_DAYS, NOW)]
    assert FEEDBACK_DAYS == 30


async def test_set_feedback_rejects_bad_values() -> None:
    stub = StubStore(sources=[src(1)])
    with pytest.raises(ValueError):
        await set_feedback(cast(Store, stub), FixedClock(NOW), 5, 2)
    assert stub.story_feedback == {} and stub.weights == {}


async def test_set_feedback_stores_thumb_and_updates_weights() -> None:
    stub = StubStore(sources=[src(1)], feedback={1: (1, 0)})
    changed = await set_feedback(cast(Store, stub), FixedClock(NOW), 5, 1)
    assert stub.story_feedback == {5: 1}
    assert changed == {1: 1.1}


async def test_add_blocklist_keyword_normalises_and_dedupes() -> None:
    stub = StubStore()
    assert await add_blocklist_keyword(cast(Store, stub), "  Crypto   Airdrop ") == [
        "crypto airdrop"
    ]
    assert await add_blocklist_keyword(cast(Store, stub), "CRYPTO airdrop") == ["crypto airdrop"]
    assert await add_blocklist_keyword(cast(Store, stub), "giveaway") == [
        "crypto airdrop",
        "giveaway",
    ]
    assert stub.settings["blocklist"] == ["crypto airdrop", "giveaway"]


async def test_add_blocklist_keyword_rejects_empty_and_long() -> None:
    stub = StubStore()
    with pytest.raises(ValueError):
        await add_blocklist_keyword(cast(Store, stub), "   ")
    with pytest.raises(ValueError):
        await add_blocklist_keyword(cast(Store, stub), "x" * 61)
    assert "blocklist" not in stub.settings


async def test_thumbs_move_source_weights_in_real_store(store: Store) -> None:
    lab = await add_source(store, "Lab blog", trust=Trust.PRIMARY)
    person = await add_source(store, "Researcher", trust=Trust.TRUSTED)
    i1 = await add_item(store, lab.id, "Lab ships model", "https://example.com/1", NOW)
    i2 = await add_item(store, person.id, "Lab ships model: notes", "https://example.com/2", NOW)
    story = await store.create_story("Lab ships model", Category.MODEL_RELEASE, NOW)
    await store.attach_item(i1, story, NOW)
    await store.attach_item(i2, story, NOW)
    clock = FixedClock(NOW)

    async def weights() -> dict[int, float]:
        return {s.id: s.weight for s in await store.list_sources(enabled_only=False)}

    await set_feedback(store, clock, story, 1)
    assert await weights() == {lab.id: 1.1, person.id: 1.1}
    await set_feedback(store, clock, story, -1)
    assert await weights() == {lab.id: 0.9, person.id: 0.9}
    await set_feedback(store, clock, story, 0)
    assert await weights() == {lab.id: 1.0, person.id: 1.0}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_feedback.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai_newsdesk.feedback'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/ai_newsdesk/feedback.py
"""Thumbs feedback: per-source weights (fed to the pre-filter) and blocklist keywords.

weight = clamp(1 + 0.1 x (ups - downs over 30 days), 0.3, 2.0), written to the sources table on
every thumb and nightly (taste.nightly), so old thumbs age out.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ai_newsdesk.protocols import Clock
from ai_newsdesk.store.store import Store

log = logging.getLogger(__name__)

FEEDBACK_DAYS = 30
WEIGHT_STEP = 0.1
WEIGHT_MIN = 0.3
WEIGHT_MAX = 2.0
BLOCKLIST_KEY = "blocklist"
MAX_KEYWORD_CHARS = 60
VALID_FEEDBACK = (-1, 0, 1)


def weight_for(ups: int, downs: int) -> float:
    raw = 1.0 + WEIGHT_STEP * (ups - downs)
    return round(min(WEIGHT_MAX, max(WEIGHT_MIN, raw)), 3)


async def update_source_weights(
    store: Store, now: datetime, *, days: int = FEEDBACK_DAYS
) -> dict[int, float]:
    counts = await store.source_feedback(days, now)
    changed: dict[int, float] = {}
    for src in await store.list_sources(enabled_only=False):
        ups, downs = counts.get(src.id, (0, 0))
        weight = weight_for(ups, downs)
        if abs(weight - src.weight) > 1e-9:
            await store.set_source_weight(src.id, weight)
            changed[src.id] = weight
    if changed:
        log.info("source weights updated: %s", changed)
    return changed


async def set_feedback(store: Store, clock: Clock, story_id: int, value: int) -> dict[int, float]:
    if value not in VALID_FEEDBACK:
        raise ValueError("feedback must be -1, 0 or 1")
    await store.set_feedback(story_id, value)
    return await update_source_weights(store, clock.now())


async def add_blocklist_keyword(store: Store, keyword: str) -> list[str]:
    kw = " ".join(keyword.lower().split())
    if not kw or len(kw) > MAX_KEYWORD_CHARS:
        raise ValueError(f"a blocklist keyword must be 1-{MAX_KEYWORD_CHARS} characters")
    current = await store.get_setting(BLOCKLIST_KEY)
    words = [str(w) for w in current] if isinstance(current, list) else []
    if kw not in words:
        words.append(kw)
        await store.set_setting(BLOCKLIST_KEY, words)
    return words
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_feedback.py -q`
Expected: `13 passed`

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && make test
git add src/ai_newsdesk/feedback.py tests/feedback_stubs.py tests/test_feedback.py
git commit -m "feat(feedback): thumbs update source weights and blocklist (V1-S4)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 24: Taste model (`taste.py`)

**Goal:** A tiny, deterministic, pure-Python logistic regression over the stored judge outputs, source trust and source kind, refitted nightly once there are at least 30 thumbs, that nudges story ranking by at most ±2 points.

**Files:**
- Create: `src/ai_newsdesk/taste.py`
- Test: `tests/test_taste.py`

**Model:**
- Input: the feature dict step 1 returns, keys exactly `signal`, `junk`, `category`, `trust`, `source_kind` (`FEATURE_KEYS`).
- Features (18): `signal / 10`, `junk` (0/1), one-hot category (7, in `Category` order), one-hot trust (3, in `Trust` order), one-hot source kind (6, in `SourceKind` order). An unknown or missing value gives an all-zero block.
- Label: thumb up = 1, thumb down = 0; cleared thumbs (0) are ignored. At most the 2,000 newest examples are used.
- Fit: zero initial weights, 500 epochs of full-batch gradient descent, learning rate 0.5, L2 0.01 (not on the bias). No randomness, so the same data always gives the same model.
- Not fitted (`None`) with fewer than 30 labelled examples or only one class.
- Adjustment: `4 × (p − 0.5)`, clamped to [−2, +2]; `0` when there is no model or no features for the story.

**Acceptance Criteria:**
- [ ] No `numpy` / `sklearn`; `taste.py` imports only the standard library and project modules.
- [ ] On synthetic data where the owner likes trusted research and dislikes discovery opinion, the adjustment is > +1 for the former and < −1 for the latter, and always within [−2, +2].
- [ ] Fitting the same data twice gives an identical model; fewer than 30 thumbs, or one class only, gives `None`.
- [ ] `apply` adds the adjustment to each story's score and re-sorts by score (then newest); stories without features keep their score; with no model the list is returned unchanged.
- [ ] `refit` stores the model JSON (or `null`) under `taste_model`; `load_model` reads it back and rejects malformed or out-of-date JSON.
- [ ] `rank_today` doesn't query features when there is no model; `nightly` updates source weights and refits.
- [ ] With the real Store, `feedback_examples()` and `story_features()` return dicts whose keys are exactly `FEATURE_KEYS`, and `featurize` uses all of them.

**Verify:** `uv run pytest tests/test_taste.py -q` → `14 passed`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/test_taste.py
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_taste.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai_newsdesk.taste'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/ai_newsdesk/taste.py
"""Learned taste: a tiny logistic regression over judge outputs, source trust and source kind.

Refitted nightly once there are at least 30 thumbs. It produces a ranking adjustment in
[-2, +2] that is added to a story's score when the Today page is ranked. Pure Python and
deterministic: zero initial weights and full-batch gradient descent, no randomness.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ai_newsdesk import feedback
from ai_newsdesk.models import Category, SourceKind, Story, Trust
from ai_newsdesk.pipeline import PipelineContext
from ai_newsdesk.store.store import Store

log = logging.getLogger(__name__)

TASTE_KEY = "taste_model"
MIN_EXAMPLES = 30
MAX_EXAMPLES = 2000
MAX_ADJUST = 2.0
EPOCHS = 500
LEARNING_RATE = 0.5
L2 = 0.01

# Exactly the keys of the lead-item feature dict that Store.feedback_examples() and
# Store.story_features() return (step 1).
FEATURE_KEYS: tuple[str, ...] = ("signal", "junk", "category", "trust", "source_kind")

FEATURE_NAMES: tuple[str, ...] = (
    "signal",
    "junk",
    *(f"cat_{c.value}" for c in Category),
    *(f"trust_{t.value}" for t in Trust),
    *(f"kind_{k.value}" for k in SourceKind),
)


def featurize(features: Mapping[str, Any]) -> list[float]:
    try:
        signal = float(features.get("signal") or 0) / 10.0
    except (TypeError, ValueError):
        signal = 0.0
    junk = 1.0 if features.get("junk") else 0.0
    category = str(features.get("category") or "")
    trust = str(features.get("trust") or "")
    kind = str(features.get("source_kind") or "")
    return [
        max(0.0, min(1.0, signal)),
        junk,
        *(1.0 if category == c.value else 0.0 for c in Category),
        *(1.0 if trust == t.value else 0.0 for t in Trust),
        *(1.0 if kind == k.value else 0.0 for k in SourceKind),
    ]


@dataclass(frozen=True)
class TasteModel:
    weights: tuple[float, ...]
    bias: float
    n_examples: int

    def to_json(self) -> dict[str, Any]:
        return {
            "features": list(FEATURE_NAMES),
            "weights": list(self.weights),
            "bias": self.bias,
            "n_examples": self.n_examples,
        }

    @classmethod
    def from_json(cls, data: object) -> TasteModel | None:
        if not isinstance(data, dict):
            return None
        weights = data.get("weights")
        bias = data.get("bias")
        if data.get("features") != list(FEATURE_NAMES):
            return None  # written for a different feature set: wait for the next refit
        if not isinstance(weights, list) or len(weights) != len(FEATURE_NAMES):
            return None
        if not all(isinstance(w, int | float) for w in weights) or not isinstance(
            bias, int | float
        ):
            return None
        return cls(tuple(float(w) for w in weights), float(bias), int(data.get("n_examples") or 0))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _dot(w: Sequence[float], x: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(w, x, strict=True))


def fit(examples: Sequence[tuple[Mapping[str, Any], int]]) -> TasteModel | None:
    rows = [(featurize(f), 1.0 if label > 0 else 0.0) for f, label in examples if label != 0]
    rows = rows[:MAX_EXAMPLES]
    if len(rows) < MIN_EXAMPLES or len({y for _, y in rows}) < 2:
        return None
    d = len(FEATURE_NAMES)
    n = len(rows)
    w = [0.0] * d
    b = 0.0
    for _ in range(EPOCHS):
        grad_w = [0.0] * d
        grad_b = 0.0
        for x, y in rows:
            err = _sigmoid(_dot(w, x) + b) - y
            for k in range(d):
                grad_w[k] += err * x[k]
            grad_b += err
        for k in range(d):
            w[k] -= LEARNING_RATE * (grad_w[k] / n + L2 * w[k])
        b -= LEARNING_RATE * grad_b / n
    return TasteModel(tuple(round(v, 6) for v in w), round(b, 6), n)


def probability(model: TasteModel, features: Mapping[str, Any]) -> float:
    return _sigmoid(_dot(model.weights, featurize(features)) + model.bias)


def adjustment(model: TasteModel | None, features: Mapping[str, Any]) -> float:
    if model is None:
        return 0.0
    raw = 2 * MAX_ADJUST * (probability(model, features) - 0.5)
    return round(max(-MAX_ADJUST, min(MAX_ADJUST, raw)), 3)


def apply(
    stories: Sequence[Story],
    features_by_id: Mapping[int, Mapping[str, Any]],
    model: TasteModel | None,
) -> list[Story]:
    if model is None:
        return list(stories)
    adjusted = [
        s.model_copy(update={"score": round(s.score + adjustment(model, features_by_id[s.id]), 3)})
        if s.id in features_by_id
        else s
        for s in stories
    ]
    return sorted(adjusted, key=lambda s: (-s.score, -s.last_updated_at.timestamp()))


async def refit(store: Store) -> TasteModel | None:
    model = fit(await store.feedback_examples())
    await store.set_setting(TASTE_KEY, model.to_json() if model else None)
    log.info(
        "taste model %s", f"refitted on {model.n_examples} thumbs" if model else "not fitted yet"
    )
    return model


async def load_model(store: Store) -> TasteModel | None:
    return TasteModel.from_json(await store.get_setting(TASTE_KEY))


async def rank_today(store: Store, stories: Sequence[Story]) -> list[Story]:
    model = await load_model(store)
    if model is None or not stories:
        return list(stories)
    features = await store.story_features([s.id for s in stories])
    return apply(stories, features, model)


async def nightly(ctx: PipelineContext) -> None:
    await feedback.update_source_weights(ctx.store, ctx.clock.now())
    await refit(ctx.store)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_taste.py -q`
Expected: `14 passed`

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && make test
git add src/ai_newsdesk/taste.py tests/test_taste.py
git commit -m "feat(taste): nightly logistic-regression ranking adjustment (V1-S4)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Step done when

- [ ] Tasks 23 and 24 are committed on `step-4-feedback`, one Conventional Commit per task, each ending with the `Co-Authored-By` trailer.
- [ ] `make test` passes: ruff, ruff format check, pyright and the whole pytest suite, including the 27 new tests in this step.
- [ ] `grep -rn "numpy\|sklearn" src/` finds nothing.
- [ ] The step 5 requirements above (thumbs through `feedback.set_feedback`, `taste.rank_today` on Today, `Scheduler(ctx, nightly_fn=taste.nightly)`) are noted in the step 5 plan, or raised with the plan owner.
- [ ] The branch is pushed and a PR is open and merged:

```bash
git push -u origin step-4-feedback
GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr create --base main --head step-4-feedback \
  --title "Step 4: feedback weights and taste model" \
  --body "Tasks 23-24 of docs/plans/v1/step-4-feedback.md. make test passes.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```
