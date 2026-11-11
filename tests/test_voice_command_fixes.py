import json
from pathlib import Path
import queue
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from rocky.tools import DesktopTools, parse_action
from rocky.voice import VoiceWorker
from rocky.voice_benchmark import command_score
from rocky.voice_interrupt import VoiceInterrupt
from rocky.wake import PhraseDetector, STOP_PHRASES, is_stop_phrase


class VoiceCommandFixTests(unittest.TestCase):
    def test_spoken_whatsapp_variants_resolve_to_installed_app(self):
        shell = Mock()
        shell.NameSpace.return_value.Items.return_value = [
            types.SimpleNamespace(Name='WhatsApp', Path='WhatsAppDesktop!App')]
        with patch('win32com.client.Dispatch', return_value=shell), \
             patch('rocky.tools.app_path', return_value='C:/Windows/explorer.exe'), \
             patch('rocky.tools.subprocess.Popen') as launch:
            for command in ('open whatapp', 'open what app', "open what's app", 'open the whats app'):
                self.assertEqual(DesktopTools().execute(parse_action(command)), 'Opening WhatsApp.')
                self.assertEqual(launch.call_args.args[0][-1], 'shell:AppsFolder\\WhatsAppDesktop!App')
                self.assertEqual(command_score('Open WhatsApp', command), 1)

    def test_partial_stop_requires_two_confident_observations(self):
        recognizer = Mock()
        recognizer.AcceptWaveform.return_value = False
        recognizer.PartialResult.return_value = json.dumps({
            'partial':'stop speaking', 'partial_result':[{'conf':.95},{'conf':.95}]})
        with patch.dict(sys.modules, {'vosk': types.SimpleNamespace(KaldiRecognizer=Mock(return_value=recognizer))}):
            detector = PhraseDetector(Mock(), STOP_PHRASES, .8, partial=True)
            self.assertFalse(detector.feed(bytes(3200)))
            self.assertTrue(detector.feed(bytes(3200)))
        recognizer.SetPartialWords.assert_called_once_with(True)

    def test_echo_filter_matches_only_the_recognized_stop_phrase(self):
        listener = VoiceInterrupt(Mock(), Mock(), None, lambda: True, Mock(), Mock())
        listener.set_reference('Click Stop speaking to interrupt.')
        self.assertTrue(listener.echo_expected('stop speaking'))
        self.assertFalse(listener.echo_expected('rocky stop'))
        for phrase in ('Stop speaking!', 'STOP TALKING', 'Rocky, stop.'):
            self.assertTrue(is_stop_phrase(phrase))
        self.assertFalse(is_stop_phrase('Explain how to stop speaking'))

    def test_stop_received_after_playback_does_not_start_a_new_ai_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            assistant = Mock()
            assistant.respond.return_value = 'Hello'
            worker = VoiceWorker(queue.Queue(), Path(folder)/'missing', assistant=assistant, stt='vosk')
            worker.messages.put('stop speaking')
            worker.messages.put('hello')
            original = worker.emit
            def emit(kind, value):
                original(kind, value)
                if kind == 'speaking' and value is False:
                    worker.stopping.set()
            worker.emit = emit
            audio = types.SimpleNamespace(query_devices=Mock(return_value={'name':'Test'}))
            with patch.dict(sys.modules, {'sounddevice':audio,'pythoncom':Mock(),'winsound':Mock(),
                                         'vosk':types.SimpleNamespace(Model=Mock(),KaldiRecognizer=Mock())}), \
                 patch('rocky.speech.Speech'):
                worker.run()
            assistant.respond.assert_called_once_with('hello')
