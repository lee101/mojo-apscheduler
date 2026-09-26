"""Parity tests for the interval and calendar-interval arithmetic."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pytest
from conftest import UTC

import mojo_apscheduler as ma
from apscheduler.triggers.calendarinterval import (
    CalendarIntervalTrigger as RealCalendar,
)
from apscheduler.triggers.interval import IntervalTrigger as RealInterval

NOW = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
START = datetime(2023, 12, 1, 6, 30, tzinfo=UTC)


def _pair(**kwargs):
    return (
        ma.IntervalTrigger(timezone="UTC", **kwargs),
        RealInterval(timezone="UTC", **kwargs),
    )


def _cal_pair(**kwargs):
    return (
        ma.CalendarIntervalTrigger(timezone="UTC", **kwargs),
        RealCalendar(timezone="UTC", **kwargs),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seconds": 90},
        {"minutes": 17},
        {"hours": 5, "minutes": 30},
        {"days": 3},
        {"weeks": 2},
        {"days": 1, "seconds": 1},
    ],
)
def test_first_fire_from_start_date(kwargs):
    mine, real = _pair(start_date=START, **kwargs)
    assert mine.get_next_fire_time(None, NOW) == real.get_next_fire_time(None, NOW)


@pytest.mark.parametrize(
    "kwargs", [{"seconds": 90}, {"minutes": 17}, {"hours": 5, "minutes": 30}, {"days": 3}]
)
def test_first_fire_from_now_before_the_start_date(kwargs):
    """``start_date > now`` short-circuits the ceil branch."""
    mine, real = _pair(start_date=START, **kwargs)
    early = datetime(2023, 1, 1, tzinfo=UTC)
    assert mine.get_next_fire_time(None, early) == real.get_next_fire_time(None, early)
    assert mine.get_next_fire_time(None, early) == START


@pytest.mark.parametrize(
    "kwargs", [{"seconds": 90}, {"minutes": 17}, {"hours": 5, "minutes": 30}, {"days": 3}]
)
def test_chained_interval_fires(kwargs):
    mine, real = _pair(start_date=START, **kwargs)
    previous = None
    for _ in range(5):
        got = mine.get_next_fire_time(previous, NOW)
        want = real.get_next_fire_time(previous, NOW)
        assert got == want, (previous, got, want)
        if want is None:
            return
        previous = want


def test_zero_interval_is_clamped_to_one_second():
    mine, real = _pair(start_date=START)
    assert mine.interval_length == real.interval_length == 1.0
    assert mine.get_next_fire_time(None, NOW) == real.get_next_fire_time(None, NOW)


def test_end_date_ends_the_schedule():
    end = datetime(2023, 12, 1, 6, 45, tzinfo=UTC)
    mine, real = _pair(start_date=START, minutes=30, end_date=end)
    previous = datetime(2023, 12, 1, 6, 30, tzinfo=UTC)
    assert mine.get_next_fire_time(previous, NOW) is None
    assert real.get_next_fire_time(previous, NOW) is None


def test_end_date_is_inclusive():
    end = datetime(2023, 12, 1, 7, 0, tzinfo=UTC)
    mine, real = _pair(start_date=START, minutes=30, end_date=end)
    previous = datetime(2023, 12, 1, 6, 30, tzinfo=UTC)
    assert mine.get_next_fire_time(previous, NOW) == end
    assert real.get_next_fire_time(previous, NOW) == end


def test_ceil_on_an_exact_boundary_keeps_that_boundary():
    """``ceil(1800 / 1800)`` is 1, so a ``now`` on a boundary fires on it.

    This is the real behaviour, and it is the case a wrong strict-inequality
    in the kernel would turn into a 7:30 instead of a 7:00.
    """
    mine, real = _pair(start_date=START, minutes=30)
    on_boundary = datetime(2023, 12, 1, 7, 0, tzinfo=UTC)
    assert mine.get_next_fire_time(None, on_boundary) == datetime(
        2023, 12, 1, 7, 0, tzinfo=UTC
    )
    assert real.get_next_fire_time(None, on_boundary) == datetime(
        2023, 12, 1, 7, 0, tzinfo=UTC
    )


def test_ceil_past_a_boundary_advances_a_whole_interval():
    mine, real = _pair(start_date=START, minutes=30)
    just_past = datetime(2023, 12, 1, 7, 0, 1, tzinfo=UTC)
    assert mine.get_next_fire_time(None, just_past) == datetime(
        2023, 12, 1, 7, 30, tzinfo=UTC
    )
    assert real.get_next_fire_time(None, just_past) == datetime(
        2023, 12, 1, 7, 30, tzinfo=UTC
    )


def test_jitter_stays_inside_its_window():
    trigger = ma.IntervalTrigger(start_date=START, minutes=30, jitter=120)
    plain = ma.IntervalTrigger(start_date=START, minutes=30)
    for _ in range(20):
        got = trigger.get_next_fire_time(None, NOW)
        base = plain.get_next_fire_time(None, NOW)
        assert base <= got <= base + timedelta(seconds=120)


def test_jitter_matches_the_real_trigger_with_a_seeded_rng():
    import random as _random

    mine, real = _pair(start_date=START, minutes=30, jitter=120)
    mine.jitter = real.jitter = 120
    for _ in range(10):
        _random.seed(4242)
        got = mine.get_next_fire_time(None, NOW)
        _random.seed(4242)
        want = real.get_next_fire_time(None, NOW)
        assert got == want


def test_next_fire_times_helper_chains():
    mine, real = _pair(start_date=START, minutes=30)
    expected = []
    previous = None
    for _ in range(6):
        fire = real.get_next_fire_time(previous, NOW)
        expected.append(fire)
        previous = fire
    assert mine.next_fire_times(None, NOW, 6) == expected


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_intervals_match(seed):
    rng = random.Random(seed)
    for _ in range(15):
        kwargs = dict(
            weeks=rng.randint(0, 2),
            days=rng.randint(0, 5),
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
            seconds=rng.randint(0, 59),
        )
        if not any(kwargs.values()):
            kwargs["seconds"] = 1
        start = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(
            days=rng.randint(0, 900)
        )
        mine, real = _pair(start_date=start, **kwargs)
        now = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(
            seconds=rng.randint(0, 10**7)
        )
        previous = None
        for _ in range(3):
            got = mine.get_next_fire_time(previous, now)
            want = real.get_next_fire_time(previous, now)
            assert got == want, (kwargs, start, previous, now, got, want)
            if want is None:
                break
            previous = want


# ------------------------------------------------------ calendar interval


def test_calendar_first_fire_is_the_start_date():
    start = date(2024, 1, 15)
    mine, real = _cal_pair(months=1, hour=7, start_date=start)
    assert mine.get_next_fire_time(None) == real.get_next_fire_time(None, NOW)
    assert mine.get_next_fire_time(None) == datetime(2024, 1, 15, 7, 0, tzinfo=UTC)


def test_calendar_month_rollover():
    mine, real = _cal_pair(months=1, hour=3, start_date=date(2024, 1, 31))
    previous = datetime(2024, 1, 31, 3, 0, tzinfo=UTC)
    got = mine.get_next_fire_time(previous)
    assert got == real.get_next_fire_time(previous, None)
    # February has no 31st even in a leap year, so the rollover retries March.
    assert got == datetime(2024, 3, 31, 3, 0, tzinfo=UTC)


def test_calendar_skips_a_month_with_no_such_day():
    """There is no 31 February, so the rollover has to try February and March."""
    mine, real = _cal_pair(months=1, hour=3, start_date=date(2023, 1, 31))
    previous = datetime(2023, 1, 31, 3, 0, tzinfo=UTC)
    got = mine.get_next_fire_time(previous)
    assert got == real.get_next_fire_time(previous, None)
    assert got == datetime(2023, 3, 31, 3, 0, tzinfo=UTC)


def test_calendar_skips_a_non_leap_february():
    """29 January plus one month has no 29 February in 2023 either."""
    mine, real = _cal_pair(months=1, hour=3, start_date=date(2023, 1, 29))
    previous = datetime(2023, 1, 29, 3, 0, tzinfo=UTC)
    got = mine.get_next_fire_time(previous)
    assert got == real.get_next_fire_time(previous, None)
    assert got == datetime(2023, 3, 29, 3, 0, tzinfo=UTC)


def test_calendar_years_and_months_roll_together():
    mine, real = _cal_pair(years=1, months=1, hour=3, start_date=date(2020, 2, 29))
    previous = datetime(2020, 2, 29, 3, 0, tzinfo=UTC)
    got = mine.get_next_fire_time(previous)
    assert got == real.get_next_fire_time(previous, None)
    assert got == datetime(2021, 3, 29, 3, 0, tzinfo=UTC)


def test_calendar_weeks_and_days_are_added_after_the_rollover():
    mine, real = _cal_pair(
        months=1, weeks=1, days=2, hour=3, start_date=date(2024, 1, 1)
    )
    previous = datetime(2024, 1, 1, 3, 0, tzinfo=UTC)
    got = mine.get_next_fire_time(previous)
    assert got == real.get_next_fire_time(previous, None)
    assert got == datetime(2024, 2, 10, 3, 0, tzinfo=UTC)


def test_calendar_negative_days():
    mine, real = _cal_pair(months=1, days=-3, hour=3, start_date=date(2024, 3, 10))
    previous = datetime(2024, 3, 10, 3, 0, tzinfo=UTC)
    got = mine.get_next_fire_time(previous)
    assert got == real.get_next_fire_time(previous, None)
    assert got == datetime(2024, 4, 7, 3, 0, tzinfo=UTC)


def test_calendar_end_date_stops_the_schedule():
    end = date(2024, 5, 15)
    mine, real = _cal_pair(months=1, hour=3, start_date=date(2024, 1, 1), end_date=end)
    previous = datetime(2024, 5, 1, 3, 0, tzinfo=UTC)
    assert mine.get_next_fire_time(previous) is None
    assert real.get_next_fire_time(previous, None) is None


def test_calendar_end_date_is_inclusive():
    end = date(2024, 6, 1)
    mine, real = _cal_pair(months=1, hour=3, start_date=date(2024, 1, 1), end_date=end)
    previous = datetime(2024, 5, 1, 3, 0, tzinfo=UTC)
    assert mine.get_next_fire_time(previous) == datetime(2024, 6, 1, 3, 0, tzinfo=UTC)
    assert real.get_next_fire_time(previous, None) == datetime(
        2024, 6, 1, 3, 0, tzinfo=UTC
    )


def test_calendar_rejects_a_zero_length_interval():
    with pytest.raises(ValueError):
        ma.CalendarIntervalTrigger(timezone="UTC", start_date=date(2024, 1, 1))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_calendar_intervals_match(seed):
    rng = random.Random(seed)
    for _ in range(20):
        kwargs = dict(
            years=rng.choice([0, 0, 1, 2]),
            months=rng.choice([0, 0, 1, 3, 6, 11]),
            weeks=rng.choice([0, 0, 1, 2]),
            days=rng.choice([0, 0, 1, 5, -3]),
            hour=rng.randint(0, 23),
            minute=rng.choice([0, 15, 30]),
            second=rng.choice([0, 0, 30]),
        )
        if not any(kwargs[k] for k in ("years", "months", "weeks", "days")):
            continue
        start = date(2024, rng.randint(1, 12), rng.randint(1, 28))
        end = date(2032, 1, 1) if rng.random() < 0.4 else None
        mine, real = _cal_pair(start_date=start, end_date=end, **kwargs)
        previous = None
        for _ in range(3):
            got = mine.get_next_fire_time(previous)
            want = real.get_next_fire_time(previous, None)
            assert got == want, (kwargs, start, end, previous, got, want)
            if want is None:
                break
            previous = got
