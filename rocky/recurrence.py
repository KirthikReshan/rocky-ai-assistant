"""Daily and weekly wall-clock schedules in an event's time zone."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import calendar

REPEATS = {'once': 'Does not repeat', 'daily': 'Daily', 'weekly': 'Weekly',
           'weekdays': 'Weekdays (Mon–Fri)', 'monthly': 'Monthly', 'yearly': 'Yearly / birthday',
           'until_done': 'Every 5 minutes until marked done'}


def align_weekday(due, zone, repeat):
    local = due.astimezone(ZoneInfo(zone))
    while repeat == 'weekdays' and local.weekday() >= 5:
        local += timedelta(days=1)
    return local.astimezone(timezone.utc)


def local_clock(due, zone):
    return due.astimezone(ZoneInfo(zone)).time().replace(tzinfo=None).isoformat()


def next_occurrence(previous, now):
    zone = ZoneInfo(previous['timezone'])
    local = datetime.fromtimestamp(previous['due_at'], zone)
    clock = time.fromisoformat(previous['repeat_time'])
    repeat = previous['repeat']
    if repeat == 'until_done':
        try:
            return now + timedelta(minutes=5)
        except OverflowError:
            return None
    if repeat in {'monthly', 'yearly'}:
        anchor = datetime.fromisoformat(dict(previous).get('repeat_anchor') or local.date().isoformat()).date()
        month_index = max(local.year * 12 + local.month, now.astimezone(zone).year * 12 + now.astimezone(zone).month - 1)
        annual_year = max(local.year + 1, now.astimezone(zone).year)
        for _ in range(3):
            year, month = divmod(month_index, 12)
            month += 1
            if repeat == 'yearly':
                year = annual_year
                month = anchor.month
            if year > 9999:
                return None
            day = min(anchor.day, calendar.monthrange(year, month)[1])
            candidate = datetime(year, month, day, clock.hour, clock.minute, clock.second,
                                 clock.microsecond, tzinfo=zone).astimezone(timezone.utc)
            if candidate > now:
                return candidate
            month_index += 1
            annual_year += 1
        return None
    if repeat == 'weekdays':
        try:
            date = max(local.date() + timedelta(days=1), now.astimezone(zone).date())
            for _ in range(8):
                candidate = datetime.combine(date, clock, tzinfo=zone).astimezone(timezone.utc)
                if date.weekday() < 5 and candidate > now:
                    return candidate
                date += timedelta(days=1)
        except OverflowError:
            return None
        return None
    days = 1 if previous['repeat'] == 'daily' else 7
    steps = max(1, (now.astimezone(zone).date() - local.date()).days // days)
    while True:
        try:
            date = local.date() + timedelta(days=steps * days)
        except OverflowError:
            return None
        candidate = datetime.combine(date, clock, tzinfo=zone)
        # During a spring clock jump, normalize the skipped time forward.
        candidate = candidate.astimezone(timezone.utc)
        if candidate > now:
            return candidate
        steps += 1
