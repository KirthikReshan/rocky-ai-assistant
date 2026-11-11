"""User controls for optional local AI conversation memory."""

import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

from rocky.brain import validated_history


def add_memory_controls(parent, root, brain, store):
    enabled = tk.BooleanVar(value=brain.memory_enabled if brain is not None else False)
    ttk.Label(parent, text='Conversation memory', font=('Segoe UI', 13, 'bold')).pack(anchor='w', pady=(16, 4))

    def toggle():
        try:
            brain.set_memory_enabled(enabled.get())
        except sqlite3.Error as exc:
            enabled.set(brain.memory_enabled)
            messagebox.showerror('Memory setting', str(exc), parent=root)

    control = ttk.Checkbutton(parent, text='Remember recent chats on this laptop', variable=enabled, command=toggle)
    control.pack(anchor='w')
    if brain is None:
        control.state(['disabled'])
    ttk.Label(parent, text='Stores up to four recent AI exchanges when enabled. '
              'Turning it off clears saved chats. Code-tab excerpts are kept out of this memory.',
              wraplength=420).pack(anchor='w', pady=(4, 8))

    def review():
        popup = tk.Toplevel(root)
        popup.title('Rocky — Conversation memory')
        popup.geometry('650x450')
        text = ScrolledText(popup, wrap='word', padx=12, pady=12)
        text.pack(fill='both', expand=True)
        try:
            history = validated_history(brain.history_snapshot() if brain is not None else store.setting('chat_history', []))
            for message in history:
                text.insert('end', f"{message['role'].title()}\n{message['content']}\n\n")
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            text.insert('end', 'Saved conversation could not be read.')
        text.configure(state='disabled')

    def clear():
        if messagebox.askyesno('Clear conversation', 'Clear the current AI conversation and its saved memory?', parent=root):
            try:
                if brain is not None:
                    brain.clear_history()
                else:
                    store.set_setting('chat_history', [])
            except sqlite3.Error as exc:
                messagebox.showerror('Could not clear memory', str(exc), parent=root)

    controls = ttk.Frame(parent)
    controls.pack(anchor='w')
    ttk.Button(controls, text='Review recent chat', command=review).pack(side='left')
    ttk.Button(controls, text='Clear conversation', command=clear).pack(side='left', padx=6)
