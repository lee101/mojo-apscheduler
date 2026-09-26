# mojo-apscheduler

`mojo-apscheduler` is a Mojo port of the compute side of
[APScheduler](https://apscheduler.readthedocs.io/): the arithmetic that
decides *when a job fires next*. A scheduler with a few thousand jobs runs that
arithmetic a few thousand times every tick, and each call is a loop over
calendar fields or a repeated `ceil` -- exactly the work a compiled inner loop
is for.

The Python package is `mojo_apscheduler`, so it installs alongside the real
`apscheduler` and the tests compare the two directly.

```python
from datetime import datetime, timezone
import mojo_apscheduler as ma

t = ma.CronTrigger(hour="9-17", minute="*/15", day_of_week="mon-fri", timezone="UTC")
t.get_next_fire_time(None, datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc))
# datetime(2024, 3, 1, 12, 15, tzinfo=datetime.timezone.utc)

ma.cron_next_batch(triggers, [None] * len(triggers), [now] * len(triggers))
# one kernel call for a whole scheduler's worth of triggers
```

## Covered subset

| area | implemented API | kernel |
| --- | --- | --- |
| Cron search | `CronTrigger.get_next_fire_time`, `next_fire_times` | `aps_cron_next_range` |
| Cron, many at once | `cron_next_batch(triggers, previous, now, workers=)` | `aps_cron_next_range` |
| Interval | `IntervalTrigger.get_next_fire_time`, `next_fire_times` | `aps_interval_next_range` |
| Interval, many at once | `interval_next_batch(triggers, previous, now, workers=)` | `aps_interval_next_range` |
| Calendar interval | `CalendarIntervalTrigger.get_next_fire_time` | `aps_cal_interval_next_range` |
| Calendar, many at once | `cal_interval_next_batch(triggers, previous, workers=)` | `aps_cal_interval_next_range` |
| Date trigger | `DateTrigger` -- the real class, returned as-is | none |

The cron kernel covers all four expression classes APScheduler compiles:
`AllExpression` (with a step), `RangeExpression` (and its `Month`/`Weekday`
subclasses), `WeekdayPositionExpression` (`2nd fri`, `last fri`) and
`LastDayOfMonthExpression` (`last`). It also covers the two pseudo-fields
`week` (ISO week) and `day_of_week`, which APScheduler tests but never writes,
so a miss on either has to carry into the day of month.

What stays on the Python side, and why:

* **Expression parsing.** The real `CronTrigger` compiles the cron grammar into
  expression objects; this port reads those objects and lowers them to the flat
  descriptor tables the kernel walks. Reimplementing the regexes would be
  porting string work and would risk disagreeing with the real parser.
* **The start-date policy.** `min(now, previous + 1us)` and
  `max(now, start_date)` are calendar-object operations on `datetime`
  instances, not loops.
* **Jitter.** `random.uniform(0, jitter)` is the module-level RNG; the kernel
  computes the un-jittered fire time and the shim adds the same call, so a
  seeded RNG reproduces the real trigger exactly.
* **Timezones.** See the restriction below.

Not implemented: `CronTrigger.from_crontab` is not overridden (use the real
class), neither are `DateTrigger`'s arithmetic (it is a single field read, so
the real class is returned instead of a copy), the job/executor/scheduler
machinery, the jobstores, and the asyncio/gevent/tornado executors.

**The ported triggers are UTC-only.** APScheduler's `datetime_utc_add` and the
DST re-check in `CalendarIntervalTrigger` exist to keep the arithmetic correct
across a DST transition, and that needs real `tzinfo` objects. A non-UTC
timezone raises `ValueError` rather than returning a plausible wrong answer; use
the real `apscheduler` there.

## Parity

131 tests, all against the real `apscheduler` triggers, not against a
restatement of them:

* every cron spec is built twice, once as `mojo_apscheduler.CronTrigger` and
  once as `apscheduler.triggers.cron.CronTrigger`, and both the first fire time
  and a chain of six following fires are compared. A kernel that agrees on the
  first call and carries wrong on the second fails.
* the same shape for `IntervalTrigger` and `CalendarIntervalTrigger`.
* the boundary rules are pinned explicitly: `datetime_ceil` after the one
  microsecond that separates consecutive fires, `ceil(1800/1800) == 1` keeping
  a `now` that lands exactly on a boundary, the end date being inclusive on an
  exact second, the 29th of February existing only in a leap year, and 31
  January plus one month landing on 31 March because February has no 31st.
* the batched entry points are compared against the per-trigger calls *and*
  against the real triggers, and `cron_next_batch` is run at 1, 2, 3 and 8
  workers to prove that chunk boundaries -- which move every trigger's field
  and expression offsets -- do not change the answer.

All comparisons are exact. The cron and calendar-interval kernels are integer
arithmetic on day counts and second counts, so there is nothing to approximate;
no tolerance is needed anywhere.

## Install

```bash
pixi install
pixi run build
pixi run test
```

`pixi run build` produces `dist/libmojo-apscheduler.so`. Set
`PYTHONPATH=python` outside a Pixi task. With the shared toolchain:

```bash
source /nvme0n1-disk/mojo-toolchain/activate.sh
bash build/build.sh
PYTHONPATH=python /nvme0n1-disk/mojo-toolchain/testvenv/bin/python -m pytest tests -q
```

## Performance

Best-of-three wall clock in one process, against the real `apscheduler`
computing the same fire times one trigger at a time. Every case asserts
equality with the real triggers before timing.

| case | real apscheduler | mojo-apscheduler | result |
| --- | ---: | ---: | ---: |
| cron batch n=1500 | 152.20 ms | 37.11 ms | 4.10x faster |
| cron batch n=4000, 8 threads vs serial | 61.79 ms | 77.91 ms | 0.79x, a loss |
| cron, unsatisfiable expression | 12494.27 ms | 211.63 ms | 59.04x faster |
| interval batch n=4000 | 31.51 ms | 46.83 ms | 0.67x, a loss |
| calendar interval batch n=2000 | 34.29 ms | 90.77 ms | 0.38x, a loss |

Reproduce with:

```bash
pixi run bench
```

Only the cron case is a clear win, and the other three are reported as losses
because they are losses. The reason is the same in all of them: an interval
fire time is one `ceil` and a calendar-interval fire time is one month
addition, so there is nothing to compile -- and the per-trigger Python work of
packing a descriptor array dominates. The cron search is the only one of the
three that actually walks, and it is the only one that wins. Use the interval
and calendar-interval entry points when you want one kernel call for many
triggers, not for speed on a single one.

Threading the cron batch with 8 workers measured *slower* than serial here, and
reported as such. The per-trigger search is a few hundred field operations, so
the GIL released around the ctypes call and the thread fan-out do not recover
their cost on this workload. A box with a real scheduler load -- bigger
`int32` descriptor tables, colder caches -- could go either way; the honest
number from this machine is the one above.

The unsatisfiable row is the largest single difference. `day="2nd fri"` with
`day_of_week="sat,sun"` has no solution, and the real search walks for twelve
seconds before giving up. The kernel bounds the walk at a million field
iterations, reaches the same `None` in 0.2 s, and the test asserts the two
answers are equal. An earlier draft of this benchmark mixed such specs into the
steady-state cron batch, which inflated the reference to 156 seconds and
overstated the win; they now have their own row.

This box is shared, so repeated runs vary. The cron-batch ordering and the
interval and calendar-interval losses were stable across runs; the threaded
row moved between 0.8x and 1.3x.

## How it works

All kernels live in `src/kernels.mojo`, one compilation unit.
`build/build.sh` compiles it with `mojo build --emit shared-lib` into
`dist/libmojo-apscheduler.so`.

The cron kernel is a faithful port of `CronTrigger.get_next_fire_time`. It walks
the eight fields from year to second; when a field allows no value at or after
the current date, it carries into the next more significant field and resets the
less significant ones. `week` and `day_of_week` are pseudo-fields with
`REAL = False`, so a hit on them is tested but not written, and a miss carries
past them into the day. The civil-date conversions are Howard Hinnant's
`days_from_civil` and `civil_from_days`, so there is no month-length table and
no timezone in the kernel at all.

Everything crosses the C ABI as flat integer tables and 64-bit addresses. A
trigger is 7 `int32`; a field is 3; an expression is 4 (kind, a, b, step). The
Python layer packs many triggers into one `fields` table and one `exprs` table
and hands the kernel a start index per trigger. The `*_range` entry points take
a `[n0, n1)` slice so the shim can fan out over a `ThreadPoolExecutor`, which
releases the GIL around each call.

Results come back as `(day, second_of_day)` pairs and are turned into
`datetime` objects with `date.fromordinal`, so the conversion in both
directions is exact and timezone-free.

## License

MIT
