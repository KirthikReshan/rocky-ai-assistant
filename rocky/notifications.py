"""Poll durable due times. Called by Tk's UI loop, independently of AI work."""
import queue
import sqlite3
from rocky.toast import TOAST_DURATION_MS, show_toast


def show_reminder(root, row, duration_ms=TOAST_DURATION_MS, on_snooze=None, on_done=None, presenter=None):
    actions = []
    if on_snooze is not None:
        actions += [('Snooze 5 min',lambda:on_snooze(5)),('Snooze 15 min',lambda:on_snooze(15))]
    if on_done is not None and row['repeat'] == 'until_done':
        actions.append(('Done',on_done))
    options = dict(title='Rocky · Reminder',actions=actions,duration_ms=duration_ms or TOAST_DURATION_MS)
    return presenter(row['body'],**options) if presenter else show_toast(root,row['body'],**options)


class ReminderDelivery:
    def __init__(self, store, events, announcements=None):
        self.store = store
        self.events = events
        self.announcements = announcements
        self.error_reported = False

    def tick(self, show, now=None, limit=20):
        try:
            for reminder in self.store.due(now, limit):
                show(reminder)
                if self.store.acknowledge(reminder['id'], expected_due=reminder['due_at'],
                                          expected_body=reminder['body'], now=now):
                    message = f"Reminder: {reminder['body']}"
                    self.events.put(('Reminder', reminder['body']))
                    if self.announcements is not None:
                        try:
                            self.announcements.put_nowait(message)
                        except queue.Full:
                            self.events.put(('info', 'Voice notification queue full; reminder displayed on desktop.'))
            self.error_reported = False
        except (sqlite3.Error, OSError) as exc:
            if not self.error_reported:
                self.events.put(('error', f'Reminder delivery failed; will retry: {exc}'))
                self.error_reported = True
