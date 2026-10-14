"""Helpers shared by the Claude judge and writer. The shared pieces now live in
`ai_newsdesk.llm`; this module re-exports them for existing imports."""

from __future__ import annotations

from ai_newsdesk.llm.anthropic_client import usage_from_response
from ai_newsdesk.llm.base import (
    SCHEMA_OVERHEAD_TOKENS,
    describe_error,
    estimate_tokens,
    safe_json,
)
from ai_newsdesk.pricing import Pricing

__all__ = [
    "SCHEMA_OVERHEAD_TOKENS",
    "describe_error",
    "estimate_tokens",
    "safe_json",
    "usage_from_response",
    "worst_case_usd",
]


def worst_case_usd(pricing: Pricing, model: str, input_tokens: int, max_tokens: int) -> float:
    """Upper bound for one call: all input at the highest input rate (cache write, 1.25x)."""
    return pricing.cost(model, 0, max_tokens, cache_write_tokens=input_tokens)
