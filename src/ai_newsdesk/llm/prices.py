"""Provider-aware prices (spec 3.2): (provider, model) -> USD per million tokens.

- anthropic, typesafe: config/pricing.yaml, as before.
- openai: pricing.yaml when listed with provider openai, else the catalogue's `openai/<model>`.
- openrouter: the catalogue only.

A model with no price from these sources is unusable: every lookup raises UnknownModelError.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ai_newsdesk.llm.catalogue import Catalogue
from ai_newsdesk.models import Usage
from ai_newsdesk.pricing import CACHE_WRITE_MULTIPLIER, Pricing, UnknownModelError

_PER_TOKEN = 1_000_000
_YAML_PROVIDERS = ("anthropic", "typesafe")
_CATALOGUE_PROVIDERS = ("openai", "openrouter")


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float


class PriceBook:
    def __init__(
        self, pricing: Pricing, catalogue: Callable[[], Catalogue | None] = lambda: None
    ) -> None:
        self.pricing = pricing
        self._catalogue = catalogue  # read on every lookup, so a refresh applies at once

    def _yaml(self, provider: str, model: str) -> Price | None:
        try:
            p = self.pricing.price(model)
        except UnknownModelError:
            return None
        if p.provider != provider:
            return None
        return Price(p.input_per_mtok, p.output_per_mtok)

    def _from_catalogue(self, model_id: str) -> Price | None:
        catalogue = self._catalogue()
        entry = catalogue.get(model_id) if catalogue is not None else None
        if entry is None:
            return None
        return Price(entry.input_per_mtok, entry.output_per_mtok)

    def price(self, provider: str, model: str) -> Price:
        found: Price | None = None
        if provider in _YAML_PROVIDERS:
            found = self._yaml(provider, model)
        elif provider == "openai":
            found = self._yaml("openai", model) or self._from_catalogue(f"openai/{model}")
        elif provider == "openrouter":
            found = self._from_catalogue(model)
        if found is None:
            raise UnknownModelError(f"{provider}:{model}")
        return found

    def is_priced(self, provider: str, model: str) -> bool:
        try:
            self.price(provider, model)
        except UnknownModelError:
            return False
        return True

    def worst_case_usd(
        self, provider: str, model: str, input_tokens: int, max_tokens: int
    ) -> float:
        """All input at the cache-write rate (1.25x) plus `max_tokens` of output."""
        p = self.price(provider, model)
        return (
            input_tokens * p.input_per_mtok * CACHE_WRITE_MULTIPLIER
            + max_tokens * p.output_per_mtok
        ) / _PER_TOKEN

    def usage_cost(self, usage: Usage) -> float:
        """USD for a recorded call, from our prices (the provider's reported cost is the
        Budget's business)."""
        if usage.provider not in _CATALOGUE_PROVIDERS:
            self.price(usage.provider, usage.model)  # provider must match the yaml entry
            return self.pricing.usage_cost(usage)
        tokens = (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
        if min(tokens) < 0:
            raise ValueError("token counts cannot be negative")
        p = self.price(usage.provider, usage.model)
        # Cache-read discounts differ by provider and model: count cached input at full price.
        input_equiv = (
            usage.input_tokens
            + usage.cache_read_tokens
            + usage.cache_write_tokens * CACHE_WRITE_MULTIPLIER
        )
        return (input_equiv * p.input_per_mtok + usage.output_tokens * p.output_per_mtok) / (
            _PER_TOKEN
        )
