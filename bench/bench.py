"""Correctness-gated benchmark for mojo-apscheduler.

Every case compares the compiled result against the real ``apscheduler``
triggers before timing, so a regression in the Mojo kernels shows up as a
correctness failure rather than a suspiciously good number. The baseline is the
real package computing the same fire times one trigger at a time.
"""

from __future__ import annotations

import pathlib
import random
import sys
import time
from datetime import datetime, timezone

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "python"))

import mojo_apscheduler as ma  # noqa: E402
from apscheduler.triggers.calendarinterval import (  # noqa: E402
    CalendarIntervalTrigger as RealCalendar,
)
from apscheduler.triggers.cron import CronTrigger as RealCron  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger as RealInterval  # noqa: E402

UTC = timezone.utc
NOW = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)

# A weekday-position day ("2nd fri") combined with a day_of_week constraint can
# be unsatisfiable, and the real search then walks for seconds. Those specs
# belong in their own case below, not in the steady-state baseline.
_POOLS = {
    "day": ["1", "5", "15", "28", "last", "1,15", "8-14", "*/3"],
    "month": ["*", "2", "6", "12", "1,4,7,10", "jan-mar", "*/2"],
    "hour": ["*", "0", "9-17", "*/4", "0,12"],
    "minute": ["*", "0", "15", "*/7", "0,30"],
    "second": ["*", "0", "*/15", "7"],
    "day_of_week": ["*", "mon", "mon-fri", "sat,sun"],
}


def _time(fn, repeats=3):
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def _cron_pair(n, seed=0):
    rng = random.Random(seed)
    specs = []
    while len(specs) < n:
        spec = {
            name: rng.choice(pool)
            for name, pool in _POOLS.items()
            if rng.random() < 0.6
        }
        specs.append(spec)
    mine = [ma.CronTrigger(timezone="UTC", **s) for s in specs]
    real = [RealCron(timezone="UTC", **s) for s in specs]
    return mine, real


def bench_cron_batch(n=1500):
    mine, real = _cron_pair(n, seed=1)
    _ = real
    expected = [t.get_next_fire_time(None, NOW) for t in real]
    got = ma.cron_next_batch(mine, [None] * n, [NOW] * n)
    assert got == expected, "cron_next_batch mismatch"

    python_time = _time(
        lambda: [t.get_next_fire_time(None, NOW) for t in real], 1
    )
    mojo_time = _time(lambda: ma.cron_next_batch(mine, [None] * n, [NOW] * n))
    return f"cron batch n={n}", python_time, mojo_time


def bench_cron_batch_threaded(n=4000, workers=8):
    """Chunked across a thread pool, which is the shape a scheduler wants."""
    mine, real = _cron_pair(n, seed=2)
    baseline = ma.cron_next_batch(mine, [None] * n, [NOW] * n, workers=1)
    got = ma.cron_next_batch(mine, [None] * n, [NOW] * n, workers=workers)
    assert got == baseline, "threaded cron batch mismatch"

    serial = _time(lambda: ma.cron_next_batch(mine, [None] * n, [NOW] * n, workers=1))
    threaded = _time(
        lambda: ma.cron_next_batch(mine, [None] * n, [NOW] * n, workers=workers)
    )
    return f"cron batch n={n} x{workers} threads", serial, threaded


def bench_cron_unsatisfiable():
    """An expression that can never be satisfied.

    ``day="2nd fri"`` with ``day_of_week="sat,sun"`` has no solution, so both
    implementations give up -- but the real search walks for seconds before it
    does, where the kernel's iteration bound stops it in milliseconds.
    """
    spec = {
        "day": "2nd fri", "month": "*", "hour": "*", "second": "0",
        "day_of_week": "sat,sun",
    }
    mine = ma.CronTrigger(timezone="UTC", **spec)
    real = RealCron(timezone="UTC", **spec)
    assert mine.get_next_fire_time(None, NOW) == real.get_next_fire_time(None, NOW)
    assert mine.get_next_fire_time(None, NOW) is None
    return (
        "cron unsatisfiable",
        _time(lambda: real.get_next_fire_time(None, NOW), 1),
        _time(lambda: mine.get_next_fire_time(None, NOW), 1),
    )


def bench_interval_batch(n=4000):
    mine = [
        ma.IntervalTrigger(
            timezone="UTC", minutes=(i % 59) + 1,
            start_date=datetime(2023, 12, 1, tzinfo=UTC),
        )
        for i in range(n)
    ]
    real = [
        RealInterval(
            timezone="UTC", minutes=(i % 59) + 1,
            start_date=datetime(2023, 12, 1, tzinfo=UTC),
        )
        for i in range(n)
    ]
    expected = [t.get_next_fire_time(None, NOW) for t in real]
    got = ma.interval_next_batch(mine, [None] * n, NOW)
    assert got == expected, "interval batch mismatch"

    python_time = _time(lambda: [t.get_next_fire_time(None, NOW) for t in real], 1)
    mojo_time = _time(lambda: ma.interval_next_batch(mine, [None] * n, NOW))
    return f"interval batch n={n}", python_time, mojo_time


def bench_calendar_batch(n=2000):
    from datetime import date

    mine, real = [], []
    for i in range(n):
        kwargs = dict(
            months=(i % 11) + 1, days=i % 5, hour=i % 24,
            start_date=date(2024, 1, 1),
        )
        mine.append(ma.CalendarIntervalTrigger(timezone="UTC", **kwargs))
        real.append(RealCalendar(timezone="UTC", **kwargs))
    expected = [t.get_next_fire_time(None, NOW) for t in real]
    got = ma.cal_interval_next_batch(mine, [None] * n)
    assert got == expected, "calendar batch mismatch"

    python_time = _time(lambda: [t.get_next_fire_time(None, NOW) for t in real], 1)
    mojo_time = _time(lambda: ma.cal_interval_next_batch(mine, [None] * n))
    return f"calendar batch n={n}", python_time, mojo_time


def main():
    print(f"{'case':<28}{'real apscheduler':>19}{'mojo-apscheduler':>20}{'ratio':>9}")
    print("-" * 76)
    for fn in (
        bench_cron_batch,
        bench_cron_batch_threaded,
        bench_cron_unsatisfiable,
        bench_interval_batch,
        bench_calendar_batch,
    ):
        label, ref, got = fn()
        ratio = ref / got if got else float("nan")
        print(f"{label:<28}{ref * 1e3:>17.2f}ms{got * 1e3:>18.2f}ms{ratio:>8.2f}x")


if __name__ == "__main__":
    main()
