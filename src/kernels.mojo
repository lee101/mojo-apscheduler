"""Calendar arithmetic for APScheduler's triggers.

Three kernels, all of it pure integer calendar work:

* ``aps_cron_next_range`` -- the field search from
  ``apscheduler.triggers.cron.CronTrigger.get_next_fire_time``: walk the eight
  cron fields, and on a miss carry into the next more significant field and
  reset the ones below it. ``week`` and ``day_of_week`` are pseudo-fields with
  ``REAL = False``: they are tested but never written.
* ``aps_interval_next_range`` -- ``IntervalTrigger.get_next_fire_time``:
  previous plus the interval, or start plus a whole number of intervals chosen
  by ``ceil``.
* ``aps_cal_interval_next_range`` --
  ``CalendarIntervalTrigger.get_next_fire_time``: add years and months keeping
  the day of month, retrying until the date exists, then add weeks and days.

The civil-date conversions are Howard Hinnant's ``days_from_civil`` and
``civil_from_days``, so no month-length table is needed. Buffers cross the C
ABI as 64-bit addresses because ``@export`` rejects parametric functions and an
inferred pointer origin would make a symbol parametric.
"""

comptime I32Ptr = Pointer[Int32, AnyOrigin[mut=True]]
comptime F64Ptr = Pointer[Float64, AnyOrigin[mut=True]]

comptime NFIELDS = 8
comptime FIELD_STRIDE = 3
comptime EXPR_STRIDE = 4
comptime TRIG_STRIDE = 7
comptime CAL_STRIDE = 11
comptime SCRATCH_STRIDE = 16

# st[] layout for the working datetime; slots 8..10 are the ISO-week scratch.
comptime ST_YEAR = 0
comptime ST_MONTH = 1
comptime ST_DAY = 2
comptime ST_HOUR = 3
comptime ST_MINUTE = 4
comptime ST_SECOND = 5
comptime ST_ISO = 8


def i32p(addr: Int) -> I32Ptr:
    return I32Ptr(unsafe_from_address=addr)


def f64p(addr: Int) -> F64Ptr:
    return F64Ptr(unsafe_from_address=addr)


# ---------------------------------------------------------------- calendar


def is_leap(y: Int) -> Bool:
    return (y % 4 == 0 and y % 100 != 0) or y % 400 == 0


def days_in_month(y: Int, m: Int) -> Int:
    if m == 2:
        return 29 if is_leap(y) else 28
    if m == 4 or m == 6 or m == 9 or m == 11:
        return 30
    return 31


def days_from_civil(y_in: Int, m: Int, d: Int) -> Int:
    """Days since 1970-01-01 for a proleptic Gregorian date."""
    var y = y_in
    if m <= 2:
        y -= 1
    var era = y // 400
    var yoe = y - era * 400
    var mp = m - 3 if m > 2 else m + 9
    var doy = (153 * mp + 2) // 5 + d - 1
    var doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def civil_from_days(z_in: Int, st: I32Ptr):
    """Write the year, month and day of a day count into st[0..2]."""
    var z = z_in + 719468
    var era = z // 146097
    var doe = z - era * 146097
    var yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    var y = yoe + era * 400
    var doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    var mp = (5 * doy + 2) // 153
    var d = doy - (153 * mp + 2) // 5 + 1
    var m = mp + 3 if mp < 10 else mp - 9
    st[unsafe_offset=ST_YEAR] = Int32(y + (1 if m <= 2 else 0))
    st[unsafe_offset=ST_MONTH] = Int32(m)
    st[unsafe_offset=ST_DAY] = Int32(d)


def floor_mod7(v: Int) -> Int:
    var r = v % 7
    if r < 0:
        r += 7
    return r


def weekday(days: Int) -> Int:
    """Python's date.weekday(): 0 is Monday, and 1970-01-01 was a Thursday."""
    return floor_mod7(days + 3)


def iso_week(st: I32Ptr) -> Int:
    """ISO 8601 week number, which is what WeekField.get_value returns."""
    var days = days_from_civil(
        Int(st[unsafe_offset=ST_YEAR]),
        Int(st[unsafe_offset=ST_MONTH]),
        Int(st[unsafe_offset=ST_DAY]),
    )
    # An ISO week belongs to the year of its Thursday.
    var thursday = days - weekday(days) + 3
    civil_from_days(thursday, st + ST_ISO)
    var year = Int(st[unsafe_offset=ST_ISO])
    var jan1 = days_from_civil(year, 1, 1)
    return (thursday - jan1) // 7 + 1


def secs_of_day(st: I32Ptr) -> Int:
    return (
        Int(st[unsafe_offset=ST_HOUR]) * 3600
        + Int(st[unsafe_offset=ST_MINUTE]) * 60
        + Int(st[unsafe_offset=ST_SECOND])
    )


def load_st(st: I32Ptr, days: Int, secs: Int):
    civil_from_days(days, st)
    st[unsafe_offset=ST_HOUR] = Int32(secs // 3600)
    st[unsafe_offset=ST_MINUTE] = Int32((secs // 60) % 60)
    st[unsafe_offset=ST_SECOND] = Int32(secs % 60)


def st_days(st: I32Ptr) -> Int:
    return days_from_civil(
        Int(st[unsafe_offset=ST_YEAR]),
        Int(st[unsafe_offset=ST_MONTH]),
        Int(st[unsafe_offset=ST_DAY]),
    )


# ------------------------------------------------------------- cron fields


def field_is_real(fields: I32Ptr, fidx: Int) -> Bool:
    return fields[unsafe_offset=fidx * FIELD_STRIDE] != Int32(0)


def field_min(fieldnum: Int) -> Int:
    if fieldnum == 0:
        return 1970
    if fieldnum >= 1 and fieldnum <= 3:
        return 1
    return 0


def field_max(st: I32Ptr, fieldnum: Int) -> Int:
    if fieldnum == 0:
        return 9999
    if fieldnum == 1:
        return 12
    if fieldnum == 2:
        return days_in_month(
            Int(st[unsafe_offset=ST_YEAR]), Int(st[unsafe_offset=ST_MONTH])
        )
    if fieldnum == 3:
        return 53
    if fieldnum == 4:
        return 6
    if fieldnum == 5:
        return 23
    if fieldnum == 6:
        return 59
    return 59


def field_value(st: I32Ptr, days: Int, fieldnum: Int) -> Int:
    if fieldnum == 0:
        return Int(st[unsafe_offset=ST_YEAR])
    if fieldnum == 1:
        return Int(st[unsafe_offset=ST_MONTH])
    if fieldnum == 2:
        return Int(st[unsafe_offset=ST_DAY])
    if fieldnum == 3:
        return iso_week(st)
    if fieldnum == 4:
        return weekday(days)
    var secs = secs_of_day(st)
    if fieldnum == 5:
        return secs // 3600
    if fieldnum == 6:
        return (secs // 60) % 60
    return secs % 60


def expr_next_value(
    exprs: I32Ptr, eidx: Int, st: I32Ptr, days: Int, fieldnum: Int
) -> Int:
    """Value this expression allows at or after st, or -1 when it allows none.

    Mirrors the get_next_value of apscheduler's AllExpression,
    RangeExpression, WeekdayPositionExpression and LastDayOfMonthExpression.
    """
    var base = eidx * EXPR_STRIDE
    var kind = Int(exprs[unsafe_offset=base])
    var a = Int(exprs[unsafe_offset=base + 1])
    var b = Int(exprs[unsafe_offset=base + 2])
    var step = Int(exprs[unsafe_offset=base + 3])
    var start = field_value(st, days, fieldnum)
    var minval = field_min(fieldnum)
    var maxval = field_max(st, fieldnum)
    var year = Int(st[unsafe_offset=ST_YEAR])
    var month = Int(st[unsafe_offset=ST_MONTH])
    var dom = Int(st[unsafe_offset=ST_DAY])

    if kind == 2:
        # WeekdayPositionExpression: the nth occurrence of a weekday in the
        # month. a is the weekday, b the position (0..4, or 5 for "last").
        var last_day = days_in_month(year, month)
        var first_hit = a - weekday(days_from_civil(year, month, 1)) + 1
        if first_hit <= 0:
            first_hit += 7
        var target = first_hit
        if b < 5:
            target = first_hit + b * 7
        else:
            target = first_hit + ((last_day - first_hit) // 7) * 7
        if target <= last_day and target >= dom:
            return target
        return -1

    if kind == 3:
        # LastDayOfMonthExpression
        return days_in_month(year, month)

    if kind == 0:
        # AllExpression, with an optional step
        var lo = minval
        var value = start if start > lo else lo
        if step != 0:
            value = value + (step - (value - lo)) % step
        if value <= maxval:
            return value
        return -1

    # RangeExpression; b below zero means "no explicit last value".
    var lo = minval if minval > a else a
    var hi = maxval
    if b >= 0 and b < hi:
        hi = b
    var value = lo if lo > start else start
    if step != 0:
        value = value + (step - (value - lo)) % step
    if value <= hi:
        return value
    return -1


def field_next_value(
    fields: I32Ptr, fidx: Int, exprs: I32Ptr, st: I32Ptr, days: Int
) -> Int:
    """Smallest value any of the field's expressions allows, or -1 for none."""
    var base = fidx * FIELD_STRIDE
    var estart = Int(fields[unsafe_offset=base + 1])
    var count = Int(fields[unsafe_offset=base + 2])
    var smallest = -1
    for k in range(count):
        var value = expr_next_value(exprs, estart + k, st, days, fidx)
        if value >= 0 and (smallest < 0 or value < smallest):
            smallest = value
    return smallest


def set_field_slot(st: I32Ptr, fidx: Int, value: Int):
    if fidx == 0:
        st[unsafe_offset=ST_YEAR] = Int32(value)
    elif fidx == 1:
        st[unsafe_offset=ST_MONTH] = Int32(value)
    elif fidx == 2:
        st[unsafe_offset=ST_DAY] = Int32(value)
    elif fidx == 5:
        st[unsafe_offset=ST_HOUR] = Int32(value)
    elif fidx == 6:
        st[unsafe_offset=ST_MINUTE] = Int32(value)
    else:
        st[unsafe_offset=ST_SECOND] = Int32(value)


def reset_higher_fields(fields: I32Ptr, fidx: Int, st: I32Ptr):
    """Set every real field above fidx to its minimum.

    The field order is year, month, day, week, day_of_week, hour, minute,
    second, so a higher index is a less significant field. That is the
    direction _set_field_value resets: setting the hour clears the minute and
    the second, and leaves the date alone.
    """
    for i in range(fidx + 1, NFIELDS):
        if field_is_real(fields, i):
            set_field_slot(st, i, field_min(i))


def set_field_value(fields: I32Ptr, fidx: Int, st: I32Ptr, new_value: Int):
    """Write one real field and reset the real fields above it."""
    set_field_slot(st, fidx, new_value)
    reset_higher_fields(fields, fidx, st)


def increment_field_value(fields: I32Ptr, st: I32Ptr, fieldnum_in: Int) -> Int:
    """Carry out of fieldnum_in; returns the new field number, or -1.

    This is the whole of CronTrigger._increment_field_value: walk down from
    the requested field until a real field has room, increment it, and reset
    every less significant field above it to its minimum. Returns -1 when the
    carry runs off the top, which the real code would surface as a
    ValueError.
    """
    var fieldnum = fieldnum_in
    # A pseudo-field target moves up to the real field above it, exactly as the
    # real loop does when it decrements past week and day_of_week.
    while fieldnum == 3 or fieldnum == 4:
        fieldnum -= 1
    while fieldnum >= 0:
        if not field_is_real(fields, fieldnum):
            fieldnum -= 1
            continue
        var days = st_days(st)
        var value = field_value(st, days, fieldnum)
        if value == field_max(st, fieldnum):
            fieldnum -= 1
            continue
        set_field_value(fields, fieldnum, st, value + 1)
        return fieldnum
    return -1


def cron_next(
    fields: I32Ptr, exprs: I32Ptr, st: I32Ptr, days: Int, secs: Int
) -> Bool:
    """Fill st with the next fire time at or after (days, secs)."""
    load_st(st, days, secs)
    var fieldnum = 0
    var guard = 0
    while fieldnum >= 0 and fieldnum < NFIELDS:
        guard += 1
        # An unsatisfiable expression such as "0 0 0 30 2 ?" never resolves.
        # The real code spins here too, so bound the walk instead.
        if guard > 1000000:
            return False
        var days_now = st_days(st)
        var curr = field_value(st, days_now, fieldnum)
        var nxt = field_next_value(fields, fieldnum, exprs, st, days_now)
        if nxt < 0:
            fieldnum = increment_field_value(fields, st, fieldnum - 1)
        elif nxt > curr:
            if field_is_real(fields, fieldnum):
                set_field_value(fields, fieldnum, st, nxt)
                fieldnum += 1
            else:
                fieldnum = increment_field_value(fields, st, fieldnum)
        else:
            fieldnum += 1
    return fieldnum >= 0


@export("aps_cron_next_range")
def aps_cron_next_range(
    trig_addr: Int,
    fields_addr: Int,
    exprs_addr: Int,
    n0: Int,
    n1: Int,
    scratch_addr: Int,
    dst_addr: Int
) abi("C"):
    """Next cron fire time for triggers [n0, n1).

    trig holds 7 Int32 per trigger: field start, expression start, start day,
    start second, start microsecond, end day (-1 for none) and end second.
    fields holds 3 Int32 per field: the real flag, the first expression index
    and the expression count. exprs holds 4 Int32 per expression: kind, a,
    b, step, with kind 0 all, 1 range, 2 weekday position and 3 last day of
    month. scratch is SCRATCH_STRIDE Int32 per trigger for the working
    datetime and the ISO-week scratch. dst receives two Int32 per trigger, the
    day and the second of day, or a day of -1 when the trigger is exhausted.
    """
    if n1 <= n0:
        return
    var trig = i32p(trig_addr)
    var fields = i32p(fields_addr)
    var exprs = i32p(exprs_addr)
    var scratch = i32p(scratch_addr)
    var dst = i32p(dst_addr)
    for t in range(n0, n1):
        var base = t * TRIG_STRIDE
        var fstart = Int(trig[unsafe_offset=base])
        var estart = Int(trig[unsafe_offset=base + 1])
        var days = Int(trig[unsafe_offset=base + 2])
        var secs = Int(trig[unsafe_offset=base + 3])
        var usec = Int(trig[unsafe_offset=base + 4])
        var end_days = Int(trig[unsafe_offset=base + 5])
        var end_secs = Int(trig[unsafe_offset=base + 6])
        var st = scratch + t * SCRATCH_STRIDE
        # apscheduler.util.datetime_ceil
        if usec > 0:
            secs += 1
            if secs == 86400:
                secs = 0
                days += 1
        var ok = cron_next(
            fields + fstart * FIELD_STRIDE,
            exprs + estart * EXPR_STRIDE,
            st,
            days,
            secs,
        )
        var got_days = days
        var got_secs = secs
        if ok:
            got_days = st_days(st)
            got_secs = secs_of_day(st)
            if end_days >= 0:
                if got_days > end_days or (
                    got_days == end_days and got_secs > end_secs
                ):
                    ok = False
        dst[unsafe_offset=t * 2] = Int32(got_days if ok else -1)
        dst[unsafe_offset=t * 2 + 1] = Int32(got_secs if ok else 0)


# --------------------------------------------------------------- interval


@export("aps_interval_next_range")
def aps_interval_next_range(
    cfg_addr: Int, n0: Int, n1: Int, dst_addr: Int
) abi("C"):
    """Next fire time for IntervalTrigger over triggers [n0, n1).

    cfg holds 5 Float64 per trigger: previous timestamp (NaN when absent),
    start timestamp, end timestamp (NaN when absent), interval length in
    seconds and the scheduler's now. dst receives one Float64 per trigger:
    the next timestamp, or -1.0 when the trigger is finished.
    """
    if n1 <= n0:
        return
    var cfg = f64p(cfg_addr)
    var dst = f64p(dst_addr)
    for t in range(n0, n1):
        var base = t * 5
        var previous = cfg[unsafe_offset=base]
        var start = cfg[unsafe_offset=base + 1]
        var end = cfg[unsafe_offset=base + 2]
        var length = cfg[unsafe_offset=base + 3]
        var now = cfg[unsafe_offset=base + 4]
        var next = start
        if previous == previous:
            next = previous + length
        elif start > now:
            next = start
        else:
            # start + length * ceil((now - start) / length)
            var q = (now - start) / length
            var steps = Int64(q)
            if Float64(steps) < q:
                steps += 1
            next = start + length * Float64(steps)
        if end == end and next > end:
            dst[unsafe_offset=t] = Float64(-1.0)
        else:
            dst[unsafe_offset=t] = next


# ------------------------------------------------------ calendar interval


@export("aps_cal_interval_next_range")
def aps_cal_interval_next_range(
    cal_addr: Int, n0: Int, n1: Int, scratch_addr: Int, dst_addr: Int
) abi("C"):
    """Next fire time for CalendarIntervalTrigger over triggers [n0, n1).

    cal holds 11 Int32 per trigger: years, months, weeks, days, hour, minute,
    second, start day, end day (-1 for none), previous day (-1 when absent)
    and previous second. scratch is SCRATCH_STRIDE Int32 per trigger. dst
    receives two Int32 per trigger, the day and the second of day, or a day of
    -1 when the trigger is finished.
    """
    if n1 <= n0:
        return
    var cal = i32p(cal_addr)
    var scratch = i32p(scratch_addr)
    var dst = i32p(dst_addr)
    for t in range(n0, n1):
        var base = t * CAL_STRIDE
        var years = Int(cal[unsafe_offset=base])
        var months = Int(cal[unsafe_offset=base + 1])
        var weeks = Int(cal[unsafe_offset=base + 2])
        var extra_days = Int(cal[unsafe_offset=base + 3])
        var hour = Int(cal[unsafe_offset=base + 4])
        var minute = Int(cal[unsafe_offset=base + 5])
        var second = Int(cal[unsafe_offset=base + 6])
        var start_day = Int(cal[unsafe_offset=base + 7])
        var end_day = Int(cal[unsafe_offset=base + 8])
        var prev_day = Int(cal[unsafe_offset=base + 9])
        var prev_second = Int(cal[unsafe_offset=base + 10])
        var st = scratch + t * SCRATCH_STRIDE

        var next_day = -1
        if prev_day < 0:
            next_day = start_day
        else:
            load_st(st, prev_day, prev_second)
            var year = Int(st[unsafe_offset=ST_YEAR])
            var month = Int(st[unsafe_offset=ST_MONTH])
            var dom = Int(st[unsafe_offset=ST_DAY])
            var tries = 0
            var found = False
            while not found and tries < 4800:
                tries += 1
                month += months
                year += years + (month - 1) // 12
                month = (month - 1) % 12 + 1
                if month < 1 or month > 12 or year < 1 or year > 9999:
                    break
                # A day of month that does not exist in the target month is
                # skipped, which is how 2020-01-31 becomes 2020-02-29.
                if dom > days_in_month(year, month):
                    continue
                next_day = (
                    days_from_civil(year, month, dom) + extra_days + weeks * 7
                )
                found = True
            if not found:
                next_day = -1

        if next_day >= 0 and end_day >= 0 and next_day > end_day:
            next_day = -1
        dst[unsafe_offset=t * 2] = Int32(next_day)
        dst[unsafe_offset=t * 2 + 1] = Int32(
            hour * 3600 + minute * 60 + second if next_day >= 0 else 0
        )
