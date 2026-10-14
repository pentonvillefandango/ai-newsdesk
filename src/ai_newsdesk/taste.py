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
