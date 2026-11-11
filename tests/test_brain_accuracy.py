import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from rocky.brain import BrainError, OllamaBrain, clean_answer
from rocky.coding import CodeCommands, Workspace
from rocky.reply_stream import draft_reply
from rocky.storage import Store


def reply(answer):
    return io.BytesIO(json.dumps({'message': {'content': json.dumps({'reply': answer})}}).encode())


class ReplyValidationTests(unittest.TestCase):
    def test_reasoning_tags_are_case_insensitive(self):
        self.assertEqual(clean_answer('<THINK>private</THINK>Hello'), 'Hello')
        self.assertEqual(clean_answer('Hello<Think>private'), 'Hello')

    def test_invalid_unicode_cannot_reach_draft_or_final_output(self):
        for invalid in ('\udc00', '\ud800', 'Hello\ud800world'):
            with self.subTest(invalid=repr(invalid)):
                draft = draft_reply(json.dumps({'reply': invalid}))
                draft.encode('utf-8')
                brain = OllamaBrain()
                brain.http.open = Mock(return_value=reply(invalid))
                with self.assertRaises(BrainError):
                    brain.chat('Question')
                self.assertEqual(brain.history, [])


class FinalizedMemoryTests(unittest.TestCase):
    def test_toggle_between_raw_response_and_router_correction_saves_only_final_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'memory.db')
            brain = OllamaBrain(memory_store=store)
            brain.http.open = Mock(return_value=reply("I've saved your reminder."))
            brain.chat('Are you ready?')
            brain.set_memory_enabled(True)
            self.assertEqual(store.setting('chat_history'), [])
            self.assertEqual(brain.history_snapshot(), [])
            brain.correct_last_reply('No reminder was saved.')
            self.assertEqual(store.setting('chat_history')[-1]['content'], 'No reminder was saved.')
            self.assertEqual(brain.history_snapshot()[-1]['content'], 'No reminder was saved.')

    def test_clear_before_correction_does_not_restore_pending_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'memory.db')
            brain = OllamaBrain(memory_store=store)
            brain.set_memory_enabled(True)
            brain.http.open = Mock(return_value=reply('Pending answer'))
            brain.chat('Question')
            brain.clear_history()
            brain.correct_last_reply('Corrected answer')
            self.assertEqual(brain.history_snapshot(), [])
            self.assertEqual(store.setting('chat_history'), [])


class CodingAccuracyTests(unittest.TestCase):
    def test_traceback_reads_reported_line_beyond_first_excerpt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'main.py'
            path.write_text('# unrelated ValueError\n' * 150 + 'raise ValueError("actual failure")\n')
            workspace = Workspace()
            workspace.select(root)
            brain = Mock()
            brain.code_chat.return_value = 'Explanation'
            CodeCommands(workspace, brain).handle(
                f'explain error: File "{path}", line 151: ValueError')
            context = brain.code_chat.call_args.args[1]
            self.assertIn('151: raise ValueError("actual failure")', context)
            self.assertNotIn('1: # unrelated', context)

    def test_natural_bug_review_retains_review_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'main.py').write_text('def divide(a, b):\n    return a / b\n')
            workspace = Workspace()
            workspace.select(root)
            brain = Mock()
            brain.code_chat.return_value = 'Review'
            CodeCommands(workspace, brain).handle('check my code for bugs')
            self.assertIn('Review the supplied source excerpts for concrete bugs',
                          brain.code_chat.call_args.args[0])
