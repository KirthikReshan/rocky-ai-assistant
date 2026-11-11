"""Capture regressions without using the microphone, COM, or an AI model."""
import json
from pathlib import Path
import queue
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from rocky.voice import VoiceWorker
import rocky.speech  # Load optional speech dependencies before isolated module patches.
from rocky.wake import PhraseDetector, STOP_PHRASES, evaluate_wake, wake_command


class VoiceCaptureTests(unittest.TestCase):
    def test_wake_prefix_preserves_a_command_without_loosening_stop_matching(self):
        result = json.dumps({'text': 'hey rocky [unk]', 'result': [
            {'word': 'hey', 'conf': .95}, {'word': 'rocky', 'conf': .95},
            {'word': '[unk]', 'conf': .05}]})
        recognizer = Mock()
        recognizer.AcceptWaveform.return_value = True
        recognizer.Result.return_value = result
        recognizer.FinalResult.return_value = result
        with patch.dict(sys.modules, {'vosk': types.SimpleNamespace(KaldiRecognizer=Mock(return_value=recognizer))}):
            detector = PhraseDetector(Mock(), prefix=True)
            self.assertTrue(detector.feed(bytes(960)))
            self.assertTrue(detector.has_suffix)
            self.assertEqual(detector.confidence, .95)
            self.assertFalse(PhraseDetector(Mock()).accept(result))
            self.assertFalse(PhraseDetector(Mock(), STOP_PHRASES).accept(
                json.dumps({'text': 'stop speaking about it', 'result': [{'conf': .95}] * 4})))
            verifier = Mock()
            verifier.transcribe.return_value = 'Hey Rocky, open WhatsApp.'
            self.assertTrue(evaluate_wake(bytes(960), Mock(), verifier=verifier))
            verifier.transcribe.return_value = 'They said hey Rocky yesterday'
            self.assertFalse(evaluate_wake(bytes(960), Mock(), verifier=verifier))
        self.assertEqual(wake_command('Hey Rocky, open WhatsApp!'), 'open WhatsApp!')
        self.assertEqual(wake_command('Hey Rocky!'), '')
        self.assertIsNone(wake_command('Someone said Hey Rocky'))

    def run_capture(self, *, active=False, pause_during_decode=False, whisper=False, verify=False,
                    prefixed_command=False, decode_error=False, pause_on_you=False,
                    resume_after_pause=False):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'model.bin').touch()
            assistant = Mock()
            assistant.respond.return_value = 'Opened.'
            worker = VoiceWorker(queue.Queue(), Path(folder), assistant=assistant,
                                 stt='auto' if whisper else 'vosk')
            worker.verify_wake = verify
            if active:
                worker.conversation.renew()
            original_emit = worker.emit
            paused = False
            typed_queued = False

            def emit(kind, value):
                nonlocal paused, typed_queued
                original_emit(kind, value)
                if pause_on_you and kind == 'You' and not paused:
                    paused = True
                    worker.set_listening(False)
                    if resume_after_pause:
                        worker.set_listening(True)
                if paused and not typed_queued and kind == 'status' and value in {
                        'Microphone paused', 'Say "Hey Rocky" to start', 'Listening — you can keep talking'}:
                    worker.messages.put_nowait('typed request')
                    typed_queued = True
                if kind in {'Rocky', 'error'} or (kind == 'status' and value == 'Microphone paused'
                                                and not pause_on_you):
                    worker.stopping.set()

            worker.emit = emit
            wake = Mock()
            wake.AcceptWaveform.return_value = True
            wake.Result.return_value = json.dumps({'text': 'hey rocky [unk]', 'result': [
                {'word': 'hey', 'conf': .95}, {'word': 'rocky', 'conf': .95},
                {'word': '[unk]', 'conf': .1}]})
            decoder = Mock()

            def decode(pcm):
                if pause_during_decode and not whisper:
                    worker.set_listening(False)
                return True

            decoder.AcceptWaveform.side_effect = decode
            decoder.Result.return_value = json.dumps({
                'text': 'hey rocky open whatsapp' if not active or prefixed_command else 'open whatsapp'})
            decoder.FinalResult.return_value = json.dumps({'text': ''})
            vosk = types.SimpleNamespace(Model=Mock(), KaldiRecognizer=Mock(
                side_effect=lambda *args: wake if len(args) == 3 else decoder))
            def transcribe(pcm):
                if decode_error:
                    raise RuntimeError('Temporary decoder failure')
                if pause_during_decode:
                    worker.set_listening(False)
                return 'Hey Rocky, open WhatsApp'
            transcriber = Mock()
            transcriber.transcribe.side_effect = transcribe

            class Stream:
                def __init__(self, **options):
                    self.callback = options['callback']
                def __enter__(self):
                    self.callback(bytes(960), 480, None, None)
                    return self
                def __exit__(self, *args):
                    return False

            audio = types.SimpleNamespace(query_devices=Mock(return_value={'name': 'Test'}), RawInputStream=Stream)
            with patch.dict(sys.modules, {'sounddevice': audio, 'pythoncom': Mock(), 'winsound': Mock(),
                                         'vosk': vosk, 'webrtcvad': Mock()}), \
                 patch('rocky.speech.Speech'), \
                 patch('rocky.voice.WHISPER_MODEL', Path(folder)), \
                 patch('rocky.voice.Transcriber', return_value=transcriber):
                worker.start()
                try:
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive(), 'Capture did not complete')
                finally:
                    worker.stopping.set()
                    worker.join(timeout=1)
            return assistant, list(worker.events.queue)

    def test_one_breath_wake_and_command_uses_full_vosk_decoder(self):
        assistant, events = self.run_capture()
        assistant.respond.assert_called_once_with('open whatsapp')
        self.assertIn(('You', 'open whatsapp'), events)

    def test_one_breath_wake_and_command_uses_verified_whisper_transcript(self):
        assistant, events = self.run_capture(whisper=True, verify=True)
        assistant.respond.assert_called_once_with('open WhatsApp')

    def test_wake_prefix_is_removed_during_follow_up_listening_too(self):
        assistant, events = self.run_capture(active=True, prefixed_command=True)
        assistant.respond.assert_called_once_with('open whatsapp')

    def test_pause_during_vosk_decoding_discards_command(self):
        assistant, events = self.run_capture(active=True, pause_during_decode=True)
        assistant.respond.assert_not_called()
        self.assertFalse(any(kind == 'You' for kind, text in events))

    def test_pause_during_wake_verification_discards_command(self):
        assistant, events = self.run_capture(whisper=True, verify=True, pause_during_decode=True)
        assistant.respond.assert_not_called()
        self.assertFalse(any(kind == 'You' for kind, text in events))

    def test_failed_wake_verification_does_not_disable_microphone(self):
        assistant, events = self.run_capture(whisper=True, verify=True, decode_error=True)
        assistant.respond.assert_not_called()
        self.assertNotIn(('microphone', False), events)
        self.assertTrue(any(kind == 'error' and 'Transcription failed' in text for kind, text in events))

    def test_pause_at_final_dispatch_discards_voice_but_accepts_typed_request(self):
        assistant, events = self.run_capture(active=True, pause_on_you=True)
        assistant.respond.assert_called_once_with('typed request')
        self.assertIn(('microphone', False), events)

    def test_pause_and_resume_at_final_dispatch_does_not_revive_old_command(self):
        assistant, events = self.run_capture(active=True, pause_on_you=True, resume_after_pause=True)
        assistant.respond.assert_called_once_with('typed request')
        self.assertIn(('microphone', False), events)
        self.assertIn(('microphone', True), events)
