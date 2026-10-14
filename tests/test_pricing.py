from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_newsdesk.models import Purpose, Usage
from ai_newsdesk.pricing import Pricing, UnknownModelError
from ai_newsdesk.runtime_settings import RuntimeSettings
from tests.conftest import ROOT

PRICING = ROOT / "config" / "pricing.yaml"


@pytest.fixture
def pricing() -> Pricing:
    return Pricing.load(PRICING)


def test_shipped_table(pricing: Pricing) -> None:
    assert pricing.as_of == "2026-10-14"
    assert pricing.models() == [
        "claude-haiku-5-5",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "jev-1.13.0",
    ]
    assert pricing.models("anthropic") == [
        "claude-haiku-5-5",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]
    assert pricing.models("typesafe") == ["jev-1.13.0"]
    assert pricing.models("nobody") == []
    haiku = pricing.price("claude-haiku-5-5")
    assert (haiku.provider, haiku.input_per_mtok, haiku.output_per_mtok) == ("anthropic", 0.1, 0.5)


def test_runtime_defaults_are_priced(pricing: Pricing) -> None:
    rs = RuntimeSettings()
    for model in (rs.judge_model, rs.writer_model, rs.briefing_model):
        assert model in pricing.models("anthropic")


@pytest.mark.parametrize(
    ("model", "in_tok", "out_tok", "expected"),
    [
        ("claude-haiku-5-5", 1_000_000, 1_000_000, 0.60),
        ("claude-haiku-5-5", 2_000, 500, 0.00045),
        ("claude-sonnet-5-5", 1_000_000, 0, 2.00),
        ("claude-opus-5-5", 0, 1_000_000, 20.00),
        ("jev-1.13.0", 1_000_000, 1_000_000, 0.042),
        ("claude-haiku-5-5", 0, 0, 0.0),
    ],
)
def test_cost(pricing: Pricing, model: str, in_tok: int, out_tok: int, expected: float) -> None:
    assert pricing.cost(model, in_tok, out_tok) == pytest.approx(expected)


def test_cache_tokens_and_usage_cost(pricing: Pricing) -> None:
    # 1M cache writes at 1.25x and 1M cache reads at 0.1x of $0.10
    cost = pricing.cost(
        "claude-haiku-5-5", 0, 0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000
    )
    assert cost == pytest.approx(0.135)
    usage = Usage(
        provider="anthropic",
        model="claude-haiku-5-5",
        purpose=Purpose.REVIEW,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    assert pricing.usage_cost(usage) == pytest.approx(0.61)


def test_unknown_model_and_bad_counts(pricing: Pricing) -> None:
    with pytest.raises(UnknownModelError):
        pricing.cost("claude-haiku-5-5-20261001", 1, 1)
    with pytest.raises(KeyError):  # UnknownModelError is a KeyError
        pricing.price("gpt-x")
    with pytest.raises(ValueError, match="negative"):
        pricing.cost("claude-haiku-5-5", -1, 0)


def test_load_validates(tmp_path: Path) -> None:
    bad = tmp_path / "pricing.yaml"
    bad.write_text(
        "as_of: '2026-10-14'\n"
        "models:\n"
        "  m: {provider: other, input_per_mtok: 1, output_per_mtok: 1}\n"
    )
    with pytest.raises(ValidationError):
        Pricing.load(bad)
    bad.write_text("as_of: '2026-10-14'\nmodels: {}\n")
    with pytest.raises(ValidationError):
        Pricing.load(bad)
