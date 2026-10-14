"""Helpers shared by the Claude judge and writer."""

from __future__ import annotations

import json
import math
from typing import Any

import anthropic

from ai_newsdesk.models import Purpose, Usage

# Structured output adds a schema-derived preamble to the input; count it generously.
SCHEMA_OVERHEAD_TOKENS = 600


def estimate_tokens(text: str) -> int:
    """Pessimistic token estimate (2 characters per token) for budget pre-checks."""
    return math.ceil(len(text) / 2) + 1


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
