import queue
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from rocky.storage import Store
from rocky.voice import VoiceWorker
from rocky.voice_benchmark import levels, record_test, score


class VoiceBenchmarkTests(unittest.TestCase):
    def test_scoring_counts_missing_and_inserted_words(self):
        self.assertEqual(score('Open WhatsApp!', 'open whatsapp')['word_errors'], 0)
        self.assertEqual(score('open vs code', 'open code')['word_errors'], 1)
        self.assertEqual(score('open code', '')['word_errors'], 2)
        self.assertEqual(score('open code', 'please open code now')['word_errors'], 2)

    def test_levels_detect_silence_and_clipping(self):
        self.assertEqual(levels(bytes(3200))['peak'], 0)
        self.assertEqual(levels(b'\xff\x7f' * 1600)['clipped'], 1)

    def test_recording_reports_overflow_and_stops_on_exit(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.return_value = (bytes(3200), True)
        audio = Mock(RawInputStream=Mock(return_value=stream))
        stop = threading.Event()
        pcm, overflow = record_test(audio, None, stop, Mock())
        self.assertEqual(len(pcm), 192000)
        self.assertTrue(overflow)
        stop.set()
        with self.assertRaises(InterruptedError):
            record_test(audio, None, stop, Mock())

    def test_speak_now_is_emitted_only_after_the_microphone_opens(self):
        stream = Mock()
        opened = []
        stream.__enter__ = Mock(side_effect=lambda: (opened.append(True), stream)[1])
        stream.__exit__ = Mock(return_value=False)
        stream.read.return_value = (bytes(3200), False)
        record_test(Mock(RawInputStream=Mock(return_value=stream)), None, threading.Event(), Mock(),
                    started=lambda: self.assertEqual(opened, [True]))
        failed = Mock(RawInputStream=Mock(side_effect=OSError('No microphone')))
        started = Mock()
        with self.assertRaises(OSError):
            record_test(failed, None, threading.Event(), Mock(), started=started)
        started.assert_not_called()

    def test_results_survive_reopening_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rocky.db'
            Store(path).add_voice_test(dict(expected='open code', heard='open code',
                                           **score('open code', 'open code'), latency=.5,
                                           environment='Quiet room', device='Headset', overflow=False))
            self.assertEqual(Store(path).voice_tests()[0]['heard'], 'open code')

    def test_test_phrase_never_reaches_assistant(self):
        with tempfile.TemporaryDirectory() as folder:
            events = queue.Queue()
            assistant = Mock()
            worker = VoiceWorker(events, Path(folder) / 'missing', assistant=assistant)
            worker.enabled.clear()
            worker.tests.put({'expected': 'open calculator', 'environment': 'Quiet room'})
            original_emit = worker.emit

            def emit(kind, value):
                original_emit(kind, value)
                if kind in {'TestResult', 'TestError'}:
                    worker.stopping.set()

            worker.emit = emit
            audio = types.SimpleNamespace(query_devices=Mock(return_value={'name': 'Headset'}))
            with patch.dict(sys.modules, {'sounddevice': audio, 'pythoncom': Mock(), 'winsound': Mock(),
                                         'vosk': types.SimpleNamespace(Model=Mock(), KaldiRecognizer=Mock())}), \
                 patch('rocky.speech.Speech'), \
                 patch('rocky.voice.Transcriber') as transcriber, \
                 patch('rocky.voice.record_test', return_value=(bytes(3200), False)), \
                 patch.object(Path, 'is_file', return_value=True):
                transcriber.return_value.transcribe.return_value = 'open calculator'
                worker.run()
            assistant.respond.assert_not_called()
            self.assertFalse(worker.enabled.is_set())
            results = [value for kind, value in list(events.queue) if kind == 'TestResult']
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]['exact'])
