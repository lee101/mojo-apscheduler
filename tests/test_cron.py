"""Parity tests for the cron field search against the real APScheduler.

Every case builds the real ``apscheduler.triggers.cron.CronTrigger`` and the
ported one from the same keyword arguments and compares
``get_next_fire_time`` -- first fire and a chain of following fires, because a
kernel can agree on the first call and still carry wrong on the second.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest
from conftest import UTC

import mojo_apscheduler as ma
from apscheduler.triggers.cron import CronTrigger as RealCronTrigger

SPECS = [
    # every expression kind, one field at a time
    {"second": "*/15"},
    {"second": "0"},
    {"minute": "*/7"},
    {"minute": "0,30"},
    {"hour": "9-17"},
    {"hour": "3-21/2"},
    {"day": "1,15"},
    {"day": "8-14"},
    {"day": "*/3"},
    {"day": "last"},
    {"day": "2nd fri"},
    {"day": "last fri"},
    {"day": "3rd wed"},
    {"month": "1,4,7,10"},
    {"month": "jan-mar"},
    {"month": "*/2"},
    {"month": "feb"},
    {"year": "2024"},
    {"year": "2023-2025"},
    {"year": "2020/2"},
    # the two pseudo-fields, which are tested but never written
    {"day_of_week": "mon"},
    {"day_of_week": "mon-fri"},
    {"day_of_week": "sat,sun"},
    {"day_of_week": "0"},
    {"week": "1"},
    {"week": "*/4"},
    {"week": "1", "day_of_week": "mon"},
    # combinations, including the ones that force a carry
    {"minute": "0,30", "hour": "9-17", "day_of_week": "mon-fri"},
    {"day": "1,15", "month": "*/2", "hour": "1-5/2"},
    {"day": "29", "month": "2", "year": "2024"},
    {"day": "31", "month": "1,3,5,7,8,10,12"},
    {"month": "jan-mar", "year": 2025, "hour": "3"},
    {"second": "0", "minute": "0", "hour": "0", "day": "1", "month": "1", "year": "2024"},
]

POOLS = {
    "day": ["1", "5", "15", "28", "last", "1,15", "8-14", "*/3", "2nd fri", "last sun"],
    "month": ["*", "2", "6", "12", "1,4,7,10", "jan-mar", "*/2", "feb"],
    "hour": ["*", "0", "9-17", "*/4", "0,12", "3-21/2"],
    "minute": ["*", "0", "15", "*/7", "0,30", "5-55/10"],
    "second": ["*", "0", "*/15", "7", "0,30"],
    "day_of_week": ["*", "mon", "mon-fri", "sat,sun", "0", "1-5"],
    "week": ["*", "1", "10", "*/4"],
}


def _pair(**spec):
    return (
        ma.CronTrigger(timezone="UTC", **spec),
        RealCronTrigger(timezone="UTC", **spec),
    )


def _chain(trigger, previous, now, steps):
    out = []
    for _ in range(steps):
        fire = trigger.get_next_fire_time(previous, now)
        out.append(fire)
        if fire is None:
            return out
        previous = fire
    return out


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: str(s))
def test_first_fire_matches(spec, now):
    mine, real = _pair(**spec)
    assert mine.get_next_fire_time(None, now) == real.get_next_fire_time(None, now)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: str(s))
def test_chained_fires_match(spec, now):
    mine, real = _pair(**spec)
    assert _chain(mine, None, now, 6) == _chain(real, None, now, 6)


def test_datetime_ceil_rounds_a_fractional_start_up():
    """A previous fire time plus one microsecond must not fire twice.

    The real code adds a microsecond to the previous fire time and then ceils
    the result, so a ``*/15`` second trigger has to land on :30 and not back
    on :15.
    """
    after = datetime(2024, 3, 1, 12, 0, 20, tzinfo=UTC)
    previous = datetime(2024, 3, 1, 12, 0, 15, tzinfo=UTC)
    mine, real = _pair(second="*/15")
    got = mine.get_next_fire_time(previous, after)
    assert got == real.get_next_fire_time(previous, after)
    assert got == datetime(2024, 3, 1, 12, 0, 30, tzinfo=UTC)


def test_now_wins_over_a_later_previous_fire_time(now):
    """``min(now, previous + 1us)`` is the real rule when the job is behind."""
    mine, real = _pair(minute="*/10")
    previous = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    assert mine.get_next_fire_time(previous, now) == real.get_next_fire_time(
        previous, now
    )


def test_start_date_is_used_for_the_first_fire(now):
    start = datetime(2024, 6, 1, 8, 0, tzinfo=UTC)
    mine, real = _pair(hour="8", start_date=start)
    assert mine.get_next_fire_time(None, now) == real.get_next_fire_time(None, now)
    assert mine.get_next_fire_time(None, now) == datetime(2024, 6, 1, 8, 0, tzinfo=UTC)


def test_end_date_stops_the_search():
    end = datetime(2024, 3, 1, 12, 59, tzinfo=UTC)
    after = datetime(2024, 3, 1, 12, 35, tzinfo=UTC)
    previous = datetime(2024, 3, 1, 12, 30, tzinfo=UTC)
    mine, real = _pair(minute="*/30", end_date=end)
    assert mine.get_next_fire_time(previous, after) is None
    assert real.get_next_fire_time(previous, after) is None


def test_end_date_on_an_exact_second_is_inclusive():
    end = datetime(2024, 3, 1, 13, 0, tzinfo=UTC)
    after = datetime(2024, 3, 1, 12, 35, tzinfo=UTC)
    previous = datetime(2024, 3, 1, 12, 30, tzinfo=UTC)
    mine, real = _pair(minute="*/30", end_date=end)
    assert mine.get_next_fire_time(previous, after) == end
    assert real.get_next_fire_time(previous, after) == end


def test_a_never_satisfiable_field_returns_none(now):
    """0 February never happens, and both implementations give up on it."""
    mine, real = _pair(month="2", day="30", year="2024")
    assert mine.get_next_fire_time(None, now) is None
    assert real.get_next_fire_time(None, now) is None


def test_next_fire_times_helper_matches_the_stepwise_chain(now):
    mine, real = _pair(minute="*/7")
    assert mine.next_fire_times(None, now, 8) == _chain(real, None, now, 8)
    assert len(mine.next_fire_times(None, now, 8)) == 8


def test_next_fire_times_stops_at_the_end_date():
    """``now`` frozen behind the fires makes the real trigger loop, and so must we.

    APScheduler takes ``min(now, previous + 1us)`` as its search start, so
    calling it repeatedly with a ``now`` that never advances sends it back to
    the same second. The helper is only useful when ``now`` moves, so the test
    pins the equality with the real stepwise chain rather than a hand-written
    expectation.
    """
    end = datetime(2024, 3, 1, 12, 30, tzinfo=UTC)
    now = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    mine, real = _pair(minute="*/15", end_date=end)
    assert mine.next_fire_times(None, now, 12) == _chain(real, None, now, 12)
    assert len(mine.next_fire_times(None, now, 12)) == 12


def test_next_fire_times_walks_forward_when_now_advances():
    mine, real = _pair(minute="*/15")
    now = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    got = []
    previous = None
    for _ in range(4):
        step_now = now + timedelta(minutes=15 * len(got))
        got.append(mine.get_next_fire_time(previous, step_now))
        previous = got[-1]
    want = []
    previous = None
    for _ in range(4):
        step_now = now + timedelta(minutes=15 * len(want))
        want.append(real.get_next_fire_time(previous, step_now))
        previous = want[-1]
    assert got == want
    assert mine.next_fire_times(None, now, 4) == _chain(real, None, now, 4)


def test_non_utc_timezone_is_refused():
    with pytest.raises(ValueError, match="UTC only"):
        ma.CronTrigger(second="*/5", timezone="Europe/Helsinki")


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_specs_match(seed):
    rng = random.Random(seed)
    now = datetime(
        2021 + rng.randint(0, 4), rng.randint(1, 12), rng.randint(1, 28),
        rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59), tzinfo=UTC,
    )
    for _ in range(10):
        spec = {
            name: rng.choice(pool)
            for name, pool in POOLS.items()
            if rng.random() < 0.6
        }
        try:
            mine, real = _pair(**spec)
        except ValueError:
            continue
        assert _chain(mine, None, now, 3) == _chain(real, None, now, 3), spec
