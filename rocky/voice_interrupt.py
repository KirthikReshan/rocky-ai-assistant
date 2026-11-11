"""Opt-in headset voice interruption, with a filter for Rocky's own words."""

import queue
import re
import threading
import time

from rocky.wake import PhraseDetector, STOP_PHRASES


def normalized(text):
    return ' '.join(re.findall(r'[a-z]+', text.casefold()))


class VoiceInterrupt(threading.Thread):
    def __init__(self, sd, model, device, allowed, interrupt, report, on_command=None):
        super().__init__(name='rocky-voice-stop', daemon=True)
        self.sd, self.model, self.device = sd, model, device
        self.allowed, self.interrupt, self.report = allowed, interrupt, report
        self.on_command = on_command
        self.closed = threading.Event()
        self.audio = queue.Queue(maxsize=20)
        self.lost = threading.Event()
        self.lock = threading.Lock()
        self.reference = ''
        self.previous = ('', 0.0)

    def set_reference(self, text):
        with self.lock:
            self.previous = (self.reference, time.monotonic() + .8)
            self.reference = normalized(text)

    def echo_expected(self, phrase=None):
        with self.lock:
            references = [self.reference]
            if self.previous[1] > time.monotonic():
                references.append(self.previous[0])
        phrases = {phrase} if isinstance(phrase, str) and (phrase in STOP_PHRASES or phrase == 'hey rocky') else STOP_PHRASES
        return any(f' {candidate} ' in f' {reference} ' for reference in references for candidate in phrases)

    def run(self):
        def capture(data, frames, timing, status):
            if status:
                self.lost.set()
            try:
                self.audio.put_nowait(bytes(data))
            except queue.Full:
                self.lost.set()

        try:
            if self.closed.is_set() or not self.allowed():
                return
            detector = PhraseDetector(self.model, STOP_PHRASES, threshold=.8, partial=True)
            wake = PhraseDetector(self.model, threshold=.8, prefix=True) if self.on_command is not None else None
            if self.closed.is_set() or not self.allowed():
                return
            with self.sd.RawInputStream(device=self.device, samplerate=16000, channels=1,
                                        dtype='int16', blocksize=1600, callback=capture):
                while not self.closed.is_set() and self.allowed():
                    if self.lost.is_set():
                        while not self.audio.empty():
                            try:
                                self.audio.get_nowait()
                            except queue.Empty:
                                break
                        detector = PhraseDetector(self.model, STOP_PHRASES, threshold=.8, partial=True)
                        wake = PhraseDetector(self.model, threshold=.8, prefix=True) if self.on_command is not None else None
                        self.lost.clear()
                    try:
                        pcm = self.audio.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if (detector.feed(pcm) and not self.closed.is_set() and self.allowed()
                            and not self.echo_expected(detector.matched_phrase)):
                        self.interrupt()
                        return
                    if (wake is not None and wake.feed(pcm) and not self.closed.is_set() and self.allowed()
                            and not self.echo_expected('hey rocky')):
                        self.interrupt()
                        self.on_command(bytes(wake.audio))
                        return
        except Exception as exc:
            self.report('info', f'Voice stop is unavailable for this reply. Use Stop speaking. {exc}')

    def close(self):
        self.closed.set()
        self.join(timeout=2)
        return not self.is_alive()
