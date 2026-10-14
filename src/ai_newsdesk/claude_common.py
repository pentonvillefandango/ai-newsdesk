"""Helpers shared by the Claude judge and writer."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from ai_newsdesk.models import Purpose, Usage
from ai_newsdesk.pricing import Pricing

# Structured output adds a schema-derived preamble to the input; count it generously.
SCHEMA_OVERHEAD_TOKENS = 600


def estimate_tokens(text: str) -> int:
    """Upper bound on tokens for budget checks: a token covers at least one UTF-8 byte."""
    return len(text.encode("utf-8"))


def worst_case_usd(pricing: Pricing, model: str, input_tokens: int, max_tokens: int) -> float:
    """Upper bound for one call: all input at the highest input rate (cache write, 1.25x)."""
    return pricing.cost(model, 0, max_tokens, cache_write_tokens=input_tokens)


def safe_json(payload: object) -> str:
    """JSON for embedding untrusted text inside <item>/<story> tags: no raw angle brackets."""
    return json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")


def usage_from_response(response: Any, model: str, purpose: Purpose) -> Usage:
    u = response.usage
    return Usage(
        provider="anthropic",
        model=model,
        purpose=purpose,
        input_tokens=int(u.input_tokens or 0),
        output_tokens=int(u.output_tokens or 0),
        cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    )


def describe_error(exc: BaseException) -> str:
    """A log-safe description of a failed Claude call (never the request body or the key)."""
    if isinstance(exc, anthropic.RateLimitError):
        return "rate limited (429) after SDK retries"
    if isinstance(exc, anthropic.APIStatusError):
        return f"HTTP {exc.status_code} ({type(exc).__name__})"
    if isinstance(exc, anthropic.APIConnectionError):
        return f"connection error ({type(exc).__name__})"
    return type(exc).__name__
