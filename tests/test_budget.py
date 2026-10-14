# tests/test_budget.py
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_newsdesk.budget import Budget
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.models import Purpose, Usage
from ai_newsdesk.pricing import Pricing
from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.store.store import Store

PRICING = Pricing.load(Path(__file__).resolve().parents[1] / "config" / "pricing.yaml")
MODEL = "claude-haiku-5-5"
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
TZ = "Europe/London"


def usage(inp: int, out: int = 0, *, cache_read: int = 0, cache_write: int = 0) -> Usage:
    return Usage(
        provider="anthropic",
        model=MODEL,
        purpose=Purpose.REVIEW,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def make(store: Store, **runtime: object) -> Budget:
    return Budget(store, PRICING, FixedClock(NOW), RuntimeSettings(**runtime), tz=TZ)  # type: ignore[arg-type]


async def test_fails_closed_before_refresh(store: Store) -> None:
    budget = make(store)
    assert budget.can_afford(0.0001) is False
    await budget.refresh()
    assert budget.can_afford(0.0001) is True


async def test_can_afford_compares_worst_case_with_remaining(store: Store) -> None:
    budget = make(store, monthly_budget_usd=5.0)
    await budget.refresh()
    assert budget.remaining_month() == pytest.approx(5.0)
    assert budget.can_afford(5.0) is True
    assert budget.can_afford(5.01) is False
    assert budget.can_afford(-1.0) is False


async def test_zero_budget_affords_nothing(store: Store) -> None:
    budget = make(store, monthly_budget_usd=0.0)
    await budget.refresh()
    assert budget.can_afford(0.0) is False


async def test_record_prices_and_persists(store: Store) -> None:
    budget = make(store, monthly_budget_usd=5.0)
    await budget.refresh()
    cost = await budget.record(usage(1_000_000, 0))
    assert cost == pytest.approx(0.10)
    assert budget.remaining_month() == pytest.approx(4.90)
    assert await store.month_spend(NOW, TZ) == pytest.approx(0.10)


async def test_cache_tokens_are_priced(store: Store) -> None:
    budget = make(store)
    assert budget.cost(usage(0, cache_write=1_000_000)) == pytest.approx(0.125)
    assert budget.cost(usage(0, cache_read=1_000_000)) == pytest.approx(0.01)
    assert budget.cost(usage(0, 1_000_000)) == pytest.approx(0.50)


async def test_refresh_reads_existing_spend(store: Store) -> None:
    await store.add_spend(usage(10_000_000), 1.0, NOW)
    budget = make(store, monthly_budget_usd=1.5)
    await budget.refresh()
    assert budget.remaining_month() == pytest.approx(0.5)
    assert budget.can_afford(0.6) is False


async def test_remaining_reviews_today(store: Store) -> None:
    budget = make(store, daily_review_cap=500)
    await budget.refresh()
    assert budget.remaining_reviews_today() == 500
    budget.note_reviewed(20)
    assert budget.remaining_reviews_today() == 480
    budget.note_reviewed(1000)
    assert budget.remaining_reviews_today() == 0


@pytest.mark.parametrize("seed", range(20))
async def test_spend_never_exceeds_budget(store: Store, seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not crypto
    cap = rng.uniform(0.001, 0.05)
    budget = make(store, monthly_budget_usd=cap)
    await budget.refresh()
    for _ in range(40):
        if rng.random() < 0.2:
            await budget.refresh()
        inp = rng.randint(100, 50_000)
        max_out = rng.randint(100, 4_000)
        worst = PRICING.cost(MODEL, inp, max_out)
        if budget.can_afford(worst):
            await budget.record(usage(inp, rng.randint(0, max_out)))
    assert await store.month_spend(NOW, TZ) <= cap + 1e-12
