"""Manage user-chosen names for installed Windows apps."""

import sqlite3
import tkinter as tk
from tkinter import messagebox, ttk

from rocky.tools import APPS, app_path, installed_app_id


def show_nicknames(root, store):
    try:
        saved = store.setting('app_nicknames', {})
    except sqlite3.Error as exc:
        messagebox.showerror('App nicknames', str(exc), parent=root)
        return
    aliases = saved if isinstance(saved, dict) else {}
    popup = tk.Toplevel(root)
    popup.title('Rocky — App nicknames')
    popup.geometry('530x420')
    popup.minsize(490, 400)
    popup.configure(background=root.cget('background'))
    panel = ttk.Frame(popup, padding=16)
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text='Give an installed app a name you like.', wraplength=460).pack(anchor='w')
    ttk.Label(panel, text='For example, nickname “my editor” and app “VS Code”.',
              wraplength=460).pack(anchor='w', pady=(0, 8))
    listing = tk.Listbox(panel, height=6, exportselection=False)
    listing.pack(fill='both', expand=True)
    ttk.Label(panel, text='Nickname').pack(anchor='w', pady=(8, 0))
    nickname = ttk.Entry(panel)
    nickname.pack(fill='x')
    ttk.Label(panel, text='Installed app name').pack(anchor='w')
    target = ttk.Entry(panel)
    target.pack(fill='x')

    def refresh():
        listing.delete(0, 'end')
        for name, value in sorted(aliases.items()):
            listing.insert('end', f'{name} → {value}')

    def persist(updated):
        try:
            store.set_setting('app_nicknames', updated)
        except sqlite3.Error as exc:
            messagebox.showerror('Could not save nicknames', str(exc), parent=popup)
            return False
        aliases.clear()
        aliases.update(updated)
        refresh()
        return True

    def save():
        name, app = nickname.get().strip().casefold(), target.get().strip()
        try:
            if not name or not app or len(name) > 80 or len(app) > 120:
                raise ValueError('Enter a nickname and an installed app name.')
            if any(ord(char) < 32 for char in name + app):
                raise ValueError('Use a single line for each name.')
            if app.casefold() in APPS:
                app_path(APPS[app.casefold()])
            else:
                installed_app_id(app)
        except (ValueError, OSError) as exc:
            messagebox.showerror('Could not save nickname', str(exc), parent=popup)
            return
        if persist(dict(aliases, **{name: app})):
            nickname.delete(0, 'end')
            target.delete(0, 'end')

    def remove():
        selected = listing.curselection()
        if selected:
            name = sorted(aliases)[selected[0]]
            persist({key: value for key, value in aliases.items() if key != name})

    def select(event=None):
        selected = listing.curselection()
        if selected:
            name = sorted(aliases)[selected[0]]
            nickname.delete(0, 'end')
            nickname.insert(0, name)
            target.delete(0, 'end')
            target.insert(0, aliases[name])

    listing.bind('<<ListboxSelect>>', select)
    controls = ttk.Frame(panel)
    controls.pack(fill='x', pady=(10, 0))
    ttk.Button(controls, text='Save nickname', command=save).pack(side='left')
    ttk.Button(controls, text='Remove selected', command=remove).pack(side='left', padx=6)
    refresh()
    return popup
