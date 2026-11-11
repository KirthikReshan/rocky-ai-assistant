"""Saved routines contain bounded desktop/web commands, never arbitrary code."""
import re
from rocky.tools import parse_action


def validate_routine(name, commands):
    name = name.strip().casefold()
    if not re.fullmatch(r'[a-z0-9][a-z0-9 _-]{0,59}', name):
        raise ValueError('Use a routine name of 1–60 English letters, numbers, spaces or hyphens.')
    if not isinstance(commands, list) or not 1 <= len(commands) <= 10:
        raise ValueError('A routine needs one to ten commands.')
    for command in commands:
        if not isinstance(command, str) or len(command) > 1000 or any(ord(c) < 32 for c in command):
            raise ValueError('Use one short command per line.')
        action = parse_action(command)
        if action is None or action.kind not in {'open','google','youtube_search','youtube_play'}:
            raise ValueError('Routines support opening apps/folders/websites and Google/YouTube commands.')
    return name, list(commands)


class Routines:
    def __init__(self, store, tools):
        self.store, self.tools = store, tools

    def saved(self):
        value = self.store.setting('routines', {})
        return value if isinstance(value, dict) else {}

    def save(self, name, commands):
        name, commands = validate_routine(name, commands)
        def change(value):
            saved = value if isinstance(value,dict) else {}
            if name not in saved and len(saved) >= 30:
                raise ValueError('Keep at most 30 routines.')
            saved[name] = commands
            return saved
        self.store.update_setting('routines',change)

    def remove(self, name):
        def change(value):
            saved = value if isinstance(value,dict) else {}
            saved.pop(name,None)
            return saved
        self.store.update_setting('routines',change)

    def handle(self, text):
        raw = text.strip().casefold().rstrip('.!?')
        if raw in {'show routines','list routines','my routines'}:
            return 'Saved routines: ' + ', '.join(sorted(self.saved())) if self.saved() else 'No routines yet. Add one in Settings → Routines.'
        name = re.sub(r'^(?:run|start|activate)(?: routine)?\s+', '', raw)
        saved = self.saved()
        if name not in saved:
            return 'No routine with that name. Open Settings → Routines.' if raw.startswith('run routine ') else None
        try:
            _, commands = validate_routine(name, saved[name])
        except ValueError as exc:
            return f'That saved routine is invalid: {exc} Open Settings → Routines to correct it.'
        results = []
        for command in commands:
            result = self.tools.execute(parse_action(command))
            results.append(result)
            if not result.startswith('Opening '):
                return f'Routine {name} stopped: {result}'
        return f'Ran {name}. ' + ' '.join(results)
