import unittest
import numpy as np
from rocky.playback import shorten_long_silences


class PlaybackTests(unittest.TestCase):
    def test_long_silence_is_shortened_without_losing_voice(self):
        tone = np.full(16000, 2000, dtype=np.int16)
        clip = np.concatenate((tone, np.zeros(16000, dtype=np.int16), tone))
        result = shorten_long_silences(clip, 16000)
        self.assertEqual(len(result), 39200)
        self.assertTrue(np.array_equal(result[:16000], tone))
        self.assertTrue(np.array_equal(result[-16000:], tone))

    def test_short_pause_is_kept(self):
        clip = np.concatenate((np.full(16000, 1000, dtype=np.int16), np.zeros(4800, dtype=np.int16)))
        self.assertTrue(np.array_equal(clip, shorten_long_silences(clip, 16000)))

    def test_quiet_speech_is_kept(self):
        clip = np.full(32000, 100, dtype=np.int16)
        self.assertTrue(np.array_equal(clip, shorten_long_silences(clip, 16000)))

    def test_int16_minimum_is_not_mistaken_for_silence(self):
        clip = np.full(32000, -32768, dtype=np.int16)
        self.assertTrue(np.array_equal(clip, shorten_long_silences(clip, 16000)))
