"""Model price table from config/pricing.yaml (V1-S1). One table for every provider."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from ai_newsdesk.models import Usage

DEFAULT_PRICING_PATH = Path("config/pricing.yaml")
# Anthropic prompt caching (5-minute cache), as multiples of the input price.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10
_PER_TOKEN = 1_000_000


class UnknownModelError(KeyError):
    """The model ID is not in pricing.yaml."""


class ModelPrice(BaseModel):
    provider: Literal["anthropic", "typesafe"]
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class _PricingFile(BaseModel):
    as_of: str
    models: dict[str, ModelPrice] = Field(min_length=1)


class Pricing:
    def __init__(self, models: dict[str, ModelPrice], as_of: str) -> None:
        self._models = dict(models)
        self.as_of = as_of

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PRICING_PATH) -> "Pricing":
        data = _PricingFile.model_validate(yaml.safe_load(Path(path).read_text()))
        return cls(data.models, data.as_of)

    def price(self, model: str) -> ModelPrice:
        try:
            return self._models[model]
        except KeyError:
            raise UnknownModelError(model) from None

    def models(self, provider: str | None = None) -> list[str]:
        """Model IDs, sorted; only `provider`'s when given."""
        return sorted(
            name for name, p in self._models.items() if provider is None or p.provider == provider
        )

    def cost(
        self,
        model: str,
        in_tokens: int,
        out_tokens: int,
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """USD for one call. `in_tokens` excludes cached tokens, as the Anthropic API reports."""
        if min(in_tokens, out_tokens, cache_read_tokens, cache_write_tokens) < 0:
            raise ValueError("token counts cannot be negative")
        p = self.price(model)
        input_equiv = (
            in_tokens
            + cache_write_tokens * CACHE_WRITE_MULTIPLIER
            + cache_read_tokens * CACHE_READ_MULTIPLIER
        )
        return (input_equiv * p.input_per_mtok + out_tokens * p.output_per_mtok) / _PER_TOKEN

    def usage_cost(self, usage: Usage) -> float:
        return self.cost(
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
