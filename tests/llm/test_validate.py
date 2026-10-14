"""validate_model_choice and missing_key_error: the one check used by Settings and Compare."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_newsdesk.llm.catalogue import Catalogue, CatalogueModel
from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.llm.validate import (
    PROVIDER_LABELS,
    missing_key_error,
    validate_model_choice,
)
from ai_newsdesk.pricing import Pricing
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
            id="google/gemini-3.8-flash",
            name="Gemini 3.8 Flash",
            input_per_mtok=0.75,
            output_per_mtok=3.75,
            context_length=None,
        ),
        CatalogueModel(
            id="unbiased/pareto-26.9",  # priced and structured, but not on the shortlist
            name="Pareto",
            input_per_mtok=0.1,
            output_per_mtok=0.4,
            context_length=None,
        ),
    ],
    fetched_at=datetime(2026, 10, 14, 6, 0, tzinfo=UTC),
)
PRICES = PriceBook(PRICING, lambda: CATALOGUE)


def check(provider: str, model: str, catalogue: Catalogue | None = CATALOGUE) -> str | None:
    prices = PriceBook(PRICING, lambda: catalogue)
    return validate_model_choice(provider, model, pricebook=prices, catalogue=catalogue)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("anthropic", "claude-haiku-5-5"),
        ("anthropic", "claude-opus-5-5"),
        ("openai", "gpt-5.4-nano"),  # priced through the catalogue's openai/ entry
        ("openrouter", "google/gemini-3.8-flash"),  # shortlisted
        ("openrouter", "unbiased/pareto-26.9"),  # manual entry from the catalogue
        ("openrouter", "openai/gpt-5.4-nano"),
    ],
)
def test_usable_choices(provider: str, model: str) -> None:
    assert check(provider, model) is None


@pytest.mark.parametrize(
    ("provider", "model", "message"),
    [
        ("anthropic", "claude-unknown", "Choose a Claude model from the list."),
        ("anthropic", "jev-1.13.0", "Choose a Claude model from the list."),  # another provider's
        ("anthropic", "anthropic/claude-haiku-5.5", "Choose a Claude model from the list."),
        ("openai", "gpt-unknown", "OpenAI model has no known price"),
        ("openai", "claude-haiku-5-5", "OpenAI model has no known price"),
        ("openai", "openai/gpt-5.4-nano", "OpenAI model has no known price"),  # no prefix
        ("openrouter", "nobody/unknown", "isn't in the OpenRouter model list"),
        ("openrouter", "nvidia/nemotron-3.5-lightning:free", "isn't in the OpenRouter model list"),
        ("openrouter", "gpt-5.4-nano", "isn't in the OpenRouter model list"),
        ("openrouter", "claude-haiku-5-5", "isn't in the OpenRouter model list"),
    ],
)
def test_unusable_choices(provider: str, model: str, message: str) -> None:
    error = check(provider, model)
    assert error is not None and message in error


@pytest.mark.parametrize("model", ["", "   ", "has space", "x" * 201, "tab\there"])
def test_malformed_model_ids(model: str) -> None:
    assert check("openrouter", model) == "Enter a model id: no spaces, at most 200 characters."


@pytest.mark.parametrize("provider", ["typesafe", "google", ""])
def test_unknown_providers(provider: str) -> None:
    assert check(provider, "claude-haiku-5-5") == "Choose a provider from the list."


def test_without_a_catalogue_only_pricing_yaml_models_are_usable() -> None:
    assert check("anthropic", "claude-haiku-5-5", None) is None
    assert check("openai", "gpt-5.4-nano", None) is not None
    error = check("openrouter", "google/gemini-3.8-flash", None)
    assert error is not None and "model list hasn't loaded" in error


def test_missing_key_error_names_the_key() -> None:
    assert missing_key_error("openrouter", set()) == "Add your OpenRouter API key first."
    assert missing_key_error("openai", {"anthropic"}) == "Add your OpenAI API key first."
    assert missing_key_error("openai", {"openai"}) is None
    assert missing_key_error("openrouter", {"openrouter", "openai"}) is None


def test_anthropic_never_needs_a_key_to_save() -> None:
    """Today's behaviour: the Claude default works before a key is added (calls fail with 401)."""
    assert missing_key_error("anthropic", set()) is None


def test_provider_labels() -> None:
    assert PROVIDER_LABELS == {
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "openrouter": "OpenRouter",
    }
