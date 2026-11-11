from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import queue
import tempfile
import unittest
from unittest.mock import Mock

from rocky.storage import Store
from rocky.reminders import ReminderCommands, parse_due
from rocky.notifications import ReminderDelivery
from rocky.assistant import Assistant


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'rocky.db'
        self.store = Store(self.path)
        self.now = datetime(2026, 11, 2, 16, tzinfo=ZoneInfo('Asia/Kolkata'))

    def test_relative_days_and_seconds(self):
        self.assertEqual(parse_due('in two days', now=self.now), self.now + timedelta(days=2))
        self.assertEqual(parse_due('in ten seconds', now=self.now), self.now + timedelta(seconds=10))

    def test_ist_in_india_is_india_time_not_israel_time(self):
        for zone in ('Asia/Kolkata', 'Asia/Calcutta'):
            due = parse_due('at 5 PM IST', zone=zone, now=self.now)
            self.assertEqual(due.astimezone(self.now.tzinfo).hour, 17)
            self.assertEqual(due.astimezone(timezone.utc).hour, 11)
            self.assertEqual(due.astimezone(timezone.utc).minute, 30)
        tomorrow = parse_due('tomorrow at 5 PM IST', now=self.now).astimezone(self.now.tzinfo)
        self.assertEqual((tomorrow.day, tomorrow.hour), (3, 17))

    def test_ambiguous_ist_outside_india_requests_clarification(self):
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            parse_due('at 5 PM IST', zone='Europe/Dublin', now=self.now)

    def test_noon_and_midnight(self):
        noon = parse_due('at noon', now=self.now).astimezone(self.now.tzinfo)
        midnight = parse_due('at midnight', now=self.now).astimezone(self.now.tzinfo)
        self.assertEqual((noon.day, noon.hour), (3, 12))
        self.assertEqual((midnight.day, midnight.hour), (3, 0))

    def test_clock_today_or_next_day(self):
        self.assertEqual(parse_due('at 5 PM', now=self.now).astimezone(self.now.tzinfo).hour, 17)
        self.assertEqual(parse_due('at 3 PM', now=self.now).astimezone(self.now.tzinfo).day, 3)
        tomorrow = parse_due('tomorrow at 5 PM', now=self.now).astimezone(self.now.tzinfo)
        self.assertEqual((tomorrow.day, tomorrow.hour), (3, 17))

    def test_unclear_and_past_times_do_not_schedule(self):
        for phrase in ('at 5', 'tomorrow', '02/03/2027 at 5 PM', 'yesterday at 5 PM', 'every day at 5 PM', 'at 99 PM', 'in 0 seconds'):
            with self.assertRaises(ValueError, msg=phrase):
                parse_due(phrase, now=self.now)

    def test_notes_survive_reopen_and_are_parameterized(self):
        self.store.add_note("Remember '; DROP TABLE notes; --")
        reopened = Store(self.path)
        self.assertIn('DROP TABLE', reopened.notes()[0]['body'])

    def test_due_reminder_survives_reopen(self):
        self.store.add_reminder('call Mom', self.now, 'Asia/Kolkata', 'original')
        reopened = Store(self.path)
        due = reopened.due(self.now + timedelta(days=2))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]['body'], 'call Mom')

    def test_notification_delivers_once_across_restart(self):
        self.store.add_reminder('test alarm', self.now, 'Asia/Kolkata', 'original')
        events, speech = queue.Queue(), queue.Queue()
        show = Mock()
        ReminderDelivery(self.store, events, speech).tick(show, now=self.now)
        ReminderDelivery(Store(self.path), events, speech).tick(show, now=self.now)
        show.assert_called_once()
        self.assertEqual(events.get_nowait(), ('Reminder', 'test alarm'))
        self.assertTrue(events.empty())
        self.assertEqual(speech.get_nowait(), 'Reminder: test alarm')
        self.assertTrue(speech.empty())

    def test_failed_display_is_not_marked_delivered(self):
        self.store.add_reminder('test alarm', self.now, 'Asia/Kolkata', 'original')
        delivery = ReminderDelivery(self.store, queue.Queue(), queue.Queue())
        with self.assertRaises(RuntimeError):
            delivery.tick(Mock(side_effect=RuntimeError('no display')), now=self.now)
        self.assertEqual(len(Store(self.path).due(self.now)), 1)

    def test_future_alarm_does_not_fire_early(self):
        self.store.add_reminder('later', self.now + timedelta(hours=1), 'Asia/Kolkata', 'original')
        self.assertEqual(self.store.due(self.now), [])

    def test_commands_save_and_list(self):
        commands = ReminderCommands(self.store)
        self.assertIn('Saved note', commands.handle('Take a note that my project is Rocky.'))
        self.assertIn('Rocky', commands.handle('show my notes'))
        self.assertIn('saved', commands.handle('Remind me in two days to call Mom.'))
        self.assertIn('saved', commands.handle('Remind me to drink water in ten minutes.'))
        self.assertEqual(len(self.store.reminders()), 2)
        self.assertIn('call Mom', commands.handle('show my reminders'))

    def test_clarification_never_reaches_brain_or_saves(self):
        brain = Mock()
        assistant = Assistant(brain, reminders=ReminderCommands(self.store))
        self.assertIn('AM or PM', assistant.respond('remind me at 5 to call Mom'))
        brain.chat.assert_not_called()
        self.assertEqual(self.store.reminders(), [])

    def test_time_suffix_preserves_phrasal_verbs_and_date(self):
        commands = ReminderCommands(self.store)
        examples = [('log in tomorrow at 5 PM', 'log in'),
                    ('sign in at 5 PM', 'sign in'),
                    ('turn on the light in ten minutes', 'turn on the light'),
                    ('meet at the cafe tomorrow at 5 PM', 'meet at the cafe'),
                    ('work on my project in two days', 'work on my project')]
        from unittest.mock import patch
        with patch('rocky.reminders.utc_now', return_value=self.now):
            for phrase, body in examples:
                self.assertIn('saved', commands.handle('remind me to ' + phrase))
                latest = max(self.store.reminders(), key=lambda row: row['id'])
                self.assertEqual(latest['body'], body)
            first = min(self.store.reminders(), key=lambda row: row['id'])
            due = datetime.fromtimestamp(first['due_at'], self.now.tzinfo)
            self.assertEqual((due.day, due.hour), (3, 17))

    def test_oversized_duration_is_rejected_without_saving(self):
        for phrase in ('in 1000000000 days', 'in ' + '9' * 400 + ' weeks'):
            with self.assertRaises(ValueError):
                parse_due(phrase, now=self.now)
            reply = ReminderCommands(self.store).handle(f'remind me {phrase} to test')
            self.assertIn('nearer time', reply)
        self.assertEqual(self.store.reminders(), [])
