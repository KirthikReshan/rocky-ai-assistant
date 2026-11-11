"""Confidence-gated phrase detection, shared by wake tests and live listening."""

import json
import re

SENSITIVITIES = {'Relaxed': .35, 'Balanced': .55, 'Strict': .8}
STOP_PHRASES = {'rocky stop', 'hey rocky stop', 'stop speaking', 'stop talking',
                'rocky stop speaking', 'hey rocky stop speaking', 'rocky stop talking',
                'wait', 'rocky wait', 'hey rocky wait', 'change that', 'rocky change that'}


def is_stop_phrase(text):
    value = ' '.join(re.findall(r'[a-z]+', text.casefold()))
    return value in STOP_PHRASES


def wake_command(text):
    """Return the words after an anchored wake phrase, or None if it is absent."""
    match = re.match(r'^\s*hey[\s,.!?-]+rocky\b[\s,.;:!?-]*(.*)$', text,
                     flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


class PhraseDetector:
    def __init__(self, model, phrases=('hey rocky',), threshold=.55, partial=False, prefix=False):
        from vosk import KaldiRecognizer

        self.phrases = set(phrases)
        self.threshold = threshold
        self.recognizer = KaldiRecognizer(model, 16000, json.dumps([*sorted(self.phrases), '[unk]']))
        self.recognizer.SetWords(True)
        self.partial = partial
        self.prefix = prefix
        self.last_partial = ''
        self.partial_matches = 0
        if partial:
            self.recognizer.SetPartialWords(True)
        self.audio = bytearray()
        self.confidence = 0.0
        self.matched_phrase = ''
        self.has_suffix = False

    def accept(self, result):
        payload = json.loads(result)
        phrase = payload.get('text', '').strip().casefold()
        matched = next((item for item in sorted(self.phrases, key=len, reverse=True)
                        if phrase == item or self.prefix and phrase.startswith(item + ' ')), None)
        if matched is None:
            return False
        self.matched_phrase = matched
        self.has_suffix = phrase != matched
        confidences = [word.get('conf', 0) for word in payload.get('result', [])]
        if self.has_suffix:
            # The constrained wake grammar represents command words as [unk].
            # Its low confidence must not discard a clearly spoken wake prefix.
            confidences = confidences[:len(matched.split())]
            if len(confidences) < len(matched.split()):
                return False
        self.confidence = min(confidences) if confidences else 0.0
        return self.confidence >= self.threshold

    def feed(self, pcm):
        self.audio.extend(pcm)
        # A wake plus command can be much longer than the wake phrase alone.
        del self.audio[:-(960000 if self.prefix else 96000)]
        if self.recognizer.AcceptWaveform(pcm):
            self.partial_matches = 0
            accepted = self.accept(self.recognizer.Result())
            if self.prefix and not accepted:
                # Keep a little pre-roll, not previous conversations, for the
                # full-vocabulary transcription of a subsequent wake + command.
                del self.audio[:-9600]
            return accepted
        if self.partial:
            payload = json.loads(self.recognizer.PartialResult())
            phrase = payload.get('partial', '')
            accepted = self.accept(json.dumps({'text': phrase, 'result': payload.get('partial_result', [])}))
            self.partial_matches = self.partial_matches + 1 if accepted and phrase == self.last_partial else int(accepted)
            self.last_partial = phrase
            return self.partial_matches >= 2
        return False

    def finish(self):
        return self.accept(self.recognizer.FinalResult())


def evaluate_wake(pcm, model, threshold=.55, verifier=None):
    detector = PhraseDetector(model, threshold=threshold, prefix=True)
    for offset in range(0, len(pcm), 960):
        if detector.feed(pcm[offset:offset + 960]):
            accepted = verifier is None or wake_command(verifier.transcribe(bytes(detector.audio))) is not None
            if accepted:
                return True
    if detector.finish():
        return verifier is None or wake_command(verifier.transcribe(bytes(detector.audio))) is not None
    return False
