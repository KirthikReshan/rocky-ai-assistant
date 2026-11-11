import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

# Import before patch.dict snapshots sys.modules, so its restoration cannot
# unload the mocked speech module between the two worker subtests.
import rocky.speech
from rocky.assistant import Assistant
from rocky.brain import OllamaBrain
from rocky.sentence_stream import SentenceStream
from rocky.voice import VoiceWorker


class SentenceStreamTests(unittest.TestCase):
    def test_complete_sentences_emit_once_and_final_remainder_is_not_repeated(self):
        spoken = []
        sentences = SentenceStream(spoken.append)
        sentences.feed('Hello.')
        self.assertEqual(spoken, [])
        sentences.feed('Hello. There is more')
        sentences.feed('Hello. There is more to say. Finally')
        self.assertEqual(spoken, ['Hello.', 'There is more to say.'])
        self.assertEqual(sentences.remaining('Hello. There is more to say. Finally.'), 'Finally.')

    def test_abbreviations_decimals_and_whitespace(self):
        spoken = []
        sentences = SentenceStream(spoken.append)
        sentences.feed('Dr. Smith has 3.14 units.\n\nNext sentence.')
        self.assertEqual(spoken, ['Dr. Smith has 3.14 units.'])
        self.assertEqual(sentences.remaining('Dr. Smith has 3.14 units. Next sentence.'), 'Next sentence.')

    def test_revision_halts_further_draft_speech(self):
        spoken = []
        sentences = SentenceStream(spoken.append)
        sentences.feed('First sentence. Next')
        sentences.feed('Changed sentence. More')
        sentences.feed('Changed sentence. More text. End')
        self.assertEqual(spoken, ['First sentence.'])
        self.assertEqual(sentences.remaining('Corrected answer.'), 'Corrected answer.')

    def test_assistant_filters_false_reminder_claims_before_speech(self):
        brain = OllamaBrain()
        brain.http.open = Mock(return_value=io.BytesIO(
            (json.dumps({'done': True, 'message': {'content':
             json.dumps({'reply': "I've saved your reminder. You are all set."})}}) + '\n').encode()))
        spoken = []
        result = Assistant(brain).respond('Tell me something', on_draft=SentenceStream(spoken.append).feed)
        self.assertEqual(spoken, [])
        self.assertIn('No reminder was saved', result)

    def test_worker_speaks_first_sentence_before_generation_finishes_and_stop_cancels_rest(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt), tempfile.TemporaryDirectory() as folder:
                events = queue.Queue()
                speaker = Mock()
                worker = VoiceWorker(events, Path(folder) / 'missing', stt='vosk')
                calls = []
                first_spoken = threading.Event()
                first_stopped = threading.Event()

                class StreamingAssistant:
                    supports_sentence_speech = True

                    def respond(self, text, on_draft=None):
                        on_draft('First sentence. Second')
                        self_test.assertTrue(first_spoken.wait(2))
                        self_test.assertEqual(calls, ['First sentence.'])
                        if interrupt:
                            worker.stop_speaking()
                        first_stopped.set()
                        on_draft('First sentence. Second sentence. Third')
                        return 'First sentence. Second sentence. Third sentence.'

                self_test = self
                worker.assistant = StreamingAssistant()
                worker.messages.put('Tell me something')
                worker.enabled.clear()
                def speak_sentence(text, **kwargs):
                    calls.append(text)
                    first_spoken.set()
                    self_test.assertTrue(first_stopped.wait(2))

                speaker.speak.side_effect = speak_sentence
                original_emit = worker.emit

                def emit(kind, value):
                    original_emit(kind, value)
                    if kind == 'speaking' and value is False:
                        worker.stopping.set()

                worker.emit = emit
                audio = types.SimpleNamespace(query_devices=Mock(return_value={'name': 'Test input'}))
                with patch.dict(sys.modules, {'pythoncom': Mock(), 'sounddevice': audio, 'winsound': Mock(),
                                             'vosk': types.SimpleNamespace(Model=Mock(), KaldiRecognizer=Mock())}), \
                     patch('rocky.speech.Speech', return_value=speaker):
                    worker.run()
                self.assertEqual(' '.join(calls), 'First sentence.' if interrupt else
                                 'First sentence. Second sentence. Third sentence.')
                self.assertEqual(sum(kind == 'Rocky' for kind, _ in list(events.queue)), 1)
                self.assertFalse(worker.speaking.is_set())
