"""The provider-neutral model client contract and the helpers every client shares."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import anthropic
import openai
from pydantic import BaseModel

from ai_newsdesk.models import Purpose, Usage

# Structured output adds a schema-derived preamble to the input. Bytes bound tokens, so this
# covers any production schema whose strict JSON is at most this many bytes (tested).
SCHEMA_OVERHEAD_TOKENS = 1500


def estimate_tokens(text: str) -> int:
    """Upper bound on tokens for budget checks: a token covers at least one UTF-8 byte."""
    return len(text.encode("utf-8"))


def safe_json(payload: object) -> str:
    """JSON for embedding untrusted text inside <item>/<story> tags: no raw angle brackets."""
    return json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")


def prompt_tokens(system: str, user: str) -> int:
    """The input-token upper bound used for every worst case (spec 3.2)."""
    return estimate_tokens(system + user) + SCHEMA_OVERHEAD_TOKENS


def describe_error(exc: BaseException) -> str:
    """A log-safe description of a failed model call (never the request, the body or the key)."""
    if isinstance(exc, anthropic.RateLimitError | openai.RateLimitError):
        return "rate limited (429) after SDK retries"
    if isinstance(exc, anthropic.APIStatusError | openai.APIStatusError):
        return f"HTTP {exc.status_code} ({type(exc).__name__})"
    if isinstance(exc, anthropic.APIConnectionError | openai.APIConnectionError):
        return f"connection error ({type(exc).__name__})"
    return type(exc).__name__


def abandoned_usage(provider: str, model: str, purpose: Purpose, worst_case_usd: float) -> Usage:
    """The spend row for a call that may have been billed with no usage known (we abandoned it,
    or it failed after the request may have reached the provider): it costs its worst case."""
    return Usage(
        provider=provider,
        model=model,
        purpose=purpose,
        input_tokens=0,
        reported_cost_usd=worst_case_usd,
    )


def may_be_billed(exc: BaseException) -> bool:
    """False only when the provider certainly charged nothing: it answered with a 4xx (bad key,
    bad request, rate limit) other than 408. A timeout, a dropped connection, a 5xx or anything
    unexpected may have been billed, so it counts."""
    if isinstance(exc, anthropic.APIStatusError | openai.APIStatusError):
        return exc.status_code >= 500 or exc.status_code == 408
    return True


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    """One structured call. `parsed` is set only for schema-valid, non-refused output; `usage`
    is set whenever the provider answered (a refused or invalid answer is still paid for)."""

    parsed: T | None = None
    usage: Usage | None = None
    refused: bool = False
    error: str | None = None  # short and scrubbed


class ModelClient(Protocol):
    provider: str  # "anthropic" | "openai" | "openrouter"
    model: str

    async def structured[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, purpose: Purpose
    ) -> StructuredResult[T]: ...

    def worst_case_usd(self, *, system: str, user: str, max_tokens: int) -> float: ...


def failed_call[T: BaseModel](
    exc: BaseException, provider: str, model: str, purpose: Purpose, worst_case: Callable[[], float]
) -> StructuredResult[T]:
    """A client's result for a call that raised: scrubbed, and charged its worst case once when
    it may have been billed (the meter can over-count but never under-count). A model with no
    price could never have passed the budget pre-check, so it is never charged here."""
    usage: Usage | None = None
    if may_be_billed(exc):
        try:
            usage = abandoned_usage(provider, model, purpose, worst_case())
        except KeyError:  # pricing.UnknownModelError
            usage = None
    return StructuredResult(usage=usage, error=describe_error(exc))
