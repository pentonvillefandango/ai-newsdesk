"""The Settings page's Test button (multi-provider spec 3.5): one tiny structured call to a
(provider, model) choice, so the owner learns before saving whether it answers.

The call is refused before anything is sent when the choice fails validation, its key is
missing or the month's budget cannot cover its worst case. A call that is made is recorded as
spend like any other (purpose review: the spend table allows only the three job purposes).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Collection
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from ai_newsdesk.llm.base import ModelClient, StructuredResult, describe_error
from ai_newsdesk.llm.catalogue import Catalogue
from ai_newsdesk.llm.factory import UnusableModelError
from ai_newsdesk.llm.prices import Price, PriceBook
from ai_newsdesk.llm.validate import PROVIDER_LABELS, validate_model_choice
from ai_newsdesk.models import Purpose, Usage

# Room for a reasoning model's thinking as well as the ~10-token answer: a lower cap makes
# such models stop at the limit and the test fail although the model works.
TEST_MAX_TOKENS = 512
# The Test holds the pipeline lock: a provider that accepts the call and then stalls must not
# hold it (and delay a scheduled run) for the SDK's 10-minute default.
TEST_TIMEOUT_S = 30
TIMEOUT_MESSAGE = "The model didn't answer within 30 seconds."
TEST_SYSTEM = (
    "You are answering a connection test. Reply with the JSON object the schema asks for,"
    " with ok set to true."
)
TEST_USER = "Connection test: are you there?"
JEV_MESSAGE = (
    "Jev can't be tested here. The Test button works for Anthropic, OpenAI and OpenRouter."
)


class ConnectionCheck(BaseModel):
    ok: bool


class SpendGuard(Protocol):
    """The two Budget methods the test needs."""

    def can_afford(self, worst_case_usd: float) -> bool: ...

    async def record(self, usage: Usage) -> float: ...


ClientFor = Callable[[str, str], AbstractAsyncContextManager[ModelClient]]


@dataclass(frozen=True)
class ModelTestResult:
    job: str
    provider: str
    model: str
    ok: bool
    called: bool  # False: refused before anything was sent
    message: str  # for the owner; never holds a key
    latency_ms: int | None = None
    price: Price | None = None
    cost_usd: float | None = None


async def run_model_test(
    job: str,
    provider: str,
    model: str,
    *,
    client_for: ClientFor,
    prices: PriceBook,
    catalogue: Catalogue | None,
    keys: Collection[str],
    budget: SpendGuard,
) -> ModelTestResult:
    """`keys` holds the names of the keys that resolve. Never raises for a provider error."""

    def refused(message: str) -> ModelTestResult:
        return ModelTestResult(job, provider, model, ok=False, called=False, message=message)

    if provider == "typesafe":
        return refused(JEV_MESSAGE)
    error = validate_model_choice(provider, model, pricebook=prices, catalogue=catalogue)
    if error is not None:
        return refused(error)
    if provider not in keys:  # Anthropic too: a test without a key could only fail
        return refused(f"Add your {PROVIDER_LABELS[provider]} API key first.")
    price = prices.price(provider, model)
    try:
        async with client_for(provider, model) as client:
            worst = client.worst_case_usd(
                system=TEST_SYSTEM, user=TEST_USER, max_tokens=TEST_MAX_TOKENS
            )
            if not budget.can_afford(worst):
                return refused(
                    "This month's budget can't cover a test call. Raise the budget or wait"
                    " for the 1st."
                )
            started = time.perf_counter()
            timed_out = False
            try:
                async with asyncio.timeout(TEST_TIMEOUT_S):
                    result: StructuredResult[ConnectionCheck] = await client.structured(
                        system=TEST_SYSTEM,
                        user=TEST_USER,
                        schema=ConnectionCheck,
                        max_tokens=TEST_MAX_TOKENS,
                        purpose=Purpose.REVIEW,
                    )
            except TimeoutError:  # no usage is known, so nothing is recorded
                result, timed_out = StructuredResult(), True
            except Exception as exc:  # clients should not raise; never show the message
                result = StructuredResult(error=describe_error(exc))
            latency_ms = round((time.perf_counter() - started) * 1000)
    except UnusableModelError as exc:
        return refused(str(exc))
    cost = await budget.record(result.usage) if result.usage is not None else None
    if timed_out:
        message = TIMEOUT_MESSAGE
    elif result.parsed is not None:
        message = "It works."
    elif result.refused:
        message = "The model refused the test."
    else:
        message = f"It failed: {result.error or 'no valid answer'}."
    return ModelTestResult(
        job,
        provider,
        model,
        ok=result.parsed is not None,
        called=True,
        message=message,
        latency_ms=latency_ms,
        price=price,
        cost_usd=cost,
    )
