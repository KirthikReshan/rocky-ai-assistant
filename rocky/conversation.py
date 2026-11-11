"""Audio-independent conversation state with idle expiry."""
import time


class Conversation:
    def __init__(self, idle_seconds=30, clock=time.monotonic):
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.deadline = 0.0

    @property
    def active(self):
        return self.clock() < self.deadline

    def renew(self):
        self.deadline = self.clock() + self.idle_seconds

    def end(self):
        self.deadline = 0.0


def wants_to_end(text):
    return text.lower().strip().rstrip(".!?") in {
        "goodbye", "bye rocky", "stop listening", "go to sleep", "that's all", "that is all"
    }
