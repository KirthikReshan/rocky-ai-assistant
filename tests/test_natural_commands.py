from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rocky.assistant import Assistant
from rocky.reminders import ReminderCommands
from rocky.storage import Store
from rocky.tools import Action, DesktopTools, parse_action


class NaturalCommandsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'rocky.db')
        self.now = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
        clock = patch('rocky.reminders.utc_now', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.reminders = ReminderCommands(self.store)

    def test_natural_open_phrases(self):
        for phrase in ('Can you bring up WhatsApp?', 'Could you please open WhatsApp for me?',
                       'Would you pull up WhatsApp please?'):
            self.assertEqual(parse_action(phrase), Action('open', 'WhatsApp'))
        self.assertIsNone(parse_action('Explain how to bring up WhatsApp'))

    def test_saved_nickname_resolves_without_executing_model_text(self):
        self.store.set_setting('app_nicknames', {'work editor': 'VS Code'})
        with patch('rocky.tools.app_path', return_value='C:/Apps/Code.exe'), \
             patch('rocky.tools.subprocess.Popen') as launch:
            result = DesktopTools(Store(self.store.path)).execute(Action('open', 'work editor'))
            launch.assert_called_once_with(['C:/Apps/Code.exe'], shell=False)
            self.assertIn('Opening', result)

    def test_recent_reminder_is_rescheduled_without_creating_another(self):
        self.reminders.handle('remind me in two minutes to stretch')
        self.assertIn('updated', self.reminders.handle('Actually, make that ten minutes'))
        rows = self.store.reminders()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['body'], 'stretch')
        self.assertEqual(rows[0]['due_at'], (self.now + timedelta(minutes=10)).timestamp())

    def test_delivered_or_externally_edited_reminder_cannot_be_rescheduled(self):
        self.reminders.handle('remind me in two minutes to stretch')
        row = self.store.latest_reminder()
        self.store.update_reminder(row['id'], 'new body', self.now + timedelta(minutes=8),
                                   row['timezone'], created_at=row['created_at'])
        self.assertIn('changed', self.reminders.handle('make that ten minutes'))
        self.assertEqual(self.store.latest_reminder()['body'], 'new body')
        self.reminders.handle('remind me in two minutes to stretch')
        self.store.acknowledge(self.store.latest_reminder()['id'])
        self.assertIn('already issued', self.reminders.handle('make that ten minutes'))

    def test_expired_or_unrelated_context_requests_clarification(self):
        self.reminders.handle('remind me in two minutes to stretch')
        self.reminders.recent_until = 0
        self.assertIn('Which reminder', self.reminders.handle('make that ten minutes'))
        self.reminders.handle('remind me in two minutes to stretch')
        self.reminders.handle('can you bring up WhatsApp')
        self.assertIn('Which reminder', self.reminders.handle('make that ten minutes'))

    def test_open_command_does_not_become_unfinished_reminder_body(self):
        self.reminders.handle('remind me in two minutes')
        tools = Mock()
        Assistant(tools=tools, reminders=self.reminders).respond('bring up WhatsApp')
        tools.execute.assert_called_once_with(Action('open', 'WhatsApp'))
        self.assertEqual(self.store.reminders(), [])

    def test_draft_correction_still_waits_for_body(self):
        self.reminders.handle('remind me in two minutes')
        self.assertIn('What should', self.reminders.handle('make that ten minutes'))
        self.reminders.handle('stretch')
        self.assertEqual(self.store.latest_reminder()['due_at'], (self.now + timedelta(minutes=10)).timestamp())
