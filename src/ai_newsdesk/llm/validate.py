"""Is a (provider, model) choice usable? One check for Settings, Compare and the wiring
(spec 3.4). Each function returns a short message for the owner, or None when all is well.

- anthropic: the model must be in pricing.yaml (as an Anthropic model).
- openai: priced by pricing.yaml or by the catalogue's `openai/<id>` entry.
- openrouter: in the filtered catalogue (priced, structured output, not `:free`).
"""

from __future__ import annotations

from collections.abc import Collection

from ai_newsdesk.llm.catalogue import Catalogue
from ai_newsdesk.llm.prices import PriceBook

PROVIDER_LABELS = {"anthropic": "Anthropic", "openai": "OpenAI", "openrouter": "OpenRouter"}
MAX_MODEL_ID = 200
# Providers whose key must resolve before a job may use them. Anthropic is the default that
# works before any key is added (its calls then fail with 401), as it always has.
KEYED_PROVIDERS = ("openai", "openrouter")

PROVIDER_ERROR = "Choose a provider from the list."
MODEL_ID_ERROR = f"Enter a model id: no spaces, at most {MAX_MODEL_ID} characters."
CLAUDE_ERROR = "Choose a Claude model from the list."
OPENAI_ERROR = "This OpenAI model has no known price, so it can't be used."
OPENROUTER_ERROR = "This model isn't in the OpenRouter model list."
NO_CATALOGUE_ERROR = "The OpenRouter model list hasn't loaded yet. Try again in a minute."


def validate_model_choice(
    provider: str, model: str, *, pricebook: PriceBook, catalogue: Catalogue | None
) -> str | None:
    if provider not in PROVIDER_LABELS:
        return PROVIDER_ERROR
    if not model or len(model) > MAX_MODEL_ID or any(ch.isspace() for ch in model):
        return MODEL_ID_ERROR
    if provider == "anthropic":
        return None if pricebook.is_priced("anthropic", model) else CLAUDE_ERROR
    if provider == "openai":
        return None if pricebook.is_priced("openai", model) else OPENAI_ERROR
    if catalogue is None:
        return NO_CATALOGUE_ERROR
    return None if catalogue.get(model) is not None else OPENROUTER_ERROR


def missing_key_error(provider: str, api_keys: Collection[str]) -> str | None:
    """`api_keys` holds the names of the keys that resolve (UI or `.env`)."""
    if provider in KEYED_PROVIDERS and provider not in api_keys:
        return f"Add your {PROVIDER_LABELS[provider]} API key first."
    return None
