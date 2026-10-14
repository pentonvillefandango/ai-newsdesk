"""PriceBook: (provider, model) -> prices, the worst case and the cost of a recorded call."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_newsdesk.llm.catalogue import Catalogue, CatalogueModel
from ai_newsdesk.llm.prices import Price, PriceBook
from ai_newsdesk.models import Purpose, Usage
from ai_newsdesk.pricing import ModelPrice, Pricing, UnknownModelError
from tests.conftest import ROOT

PRICING = Pricing.load(ROOT / "config" / "pricing.yaml")
CATALOGUE = Catalogue.build(
    [
        CatalogueModel(
            id="openai/gpt-5.4-nano",
            name="GPT-5.4 Nano",
            input_per_mtok=0.2,
            output_per_mtok=1.25,
            context_length=400_000,
        ),
        CatalogueModel(
            id="openai/gpt-6-luna",
            name="GPT-6 Luna",
            input_per_mtok=0.2,
            output_per_mtok=0.75,
            context_length=400_000,
        ),
        CatalogueModel(
            id="google/gemini-3.8-flash",
            name="Gemini 3.8 Flash",
            input_per_mtok=0.75,
            output_per_mtok=3.75,
            context_length=None,
        ),
    ],
    fetched_at=datetime(2026, 10, 14, 6, 0, tzinfo=UTC),
)


def book(pricing: Pricing = PRICING, catalogue: Catalogue | None = CATALOGUE) -> PriceBook:
    return PriceBook(pricing, lambda: catalogue)


def usage(provider: str, model: str, **tokens: int) -> Usage:
    return Usage(provider=provider, model=model, purpose=Purpose.REVIEW, **tokens)


def test_anthropic_and_typesafe_come_from_pricing_yaml() -> None:
    assert book().price("anthropic", "claude-haiku-5-5") == Price(0.10, 0.50)
    assert book().price("typesafe", "jev-1.13.0") == Price(0.042, 0.0)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("anthropic", "claude-unknown"),
        ("anthropic", "jev-1.13.0"),  # listed, but for another provider
        ("anthropic", "anthropic/claude-haiku-5.5"),  # catalogue ids never price Anthropic
        ("typesafe", "claude-haiku-5-5"),
        ("openai", "gpt-unknown"),
        ("openai", "claude-haiku-5-5"),
        ("openrouter", "nobody/unknown"),
        ("openrouter", "claude-haiku-5-5"),  # pricing.yaml never prices OpenRouter
        ("someone-else", "gpt-5.4-nano"),
    ],
)
def test_unknown_models_raise(provider: str, model: str) -> None:
    with pytest.raises(UnknownModelError):
        book().price(provider, model)


def test_openai_falls_back_to_the_catalogue() -> None:
    assert book().price("openai", "gpt-5.4-nano") == Price(0.2, 1.25)


def test_openai_prefers_pricing_yaml() -> None:
    models = {
        "gpt-5.4-nano": ModelPrice(provider="openai", input_per_mtok=0.1, output_per_mtok=1.0)
    }
    assert book(Pricing(models, "2026-10-14")).price("openai", "gpt-5.4-nano") == Price(0.1, 1.0)


def test_openrouter_uses_the_catalogue() -> None:
    assert book().price("openrouter", "google/gemini-3.8-flash") == Price(0.75, 3.75)


def test_without_a_catalogue_only_pricing_yaml_models_are_priced() -> None:
    nothing = book(catalogue=None)
    assert nothing.price("anthropic", "claude-haiku-5-5") == Price(0.10, 0.50)
    for provider, model in [("openai", "gpt-5.4-nano"), ("openrouter", "google/gemini-3.8-flash")]:
        with pytest.raises(UnknownModelError):
            nothing.price(provider, model)


def test_the_catalogue_is_read_at_lookup_time() -> None:
    current: list[Catalogue | None] = [None]
    prices = PriceBook(PRICING, lambda: current[0])
    assert prices.is_priced("openrouter", "google/gemini-3.8-flash") is False
    current[0] = CATALOGUE
    assert prices.is_priced("openrouter", "google/gemini-3.8-flash") is True


def test_worst_case_formula() -> None:
    worst = book().worst_case_usd("openrouter", "google/gemini-3.8-flash", 10_000, 2_000)
    assert worst == pytest.approx((10_000 * 0.75 * 1.25 + 2_000 * 3.75) / 1_000_000)


def test_anthropic_usage_cost_matches_pricing_yaml() -> None:
    u = usage(
        "anthropic",
        "claude-haiku-5-5",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
    )
    assert book().usage_cost(u) == pytest.approx(PRICING.usage_cost(u))


def test_openai_cache_reads_are_priced_conservatively() -> None:
    # OpenAI-style providers discount cached input by different amounts; count it at full price.
    u = usage("openai", "gpt-5.4-nano", input_tokens=400, cache_read_tokens=600, output_tokens=100)
    assert book().usage_cost(u) == pytest.approx((1000 * 0.2 + 100 * 1.25) / 1_000_000)


def test_usage_cost_of_an_unknown_model_raises() -> None:
    with pytest.raises(UnknownModelError):
        book().usage_cost(usage("openrouter", "nobody/unknown", input_tokens=1))


def test_pricing_yaml_accepts_openai_entries() -> None:
    price = ModelPrice(provider="openai", input_per_mtok=1, output_per_mtok=2)
    assert Pricing({"gpt-x": price}, "2026-10-14").models("openai") == ["gpt-x"]
