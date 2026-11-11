"""Natural-language notes/reminders. No AI involved in saving or scheduling."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import re
import sqlite3
import math
import time

from rocky.storage import utc_now
from rocky.tools import parse_action

_ONES = ('one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine')
NUMBERS = dict(zip(_ONES + ('ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen',
                            'sixteen', 'seventeen', 'eighteen', 'nineteen'), range(1, 20)))
for _value, _word in enumerate(('twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'), 2):
    NUMBERS[_word] = _value * 10
    for _unit, _name in enumerate(_ONES, 1):
        NUMBERS[_word + ' ' + _name] = NUMBERS[_word + '-' + _name] = _value * 10 + _unit
# Match compound numbers before their individual words, including in voice scoring.
NUMBERS = dict(sorted(NUMBERS.items(), key=lambda item: len(item[0]), reverse=True))


def _check_clock_change(due):
    if due.replace(fold=0).utcoffset() != due.replace(fold=1).utcoffset():
        raise ValueError('Choose a time outside the daylight saving clock change.')


def is_time_fragment(text):
    value = re.sub(r'^for\s+', '', text.strip().lower())
    return bool(re.fullmatch(r'(?:today|tomorrow|\d{1,2}(?:[.:]\d\d)?\s*(?:am|pm)?)', value)
                or (re.match(r'^(?:today|tomorrow)\b', value) and re.search(r'\d|noon|midnight', value))
                or re.match(r'^at (?:\d|noon\b|midnight\b)', value)
                or re.fullmatch(r'(?:in )?(?:\d+(?:\.\d+)?|' + '|'.join(NUMBERS) + r'|a|an) (?:seconds?|minutes?|hours?|days?|weeks?)', value))


def claims_reminder_action(text):
    value = text.lower()
    return bool(re.search(r'\bi(?:[\u2019\x27]ll| will) remind you\b', value)
                or (re.search(r'\breminders?\b', value)
                    and re.search(r'\b(?:saved|save|set|scheduled|schedule|created|create)\b', value)
                    and re.search(r'\b(?:i[\u2019\x27](?:ve|ll)|i have|i will|i saved|i set|your reminder|the reminder)\b', value)))


def denies_reminder_service(text):
    return bool(re.search(r"\b(?:don't have|do not have|have no|there is no|doesn't have|does not have) (?:a |any )?(?:working |local )?reminder service\b",
                          text.lower().replace('\u2019', "'")))


def normalize_reminder_request(raw):
    """Repair a known speech confusion only at the start of a reminder request."""
    amount = r'(?:\d+(?:\.\d+)?|' + '|'.join(NUMBERS) + r'|a|an)'
    time_start = (r'(?:in\s+' + amount + r'\b|' + amount +
                  r'\s+(?:seconds?|minutes?|hours?|days?|weeks?)\b|at\s+(?:\d|noon\b|midnight\b)|today\b|tomorrow\b|to\s+)')
    return re.sub(r'^remain\s+me\s+(?=' + time_start + r')', 'remind me ', raw, flags=re.I)


def parse_due(phrase, zone='Asia/Kolkata', now=None):
    import dateparser
    local_zone = ZoneInfo(zone)
    base = (now or utc_now()).astimezone(local_zone)
    phrase = phrase.strip().lower().replace('a.m.', 'am').replace('p.m.', 'pm')
    if not phrase.startswith('in '):
        phrase = re.sub(r'\b(\d{1,2})\.(\d{2})(?=\s*(?:am|pm)\b|\b)', r'\1:\2', phrase)
    if re.search(r'\bist\b', phrase):
        if zone not in {'Asia/Kolkata', 'Asia/Calcutta'}:
            raise ValueError('IST is ambiguous. Use your configured local time without an abbreviation, or choose Asia/Kolkata for India time.')
        # dateparser otherwise treats IST as Israel Standard Time.
        phrase = re.sub(r'\s*\bist\b', '', phrase).strip()
    phrase = re.sub(r'\bnoon\b', '12 PM', phrase, flags=re.I)
    phrase = re.sub(r'\bmidnight\b', '12 AM', phrase, flags=re.I).lower()
    phrase = re.sub(r'\b(' + '|'.join(NUMBERS) + r')\b', lambda m: str(NUMBERS[m[0]]), phrase)
    phrase = re.sub(r'\b(?:a|an) (?=(?:second|minute|hour|day|week)\b)', '1 ', phrase)
    if re.search(r'\b(?:every|daily|weekly|monthly)\b', phrase):
        raise ValueError('Use remind me every day at 5 PM to stretch, or every week with a date and time. '
                         'Other repeat patterns need a single future date and time.')
    if re.search(r'\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?', phrase):
        raise ValueError('Please spell the month, such as 5 November at 5 PM, to avoid date ambiguity.')
    # Bare clock hours such as "at 5" need AM/PM clarification.
    clock_numbers = list(re.finditer(r'\b(\d{1,2}):\d\d\b', phrase))
    if (re.fullmatch(r'(?:at )?\d{1,2}', phrase)
            or (re.search(r'\d{1,2}:\d\d', phrase) and not re.search(r'\b(?:am|pm)\b', phrase)
                and any(1 <= int(match[1]) <= 12 for match in clock_numbers))):
        raise ValueError('Please include AM or PM, such as at 5 PM.')
    relative = bool(re.fullmatch(r'in \d+(?:\.\d+)? (?:seconds?|minutes?|hours?|days?|weeks?)', phrase))
    has_clock = bool(re.search(r'\d(?::\d\d)?\s*(?:am|pm)\b|\d{1,2}:\d\d|\bnoon\b|\bmidnight\b', phrase))
    if not relative and not has_clock:
        raise ValueError('Please include a time, such as tomorrow at 5 PM, or say in two days.')
    if relative:
        match = re.fullmatch(r'in (\d+(?:\.\d+)?) (second|minute|hour|day|week)s?', phrase)
        try:
            amount = float(match[1])
            if not math.isfinite(amount):
                raise OverflowError('Non-finite duration')
            due = base.astimezone(timezone.utc) + timedelta(**{match[2] + 's': amount})
        except OverflowError as exc:
            raise ValueError('That reminder is too far in the future. Please choose a nearer time.') from exc
        if due <= base:
            raise ValueError('Please choose a time in the future.')
        return due
    clock = re.fullmatch(r'(?:at )?(\d{1,2})(?::(\d\d))?\s*(am|pm)?', phrase)
    if clock:
        hour, minute = int(clock[1]), int(clock[2] or 0)
        if minute > 59 or (clock[3] and not 1 <= hour <= 12) or hour > 23:
            raise ValueError('Please use a valid time, such as 5:30 PM.')
        if clock[3]:
            hour = hour % 12 + (12 if clock[3] == 'pm' else 0)
        due = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= base:
            due += timedelta(days=1)
        _check_clock_change(due)
        return due.astimezone(timezone.utc)
    due = dateparser.parse(phrase, languages=['en'], settings={
        'RELATIVE_BASE': base, 'TIMEZONE': zone, 'RETURN_AS_TIMEZONE_AWARE': True,
        'PREFER_DATES_FROM': 'future', 'DATE_ORDER': 'DMY', 'PREFER_LOCALE_DATE_ORDER': False})
    if due is None or due <= base:
        raise ValueError('That time is unclear or in the past. Please give a future date and time.')
    _check_clock_change(due)
    return due.astimezone(timezone.utc)


def display_time(due, zone):
    return due.astimezone(ZoneInfo(zone)).strftime('%d %B %Y at %I:%M:%S %p') + f' ({zone})'


def repeat_note(repeat):
    return f' Repeats {repeat}.' if repeat != 'once' else ''


def recurring_request(raw):
    repeats = {'every day':'daily', 'daily':'daily', 'every week':'weekly', 'weekly':'weekly',
               'every weekday':'weekdays', 'weekdays':'weekdays', 'every month':'monthly', 'monthly':'monthly',
               'every year':'yearly', 'yearly':'yearly', 'annually':'yearly',
               'until acknowledged':'until_done', 'until i acknowledge it':'until_done', 'until done':'until_done'}
    if raw.lower().startswith('remind me '):
        suffix = re.fullmatch(r'(.+?)\s+until (?:i acknowledge it|acknowledged|done)', raw, re.I)
        if suffix:
            return 'until_done', suffix[1]
    choices = '|'.join(sorted(repeats, key=len, reverse=True))
    front = re.fullmatch(r'remind me (' + choices + r')(?: (.+))?', raw, re.I)
    if front:
        repeat = repeats[front[1].lower()]
        return repeat, 'remind me ' + (front[2] or '')
    body_first = re.fullmatch(r'remind me to (.+?) (' + choices + r')(?: (.+))?', raw, re.I)
    if body_first:
        repeat = repeats[body_first[2].lower()]
        return repeat, 'remind me to ' + body_first[1] + ' ' + (body_first[3] or '')
    return 'once', raw


def split_reminder(raw):
    """Find the first real time clause, preserving phrasal verbs in the message."""
    if not raw.lower().startswith('remind me to '):
        match = re.fullmatch(r'remind me (.+?) to (.+)', raw, re.I)
        if match:
            return match[1], match[2]
    else:
        message = raw[len('remind me to '):]
        weekdays = r'monday|tuesday|wednesday|thursday|friday|saturday|sunday'
        months = r'january|february|march|april|may|june|july|august|september|october|november|december'
        dated = (r'(?:on (?:\d|(?:(?:next|this) )?(?:' + weekdays + '|' + months + r')(?:\s|$))'
                 r'|(?:(?:next|this) )?(?:' + weekdays + r')[,\s]+(?:at\b|\d|noon\b|midnight\b|morning\b|afternoon\b|evening\b|night\b)'
                 r'|\d{1,2}(?:st|nd|rd|th)?\s+(?:' + months + r')\b'
                 r'|(?:' + months + r')\s+\d)')
        for boundary in re.finditer(r'\s+', message):
            phrase = message[boundary.end():]
            relative = r'in (?:\d+(?:\.\d+)?|' + '|'.join(NUMBERS) + r'|a|an) (?:seconds?|minutes?|hours?|days?|weeks?)\b'
            if (re.match(relative, phrase, re.I)
                    or re.match(r'at (?:\d|noon\b|midnight\b)', phrase, re.I)
                    or re.match(r'(?:tomorrow|today)(?:\s|$)', phrase, re.I)
                    or re.match(dated, phrase, re.I)):
                body = message[:boundary.start()].strip()
                if body:
                    return phrase, body
    raise ValueError('What should I remind you about, and when? For example, remind me in two days to call Mom.')


class ReminderCommands:
    def __init__(self, store, zone='Asia/Kolkata', require_advanced_calendar=False):
        self.store = store
        self.zone = zone
        self.require_advanced_calendar = require_advanced_calendar
        ZoneInfo(zone)
        self.pending = None
        self.pending_until = 0.0
        self.recent = None
        self.recent_until = 0.0

    @property
    def expects_body(self):
        return bool(self.pending and time.monotonic() <= self.pending_until and not self.pending.get('body'))

    def status(self):
        if self.pending and time.monotonic() <= self.pending_until:
            return 'That reminder is not saved yet. ' + self._missing()
        latest = self.store.latest_reminder()
        if latest is None:
            return 'There is no saved reminder in the database. Tell me what to remind you about and when.'
        due = datetime.fromtimestamp(latest['due_at'], timezone.utc)
        state = ('The desktop alert was issued.' if latest['notified_at'] is not None else
                 'It is overdue and waiting for its desktop alert.' if due <= utc_now() else 'Its alert is pending.')
        return (f"Saved reminder {latest['id']}: {latest['body']}, {display_time(due, latest['timezone'])}. "
                f"{state}" + repeat_note(latest['repeat']))

    def _missing(self):
        draft = self.pending
        if draft.get('error'):
            return draft['error']
        if not draft.get('body'):
            return 'What should I remind you about?'
        return 'When should I remind you? Include AM or PM for clock times.'

    def _time(self, phrase):
        phrase = re.sub(r'^for\s+', '', phrase.strip(), flags=re.I)
        if re.fullmatch(r'(?:\d+(?:\.\d+)?|' + '|'.join(NUMBERS) + r') (?:seconds?|minutes?|hours?|days?|weeks?)', phrase, re.I):
            phrase = 'in ' + phrase
        self.pending['phrase'] = phrase
        self.pending['due'] = None
        try:
            self.pending['due'] = parse_due(phrase, self.zone)
            self.pending['error'] = None
        except ValueError as exc:
            self.pending['error'] = str(exc)

    def _finish(self):
        draft = self.pending
        if not draft.get('body') or not draft.get('due'):
            return self._missing()
        if len(draft['body']) > 1000:
            return 'Please keep reminder messages under 1,000 characters.'
        if draft['due'] <= utc_now():
            draft['due'] = None
            draft['error'] = 'That time has already passed. Please give a new future time.'
            return draft['error']
        repeat = draft.get('repeat', 'once')
        if self.require_advanced_calendar and repeat not in {'once','daily','weekly'}:
            from rocky.calendar_service import service_compatible
            if not service_compatible(self.store.path):
                return 'The updated Calendar is not ready. Wait a few seconds; if needed, choose Quit Rocky Calendar from its tray menu and reopen Calendar. This reminder is not saved yet.'
        reminder_id = self.store.add_reminder(draft['body'], draft['due'], self.zone, draft['original'], repeat=repeat)
        self.recent = self.store.reminder(reminder_id)
        saved_due = datetime.fromtimestamp(self.recent['due_at'], timezone.utc)
        self.recent_until = time.monotonic() + 300
        answer = (f"Reminder {reminder_id} saved: {draft['body']}, {display_time(saved_due, self.zone)}."
                  + repeat_note(repeat))
        self.pending = None
        return answer

    def _dialog(self, raw):
        original = raw
        repeat, raw = recurring_request(raw)
        command = re.sub(r',\s*', ' ', raw.lower())
        if self.pending and time.monotonic() > self.pending_until:
            self.pending = None
        correction = re.fullmatch(r'(?:actually[ ,]+)?(?:make|change) that(?: to)? (.+)', raw, re.I)
        if correction:
            phrase = correction[1].strip()
            if self.pending:
                self._time(phrase)
                self.pending_until = time.monotonic() + 300
                return self._finish()
            if self.recent is None or time.monotonic() > self.recent_until:
                return 'Which reminder should I change? Open Calendar to select it.'
            if re.fullmatch(r'(?:\d+(?:\.\d+)?|' + '|'.join(NUMBERS) + r') (?:seconds?|minutes?|hours?|days?|weeks?)', phrase, re.I):
                phrase = 'in ' + phrase
            due = parse_due(phrase, self.recent['timezone'])
            if not self.store.reschedule_reminder(self.recent, due):
                self.recent = None
                return 'That reminder changed or its alert was already issued. Open Calendar to check it.'
            self.recent = self.store.reminder(self.recent['id'])
            self.recent_until = time.monotonic() + 300
            due = datetime.fromtimestamp(self.recent['due_at'], timezone.utc)
            return (f"Reminder {self.recent['id']} updated: {self.recent['body']}, {display_time(due, self.recent['timezone'])}."
                    + repeat_note(self.recent['repeat']))
        if command in {'cancel reminder', 'cancel that reminder', 'never mind', 'nevermind'} and self.pending:
            self.pending = None
            return 'Discarded the unfinished reminder. No new reminder was saved.'
        if command in {'yes', 'yes save it', 'save it', 'confirm reminder', 'yes please'}:
            if self.pending:
                return self._finish()
            return None if command in {'yes', 'yes please'} else self.status()

        # Also accept time-first conversational wording from speech recognition.
        front = re.fullmatch(r'(.+?) (?:you should |please )?remind me (?:for|to|about) (.+)', raw, re.I)
        explicit = command.startswith('remind me') or front is not None
        if explicit:
            self.pending = {'original': original, 'body': None, 'phrase': None, 'due': None, 'error': None,
                            'repeat': repeat}
            self.pending_until = time.monotonic() + 300
            if front:
                phrase, body = front[1], front[2]
            else:
                spoken = re.fullmatch(r'remind me (.+?) (?:and )?(?:say|tell me) (.+)', raw, re.I)
                if spoken:
                    phrase, body = spoken[1], spoken[2]
                else:
                    try:
                        phrase, body = split_reminder(raw)
                    except ValueError:
                        rest = re.sub(r'^remind me\s*', '', raw, flags=re.I)
                        if rest.lower().startswith(('to ', 'about ')):
                            body = re.sub(r'^(?:to|about)\s+', '', rest, flags=re.I)
                            phrase = None
                        else:
                            phrase, body = rest, None
            self.pending['body'] = body.strip().strip('"') if body else None
            if phrase:
                self._time(phrase)
            return self._finish()

        if re.search(r'\bremind(?:er|ers)?\b', command):
            return self.status()
        if self.pending and (re.search(r'\bdoing all that\b|\bis it saved\b', command)
                             or re.search(r'\b(?:did|have|will) you\b.*\b(?:check|save|set|schedule)\b', command)):
            return self.status()
        if not self.pending and is_time_fragment(raw):
            self.pending = {'original': raw, 'body': None, 'phrase': None, 'due': None, 'error': None}
            self.pending_until = time.monotonic() + 300
            self._time(raw)
            return self._finish()
        if self.pending:
            draft = self.pending
            if parse_action(raw) is not None:
                return None
            time_reply = is_time_fragment(raw)
            if command in {'am', 'pm', 'a.m', 'p.m'} and draft.get('phrase'):
                self._time(draft['phrase'] + ' ' + command.replace('.', ''))
            elif time_reply:
                self._time(raw)
            elif not draft.get('body') and not re.match(r'^(?:open|close|play|search|edit|explain|read|show|list|take|save|what|why|how|when|did|do|can|hello|hi)\b', command):
                draft['body'] = re.sub(r'^(?:to|about)\s+', '', raw, flags=re.I)
            else:
                return None
            self.pending_until = time.monotonic() + 300
            return self._finish()
        return None

    def handle(self, text):
        raw = re.sub(r'^(?:please |can you |could you )', '', text.strip(), flags=re.I).rstrip('.!?')
        raw = normalize_reminder_request(raw)
        if re.match(r'^remain\s+me\b', raw, re.I):
            return 'Did you mean a reminder? Tell me when and what to remind you about.'
        command = raw.lower()
        try:
            done = re.fullmatch(r'(?:mark reminder (\d+) (?:done|complete)|acknowledge reminder (\d+))', command)
            if done:
                reminder_id = int(done[1] or done[2])
                return (f'Reminder {reminder_id} marked done.' if self.store.complete_reminder(reminder_id) else
                        'That ID is not a pending reminder that repeats until done.')
            if re.match(r'^snooze\b', command):
                snooze = re.fullmatch(r'snooze(?: (?:that|the last|my last)(?: reminder| alert)?)?'
                                      r'(?: for)? (5|15|30|five|fifteen|thirty) minutes?', command)
                if not snooze:
                    return 'Say snooze for five, fifteen or thirty minutes, or select an alert in Calendar.'
                alerts = self.store.recent_alerts(limit=1)
                if not alerts or utc_now().timestamp() - alerts[0]['notified_at'] > 300:
                    return 'Open Recent alerts in Calendar to choose which alert to snooze.'
                amount = {'five': 5, 'fifteen': 15, 'thirty': 30}.get(snooze[1])
                minutes = amount if amount is not None else int(snooze[1])
                reminder = self.store.snooze_alert(alerts[0]['id'], minutes, now=utc_now())
                row = self.store.reminder(reminder)
                self.pending = None
                self.recent = row
                self.recent_until = time.monotonic() + 300
                return f"Snoozed {row['body']} until {display_time(datetime.fromtimestamp(row['due_at'], timezone.utc), row['timezone'])}."
            note = re.fullmatch(r'(?:take (?:a )?note|save (?:a )?note|note)(?: that)?[ :]+(.+)', raw, re.I)
            if note:
                body = note[1].strip()
                if len(body) > 8000:
                    return 'Please keep each note under 8,000 characters.'
                note_id = self.store.add_note(body)
                return f'Saved note {note_id}: {body}'
            if command in {'show notes', 'show my notes', 'list notes', 'read my notes'}:
                notes = self.store.notes()
                return '\n'.join(f"Note {r['id']}: {r['body']}" for r in notes) or 'You have no saved notes yet.'
            if command in {'show reminders', 'show my reminders', 'list reminders', 'what are my reminders'}:
                rows = self.store.reminders()
                return '\n'.join(f"Reminder {r['id']}: {r['body']}, {display_time(datetime.fromtimestamp(r['due_at'], timezone.utc), r['timezone'])}."
                                 + repeat_note(r['repeat']) for r in rows) or 'You have no pending reminders.'
            result = self._dialog(raw)
            if result is None:
                self.recent = None
            return result
        except ValueError as exc:
            return str(exc)
        except OverflowError:
            return 'That reminder is too far in the future. Please choose a nearer time.'
        except (sqlite3.Error, OSError):
            return 'I could not save or read local data. Check the Rocky data folder and free disk space.'
