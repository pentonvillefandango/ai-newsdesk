"""Estimated monthly cost for each update frequency (shown next to each option on Settings).

estimate = 30 x (min(items per run x runs per day, daily review cap)
                 x (review cost per item + writer cost per item [x 0.6 with why-it-matters off])
                 + briefing cost per day)

The costs per item come from tokens, priced with each job's chosen (provider, model) through
the PriceBook: the last 7 days' tokens when there is enough history, else a starting profile.
A chosen model with no known price makes the estimate unknown (None).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ai_newsdesk.llm.prices import PriceBook
from ai_newsdesk.models import Purpose, TokenTotals, Usage, UsageStats
from ai_newsdesk.pricing import UnknownModelError
from ai_newsdesk.runtime_settings import RUNS_PER_DAY_CHOICES, RuntimeSettings

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


@dataclass(frozen=True)
class UsageProfile:
    """Tokens per purpose: review and write over `items` reviewed items, briefing over `days`."""

    items_per_run: float
    review: TokenTotals
    write: TokenTotals
    briefing: TokenTotals
    items: float
    days: float
    from_history: bool


# The starting figures in tokens: on Claude Haiku ($0.10 in, $0.50 out) they cost exactly the
# DEFAULT_RATES above (660 x 0.1 + 80 x 0.5 = $0.000106 a reviewed item, and so on).
DEFAULT_PROFILE = UsageProfile(
    items_per_run=DEFAULT_ITEMS_PER_RUN,
    review=TokenTotals(input_tokens=660, output_tokens=80),
    write=TokenTotals(input_tokens=120, output_tokens=20),
    briefing=TokenTotals(input_tokens=25_000, output_tokens=5_000),
    items=1.0,
    days=1.0,
    from_history=False,
)

# (provider, model) per purpose; the duplicate merger's calls are counted as review.
Choices = Mapping[Purpose, tuple[str, str]]


def profile_from(stats: UsageStats) -> UsageProfile:
    if stats.runs < MIN_RUNS_FOR_HISTORY or stats.items_reviewed < MIN_ITEMS_FOR_HISTORY:
        return DEFAULT_PROFILE
    return UsageProfile(
        items_per_run=stats.items_reviewed / stats.runs,
        review=stats.review_tokens,
        write=stats.write_tokens,
        briefing=stats.briefing_tokens,
        items=float(stats.items_reviewed),
        days=float(max(stats.days, 1)),
        from_history=True,
    )


def choices_of(runtime: RuntimeSettings) -> dict[Purpose, tuple[str, str]]:
    return {
        Purpose.REVIEW: (runtime.judge_provider, runtime.judge_model),
        Purpose.WRITE: (runtime.writer_provider, runtime.writer_model),
        Purpose.BRIEFING: (runtime.briefing_provider, runtime.briefing_model),
    }


def _cost(prices: PriceBook, purpose: Purpose, choice: tuple[str, str], t: TokenTotals) -> float:
    provider, model = choice
    usage = Usage(
        provider=provider,
        model=model,
        purpose=purpose,
        input_tokens=t.input_tokens,
        output_tokens=t.output_tokens,
        cache_read_tokens=t.cache_read_tokens,
        cache_write_tokens=t.cache_write_tokens,
    )
    return prices.usage_cost(usage)


def rates_for(profile: UsageProfile, prices: PriceBook, choices: Choices) -> Rates | None:
    """Costs per item and per day with the chosen models, or None if one has no known price."""
    try:
        review = _cost(prices, Purpose.REVIEW, choices[Purpose.REVIEW], profile.review)
        write = _cost(prices, Purpose.WRITE, choices[Purpose.WRITE], profile.write)
        briefing = _cost(prices, Purpose.BRIEFING, choices[Purpose.BRIEFING], profile.briefing)
    except UnknownModelError:
        return None
    return Rates(
        items_per_run=profile.items_per_run,
        review_per_item=review / profile.items,
        write_per_item=write / profile.items,
        briefing_per_day=briefing / profile.days,
        from_history=profile.from_history,
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
    monthly: float | None  # None: a chosen model has no known price
    over_budget: bool


def frequency_label(runs: int) -> str:
    return {1: "Once a day", 2: "Twice a day"}.get(runs, f"{runs} times a day")


def frequency_options(
    rates: Rates | None, *, daily_cap: int, why_it_matters: bool, budget: float
) -> list[FrequencyOption]:
    options: list[FrequencyOption] = []
    for runs in RUNS_PER_DAY_OPTIONS:
        if rates is None:
            options.append(FrequencyOption(runs, frequency_label(runs), None, False))
            continue
        monthly = monthly_estimate(
            rates, runs_per_day=runs, daily_cap=daily_cap, why_it_matters=why_it_matters
        )
        options.append(FrequencyOption(runs, frequency_label(runs), monthly, monthly > budget))
    return options
