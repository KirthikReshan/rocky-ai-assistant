import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
import sys
from rocky.file_lock import write_exact
from unittest.mock import Mock, patch

from rocky.coding import Workspace, CodeCommands
from rocky.editing import EditService, replacement


class EditingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / 'main.py'
        self.original = b'def add(a, b):\n    return a - b\n'
        self.source.write_bytes(self.original)
        self.workspace = Workspace()
        self.workspace.select(self.root)
        self.brain = Mock()
        self.brain.propose_edit.return_value = {'summary': 'Correct addition.', 'edits': [
            {'old': 'return a - b', 'new': 'return a + b'}]}
        self.backups = self.root / 'backups'
        self.now = [1000.0]
        self.editor = EditService(self.workspace, self.brain, self.backups, clock=lambda: self.now[0])

    def propose(self):
        return self.editor.propose('main.py', 'Use addition instead of subtraction')

    def test_proposal_does_not_write_source_or_backup(self):
        proposal = self.propose()
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(self.backups.exists())
        self.assertIn('-    return a - b', proposal.diff)
        self.assertIn('+    return a + b', proposal.diff)

    def test_explicit_approval_applies_exact_bytes_and_saves_backup(self):
        proposal = self.propose()
        backup = self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), proposal.updated)
        self.assertEqual((backup / 'original.bin').read_bytes(), self.original)
        manifest = json.loads((backup / 'manifest.json').read_text())
        self.assertEqual(manifest['original_sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(manifest['path'], str(self.source))
        with self.assertRaises(ValueError):
            self.editor.apply(proposal.id)

    def test_no_proposal_or_wrong_id_cannot_authorize_write(self):
        with self.assertRaises(ValueError):
            self.editor.apply('anything')
        proposal = self.propose()
        with self.assertRaises(ValueError):
            self.editor.apply('not-the-preview-id')
        self.assertEqual(self.source.read_bytes(), self.original)
        self.editor.apply(proposal.id)

    def test_rejected_proposal_cannot_be_applied(self):
        proposal = self.propose()
        self.editor.discard(proposal.id)
        with self.assertRaises(ValueError):
            self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_new_request_invalidates_old_approval(self):
        first = self.propose()
        second = self.propose()
        with self.assertRaises(ValueError):
            self.editor.apply(first.id)
        self.editor.apply(second.id)

    def test_restart_does_not_preserve_approval_authority(self):
        proposal = self.propose()
        restarted = EditService(self.workspace, self.brain, self.backups)
        with self.assertRaises(ValueError):
            restarted.apply(proposal.id)

    def test_expired_proposal_cannot_be_applied(self):
        proposal = self.propose()
        self.now[0] += 601
        with self.assertRaises(ValueError):
            self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_modified_source_is_not_overwritten(self):
        proposal = self.propose()
        self.source.write_bytes(b'# external edit\n')
        with self.assertRaises(ValueError):
            self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), b'# external edit\n')
        self.assertFalse(self.backups.exists())

    def test_clear_or_reselect_invalidates_approval(self):
        proposal = self.propose()
        self.workspace.select(self.root)  # even choosing the same folder again invalidates it
        with self.assertRaises(ValueError):
            self.editor.apply(proposal.id)
        proposal = self.propose()
        self.workspace.clear()
        with self.assertRaises(ValueError):
            self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_model_cannot_choose_another_target(self):
        self.brain.propose_edit.return_value['path'] = '../outside.py'
        with self.assertRaises(ValueError):
            self.propose()
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_path_escape_and_secret_targets_are_rejected(self):
        for name in ('../main.py', '.env', str(self.source)):
            with self.assertRaises((ValueError, OSError)):
                self.editor.propose(name, 'change it')
        self.brain.propose_edit.assert_not_called()

    def test_source_too_large_is_not_truncated_for_proposal(self):
        self.source.write_bytes(b'#' * 12001)
        with self.assertRaises(ValueError):
            self.propose()
        self.brain.propose_edit.assert_not_called()

    def test_ambiguous_or_overlapping_replacements_rejected(self):
        with self.assertRaises(ValueError):
            replacement(b'x = 1\nx = 1\n', [{'old': 'x = 1', 'new': 'x = 2'}], '.py')
        with self.assertRaises(ValueError):
            replacement(self.original, [{'old': 'return a - b', 'new': 'return a + b'},
                                        {'old': 'a - b', 'new': 'a * b'}], '.py')

    def test_invalid_python_and_json_are_rejected(self):
        with self.assertRaises(ValueError):
            replacement(self.original, [{'old': 'return a - b', 'new': 'return ('}], '.py')
        with self.assertRaises(ValueError):
            replacement(b'{"value": 1}', [{'old': '1', 'new': 'not-json'}], '.json')

    def test_bom_and_windows_line_endings_are_preserved(self):
        source = b'\xef\xbb\xbfx = 1\r\n'
        result = replacement(source, [{'old': 'x = 1', 'new': 'x = 2'}], '.py')
        self.assertEqual(result, b'\xef\xbb\xbfx = 2\r\n')

    def test_missing_final_newline_is_visible_in_preview(self):
        self.source.write_bytes(b'def add(a, b):\n    return a - b')
        self.assertIn('No newline at end of file', self.propose().diff)

    def test_backup_failure_leaves_source_unchanged(self):
        proposal = self.propose()
        with patch('rocky.editing.Path.mkdir', side_effect=PermissionError('backup unavailable')):
            with self.assertRaises(OSError):
                self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_write_failure_restores_original_and_keeps_backup(self):
        proposal = self.propose()
        writes = []
        def partial_failure(stream, data):
            writes.append(data)
            if len(writes) == 1:
                stream.seek(0)
                stream.write(b'# partial damaged write')
                stream.flush()
                raise OSError('simulated write failure')
            write_exact(stream, data)
        with patch('rocky.editing.write_exact', side_effect=partial_failure):
            with self.assertRaises(OSError):
                self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual((self.backups / proposal.id / 'original.bin').read_bytes(), self.original)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_concurrent_write_and_replace_are_blocked_during_commit(self):
        proposal = self.propose()
        other = self.root / 'replacement.tmp'
        other.write_bytes(b'# concurrent replacement\n')
        def competing_editor(stream, data):
            with self.assertRaises(OSError):
                self.source.write_bytes(b'# concurrent edit\n')
            with self.assertRaises(OSError):
                os.replace(other, self.source)
            write_exact(stream, data)
        with patch('rocky.editing.write_exact', side_effect=competing_editor):
            self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), proposal.updated)
        self.assertEqual(other.read_bytes(), b'# concurrent replacement\n')

    def test_voice_or_text_approval_never_applies(self):
        coding = CodeCommands(self.workspace, self.brain, editor=self.editor)
        proposal = self.propose()
        for command in ('approve edit', 'apply edit', 'yes apply it'):
            self.assertIn('cannot approve', coding.handle(command))
        self.assertEqual(self.source.read_bytes(), self.original)
        self.editor.apply(proposal.id)

    def test_edit_command_only_emits_preview(self):
        preview = Mock()
        coding = CodeCommands(self.workspace, self.brain, editor=self.editor, preview=preview)
        self.assertIn('No source file has been changed', coding.handle('edit file main.py: fix addition'))
        preview.assert_called_once()
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_replaced_file_with_same_content_is_stale(self):
        proposal = self.propose()
        replacement_file = self.root / 'replacement.tmp'
        replacement_file.write_bytes(self.original)
        os.replace(replacement_file, self.source)
        with self.assertRaises(ValueError):
            self.editor.apply(proposal.id)

    def test_selection_changed_during_generation_discards_result(self):
        response = self.brain.propose_edit.return_value
        def change_selection(*args):
            self.workspace.clear()
            return response
        self.brain.propose_edit.side_effect = change_selection
        with self.assertRaises(ValueError):
            self.propose()
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_read_only_source_is_rejected(self):
        self.source.chmod(0o444)
        try:
            with self.assertRaises(ValueError):
                self.propose()
        finally:
            self.source.chmod(0o666)

    def test_hard_linked_source_is_rejected(self):
        os.link(self.source, self.root / 'alias.py')
        with self.assertRaises(ValueError):
            self.propose()

    def test_no_net_change_is_rejected(self):
        self.brain.propose_edit.return_value = {'summary': 'No useful change', 'edits': [
            {'old': 'return a - b', 'new': 'return a - b'}]}
        with self.assertRaises(ValueError):
            self.propose()

    def test_contextually_invalid_python_is_rejected(self):
        for bad in ('return 1', 'break', 'continue', 'await example()', 'nonlocal missing'):
            with self.assertRaises(ValueError, msg=bad):
                replacement(b'value = 1\n', [{'old': 'value = 1', 'new': bad}], '.py')

    def test_compilation_does_not_execute_proposed_code(self):
        result = replacement(b'value = 1\n', [{'old': 'value = 1',
                             'new': "raise RuntimeError('must never execute during validation')"}], '.py')
        self.assertIn(b'raise RuntimeError', result)

    def test_existing_open_writer_blocks_approval(self):
        proposal = self.propose()
        with self.source.open('r+b'):
            with self.assertRaises(OSError):
                self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(self.backups.exists())

    def test_separate_process_cannot_write_during_commit(self):
        proposal = self.propose()
        def competing_process(stream, data):
            script = ("from pathlib import Path; import sys\n"
                      "try: Path(sys.argv[1]).write_bytes(b'concurrent change')\n"
                      "except PermissionError: sys.exit(0)\n"
                      "else: sys.exit(2)\n")
            result = subprocess.run([sys.executable, '-c', script, str(self.source)], timeout=5,
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            write_exact(stream, data)
        with patch('rocky.editing.write_exact', side_effect=competing_process):
            self.editor.apply(proposal.id)
        self.assertEqual(self.source.read_bytes(), proposal.updated)

    def test_failed_rollback_reports_durable_backup_location(self):
        proposal = self.propose()
        with patch('rocky.editing.write_exact', side_effect=OSError('persistent I/O failure')):
            with self.assertRaisesRegex(OSError, 'Recover original bytes'):
                self.editor.apply(proposal.id)
        self.assertEqual((self.backups / proposal.id / 'original.bin').read_bytes(), self.original)

    def test_successful_edit_preserves_file_identity(self):
        proposal = self.propose()
        before = self.source.stat().st_ino
        self.editor.apply(proposal.id)
        self.assertEqual(self.source.stat().st_ino, before)
