"""Estimated monthly cost for each update frequency (shown next to each option on Settings).

estimate = 30 x (min(items per run x runs per day, daily review cap)
                 x (review cost per item + writer cost per item [x 0.6 with why-it-matters off])
                 + briefing cost per day)
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_newsdesk.models import UsageStats
from ai_newsdesk.runtime_settings import RUNS_PER_DAY_CHOICES

RUNS_PER_DAY_OPTIONS: tuple[int, ...] = RUNS_PER_DAY_CHOICES  # step 1 owns the allowed values
DAYS_PER_MONTH = 30
# Spec section 5 (bake-off actuals): 500 reviews/day about $0.053, writing about $0.011/day for
# those items, briefing about $0.005/day.
DEFAULT_ITEMS_PER_RUN = 125.0
DEFAULT_REVIEW_PER_ITEM = 0.000106
DEFAULT_WRITE_PER_ITEM = 0.000022
DEFAULT_BRIEFING_PER_DAY = 0.005
WHY_OFF_WRITE_FACTOR = 0.6  # summary only: roughly 40 % fewer output tokens
MIN_RUNS_FOR_HISTORY = 3
MIN_ITEMS_FOR_HISTORY = 50


@dataclass(frozen=True)
class Rates:
    items_per_run: float
    review_per_item: float
    write_per_item: float
    briefing_per_day: float
    from_history: bool


DEFAULT_RATES = Rates(
    items_per_run=DEFAULT_ITEMS_PER_RUN,
    review_per_item=DEFAULT_REVIEW_PER_ITEM,
    write_per_item=DEFAULT_WRITE_PER_ITEM,
    briefing_per_day=DEFAULT_BRIEFING_PER_DAY,
    from_history=False,
)


def rates_from(stats: UsageStats) -> Rates:
    if stats.runs < MIN_RUNS_FOR_HISTORY or stats.items_reviewed < MIN_ITEMS_FOR_HISTORY:
        return DEFAULT_RATES
    items = stats.items_reviewed
    return Rates(
        items_per_run=items / stats.runs,
        review_per_item=stats.review_usd / items,
        write_per_item=stats.write_usd / items,
        briefing_per_day=stats.briefing_usd / max(stats.days, 1),
        from_history=True,
    )


def monthly_estimate(
    rates: Rates, *, runs_per_day: int, daily_cap: int, why_it_matters: bool
) -> float:
    items_per_day = min(rates.items_per_run * runs_per_day, float(daily_cap))
    write = rates.write_per_item * (1.0 if why_it_matters else WHY_OFF_WRITE_FACTOR)
    return DAYS_PER_MONTH * (
        items_per_day * (rates.review_per_item + write) + rates.briefing_per_day
    )


@dataclass(frozen=True)
class FrequencyOption:
    runs: int
    label: str
    monthly: float
    over_budget: bool


def frequency_label(runs: int) -> str:
    return {1: "Once a day", 2: "Twice a day"}.get(runs, f"{runs} times a day")


def frequency_options(
    rates: Rates, *, daily_cap: int, why_it_matters: bool, budget: float
) -> list[FrequencyOption]:
    options: list[FrequencyOption] = []
    for runs in RUNS_PER_DAY_OPTIONS:
        monthly = monthly_estimate(
            rates, runs_per_day=runs, daily_cap=daily_cap, why_it_matters=why_it_matters
        )
        options.append(FrequencyOption(runs, frequency_label(runs), monthly, monthly > budget))
    return options
