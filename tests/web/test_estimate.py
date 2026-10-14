from __future__ import annotations

import pytest

from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.models import Purpose, TokenTotals, UsageStats
from ai_newsdesk.web.estimate import (
    DEFAULT_PROFILE,
    DEFAULT_RATES,
    RUNS_PER_DAY_OPTIONS,
    frequency_options,
    monthly_estimate,
    profile_from,
    rates_for,
)
from tests.llm.test_validate import CATALOGUE, PRICING

PRICES = PriceBook(PRICING, lambda: CATALOGUE)
HAIKU = ("anthropic", "claude-haiku-5-5")
HAIKU_EVERYWHERE = {Purpose.REVIEW: HAIKU, Purpose.WRITE: HAIKU, Purpose.BRIEFING: HAIKU}


def test_defaults_match_the_spec() -> None:
    assert monthly_estimate(
        DEFAULT_RATES, runs_per_day=4, daily_cap=500, why_it_matters=True
    ) == pytest.approx(2.07)
    assert monthly_estimate(
        DEFAULT_RATES, runs_per_day=1, daily_cap=500, why_it_matters=True
    ) == pytest.approx(0.63)


def test_daily_cap_limits_the_estimate() -> None:
    four = monthly_estimate(DEFAULT_RATES, runs_per_day=4, daily_cap=500, why_it_matters=True)
    assert monthly_estimate(
        DEFAULT_RATES, runs_per_day=24, daily_cap=500, why_it_matters=True
    ) == pytest.approx(four)
    assert monthly_estimate(
        DEFAULT_RATES, runs_per_day=24, daily_cap=0, why_it_matters=True
    ) == pytest.approx(0.15)


def test_why_it_matters_off_is_cheaper() -> None:
    assert monthly_estimate(
        DEFAULT_RATES, runs_per_day=4, daily_cap=500, why_it_matters=False
    ) == pytest.approx(1.938)


def test_history_replaces_the_defaults() -> None:
    stats = UsageStats(
        days=7,
        runs=28,
        items_reviewed=2800,
        review_usd=0.28,
        write_usd=0.07,
        briefing_usd=0.035,
        review_tokens=TokenTotals(input_tokens=2_000_000, output_tokens=160_000),
        write_tokens=TokenTotals(input_tokens=200_000, output_tokens=100_000),
        briefing_tokens=TokenTotals(input_tokens=100_000, output_tokens=50_000),
    )
    profile = profile_from(stats)
    assert profile.from_history
    rates = rates_for(profile, PRICES, HAIKU_EVERYWHERE)
    assert rates is not None and rates.from_history
    assert rates.items_per_run == pytest.approx(100)
    # Haiku: review (0.2 + 0.08) / 2800, write (0.02 + 0.05) / 2800, briefing (0.01 + 0.025) / 7
    assert rates.review_per_item == pytest.approx(0.28 / 2800)
    assert rates.write_per_item == pytest.approx(0.07 / 2800)
    assert rates.briefing_per_day == pytest.approx(0.035 / 7)
    assert monthly_estimate(
        rates, runs_per_day=4, daily_cap=500, why_it_matters=True
    ) == pytest.approx(1.65)


def test_thin_history_keeps_the_defaults() -> None:
    assert (
        profile_from(
            UsageStats(
                days=7, runs=2, items_reviewed=900, review_usd=1, write_usd=1, briefing_usd=1
            )
        )
        == DEFAULT_PROFILE
    )
    assert (
        profile_from(
            UsageStats(days=7, runs=9, items_reviewed=10, review_usd=1, write_usd=1, briefing_usd=1)
        )
        == DEFAULT_PROFILE
    )


def test_the_default_profile_on_haiku_is_the_spec_figures() -> None:
    assert rates_for(DEFAULT_PROFILE, PRICES, HAIKU_EVERYWHERE) == pytest.approx(DEFAULT_RATES)


def test_each_job_is_priced_with_its_chosen_model() -> None:
    """Gemini Flash on OpenRouter costs 7.5x Haiku per token: only the judge's share grows."""
    choices = {**HAIKU_EVERYWHERE, Purpose.REVIEW: ("openrouter", "google/gemini-3.8-flash")}
    rates = rates_for(DEFAULT_PROFILE, PRICES, choices)
    assert rates is not None
    assert rates.review_per_item == pytest.approx(7.5 * DEFAULT_RATES.review_per_item)
    assert rates.write_per_item == pytest.approx(DEFAULT_RATES.write_per_item)
    assert rates.briefing_per_day == pytest.approx(DEFAULT_RATES.briefing_per_day)
    openai = {**HAIKU_EVERYWHERE, Purpose.WRITE: ("openai", "gpt-5.4-nano")}
    rates = rates_for(DEFAULT_PROFILE, PRICES, openai)
    assert rates is not None
    assert rates.write_per_item == pytest.approx((120 * 0.2 + 20 * 1.25) / 1e6)


def test_an_unpriced_choice_makes_the_estimate_unknown() -> None:
    choices = {**HAIKU_EVERYWHERE, Purpose.BRIEFING: ("openrouter", "nobody/unknown")}
    assert rates_for(DEFAULT_PROFILE, PRICES, choices) is None
    assert (
        rates_for(
            DEFAULT_PROFILE,
            PriceBook(PRICING),
            {**HAIKU_EVERYWHERE, Purpose.REVIEW: ("openrouter", "google/gemini-3.8-flash")},
        )
        is None
    )  # no catalogue yet
    options = frequency_options(None, daily_cap=500, why_it_matters=True, budget=1.0)
    assert [o.monthly for o in options] == [None] * len(RUNS_PER_DAY_OPTIONS)
    assert not any(o.over_budget for o in options)


def test_frequency_options() -> None:
    options = frequency_options(DEFAULT_RATES, daily_cap=500, why_it_matters=True, budget=1.0)
    assert [o.runs for o in options] == list(RUNS_PER_DAY_OPTIONS)
    assert [o.label for o in options[:3]] == ["Once a day", "Twice a day", "4 times a day"]
    assert not options[0].over_budget
    assert options[2].over_budget
