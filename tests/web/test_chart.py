from __future__ import annotations

from datetime import date

import pytest

from ai_newsdesk.web.chart import CHART_HEIGHT, nice_ceiling, spend_chart


def test_nice_ceiling() -> None:
    assert nice_ceiling(0) == 0.01
    assert nice_ceiling(0.083) == pytest.approx(0.1)
    assert nice_ceiling(0.1) == pytest.approx(0.1)
    assert nice_ceiling(0.13) == pytest.approx(0.2)
    assert nice_ceiling(0.24) == pytest.approx(0.25)
    assert nice_ceiling(3.1) == pytest.approx(5)


def test_spend_chart_has_one_bar_per_day_so_far() -> None:
    rows = [("2026-09-30", 9.0), ("2026-10-01", 0.05), ("2026-10-03", 0.1), ("2026-10-03", 0.0)]
    chart = spend_chart(rows, date(2026, 10, 3))
    assert [b.day.day for b in chart.bars] == [1, 2, 3]
    assert chart.scale_max == pytest.approx(0.1)
    assert chart.total == pytest.approx(0.15)
    assert chart.bars[1].height == 0
    assert chart.bars[2].height == pytest.approx(CHART_HEIGHT - 4)
    assert chart.bars[0].height == pytest.approx((CHART_HEIGHT - 4) / 2)
    assert chart.bars[2].y == pytest.approx(4)
    assert [b.x for b in chart.bars] == [2, 14, 26]
    assert chart.width == 7 * 12
    assert chart.peak is not None and chart.peak.day == date(2026, 10, 3)


def test_tiny_values_stay_visible_and_empty_months_have_no_peak() -> None:
    chart = spend_chart([("2026-10-01", 0.001), ("2026-10-02", 0.1)], date(2026, 10, 2))
    assert chart.bars[0].height == 2.0
    empty = spend_chart([], date(2026, 10, 14))
    assert len(empty.bars) == 14
    assert empty.peak is None
    assert all(b.height == 0 for b in empty.bars)
