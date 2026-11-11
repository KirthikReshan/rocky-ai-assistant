import io
import json
import unittest
from unittest.mock import Mock

from rocky.brain import BrainError, OllamaBrain
from rocky.reply_stream import draft_reply


def stream(*parts):
    return io.BytesIO(b''.join(json.dumps(part).encode() + b'\n' for part in parts))


class ReplyStreamTests(unittest.TestCase):
    def test_partial_json_and_escapes(self):
        self.assertEqual(draft_reply('{"reply":"Hello'), 'Hello')
        self.assertEqual(draft_reply('{"reply":"Hello\\'), 'Hello')
        self.assertEqual(draft_reply('{"reply":"Hello\\nworld"}'), 'Hello\nworld')
        self.assertEqual(draft_reply('{"thinking":"hidden'), '')
        self.assertEqual(draft_reply('{"reply":"<think>hidden'), '')
        self.assertEqual(draft_reply('{"reply":"\\ud83d'), '')
        self.assertEqual(draft_reply('{"reply":"\\ud83d\\ude00"}'), '😀')

    def test_streamed_reply_is_finalized_and_saved_once(self):
        drafts = []
        brain = OllamaBrain(on_partial=drafts.append)
        brain.http.open = Mock(return_value=stream(
            {'message': {'thinking': 'private reasoning', 'content': '{"reply":"Hello'}},
            {'done': True, 'message': {'content': ' there"}'}}))
        self.assertEqual(brain.chat('Tell me something'), 'Hello there')
        self.assertIn('Hello', drafts)
        self.assertIn('Hello there', drafts)
        self.assertEqual(drafts[-1], '')
        self.assertEqual(len(brain.history), 2)
        self.assertTrue(json.loads(brain.http.open.call_args.args[0].data)['stream'])
        self.assertNotIn('private reasoning', ''.join(drafts))

    def test_broken_stream_clears_draft_without_saving_partial_history(self):
        for ending in ({'error': 'model stopped'}, {'done': True, 'done_reason': 'length'},
                       {'done': True, 'message': {'content': 'broken'}}):
            drafts = []
            brain = OllamaBrain(on_partial=drafts.append)
            brain.http.open = Mock(return_value=stream(
                {'message': {'content': '{"reply":"partial'}}, ending))
            with self.assertRaises(BrainError):
                brain.chat('Question')
            self.assertEqual(brain.history, [])
            self.assertEqual(drafts[-1], '')

    def test_missing_done_marker_is_rejected(self):
        brain = OllamaBrain(on_partial=Mock())
        brain.http.open = Mock(return_value=stream({'message': {'content': '{"reply":"Hello"}'}}))
        with self.assertRaises(BrainError):
            brain.chat('Question')
        self.assertEqual(brain.history, [])

    def test_code_proposals_still_use_complete_structured_responses(self):
        brain = OllamaBrain(on_partial=Mock())
        result = {'summary': 'Fix', 'edits': [{'old': 'x', 'new': 'y'}]}
        brain.http.open = Mock(return_value=io.BytesIO(json.dumps({
            'message': {'content': json.dumps(result)}}).encode()))
        self.assertEqual(brain.propose_edit('Fix', 'a.py', 'x'), result)
        self.assertFalse(json.loads(brain.http.open.call_args.args[0].data)['stream'])
        brain.on_partial.assert_not_called()
