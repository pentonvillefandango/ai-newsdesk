# tests/test_budget.py
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_newsdesk.budget import Budget
from ai_newsdesk.clock import FixedClock
from ai_newsdesk.llm.catalogue import Catalogue, CatalogueModel
from ai_newsdesk.llm.prices import PriceBook
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


# ---- multi-provider: prices by (provider, model) and the provider's reported cost ----------

CATALOGUE = Catalogue.build(
    [
        CatalogueModel(
            id="google/gemini-3.8-flash",
            name="Gemini 3.8 Flash",
            input_per_mtok=0.75,
            output_per_mtok=3.75,
        ),
        CatalogueModel(
            id="openai/gpt-5.4-nano", name="GPT-5.4 Nano", input_per_mtok=0.2, output_per_mtok=1.25
        ),
    ],
    fetched_at=NOW,
)


def make_with_catalogue(store: Store, **runtime: object) -> Budget:
    return Budget(
        store,
        PRICING,
        FixedClock(NOW),
        RuntimeSettings(**runtime),  # type: ignore[arg-type]
        tz=TZ,
        prices=PriceBook(PRICING, lambda: CATALOGUE),
    )


def other_usage(provider: str, model: str, *, reported: float | None = None) -> Usage:
    return Usage(
        provider=provider,
        model=model,
        purpose=Purpose.WRITE,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        reported_cost_usd=reported,
    )


async def test_cost_prices_by_provider(store: Store) -> None:
    budget = make_with_catalogue(store)
    assert budget.cost(other_usage("openrouter", "google/gemini-3.8-flash")) == pytest.approx(4.5)
    assert budget.cost(other_usage("openai", "gpt-5.4-nano")) == pytest.approx(1.45)
    assert budget.cost(other_usage("anthropic", "claude-haiku-5-5")) == pytest.approx(0.6)


async def test_reported_cost_wins(store: Store) -> None:
    budget = make_with_catalogue(store)
    u = other_usage("openrouter", "google/gemini-3.8-flash", reported=0.0123)
    assert budget.cost(u) == pytest.approx(0.0123)
    # Even for a model we could not price ourselves.
    assert budget.cost(other_usage("openrouter", "x/y", reported=0.5)) == pytest.approx(0.5)


async def test_unknown_provider_model_is_free_with_a_warning(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    budget = make_with_catalogue(store)
    assert budget.cost(other_usage("openrouter", "nobody/unknown")) == 0.0
    assert "nobody/unknown" in caplog.text


async def test_default_price_book_has_no_catalogue(store: Store) -> None:
    budget = make(store)
    assert budget.cost(other_usage("openrouter", "google/gemini-3.8-flash")) == 0.0
    assert budget.cost(other_usage("anthropic", "claude-haiku-5-5")) == pytest.approx(0.6)


async def test_record_persists_an_openrouter_call(store: Store) -> None:
    budget = make_with_catalogue(store, monthly_budget_usd=5.0)
    await budget.refresh()
    cost = await budget.record(other_usage("openrouter", "google/gemini-3.8-flash", reported=0.2))
    assert cost == pytest.approx(0.2)
    assert await store.month_spend(NOW, TZ) == pytest.approx(0.2)
    assert budget.remaining_month() == pytest.approx(4.8)
