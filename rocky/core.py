"""Pure command and wake phrase logic; no system actions in milestone one."""
import re
from datetime import datetime


def is_wake(text: str) -> bool:
    return re.sub(r"[^a-z ]", "", text.lower()).split() == ["hey", "rocky"]


def casual_greeting(text: str) -> str | None:
    command = text.casefold().replace('’', "'")
    command = re.sub(r"[,.!?]", " ", command)
    command = ' '.join(command.split())
    if command in {'hello', 'hi', 'hey'}:
        return 'Hey!'
    if re.fullmatch(r"(?:hey )?(?:what's|whats|what|waht) up", command):
        return "Hey! How's it going?"
    return None


def clock_command(text):
    command = ' '.join(text.casefold().replace('’', "'").split()).rstrip('.?!')
    command = re.sub(r'^(?:(?:please|can you|could you|would you)\s+)+', '', command)
    return re.sub(r'\s+(?:for me|please)$', '', command)


def is_time_request(text: str) -> bool:
    command = clock_command(text)
    return bool(re.fullmatch(
        r"(?:(?:what(?: is|'s|s)|tell me|do you know) (?:the )?(?:current )?time|"
        r"what time is it|tell me what time it is|do you know what time it is|(?:current )?time)(?: (?:now|right now))?",
        command,
    ))


def is_date_request(text: str) -> bool:
    return bool(re.fullmatch(
        r"(?:(?:what(?: is|'s|s)|tell me) (?:the )?(?:today's |current )?date|"
        r"what date is it|what day is it|what(?: is|'s) (?:the )?day)(?: today)?",
        clock_command(text)))


def reply(text: str) -> str:
    command = text.lower().strip().rstrip(".?!")
    greeting = casual_greeting(text)
    if greeting:
        return greeting
    if is_time_request(text):
        return datetime.now().strftime("It is %I:%M %p.")
    if is_date_request(text):
        return datetime.now().strftime("Today is %A, %d %B %Y.")
    if command in {"help", "what can you do"}:
        return "This is my voice prototype. Say hello, or ask what time it is."
    return "I heard you. That feature is planned for a later milestone."
