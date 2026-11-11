import io
import json
import unittest
import urllib.error
from unittest.mock import Mock
from http.client import RemoteDisconnected, IncompleteRead

from rocky.assistant import Assistant
from rocky.brain import BrainError, OllamaBrain


class BrainTests(unittest.TestCase):
    def test_local_request_and_bounded_history(self):
        brain = OllamaBrain()
        brain.http.open = Mock(side_effect=lambda *a, **k: io.BytesIO(
            json.dumps({"message": {"content": json.dumps({"reply": "Hello"})}}).encode()))
        for _ in range(6):
            self.assertEqual(brain.chat("Hi"), "Hello")
        request = brain.http.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        body = json.loads(request.data)
        self.assertFalse(body["stream"])
        self.assertFalse(body["think"])
        self.assertEqual(body["format"]["required"], ["reply"])
        self.assertNotIn("tools", body)
        self.assertEqual(len(brain.history), 8)

    def test_failure_does_not_enter_history(self):
        for error in (urllib.error.URLError("refused"), TimeoutError(),
                      urllib.error.HTTPError("url", 404, "missing", {}, None)):
            brain = OllamaBrain()
            brain.http.open = Mock(side_effect=error)
            with self.assertRaises(BrainError):
                brain.chat("hello")
            self.assertEqual(brain.history, [])

    def test_disconnect_and_incomplete_response_are_recoverable(self):
        for error in (ConnectionResetError('reset'), RemoteDisconnected('closed'), IncompleteRead(b'partial')):
            brain = OllamaBrain()
            brain.http.open = Mock(side_effect=error)
            with self.assertRaises(BrainError):
                brain.chat('first question')
            self.assertEqual(brain.history, [])
            brain.http.open = Mock(return_value=io.BytesIO(json.dumps({'message': {
                'content': json.dumps({'reply': 'recovered'})}}).encode()))
            self.assertEqual(brain.chat('second question'), 'recovered')

    def test_bad_responses(self):
        for payload in (b"bad json", b"{}", b'{"message":{"content":""}}'):
            brain = OllamaBrain()
            brain.http.open = Mock(return_value=io.BytesIO(payload))
            with self.assertRaises(BrainError):
                brain.chat("hello")

    def test_builtin_time_does_not_require_model(self):
        brain = Mock()
        self.assertIn("It is", Assistant(brain).respond("What time is it?"))
        brain.chat.assert_not_called()

    def test_failure_becomes_user_message(self):
        brain = Mock()
        brain.chat.side_effect = BrainError("Start Ollama")
        self.assertEqual(Assistant(brain).respond("Explain gravity"), "Start Ollama")

    def test_tagged_reasoning_is_not_saved(self):
        brain = OllamaBrain()
        brain.http.open = Mock(return_value=io.BytesIO(json.dumps({
            "message": {"content": json.dumps({"reply": "private</think>Hello"})}
        }).encode()))
        self.assertEqual(brain.chat("Hi"), "Hello")
        self.assertEqual(brain.history[-1]["content"], "Hello")

    def test_edit_proposal_uses_structured_schema_without_chat_history(self):
        brain = OllamaBrain()
        result = {'summary': 'Fix sum', 'edits': [{'old': 'a - b', 'new': 'a + b'}]}
        brain.http.open = Mock(return_value=io.BytesIO(json.dumps({'message': {
            'content': json.dumps(result)}}).encode()))
        self.assertEqual(brain.propose_edit('Fix sum', 'main.py', 'return a - b'), result)
        self.assertEqual(brain.history, [])
        request = brain.http.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body['format']['required'], ['summary', 'edits'])
        self.assertNotIn('tools', body)

    def test_truncated_proposal_is_rejected(self):
        brain = OllamaBrain()
        brain.http.open = Mock(return_value=io.BytesIO(json.dumps({'done_reason': 'length',
            'message': {'content': '{"summary":"partial","edits":[]}'}}).encode()))
        with self.assertRaises(BrainError):
            brain.propose_edit('Fix sum', 'main.py', 'return a - b')

    def test_corrected_reply_replaces_false_claim_in_history(self):
        brain = OllamaBrain()
        brain.history = [{'role': 'user', 'content': 'save it'},
                         {'role': 'assistant', 'content': "I've saved your reminder"}]
        brain.correct_last_reply('No reminder has been saved.')
        self.assertEqual(brain.history[-1]['content'], 'No reminder has been saved.')
