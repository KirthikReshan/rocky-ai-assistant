"""Live isolated popup/speech test using a temporary database."""
from pathlib import Path
import queue
import sys
import tempfile
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rocky.assistant import Assistant
from rocky.storage import Store
from rocky.reminders import ReminderCommands
from rocky.notifications import ReminderDelivery, show_reminder
from rocky.speech import Speech


def main():
    with tempfile.TemporaryDirectory(prefix='rocky-reminder-test-') as directory:
        path = Path(directory) / 'rocky.db'
        assistant = Assistant(reminders=ReminderCommands(Store(path)))
        print(assistant.respond('remain me in two seconds and say test the Rocky reminder alert'), flush=True)
        # Reopen persisted state, as after application restart.
        store = Store(path)
        events, announcements = queue.Queue(), queue.Queue()
        delivery = ReminderDelivery(store, events, announcements)
        speech = Speech(lambda kind, text: print(text, flush=True))
        root = tk.Tk()
        root.withdraw()
        delivered = []

        def show(row):
            popup = show_reminder(root, row)
            delivered.append(popup)
            print('Created real reminder popup:', popup.title(), flush=True)

        def check():
            delivery.tick(show)
            if not announcements.empty():
                speech.speak(announcements.get_nowait())
                root.after(3000, root.destroy)
            else:
                root.after(250, check)

        root.after(100, check)
        root.after(25000, root.destroy)
        root.mainloop()
        assert len(delivered) == 1, 'No popup was delivered'
        assert not Store(path).reminders(), 'Reminder was not durably acknowledged'
        print('Live popup, speech and persistence test passed.', flush=True)


if __name__ == '__main__':
    main()
