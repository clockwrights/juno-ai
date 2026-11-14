"""Injectable clock.

Tests never sleep (docs/12 §5). Production code takes a `Clock` and tests pass
`FrozenClock`, so anything time-dependent — lock expiry, daily counter
rollover, decay — is deterministic.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol


class Clock(Protocol):
    def now(self) -> dt.datetime:
        """Current time, always timezone-aware and in UTC."""
        ...


class SystemClock:
    """Real time."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=dt.UTC)


class FrozenClock:
    """Fixed time that only moves when a test moves it."""

    def __init__(self, at: dt.datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock needs an aware datetime")
        self._at = at.astimezone(dt.UTC)

    def now(self) -> dt.datetime:
        return self._at

    def advance(self, **delta: float) -> None:
        self._at += dt.timedelta(**delta)


SYSTEM_CLOCK = SystemClock()


def utc_now_iso(clock: Clock = SYSTEM_CLOCK) -> str:
    """ISO 8601 with milliseconds and a trailing Z, the format used in every key."""
    return clock.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def utc_date(clock: Clock = SYSTEM_CLOCK) -> str:
    """UTC calendar date, the partition for daily counters and session records."""
    return clock.now().strftime("%Y-%m-%d")
