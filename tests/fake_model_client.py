"""A stand-in ModelClient: records structured() calls and replays scripted results."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from ai_newsdesk.llm.base import StructuredResult, prompt_tokens
from ai_newsdesk.models import Purpose, Usage


class FakeModelClient:
    """`script` holds, per call, a parsed pydantic object (success), "refused", "invalid",
    an exception to raise, or a ready StructuredResult."""

    def __init__(
        self,
        script: Sequence[object] = (),
        *,
        provider: str = "openrouter",
        model: str = "google/gemini-3.8-flash",
        usd_per_token: float = 1e-6,
        reported_cost_usd: float | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self._script = list(script)
        self._usd_per_token = usd_per_token
        self._reported = reported_cost_usd
        self.calls: list[dict[str, Any]] = []

    def _usage(self, purpose: Purpose) -> Usage:
        return Usage(
            provider=self.provider,
            model=self.model,
            purpose=purpose,
            input_tokens=500,
            output_tokens=50,
            reported_cost_usd=self._reported,
        )

    def worst_case_usd(self, *, system: str, user: str, max_tokens: int) -> float:
        return (prompt_tokens(system, user) + max_tokens) * self._usd_per_token

    async def structured[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], max_tokens: int, purpose: Purpose
    ) -> StructuredResult[T]:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "schema": schema,
                "max_tokens": max_tokens,
                "purpose": purpose,
            }
        )
        nxt = self._script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if isinstance(nxt, StructuredResult):
            return nxt  # pyright: ignore[reportUnknownVariableType]
        if nxt == "refused":
            return StructuredResult(usage=self._usage(purpose), refused=True, error="refused")
        if nxt == "invalid":
            return StructuredResult(usage=self._usage(purpose), error="invalid output")
        assert isinstance(nxt, schema)
        return StructuredResult(parsed=nxt, usage=self._usage(purpose))
