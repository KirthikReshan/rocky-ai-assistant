import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from rocky.brain import BrainError, OllamaBrain, validated_history
from rocky.coding import CodeCommands, Workspace
from rocky.editing import EditService, replacement


class EditIntegrityTests(unittest.TestCase):
    def test_non_utf8_python_declaration_is_rejected_before_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = b'# coding: latin-1\nname = "a"\n'
            (root / 'main.py').write_bytes(original)
            workspace = Workspace()
            workspace.select(root)
            brain = Mock()
            editor = EditService(workspace, brain, root / 'backups')
            with self.assertRaisesRegex(ValueError, 'UTF-8'):
                editor.propose('main.py', 'Change the name to cafe')
            brain.propose_edit.assert_not_called()
            self.assertEqual((root / 'main.py').read_bytes(), original)

    def test_replacement_cannot_change_the_encoding_to_non_utf8(self):
        original = b'# coding: utf-8\nname = "a"\n'
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            replacement(original, [{'old': 'utf-8', 'new': 'latin-1'},
                                   {'old': '"a"', 'new': '"caf\u00e9"'}], '.py')

    def test_utf8_bytes_are_validated_with_their_declared_encoding(self):
        original = b'# coding: utf-8\nname = "a"\n'
        updated = replacement(original, [{'old': '"a"', 'new': '"caf\u00e9"'}], '.py')
        self.assertEqual(ast.parse(updated).body[0].value.value, 'caf\u00e9')

    def test_nonfinite_numbers_are_rejected_as_invalid_json(self):
        for value in ('NaN', 'Infinity', '-Infinity'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replacement(b'{"value": 1}', [{'old': '1', 'new': value}], '.json')

    def test_edit_instructions_retain_requested_punctuation(self):
        for instruction in ('change the greeting to Hello!', 'append a question mark?',
                            'end the greeting with a period.'):
            with self.subTest(instruction=instruction):
                editor = Mock()
                editor.propose.return_value.relative = 'main.py'
                coding = CodeCommands(Workspace(), editor=editor)
                coding.handle('edit file main.py: ' + instruction)
                editor.propose.assert_called_once_with('main.py', instruction)

    def test_invalid_unicode_summary_cannot_reach_the_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = b'value = 1\n'
            (root / 'main.py').write_bytes(original)
            workspace = Workspace()
            workspace.select(root)
            brain = Mock()
            brain.propose_edit.return_value = {'summary': 'Bad\ud800summary',
                                              'edits': [{'old': '1', 'new': '2'}]}
            editor = EditService(workspace, brain, root / 'backups')
            preview = Mock()
            response = CodeCommands(workspace, brain, editor=editor, preview=preview).handle(
                'edit file main.py: change the value')
            self.assertIn('could not prepare', response)
            preview.assert_not_called()
            self.assertEqual((root / 'main.py').read_bytes(), original)
            self.assertFalse((root / 'backups').exists())


class MemoryIntegrityTests(unittest.TestCase):
    def test_malformed_saved_messages_are_rejected_before_ui_or_model_use(self):
        for invalid in ({'role': 'assistant', 'content': 'out of order'},
                        {'role': 'user', 'content': 42},
                        {'role': 'user', 'content': 'Bad\udc00text'}):
            with self.subTest(invalid=repr(invalid)):
                saved = [invalid, {'role': 'assistant', 'content': 'Hello'}]
                with self.assertRaises(ValueError):
                    validated_history(saved)
                store = Mock()
                store.setting.side_effect = lambda name, default: True if name == 'remember_chats' else saved
                notice = Mock()
                brain = OllamaBrain(memory_store=store, notice=notice)
                self.assertEqual(brain.history_snapshot(), [])
                notice.assert_called_once()

    def test_restored_history_is_bounded_and_copied(self):
        saved = [{'role': role, 'content': str(index)} for index in range(20)
                 for role in ('user', 'assistant')]
        restored = validated_history(saved)
        self.assertEqual(len(restored), 8)
        self.assertEqual(restored[0]['content'], '16')
        restored[0]['content'] = 'Changed'
        self.assertEqual(saved[-8]['content'], '16')

    def test_invalid_input_text_does_not_enter_memory(self):
        brain = OllamaBrain()
        brain.http.open = Mock()
        with self.assertRaises(BrainError):
            brain.chat('Bad\ud800message')
        brain.http.open.assert_not_called()
        self.assertEqual(brain.history, [])
