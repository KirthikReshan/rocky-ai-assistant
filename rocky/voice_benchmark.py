"""Local microphone measurements and transcript scoring."""

import re
import ntpath
from urllib.parse import urlsplit
from datetime import datetime, timezone


PHRASES = (
    'Open WhatsApp', 'Open VS Code', 'Open Codex', 'Open calculator',
    'Open Downloads', 'Search Google for Python tutorials',
    'Play relaxing music on YouTube', 'Remind me in two minutes to drink water',
    'Remind me tomorrow at five PM to call Mom', 'Show my reminders',
    'Save a note that my meeting is tomorrow', 'Show my notes',
    'Hey Rocky', 'Hey Rocky', 'Hey Rocky',
    'What time is it', 'Explain Python lists in simple words',
    'How is your day going', 'Tell me about yourself', 'Goodbye',
)


def words(text):
    return re.findall(r"[a-z0-9]+", text.casefold())


def score(expected, actual):
    reference, heard = words(expected), words(actual)
    previous = list(range(len(heard) + 1))
    for row, word in enumerate(reference, 1):
        current = [row]
        for column, candidate in enumerate(heard, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (word != candidate)))
        previous = current
    return {'word_errors': previous[-1], 'reference_words': len(reference),
            'exact': reference == heard}


def command_signature(text, now, nicknames=None):
    """Describe a command without creating reminders or opening any application."""
    from rocky.tools import APPS, SITES, parse_action, open_target, folder_alias, is_website_target, website_url
    from rocky.core import is_time_request, is_date_request
    from rocky.reminders import NUMBERS, normalize_reminder_request, recurring_request, split_reminder, parse_due

    text = re.sub(r'^(?:please |can you |could you )', '', text.strip(), flags=re.I).rstrip('.!?')
    action = parse_action(text)
    if action is not None and action.kind != 'blocked':
        target = action.target.casefold()
        if action.kind == 'open':
            raw_target = open_target(action.target, nicknames)
            target = raw_target.casefold()
            if target in SITES or target == 'browser' or is_website_target(target):
                try:
                    parts = urlsplit(website_url(SITES.get(target, SITES['google'] if target == 'browser' else raw_target)))
                    return 'open_url', parts.scheme, parts.netloc.casefold(), parts.path or '/', parts.query, parts.fragment
                except ValueError:
                    return None
            if target in APPS:
                return 'open_app', APPS[target].casefold()
            if folder_alias(target) is not None:
                return 'open_folder', folder_alias(target)
            if ntpath.isabs(raw_target):
                return 'open_folder', ntpath.normcase(ntpath.normpath(raw_target))
            if target in {'codex', 'openai codex'}:
                target = 'codex'
            return 'open_installed_app', re.sub(r'\s+', ' ', target.strip())
        # Query punctuation can change the requested result (C++ versus C#).
        return action.kind, re.sub(r'\s+', ' ', target.strip())
    raw = normalize_reminder_request(text)
    repeat, raw = recurring_request(raw)
    if raw.casefold().startswith('remind me '):
        try:
            phrase, body = split_reminder(raw)
            if re.fullmatch(r'(?:\d+|' + '|'.join(NUMBERS) + r') (?:seconds?|minutes?|hours?|days?|weeks?)', phrase, re.I):
                phrase = 'in ' + phrase
            return 'reminder', parse_due(phrase, now=now).timestamp(), ' '.join(words(body)), repeat
        except ValueError:
            return None
    note = re.fullmatch(r'(?:take (?:a )?note|save (?:a )?note|note)(?: that)?[ :]+(.+)', raw, re.I)
    if note:
        return 'note', ' '.join(words(note[1]))
    fixed = {'show my reminders': 'reminders', 'show reminders': 'reminders', 'list reminders': 'reminders',
             'show my notes': 'notes', 'show notes': 'notes', 'list notes': 'notes'}
    if is_time_request(text):
        return ('builtin', 'time')
    if is_date_request(text):
        return ('builtin', 'date')
    if raw.casefold() in fixed:
        return ('builtin', fixed[raw.casefold()])
    return None


def command_score(expected, actual, nicknames=None):
    now = datetime.now(timezone.utc)
    reference = command_signature(expected, now, nicknames)
    if reference is None:
        return None
    return int(reference == command_signature(actual, now, nicknames))


def levels(pcm):
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if not len(samples):
        return {'peak': 0.0, 'rms': 0.0, 'clipped': 0.0}
    return {'peak': float(np.max(np.abs(samples))),
            'rms': float(np.sqrt(np.mean(samples ** 2))),
            'clipped': float(np.mean(np.abs(samples) >= .99))}


def record_test(sd, device, stopping, progress, started=None):
    """Use the voice worker's thread, so normal recognition cannot share the mic."""
    chunks = []
    overflowed = False
    with sd.RawInputStream(samplerate=16000, channels=1, dtype='int16',
                           device=device, blocksize=1600) as stream:
        if stopping.is_set():
            raise InterruptedError('Microphone test cancelled.')
        if started is not None:
            started()
        for step in range(60):
            if stopping.is_set():
                raise InterruptedError('Microphone test cancelled.')
            data, overflow = stream.read(1600)
            chunks.append(bytes(data))
            overflowed |= overflow
            if step % 10 == 9:
                progress(levels(b''.join(chunks[-10:])))
    return b''.join(chunks), bool(overflowed)
