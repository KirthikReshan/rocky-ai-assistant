"""SQLite state, independent connections for UI and voice worker threads."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import os
import json
import sqlite3
from contextlib import contextmanager
from rocky.recurrence import REPEATS, local_clock, next_occurrence, align_weekday


def utc_now():
    return datetime.now(timezone.utc)


def _pending_match(reminder_id, created_at, expected):
    where = 'id=? AND created_at=? AND notified_at IS NULL'
    values = [reminder_id, created_at]
    if expected is not None:
        for column in ('body', 'due_at', 'timezone', 'repeat', 'repeat_time', 'repeat_anchor'):
            where += f' AND {column} IS ?'
            values.append(expected[column])
    return where, values


def schedule_anchor(previous, due, zone, repeat):
    date = due.astimezone(ZoneInfo(zone)).date()
    if (repeat == previous['repeat'] and date == datetime.fromtimestamp(previous['due_at'], ZoneInfo(previous['timezone'])).date()
            and previous['repeat_anchor']):
        return previous['repeat_anchor']
    return date.isoformat()


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Rocky/rocky.db'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY, body TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY, body TEXT NOT NULL, due_at REAL NOT NULL,
                    timezone TEXT NOT NULL, original TEXT NOT NULL, created_at REAL NOT NULL,
                    notified_at REAL);
                CREATE INDEX IF NOT EXISTS reminder_due ON reminders(notified_at, due_at);
                CREATE TABLE IF NOT EXISTS voice_tests (
                    id INTEGER PRIMARY KEY, created_at REAL NOT NULL, expected TEXT NOT NULL,
                    heard TEXT NOT NULL, word_errors INTEGER NOT NULL, reference_words INTEGER NOT NULL,
                    exact INTEGER NOT NULL, latency REAL NOT NULL, environment TEXT NOT NULL,
                    device TEXT NOT NULL, overflow INTEGER NOT NULL,
                    engine TEXT NOT NULL DEFAULT 'CPU (legacy)');
                CREATE TABLE IF NOT EXISTS settings (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reminder_alerts (
                    id INTEGER PRIMARY KEY, reminder_id INTEGER NOT NULL, body TEXT NOT NULL,
                    due_at REAL NOT NULL, timezone TEXT NOT NULL, notified_at REAL NOT NULL,
                    repeat TEXT NOT NULL DEFAULT 'once',
                    UNIQUE(reminder_id,due_at));
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(voice_tests)')}
            if 'engine' not in columns:
                db.execute("ALTER TABLE voice_tests ADD COLUMN engine TEXT NOT NULL DEFAULT 'CPU (legacy)'")
            for name, declaration in {
                'test_kind': "TEXT NOT NULL DEFAULT 'command'",
                'action_correct': 'INTEGER', 'wake_detected': 'INTEGER', 'wake_baseline': 'INTEGER',
                'wake_config': "TEXT NOT NULL DEFAULT ''",
                'peak': 'REAL', 'rms': 'REAL', 'clipped': 'REAL',
            }.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE voice_tests ADD COLUMN {name} {declaration}')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(reminders)')}
            if 'repeat' not in columns:
                db.execute("ALTER TABLE reminders ADD COLUMN repeat TEXT NOT NULL DEFAULT 'once'")
            if 'repeat_time' not in columns:
                db.execute("ALTER TABLE reminders ADD COLUMN repeat_time TEXT")
            if 'snoozed_from' not in columns:
                db.execute("ALTER TABLE reminders ADD COLUMN snoozed_from INTEGER")
            if 'repeat_anchor' not in columns:
                db.execute('ALTER TABLE reminders ADD COLUMN repeat_anchor TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS reminder_snooze_pending '
                       'ON reminders(snoozed_from) WHERE notified_at IS NULL')
            db.execute('INSERT OR IGNORE INTO reminder_alerts(reminder_id,body,due_at,timezone,notified_at,repeat) '
                       'SELECT id,body,due_at,timezone,notified_at,repeat FROM reminders WHERE notified_at >= 0')
            db.execute('CREATE INDEX IF NOT EXISTS reminder_alert_date ON reminder_alerts(due_at)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_note(self, text):
        with self.connect() as db:
            return db.execute('INSERT INTO notes(body,created_at) VALUES (?,?)',
                              (text, utc_now().timestamp())).lastrowid

    def setting(self, name, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE name=?', (name,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row['value'])
        except (ValueError, TypeError):
            return default

    def set_setting(self, name, value):
        self.set_settings({name: value})

    def set_settings(self, values):
        with self.connect() as db:
            db.executemany('INSERT INTO settings(name,value) VALUES (?,?) '
                           'ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                           [(name, json.dumps(value, allow_nan=False)) for name, value in values.items()])

    def update_setting(self, name, change):
        """Atomically merge one setting across independent Rocky instances."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM settings WHERE name=?',(name,)).fetchone()
            try:
                value = json.loads(row['value']) if row else None
            except (ValueError,TypeError):
                value = None
            updated = change(value)
            db.execute('INSERT INTO settings(name,value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                       (name,json.dumps(updated,allow_nan=False)))

    def add_voice_test(self, result):
        with self.connect() as db:
            db.execute('INSERT INTO voice_tests(created_at,expected,heard,word_errors,reference_words,'
                       'exact,latency,environment,device,overflow,engine,test_kind,action_correct,wake_detected,wake_baseline,wake_config,peak,rms,clipped) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (utc_now().timestamp(), result['expected'], result['heard'], result['word_errors'],
                        result['reference_words'], result['exact'], result['latency'],
                        result['environment'], result['device'], result['overflow'], result.get('engine', 'CPU (legacy)'),
                        result.get('test_kind', 'command'), result.get('action_correct'),
                        result.get('wake_detected'), result.get('wake_baseline'), result.get('wake_config', ''),
                        result.get('peak'),result.get('rms'),result.get('clipped')))

    def voice_tests(self, limit=100):
        with self.connect() as db:
            return db.execute('SELECT * FROM voice_tests ORDER BY id DESC LIMIT ?', (limit,)).fetchall()

    def notes(self, limit=20):
        with self.connect() as db:
            return db.execute('SELECT * FROM notes ORDER BY id DESC LIMIT ?', (limit,)).fetchall()

    def add_reminder(self, body, due, zone, original, repeat='once'):
        if due.tzinfo is None:
            raise ValueError('Reminder time must include a timezone')
        if repeat not in REPEATS:
            raise ValueError('Choose a repeat option from Calendar.')
        due = align_weekday(due, zone, repeat)
        clock = local_clock(due, zone)
        with self.connect() as db:
            return db.execute('INSERT INTO reminders(body,due_at,timezone,original,created_at,repeat,repeat_time,repeat_anchor) '
                              'VALUES (?,?,?,?,?,?,?,?)',
                              (body, due.timestamp(), zone, original, utc_now().timestamp(), repeat, clock,
                               due.astimezone(ZoneInfo(zone)).date().isoformat())).lastrowid

    def reminders(self, limit=20):
        with self.connect() as db:
            return db.execute('SELECT * FROM reminders WHERE notified_at IS NULL ORDER BY due_at LIMIT ?',
                              (limit,)).fetchall()

    def recent_alerts(self, limit=50):
        with self.connect() as db:
            return db.execute('SELECT * FROM reminder_alerts '
                              'ORDER BY notified_at DESC, id DESC LIMIT ?', (limit,)).fetchall()

    def alert_for(self, reminder_id, due_at):
        with self.connect() as db:
            return db.execute('SELECT * FROM reminder_alerts WHERE reminder_id=? AND due_at=?',
                              (reminder_id, due_at)).fetchone()

    def snooze_alert(self, alert_id, minutes, now=None):
        if type(minutes) is not int or minutes not in (5, 15, 30):
            raise ValueError('Choose 5, 15 or 30 minutes.')
        now = now or utc_now()
        due = now + timedelta(minutes=minutes)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            alert = db.execute('SELECT * FROM reminder_alerts WHERE id=?', (alert_id,)).fetchone()
            if alert is None:
                raise ValueError('That alert is unavailable. Refresh Recent alerts.')
            if alert['repeat'] == 'until_done':
                original = db.execute('SELECT * FROM reminders WHERE id=?',(alert['reminder_id'],)).fetchone()
                if original is None or original['notified_at'] is not None or original['repeat'] != 'until_done':
                    raise ValueError('That repeating reminder is completed, cancelled or changed. Check Calendar.')
                if original['body'] != alert['body']:
                    raise ValueError('That event changed. Choose the current event in Calendar.')
                db.execute('UPDATE reminders SET due_at=?, repeat_time=? WHERE id=?',
                           (due.timestamp(),local_clock(due,original['timezone']),original['id']))
                db.execute("UPDATE reminders SET notified_at=-1 WHERE repeat='once' AND notified_at IS NULL AND snoozed_from IN "
                           '(SELECT id FROM reminder_alerts WHERE reminder_id=?)',(original['id'],))
                return original['id']
            pending = db.execute('SELECT * FROM reminders WHERE snoozed_from=? AND notified_at IS NULL',
                                 (alert_id,)).fetchone()
            clock = local_clock(due, alert['timezone'])
            if pending is not None:
                if pending['repeat'] != 'once':
                    raise ValueError('This snoozed event was changed to repeat. Edit it in Calendar.')
                db.execute('UPDATE reminders SET due_at=?, repeat_time=? WHERE id=?',
                           (due.timestamp(), clock, pending['id']))
                return pending['id']
            return db.execute('INSERT INTO reminders(body,due_at,timezone,original,created_at,repeat,'
                              'repeat_time,snoozed_from) VALUES (?,?,?,?,?,?,?,?)',
                              (alert['body'], due.timestamp(), alert['timezone'], 'Snoozed alert',
                               now.timestamp(), 'once', clock, alert_id)).lastrowid

    def update_reminder(self, reminder_id, body, due, zone, *, created_at, repeat=None, expected=None):
        if due.tzinfo is None:
            raise ValueError('Reminder time must include a timezone')
        if repeat is not None and repeat not in REPEATS:
            raise ValueError('Choose a repeat option from Calendar.')
        where, identity = _pending_match(reminder_id, created_at, expected)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM reminders WHERE '+where,identity).fetchone()
            if previous is None:
                return False
            repeat = repeat if repeat is not None else previous['repeat']
            due = align_weekday(due,zone,repeat)
            updated = db.execute('UPDATE reminders SET body=?, due_at=?, timezone=?, original=?, '
                                 'repeat=COALESCE(?,repeat), repeat_time=?, repeat_anchor=? '
                                 'WHERE ' + where,
                                 [body, due.timestamp(), zone, body, repeat, local_clock(due, zone),
                                  schedule_anchor(previous,due,zone,repeat)] + identity)
            return updated.rowcount == 1

    def cancel_reminder(self, reminder_id, *, created_at, expected=None):
        where, identity = _pending_match(reminder_id, created_at, expected)
        with self.connect() as db:
            # Keep cancelled rows so SQLite cannot reuse their IDs.
            cancelled = db.execute('UPDATE reminders SET notified_at=-1 WHERE ' + where, identity)
            return cancelled.rowcount == 1

    def latest_reminder(self):
        with self.connect() as db:
            return db.execute('SELECT * FROM reminders WHERE notified_at IS NULL OR notified_at >= 0 '
                              'ORDER BY id DESC LIMIT 1').fetchone()

    def reminder(self, reminder_id):
        with self.connect() as db:
            return db.execute('SELECT * FROM reminders WHERE id=?', (reminder_id,)).fetchone()

    def reschedule_reminder(self, previous, due):
        due = align_weekday(due, previous['timezone'], previous['repeat'])
        where, identity = _pending_match(previous['id'], previous['created_at'], previous)
        with self.connect() as db:
            changed = db.execute('UPDATE reminders SET due_at=?, repeat_time=?, repeat_anchor=? WHERE ' + where,
                                 [due.timestamp(), local_clock(due, previous['timezone']),
                                  schedule_anchor(previous,due,previous['timezone'],previous['repeat'])] + identity)
            return changed.rowcount == 1

    def complete_reminder(self, reminder_id, expected=None):
        where,identity=_pending_match(reminder_id,expected['created_at'],expected) if expected is not None else ('id=? AND notified_at IS NULL',[reminder_id])
        with self.connect() as db:
            changed = db.execute("UPDATE reminders SET notified_at=-2 WHERE repeat='until_done' AND "+where,identity)
            if changed.rowcount:
                # Clean up one-time snoozes created by the previous release.
                db.execute("UPDATE reminders SET notified_at=-2 WHERE repeat='once' AND notified_at IS NULL AND snoozed_from IN "
                           '(SELECT id FROM reminder_alerts WHERE reminder_id=?)',(reminder_id,))
            return changed.rowcount == 1

    def due(self, now=None, limit=20):
        with self.connect() as db:
            return db.execute('SELECT * FROM reminders WHERE notified_at IS NULL AND due_at<=? ORDER BY due_at LIMIT ?',
                              ((now or utc_now()).timestamp(), limit)).fetchall()

    def acknowledge(self, reminder_id, *, expected_due=None, expected_body=None, now=None):
        """Claim notification only after the desktop alert has been created."""
        now = now or utc_now()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM reminders WHERE id=? AND notified_at IS NULL', (reminder_id,)).fetchone()
            if row is None or (expected_due is not None and row['due_at'] != expected_due):
                return False
            if expected_body is not None and row['body'] != expected_body:
                return False
            if row['repeat'] != 'once' and row['due_at'] > now.timestamp():
                return False
            next_due = next_occurrence(row, now) if row['repeat'] != 'once' else None
            db.execute('INSERT OR IGNORE INTO reminder_alerts(reminder_id,body,due_at,timezone,notified_at,repeat) '
                       'VALUES (?,?,?,?,?,?)',
                       (row['id'], row['body'], row['due_at'], row['timezone'], now.timestamp(), row['repeat']))
            if next_due:
                db.execute('UPDATE reminders SET due_at=? WHERE id=?', (next_due.timestamp(), reminder_id))
            else:
                db.execute('UPDATE reminders SET notified_at=? WHERE id=?', (now.timestamp(), reminder_id))
            return True
