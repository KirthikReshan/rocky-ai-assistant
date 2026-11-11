"""Voice lifetime and output-device failures without recording or playback."""
from pathlib import Path
import queue
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from rocky.speech import Speech
from rocky.voice import VoiceWorker
from rocky.voice_interrupt import VoiceInterrupt


class VoiceLifecycleTests(unittest.TestCase):
    def test_piper_output_failure_keeps_engine_and_does_not_redirect_to_windows_voice(self):
        speech = Speech.__new__(Speech)
        voice = speech.voice = Mock()
        voice.config.sample_rate = 16000
        voice.synthesize.side_effect = lambda *args, **kwargs: iter([
            types.SimpleNamespace(audio_int16_bytes=bytes(3200))])
        speech.config = None
        speech.report = Mock()
        speech.sapi = Mock()
        with patch('rocky.playback.play_pcm', side_effect=OSError('Selected headset disconnected')):
            with self.assertRaisesRegex(OSError, 'headset disconnected'):
                speech.speak('Hello', device=7)
        self.assertIs(speech.voice, voice)
        speech.sapi.Speak.assert_not_called()
        with patch('rocky.playback.play_pcm') as playback:
            speech.speak('Hello again', device=7)
        self.assertEqual(playback.call_args.args[3], 7)

    def test_actual_piper_synthesis_failure_still_uses_windows_voice(self):
        speech = Speech.__new__(Speech)
        speech.voice = Mock()
        speech.voice.config.sample_rate = 16000
        speech.voice.synthesize.side_effect = RuntimeError('Synthesis failed')
        speech.config = None
        speech.report = Mock()
        speech.sapi = Mock()
        speech.sapi.WaitUntilDone.return_value = True
        speech.speak('Hello')
        speech.sapi.Speak.assert_called_once_with('Hello', 1)

    def test_closed_stop_listener_cannot_interrupt_after_inflight_decode(self):
        interrupt = Mock()
        listener = VoiceInterrupt(Mock(), Mock(), None, lambda: True, interrupt, Mock())
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        listener.sd.RawInputStream.return_value = stream
        listener.audio.put(bytes(3200))
        with patch('rocky.voice_interrupt.PhraseDetector') as detector:
            detector.return_value.feed.side_effect = lambda pcm: listener.closed.set() or True
            listener.run()
        interrupt.assert_not_called()

    def test_inactive_stop_listener_never_opens_the_microphone(self):
        for already_closed in (False, True):
            with self.subTest(already_closed=already_closed):
                listener = VoiceInterrupt(Mock(), Mock(), None, lambda: already_closed, Mock(), Mock())
                if already_closed:
                    listener.closed.set()
                with patch('rocky.voice_interrupt.PhraseDetector') as detector:
                    listener.run()
                detector.assert_not_called()
                listener.sd.RawInputStream.assert_not_called()

    def test_stop_listener_follows_current_option_microphone_and_listening_session(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = VoiceWorker(queue.Queue(), Path(folder), assistant=Mock(), stt='vosk')
            worker.assistant.respond.return_value = 'Hello'
            worker.voice_interrupt_enabled = True
            worker.device = 3
            worker.messages.put('hello')
            original_emit = worker.emit
            def emit(kind, value):
                original_emit(kind, value)
                if kind == 'speaking' and value is False:
                    worker.stopping.set()
            worker.emit = emit
            speaker = Mock()
            listener = Mock()
            listener.close.return_value = True
            def speak(*args, **kwargs):
                allowed = create_listener.call_args.args[3]
                self.assertTrue(allowed())
                worker.voice_interrupt_enabled = False
                self.assertFalse(allowed())
                worker.voice_interrupt_enabled = True
                self.assertTrue(allowed())
                worker.device = 4
                self.assertFalse(allowed())
                worker.device = 3
                self.assertTrue(allowed())
                worker.set_listening(False)
                worker.set_listening(True)
                self.assertFalse(allowed())
            speaker.speak.side_effect = speak
            audio = types.SimpleNamespace(query_devices=Mock(return_value={'name': 'Test'}))
            with patch.dict(sys.modules, {'sounddevice': audio, 'pythoncom': Mock(), 'winsound': Mock(),
                                         'vosk': types.SimpleNamespace(Model=Mock(), KaldiRecognizer=Mock())}), \
                 patch('rocky.speech.Speech', return_value=speaker), \
                 patch('rocky.voice.VoiceInterrupt', return_value=listener) as create_listener:
                worker.run()
            listener.start.assert_called_once()
            listener.close.assert_called_once()
            self.assertFalse(any(kind == 'error' for kind, _ in list(worker.events.queue)),
                             list(worker.events.queue))
