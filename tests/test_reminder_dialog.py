from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch

from rocky.assistant import Assistant
from rocky.reminders import ReminderCommands, parse_due, normalize_reminder_request
from rocky.storage import Store
from rocky.voice import VoiceWorker


class ReminderDialogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / 'state.db')
        self.now = datetime(2026, 11, 2, 21, 0, tzinfo=ZoneInfo('Asia/Kolkata'))
        self.clock = patch('rocky.reminders.utc_now', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.reminders = ReminderCommands(self.store)
        self.brain = Mock()
        self.brain.chat.return_value = "I've saved your reminder for today."
        self.assistant = Assistant(self.brain, reminders=self.reminders)

    def test_relative_time_then_message_saves_at_original_deadline(self):
        self.assertIn('What should', self.assistant.respond('Remind me in 2 minutes'))
        self.assertEqual(self.store.reminders(), [])
        with patch('rocky.reminders.utc_now', return_value=self.now + timedelta(seconds=30)):
            reply = self.assistant.respond('to go to bed')
        self.assertIn('Reminder 1 saved', reply)
        row = self.store.latest_reminder()
        self.assertEqual(row['body'], 'go to bed')
        self.assertEqual(row['due_at'], (self.now + timedelta(minutes=2)).timestamp())
        self.brain.chat.assert_not_called()

    def test_bare_duration_and_say_clause(self):
        reply = self.assistant.respond("Remind me 2 minutes and say it's time to go to bed.")
        self.assertIn('saved', reply)
        self.assertEqual(self.store.latest_reminder()['body'], "it's time to go to bed")
        self.brain.chat.assert_not_called()

    def test_reported_remain_me_phrases_save_real_reminders_without_model(self):
        for text in ('remain me in two minutes and say go to sleep.',
                     'remain me two minutes to go to sleep'):
            reply = self.assistant.respond(text)
            self.assertIn('saved', reply)
            row = self.store.latest_reminder()
            self.assertEqual(row['body'], 'go to sleep')
            self.assertEqual(row['due_at'], (self.now + timedelta(minutes=2)).timestamp())
        self.brain.chat.assert_not_called()

    def test_remain_me_incomplete_request_retains_followup(self):
        self.assertIn('What should', self.assistant.respond('Please remain me in two minutes'))
        self.assertIn('saved', self.assistant.respond('to go to sleep'))
        self.assertEqual(self.store.latest_reminder()['body'], 'go to sleep')
        self.brain.chat.assert_not_called()

    def test_remain_is_not_replaced_inside_notes_or_unrelated_text(self):
        for text in ('take a note that remain me is a transcription mistake',
                     'explain remain me', 'remain me in peace'):
            self.assertEqual(normalize_reminder_request(text), text)
        self.assistant.respond('take a note that remain me is a transcription mistake')
        self.assertIn('remain me', self.store.notes()[0]['body'])
        self.assertEqual(self.store.reminders(), [])

    def test_false_model_denial_is_corrected_using_actual_service_state(self):
        self.brain.chat.return_value = "I don't have a reminder service that can save reminders with specific phrases."
        reply = self.assistant.respond('Can you handle this for me?')
        self.assertIn('reminder service is available', reply)
        self.assertEqual(self.store.reminders(), [])
        self.brain.correct_last_reply.assert_called_once_with(reply)

    def test_reported_time_first_sentence_requires_ampm_then_saves(self):
        reply = self.assistant.respond('For today at 10.35 you should remind me for go to sleep')
        self.assertIn('AM or PM', reply)
        self.assertEqual(self.store.reminders(), [])
        self.assertIn('AM or PM', self.assistant.respond('Yes, save it'))
        self.assertEqual(self.store.reminders(), [])
        reply = self.assistant.respond('PM')
        self.assertIn('saved', reply)
        row = self.store.latest_reminder()
        self.assertEqual(row['body'], 'go to sleep')
        due = datetime.fromtimestamp(row['due_at'], self.now.tzinfo)
        self.assertEqual((due.day, due.hour, due.minute), (2, 22, 35))

    def test_status_questions_do_not_reach_model_or_claim_save(self):
        self.assistant.respond('remind me in 2 minutes')
        for question in ('did you check the reminder or you will check',
                         "why didn't you remind me", 'yes save it'):
            self.assertNotIn('Reminder 1 saved', self.assistant.respond(question))
        self.assertEqual(self.store.reminders(), [])
        self.brain.chat.assert_not_called()

    def test_hallucinated_model_save_is_replaced_with_database_status(self):
        self.assistant.respond('remind me to go to sleep')
        reply = self.assistant.respond('Tell me what happened earlier')
        self.assertIn('not saved yet', reply)
        self.assertEqual(self.store.reminders(), [])
        self.brain.correct_last_reply.assert_called_once_with(reply)

    def test_contextual_status_follow_up_stays_out_of_general_chat(self):
        self.assistant.respond('remind me to sleep')
        self.assertIn('not saved', self.assistant.respond('When did you keep doing all that?'))
        self.brain.chat.assert_not_called()

    def test_model_save_claim_without_pending_reminder_is_rejected(self):
        self.assertIn('no saved reminder', self.assistant.respond('Are you done?'))
        self.assertEqual(self.store.reminders(), [])

    def test_repeated_confirmation_does_not_duplicate_existing_reminder(self):
        self.assistant.respond('remind me in 2 minutes to sleep')
        for _ in range(3):
            self.assertIn('Saved reminder 1', self.assistant.respond('yes save it'))
        self.assertEqual(len(self.store.reminders()), 1)

    def test_past_today_time_is_not_silently_moved_to_tomorrow(self):
        self.assistant.respond('For today at 10.35 you should remind me for sleep')
        with patch('rocky.reminders.utc_now', return_value=self.now.replace(hour=23)):
            self.assertIn('past', self.assistant.respond('PM'))
        self.assertEqual(self.store.reminders(), [])

    def test_draft_can_be_cancelled_or_expire(self):
        self.assistant.respond('remind me in 2 minutes')
        self.assertIn('Discarded', self.assistant.respond('cancel that reminder'))
        self.assistant.respond('remind me in 2 minutes')
        self.reminders.pending_until = -1
        self.assertIn('no saved reminder', self.assistant.respond('yes save it'))
        self.assertEqual(self.store.reminders(), [])

    def test_unrelated_commands_are_not_taken_as_reminder_message(self):
        self.assistant.respond('remind me in 2 minutes')
        self.assertIsNone(self.reminders.handle('open calculator'))
        self.assertEqual(self.store.reminders(), [])

    def test_dotted_time_and_24_hour_time(self):
        with self.assertRaisesRegex(ValueError, 'AM or PM'):
            parse_due('today 10.35', now=self.now)
        due = parse_due('today at 22:35', now=self.now).astimezone(self.now.tzinfo)
        self.assertEqual((due.hour, due.minute), (22, 35))

    def test_noise_warning_does_not_extend_session_and_is_throttled(self):
        worker = VoiceWorker(queue.Queue(), Path('missing'))
        worker.conversation.renew()
        deadline = worker.conversation.deadline
        for _ in range(10):
            worker.unheard()
        self.assertEqual(worker.conversation.deadline, deadline)
        self.assertEqual(len(list(worker.events.queue)), 1)

    def test_go_to_sleep_as_reminder_body_does_not_end_conversation(self):
        worker = VoiceWorker(queue.Queue(), Path('missing'), assistant=self.assistant)
        self.assistant.respond('remind me in two minutes')
        self.assertFalse(worker.should_end('Go to sleep.'))
        self.assertIn('saved', self.assistant.respond('Go to sleep.'))
        self.assertEqual(self.store.latest_reminder()['body'], 'Go to sleep')
        self.assertTrue(worker.should_end('go to sleep'))

    def test_short_false_model_confirmation_is_blocked_during_draft(self):
        self.assistant.respond('remind me to sleep')
        self.brain.chat.return_value = 'Done!'
        self.assertIn('not saved', self.assistant.respond('Okay then'))
        self.assertEqual(self.store.reminders(), [])
