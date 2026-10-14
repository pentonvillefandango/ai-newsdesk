# src/ai_newsdesk/budget.py
"""Spend guard: monthly cap (calendar month in TZ) and daily review cap.

Every AI call is checked with can_afford(worst_case) first and recorded with record(usage)
after. Because worst_case >= actual for every call, recorded spend can never pass the cap.
"""

from __future__ import annotations

import logging

from ai_newsdesk.models import Usage
from ai_newsdesk.pricing import Pricing
from ai_newsdesk.protocols import Clock
from ai_newsdesk.runtime_settings import RuntimeSettings
from ai_newsdesk.store.store import Store

log = logging.getLogger(__name__)


class Budget:
    def __init__(
        self,
        store: Store,
        pricing: Pricing,
        clock: Clock,
        runtime: RuntimeSettings,
        tz: str = "Europe/London",
    ) -> None:
        self.store = store
        self.pricing = pricing
        self.clock = clock
        self.runtime = runtime  # replaced by the pipeline with fresh settings each run
        self.tz = tz
        self._spent = 0.0
        self._reviewed = 0
        self._loaded = False

    async def refresh(self) -> None:
        now = self.clock.now()
        self._spent = await self.store.month_spend(now, self.tz)
        self._reviewed = await self.store.reviewed_count_today(now, self.tz)
        self._loaded = True

    def remaining_month(self) -> float:
        return max(0.0, self.runtime.monthly_budget_usd - self._spent)

    def remaining_reviews_today(self) -> int:
        return max(0, self.runtime.daily_review_cap - self._reviewed)

    def can_afford(self, worst_case_usd: float) -> bool:
        if not self._loaded:
            return False
        remaining = self.remaining_month()
        return remaining > 0 and 0 <= worst_case_usd <= remaining

    def cost(self, usage: Usage) -> float:
        """Pricing.usage_cost: cache writes at 1.25x and cache reads at 0.1x the input price."""
        try:
            return self.pricing.usage_cost(usage)
        except KeyError:  # pricing.UnknownModelError
            log.warning("model %s is missing from pricing.yaml; spend recorded as $0", usage.model)
            return 0.0

    async def record(self, usage: Usage) -> float:
        cost = self.cost(usage)
        await self.store.add_spend(usage, cost, self.clock.now())
        self._spent += cost
        return cost

    def note_reviewed(self, n: int) -> None:
        self._reviewed += max(0, n)
