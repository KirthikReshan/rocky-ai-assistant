import unittest
from rocky.assistant import Assistant
from rocky.core import is_wake, reply


class CoreTests(unittest.TestCase):
    def test_wake_requires_entire_phrase(self):
        self.assertTrue(is_wake("Hey Rocky!"))
        for text in ("rocky", "hey", "hey rocky mountain", "play hey rocky"):
            self.assertFalse(is_wake(text))

    def test_actions_are_not_executed(self):
        for text in ("shutdown", "close chrome", "edit my code", "google cats"):
            self.assertIn("later milestone", reply(text))

    def test_casual_greeting_handles_typo_without_calling_the_brain(self):
        self.assertEqual(Assistant(brain=object()).respond("hey waht up!!"), "Hey! How's it going?")

    def test_common_time_questions_use_the_local_clock_without_the_brain(self):
        for text in ("What's the time now?", "What is the time?", "What’s the time?", "What time is it?"):
            with self.subTest(text=text):
                self.assertRegex(Assistant(brain=object()).respond(text), r'^It is \d{2}:\d{2} [AP]M\.$')


if __name__ == "__main__":
    unittest.main()
