from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from rocky.reminders import ReminderCommands, parse_due
from rocky.storage import Store


class CalendarReliabilityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'rocky.db')
        self.zone = 'Asia/Kolkata'
        self.now = datetime(2026, 11, 4, 12, tzinfo=ZoneInfo(self.zone))

    def create_event(self, repeat='once'):
        reminder = self.store.add_reminder('Take a break', self.now, self.zone, 'test', repeat=repeat)
        return self.store.reminder(reminder)

    def test_stale_editor_cannot_overwrite_a_newer_edit(self):
        original = self.create_event()
        changed_due = self.now + timedelta(hours=1)
        self.assertTrue(self.store.update_reminder(
            original['id'], 'New title', changed_due, self.zone,
            created_at=original['created_at'], expected=original))
        self.assertFalse(self.store.update_reminder(
            original['id'], 'Stale title', self.now + timedelta(hours=2), self.zone,
            created_at=original['created_at'], expected=original))
        current = self.store.reminder(original['id'])
        self.assertEqual(current['body'], 'New title')
        self.assertEqual(current['due_at'], changed_due.timestamp())

    def test_stale_confirmation_cannot_cancel_a_changed_event(self):
        original = self.create_event()
        self.store.update_reminder(original['id'], 'Changed event', self.now, self.zone,
                                   created_at=original['created_at'])
        self.assertFalse(self.store.cancel_reminder(
            original['id'], created_at=original['created_at'], expected=original))
        current = self.store.reminder(original['id'])
        self.assertTrue(self.store.cancel_reminder(
            current['id'], created_at=current['created_at'], expected=current))
        self.assertEqual(self.store.reminders(), [])

    def test_delivery_during_edit_does_not_rewind_a_recurring_event(self):
        original = self.create_event(repeat='daily')
        self.store.acknowledge(original['id'], now=self.now)
        next_due = self.store.reminder(original['id'])['due_at']
        self.assertFalse(self.store.update_reminder(
            original['id'], original['body'], self.now + timedelta(minutes=5), self.zone,
            created_at=original['created_at'], repeat='daily', expected=original))
        self.assertEqual(self.store.reminder(original['id'])['due_at'], next_due)

    def test_stale_voice_correction_cannot_overwrite_a_calendar_timezone_change(self):
        original = self.create_event(repeat='daily')
        due = datetime.fromtimestamp(original['due_at'], timezone.utc)
        self.assertTrue(self.store.update_reminder(
            original['id'], original['body'], due, 'UTC',
            created_at=original['created_at'], expected=original))
        current = self.store.reminder(original['id'])
        self.assertEqual(current['repeat_time'], '06:30:00')
        self.assertFalse(self.store.reschedule_reminder(original, due + timedelta(minutes=5)))
        after = self.store.reminder(original['id'])
        self.assertEqual(after['timezone'], 'UTC')
        self.assertEqual(after['due_at'], due.timestamp())
        self.assertEqual(after['repeat_time'], current['repeat_time'])

    def test_spoken_durations_accept_common_and_compound_numbers(self):
        for words, minutes in [('fifteen', 15), ('thirty', 30), ('forty five', 45),
                               ('twenty-five', 25), ('ninety nine', 99)]:
            with self.subTest(words=words):
                self.assertEqual(parse_due('in ' + words + ' minutes', now=self.now),
                                 self.now + timedelta(minutes=minutes))

    def test_spoken_duration_works_in_body_first_drafts_and_corrections(self):
        with patch('rocky.reminders.utc_now', return_value=self.now):
            commands = ReminderCommands(self.store)
            self.assertIn('saved', commands.handle('remind me to stretch in twenty five minutes'))
            self.assertEqual(self.store.latest_reminder()['body'], 'stretch')
            self.assertIn('updated', commands.handle('make that forty-five minutes'))
            self.assertEqual(self.store.latest_reminder()['due_at'],
                             (self.now + timedelta(minutes=45)).timestamp())
            commands = ReminderCommands(self.store)
            self.assertIn('What should', commands.handle('remain me in fifteen minutes'))
            self.assertIn('saved', commands.handle('drink water'))
            self.assertEqual(self.store.latest_reminder()['due_at'],
                             (self.now + timedelta(minutes=15)).timestamp())

    def test_snooze_correction_changes_the_snoozed_alert_not_an_older_reminder(self):
        original = self.create_event()
        self.store.acknowledge(original['id'], now=self.now)
        commands = ReminderCommands(self.store)
        with patch('rocky.reminders.utc_now', return_value=self.now):
            commands.handle('remind me in twenty minutes to call Mom')
            other = self.store.latest_reminder()
            self.assertIn('Snoozed', commands.handle('snooze for five minutes'))
            snoozed = self.store.latest_reminder()
            self.assertIn('updated', commands.handle('make that fifteen minutes'))
        self.assertEqual(self.store.reminder(snoozed['id'])['due_at'],
                         (self.now + timedelta(minutes=15)).timestamp())
        self.assertEqual(self.store.reminder(other['id'])['due_at'], other['due_at'])

    def test_snooze_correction_does_not_complete_an_older_unfinished_draft(self):
        original = self.create_event()
        self.store.acknowledge(original['id'], now=self.now)
        commands = ReminderCommands(self.store)
        with patch('rocky.reminders.utc_now', return_value=self.now):
            self.assertIn('What should', commands.handle('remind me in twenty minutes'))
            self.assertIn('Snoozed', commands.handle('snooze for five minutes'))
            self.assertIn('updated', commands.handle('make that fifteen minutes'))
        rows = self.store.reminders()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['body'], original['body'])
        self.assertEqual(rows[0]['due_at'], (self.now + timedelta(minutes=15)).timestamp())

    def test_body_first_weekday_and_date_clauses_are_not_saved_for_today(self):
        with patch('rocky.reminders.utc_now', return_value=self.now):
            for date, day in [('Monday', 9), ('on Monday', 9), ('5 November', 5), ('November 5', 5)]:
                with self.subTest(date=date):
                    commands = ReminderCommands(self.store)
                    self.assertIn('saved', commands.handle('remind me to call Mom ' + date + ' at 5 PM'))
                    row = self.store.latest_reminder()
                    due = datetime.fromtimestamp(row['due_at'], ZoneInfo(self.zone))
                    self.assertEqual((due.day, due.hour, row['body']), (day, 17, 'call Mom'))
            commands.handle('remind me to finish the Monday report at 5 PM')
            self.assertEqual(self.store.latest_reminder()['body'], 'finish the Monday report')

    def test_unclear_next_weekday_does_not_silently_fall_back_to_today(self):
        with patch('rocky.reminders.utc_now', return_value=self.now):
            for date in ('next Monday', 'on next Monday', 'Monday evening', 'Monday morning'):
                commands = ReminderCommands(self.store)
                self.assertIn('unclear', commands.handle('remind me to call Mom ' + date + ' at 5 PM'))
                self.assertEqual(self.store.reminders(), [])

    def test_dotted_clock_accepts_attached_meridiem_without_changing_decimal_durations(self):
        for phrase, day in [('at 5.30pm', 4), ('tomorrow at 5.30PM', 5)]:
            due = parse_due(phrase, now=self.now).astimezone(ZoneInfo(self.zone))
            self.assertEqual((due.day, due.hour, due.minute), (day, 17, 30))
        self.assertEqual(parse_due('in 1.25 hours', now=self.now),
                         self.now + timedelta(hours=1.25))

    def test_clock_changes_request_clarification_instead_of_silently_shifting(self):
        zone = 'America/New_York'
        for now, clock in [(datetime(2027, 3, 13, 12, tzinfo=ZoneInfo(zone)), '2:30 AM'),
                           (datetime(2027, 11, 6, 12, tzinfo=ZoneInfo(zone)), '1:30 AM')]:
            for phrase in ['at ' + clock, 'tomorrow at ' + clock]:
                with self.subTest(phrase=phrase, now=now):
                    with self.assertRaisesRegex(ValueError, 'clock change'):
                        parse_due(phrase, zone=zone, now=now)
            # A duration is an elapsed interval; it remains unambiguous across a clock change.
            self.assertEqual(parse_due('in fifteen hours', zone=zone, now=now),
                             now.astimezone(timezone.utc) + timedelta(hours=15))
            due = parse_due('tomorrow at ' + clock + ' UTC', zone=zone, now=now)
            self.assertEqual(due.hour, int(clock.split(':')[0]))
