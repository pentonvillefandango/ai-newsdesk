from __future__ import annotations

import pytest

from ai_newsdesk.models import UsageStats
from ai_newsdesk.web.estimate import (
    DEFAULT_RATES,
    RUNS_PER_DAY_OPTIONS,
    frequency_options,
    monthly_estimate,
    rates_from,
)


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
        days=7, runs=28, items_reviewed=2800, review_usd=0.28, write_usd=0.07, briefing_usd=0.035
    )
    rates = rates_from(stats)
    assert rates.from_history
    assert rates.items_per_run == pytest.approx(100)
    assert monthly_estimate(
        rates, runs_per_day=4, daily_cap=500, why_it_matters=True
    ) == pytest.approx(1.65)


def test_thin_history_keeps_the_defaults() -> None:
    assert (
        rates_from(
            UsageStats(
                days=7, runs=2, items_reviewed=900, review_usd=1, write_usd=1, briefing_usd=1
            )
        )
        == DEFAULT_RATES
    )
    assert (
        rates_from(
            UsageStats(days=7, runs=9, items_reviewed=10, review_usd=1, write_usd=1, briefing_usd=1)
        )
        == DEFAULT_RATES
    )


def test_frequency_options() -> None:
    options = frequency_options(DEFAULT_RATES, daily_cap=500, why_it_matters=True, budget=1.0)
    assert [o.runs for o in options] == list(RUNS_PER_DAY_OPTIONS)
    assert [o.label for o in options[:3]] == ["Once a day", "Twice a day", "4 times a day"]
    assert not options[0].over_budget
    assert options[2].over_budget
