from pathlib import Path
import tempfile
import unittest

from rocky.audio_preferences import device_key, resolve_device, speech_pause
from rocky.storage import Store


class AudioPreferenceTests(unittest.TestCase):
    def test_reordered_devices_restore_by_name_and_host(self):
        hosts = [{'name': 'Windows WASAPI'}, {'name': 'MME'}]
        headset = {'name': 'Headset', 'hostapi': 0, 'max_input_channels': 1, 'max_output_channels': 0}
        other = dict(headset, hostapi=1)
        preference = device_key(headset, hosts)
        self.assertEqual(resolve_device(preference, [other, headset], hosts, 'input'), 1)
        self.assertIsNone(resolve_device(preference, [other], hosts, 'input'))
        self.assertIsNone(resolve_device(preference, [headset, headset], hosts, 'input'))
        self.assertIsNone(resolve_device(preference, [headset], hosts, 'output'))

    def test_settings_survive_reopening_and_can_follow_windows_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rocky.db'
            store = Store(path)
            self.assertIsNone(store.setting('microphone'))
            store.set_setting('microphone', {'name': 'Headset', 'hostapi': 'MME'})
            store.set_setting('speech_pause', 2.0)
            reopened = Store(path)
            self.assertEqual(reopened.setting('microphone')['name'], 'Headset')
            self.assertEqual(reopened.setting('speech_pause'), 2.0)
            reopened.set_setting('microphone', None)
            self.assertIsNone(Store(path).setting('microphone'))

    def test_invalid_saved_pause_uses_default(self):
        for value in (None, '2.0', 0, 100, float('nan'), True):
            self.assertEqual(speech_pause(value), 1.5)
        self.assertEqual(speech_pause(2.5), 2.5)
