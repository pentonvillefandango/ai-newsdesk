"""The provider-neutral model client contract and the helpers every client shares."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import anthropic
import openai
from pydantic import BaseModel

from ai_newsdesk.models import Purpose, Usage

# Structured output adds a schema-derived preamble to the input; count it generously.
SCHEMA_OVERHEAD_TOKENS = 600


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
