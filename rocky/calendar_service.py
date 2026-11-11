"""Independent Windows reminder process. Shares Rocky's SQLite database."""

import argparse
import hashlib
import logging
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import tkinter as tk

from rocky.calendar_startup import CalendarStartup
from rocky.notifications import ReminderDelivery, show_reminder
from rocky.runtime import calendar_command,calendar_page_command
from rocky.app_instance import database_key
from rocky.storage import Store
from rocky.toast import ToastQueue

SERVICE_PROTOCOL = 5


def service_compatible(database):
    """A live named event avoids stale version flags after a process exits."""
    from pywintypes import error as WindowsError
    import win32event
    identity = os.path.normcase(str(Path(database).resolve()))
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    try:
        event = win32event.OpenEvent(0x00100000, False, f'Local\\RockyCalendarV{SERVICE_PROTOCOL}-{key}')
    except WindowsError:
        return False
    try:
        return win32event.WaitForSingleObject(event,0) == win32event.WAIT_OBJECT_0
    finally:
        event.Close()


def start_service(database, *, show=False):
    subprocess.Popen(
        calendar_command(database, show=show), cwd=Path(__file__).resolve().parents[1],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def open_calendar_page(database):
    subprocess.Popen(calendar_page_command(database),cwd=Path(__file__).resolve().parents[1],
                     creationflags=subprocess.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def stop_service(database):
    from pywintypes import error as WindowsError
    import win32event
    try:
        event=win32event.OpenEvent(0x0002,False,f'Local\\RockyCalendarStop-{database_key(database)}')
    except WindowsError:
        return False
    try:
        win32event.SetEvent(event)
        return True
    finally:
        event.Close()


def run_service(store, key, show_event, show_window,stop_event=None):
    from pywintypes import error as WindowsError
    import win32event
    from rocky.tray import TrayIcon

    root = tk.Tk()
    root.title('Rocky Calendar')
    root.withdraw()
    events = queue.Queue()
    delivery = ReminderDelivery(store, events)
    notifications = ToastQueue(root)
    tray = None

    def reveal():
        try:
            open_calendar_page(store.path)
        except OSError as exc:
            events.put(('error',f'Could not open Calendar in Rocky: {exc}'))

    def show(row):
        show_reminder(root, row, on_snooze=lambda minutes: snooze(row, minutes),
                      on_done=lambda: complete(row),presenter=notifications.show_now)

    def complete(row):
        try:
            return store.complete_reminder(row['id'])
        except sqlite3.Error as exc:
            events.put(('error', f'Could not mark reminder done: {exc}'))
            return False

    def snooze(row, minutes):
        try:
            alert = store.alert_for(row['id'], row['due_at'])
            if alert is None:
                raise ValueError('The alert could not be recorded. Check Calendar before snoozing.')
            store.snooze_alert(alert['id'], minutes)
        except (ValueError, sqlite3.Error) as exc:
            events.put(('error', f'Could not snooze alert: {exc}'))
            return False
        events.put(('CalendarStatus', f"Snoozed {row['body']} for {minutes} minutes."))
        return True

    def poll():
        if stop_event is not None and win32event.WaitForSingleObject(stop_event,0)==win32event.WAIT_OBJECT_0:
            root.quit()
            return
        if win32event.WaitForSingleObject(show_event, 0) == win32event.WAIT_OBJECT_0:
            reveal()
        while not events.empty():
            kind, value = events.get_nowait()
            if kind == 'tray':
                if value == 'quit':
                    root.quit()
                    return
                reveal()
            elif kind == 'error':
                logging.error('%s', value)
                notifications.post(value,title='Rocky Calendar · Attention',error=True)
            elif kind == 'Reminder':
                pass  # The reminder card is already displayed; history is durable.
            elif kind == 'CalendarStatus':
                notifications.post(value,title='Rocky Calendar')
            elif kind == 'info':
                notifications.post(value,title='Rocky reminders')
        try:
            if notifications.idle():
                delivery.tick(show, limit=1)
        except tk.TclError as exc:
            logging.error('Could not display reminder; will retry: %s', exc)
        root.after(250, poll)

    try:
        try:
            tray = TrayIcon(events, 'Rocky reminders',open_label='Open Calendar in Rocky',quit_label='Pause reminder alerts')
            tray.show()
        except WindowsError as exc:
            logging.error('Calendar tray unavailable: %s', exc)
            show_window = True
        if show_window:
            reveal()
        poll()
        root.mainloop()
    finally:
        try:
            if tray is not None:
                tray.close()
        finally:
            notifications.close()
            root.destroy()


def main():
    import win32api
    import win32event
    import winerror

    parser = argparse.ArgumentParser(description='Rocky Calendar reminder service')
    parser.add_argument('--database', type=Path)
    parser.add_argument('--show', action='store_true', help='Open the calendar window')
    args = parser.parse_args()
    store = Store(args.database)
    identity = os.path.normcase(str(store.path.resolve()))
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    show_event = win32event.CreateEvent(None, False, False, f'Local\\RockyCalendarShow-{key}')
    try:
        mutex = win32event.CreateMutex(None, False, f'Local\\RockyCalendar-{key}')
        try:
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                if args.show:
                    win32event.SetEvent(show_event)
                return
            compatible = win32event.CreateEvent(None, True, True, f'Local\\RockyCalendarV{SERVICE_PROTOCOL}-{key}')
            stop_event=win32event.CreateEvent(None,False,False,f'Local\\RockyCalendarStop-{key}')
            handler = logging.FileHandler(store.path.parent / 'calendar.log', encoding='utf-8', delay=True)
            logging.basicConfig(handlers=[handler], level=logging.ERROR)
            try:
                run_service(store, key, show_event, args.show,stop_event)
            except Exception:
                logging.exception('Calendar stopped unexpectedly')
                raise
            finally:
                compatible.Close()
                stop_event.Close()
        finally:
            mutex.Close()
    finally:
        show_event.Close()


if __name__ == '__main__':
    main()
