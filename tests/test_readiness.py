import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from rocky.readiness import check_audio, check_ollama, check_setup


class ReadinessTests(unittest.TestCase):
    def model_response(self, payload):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
        return patch('rocky.readiness.urllib.request.build_opener', return_value=opener)

    def test_installed_and_missing_models(self):
        with self.model_response({'models': [{'name': 'qwen3:4b'}]}):
            self.assertIn('installed', check_ollama('qwen3:4b'))
        with self.model_response({'models': [{'name': 'qwen3:latest'}]}):
            self.assertIn('installed', check_ollama('qwen3'))
        with self.model_response({'models': []}):
            self.assertIn('ollama pull qwen3:4b', check_ollama('qwen3:4b'))

    def test_unreachable_invalid_and_offline_states(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError('refused')
        with patch('rocky.readiness.urllib.request.build_opener', return_value=opener):
            self.assertIn('unreachable', check_ollama('qwen3:4b'))
        with self.model_response({'models': None}):
            self.assertIn('unexpected', check_ollama('qwen3:4b'))
        with patch('rocky.readiness.urllib.request.build_opener') as factory:
            self.assertIn('disabled', check_ollama('qwen3:4b', offline=True))
            factory.assert_not_called()

    def test_audio_checks_never_record_or_play(self):
        audio = Mock()
        audio.query_devices.return_value = {'name': 'Test headset', 'default_samplerate': 48000}
        with patch.dict('sys.modules', {'sounddevice': audio}):
            self.assertIn('supports', check_audio('input', 3))
            self.assertIn('supports', check_audio('output', 4))
            audio.check_input_settings.assert_called_once_with(device=3, samplerate=16000, channels=1, dtype='int16')
            audio.check_output_settings.assert_called_once_with(device=4, samplerate=48000, channels=1, dtype='int16')
            audio.RawInputStream.assert_not_called()
            audio.RawOutputStream.assert_not_called()
            audio.query_devices.side_effect = RuntimeError('device removed')
            self.assertIn('unavailable', check_audio('input', 3))

    def test_missing_files_and_both_vosk_graph_formats(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            voice = root / 'voice.onnx'
            with patch('rocky.readiness.WHISPER_MODEL', root / 'whisper'), \
                 patch('rocky.readiness.VOICE', voice), patch('rocky.readiness.VOICE_DIR', root), \
                 patch('rocky.readiness.check_ollama', return_value='Test'), \
                 patch('rocky.readiness.check_audio', return_value='Test'):
                result = dict(check_setup('qwen3:4b', root))
                self.assertIn('missing', result['Wake-word files'])
                self.assertIn('missing', result['Speech recognition'])
                self.assertIn('missing', result['Neural voice files'])
                for name in ('am/final.mdl', 'conf/model.conf', 'graph/HCLG.fst'):
                    file = root / name
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.touch()
                self.assertIn('present', dict(check_setup('qwen3:4b', root))['Wake-word files'])
                (root / 'graph/HCLG.fst').unlink()
                (root / 'graph/HCLr.fst').touch()
                (root / 'graph/Gr.fst').touch()
                self.assertIn('present', dict(check_setup('qwen3:4b', root))['Wake-word files'])
