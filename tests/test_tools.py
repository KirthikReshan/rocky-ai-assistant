import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rocky.assistant import Assistant
from rocky.tools import Action, DesktopTools, folder_path, parse_action, website_url
from rocky.tools import app_path
import os


class ToolTests(unittest.TestCase):
    def test_local_executable_cannot_shadow_windows_calculator(self):
        before = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            (folder / 'calc.exe').write_bytes(b'inert test fixture - never execute')
            try:
                os.chdir(folder)
                result = Path(app_path('calc.exe'))
                self.assertTrue(result.is_absolute())
                self.assertNotEqual(result.parent, folder)
                import win32api
                self.assertEqual(result, (Path(win32api.GetSystemDirectory()) / 'calc.exe').resolve())
            finally:
                os.chdir(before)

    def test_unregistered_local_chrome_on_path_is_not_launched(self):
        before = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            (folder / 'chrome.exe').write_bytes(b'inert')
            try:
                os.chdir(folder)
                with patch('winreg.OpenKey', side_effect=FileNotFoundError), \
                     patch.dict(os.environ, {'PATH': str(folder), 'LOCALAPPDATA': str(folder),
                                             'ProgramFiles': str(folder), 'ProgramFiles(x86)': str(folder)}):
                    with self.assertRaises(FileNotFoundError):
                        app_path('chrome.exe')
            finally:
                os.chdir(before)

    def test_common_voice_commands(self):
        cases = {
            'Please open calculator.': Action('open', 'calculator'),
            'Can you search Google for Python lists?': Action('google', 'Python lists'),
            'Search for cats on Google': Action('google', 'cats'),
            'Play relaxing music on YouTube': Action('youtube_play', 'relaxing music'),
            'Play song Imagine': Action('youtube_play', 'Imagine'),
            'Search YouTube for Python lessons': Action('youtube_search', 'Python lessons'),
            'Can you find in YouTube how to make Maggie?': Action('youtube_search', 'how to make Maggie'),
            'Search for C++ lessons on YouTube': Action('youtube_search', 'C++ lessons'),
            'Hey Rocky, open downloads': Action('open', 'downloads'),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_action(text), expected)
        self.assertIsNone(parse_action('How do I open calculator in Python?'))

    def test_sensitive_actions_are_blocked(self):
        tools = DesktopTools()
        for text in ('close chrome', 'shutdown', 'shut down Windows', 'delete my code'):
            with patch('rocky.tools.subprocess.Popen') as launch:
                self.assertIn('confirmation', tools.execute(parse_action(text)))
                launch.assert_not_called()

    def test_search_encodes_query(self):
        tools = DesktopTools()
        tools.browser = Mock()
        tools.execute(Action('google', 'cats & dogs #1'))
        tools.browser.assert_called_once_with('https://www.google.com/search?q=cats+%26+dogs+%231')

    def test_url_protocols_and_credentials(self):
        self.assertEqual(website_url('example.com/path'), 'https://example.com/path')
        for url in ('file:///C:/Windows', 'javascript:alert(1)', 'https://user:pass@example.com',
                    'https://example.com\nother', 'https://example.com:wrong'):
            with self.assertRaises(ValueError):
                website_url(url)

    def test_app_launch_is_allowlisted_without_shell(self):
        with patch('rocky.tools.app_path', return_value='C:/Windows/notepad.exe'), \
             patch('rocky.tools.subprocess.Popen') as launch:
            result = DesktopTools().execute(Action('open', 'notepad'))
            self.assertIn('Opening', result)
            launch.assert_called_once_with(['C:/Windows/notepad.exe'], shell=False)

    def test_arbitrary_executable_is_not_launched(self):
        with patch('rocky.tools.subprocess.Popen') as launch:
            self.assertIn("couldn't", DesktopTools().execute(Action('open', 'powershell -Command shutdown')))
            launch.assert_not_called()

    def test_folder_must_be_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(folder_path(directory), Path(directory).resolve())
            file = Path(directory) / 'example.txt'
            file.touch()
            with self.assertRaises(FileNotFoundError):
                folder_path(str(file))

    def test_youtube_opens_watch_page_not_search(self):
        tools = DesktopTools()
        tools.youtube_result = Mock(return_value={'id': 'abcdefghijk', 'title': 'Example'})
        tools.browser = Mock()
        self.assertIn('Example', tools.execute(Action('youtube_play', 'example music')))
        tools.browser.assert_called_once_with('https://www.youtube.com/watch?v=abcdefghijk&autoplay=1')

    def test_youtube_result_id_is_validated(self):
        with patch('rocky.tools.subprocess.run', return_value=Mock(returncode=0, stdout='{"id":"file:///bad"}')):
            with self.assertRaises(ValueError):
                DesktopTools().youtube_result('example')

    def test_playback_lookup_failure_opens_search_results(self):
        tools = DesktopTools()
        for failure in (subprocess.TimeoutExpired('lookup', 30), OSError('worker failed')):
            with self.subTest(failure=failure):
                tools.youtube_result = Mock(side_effect=failure)
                tools.browser = Mock()
                response = tools.execute(Action('youtube_play', 'how to make Mysore pak'))
                self.assertIn("couldn't pick a video automatically", response)
                tools.browser.assert_called_once_with('https://www.youtube.com/results?search_query=how+to+make+Mysore+pak')

    def test_tool_command_bypasses_brain(self):
        brain, tools = Mock(), Mock()
        tools.execute.return_value = 'Opening calculator.'
        self.assertEqual(Assistant(brain, tools).respond('open calculator'), 'Opening calculator.')
        brain.chat.assert_not_called()

    def test_brain_output_cannot_execute_actions(self):
        brain, tools = Mock(), Mock()
        brain.chat.return_value = 'open calculator'
        self.assertEqual(Assistant(brain, tools).respond('Tell me something'), 'open calculator')
        tools.execute.assert_not_called()
