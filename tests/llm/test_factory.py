"""make_client: one ModelClient per job, over SDK clients shared per provider per build."""

from __future__ import annotations

import pytest

from ai_newsdesk.llm.anthropic_client import AnthropicClient
from ai_newsdesk.llm.factory import CALL_TIMEOUT_S, SdkPool, UnusableModelError, make_client
from ai_newsdesk.llm.openai_client import OPENROUTER_BASE_URL, OpenAICompatibleClient
from ai_newsdesk.llm.prices import PriceBook
from tests.llm.test_validate import CATALOGUE, PRICES, PRICING

KEYS = {"anthropic": "", "openai": "sk-oa-" + "a" * 30, "openrouter": "sk-or-" + "b" * 30}


def build(provider: str, model: str, pool: SdkPool, **options: object) -> object:
    return make_client(
        provider,
        model,
        sdks=pool,
        pricebook=PRICES,
        catalogue=CATALOGUE,
        **options,  # type: ignore[arg-type]
    )


def test_anthropic_clients_keep_their_per_job_options_and_share_one_sdk() -> None:
    pool = SdkPool(KEYS)
    judge = build("anthropic", "claude-haiku-5-5", pool)
    briefing = build("anthropic", "claude-sonnet-5-5", pool, effort="medium")
    merger = build("anthropic", "claude-haiku-5-5", pool, cache_system=False)
    assert isinstance(judge, AnthropicClient)
    assert isinstance(briefing, AnthropicClient)
    assert isinstance(merger, AnthropicClient)
    assert (judge.effort, judge.cache_system) == ("low", True)
    assert briefing.effort == "medium" and briefing.model == "claude-sonnet-5-5"
    assert merger.cache_system is False
    assert judge._client is briefing._client is merger._client is pool.anthropic()


@pytest.mark.parametrize(
    ("provider", "model"), [("openai", "gpt-5.4-nano"), ("openrouter", "google/gemini-3.8-flash")]
)
def test_openai_compatible_clients_share_one_sdk_per_provider(provider: str, model: str) -> None:
    pool = SdkPool(KEYS)
    one = build(provider, model, pool)
    two = build(provider, model, pool, effort="medium", cache_system=False)  # options ignored
    assert isinstance(one, OpenAICompatibleClient) and isinstance(two, OpenAICompatibleClient)
    assert (one.provider, one.model) == (provider, model)
    assert one._client is two._client is pool.openai_compatible(provider)


def test_the_two_openai_compatible_providers_get_separate_sdks() -> None:
    pool = SdkPool(KEYS)
    openrouter = pool.openai_compatible("openrouter")
    assert openrouter is not pool.openai_compatible("openai")
    assert str(openrouter.base_url).rstrip("/") == OPENROUTER_BASE_URL
    assert openrouter.api_key == KEYS["openrouter"]


@pytest.mark.parametrize(
    ("provider", "model", "keys", "message"),
    [
        ("openrouter", "nobody/unknown", KEYS, "isn't in the OpenRouter model list"),
        ("openai", "gpt-unknown", KEYS, "no known price"),
        ("anthropic", "claude-unknown", KEYS, "Choose a Claude model"),
        ("openrouter", "google/gemini-3.8-flash", {}, "Add your OpenRouter API key first."),
        ("openai", "gpt-5.4-nano", {"openai": ""}, "Add your OpenAI API key first."),
        ("typesafe", "jev-1.13.0", KEYS, "Choose a provider"),
    ],
)
def test_unusable_choices_raise_with_the_reason(
    provider: str, model: str, keys: dict[str, str], message: str
) -> None:
    with pytest.raises(UnusableModelError, match=message):
        build(provider, model, SdkPool(keys))


def test_an_empty_anthropic_key_still_builds_a_client() -> None:
    """As before: calls then fail with 401 and items stay unscored."""
    pool = SdkPool({})
    client = build("anthropic", "claude-haiku-5-5", pool)
    assert isinstance(client, AnthropicClient)
    assert pool.anthropic().api_key == "not-set"


def test_without_a_catalogue_openrouter_is_unusable() -> None:
    with pytest.raises(UnusableModelError, match="model list hasn't loaded"):
        make_client(
            "openrouter",
            "google/gemini-3.8-flash",
            sdks=SdkPool(KEYS),
            pricebook=PriceBook(PRICING),
            catalogue=None,
        )


def test_the_pool_never_shows_keys() -> None:
    pool = SdkPool(KEYS)
    for text in (repr(pool), str(pool)):
        assert KEYS["openai"] not in text and KEYS["openrouter"] not in text


async def test_a_pool_closes_the_sdk_clients_it_built() -> None:
    pool = SdkPool(KEYS)
    sdk = pool.openai_compatible("openrouter")
    claude = pool.anthropic()
    await pool.aclose()
    assert sdk.is_closed()
    assert claude.is_closed()


async def test_a_pool_leaves_an_injected_http_client_open() -> None:
    import httpx

    http = httpx.AsyncClient()
    pool = SdkPool(KEYS, http_client=http)
    pool.openai_compatible("openai")
    await pool.aclose()
    assert not http.is_closed
    await http.aclose()


def test_production_clients_never_retry_and_time_out_after_120_seconds() -> None:
    """Feedback-2: a failed call is recorded once at its worst case, so no SDK retry may add an
    unrecorded attempt (the next run retries the items); and no call holds the run lock for
    long."""
    pool = SdkPool(KEYS)
    for sdk in (
        pool.anthropic(),
        pool.openai_compatible("openai"),
        pool.openai_compatible("openrouter"),
    ):
        assert sdk.max_retries == 0
        assert sdk.timeout == CALL_TIMEOUT_S == 120
