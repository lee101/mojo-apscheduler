"""mojo-apscheduler: APScheduler trigger arithmetic with Mojo kernels.

Installable alongside the real ``apscheduler``, whose triggers it is tested
against for parity. The cron field search, the interval ``ceil`` and the
calendar-interval month rollover live in ``src/kernels.mojo``; expression
parsing, timezones and jitter stay with the real package.
"""

from .core import (
    CalendarIntervalTrigger,
    CronTrigger,
    DateTrigger,
    IntervalTrigger,
    cal_interval_next_batch,
    cron_next_batch,
    interval_next_batch,
)

__all__ = [
    "CalendarIntervalTrigger",
    "CronTrigger",
    "DateTrigger",
    "IntervalTrigger",
    "cal_interval_next_batch",
    "cron_next_batch",
    "interval_next_batch",
]
__version__ = "0.1.0"
