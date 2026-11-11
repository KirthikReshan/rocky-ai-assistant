import json
from pathlib import Path
import queue
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from rocky.storage import Store
from rocky.tools import Action, DesktopTools
from rocky.voice_benchmark import PHRASES, command_score
from rocky.voice_setup import VoiceSetup
from scripts.windows_app import prepare_windowed_streams, youtube_worker


class DesktopAccuracyTests(unittest.TestCase):
    def test_conversational_app_wrappers_use_the_known_launcher(self):
        for phrase in ('open the calculator', 'open my calculator app', 'open calculator application'):
            with self.subTest(phrase=phrase), patch('rocky.tools.app_path', return_value='C:/Windows/calc.exe'), \
                    patch('rocky.tools.subprocess.Popen') as launch:
                from rocky.tools import parse_action
                self.assertIn('Opening', DesktopTools().execute(parse_action(phrase)))
                launch.assert_called_once_with(['C:/Windows/calc.exe'], shell=False)
                self.assertEqual(command_score('open calculator', phrase), 1)
        tools = DesktopTools()
        tools.browser = Mock()
        self.assertIn('Opening', tools.execute(Action('open', 'my browser')))
        tools.browser.assert_called_once_with('https://www.google.com')

    def test_app_wrapper_cleanup_does_not_rewrite_unknown_names_or_paths(self):
        from rocky.tools import open_target
        for target in ('The Example App', r'D:\My Browser App', 'My Custom Application'):
            self.assertEqual(open_target(target), target)

    def test_windowed_argparse_help_has_safe_output_streams(self):
        import argparse
        import sys

        with patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
            prepare_windowed_streams()
            output, errors = sys.stdout, sys.stderr
            try:
                with self.assertRaises(SystemExit) as exited:
                    argparse.ArgumentParser().parse_args(['--help'])
                self.assertEqual(exited.exception.code, 0)
            finally:
                output.close()
                errors.close()

    def test_quoted_dotted_folder_is_opened_in_explorer(self):
        with tempfile.TemporaryDirectory(suffix='.project') as folder, \
                patch('rocky.tools.subprocess.Popen') as launch, \
                patch('rocky.tools.app_path', return_value='C:/Windows/explorer.exe'):
            tools = DesktopTools()
            tools.browser = Mock()
            self.assertIn('Opening', tools.execute(Action('open', f'"{folder}"')))
            launch.assert_called_once_with(['C:/Windows/explorer.exe', str(Path(folder).resolve())], shell=False)
            tools.browser.assert_not_called()

    def test_unc_path_is_not_treated_as_a_website(self):
        with patch('rocky.tools.folder_path', return_value=Path(r'\\server\my.folder')), \
                patch('rocky.tools.app_path', return_value='C:/Windows/explorer.exe'), \
                patch('rocky.tools.subprocess.Popen') as launch:
            tools = DesktopTools()
            tools.browser = Mock()
            self.assertIn('Opening', tools.execute(Action('open', r'\\server\my.folder')))
            launch.assert_called_once()
            tools.browser.assert_not_called()

    def test_installed_app_with_dot_and_spaces_is_not_a_domain(self):
        with patch('rocky.tools.installed_app_id', return_value='Example!App') as lookup, \
                patch('rocky.tools.app_path', return_value='C:/Windows/explorer.exe'), \
                patch('rocky.tools.subprocess.Popen') as launch:
            tools = DesktopTools()
            tools.browser = Mock()
            self.assertIn('Opening', tools.execute(Action('open', 'My .NET App')))
            lookup.assert_called_once_with('My .NET App')
            launch.assert_called_once()
            tools.browser.assert_not_called()

    def test_command_score_matches_equivalent_open_actions(self):
        with patch('rocky.tools.subprocess.Popen') as launch, patch('rocky.tools.webbrowser.open') as browser:
            for first, second in [('open Downloads', 'open my downloads folder'),
                                  ('open Google', 'open https://www.google.com/'),
                                  ('open VS Code', 'open "VS Code"'),
                                  ('open Codex', 'open OpenAI Codex')]:
                with self.subTest(first=first):
                    self.assertEqual(command_score(first, second), 1)
            self.assertEqual(command_score('open Downloads', 'open Documents'), 0)
            self.assertEqual(command_score('open https://example.com/Profile', 'open https://example.com/profile'), 0)
            self.assertEqual(command_score('open https://example.com/?q=a/', 'open https://example.com/?q=a'), 0)
            self.assertEqual(command_score(r'open D:\foo-bar', r'open D:\foo bar'), 0)
            self.assertEqual(command_score('Search Google for C++', 'Search Google for C#'), 0)
            self.assertEqual(command_score('open WhatsApp', 'open wahtapp', {'whatsapp': 123}), 1)
            launch.assert_not_called()
            browser.assert_not_called()

    def test_malformed_youtube_result_falls_back_to_search(self):
        for result in ([], {'id': None}, {'id': 123}):
            with self.subTest(result=result), patch('rocky.tools.subprocess.run', return_value=Mock(
                    returncode=0, stdout=json.dumps(result))):
                tools = DesktopTools()
                tools.browser = Mock()
                self.assertIn('Opening YouTube results', tools.execute(Action('youtube_play', 'music')))
                tools.browser.assert_called_once_with('https://www.youtube.com/results?search_query=music')

    def test_frozen_youtube_worker_failure_returns_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as folder, patch('rocky.youtube_search.search', side_effect=OSError('offline')):
            result = Path(folder) / 'result.json'
            self.assertEqual(youtube_worker(result, 'music'), 1)
            self.assertFalse(result.exists())


class VoiceSetupAccuracyTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory()
        self.worker = Mock(tests=queue.Queue(maxsize=1))
        self.setup = VoiceSetup(self.root, self.worker, Store(Path(self.directory.name) / 'rocky.db'))

    def tearDown(self):
        self.root.destroy()
        self.directory.cleanup()

    def test_next_phrase_advances_past_repeated_wake_samples(self):
        seen = []
        for _ in PHRASES:
            seen.append(self.setup.prompt.get())
            self.setup.next_phrase()
        self.assertEqual(seen, list(PHRASES))
        self.assertEqual(self.setup.prompt.get(), PHRASES[0])

    def test_invalid_reference_cannot_create_a_perfect_score(self):
        for phrase in ('...', 'x' * 501):
            self.setup.prompt.set(phrase)
            self.setup.start(True)
            self.assertFalse(self.setup.busy)
            self.assertTrue(self.worker.tests.empty())

    def test_benchmark_controls_cannot_change_the_spoken_prompt_mid_test(self):
        self.setup.start(True)
        self.assertTrue(self.setup.busy)
        for control in (self.setup.phrases, self.setup.mode, self.setup.room):
            self.assertTrue(control.instate(['disabled']))
        self.setup.handle('TestError', 'Microphone disconnected')
        for control in (self.setup.phrases, self.setup.mode, self.setup.room):
            self.assertFalse(control.instate(['disabled']))
        self.assertTrue(self.setup.mode.instate(['readonly']))


if __name__ == '__main__':
    unittest.main()
