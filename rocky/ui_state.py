"""Microphone control state: a click requests what the displayed label says."""
class MicrophoneControl:
    def __init__(self, worker):
        self.worker = worker
        self.active = worker.enabled.is_set()

    @property
    def label(self):
        return 'Pause microphone' if self.active else 'Resume microphone'

    def update(self, enabled):
        self.active = bool(enabled)

    def toggle(self):
        # Use displayed state, not a worker Event that may have changed before UI polling.
        self.active = not self.active
        self.worker.set_listening(self.active)
