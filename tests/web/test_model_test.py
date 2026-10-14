"""run_model_test: the Settings Test button's one tiny, budget-checked structured call."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import openai
import pytest

from ai_newsdesk.llm.base import ModelClient, StructuredResult
from ai_newsdesk.llm.factory import UnusableModelError
from ai_newsdesk.llm.prices import Price, PriceBook
from ai_newsdesk.models import Purpose, Usage
from ai_newsdesk.web import model_test
from ai_newsdesk.web.model_test import (
    TEST_MAX_TOKENS,
    TEST_SYSTEM,
    TEST_USER,
    ConnectionCheck,
    ModelTestResult,
    run_model_test,
)
from tests.fake_model_client import FakeModelClient
from tests.llm.test_validate import CATALOGUE, PRICING

PRICES = PriceBook(PRICING, lambda: CATALOGUE)
ALL_KEYS = {"anthropic", "openai", "openrouter"}


class FakeBudget:
    def __init__(self, remaining: float = 1.0) -> None:
        self.remaining = remaining
        self.asked: list[float] = []
        self.recorded: list[Usage] = []

    def can_afford(self, worst_case_usd: float) -> bool:
        self.asked.append(worst_case_usd)
        return worst_case_usd <= self.remaining

    async def record(self, usage: Usage) -> float:
        self.recorded.append(usage)
        return 0.0012


class Clients:
    """Hands out one scripted client and remembers what was asked for."""

    def __init__(self, client: FakeModelClient | None = None, error: str | None = None) -> None:
        self.client = client
        self.error = error
        self.built: list[tuple[str, str]] = []

    @asynccontextmanager
    async def __call__(self, provider: str, model: str) -> AsyncIterator[ModelClient]:
        self.built.append((provider, model))
        if self.error is not None:
            raise UnusableModelError(self.error)
        assert self.client is not None
        yield self.client


async def _test(
    provider: str,
    model: str,
    clients: Clients,
    budget: FakeBudget | None = None,
    keys: set[str] = ALL_KEYS,
) -> ModelTestResult:
    return await run_model_test(
        "judge",
        provider,
        model,
        client_for=clients,
        prices=PRICES,
        catalogue=CATALOGUE,
        keys=keys,
        budget=budget or FakeBudget(),
    )


async def test_a_working_model_is_called_once_and_its_spend_recorded() -> None:
    fake = FakeModelClient([ConnectionCheck(ok=True)])
    clients, budget = Clients(fake), FakeBudget()
    result = await _test("openrouter", "google/gemini-3.8-flash", clients, budget)
    assert result.ok and result.called
    assert result.message == "It works."
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.price == Price(0.75, 3.75)
    assert result.cost_usd == pytest.approx(0.0012)
    assert (result.job, result.provider, result.model) == (
        "judge",
        "openrouter",
        "google/gemini-3.8-flash",
    )
    assert clients.built == [("openrouter", "google/gemini-3.8-flash")]
    [call] = fake.calls
    assert call["schema"] is ConnectionCheck
    assert call["max_tokens"] == TEST_MAX_TOKENS
    assert call["purpose"] == Purpose.REVIEW
    assert len(budget.asked) == 1 and budget.asked[0] > 0
    assert [u.model for u in budget.recorded] == ["google/gemini-3.8-flash"]


@pytest.mark.parametrize(
    ("provider", "model", "keys", "message"),
    [
        ("typesafe", "jev-1.13.0", ALL_KEYS, "Jev can't be tested here"),
        ("openrouter", "nobody/unknown", ALL_KEYS, "isn't in the OpenRouter model list"),
        ("openai", "gpt-unknown", ALL_KEYS, "no known price"),
        ("anthropic", "gpt-5.4-nano", ALL_KEYS, "Choose a Claude model"),
        ("openai", "", ALL_KEYS, "Enter a model id"),
        ("openai", "gpt-5.4-nano", {"anthropic"}, "Add your OpenAI API key first."),
        ("openrouter", "google/gemini-3.8-flash", set(), "Add your OpenRouter API key first."),
        ("anthropic", "claude-haiku-5-5", {"openai"}, "Add your Anthropic API key first."),
    ],
)
async def test_refused_without_a_call(
    provider: str, model: str, keys: set[str], message: str
) -> None:
    fake = FakeModelClient([])
    clients, budget = Clients(fake), FakeBudget()
    result = await _test(provider, model, clients, budget, keys)
    assert not result.ok and not result.called
    assert message in result.message
    assert clients.built == [] and fake.calls == [] and budget.recorded == []


async def test_refused_when_the_budget_cannot_cover_it() -> None:
    fake = FakeModelClient([ConnectionCheck(ok=True)])
    budget = FakeBudget(remaining=0.0)
    result = await _test("openrouter", "google/gemini-3.8-flash", Clients(fake), budget)
    assert not result.ok and not result.called
    assert "budget" in result.message
    assert fake.calls == [] and budget.recorded == []


async def test_the_budget_is_checked_against_the_clients_worst_case() -> None:
    """Remaining just under the client's worst case for TEST_MAX_TOKENS refuses; exactly the
    worst case is enough."""
    probe = FakeModelClient([])
    worst = probe.worst_case_usd(system=TEST_SYSTEM, user=TEST_USER, max_tokens=TEST_MAX_TOKENS)
    assert worst > 0
    fake = FakeModelClient([ConnectionCheck(ok=True)])
    short = FakeBudget(remaining=worst - 1e-9)
    result = await _test("openrouter", "google/gemini-3.8-flash", Clients(fake), short)
    assert not result.called and "budget" in result.message
    assert short.asked == [pytest.approx(worst)] and fake.calls == []
    enough = FakeBudget(remaining=worst)
    result = await _test("openrouter", "google/gemini-3.8-flash", Clients(fake), enough)
    assert result.ok and enough.asked == [pytest.approx(worst)]
    assert len(fake.calls) == 1


async def test_only_the_chosen_providers_key_is_needed() -> None:
    fake = FakeModelClient([ConnectionCheck(ok=True)])
    result = await _test(
        "openrouter", "google/gemini-3.8-flash", Clients(fake), keys={"openrouter"}
    )
    assert result.ok and result.called


class SlowClient(FakeModelClient):
    """Accepts the call and never answers."""

    async def structured(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self.calls.append(kwargs)
        await asyncio.sleep(3600)


async def test_a_test_that_never_answers_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_test, "TEST_TIMEOUT_S", 0.05)
    slow, budget = SlowClient([]), FakeBudget()
    started = time.perf_counter()
    result = await _test("openrouter", "google/gemini-3.8-flash", Clients(slow), budget)
    assert time.perf_counter() - started < 1
    assert result.called and not result.ok
    assert result.message == model_test.timeout_message()  # built from the patched limit
    assert result.message == "The model didn't answer within 0.05 seconds."
    assert len(slow.calls) == 1 and budget.recorded == []


def test_the_test_timeout_is_30_seconds() -> None:
    assert model_test.TEST_TIMEOUT_S == 30
    assert model_test.timeout_message() == "The model didn't answer within 30 seconds."


async def test_an_unusable_client_is_refused() -> None:
    clients = Clients(error="Add your OpenAI API key first.")
    result = await _test("openai", "gpt-5.4-nano", clients)
    assert not result.ok and not result.called
    assert result.message == "Add your OpenAI API key first."


async def test_a_refusal_is_a_failure_but_still_paid_for() -> None:
    fake = FakeModelClient(["refused"])
    budget = FakeBudget()
    result = await _test("openrouter", "google/gemini-3.8-flash", Clients(fake), budget)
    assert result.called and not result.ok
    assert result.message == "The model refused the test."
    assert len(budget.recorded) == 1


async def test_invalid_output_is_a_failure_with_the_scrubbed_reason() -> None:
    fake = FakeModelClient([StructuredResult(error="HTTP 404 (NotFoundError)")])
    budget = FakeBudget()
    result = await _test("openai", "gpt-5.4-nano", Clients(fake), budget)
    assert result.called and not result.ok
    assert result.message == "It failed: HTTP 404 (NotFoundError)."
    assert budget.recorded == []  # no usage, nothing recorded


async def test_a_raising_client_is_described_without_its_message() -> None:
    secret = "sk-or-" + "s" * 40
    fake = FakeModelClient([openai.APIConnectionError(message=secret, request=_request())])
    result = await _test("openrouter", "google/gemini-3.8-flash", Clients(fake))
    assert result.called and not result.ok
    assert secret not in result.message
    assert result.message == "It failed: connection error (APIConnectionError)."


def _request() -> Any:
    import httpx

    return httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
