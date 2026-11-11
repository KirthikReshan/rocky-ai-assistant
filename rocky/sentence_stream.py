"""Release stable complete sentences, then reconcile with the final answer."""

import re


class SentenceStream:
    def __init__(self, speak):
        self.speak = speak
        self.consumed = 0
        self.spoken = []
        self.previous = ''
        self.revised = False

    def feed(self, draft):
        if self.revised or not draft:
            return
        if not draft.startswith(self.previous):
            self.revised = True
            return
        self.previous = draft
        base = self.consumed
        for boundary in re.finditer(r'[.!?]["\u201d\u2019\x27)]*\s+(?=\S)', draft[base:]):
            end = base + boundary.end()
            sentence = draft[self.consumed:end].strip()
            if re.fullmatch(r'\d+\.', sentence):
                continue
            if re.search(r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc)\.$', sentence, re.I):
                continue
            self.spoken.append(sentence)
            self.speak(sentence)
            self.consumed = end

    def remaining(self, final):
        return remaining_reply(final, self.spoken)


def remaining_reply(final, spoken):
    prefix = ' '.join(' '.join(spoken).split())
    normalized = ' '.join(final.split())
    if not prefix:
        return final
    if normalized.startswith(prefix):
        return normalized[len(prefix):].strip()
    return final
