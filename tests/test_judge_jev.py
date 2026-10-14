# tests/test_judge_jev.py
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_newsdesk.judges import jev
from ai_newsdesk.judges.jev import JevJudge, jev_signal, verdict_from_answers
from ai_newsdesk.models import Category, Purpose, ReviewInput, SourceKind, Trust
from ai_newsdesk.pricing import Pricing

PRICING = Pricing.load(Path(__file__).resolve().parents[1] / "config" / "pricing.yaml")


def answers(
    *,
    ai: float = 0.9,
    bait: float = 0.1,
    imp: float = 3.0,
    sub: float = 2.0,
    cred: float = 4.0,
    cats: dict[str, float] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ai_related": SimpleNamespace(noul=ai),
        "bait": SimpleNamespace(noul=bait),
        "importance": SimpleNamespace(score=imp),
        "substance": SimpleNamespace(score=sub),
        "credibility": SimpleNamespace(score=cred),
    }
    for c in jev.CATEGORIES:
        out[f"cat_{c.value}"] = SimpleNamespace(noul=(cats or {}).get(c.value, 0.1))
    return out


def inp(item_id: int, title: str = "Lab ships model") -> ReviewInput:
    return ReviewInput(
        item_id=item_id,
        source_name="Lab blog",
        source_kind=SourceKind.FEED,
        trust=Trust.PRIMARY,
        title=title,
        text="Body text.",
    )


def test_signal_formula_matches_bakeoff_v3() -> None:
    # base = 10 * (0.55*3 + 0.20*2 + 0.25*4) / 4 = 7.625; 7.625 * 0.9 - 0.4 = 6.4625
    assert jev_signal(ai=0.9, bait=0.1, imp=3.0, sub=2.0, cred=4.0) == pytest.approx(6.4625)
    assert jev_signal(ai=0.0, bait=1.0, imp=4.0, sub=4.0, cred=4.0) == 0.0


def test_verdict_from_answers() -> None:
    v = verdict_from_answers(7, answers(cats={"model_release": 0.8}))
    assert (v.item_id, v.signal, v.junk, v.category, v.same_story_id) == (
        7,
        6,
        False,
        Category.MODEL_RELEASE,
        None,
    )


def test_bait_or_off_topic_is_junk_and_low_categories_are_other() -> None:
    assert verdict_from_answers(1, answers(bait=0.7)).junk is True
    assert verdict_from_answers(1, answers(ai=0.4)).junk is True
    assert verdict_from_answers(1, answers()).category is Category.OTHER


class FakeJevClient:
    def __init__(self, fail_titles: set[str]) -> None:
        self.fail_titles = fail_titles
        self.states: list[dict[str, Any]] = []

    async def __aenter__(self) -> "FakeJevClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def system_one(self, *, state: dict[str, Any], questions: dict[str, Any]) -> Any:
        self.states.append(state)
        if state["title"] in self.fail_titles:
            raise RuntimeError("499 client closed request")
        return SimpleNamespace(answers=answers(), usage=SimpleNamespace(input_tokens=1200))


async def test_review_tolerates_per_item_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jev, "build_questions", lambda: {})
    client = FakeJevClient({"Broken"})
    judge = JevJudge("key", PRICING, client_factory=lambda: client)
    result = await judge.review([inp(1), inp(2, "Broken"), inp(3)])
    assert sorted(v.item_id for v in result.verdicts) == [1, 3]
    assert result.failed_item_ids == [2]
    assert len(result.usage) == 1
    u = result.usage[0]
    assert (u.provider, u.model, u.purpose, u.input_tokens) == (
        "typesafe",
        "jev-1.13.0",
        Purpose.REVIEW,
        # Two answered items, plus the failed one at its worst case: it may have been billed.
        2400 + judge.item_worst_tokens(inp(2, "Broken")),
    )
    assert client.states[0]["source"] == {"name": "Lab blog", "kind": "feed", "trust": "primary"}


async def test_missing_sdk_fails_every_item(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict[str, Any]:
        raise ImportError("typesafe_sdk")

    monkeypatch.setattr(jev, "build_questions", boom)
    result = await JevJudge("key", PRICING).review([inp(1), inp(2)])
    assert result.failed_item_ids == [1, 2]
    assert result.verdicts == [] and result.usage == []


def test_worst_case_cost_is_positive_and_grows() -> None:
    judge = JevJudge("key", PRICING)
    assert 0 < judge.worst_case_cost([inp(1)]) < judge.worst_case_cost([inp(1), inp(2)])


def test_question_set_with_real_sdk() -> None:
    pytest.importorskip("typesafe_sdk")
    questions = jev.build_questions()
    expected = {"ai_related", "bait", "importance", "substance", "credibility"}
    expected |= {f"cat_{c.value}" for c in jev.CATEGORIES}
    assert set(questions) == expected
    assert len(json.dumps(questions, default=repr).encode("utf-8")) < jev.QUESTION_BYTES


def test_worst_case_cost_bounds_state_and_questions() -> None:
    judge = JevJudge("key", PRICING)
    item = inp(1, "Ünïcode " * 50)
    sent = len(json.dumps(jev.state_for(item)).encode("utf-8")) + jev.QUESTION_BYTES
    assert judge.worst_case_cost([item]) >= PRICING.cost(jev.JEV_MODEL, sent, 0)
