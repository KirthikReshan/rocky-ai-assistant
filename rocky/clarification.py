"""Ask about uncertain app names; never execute a fuzzy match automatically."""
import difflib
import re
import time
from rocky.tools import APPS, SITES, open_target, is_website_target, folder_alias, installed_app_id, Action


class AppClarifier:
    def __init__(self, store, clock=time.monotonic):
        self.store, self.clock = store, clock
        self.pending = None

    def handle(self, text, action, tools):
        previous, self.pending = self.pending, None
        answer = text.strip().casefold().rstrip('.!?')
        if previous and self.clock() < previous[1]:
            options = previous[0]
            if answer in {'no', 'no thanks', 'cancel', 'never mind'}:
                return 'Okay. Tell me the app name again.'
            chosen = None
            if len(options) == 1 and answer in {'yes', 'yes please', 'correct', 'that one'}:
                chosen = options[0]
            elif answer in {'first', 'first one', 'second', 'second one'}:
                position = int(answer.startswith('second'))
                if position < len(options):
                    chosen = options[position]
            elif answer in options:
                chosen = answer
            if chosen:
                return tools.execute(Action('open', chosen))
        if action is None or action.kind != 'open':
            return None
        aliases = self.store.setting('app_nicknames', {})
        target = open_target(action.target, aliases).casefold()
        if (not target or target in APPS or target in SITES or target in {'browser','whatsapp','codex','openai codex'}
                or folder_alias(target) or is_website_target(target) or re.search(r'[:\\/]', target)):
            return None
        try:
            installed_app_id(target)
            return None
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            return None
        names = set(APPS) | {'whatsapp','codex','browser'}
        if isinstance(aliases, dict):
            names.update(k for k, v in aliases.items() if isinstance(v, str))
        candidates = difflib.get_close_matches(target, sorted(names), n=2, cutoff=.65)
        if not candidates:
            return None
        # Equivalent VS Code/Chrome aliases should not produce a fake ambiguity.
        unique = []
        for candidate in candidates:
            if all(open_target(candidate, aliases) != open_target(old, aliases)
                   and APPS.get(candidate, candidate) != APPS.get(old, old) for old in unique):
                unique.append(candidate)
        self.pending = unique, self.clock() + 30
        return 'Did you mean ' + ' or '.join(unique) + '? Say the name' + (' or yes.' if len(unique) == 1 else ', first, or second.')
