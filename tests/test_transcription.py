import unittest
from rocky.transcription import Utterance, suspicious_transcript


class UtteranceTests(unittest.TestCase):
    def test_looping_transcript_is_rejected(self):
        self.assertTrue(suspicious_transcript('Say about yourself. ' + 'Say about yourself Hati. ' * 25))
        self.assertFalse(suspicious_transcript('Tell me about yourself, Rocky.'))
        self.assertFalse(suspicious_transcript('Very very good. Can you explain that again?'))

    def test_speech_and_short_pause_do_not_cut_off(self):
        recording = Utterance()
        for _ in range(8):
            self.assertIsNone(recording.feed(b"quiet", False))
        for _ in range(80):
            self.assertIsNone(recording.feed(b"word", True))
        for _ in range(30):  # 0.9 second pause inside a sentence
            self.assertIsNone(recording.feed(b"pause", False))
        self.assertIsNone(recording.feed(b"last-word", True))
        for _ in range(49):
            self.assertIsNone(recording.feed(b"silence", False))
        result = recording.feed(b"silence", False)
        self.assertIn(b"last-word", result)
        self.assertTrue(result.startswith(b"quiet"))

    def test_background_silence_does_not_start_turn(self):
        recording = Utterance()
        for _ in range(2000):
            self.assertIsNone(recording.feed(b"silence", False))
        self.assertFalse(recording.started)
        self.assertEqual(recording.frames, [])

    def test_quiet_opening_words_are_kept_before_vad_triggers(self):
        recording = Utterance(silence_seconds=.15)
        recording.feed(b'quiet-opening-word', False)
        for _ in range(14):
            recording.feed(b'quiet', False)
        for _ in range(3):
            recording.feed(b'clear-speech', True)
        for _ in range(4):
            self.assertIsNone(recording.feed(b'silence', False))
        self.assertTrue(recording.feed(b'silence', False).startswith(b'quiet-opening-word'))

    def test_single_noise_frame_does_not_start_turn(self):
        recording = Utterance()
        recording.feed(b"noise", True)
        for _ in range(10):
            recording.feed(b"silence", False)
        self.assertFalse(recording.started)

    def test_continuous_speech_has_size_limit(self):
        recording = Utterance(max_seconds=1)
        for _ in range(32):
            self.assertIsNone(recording.feed(b"x", True))
        self.assertIsNotNone(recording.feed(b"x", True))
