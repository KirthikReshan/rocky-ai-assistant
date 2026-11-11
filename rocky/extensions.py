"""Deterministic routing for the next desktop milestone."""
import re
from rocky.clarification import AppClarifier
from rocky.documents import Documents
from rocky.routines import Routines
from rocky.system_actions import PCControls
from rocky.tools import parse_action


class Extensions:
    def __init__(self, store, tools, brain, events):
        self.clarifier = AppClarifier(store)
        self.routines = Routines(store, tools)
        self.documents = Documents(store, brain, events)
        self.pc = PCControls(events)

    def handle(self, text, tools):
        response = self.clarifier.handle(text, parse_action(text), tools)
        if response is not None:
            return response
        raw = re.sub(r'^(?:(?:hey )?rocky[, ]+)?(?:(?:please|can you|could you|would you)\s+)*', '', text.strip(), flags=re.I).rstrip('.!?')
        if (re.fullmatch(r'(?:approve|confirm|yes)(?: (?:the )?(?:pc )?action)?', raw, re.I)
                and (raw.casefold().startswith('approve') or self.pc.has_pending())):
            return 'PC actions require clicking Approve in the dedicated dialog. Voice and text cannot approve them.'
        if raw.casefold() in {'cancel action', 'cancel that action', 'reject action'}:
            self.pc.cancel_pending()
            return 'Pending PC approvals cancelled.'
        if raw.casefold() in {'cancel shutdown', 'abort shutdown', 'cancel restart'}:
            try:
                return self.pc.cancel_power()
            except OSError as exc:
                return str(exc)
        close = re.fullmatch(r'close (.+?)(?: for me)?(?: please)?', raw, re.I)
        power = re.fullmatch(r'(shut down|shutdown|turn off|power off|restart|reboot)(?: (?:windows|my (?:pc|computer|laptop)|the (?:pc|computer|laptop)))?(?: please)?', raw, re.I)
        if close or power:
            try:
                kind = 'close' if close else ('restart' if power[1].casefold() in {'restart','reboot'} else 'shutdown')
                return self.pc.request(kind, close[1] if close else '')
            except (ValueError, OSError) as exc:
                return str(exc)
        response = self.routines.handle(raw)
        return response if response is not None else self.documents.handle(raw)
