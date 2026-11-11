import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from rocky.playback import play_pcm
from rocky.speech import Speech
from rocky.voice import VoiceWorker
import queue


class SpeechCancellationTests(unittest.TestCase):
    def test_playback_aborts_after_stop_and_does_not_write_remaining_audio(self):
        stop = threading.Event()
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.write.side_effect = lambda data: stop.set() or False
        audio = types.SimpleNamespace(query_devices=Mock(return_value={'default_samplerate': 16000}),
                                      RawOutputStream=Mock(return_value=stream))
        with patch.dict(sys.modules, {'sounddevice': audio}):
            play_pcm(np.full(32000, 1000, dtype=np.int16).tobytes(), 16000, Mock(), cancelled=stop.is_set)
        self.assertEqual(stream.write.call_count, 1)
        stream.abort.assert_called_once()

    def test_cancelled_playback_does_not_open_speaker(self):
        audio = Mock()
        with patch.dict(sys.modules, {'sounddevice': audio}):
            play_pcm(bytes(3200), 16000, Mock(), cancelled=lambda: True)
        audio.RawOutputStream.assert_not_called()

    def test_cancel_during_output_setup_does_not_open_speaker(self):
        stop = threading.Event()
        def query(*args):
            stop.set()
            return {'default_samplerate': 16000}
        audio = types.SimpleNamespace(query_devices=Mock(side_effect=query), RawOutputStream=Mock())
        with patch.dict(sys.modules, {'sounddevice': audio}):
            play_pcm(bytes(3200), 16000, Mock(), cancelled=stop.is_set)
        audio.RawOutputStream.assert_not_called()

    def test_cancel_during_last_write_aborts_buffered_audio(self):
        stop = threading.Event()
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        writes = []
        def write(data):
            writes.append(data)
            if len(writes) == 2:
                stop.set()
            return False
        stream.write.side_effect = write
        audio = types.SimpleNamespace(query_devices=Mock(return_value={'default_samplerate': 16000}),
                                      RawOutputStream=Mock(return_value=stream))
        # 50 ms speech + 150 ms pre-roll is exactly two output writes.
        with patch.dict(sys.modules, {'sounddevice': audio}):
            play_pcm(np.full(800, 1000, dtype=np.int16).tobytes(), 16000, Mock(), cancelled=stop.is_set)
        self.assertEqual(len(writes), 2)
        stream.abort.assert_called_once()

    def test_sapi_purges_pending_speech_on_stop(self):
        speech = Speech.__new__(Speech)
        speech.voice = None
        speech.report = Mock()
        speech.sapi = Mock()
        speech.sapi.WaitUntilDone.return_value = False
        stop = threading.Event()
        speech.sapi.WaitUntilDone.side_effect = lambda timeout: stop.set() or False
        speech.speak('Long answer', cancelled=stop.is_set)
        self.assertEqual(speech.sapi.Speak.call_args_list[-1].args, ('', 3))
        speech.sapi.WaitUntilDone.assert_called_once_with(50)

    def test_piper_cancellation_does_not_fall_back_to_sapi(self):
        speech = Speech.__new__(Speech)
        speech.voice = Mock()
        speech.voice.config.sample_rate = 16000
        stop = threading.Event()

        def generate(*args, **kwargs):
            stop.set()
            yield types.SimpleNamespace(audio_int16_bytes=bytes(3200))

        speech.voice.synthesize.side_effect = generate
        speech.config = None
        speech.report = Mock()
        speech.sapi = Mock()
        with patch('rocky.playback.play_pcm') as playback:
            speech.speak('Answer', cancelled=stop.is_set)
        playback.assert_not_called()
        speech.sapi.Speak.assert_not_called()
        self.assertIsNotNone(speech.voice)

    def test_idle_stop_does_not_cancel_next_reply(self):
        worker = VoiceWorker(queue.Queue(), Path('missing'))
        worker.stop_speaking()
        self.assertFalse(worker.speech_cancelled.is_set())
        worker.speaking.set()
        worker.stop_speaking()
        self.assertTrue(worker.speech_cancelled.is_set())
