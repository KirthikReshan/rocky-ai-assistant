import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rocky.assistant import Assistant
from rocky.brain import OllamaBrain
from rocky.coding import Workspace, CodeCommands


class CodingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = Workspace()
        self.workspace.select(self.root)
        (self.root / 'main.py').write_text('def add(a, b):\n    return a + b\n', encoding='utf-8')
        self.project = self.workspace.snapshot()

    def test_only_selected_project_is_read(self):
        text = self.project.read('main.py')
        self.assertIn('1: def add', text)
        for name in ('../outside.py', str(self.root / 'main.py'), 'main.py:alternate'):
            with self.assertRaises(ValueError):
                self.project.read(name)

    def test_hidden_and_known_secret_files_are_excluded(self):
        for name in ('.env', 'secrets.json', 'private.key'):
            (self.root / name).write_text('private', encoding='utf-8')
            with self.assertRaises(ValueError):
                self.project.read(name)
        names, _ = self.project.files()
        self.assertEqual(names, ['main.py'])

    def test_ignored_directories_are_not_searched(self):
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules/bad.py').write_text('secret', encoding='utf-8')
        matches, _ = self.project.search('secret')
        self.assertEqual(matches, [])

    def test_binary_and_large_files_are_rejected(self):
        for name, data in (('binary.py', b'\x00abc'), ('large.py', b'x' * 100001)):
            (self.root / name).write_bytes(data)
            with self.assertRaises(ValueError):
                self.project.read(name)

    def test_links_are_rejected_before_reading(self):
        with patch('rocky.coding.linked', return_value=True):
            with self.assertRaises(ValueError):
                self.project.read('main.py')

    def test_read_is_bounded_and_marked_truncated(self):
        (self.root / 'long.py').write_text('a = 1\n' * 2000, encoding='utf-8')
        self.assertIn('[TRUNCATED]', self.project.read('long.py', limit=100))

    def test_search_returns_file_and_line(self):
        found, _ = self.project.search('return')
        self.assertEqual(found, ['main.py:2:     return a + b'])

    def test_no_selection_requests_folder(self):
        self.workspace.clear()
        self.assertIn('Choose a code folder', CodeCommands(self.workspace).handle('read file main.py'))

    def test_explanation_uses_only_code_provider_and_does_not_modify_file(self):
        brain = Mock()
        brain.code_chat.return_value = 'main.py:2 adds two values.'
        original = (self.root / 'main.py').read_bytes()
        coding = CodeCommands(self.workspace, brain)
        assistant = Assistant(brain, coding=coding)
        self.assertIn('adds', assistant.respond('explain file main.py'))
        brain.chat.assert_not_called()
        self.assertIn('2:     return', brain.code_chat.call_args.args[1])
        self.assertEqual((self.root / 'main.py').read_bytes(), original)

    def test_general_conversation_does_not_scan_files(self):
        with patch.object(self.workspace, 'snapshot') as scan:
            self.assertIsNone(CodeCommands(self.workspace).handle('how are you'))
            scan.assert_not_called()

    def test_code_context_does_not_enter_chat_history(self):
        brain = OllamaBrain()
        brain.http.open = Mock(return_value=io.BytesIO(json.dumps({'message': {
            'content': json.dumps({'reply': 'Check main.py:2.'})}}).encode()))
        self.assertEqual(brain.code_chat('explain', 'main.py:2 return a + b'), 'Check main.py:2.')
        self.assertEqual(brain.history, [])

    def test_code_output_is_displayed_not_read_aloud(self):
        display = Mock()
        coding = CodeCommands(self.workspace, display=display)
        self.assertIn('shown in the window', coding.handle('read file main.py'))
        self.assertIn('1: def add', display.call_args.args[0])

    def test_traceback_selects_project_file_context(self):
        brain = Mock()
        brain.code_chat.return_value = 'Check main.py.'
        coding = CodeCommands(self.workspace, brain)
        coding.handle(f'explain error: File "{self.root / "main.py"}", line 2: TypeError')
        self.assertIn('FILE main.py', brain.code_chat.call_args.args[1])

    def test_external_traceback_does_not_read_other_files(self):
        brain = Mock()
        brain.code_chat.return_value = 'No project context.'
        coding = CodeCommands(self.workspace, brain)
        coding.handle('explain error: File "C:/external/private.py", line 2: TypeError')
        self.assertIn('No relevant file context', brain.code_chat.call_args.args[1])

    def test_reported_voice_phrases_route_to_selected_project(self):
        for text in ('Please see through the code is selected before that.',
                     'You can go through all the codes, folders in the Raki A.',
                     'Could you look through the selected folder?',
                     'Review my code for bugs'):
            brain = Mock()
            brain.code_chat.return_value = 'Project overview.'
            display = Mock()
            assistant = Assistant(brain, coding=CodeCommands(self.workspace, brain, display))
            result = assistant.respond(text)
            self.assertIn('selected project', result)
            brain.chat.assert_not_called()
            self.assertIn(str(self.workspace.root), brain.code_chat.call_args.args[1])
            self.assertIn('main.py', brain.code_chat.call_args.args[1])
            self.assertEqual(self.workspace.root, self.root.resolve())

    def test_natural_request_without_selection_does_not_claim_access(self):
        self.workspace.clear()
        brain = Mock()
        response = CodeCommands(self.workspace, brain).handle('go through my code')
        self.assertIn('Choose a code folder', response)
        brain.code_chat.assert_not_called()

    def test_selection_question_uses_actual_state(self):
        coding = CodeCommands(self.workspace)
        self.assertIn(str(self.workspace.root), coding.handle('can you access my code?'))
        self.workspace.clear()
        self.assertIn('Choose a code folder', coding.handle('can you access my code?'))

    def test_general_chat_receives_selected_folder_state_without_source(self):
        brain = Mock()
        brain.chat.return_value = 'Hello.'
        assistant = Assistant(brain, coding=CodeCommands(self.workspace, brain))
        assistant.respond('Are you ready?')
        state = brain.chat.call_args.kwargs['app_context']
        self.assertIn('selected code project', state)
        self.assertNotIn('return a + b', state)

    def test_negated_request_does_not_scan(self):
        coding = CodeCommands(self.workspace)
        with patch.object(self.workspace, 'snapshot') as scan:
            self.assertIsNone(coding.handle("Please don't go through my code"))
            scan.assert_not_called()
