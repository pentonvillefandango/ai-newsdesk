"""The OpenRouter model catalogue: filtering, persistence, refresh and staleness."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from ai_newsdesk.clock import FixedClock
from ai_newsdesk.llm.catalogue import (
    OPENROUTER_MODELS_URL,
    SETTING_KEY,
    Catalogue,
    ModelCatalogue,
    filter_models,
    http_fetcher,
)
from ai_newsdesk.store.store import Store
from tests.conftest import ROOT

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "openrouter" / "models.json").read_text())
KEPT = [
    "anthropic/claude-haiku-5.5",
    "example/costly-cache-write",
    "google/gemini-3.8-flash",
    "openai/gpt-5.4-nano",
]


def fetcher(*replies: object) -> Any:
    queue = list(replies)

    async def fetch() -> object:
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return fetch


def test_filter_keeps_only_priced_structured_text_models() -> None:
    models = {m.id: m for m in filter_models(FIXTURE)}
    assert sorted(models) == KEPT


@pytest.mark.parametrize(
    ("model_id", "rule"),
    [
        ("nvidia/nemotron-3.5-lightning:free", "ids ending in :free"),
        ("unbiased/pareto-26.9", "no structured_outputs"),
        ("example/image-reader", "no text input"),
        ("openrouter/auto-beta", "negative (variable) price"),
        ("example/free-completion", "zero completion price"),
        ("example/no-price", "non-numeric price"),
        ("example/missing-pricing", "no pricing at all"),
        ("example/no-response-format", "response_format not supported"),
        ("example/no-max-tokens", "max_tokens not supported"),
    ],
)
def test_each_filter_rule_rejects(model_id: str, rule: str) -> None:
    entry = next(m for m in FIXTURE["data"] if m["id"] == model_id)
    assert filter_models({"data": [entry]}) == [], rule


def test_prices_are_per_million_tokens_and_take_the_highest_tier() -> None:
    models = {m.id: m for m in filter_models(FIXTURE)}
    nano = models["openai/gpt-5.4-nano"]
    assert (nano.name, nano.context_length) == ("OpenAI: GPT-5.4 Nano", 400000)
    assert (nano.input_per_mtok, nano.output_per_mtok) == pytest.approx((0.2, 1.25))
    # Above 100k prompt tokens the price rises; the worst case must use the higher tier.
    haiku = models["anthropic/claude-haiku-5.5"]
    assert (haiku.input_per_mtok, haiku.output_per_mtok) == pytest.approx((0.5, 2.5))


def test_a_cache_write_above_125_percent_raises_the_input_price() -> None:
    # The worst case prices input at 1.25x as a cache write; a 2x cache write must still fit.
    model = {m.id: m for m in filter_models(FIXTURE)}["example/costly-cache-write"]
    assert model.input_per_mtok == pytest.approx(2.0 / 1.25)
    assert model.output_per_mtok == pytest.approx(2.0)
    # A cache write within 1.25x leaves the input price alone.
    entry = {**next(m for m in FIXTURE["data"] if m["id"] == "example/costly-cache-write")}
    entry["pricing"] = {
        "prompt": "0.000001",
        "completion": "0.000002",
        "input_cache_write": "0.00000125",
    }
    assert filter_models({"data": [entry]})[0].input_per_mtok == pytest.approx(1.0)


def test_the_one_hour_cache_write_price_is_ignored() -> None:
    # We only ever use the 5-minute cache, so a pricey 1-hour write must not raise the price.
    entry = {**next(m for m in FIXTURE["data"] if m["id"] == "example/costly-cache-write")}
    entry["pricing"] = {
        "prompt": "0.000001",
        "completion": "0.000002",
        "input_cache_write": "0.00000125",
        "input_cache_write_1h": "0.00001",
        "overrides": [{"min_prompt_tokens": 100000, "input_cache_write_1h": "0.00002"}],
    }
    assert filter_models({"data": [entry]})[0].input_per_mtok == pytest.approx(1.0)


@pytest.mark.parametrize("payload", [None, [], {"data": "nope"}, {"nodata": []}, "text"])
def test_filter_rejects_a_malformed_payload(payload: object) -> None:
    with pytest.raises(ValueError):
        filter_models(payload)


def test_malformed_entries_are_skipped() -> None:
    good = next(m for m in FIXTURE["data"] if m["id"] == "openai/gpt-5.4-nano")
    assert [m.id for m in filter_models({"data": [42, {"id": 7}, good]})] == ["openai/gpt-5.4-nano"]


def test_catalogue_lookup_and_staleness(clock: FixedClock) -> None:
    now = clock.now()
    cat = Catalogue.build(filter_models(FIXTURE), fetched_at=now - timedelta(hours=23))
    assert cat.ids() == KEPT
    assert cat.get("openai/gpt-5.4-nano") is not None
    assert cat.get("example/no-price") is None
    assert cat.is_stale(now) is False
    assert cat.is_stale(now + timedelta(hours=1, seconds=1)) is True


async def test_refresh_persists_and_reloads(store: Store, clock: FixedClock) -> None:
    catalogue = ModelCatalogue(store, fetcher(FIXTURE), clock)
    assert await catalogue.load() is None
    assert catalogue.is_stale() is True
    assert await catalogue.refresh() is True
    assert catalogue.current is not None
    assert catalogue.current.fetched_at == clock.now()
    assert catalogue.is_stale() is False

    raw = await store.get_setting(SETTING_KEY)
    assert raw["fetched_at"] == clock.now().isoformat()

    again = ModelCatalogue(store, fetcher(), clock)
    loaded = await again.load()
    assert loaded == catalogue.current
    assert loaded is not None and loaded.ids() == KEPT


@pytest.mark.parametrize(
    "bad",
    [
        httpx.ConnectError("down"),
        ValueError("bad JSON"),
        {"data": "nope"},
        {"data": []},  # an empty result never replaces a good one
        {"data": [m for m in FIXTURE["data"] if m["id"].endswith(":free")]},
    ],
)
async def test_failed_refresh_keeps_the_last_good_copy(
    store: Store, clock: FixedClock, bad: object, caplog: pytest.LogCaptureFixture
) -> None:
    catalogue = ModelCatalogue(store, fetcher(FIXTURE, bad), clock)
    assert await catalogue.refresh() is True
    good = catalogue.current
    clock.advance(timedelta(hours=30))
    assert await catalogue.refresh() is False
    assert catalogue.current == good
    assert catalogue.is_stale() is True
    reloaded = await ModelCatalogue(store, fetcher(), clock).load()
    assert reloaded == good
    assert "model catalogue refresh failed" in caplog.text


async def test_failed_first_refresh_leaves_no_catalogue(store: Store, clock: FixedClock) -> None:
    catalogue = ModelCatalogue(store, fetcher(httpx.ConnectError("down")), clock)
    assert await catalogue.refresh() is False
    assert catalogue.current is None
    assert await store.get_setting(SETTING_KEY) is None


async def test_refresh_if_stale(store: Store, clock: FixedClock) -> None:
    catalogue = ModelCatalogue(store, fetcher(FIXTURE, FIXTURE), clock)
    assert await catalogue.refresh_if_stale() is True  # nothing stored yet
    clock.advance(timedelta(hours=2))
    assert await catalogue.refresh_if_stale() is False  # fresh: not fetched again
    clock.advance(timedelta(hours=23))
    assert await catalogue.refresh_if_stale() is True


async def test_a_corrupt_stored_catalogue_is_ignored(store: Store, clock: FixedClock) -> None:
    await store.set_setting(SETTING_KEY, {"fetched_at": "never", "models": 3})
    catalogue = ModelCatalogue(store, fetcher(), clock)
    assert await catalogue.load() is None


async def test_http_fetcher_gets_the_public_models_list() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=FIXTURE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await http_fetcher(http)() == FIXTURE
    assert str(seen[0].url) == OPENROUTER_MODELS_URL
    assert "authorization" not in seen[0].headers  # public: no key is ever sent


async def test_http_fetcher_raises_on_an_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await http_fetcher(http)()


@pytest.mark.parametrize("suffix", [":batch", ":free", ":beta", ":extended", ":thinking", ":nitro"])
def test_variant_ids_are_dropped(suffix: str) -> None:
    """OpenRouter lists variants (e.g. anthropic/claude-haiku-5.5:batch) beside the base id;
    they route and bill differently, so only the base id is usable."""
    base = next(m for m in FIXTURE["data"] if m["id"] == "anthropic/claude-haiku-5.5")
    variant = {**base, "id": base["id"] + suffix}
    assert [m.id for m in filter_models({"data": [base, variant]})] == [base["id"]]
