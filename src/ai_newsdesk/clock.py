"""Clocks: the real one, and a settable one for tests (V1-S0). Both return aware UTC."""

from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that only moves when told to. Accepts any aware datetime; returns UTC."""

    def __init__(self, at: datetime) -> None:
        self._at = _aware_utc(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = _aware_utc(at)

    def advance(self, delta: timedelta) -> datetime:
        self._at = self._at + delta
        return self._at


def _aware_utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("FixedClock needs a timezone-aware datetime")
    return at.astimezone(UTC)
