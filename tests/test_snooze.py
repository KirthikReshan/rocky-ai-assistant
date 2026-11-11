from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from rocky.notifications import ReminderDelivery
from rocky.reminders import ReminderCommands
from rocky.storage import Store


class SnoozeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'rocky.db'
        self.store = Store(self.path)
        self.now = datetime(2026, 11, 4, 17, tzinfo=ZoneInfo('Asia/Kolkata'))
        self.parent = self.store.add_reminder('Take a break', self.now, 'Asia/Kolkata', 'test')
        self.store.acknowledge(self.parent, now=self.now)
        self.alert = self.store.recent_alerts()[0]

    def test_snooze_survives_restart_and_delivers_once(self):
        snoozed = self.store.snooze_alert(self.alert['id'], 5, now=self.now)
        store = Store(self.path)
        self.assertEqual(store.reminder(snoozed)['due_at'], (self.now + timedelta(minutes=5)).timestamp())
        show = Mock()
        delivery = ReminderDelivery(store, queue.Queue())
        delivery.tick(show, now=self.now)
        show.assert_not_called()
        delivery.tick(show, now=self.now + timedelta(minutes=5))
        delivery.tick(show, now=self.now + timedelta(minutes=5))
        show.assert_called_once()
        self.assertEqual(len(store.recent_alerts()), 2)

    def test_repeated_click_updates_one_pending_snooze(self):
        first = self.store.snooze_alert(self.alert['id'], 5, now=self.now)
        second = self.store.snooze_alert(self.alert['id'], 15, now=self.now)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.reminders()), 1)
        self.assertEqual(self.store.reminder(first)['due_at'], (self.now + timedelta(minutes=15)).timestamp())

    def test_concurrent_snoozes_do_not_create_duplicates(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: Store(self.path).snooze_alert(self.alert['id'], 5, now=self.now), range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(len(self.store.reminders()), 1)

    def test_snooze_does_not_shift_daily_series(self):
        parent = self.store.add_reminder('Daily', self.now, 'Asia/Kolkata', 'test', repeat='daily')
        self.store.acknowledge(parent, now=self.now)
        original_due = self.store.reminder(parent)['due_at']
        alert = self.store.alert_for(parent, self.now.timestamp())
        child = self.store.snooze_alert(alert['id'], 15, now=self.now)
        self.assertEqual(self.store.reminder(parent)['due_at'], original_due)
        self.assertEqual(self.store.reminder(child)['repeat'], 'once')

    def test_invalid_or_missing_alert_does_not_create_reminder(self):
        for duration in (0, -5, 10, True, '5'):
            with self.assertRaises(ValueError):
                self.store.snooze_alert(self.alert['id'], duration, now=self.now)
        with self.assertRaises(ValueError):
            self.store.snooze_alert(99999, 5, now=self.now)
        self.assertEqual(self.store.reminders(), [])

    def test_voice_snoozes_recent_alert_and_asks_about_old_alerts(self):
        commands = ReminderCommands(self.store)
        with patch('rocky.reminders.utc_now', return_value=self.now):
            self.assertIn('Snoozed', commands.handle('Snooze that for fifteen minutes'))
        self.assertEqual(len(self.store.reminders()), 1)
        with patch('rocky.reminders.utc_now', return_value=self.now + timedelta(hours=1)):
            self.assertIn('choose', commands.handle('snooze for five minutes'))
        self.assertEqual(len(self.store.reminders()), 1)
