import unittest
from rocky.conversation import Conversation, wants_to_end
from rocky.brain import clean_answer


class ConversationTests(unittest.TestCase):
    def test_follow_up_expiry_and_renewal(self):
        now = [100.0]
        session = Conversation(30, lambda: now[0])
        self.assertFalse(session.active)
        session.renew()
        now[0] = 129
        self.assertTrue(session.active)
        session.renew()
        now[0] = 131
        self.assertTrue(session.active)
        now[0] = 160
        self.assertFalse(session.active)
        session.renew()
        session.end()
        self.assertFalse(session.active)

    def test_end_phrases(self):
        self.assertTrue(wants_to_end('Stop listening!'))
        self.assertFalse(wants_to_end('How do I stop listening on Windows?'))

    def test_reasoning_never_reaches_voice(self):
        for content in ('<think>private</think> Hello', 'private</think>Hello'):
            self.assertEqual(clean_answer(content), 'Hello')
        self.assertEqual(clean_answer('<think>unfinished'), '')
