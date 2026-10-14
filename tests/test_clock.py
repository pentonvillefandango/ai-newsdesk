from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_newsdesk.clock import FixedClock, SystemClock
from ai_newsdesk.protocols import Clock


def test_system_clock_is_aware_utc() -> None:
    clock: Clock = SystemClock()
    now = clock.now()
    assert now.tzinfo is UTC
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5


def test_fixed_clock_normalises_to_utc() -> None:
    bst = timezone(timedelta(hours=1))
    clock: Clock = FixedClock(datetime(2026, 10, 14, 13, 0, tzinfo=bst))
    assert clock.now() == datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    assert clock.now().tzinfo is UTC


def test_fixed_clock_moves_only_when_told() -> None:
    clock = FixedClock(datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
    assert clock.now() == clock.now()
    assert clock.advance(timedelta(hours=6)) == datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
    clock.set(datetime(2026, 11, 1, tzinfo=UTC))
    assert clock.now() == datetime(2026, 11, 1, tzinfo=UTC)


def test_fixed_clock_refuses_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 10, 14, 12, 0))
    clock = FixedClock(datetime(2026, 10, 14, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 10, 14))
