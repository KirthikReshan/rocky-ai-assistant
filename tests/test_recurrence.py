from datetime import datetime, timedelta
from pathlib import Path
import queue
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from rocky.notifications import ReminderDelivery
from rocky.reminders import ReminderCommands
from rocky.storage import Store


class RecurrenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'rocky.db'
        self.store = Store(self.path)
        self.now = datetime(2026, 11, 4, 17, tzinfo=ZoneInfo('Asia/Kolkata'))

    def test_daily_delivery_reschedules_and_records_history_atomically(self):
        reminder = self.store.add_reminder('Break', self.now, 'Asia/Kolkata', 'test', repeat='daily')
        show = Mock()
        delivery = ReminderDelivery(self.store, queue.Queue())
        delivery.tick(show, now=self.now)
        delivery.tick(show, now=self.now)
        show.assert_called_once()
        self.assertEqual(len(self.store.recent_alerts()), 1)
        row = Store(self.path).reminder(reminder)
        self.assertEqual(row['due_at'], (self.now + timedelta(days=1)).timestamp())
        self.assertIsNone(row['notified_at'])
        self.assertFalse(self.store.acknowledge(reminder, expected_due=self.now.timestamp(), now=self.now))
        self.assertFalse(self.store.acknowledge(reminder, now=self.now))

    def test_multiple_alerts_survive_restart_and_cancellation(self):
        reminder = self.store.add_reminder('Break', self.now, 'Asia/Kolkata', 'test', repeat='daily')
        delivery = ReminderDelivery(self.store, queue.Queue())
        delivery.tick(Mock(), now=self.now)
        delivery.tick(Mock(), now=self.now + timedelta(days=1))
        store = Store(self.path)
        self.assertEqual(len(store.recent_alerts()), 2)
        row = store.reminder(reminder)
        self.assertTrue(store.cancel_reminder(reminder, created_at=row['created_at']))
        self.assertEqual(store.reminders(), [])
        self.assertEqual(len(Store(self.path).recent_alerts()), 2)

    def test_weekly_catchup_shows_once_and_skips_missed_weeks(self):
        self.store.add_reminder('Weekly', self.now, 'Asia/Kolkata', 'test', repeat='weekly')
        delivery = ReminderDelivery(self.store, queue.Queue())
        show = Mock()
        late = self.now + timedelta(days=30)
        delivery.tick(show, now=late)
        delivery.tick(show, now=late)
        show.assert_called_once()
        self.assertEqual(self.store.latest_reminder()['due_at'], (self.now + timedelta(days=35)).timestamp())

    def test_failed_popup_does_not_advance_schedule(self):
        self.store.add_reminder('Break', self.now, 'Asia/Kolkata', 'test', repeat='daily')
        delivery = ReminderDelivery(self.store, queue.Queue())
        with self.assertRaises(RuntimeError):
            delivery.tick(Mock(side_effect=RuntimeError('display failed')), now=self.now)
        self.assertEqual(self.store.latest_reminder()['due_at'], self.now.timestamp())
        self.assertEqual(self.store.recent_alerts(), [])

    def test_voice_phrases_and_incomplete_daily_draft(self):
        with patch('rocky.reminders.utc_now', return_value=self.now):
            for phrase, expected in [
                ('remind me every day at 6 PM to take a break', 'daily'),
                ('remind me to take a break every week at 6 PM', 'weekly')]:
                commands = ReminderCommands(self.store)
                answer = commands.handle(phrase)
                self.assertIn('Repeats ' + expected, answer)
                self.assertEqual(self.store.latest_reminder()['repeat'], expected)
            commands = ReminderCommands(self.store)
            self.assertIn('What should', commands.handle('remind me every day at 6 PM'))
            commands.handle('stretch')
            self.assertEqual(self.store.latest_reminder()['repeat'], 'daily')

    def test_spring_clock_change_does_not_shift_future_daily_time(self):
        zone = ZoneInfo('America/New_York')
        due = datetime(2027, 3, 13, 2, 30, tzinfo=zone)
        reminder = self.store.add_reminder('Daily', due, str(zone), 'test', repeat='daily')
        self.store.acknowledge(reminder, expected_due=due.timestamp(), now=due)
        row = self.store.reminder(reminder)
        gap = datetime.fromtimestamp(row['due_at'], zone)
        self.assertEqual((gap.day, gap.hour, gap.minute), (14, 3, 30))
        self.store.acknowledge(reminder, expected_due=row['due_at'], now=gap)
        following = datetime.fromtimestamp(self.store.reminder(reminder)['due_at'], zone)
        self.assertEqual((following.day, following.hour, following.minute), (15, 2, 30))

    def test_old_delivered_rows_migrate_to_history_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.db'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE reminders (id INTEGER PRIMARY KEY, body TEXT, due_at REAL, '
                       'timezone TEXT, original TEXT, created_at REAL, notified_at REAL)')
            db.execute("INSERT INTO reminders VALUES(1,'Old',1,'Asia/Kolkata','Old',0,2)")
            db.commit()
            db.close()
            self.assertEqual(len(Store(path).recent_alerts()), 1)
            self.assertEqual(len(Store(path).recent_alerts()), 1)
            self.assertEqual(Store(path).reminder(1)['repeat'], 'once')

    def test_autumn_clock_change_uses_first_occurrence_only(self):
        zone = ZoneInfo('America/New_York')
        due = datetime(2027, 11, 6, 1, 30, tzinfo=zone)
        reminder = self.store.add_reminder('Daily', due, str(zone), 'test', repeat='daily')
        self.store.acknowledge(reminder, now=due)
        first = datetime.fromtimestamp(self.store.reminder(reminder)['due_at'], zone)
        self.assertEqual((first.day, first.hour, first.fold), (7, 1, 0))
        self.store.acknowledge(reminder, now=first)
        second_fold = first.replace(fold=1)
        self.assertEqual(self.store.due(second_fold), [])
