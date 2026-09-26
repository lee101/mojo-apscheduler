"""ctypes bridge to the compiled Mojo trigger kernels.

The shared library owns no memory. Every buffer crosses the C ABI as a 64-bit
address, so the argtypes below must stay ``c_int64`` for addresses; ``c_int``
truncates them and segfaults.

The cron kernel does not know anything about cron syntax. The Python layer
lowers a real ``apscheduler.triggers.cron.CronTrigger`` -- introspecting the
expression objects apscheduler already compiled -- into the flat descriptor
tables the kernel walks, and converts between ``datetime`` and the kernel's
(days, second-of-day) representation with ``date.toordinal``, which is exact.
"""

from __future__ import annotations

import ctypes
import pathlib
from concurrent.futures import ThreadPoolExecutor

import numpy as np

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
_LIB_PATH = _ROOT / "dist" / "libmojo-apscheduler.so"

_ADDR = ctypes.c_int64
_EPOCH_ORDINAL = 719163  # date(1970, 1, 1).toordinal()

NFIELDS = 8
FIELD_STRIDE = 3
EXPR_STRIDE = 4
TRIG_STRIDE = 7
CAL_STRIDE = 11
SCRATCH_STRIDE = 16

KIND_ALL = 0
KIND_RANGE = 1
KIND_WEEKDAY_POSITION = 2
KIND_LAST_DAY_OF_MONTH = 3

MAX_WORKERS = 8


def _load():
    if not _LIB_PATH.exists():
        raise RuntimeError(
            f"{_LIB_PATH} not found; run `bash build/build.sh` first"
        )
    lib = ctypes.CDLL(str(_LIB_PATH))
    lib.aps_cron_next_range.restype = None
    lib.aps_cron_next_range.argtypes = [_ADDR] * 7
    lib.aps_interval_next_range.restype = None
    lib.aps_interval_next_range.argtypes = [_ADDR] * 4
    lib.aps_cal_interval_next_range.restype = None
    lib.aps_cal_interval_next_range.argtypes = [_ADDR] * 5
    return lib


lib = _load()


# ------------------------------------------------------------ date bridge


def to_day(value) -> int:
    """Days since 1970-01-01 for a date or datetime, exactly as Mojo computes it."""
    return value.toordinal() - _EPOCH_ORDINAL


def from_day(days: int):
    """Inverse of :func:`to_day`; returns a ``date``."""
    from datetime import date

    return date.fromordinal(days + _EPOCH_ORDINAL)


def to_secs(value) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


# ------------------------------------------------------ cron descriptors


def _lower_expression(expr) -> tuple[int, int, int, int]:
    from apscheduler.triggers.cron.expressions import (
        AllExpression,
        LastDayOfMonthExpression,
        RangeExpression,
        WeekdayPositionExpression,
    )

    if isinstance(expr, WeekdayPositionExpression):
        return (KIND_WEEKDAY_POSITION, expr.weekday, expr.option_num, 0)
    if isinstance(expr, LastDayOfMonthExpression):
        return (KIND_LAST_DAY_OF_MONTH, 0, 0, 0)
    if isinstance(expr, RangeExpression):
        # MonthRangeExpression and WeekdayRangeExpression are RangeExpression
        # subclasses whose first/last are already resolved to numbers.
        return (KIND_RANGE, expr.first, -1 if expr.last is None else expr.last, expr.step or 0)
    if isinstance(expr, AllExpression):
        return (KIND_ALL, 0, 0, expr.step or 0)
    raise TypeError(f"unsupported cron expression {type(expr).__name__}")


def lower_trigger(trigger) -> tuple[np.ndarray, np.ndarray]:
    """Flatten a CronTrigger's compiled fields into (fields, exprs) tables."""
    from apscheduler.triggers.cron import CronTrigger

    if not isinstance(trigger, CronTrigger):
        raise TypeError("expected an apscheduler CronTrigger")
    if len(trigger.fields) != NFIELDS:
        raise ValueError("expected the eight cron fields")

    fields = np.zeros(NFIELDS * FIELD_STRIDE, dtype=np.int32)
    exprs: list[int] = []
    for i, field in enumerate(trigger.fields):
        base = i * FIELD_STRIDE
        fields[base] = 1 if field.REAL else 0
        fields[base + 1] = len(exprs) // EXPR_STRIDE
        fields[base + 2] = len(field.expressions)
        for expr in field.expressions:
            exprs.extend(_lower_expression(expr))
    if not exprs:
        exprs = [KIND_ALL, 0, 0, 0]
        fields[1] = 0
        fields[2] = 1
    return fields, np.asarray(exprs, dtype=np.int32)


def _pack_triggers(entries) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    fields: list[int] = []
    exprs: list[int] = []
    trig = np.zeros(len(entries) * TRIG_STRIDE, dtype=np.int32)
    for i, (f, e, sd, ss, su, ed, es) in enumerate(entries):
        base = i * TRIG_STRIDE
        # The field and expression offsets must be read before this trigger's
        # own rows are appended, or every trigger but the first reads past the
        # end of the table.
        trig[base] = len(fields) // FIELD_STRIDE
        trig[base + 1] = len(exprs) // EXPR_STRIDE
        fields.extend(f.tolist())
        exprs.extend(e.tolist())
        trig[base + 2] = sd
        trig[base + 3] = ss
        trig[base + 4] = su
        trig[base + 5] = ed
        trig[base + 6] = es
    return (
        np.asarray(fields, dtype=np.int32),
        np.asarray(exprs, dtype=np.int32),
        trig,
    )


def _chunks(n: int, workers: int) -> list[tuple[int, int]]:
    if n <= 0:
        return []
    if workers <= 1 or n == 1:
        return [(0, n)]
    step = (n + workers - 1) // workers
    return [(lo, min(lo + step, n)) for lo in range(0, n, step)]


def _run(fn, spans, *args) -> None:
    for lo, hi in spans:
        fn(*args, lo, hi)


def cron_next(entries, workers: int = 1) -> list[tuple[int, int] | None]:
    """Next fire time for each prepared trigger; None where there is none.

    Each entry is a ``(fields, exprs, start_days, start_secs, start_usec,
    end_days, end_secs)`` tuple as produced by :func:`build_entry`. Results are
    ``(days, seconds_of_day)`` pairs in the trigger's own wall clock.
    """
    n = len(entries)
    if n == 0:
        return []
    fields, exprs, trig = _pack_triggers(entries)
    scratch = np.zeros(SCRATCH_STRIDE * n, dtype=np.int32)
    dst = np.zeros(2 * n, dtype=np.int32)
    spans = _chunks(n, workers)

    def call(lo, hi):
        lib.aps_cron_next_range(
            trig.ctypes.data,
            fields.ctypes.data,
            exprs.ctypes.data,
            lo,
            hi,
            scratch.ctypes.data,
            dst.ctypes.data,
        )

    _run(call, spans)
    return [
        None if dst[2 * i] < 0 else (int(dst[2 * i]), int(dst[2 * i + 1]))
        for i in range(n)
    ]


def build_entry(trigger, start, end=None):
    """Prepare one CronTrigger for :func:`cron_next`."""
    fields, exprs = lower_trigger(trigger)
    return (
        fields,
        exprs,
        to_day(start.date() if hasattr(start, "date") else start),
        to_secs(start),
        start.microsecond if hasattr(start, "microsecond") else 0,
        -1 if end is None else to_day(end.date() if hasattr(end, "date") else end),
        0 if end is None else to_secs(end),
    )


def interval_next(cfg: np.ndarray, workers: int = 1) -> np.ndarray:
    """Next fire timestamps for prepared IntervalTrigger rows; -1.0 for none."""
    n = cfg.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.float64)
    dst = np.zeros(n, dtype=np.float64)

    def call(lo, hi):
        lib.aps_interval_next_range(cfg.ctypes.data, lo, hi, dst.ctypes.data)

    _run(call, _chunks(n, workers))
    return dst


def cal_interval_next(cal: np.ndarray, workers: int = 1) -> list[tuple[int, int] | None]:
    """Next fire times for prepared CalendarIntervalTrigger rows."""
    n = cal.shape[0]
    if n == 0:
        return []
    scratch = np.zeros(SCRATCH_STRIDE * n, dtype=np.int32)
    dst = np.zeros(2 * n, dtype=np.int32)

    def call(lo, hi):
        lib.aps_cal_interval_next_range(
            cal.ctypes.data, lo, hi, scratch.ctypes.data, dst.ctypes.data
        )

    _run(call, _chunks(n, workers))
    return [
        None if dst[2 * i] < 0 else (int(dst[2 * i]), int(dst[2 * i + 1]))
        for i in range(n)
    ]


def pool(workers: int) -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=max(1, min(workers, MAX_WORKERS)))
