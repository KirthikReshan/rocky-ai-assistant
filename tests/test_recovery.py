"""Fault injection without microphones, playback, COM or a running Ollama server."""
from pathlib import Path
import queue
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch, call

from rocky.voice import VoiceWorker
from rocky.ui_state import MicrophoneControl
from rocky.speech import Speech


class RecoveryTests(unittest.TestCase):
    def run_worker(self, assistant, *, speech_error=False, model_error=False, audio_error=False):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) if model_error else Path(directory) / 'missing-model'
            events = queue.Queue()
            worker = VoiceWorker(events, model_path, assistant=assistant, stt='vosk')
            worker.messages.put('first command')
            worker.messages.put('second command')
            original_emit = worker.emit

            def emit(kind, text):
                original_emit(kind, text)
                if kind == 'Rocky' and text == 'recovered':
                    worker.stopping.set()

            worker.emit = emit
            audio = types.SimpleNamespace(query_devices=Mock(return_value={'name': 'test mic'}),
                                          RawInputStream=Mock(side_effect=AssertionError('No real capture allowed')))
            vosk = types.SimpleNamespace(Model=Mock(side_effect=RuntimeError('corrupt model')),
                                         KaldiRecognizer=Mock())
            com = Mock()
            with patch.dict(sys.modules, {'pythoncom': com, 'sounddevice': None if audio_error else audio,
                                         'winsound': Mock(), 'vosk': vosk}), \
                 patch('rocky.speech.Speech', side_effect=OSError('speech unavailable') if speech_error else None):
                worker.start()
                try:
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive(), 'Worker failed to process its recovery command')
                finally:
                    worker.stopping.set()
                    worker.join(timeout=1)
            self.assertEqual(assistant.respond.call_args_list, [call('first command'), call('second command')])
            recorded = list(events.queue)
            self.assertIn(('Rocky', 'recovered'), recorded)
            self.assertFalse(any(kind == 'error' and 'Voice worker stopped' in text for kind, text in recorded))
            audio.RawInputStream.assert_not_called()
            return recorded

    def test_unexpected_command_failure_does_not_stop_next_command(self):
        assistant = Mock()
        assistant.respond.side_effect = [ConnectionResetError('inference disconnected'), 'recovered']
        events = self.run_worker(assistant)
        self.assertTrue(any(kind == 'error' and 'Command failed' in text for kind, text in events))

    def test_speech_startup_failure_preserves_text(self):
        assistant = Mock()
        assistant.respond.side_effect = ['first reply', 'recovered']
        events = self.run_worker(assistant, speech_error=True)
        self.assertTrue(any(kind == 'error' and 'Speech output unavailable' in text for kind, text in events))

    def test_corrupt_model_preserves_text(self):
        assistant = Mock()
        assistant.respond.side_effect = ['first reply', 'recovered']
        events = self.run_worker(assistant, model_error=True)
        self.assertTrue(any(kind == 'error' and 'model could not load' in text for kind, text in events))

    def test_missing_audio_dependency_preserves_text(self):
        assistant = Mock()
        assistant.respond.side_effect = ['first reply', 'recovered']
        self.run_worker(assistant, audio_error=True)

    def test_overflow_in_command_preserves_next_command(self):
        assistant = Mock()
        assistant.respond.side_effect = [OverflowError('invalid user duration'), 'recovered']
        self.run_worker(assistant)

    def test_stale_pause_label_cannot_resume_microphone(self):
        worker = VoiceWorker(queue.Queue(), Path('missing'))
        control = MicrophoneControl(worker)
        self.assertEqual(control.label, 'Pause microphone')
        worker.set_listening(False)  # failure happens before Tk processes its event
        control.toggle()  # user clicks the still-visible Pause action
        self.assertFalse(worker.enabled.is_set())
        self.assertEqual(control.label, 'Resume microphone')

    def test_worker_event_synchronizes_label_and_explicit_resume(self):
        worker = VoiceWorker(queue.Queue(), Path('missing'))
        control = MicrophoneControl(worker)
        worker.set_listening(False)
        kind, enabled = worker.events.get_nowait()
        self.assertEqual(kind, 'microphone')
        control.update(enabled)
        self.assertEqual(control.label, 'Resume microphone')
        control.toggle()
        self.assertTrue(worker.enabled.is_set())
        self.assertEqual(control.label, 'Pause microphone')

    def test_piper_can_initialize_without_windows_sapi(self):
        piper_voice = Mock()
        fake_piper = types.SimpleNamespace(PiperVoice=Mock(), SynthesisConfig=Mock())
        fake_piper.PiperVoice.load.return_value = piper_voice
        client = types.ModuleType('win32com.client')
        client.Dispatch = Mock(side_effect=OSError('SAPI unavailable'))
        win32 = types.ModuleType('win32com')
        win32.client = client
        with patch.dict(sys.modules, {'win32com': win32, 'win32com.client': client, 'piper': fake_piper}), \
             patch('rocky.speech.VOICE', Mock(is_file=Mock(return_value=True))):
            speech = Speech(Mock())
        self.assertIs(speech.voice, piper_voice)
        self.assertIsNone(speech.sapi)
