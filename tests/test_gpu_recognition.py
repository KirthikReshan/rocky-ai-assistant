from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from rocky.storage import Store
from rocky.transcription import Transcriber


class GPURecognitionTests(unittest.TestCase):
    def test_cpu_does_not_load_gpu_libraries(self):
        with patch('rocky.transcription.prepare_cuda') as prepare, \
             patch('faster_whisper.WhisperModel') as model:
            transcriber = Transcriber()
        prepare.assert_not_called()
        self.assertEqual(transcriber.engine, 'CPU')
        self.assertEqual(model.call_args.kwargs['compute_type'], 'int8')

    def test_missing_gpu_library_falls_back_to_cpu(self):
        with patch('rocky.transcription.prepare_cuda', side_effect=OSError('missing CUDA')), \
             patch('faster_whisper.WhisperModel') as model:
            transcriber = Transcriber(device='cuda')
        self.assertEqual(transcriber.engine, 'CPU')
        self.assertEqual(model.call_args.kwargs['device'], 'cpu')

    def test_gpu_model_load_failure_falls_back_to_cpu(self):
        with patch('rocky.transcription.prepare_cuda'), \
             patch('faster_whisper.WhisperModel', side_effect=[RuntimeError('out of memory'), Mock()]) as model:
            transcriber = Transcriber(device='cuda')
        self.assertEqual(transcriber.engine, 'CPU')
        self.assertEqual([call.kwargs['device'] for call in model.call_args_list], ['cuda', 'cpu'])

    def test_lazy_gpu_failure_retries_audio_once_without_partial_words(self):
        segment = types.SimpleNamespace(text='open calculator', avg_logprob=-.1,
                                        no_speech_prob=.1, compression_ratio=1.1)
        def partial():
            yield types.SimpleNamespace(text='wrong', avg_logprob=-.1, no_speech_prob=.1, compression_ratio=1.1)
            raise RuntimeError('GPU disappeared')
        gpu, cpu = Mock(), Mock()
        gpu.transcribe.return_value = (partial(), None)
        cpu.transcribe.return_value = ([segment], None)
        with patch('rocky.transcription.prepare_cuda'), \
             patch('faster_whisper.WhisperModel', side_effect=[gpu, cpu]):
            transcriber = Transcriber(device='cuda')
            self.assertEqual(transcriber.transcribe(bytes(3200)), 'open calculator')
        self.assertEqual(transcriber.engine, 'CPU')
        self.assertEqual(gpu.transcribe.call_count, 1)
        self.assertEqual(cpu.transcribe.call_count, 1)

    def test_existing_benchmark_database_migrates_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rocky.db'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE voice_tests (id INTEGER PRIMARY KEY, created_at REAL, expected TEXT, '
                       'heard TEXT, word_errors INTEGER, reference_words INTEGER, exact INTEGER, latency REAL, '
                       'environment TEXT, device TEXT, overflow INTEGER)')
            db.execute("INSERT INTO voice_tests VALUES (1,0,'hello','hello',0,1,1,.4,'Quiet room','Mic',0)")
            db.commit()
            db.close()
            self.assertEqual(Store(path).voice_tests()[0]['engine'], 'CPU (legacy)')
            self.assertEqual(Store(path).voice_tests()[0]['heard'], 'hello')
