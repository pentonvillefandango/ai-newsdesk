"""The Compare engine (multi-provider spec 4): samples, the maximum cost, runs under the
comparison and monthly caps, time limits, and the metrics (checked by hand against a fixture)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from ai_newsdesk import compare
from ai_newsdesk.budget import Budget
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.judges.llm import BatchJudgement, ItemJudgement, LLMJudge
from ai_newsdesk.llm.base import ModelClient, StructuredResult
from ai_newsdesk.llm.factory import UnusableModelError
from ai_newsdesk.models import (
    Category,
    ItemStatus,
    Purpose,
    ReviewInput,
    StoryText,
    Verdict,
    WriteInput,
)
from ai_newsdesk.pricing import Pricing
from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.store import Store
from ai_newsdesk.store.records import CompareItem, CompareModel, CompareResult
from ai_newsdesk.writer import LLMWriter, StoryBatch, StoryWriting
from tests.fake_model_client import FakeModelClient
from tests.web import seed
from tests.web.support import CONFIG_DIR

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
A = CompareModel(provider="anthropic", model="claude-haiku-5-5")
B = CompareModel(provider="openrouter", model="google/gemini-3.8-flash")


# ---- helpers ------------------------------------------------------------------------------


def judgement(ids: list[int], signal: int = 6, junk: bool = False) -> BatchJudgement:
    return BatchJudgement(
        verdicts=[
            ItemJudgement(
                item_id=i, signal=signal, junk=junk, category="research", same_story_id=None
            )
            for i in ids
        ]
    )


def writing(ids: list[int]) -> StoryBatch:
    return StoryBatch(
        stories=[
            StoryWriting(story_id=i, summary=f"Summary of {i}.", why_it_matters=None) for i in ids
        ]
    )


def review_inputs(n: int) -> list[ReviewInput]:
    return [
        ReviewInput(
            item_id=i,
            source_name="Lab blog",
            source_kind="feed",  # type: ignore[arg-type]
            trust="primary",  # type: ignore[arg-type]
            title=f"Item {i} " + "x" * i,
            text="Body " * i,
        )
        for i in range(1, n + 1)
    ]


async def budget_for(store: Store, monthly: float = 5.0) -> Budget:
    budget = Budget(
        store,
        Pricing.load(CONFIG_DIR / "pricing.yaml"),
        FixedClock(NOW),
        RuntimeSettings(monthly_budget_usd=monthly),
    )
    await budget.refresh()
    return budget


async def queued_items(store: Store, n: int) -> list[int]:
    src = await seed.add_source(store, name="Lab blog")
    return [
        await seed.add_item(
            store,
            source_id=src,
            url=f"https://lab.example/{i}",
            title=f"Lab item number {i}",
            at=NOW - timedelta(minutes=10 * (i + 1)),
            status=ItemStatus.QUEUED,
        )
        for i in range(n)
    ]


def clients_for(clients: dict[str, ModelClient]) -> Callable[[str, str], ModelClient]:
    def client_for(provider: str, model: str) -> ModelClient:
        return clients[f"{provider}:{model}"]

    return client_for


async def spend_rows(store: Store) -> list[tuple[Any, ...]]:
    return [
        tuple(r) for r in await store._all("SELECT provider, model, purpose, cost_usd FROM spend")
    ]


async def snapshot(store: Store) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    items = [tuple(r) for r in await store._all("SELECT * FROM items ORDER BY id")]
    stories = [tuple(r) for r in await store._all("SELECT * FROM stories ORDER BY id")]
    return items, stories


class HangingClient(FakeModelClient):
    """Accepts the call and never answers."""

    async def structured[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, purpose: Purpose
    ) -> StructuredResult[T]:
        self.calls.append({"user": user})
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


# ---- samples and maximum cost -------------------------------------------------------------


async def test_the_judge_sample_gets_the_production_judge_input(store: Store) -> None:
    src = await seed.add_source(store, name="Lab blog")
    story = await seed.add_story(
        store,
        at=NOW - timedelta(hours=1),
        headline="Northwind releases a small open model",
        sources=[(src, "https://lab.example/release", "Northwind releases a small open model")],
    )
    item = await seed.add_item(
        store,
        source_id=src,
        url="https://lab.example/again",
        title="Northwind small open model released today",
        at=NOW - timedelta(minutes=5),
        status=ItemStatus.QUEUED,
    )
    await store.set_item_fields(item, body_excerpt="Full text.")
    sample = await compare.judge_sample(store, NOW, 30)
    first = sample[0]
    assert first.item_id == item and first.text == "Full text."
    assert [c.id for c in first.story_candidates] == [story]  # as the pipeline offers them


async def test_the_writer_sample_honours_the_why_it_matters_setting(store: Store) -> None:
    src = await seed.add_source(store, name="Lab blog")
    await seed.add_story(
        store, at=NOW, headline="Story", sources=[(src, "https://lab.example/s", "Story")]
    )
    [w] = await compare.writer_sample(store, NOW, 5, RuntimeSettings(why_it_matters=False))
    assert w.include_why is False and w.texts


def test_sample_sizes_and_caps_match_the_spec() -> None:
    assert compare.SAMPLE_DEFAULT == {"judge": 30, "writer": 5}
    assert compare.SAMPLE_MAX == {"judge": 100, "writer": 15}
    assert (compare.CAP_DEFAULT, compare.CAP_MIN, compare.CAP_MAX) == (0.25, 0.01, 5.0)
    assert (compare.MIN_MODELS, compare.MAX_MODELS) == (2, 4)
    assert compare.CALL_TIMEOUT_S == 120 and compare.RUN_TIMEOUT_S == 15 * 60
    assert compare.MAX_RETRIES <= 1


def test_max_cost_sums_every_calls_worst_case_with_production_batching() -> None:
    sample = review_inputs(23)
    a, b = FakeModelClient(usd_per_token=1e-6), FakeModelClient(usd_per_token=3e-6)
    expected = sum(
        LLMJudge(c).worst_case_cost(sample[i : i + 10]) for c in (a, b) for i in (0, 10, 20)
    )
    assert compare.max_cost_usd("judge", [a, b], sample) == pytest.approx(expected)
    # The pipeline's batches of 10 are the judge's own: three calls per model.
    assert expected == pytest.approx(
        LLMJudge(a).worst_case_cost(sample) + LLMJudge(b).worst_case_cost(sample)
    )
    assert expected > 3 * LLMJudge(a).worst_case_cost(sample[:1])


def test_max_cost_for_the_writer() -> None:
    sample = [
        WriteInput(story_id=i, headline=f"H{i}", texts=["t" * 50], include_why=True)
        for i in range(12)
    ]
    a = FakeModelClient()
    w = LLMWriter(a, a)
    expected = w.worst_case_cost_stories(sample[:10]) + w.worst_case_cost_stories(sample[10:])
    assert compare.max_cost_usd("writer", [a], sample) == pytest.approx(expected)
    assert compare.max_cost_usd("writer", [a], []) == 0.0


# ---- runs ---------------------------------------------------------------------------------


async def _judge_run(
    store: Store,
    clients: dict[str, ModelClient],
    models: list[CompareModel],
    *,
    cap: float = 0.25,
    budget: Budget | None = None,
    n: int = 12,
    **kw: Any,
) -> tuple[int, list[ReviewInput], str]:
    sample = await compare.judge_sample(store, NOW, n)
    run_id = await store.create_compare_run("judge", models, [s.item_id for s in sample], cap, NOW)
    status = await compare.run_comparison(
        store,
        run_id,
        "judge",
        models,
        sample,
        cap_usd=cap,
        client_for=clients_for(clients),
        budget=budget or await budget_for(store),
        clock=FixedClock(NOW),
        **kw,
    )
    return run_id, sample, status


async def test_a_judge_run_stores_every_output_and_records_review_spend(store: Store) -> None:
    await queued_items(store, 12)
    sample = await compare.judge_sample(store, NOW, 12)
    ids = [s.item_id for s in sample]
    a = FakeModelClient(
        [judgement(ids[:10], 7), judgement(ids[10:], 7)],
        provider="anthropic",
        model="claude-haiku-5-5",
        reported_cost_usd=0.001,
    )
    b = FakeModelClient([judgement(ids[:10], 2), "invalid"], reported_cost_usd=0.002)
    before = await snapshot(store)
    run_id, _, status = await _judge_run(store, {A.key: a, B.key: b}, [A, B])
    assert status == "done"
    assert [c["user"] for c in a.calls] == [c["user"] for c in b.calls]  # same prompts
    assert all(c["purpose"] is Purpose.REVIEW for c in a.calls + b.calls)
    results = {(r.model_key, r.subject_id): r for r in await store.compare_results(run_id)}
    assert len(results) == 24
    first = results[(A.key, ids[0])]
    assert first.ok and first.output is not None and first.output["signal"] == 7
    assert first.cost_usd == pytest.approx(0.0001)  # the call's cost split over its 10 items
    failed = results[(B.key, ids[11])]
    assert failed.ran and not failed.ok and failed.output is None
    assert sorted(r[2] for r in await spend_rows(store)) == ["review"] * 4
    run = await store.compare_run(run_id)
    assert run is not None
    assert (run.status, run.finished_at, run.error) == ("done", NOW, None)
    assert run.cost_usd == pytest.approx(0.006)
    assert await snapshot(store) == before  # no verdict saved, nothing moved


async def test_a_writer_run_records_write_spend_and_changes_no_story(store: Store) -> None:
    src = await seed.add_source(store, name="Lab blog")
    ids = [
        await seed.add_story(
            store,
            at=NOW - timedelta(hours=n),
            headline=f"Story {n}",
            sources=[(src, f"https://lab.example/{n}", f"Story {n}")],
            signal=9 - n,
        )
        for n in range(3)
    ]
    sample = await compare.writer_sample(store, NOW, 5, RuntimeSettings())
    a = FakeModelClient([writing(ids)], reported_cost_usd=0.003)
    b = FakeModelClient([writing(ids[:2])], model="x/y", reported_cost_usd=0.003)
    other = CompareModel(provider="openrouter", model="x/y")
    before = await snapshot(store)
    run_id = await store.create_compare_run("writer", [B, other], ids, 0.25, NOW)
    status = await compare.run_comparison(
        store,
        run_id,
        "writer",
        [B, other],
        sample,
        cap_usd=0.25,
        client_for=clients_for({B.key: a, other.key: b}),
        budget=await budget_for(store),
        clock=FixedClock(NOW),
    )
    assert status == "done"
    assert all(c["purpose"] is Purpose.WRITE for c in a.calls + b.calls)
    results = {(r.model_key, r.subject_id): r for r in await store.compare_results(run_id)}
    assert results[(B.key, ids[0])].output == {
        "story_id": ids[0],
        "summary": f"Summary of {ids[0]}.",
        "why_it_matters": None,
    }
    assert not results[(other.key, ids[2])].ok  # left out by the model: a failure
    assert sorted(r[2] for r in await spend_rows(store)) == ["write", "write"]
    assert await snapshot(store) == before


async def test_a_capped_run_stops_before_a_call_that_would_pass_the_cap(store: Store) -> None:
    await queued_items(store, 12)
    sample = await compare.judge_sample(store, NOW, 12)
    ids = [s.item_id for s in sample]
    first_worst = LLMJudge(FakeModelClient()).worst_case_cost(sample[:10])
    cap = first_worst + 0.001
    a = FakeModelClient(
        [judgement(ids[:10])],
        provider="anthropic",
        model="claude-haiku-5-5",
        reported_cost_usd=first_worst - 0.0005,
    )
    b = FakeModelClient([])
    run_id, _, status = await _judge_run(store, {A.key: a, B.key: b}, [A, B], cap=cap)
    assert status == "capped"
    assert len(a.calls) == 1 and b.calls == []  # nothing sent that the cap could not cover
    results = await store.compare_results(run_id)
    not_run = {(r.model_key, r.subject_id) for r in results if not r.ran}
    assert not_run == {(A.key, i) for i in ids[10:]} | {(B.key, i) for i in ids}
    spent = sum(r[3] for r in await spend_rows(store))
    assert spent <= cap
    run = await store.compare_run(run_id)
    assert run is not None and run.status == "capped"
    assert run.cost_usd == pytest.approx(spent)


async def test_the_monthly_cap_is_checked_before_every_call(store: Store) -> None:
    await queued_items(store, 3)
    a = FakeModelClient([], provider="anthropic", model="claude-haiku-5-5")
    b = FakeModelClient([])
    _, _, status = await _judge_run(
        store, {A.key: a, B.key: b}, [A, B], budget=await budget_for(store, monthly=0.0001)
    )
    assert status == "capped"
    assert a.calls == [] and b.calls == []
    assert await spend_rows(store) == []


async def test_a_call_that_hangs_fails_its_batch_and_the_run_goes_on(store: Store) -> None:
    await queued_items(store, 3)
    sample = await compare.judge_sample(store, NOW, 3)
    ids = [s.item_id for s in sample]
    a = HangingClient(provider="anthropic", model="claude-haiku-5-5")
    b = FakeModelClient([judgement(ids)], reported_cost_usd=0.001)
    started = time.perf_counter()
    run_id, _, status = await _judge_run(store, {A.key: a, B.key: b}, [A, B], call_timeout_s=0.05)
    assert time.perf_counter() - started < 2
    assert status == "done"
    results = {(r.model_key, r.subject_id): r for r in await store.compare_results(run_id)}
    assert all(results[(A.key, i)].ran and not results[(A.key, i)].ok for i in ids)
    assert all(results[(B.key, i)].ok for i in ids)


async def test_a_run_over_the_time_limit_fails_and_finishes(store: Store) -> None:
    await queued_items(store, 3)
    a = HangingClient(provider="anthropic", model="claude-haiku-5-5")
    b = FakeModelClient([])
    started = time.perf_counter()
    run_id, _, status = await _judge_run(
        store, {A.key: a, B.key: b}, [A, B], call_timeout_s=60, run_timeout_s=0.1
    )
    assert time.perf_counter() - started < 2
    assert status == "failed"
    run = await store.compare_run(run_id)
    assert run is not None
    assert (run.status, run.error, run.finished_at) == ("failed", compare.TOO_LONG, NOW)
    assert "15 minutes" in compare.TOO_LONG
    assert b.calls == []


async def test_an_unusable_model_fails_the_run_before_anything_is_sent(store: Store) -> None:
    await queued_items(store, 3)
    a = FakeModelClient([], provider="anthropic", model="claude-haiku-5-5")

    def client_for(provider: str, model: str) -> ModelClient:
        if provider == "openrouter":
            raise UnusableModelError("Add your OpenRouter API key first.")
        return a

    sample = await compare.judge_sample(store, NOW, 3)
    run_id = await store.create_compare_run("judge", [A, B], [], 0.25, NOW)
    status = await compare.run_comparison(
        store,
        run_id,
        "judge",
        [A, B],
        sample,
        cap_usd=0.25,
        client_for=client_for,
        budget=await budget_for(store),
        clock=FixedClock(NOW),
    )
    assert status == "failed" and a.calls == []
    run = await store.compare_run(run_id)
    assert run is not None and run.error == "Add your OpenRouter API key first."


# ---- metrics (worked out by hand) ---------------------------------------------------------


def _item(
    item_id: int,
    *,
    weight: float = 1.0,
    production: int | None = None,
    feedback: int = 0,
    label: str | None = None,
) -> CompareItem:
    verdict = (
        Verdict(item_id=item_id, signal=production, junk=False, category=Category.RESEARCH)
        if production is not None
        else None
    )
    return CompareItem(
        item_id=item_id,
        title=f"Item {item_id}",
        url=f"https://lab.example/{item_id}",
        source_name="Lab blog",
        weight=weight,
        verdict=verdict,
        feedback=feedback,
        label=label,  # type: ignore[arg-type]
    )


def _result(
    key: str,
    item_id: int,
    signal: int | None,
    *,
    junk: bool = False,
    ran: bool = True,
    cost: float,
    latency: int,
) -> CompareResult:
    output = (
        Verdict(item_id=item_id, signal=signal, junk=junk, category=Category.PRODUCT).model_dump(
            mode="json"
        )
        if signal is not None
        else None
    )
    return CompareResult(
        model_key=key,
        subject_id=item_id,
        output=output,
        ok=output is not None,
        latency_ms=latency if ran else None,
        cost_usd=cost if ran else 0.0,
    )


ITEMS = {
    1: _item(1, production=7, feedback=1, label="important"),  # production: shown
    2: _item(2, production=2, feedback=-1, label="junk"),  # production: hidden
    3: _item(3, weight=0.5, production=6, label="show"),  # 6 x 0.5 = 3: hidden
    4: _item(4),  # never judged in production, no thumb, no label
}
RESULTS = [
    # A: 1 shown, 2 junk (hidden), 3 shown (10 x 0.5 = 5), 4 failed.
    _result(A.key, 1, 8, cost=0.001, latency=1000),
    _result(A.key, 2, 5, junk=True, cost=0.001, latency=1000),
    _result(A.key, 3, 10, cost=0.001, latency=1000),
    _result(A.key, 4, None, cost=0.001, latency=1000),
    # B: 1 hidden, 2 hidden, 3 not run, 4 shown.
    _result(B.key, 1, 3, cost=0.002, latency=2000),
    _result(B.key, 2, 1, cost=0.002, latency=2000),
    _result(B.key, 3, None, ran=False, cost=0.0, latency=0),
    _result(B.key, 4, 6, cost=0.002, latency=2000),
]


def test_judge_summary_by_hand() -> None:
    a, b = compare.judge_summary([A, B], ITEMS, RESULTS, threshold=4)
    assert (a.key, a.provider, a.model) == (A.key, "anthropic", "claude-haiku-5-5")
    assert (a.shown, a.hidden, a.junk, a.failures, a.not_run) == (2, 1, 1, 1, 0)
    assert a.avg_signal == pytest.approx(23 / 3)
    assert (a.agree_production, a.production_n) == (pytest.approx(2 / 3), 3)
    assert (a.agree_thumbs, a.thumbs_n) == (1.0, 2)
    assert (a.accuracy, a.labels_n) == (1.0, 3)  # important counts double: 4 of 4
    assert a.cost_usd == pytest.approx(0.004)
    assert a.avg_latency_ms == 1000

    assert (b.shown, b.hidden, b.junk, b.failures, b.not_run) == (1, 2, 0, 0, 1)
    assert b.avg_signal == pytest.approx(10 / 3)
    assert (b.agree_production, b.production_n) == (0.5, 2)  # item 4 has no production verdict
    assert (b.agree_thumbs, b.thumbs_n) == (0.5, 2)
    assert (b.accuracy, b.labels_n) == (pytest.approx(1 / 3), 2)  # 1 of 3 (important missed)
    assert b.cost_usd == pytest.approx(0.006)
    assert b.avg_latency_ms == 2000


def test_a_model_with_nothing_to_compare_has_no_rates() -> None:
    [a] = compare.judge_summary([A], {4: ITEMS[4]}, RESULTS[3:4], threshold=4)
    assert (a.avg_signal, a.agree_production, a.agree_thumbs, a.accuracy) == (None,) * 4
    assert (a.production_n, a.thumbs_n, a.labels_n) == (0, 0, 0)


def test_the_shown_rule_is_productions() -> None:
    v = Verdict(item_id=1, signal=8, junk=False, category=Category.RESEARCH)
    assert compare.is_shown(v, 1.0, 8) and not compare.is_shown(v, 0.9, 8)
    junk = v.model_copy(update={"junk": True})
    assert compare.is_shown(junk, 1.0, 0)  # a 0 threshold shows everything, as on Today
    assert not compare.is_shown(junk, 1.0, 1)


def test_item_rows_flag_disagreements() -> None:
    rows = {r.item.item_id: r for r in compare.judge_items([A, B], ITEMS, RESULTS, threshold=4)}
    assert [r.item.item_id for r in compare.judge_items([A, B], ITEMS, RESULTS, 4)] == [1, 2, 3, 4]
    assert rows[1].disagree and not rows[2].disagree
    assert not rows[3].disagree  # B did not run: nothing to disagree with
    assert not rows[4].disagree  # A failed
    assert [c.state for c in rows[3].cells] == ["ok", "not_run"]
    assert [c.state for c in rows[4].cells] == ["failed", "ok"]
    assert rows[1].production_shown is True and rows[4].production_shown is None
    assert rows[1].cells[0].verdict is not None and rows[1].cells[0].verdict.signal == 8


def test_the_best_judge_is_by_label_accuracy_then_production_agreement() -> None:
    rows = compare.judge_summary([A, B], ITEMS, RESULTS, threshold=4)
    assert compare.best_judge(rows) == A.key
    unlabelled = {k: v.model_copy(update={"label": None}) for k, v in ITEMS.items()}
    rows = compare.judge_summary([B, A], unlabelled, RESULTS, threshold=4)
    assert compare.best_judge(rows) == A.key  # 2/3 agreement beats 1/2
    bare = {k: v.model_copy(update={"label": None, "verdict": None}) for k, v in ITEMS.items()}
    assert compare.best_judge(compare.judge_summary([A, B], bare, RESULTS, 4)) is None


def test_writer_summary_tallies_preferences() -> None:
    def story(key: str, sid: int, ok: bool, ran: bool = True) -> CompareResult:
        text = StoryText(story_id=sid, summary="S.") if ok else None
        return CompareResult(
            model_key=key,
            subject_id=sid,
            output=text.model_dump(mode="json") if text else None,
            ok=ok,
            latency_ms=500 if ran else None,
            cost_usd=0.01 if ran else 0.0,
        )

    results = [story(A.key, 10, True), story(A.key, 11, False), story(B.key, 10, True)]
    results.append(story(B.key, 11, False, ran=False))
    a, b = compare.writer_summary([A, B], results, {10: A.key, 11: A.key})
    assert (a.written, a.failures, a.not_run, a.preferred) == (1, 1, 0, 2)
    assert (b.written, b.failures, b.not_run, b.preferred) == (1, 0, 1, 0)
    assert a.cost_usd == pytest.approx(0.02) and a.avg_latency_ms == 500
    assert compare.best_writer([a, b]) == A.key
    assert compare.best_writer(compare.writer_summary([A, B], results, {})) is None
    rows = compare.writer_stories([A, B], [10, 11], results)
    assert [r.story_id for r in rows] == [10, 11]
    first = rows[0].texts[0]
    assert first is not None and first.summary == "S."
    assert rows[1].texts == [None, None]
