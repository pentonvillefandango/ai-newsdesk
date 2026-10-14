"""Anthropic's API behind ModelClient: the exact call the judge, writer and consolidator make."""

from __future__ import annotations

from typing import Any, Literal

import anthropic
from anthropic.types import OutputConfigParam, TextBlockParam
from pydantic import BaseModel

from ai_newsdesk.llm.base import StructuredResult, failed_call, prompt_tokens
from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.models import Purpose, Usage

Effort = Literal["low", "medium", "high", "xhigh", "max"]


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


class AnthropicClient:
    """`messages.parse` with a pydantic output format, no temperature and no prefill.

    `effort` is the output_config effort ("low" for judging, "medium" for the briefing);
    `cache_system` marks the system block for prompt caching (off for the one-off
    consolidation call).
    """

    provider = "anthropic"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        prices: PriceBook,
        *,
        effort: Effort = "low",
        cache_system: bool = True,
    ) -> None:
        self._client = client
        self.model = model
        self._prices = prices
        self.effort: Effort = effort
        self.cache_system = cache_system

    def worst_case_usd(self, *, system: str, user: str, max_tokens: int) -> float:
        return self._prices.worst_case_usd(
            self.provider, self.model, prompt_tokens(system, user), max_tokens
        )

    async def structured[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, purpose: Purpose
    ) -> StructuredResult[T]:
        block: TextBlockParam = {"type": "text", "text": system}
        if self.cache_system:
            block["cache_control"] = {"type": "ephemeral"}
        config: OutputConfigParam = {"effort": self.effort}
        try:
            response = await self._client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=[block],
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                output_config=config,
            )
        except Exception as exc:  # never raises: the caller's items fail
            return failed_call(
                exc,
                self.provider,
                self.model,
                purpose,
                lambda: self.worst_case_usd(system=system, user=user, max_tokens=max_tokens),
            )
        usage = usage_from_response(response, self.model, purpose)
        if response.stop_reason == "refusal":
            return StructuredResult(usage=usage, refused=True, error="refused")
        parsed = response.parsed_output
        if parsed is None:
            return StructuredResult(
                usage=usage, error=f"no usable output (stop_reason={response.stop_reason})"
            )
        return StructuredResult(parsed=parsed, usage=usage)
