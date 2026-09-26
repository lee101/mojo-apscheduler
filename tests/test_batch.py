"""Tests for the batched entry points and the chunked, threaded fan-out."""

from __future__ import annotations

import random
from datetime import date, datetime

import numpy as np
import pytest
from conftest import UTC

import mojo_apscheduler as ma
from apscheduler.triggers.cron import CronTrigger as RealCronTrigger
from apscheduler.triggers.interval import IntervalTrigger as RealInterval

NOW = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)


def _cron_specs(count: int, seed: int = 0):
    rng = random.Random(seed)
    pools = {
        "day": ["1", "5", "15", "28", "last", "1,15", "8-14", "*/3", "2nd fri"],
        "month": ["*", "2", "6", "12", "1,4,7,10", "jan-mar", "*/2"],
        "hour": ["*", "0", "9-17", "*/4", "0,12"],
        "minute": ["*", "0", "15", "*/7", "0,30"],
        "second": ["*", "0", "*/15", "7"],
        "day_of_week": ["*", "mon", "mon-fri", "sat,sun"],
    }
    out = []
    while len(out) < count:
        spec = {
            name: rng.choice(pool)
            for name, pool in pools.items()
            if rng.random() < 0.6
        }
        try:
            ma.CronTrigger(timezone="UTC", **spec)
            RealCronTrigger(timezone="UTC", **spec)
        except ValueError:
            continue
        out.append(spec)
    return out


def test_cron_next_batch_matches_the_single_call():
    specs = _cron_specs(40, seed=1)
    triggers = [ma.CronTrigger(timezone="UTC", **s) for s in specs]
    expected = [t.get_next_fire_time(None, NOW) for t in triggers]
    got = ma.cron_next_batch(triggers, [None] * len(triggers), [NOW] * len(triggers))
    assert got == expected


def test_cron_next_batch_with_previous_fire_times():
    specs = _cron_specs(20, seed=2)
    triggers = [ma.CronTrigger(timezone="UTC", **s) for s in specs]
    previous = [datetime(2023, 6, 15, 3, 20, tzinfo=UTC) for _ in triggers]
    expected = [t.get_next_fire_time(p, NOW) for t, p in zip(triggers, previous)]
    got = ma.cron_next_batch(triggers, previous, [NOW] * len(triggers))
    assert got == expected


@pytest.mark.parametrize("workers", [1, 2, 3, 8])
def test_worker_chunking_does_not_change_the_answer(workers):
    """Chunk boundaries move the per-trigger field and expression offsets."""
    specs = _cron_specs(37, seed=3)
    triggers = [ma.CronTrigger(timezone="UTC", **s) for s in specs]
    baseline = ma.cron_next_batch(
        triggers, [None] * len(triggers), [NOW] * len(triggers), workers=1
    )
    got = ma.cron_next_batch(
        triggers, [None] * len(triggers), [NOW] * len(triggers), workers=workers
    )
    assert got == baseline


def test_cron_next_batch_empty():
    assert ma.cron_next_batch([], [], []) == []


def test_interval_next_batch_matches_the_single_call():
    triggers = [
        ma.IntervalTrigger(
            timezone="UTC",
            minutes=m,
            start_date=datetime(2023, 12, 1, tzinfo=UTC),
        )
        for m in (1, 7, 15, 30, 90)
    ]
    expected = [t.get_next_fire_time(None, NOW) for t in triggers]
    got = ma.interval_next_batch(triggers, [None] * len(triggers), NOW)
    assert got == expected


def test_interval_next_batch_reports_exhausted_triggers():
    end = datetime(2023, 12, 1, 0, 2, tzinfo=UTC)
    triggers = [
        ma.IntervalTrigger(
            timezone="UTC", minutes=1, start_date=datetime(2023, 12, 1, tzinfo=UTC),
            end_date=end,
        ),
        ma.IntervalTrigger(
            timezone="UTC", minutes=1, start_date=datetime(2023, 12, 1, tzinfo=UTC)
        ),
    ]
    previous = [datetime(2023, 12, 1, 0, 1, tzinfo=UTC)] * 2
    got = ma.interval_next_batch(triggers, previous, NOW)
    assert got[0] is None
    assert got[1] == datetime(2023, 12, 1, 0, 2, tzinfo=UTC)


def test_interval_next_batch_empty():
    assert ma.interval_next_batch([], [], NOW) == []


def test_cal_interval_next_batch_matches_the_single_call():
    triggers = [
        ma.CalendarIntervalTrigger(
            timezone="UTC",
            months=months,
            days=days,
            hour=3,
            start_date=date(2024, 1, 31) if months else date(2024, 1, 1),
        )
        for months, days in ((1, 0), (2, 0), (0, 7), (1, 3), (3, -2))
    ]
    expected = [t.get_next_fire_time(None) for t in triggers]
    got = ma.cal_interval_next_batch(triggers, [None] * len(triggers))
    assert got == expected


def test_cal_interval_next_batch_empty():
    assert ma.cal_interval_next_batch([], []) == []


def test_interval_next_batch_reports_exhausted_triggers():
    end = datetime(2023, 12, 1, 0, 1, tzinfo=UTC)
    triggers = [
        ma.IntervalTrigger(
            timezone="UTC", minutes=1, start_date=datetime(2023, 12, 1, tzinfo=UTC),
            end_date=end,
        ),
        ma.IntervalTrigger(
            timezone="UTC", minutes=1, start_date=datetime(2023, 12, 1, tzinfo=UTC)
        ),
    ]
    previous = [datetime(2023, 12, 1, 0, 1, tzinfo=UTC)] * 2
    got = ma.interval_next_batch(triggers, previous, NOW)
    assert got[0] is None
    assert got[1] == datetime(2023, 12, 1, 0, 2, tzinfo=UTC)


def test_batch_matches_the_real_apscheduler_end_to_end():
    """The batch entry point, checked against the real trigger, not against us."""
    specs = _cron_specs(25, seed=5)
    mine = [ma.CronTrigger(timezone="UTC", **s) for s in specs]
    real = [RealCronTrigger(timezone="UTC", **s) for s in specs]
    expected = [t.get_next_fire_time(None, NOW) for t in real]
    got = ma.cron_next_batch(mine, [None] * len(mine), [NOW] * len(mine), workers=4)
    assert got == expected


def test_date_trigger_is_forwarded():
    from apscheduler.triggers.date import DateTrigger as RealDate

    run = datetime(2030, 5, 5, 1, 2, 3, tzinfo=UTC)
    trigger = ma.DateTrigger(run_date=run, timezone="UTC")
    assert isinstance(trigger, RealDate)
    assert trigger.get_next_fire_time(None, NOW) == run
    assert trigger.get_next_fire_time(run, NOW) is None
