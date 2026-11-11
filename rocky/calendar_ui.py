"""Calendar agenda and reminder editor for the standalone Calendar process."""

import sqlite3
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, ttk
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rocky.storage import utc_now
from rocky.recurrence import REPEATS


DATE_FORMAT = '%Y-%m-%d %H:%M'


def event_time(value, zone):
    local = datetime.strptime(value, DATE_FORMAT)
    first = local.replace(tzinfo=ZoneInfo(zone), fold=0)
    second = first.replace(fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError('Choose a time outside the daylight saving clock change.')
    if first <= utc_now():
        raise ValueError('Choose a time in the future.')
    return first


def display_time(timestamp, zone):
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(ZoneInfo(zone)).strftime(DATE_FORMAT)


class CalendarWindow:
    def __init__(self, root, store, startup, on_test_notification=None):
        self.root = root
        self.store = store
        self.startup = startup
        self.status = tk.StringVar(value='Calendar reminders are active.')
        self.pending = {}
        self.history = {}

        root.geometry('820x490')
        root.minsize(600, 340)
        ttk.Label(root, text='Rocky Calendar', font=('Segoe UI', 19), padding=(14, 10)).pack(anchor='w')
        tabs = ttk.Notebook(root)
        tabs.pack(fill='both', expand=True, padx=14, pady=(0, 8))
        upcoming, recent = ttk.Frame(tabs, padding=8), ttk.Frame(tabs, padding=8)
        tabs.add(upcoming, text='Upcoming events')
        tabs.add(recent, text='Recent alerts')
        self.agenda = self.make_table(upcoming, 'Scheduled for')
        self.alerts = self.make_table(recent, 'Alert shown at')
        snooze_controls = ttk.Frame(recent)
        snooze_controls.pack(fill='x', pady=(0, 8), before=self.alerts.master)
        self.snooze_minutes = ttk.Combobox(snooze_controls, state='readonly', width=12,
                                          values=('5 minutes', '15 minutes', '30 minutes'))
        self.snooze_minutes.current(0)
        self.snooze_minutes.pack(side='left')
        ttk.Button(snooze_controls, text='Snooze selected alert', command=self.snooze_selected).pack(side='left', padx=6)
        controls = ttk.Frame(upcoming)
        controls.pack(fill='x', pady=(0, 8), before=self.agenda.master)
        ttk.Button(controls, text='Add event', command=self.edit).pack(side='left')
        ttk.Button(controls, text='Edit event', command=self.edit_selected).pack(side='left', padx=6)
        ttk.Button(controls, text='Cancel event', command=self.cancel_selected).pack(side='left')
        ttk.Button(controls, text='Mark done', command=self.complete_selected).pack(side='left', padx=6)
        self.agenda.bind('<Double-1>', lambda event: self.edit_selected() if self.agenda.identify_row(event.y) else None)

        footer = ttk.Frame(root, padding=(14, 0, 14, 8))
        footer.pack(fill='x')
        self.launch_at_sign_in = tk.BooleanVar(value=startup is not None and startup.enabled())
        startup_toggle = ttk.Checkbutton(footer, text='Start Calendar at Windows sign-in',
                                         variable=self.launch_at_sign_in, command=self.change_startup)
        startup_toggle.pack(side='left')
        if startup is None:
            startup_toggle.state(['disabled'])
        ttk.Button(footer, text='Quit Calendar', command=root.quit).pack(side='right')
        if on_test_notification is not None:
            ttk.Button(footer,text='Test notification',command=on_test_notification).pack(side='right',padx=6)
        ttk.Label(root, textvariable=self.status, padding=(14, 0, 14, 10)).pack(anchor='w')

    def make_table(self, parent, time_title):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        table = ttk.Treeview(frame, columns=('event', 'time', 'zone'), show='headings', selectmode='browse')
        for column, title, width in [('event', 'Event', 340), ('time', time_title, 145), ('zone', 'Time zone', 145)]:
            table.heading(column, text=title)
            table.column(column, width=width, stretch=column == 'event')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        table.pack(fill='both', expand=True)
        return table

    def refresh(self):
        pending = {str(row['id']): dict(row) for row in self.store.reminders(limit=-1)}
        history = {str(row['id']): dict(row) for row in self.store.recent_alerts()}
        if pending != self.pending:
            self.render(self.agenda, pending, 'due_at')
            self.pending = pending
        if history != self.history:
            self.render(self.alerts, history, 'notified_at')
            self.history = history

    def render(self, table, rows, time_column):
        selection = table.selection()
        position = table.yview()[0]
        table.delete(*table.get_children())
        for key, row in rows.items():
            zone = row['timezone']
            title = row['body'] + (' · ' + REPEATS[row['repeat']] if row['repeat'] != 'once' else '')
            if row.get('snoozed_from') is not None:
                title += ' · Snoozed'
            table.insert('', 'end', iid=key, values=(title, display_time(row[time_column], zone), zone))
        if selection and selection[0] in rows:
            table.selection_set(selection[0])
        table.yview_moveto(position)

    def selected(self):
        selection = self.agenda.selection()
        if selection:
            return self.pending.get(selection[0])
        self.status.set('Select an upcoming event first.')
        return None

    def snooze_selected(self):
        selection = self.alerts.selection()
        if not selection:
            self.status.set('Select a recent alert to snooze.')
            return
        minutes = (5, 15, 30)[self.snooze_minutes.current()]
        try:
            reminder = self.store.snooze_alert(int(selection[0]), minutes)
            row = self.store.reminder(reminder)
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror('Could not snooze alert', str(exc), parent=self.root)
            return
        self.status.set(f"Snoozed until {display_time(row['due_at'], row['timezone'])}.")
        self.refresh()

    def edit_selected(self):
        row = self.selected()
        if row:
            self.edit(row)

    def complete_selected(self):
        row = self.selected()
        if row:
            try:
                completed = self.store.complete_reminder(row['id'])
                self.status.set('Marked done. Repeat alerts stopped.' if completed else
                                'Mark done applies to reminders that repeat until done.')
                self.refresh()
            except sqlite3.Error as exc:
                self.status.set(f'Could not mark done: {exc}')

    def edit(self, row=None):
        editor = tk.Toplevel(self.root)
        editor.title('Edit event' if row else 'Add event')
        editor.transient(self.root)
        editor.resizable(False, False)
        zone = row['timezone'] if row else 'Asia/Kolkata'
        original_due = datetime.fromtimestamp(row['due_at'], timezone.utc) if row else None
        if row:
            date = display_time(row['due_at'], zone)
        else:
            date = (utc_now().astimezone(ZoneInfo(zone)) + timedelta(hours=1)).strftime(DATE_FORMAT)
        title = tk.StringVar(value=row['body'] if row else '')
        when = tk.StringVar(value=date)
        zone_name = tk.StringVar(value=zone)
        form = ttk.Frame(editor, padding=14)
        form.pack()
        fields = []
        for label, value in [('Event name', title), ('Date and time (YYYY-MM-DD HH:MM)', when), ('Time zone', zone_name)]:
            ttk.Label(form, text=label).pack(anchor='w')
            field = ttk.Entry(form, textvariable=value, width=44)
            field.pack(fill='x', pady=(2, 8))
            fields.append(field)
        ttk.Label(form, text='Repeat').pack(anchor='w')
        repeat_picker = ttk.Combobox(form, state='readonly', values=list(REPEATS.values()))
        repeat_picker.current(list(REPEATS).index(row['repeat']) if row else 0)
        repeat_picker.pack(fill='x', pady=(2, 8))

        def save(event=None):
            body, entered_zone = title.get().strip(), zone_name.get().strip()
            entered_time = when.get().strip()
            try:
                if not body:
                    raise ValueError('Enter an event name.')
                if original_due and entered_time == date and entered_zone == zone and original_due > utc_now():
                    due = original_due
                else:
                    due = event_time(entered_time, entered_zone)
                if row:
                    if not self.store.update_reminder(row['id'], body, due, entered_zone,
                                                      created_at=row['created_at'],
                                                      repeat=list(REPEATS)[repeat_picker.current()], expected=row):
                        raise ValueError('That event changed or is no longer pending. Close this editor and reopen the event.')
                else:
                    self.store.add_reminder(body, due, entered_zone, body,
                                            repeat=list(REPEATS)[repeat_picker.current()])
            except (ValueError, ZoneInfoNotFoundError, sqlite3.Error) as exc:
                messagebox.showerror('Could not save event', str(exc), parent=editor)
                return
            editor.destroy()
            self.status.set('Event saved.')
            self.refresh()

        ttk.Button(form, text='Save event', command=save).pack(side='left')
        ttk.Button(form, text='Close', command=editor.destroy).pack(side='right')
        editor.bind('<Return>', save)
        editor.grab_set()
        fields[0].focus_set()

    def cancel_selected(self):
        row = self.selected()
        if row is None:
            return
        prompt = f"Cancel “{row['body']}”?"
        if row['repeat'] != 'once':
            prompt = f"Cancel “{row['body']}” and all future repeats?"
        if messagebox.askyesno('Cancel event', prompt, parent=self.root):
            try:
                cancelled = self.store.cancel_reminder(row['id'], created_at=row['created_at'], expected=row)
            except sqlite3.Error as exc:
                messagebox.showerror('Could not cancel event', str(exc), parent=self.root)
                return
            self.status.set('Event cancelled.' if cancelled else 'That event changed or is no longer pending. Select it again.')
            self.refresh()

    def change_startup(self):
        try:
            self.startup.set_enabled(self.launch_at_sign_in.get())
        except Exception as exc:
            self.launch_at_sign_in.set(self.startup.enabled())
            messagebox.showerror('Could not change sign-in setting', str(exc), parent=self.root)
            return
        self.status.set('Sign-in setting saved.')
