"""Month dates and timezone-aware schedule previews, independent of Tk."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from rocky.recurrence import next_occurrence


def month_days(year,month):
    first=date(year,month,1)
    start=first-timedelta(days=first.weekday())
    result=[]
    for index in range(42):
        try:
            result.append(start+timedelta(days=index))
        except OverflowError:
            result.append(None)
    return result


def month_shift(year,month,offset):
    index=(year-1)*12+month-1+offset
    if not 0<=index<9999*12:
        raise ValueError('Choose a month between years 1 and 9999.')
    year,month=divmod(index,12)
    return year+1,month+1


def view_bounds(days,zone):
    zone=ZoneInfo(zone)
    try:
        start=datetime.combine(days[0],time.min,tzinfo=zone).astimezone(timezone.utc)
    except OverflowError:
        start=datetime.min.replace(tzinfo=timezone.utc)
    try:
        end=datetime.combine(next(day for day in reversed(days) if day)+timedelta(days=1),time.min,tzinfo=zone).astimezone(timezone.utc)
    except OverflowError:
        end=datetime.max.replace(tzinfo=timezone.utc)
    return start,end


@dataclass(frozen=True)
class CalendarEvent:
    key:str
    reminder_id:int
    due:datetime
    body:str
    repeat:str
    delivered:bool=False
    preview:bool=False


def schedule_events(pending,alerts,start,end):
    events=[]
    for original in pending:
        row=dict(original)
        due=datetime.fromtimestamp(row['due_at'],timezone.utc)
        recurring=row['repeat'] not in {'once','until_done'}
        if due<start and recurring:
            due=next_occurrence(row,start-timedelta(microseconds=1))
        for _ in range(64):
            if due is None or due>=end:
                break
            if due>=start:
                key=f'pending:{row["id"]}:{due.isoformat()}'
                events.append(CalendarEvent(key,row['id'],due,row['body'],row['repeat'],
                                            preview=due.timestamp()!=original['due_at']))
            if not recurring:
                break
            row['due_at']=due.timestamp()
            due=next_occurrence(row,due)
    for alert in alerts:
        due=datetime.fromtimestamp(alert['due_at'],timezone.utc)
        if start<=due<end:
            events.append(CalendarEvent(f'alert:{alert["id"]}',alert['reminder_id'],due,alert['body'],alert['repeat'],True))
    return sorted(events,key=lambda event:(event.due,event.reminder_id,event.key))


def group_days(events,zone):
    zone=ZoneInfo(zone)
    grouped={}
    # Keep an until-done task readable on the grid instead of drawing every
    # five-minute delivery. The Recent alerts tab retains each delivery.
    latest={}
    active={(event.reminder_id,event.due.astimezone(zone).date()) for event in events
            if event.repeat=='until_done' and not event.delivered}
    for event in events:
        day=event.due.astimezone(zone).date()
        if event.repeat=='until_done' and event.delivered:
            if (event.reminder_id,day) in active:
                continue
            latest[(event.reminder_id,day)]=event
        else:
            grouped.setdefault(day,[]).append(event)
    for (_,day),event in latest.items():
        grouped.setdefault(day,[]).append(event)
    for values in grouped.values():
        values.sort(key=lambda event:(event.due,event.key))
    return grouped


def load_calendar(store,start,end,zone='UTC'):
    with store.connect() as db:
        local_zone=ZoneInfo(zone)
        db.create_function('calendar_day',1,lambda timestamp:datetime.fromtimestamp(timestamp,local_zone).date().isoformat())
        db.execute('BEGIN')
        pending=db.execute('SELECT * FROM reminders WHERE notified_at IS NULL AND due_at<? ORDER BY due_at,id LIMIT 2001',
                           (end.timestamp(),)).fetchall()
        alerts=db.execute("WITH visible AS (SELECT * FROM reminder_alerts WHERE due_at>=? AND due_at<?) "
                          "SELECT * FROM visible WHERE repeat!='until_done' OR id IN "
                          "(SELECT MAX(id) FROM visible WHERE repeat='until_done' GROUP BY reminder_id,calendar_day(due_at)) "
                          'ORDER BY due_at,id LIMIT 2001',
                          (start.timestamp(),end.timestamp())).fetchall()
        recent=db.execute('SELECT * FROM reminder_alerts ORDER BY notified_at DESC,id DESC LIMIT 50').fetchall()
    return [dict(row) for row in pending],[dict(row) for row in alerts],[dict(row) for row in recent]
