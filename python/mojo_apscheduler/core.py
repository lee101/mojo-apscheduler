"""Trigger classes whose fire-time arithmetic runs in Mojo.

Each class takes the same keyword arguments as its ``apscheduler`` counterpart
and returns the same datetimes, but the inner loop -- the cron field search,
the interval ``ceil``, the month rollover -- runs in
``src/kernels.mojo``. Expression grammar, the scheduler's start-date policy,
timezones and jitter stay on the Python side, because they are string and
calendar-object work rather than numeric loops.

The one restriction: the ported triggers are UTC-only. APScheduler's
``datetime_utc_add`` and the DST re-check in ``CalendarIntervalTrigger`` exist
to keep arithmetic correct across a DST transition, and that needs real
``tzinfo`` objects. For a non-UTC trigger, use the real ``apscheduler``.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import numpy as np

from . import _lib

__all__ = [
    "CalendarIntervalTrigger",
    "CronTrigger",
    "DateTrigger",
    "IntervalTrigger",
    "cal_interval_next_batch",
    "cron_next_batch",
]

UTC = timezone.utc

MIN_YEAR = 1
MAX_YEAR = 9999


def _require_utc(tz, who: str) -> None:
    if tz is not None and str(tz) not in ("UTC", "utc", "UTC+00:00"):
        raise ValueError(
            f"{who} is ported for UTC only; got timezone {tz!r}. "
            "Use the real apscheduler for other timezones."
        )


def _as_utc(value) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_supported(*values) -> None:
    for value in values:
        if value is not None and not (MIN_YEAR <= value.year <= MAX_YEAR):
            raise ValueError(f"year {value.year} is outside the supported range")


def _materialise(result) -> datetime | None:
    if result is None:
        return None
    days, secs = result
    d = _lib.from_day(days)
    return datetime(
        d.year,
        d.month,
        d.day,
        secs // 3600,
        (secs // 60) % 60,
        secs % 60,
        tzinfo=UTC,
    )


class CronTrigger:
    """UTC-only ``apscheduler.triggers.cron.CronTrigger`` with a Mojo search."""

    def __init__(
        self,
        year=None,
        month=None,
        day=None,
        week=None,
        day_of_week=None,
        hour=None,
        minute=None,
        second=None,
        start_date=None,
        end_date=None,
        timezone=None,
        jitter=None,
    ):
        from apscheduler.triggers.cron import CronTrigger as _Real

        _require_utc(timezone, "CronTrigger")
        self.timezone = UTC
        self.jitter = jitter
        # The real trigger owns the expression grammar; this port reads the
        # expressions it compiled instead of reimplementing the regexes.
        self._inner = _Real(
            year=year,
            month=month,
            day=day,
            week=week,
            day_of_week=day_of_week,
            hour=hour,
            minute=minute,
            second=second,
            start_date=_as_utc(start_date) if start_date else None,
            end_date=_as_utc(end_date) if end_date else None,
            timezone=UTC,
        )
        self.start_date = self._inner.start_date
        self.end_date = self._inner.end_date
        self.fields = self._inner.fields
        self._fields, self._exprs = _lib.lower_trigger(self._inner)

    def __str__(self):
        return str(self._inner)

    def __repr__(self):
        return f"mojo_apscheduler.CronTrigger({self._inner!s})"

    def _search_start(self, previous_fire_time, now) -> datetime:
        """The policy that picks where the field search begins.

        This is the scheduler-side half of
        ``CronTrigger.get_next_fire_time`` and it is calendar-object work, so
        it stays here; the field search itself is the kernel's job.
        """
        if previous_fire_time:
            candidate = previous_fire_time + timedelta(microseconds=1)
            start = min(now, candidate)
            if start == previous_fire_time:
                start = start + timedelta(microseconds=1)
            return start
        if self.start_date:
            return max(now, self.start_date)
        return now

    def _entry(self, start: datetime, end: datetime | None):
        return (
            self._fields,
            self._exprs,
            _lib.to_day(start.date()),
            _lib.to_secs(start),
            start.microsecond,
            -1 if end is None else _lib.to_day(end.date()),
            0 if end is None else _lib.to_secs(end),
        )

    def get_next_fire_time(self, previous_fire_time=None, now=None):
        now = _as_utc(now) if now is not None else datetime.now(UTC)
        previous = _as_utc(previous_fire_time) if previous_fire_time is not None else None
        _require_supported(now, previous, self.start_date, self.end_date)
        start = self._search_start(previous, now)
        return _materialise(_lib.cron_next([self._entry(start, self.end_date)])[0])

    def next_fire_times(self, previous_fire_time, now, count: int) -> list[datetime]:
        """The next ``count`` occurrences, chained one search per step."""
        previous = _as_utc(previous_fire_time) if previous_fire_time else None
        current = _as_utc(now)
        out: list[datetime] = []
        for _ in range(count):
            start = self._search_start(previous, current)
            fire = _materialise(_lib.cron_next([self._entry(start, self.end_date)])[0])
            if fire is None:
                break
            out.append(fire)
            previous = fire
        return out


def cron_next_batch(triggers, previous_times, now, workers: int = 1):
    """One kernel call for many triggers, which is the shape a scheduler wants.

    ``triggers`` is a sequence of :class:`CronTrigger`; ``previous_times`` and
    ``now`` are per-trigger ``datetime``\\ s (or ``None`` for "first fire").
    Returns one ``datetime`` or ``None`` per trigger.
    """
    entries = []
    for trigger, previous, current in zip(triggers, previous_times, now):
        previous = _as_utc(previous) if previous is not None else None
        current = _as_utc(current) if current is not None else datetime.now(UTC)
        _require_supported(current, previous, trigger.start_date, trigger.end_date)
        start = trigger._search_start(previous, current)
        entries.append(trigger._entry(start, trigger.end_date))
    return [_materialise(r) for r in _lib.cron_next(entries, workers=workers)]


class IntervalTrigger:
    """UTC-only ``apscheduler.triggers.interval.IntervalTrigger``."""

    def __init__(
        self,
        weeks=0,
        days=0,
        hours=0,
        minutes=0,
        seconds=0,
        start_date=None,
        end_date=None,
        timezone=None,
        jitter=None,
    ):
        _require_utc(timezone, "IntervalTrigger")
        self.interval = timedelta(
            weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds
        )
        self.interval_length = self.interval.total_seconds()
        if self.interval_length == 0:
            self.interval = timedelta(seconds=1)
            self.interval_length = 1
        self.timezone = UTC
        self.jitter = jitter
        self.start_date = _as_utc(start_date) if start_date else None
        self.end_date = _as_utc(end_date) if end_date else None

    def __str__(self):
        return f"interval[{self.interval}]"

    def __repr__(self):
        return f"mojo_apscheduler.IntervalTrigger({self.interval!r})"

    def _row(self, previous_fire_time, now) -> np.ndarray:
        previous = np.nan if previous_fire_time is None else previous_fire_time.timestamp()
        start = self.start_date.timestamp()
        end = np.nan if self.end_date is None else self.end_date.timestamp()
        return np.array(
            [previous, start, end, self.interval_length, now.timestamp()],
            dtype=np.float64,
        )

    def get_next_fire_time(self, previous_fire_time=None, now=None):
        now = _as_utc(now) if now is not None else datetime.now(UTC)
        previous = _as_utc(previous_fire_time) if previous_fire_time is not None else None
        _require_supported(now, previous, self.start_date, self.end_date)
        if self.start_date is None:
            # The real trigger defaults start_date to now + interval. That is a
            # wall-clock default, not arithmetic, so it stays in Python.
            self.start_date = now + self.interval
        value = float(_lib.interval_next(self._row(previous, now).reshape(1, 5))[0])
        if value < 0:
            return None
        if self.jitter:
            value += random.uniform(0, self.jitter)
        return datetime.fromtimestamp(value, tz=UTC)

    def next_fire_times(self, previous_fire_time, now, count: int) -> list[datetime]:
        now = _as_utc(now)
        previous = _as_utc(previous_fire_time) if previous_fire_time else None
        if self.start_date is None:
            self.start_date = now + self.interval
        out: list[datetime] = []
        for _ in range(count):
            fire = self.get_next_fire_time(previous, now)
            if fire is None:
                break
            out.append(fire)
            previous = fire
        return out


def interval_next_batch(triggers, previous_times, now, workers: int = 1):
    """One kernel call for many ``IntervalTrigger``\\ s."""
    now = _as_utc(now)
    rows = []
    for trigger, previous in zip(triggers, previous_times):
        previous = _as_utc(previous) if previous is not None else None
        if trigger.start_date is None:
            trigger.start_date = now + trigger.interval
        rows.append(trigger._row(previous, now))
    values = _lib.interval_next(np.asarray(rows, dtype=np.float64), workers=workers)
    return [None if v < 0 else datetime.fromtimestamp(float(v), tz=UTC) for v in values]


class CalendarIntervalTrigger:
    """UTC-only ``apscheduler.triggers.calendarinterval.CalendarIntervalTrigger``."""

    def __init__(
        self,
        *,
        years=0,
        months=0,
        weeks=0,
        days=0,
        hour=0,
        minute=0,
        second=0,
        start_date=None,
        end_date=None,
        timezone=None,
        jitter=None,
    ):
        from apscheduler.triggers.calendarinterval import (
            CalendarIntervalTrigger as _Real,
        )

        _require_utc(timezone, "CalendarIntervalTrigger")
        self._inner = _Real(
            years=years,
            months=months,
            weeks=weeks,
            days=days,
            hour=hour,
            minute=minute,
            second=second,
            start_date=start_date,
            end_date=end_date,
            timezone=UTC,
            jitter=jitter,
        )
        self.jitter = jitter
        self.years = years
        self.months = months
        self.weeks = weeks
        self.days = days
        self.hour = hour
        self.minute = minute
        self.second = second
        self.start_date = self._inner.start_date
        self.end_date = self._inner.end_date

    def __repr__(self):
        return f"mojo_apscheduler.CalendarIntervalTrigger({self._inner!s})"

    def _row(self, previous_fire_time) -> np.ndarray:
        prev_day = -1
        prev_secs = 0
        if previous_fire_time is not None:
            prev_day = _lib.to_day(previous_fire_time.date())
            prev_secs = _lib.to_secs(previous_fire_time)
        return np.array(
            [
                self.years,
                self.months,
                self.weeks,
                self.days,
                self.hour,
                self.minute,
                self.second,
                _lib.to_day(self.start_date),
                -1 if self.end_date is None else _lib.to_day(self.end_date),
                prev_day,
                prev_secs,
            ],
            dtype=np.int32,
        )

    def get_next_fire_time(self, previous_fire_time=None, now=None):
        previous = _as_utc(previous_fire_time) if previous_fire_time is not None else None
        _require_supported(previous)
        result = _lib.cal_interval_next(
            self._row(previous).reshape(1, _lib.CAL_STRIDE)
        )[0]
        fire = _materialise(result)
        if fire is not None and self.jitter:
            fire = fire + timedelta(seconds=random.uniform(0, self.jitter))
        return fire


def cal_interval_next_batch(triggers, previous_times, workers: int = 1):
    """One kernel call for many ``CalendarIntervalTrigger``\\ s."""
    rows = []
    for trigger, previous in zip(triggers, previous_times):
        previous = _as_utc(previous) if previous is not None else None
        rows.append(trigger._row(previous))
    cal = np.asarray(rows, dtype=np.int32)
    return [_materialise(r) for r in _lib.cal_interval_next(cal, workers=workers)]


def DateTrigger(run_date=None, timezone=None):
    """``apscheduler.triggers.date.DateTrigger``, forwarded unchanged.

    It fires once and its arithmetic is a single field read, so there is no
    loop to port; the real class is returned rather than a copy of it.
    """
    from apscheduler.triggers.date import DateTrigger as _Real

    return _Real(run_date=run_date, timezone=timezone)
