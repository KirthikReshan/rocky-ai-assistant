"""Decode only the answer field from an unfinished structured chat reply."""

import json
import re


def draft_reply(content):
    match = re.match(r'^\s*\{\s*"reply"\s*:\s*"', content)
    if not match:
        return ''
    encoded = content[match.end() - 1:]
    try:
        value, _ = json.JSONDecoder().raw_decode(encoded)
    except ValueError:
        value = ''
        for trim in range(min(7, len(encoded))):
            candidate = encoded if trim == 0 else encoded[:-trim]
            try:
                value = json.loads(candidate + '"')
                break
            except ValueError:
                continue
    if not isinstance(value, str) or '<' in value:
        return ''
    # A high half may be completed by a later fragment; any lone half must
    # stay out of Tk and speech engines, including one in the middle of text.
    for index, character in enumerate(value):
        if 0xD800 <= ord(character) <= 0xDFFF:
            value = value[:index]
            break
    return value
