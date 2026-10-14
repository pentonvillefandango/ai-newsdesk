"""Geometry for the per-day spend chart, drawn as inline SVG by the template (no chart script)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

CHART_HEIGHT = 120
BAND = 12
BAR_WIDTH = 8
MIN_BAR = 2.0
TOP_PAD = 4


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    width: float
    height: float
    day: date
    value: float


@dataclass(frozen=True)
class SpendChart:
    bars: list[Bar]
    width: int
    height: int
    scale_max: float
    total: float
    peak: Bar | None


def nice_ceiling(value: float) -> float:
    """The smallest of 1, 2, 2.5, 5 x 10^k that is >= value (0.01 for nothing)."""
    if value <= 0:
        return 0.01
    base = 10.0 ** math.floor(math.log10(value))
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= step * base * (1 + 1e-9):
            return round(step * base, 10)
    return round(10 * base, 10)


def spend_chart(rows: list[tuple[str, float]], today: date) -> SpendChart:
    """One bar per day from the 1st of ``today``'s month to ``today``; other days are ignored."""
    by_day: dict[str, float] = {}
    for day, usd in rows:
        by_day[day] = by_day.get(day, 0.0) + usd
    days = [today.replace(day=d) for d in range(1, today.day + 1)]
    values = [by_day.get(d.isoformat(), 0.0) for d in days]
    scale = nice_ceiling(max(values, default=0.0))
    usable = CHART_HEIGHT - TOP_PAD
    bars: list[Bar] = []
    for i, (day, value) in enumerate(zip(days, values, strict=True)):
        height = 0.0 if value <= 0 else max(MIN_BAR, value / scale * usable)
        bars.append(
            Bar(
                x=i * BAND + (BAND - BAR_WIDTH) / 2,
                y=CHART_HEIGHT - height,
                width=BAR_WIDTH,
                height=height,
                day=day,
                value=value,
            )
        )
    peak = max((b for b in bars if b.value > 0), key=lambda b: b.value, default=None)
    return SpendChart(
        bars=bars,
        width=max(len(days), 7) * BAND,
        height=CHART_HEIGHT,
        scale_max=scale,
        total=sum(values),
        peak=peak,
    )
