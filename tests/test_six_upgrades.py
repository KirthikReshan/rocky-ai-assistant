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

from rocky.assistant import Assistant
from rocky.brain import OllamaBrain
from rocky.coding import Workspace, CodeCommands
from rocky.storage import Store
from rocky.voice_benchmark import command_score
from rocky.voice_interrupt import VoiceInterrupt
from rocky.wake import PhraseDetector


def reply(answer):
    return io.BytesIO(json.dumps({'message': {'content': json.dumps({'reply': answer})}}).encode())


class MemoryTests(unittest.TestCase):
    def test_memory_is_opt_in_and_clear_cannot_be_undone_by_inflight_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'rocky.db')
            brain = OllamaBrain(memory_store=store)
            brain.http.open = Mock(return_value=reply('Hi there'))
            Assistant(brain).respond('How are you')
            self.assertIsNone(store.setting('chat_history'))
            brain.set_memory_enabled(True)
            reloaded = OllamaBrain(memory_store=store)
            self.assertEqual(len(reloaded.history), 2)
            def complete(*args, **kwargs):
                brain.clear_history()
                return reply('This was in flight')
            brain.http.open = Mock(side_effect=complete)
            Assistant(brain).respond('Another question')
            self.assertEqual(brain.history, [])
            self.assertEqual(store.setting('chat_history'), [])
            brain.set_memory_enabled(False)
            self.assertFalse(OllamaBrain(memory_store=store).memory_enabled)

    def test_only_corrected_tool_claim_is_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'rocky.db')
            store.set_setting('remember_chats', True)
            brain = OllamaBrain(memory_store=store)
            brain.http.open = Mock(return_value=reply("I've saved your reminder."))
            Assistant(brain).respond('Are you ready')
            saved = store.setting('chat_history')
            self.assertIn('No reminder was saved', saved[-1]['content'])
            self.assertNotIn("I've saved", saved[-1]['content'])

    def test_disable_during_request_preserves_previous_session_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'rocky.db')
            store.set_settings({'remember_chats': True, 'chat_history': [
                {'role':'user','content':'Old question'}, {'role':'assistant','content':'Old answer'}]})
            brain = OllamaBrain(memory_store=store)
            def complete(*args, **kwargs):
                brain.set_memory_enabled(False)
                return reply('New answer')
            brain.http.open = Mock(side_effect=complete)
            Assistant(brain).respond('New question')
            self.assertEqual(brain.history[-1]['content'], 'Old answer')
            self.assertEqual(store.setting('chat_history'), [])


class VoiceUpgradeTests(unittest.TestCase):
    def test_command_scoring_recognizes_equivalent_actions_without_side_effects(self):
        with patch('rocky.tools.subprocess.Popen') as launch:
            self.assertEqual(command_score('Open VS Code', 'Could you bring up my code editor'), 1)
            self.assertEqual(command_score('Open WhatsApp', 'open calculator'), 0)
            self.assertEqual(command_score('Remind me in two minutes to stretch',
                                            'remain me in 2 minutes to stretch'), 1)
            self.assertIsNone(command_score('Tell me about Python', 'Hello'))
            launch.assert_not_called()

    def test_wake_confidence_gates_activation(self):
        recognizer = Mock()
        recognizer.AcceptWaveform.return_value = True
        recognizer.Result.return_value = json.dumps({'text':'hey rocky','result':[{'conf':.4},{'conf':.9}]})
        with patch.dict(sys.modules, {'vosk': types.SimpleNamespace(KaldiRecognizer=Mock(return_value=recognizer))}):
            self.assertFalse(PhraseDetector(Mock(), threshold=.55).feed(bytes(960)))
            self.assertTrue(PhraseDetector(Mock(), threshold=.35).feed(bytes(960)))

    def test_voice_stop_filters_outgoing_words_and_honors_mute(self):
        callback = Mock()
        listener = VoiceInterrupt(Mock(), Mock(), None, lambda: True, callback, Mock())
        listener.set_reference('You can say Rocky stop to interrupt me.')
        self.assertTrue(listener.echo_expected())
        listener.reference = ''
        listener.previous = ('', 0)
        self.assertFalse(listener.echo_expected())
        listener.allowed = lambda: False
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        listener.sd.RawInputStream.return_value = stream
        with patch('rocky.voice_interrupt.PhraseDetector'):
            listener.start()
            self.assertTrue(listener.close())
        callback.assert_not_called()

    def test_voice_stop_detects_phrase_and_releases_stream(self):
        stopped = threading.Event()
        listener = VoiceInterrupt(Mock(), Mock(), None, lambda: True, stopped.set, Mock())
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        listener.sd.RawInputStream.return_value = stream
        listener.audio.put(bytes(3200))
        with patch('rocky.voice_interrupt.PhraseDetector') as detector:
            detector.return_value.feed.return_value = True
            listener.start()
            self.assertTrue(stopped.wait(2))
            self.assertTrue(listener.close())
        stream.__exit__.assert_called_once()


class ProjectIndexTests(unittest.TestCase):
    def test_index_finds_cross_file_evidence_and_refreshes_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'main.py').write_text('from helpers import calculate_total\nprint(calculate_total(2))\n')
            (root/'helpers.py').write_text('def calculate_total(value):\n    return value + 1\n')
            (root/'secrets.json').write_text('{"secret":"do not index"}')
            workspace = Workspace()
            workspace.select(root)
            brain = Mock()
            brain.code_chat.return_value = 'Explanation'
            coding = CodeCommands(workspace, brain)
            coding.handle('explain file main.py')
            context = brain.code_chat.call_args.args[1]
            self.assertIn('FILE helpers.py', context)
            coding.handle('ask my code: how is calculate_total implemented?')
            self.assertIn('return value + 1', brain.code_chat.call_args.args[1])
            self.assertNotIn('secrets.json', coding.index.documents)
            (root/'helpers.py').write_text('def calculate_total(value):\n    return value + 99\n')
            coding.handle('ask my code: explain calculate_total')
            self.assertIn('return value + 99', brain.code_chat.call_args.args[1])
            self.assertEqual(len(brain.code_chat.call_args.args[1]) < 10000, True)
            self.assertEqual((root/'main.py').read_text(), 'from helpers import calculate_total\nprint(calculate_total(2))\n')
