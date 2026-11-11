"""Regression coverage for the model/document accuracy audit."""
from dataclasses import replace
from contextlib import nullcontext
from datetime import datetime
import io
import json
from pathlib import Path
import queue
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from rocky.brain import BrainError, OllamaBrain
from rocky.assistant import Assistant
from rocky.reminders import ReminderCommands
from rocky.documents import Documents, DocumentHit
from rocky.document_index import DocumentIndex
from rocky.model_settings import CredentialVault
from rocky.model_transport import local_json, local_slot, NimClient
from rocky.storage import Store
from rocky.task_brain import TaskBrain


class AccuracyAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'test.db')
        self.brain = TaskBrain(self.store)
        # These tests exercise the legacy analysis/recovery contracts. Dedicated
        # session-Q&A tests cover the new evidence-based answer pipeline.
        grounded = patch.object(TaskBrain, 'supports_grounded_documents', False)
        grounded.start()
        self.addCleanup(grounded.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_local_api_rejects_non_object_payloads(self):
        for value in ([], None, 'unexpected'):
            http = Mock(open=Mock(return_value=io.BytesIO(json.dumps(value).encode())))
            with self.assertRaises(BrainError):
                local_json('tags', http=http)

    def test_bad_nim_choice_shape_is_recoverable(self):
        client = NimClient(Mock(read=Mock(return_value='synthetic-test-key')), 'nvidia/model')
        client.http.open = Mock(return_value=io.BytesIO(json.dumps({'choices': [None]}).encode()))
        import threading
        with self.assertRaises(BrainError):
            client.request([], None, 50, threading.Event(), .1)

    def test_uncited_facts_and_unfamiliar_refusals_do_not_become_answers(self):
        for answer in ('The bank account number is 12345.', 'That detail is not mentioned in the provided material.'):
            brain = OllamaBrain()
            brain.http.open = Mock(return_value=io.BytesIO(json.dumps({'message': {'content': json.dumps({
                'answer': answer, 'sources': []})}}).encode()))
            result = brain.document_chat('What is the bank account?', '[S1] Deadline Friday')
            self.assertIn("couldn't find", result)
            self.assertNotIn('12345', result)

    def test_invalid_credential_encoding_does_not_expose_blob(self):
        module = Mock(CRED_TYPE_GENERIC=1)
        module.CredRead.return_value = {'CredentialBlob': b'secret-odd-data'}
        # An odd number of UTF-16 bytes must not leak into an error string.
        with patch.dict('sys.modules', {'win32cred': module}):
            with self.assertRaises(OSError) as error:
                CredentialVault(self.store.path).read()
        self.assertNotIn('secret', str(error.exception))

    def test_translation_rejects_added_signed_percentage_and_repeated_number_changes(self):
        cases = [('Budget 500 dollars.', 'பட்ஜெட் 500 டாலர்கள். மேலும் 100 டாலர்கள்.'),
                 ('Balance -500 dollars.', 'இருப்பு 500 டாலர்கள்.'),
                 ('Tax is 5%.', 'வரி 5.'),
                 ('A has 500 and B has 500.', 'இருவருக்கும் 500.')]
        for source, translated in cases:
            with self.subTest(source=source):
                self.brain._request = Mock(return_value=translated)
                with self.assertRaisesRegex(BrainError, 'number'):
                    self.brain.translate_text('Tamil', source)

    def test_numeric_only_passage_can_remain_numeric_in_translation(self):
        self.brain._request = Mock(return_value='2026 500')
        self.assertEqual(self.brain.translate_text('Tamil', '2026 500'), '2026 500')

    def test_whole_document_question_does_not_drop_the_last_sources(self):
        docs = Documents(self.store, self.brain, queue.Queue())
        docs.workspace.select(self.root)
        hits = [DocumentHit('project.txt', number, f'Condition {number}: deadline day {number}. ' + 'Supporting detail. ' * 80, 'project.txt')
                for number in range(1, 13)]
        docs.load = Mock(return_value=(hits, []))
        docs.index = Mock()
        docs.index.search.return_value = hits[:8]
        docs.analyze = Mock(return_value='All conditions [S12]')
        docs.perform('Ask', 'project.txt', 'List all conditions across the entire document.')
        self.assertEqual(docs.analyze.call_args.args[2], hits)
        docs.index.search.assert_not_called()

    def test_short_document_question_avoids_unnecessary_indexing(self):
        docs = Documents(self.store, self.brain, queue.Queue())
        docs.workspace.select(self.root)
        docs.load = Mock(return_value=([DocumentHit('project.txt', 1, 'Budget 500.', 'project.txt')], []))
        docs.index = Mock()
        docs.analyze = Mock(return_value='Budget 500 [S1]')
        docs.perform('Ask', 'project.txt', 'What is the budget?')
        docs.index.search.assert_not_called()

    def test_specific_long_question_still_uses_indexed_passages(self):
        docs = Documents(self.store, self.brain, queue.Queue())
        docs.workspace.select(self.root)
        hits = [DocumentHit('project.txt', 1, 'Long source text. ' * 500, 'project.txt')]
        docs.load = Mock(return_value=(hits, []))
        docs.index = Mock(search=Mock(return_value=[replace(hits[0], excerpt='The budget is 500.')]))
        docs.analyze = Mock(return_value='500 [S1]')
        docs.perform('Ask', 'project.txt', 'What is the budget?')
        docs.index.search.assert_called_once()
        self.assertEqual(docs.analyze.call_args.args[2][0].excerpt, 'The budget is 500.')

    def test_failed_generation_is_still_released_before_loading_another_model(self):
        import threading
        with patch('rocky.model_transport._resident_model', None), patch('rocky.model_transport.local_json') as unload:
            with self.assertRaises(BrainError):
                with local_slot('model-one', threading.Event()):
                    raise BrainError('Invalid response after loading')
            with local_slot('model-two', threading.Event()):
                pass
            self.assertEqual(unload.call_args.args[1]['model'], 'model-one')

    def test_natural_clock_question_cannot_become_a_reminder_body(self):
        reminders = ReminderCommands(self.store)
        reminders.handle('remind me in two minutes')
        brain = Mock()
        brain.chat.return_value = 'Guessed time'
        assistant = Assistant(brain, reminders=reminders)
        answer = assistant.respond('Can you please tell me what time it is?')
        self.assertRegex(answer, r'^It is \d{2}:\d{2} [AP]M\.$')
        self.assertEqual(self.store.reminders(), [])
        self.assertTrue(reminders.expects_body)
        brain.chat.assert_not_called()

    def test_current_date_questions_use_the_clock_instead_of_model_memory(self):
        brain = Mock()
        brain.chat.return_value = 'Guessed date'
        with patch('rocky.core.datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 9, 18, 45)
            for question in ("What's today's date?", 'What date is it today?',
                             'Could you please tell me the current date?', 'What day is it today?'):
                self.assertIn('09 November 2026', Assistant(brain).respond(question))
        brain.chat.assert_not_called()

    def test_corrupt_cached_vectors_rebuild_instead_of_disabling_search(self):
        import numpy as np
        client = Mock()
        client.digest.return_value = 'stable-model'
        client.embed.side_effect = lambda texts, cancel: np.array([[1., 0.]] * len(texts), dtype=np.float32)
        index = DocumentIndex(self.store, 'embeddinggemma', client)
        hits = [DocumentHit('plan.txt', 1, 'Deadline Friday')]
        index.search(hits, 'When is delivery?', threading.Event())
        next(iter(index.entries.values())).vectors = np.zeros((1, 2), dtype=np.float32)
        self.assertTrue(index.search(hits, 'When is delivery?', threading.Event()))
        # First index + query, then a repaired index + query.
        self.assertEqual(client.embed.call_count, 4)

    def test_clock_detection_does_not_capture_questions_about_code_or_events(self):
        from rocky.core import is_time_request, is_date_request
        for question in ('What is the time complexity of sorting?', 'What date is the meeting?',
                         'Tell me the date format used by Python.', 'What time is my flight?'):
            self.assertFalse(is_time_request(question) or is_date_request(question))

    def _document_reply(self, foreground):
        (self.root / 'plan.txt').write_text('The deadline is Friday.', encoding='utf-8')
        docs = Documents(self.store, self.brain, queue.Queue())
        docs.workspace.select(self.root)
        docs.selected = 'plan.txt'
        entered, release = threading.Event(), threading.Event()
        def answer(*args):
            entered.set()
            if foreground:
                docs._cancel.wait(3)
            else:
                release.wait(3)
            return 'Friday [S1]'
        docs.brain.document_chat = Mock(side_effect=answer)
        extensions = SimpleNamespace(documents=docs, handle=lambda text, tools: docs.handle(text))
        assistant = Assistant(self.brain, extensions=extensions)
        outputs = queue.Queue()
        def work():
            try:
                result = (assistant.respond('ask this document: When is the deadline?') if foreground
                          else docs.perform('Ask', 'plan.txt', 'When is the deadline?'))
                outputs.put(result)
            except Exception as exc:
                outputs.put(exc)
        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            assistant.cancel_current()
            if foreground:
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
                self.assertIn('cancelled', outputs.get_nowait().casefold())
            else:
                self.assertFalse(docs._cancel.is_set())
        finally:
            release.set()
            thread.join(timeout=3)
            if thread.is_alive():
                docs._cancel.set()
                thread.join(timeout=1)
        if not foreground:
            self.assertFalse(docs._cancel.is_set())
            self.assertEqual(outputs.get_nowait().answer, 'Friday [S1]')

    def test_new_question_cancels_a_foreground_document_answer(self):
        self._document_reply(True)

    def test_chat_cancellation_preserves_a_separate_document_page_job(self):
        self._document_reply(False)


if __name__ == '__main__':
    unittest.main()
