"""The injectable clock (docs/12 §5)."""

from __future__ import annotations

import datetime as dt

import pytest

from core.clock import FrozenClock, SystemClock, utc_date, utc_now_iso


def test_system_clock_is_utc_aware() -> None:
    assert SystemClock().now().tzinfo is dt.UTC


def test_frozen_clock_does_not_move_on_its_own() -> None:
    clock = FrozenClock(dt.datetime(2026, 9, 6, 10, 0, tzinfo=dt.UTC))
    assert clock.now() == clock.now()


def test_frozen_clock_advances_only_when_told() -> None:
    clock = FrozenClock(dt.datetime(2026, 9, 6, 10, 0, tzinfo=dt.UTC))
    clock.advance(hours=2)
    assert clock.now().hour == 12


def test_naive_datetimes_are_refused() -> None:
    """A naive datetime is how a UTC day boundary silently becomes a local one."""
    with pytest.raises(ValueError, match="aware"):
        FrozenClock(dt.datetime(2026, 9, 6, 10, 0))


def test_iso_format_has_milliseconds_and_z() -> None:
    clock = FrozenClock(dt.datetime(2026, 9, 6, 10, 0, 0, 123456, tzinfo=dt.UTC))
    assert utc_now_iso(clock) == "2026-09-06T10:00:00.123Z"


def test_utc_date_is_the_counter_partition() -> None:
    assert utc_date(FrozenClock(dt.datetime(2026, 9, 6, 23, 59, tzinfo=dt.UTC))) == "2026-09-06"
