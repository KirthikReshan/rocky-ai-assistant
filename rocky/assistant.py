"""Route built-in commands and conversation independently of the UI/audio."""
import threading
from rocky.brain import Brain, BrainError, BrainCancelled
from rocky.core import casual_greeting, is_time_request, is_date_request, reply
from rocky.tools import DesktopTools, parse_action
from rocky.reminders import claims_reminder_action, denies_reminder_service
from rocky.system_actions import claims_pc_action


class Assistant:
    supports_sentence_speech = True

    def __init__(self, brain: Brain | None = None, tools=None, reminders=None, coding=None, extensions=None):
        self.brain = brain
        self.tools = tools or DesktopTools()
        self.reminders = reminders
        self.coding = coding
        self.extensions = extensions
        self._reply_lock = threading.Lock()
        self._reply_thread = None

    def respond(self, text: str, on_draft=None) -> str:
        owner = threading.get_ident()
        with self._reply_lock:
            self._reply_thread = owner
        try:
            return self._respond(text, on_draft)
        finally:
            with self._reply_lock:
                if self._reply_thread == owner:
                    self._reply_thread = None

    def cancel_current(self):
        with self._reply_lock:
            owner = self._reply_thread
        cancel = getattr(self.brain, 'cancel_current', None)
        if callable(cancel):
            cancel()
        documents = getattr(self.extensions, 'documents', None)
        if documents is not None:
            documents.cancel_for_thread(owner)

    def _respond(self, text: str, on_draft=None) -> str:
        if self.extensions is not None:
            response = self.extensions.handle(text, self.tools)
            if response is not None:
                return response
        command = text.lower().strip().rstrip(".?!")
        greeting = casual_greeting(text)
        if greeting:
            return greeting
        # Clock questions are complete requests, even while a reminder draft is
        # waiting for its body. They must not be saved as reminder messages.
        if is_time_request(text) or is_date_request(text):
            return reply(text)
        if self.coding is not None:
            response = self.coding.handle(text)
            if response is not None:
                return response
        if self.reminders is not None:
            response = self.reminders.handle(text)
            if response is not None:
                return response
        action = parse_action(text)
        if action is not None:
            return self.tools.execute(action)
        if command in {"help", "what can you do", "what you can do", "so what you can do"}:
            return ("I can open apps, folders and websites, search Google, and find or play YouTube videos. "
                    "Try open calculator, search for Python tutorials, or play relaxing music on YouTube. "
                    "I can also save notes, set reminders, and read or explain files in a selected code folder. "
                    "Try run routine coding mode, search documents for a subject, or remind me every weekday at 5 PM to stretch. "
                    "PC closing and power controls require approval in a dialog; code edits need the exact diff approved.")
        if self.brain is None:
            return reply(text)
        try:
            context = self.coding.status_context() if self.coding else ''
            if self.reminders is not None:
                context += '\nRocky has an enabled local reminder service. Reminder database state (not a new save): ' + self.reminders.status()
                context += '\nNo reminder save was performed for this chat request. Never claim to save, set or schedule a reminder yourself.'
            if on_draft is not None and getattr(self.brain, 'supports_sentence_speech', False) is True:
                def checked_draft(draft):
                    if claims_reminder_action(draft) or denies_reminder_service(draft) or claims_pc_action(draft):
                        return
                    if self.reminders is not None and self.reminders.pending is not None:
                        return
                    on_draft(draft)

                answer = self.brain.chat(text, app_context=context, on_draft=checked_draft)
            else:
                answer = self.brain.chat(text, app_context=context) if context else self.brain.chat(text)
            short_confirmation = (self.reminders is not None and self.reminders.pending is not None
                                  and answer.lower().strip(' .!') in {'done', 'saved', 'saved it', 'okay, saved it', 'scheduled'})
            false_denial = self.reminders is not None and denies_reminder_service(answer)
            if claims_pc_action(answer):
                answer = 'No PC action was performed for that chat request. Say close and the app name, restart Windows, or shut down Windows, then review the approval dialog.'
            if claims_reminder_action(answer) or short_confirmation or false_denial:
                answer = self.reminders.status() if self.reminders else 'The reminder service is not enabled. No reminder was saved.'
                if false_denial:
                    answer = "Rocky's local reminder service is available. " + answer
            if hasattr(self.brain, 'correct_last_reply'):
                self.brain.correct_last_reply(answer)
            return answer
        except BrainCancelled:
            return 'Stopped the previous reply.'
        except BrainError as exc:
            return str(exc)
