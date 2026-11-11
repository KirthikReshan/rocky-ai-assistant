"""Resizable text viewer with scrolling in both directions."""
import tkinter as tk
from tkinter import ttk


class TextView(ttk.Frame):
    def __init__(self, parent, *, wrap='word', **options):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        options.setdefault('width', 1)
        options.setdefault('height', 1)
        self.text = tk.Text(self, wrap=wrap, **options)
        self.vertical = ttk.Scrollbar(self, orient='vertical', command=self.text.yview)
        self.horizontal = ttk.Scrollbar(self, orient='horizontal', command=self.text.xview)
        self.text.configure(yscrollcommand=self.vertical.set, xscrollcommand=self.horizontal.set)
        self.text.grid(row=0, column=0, sticky='nsew')
        self.vertical.grid(row=0, column=1, sticky='ns')
        self.horizontal.grid(row=1, column=0, sticky='ew')
        self.set_wrap(wrap != 'none')

    def set_wrap(self, enabled):
        self.text.configure(wrap='word' if enabled else 'none')
        if enabled:
            self.horizontal.grid_remove()
            self.text.xview_moveto(0)
        else:
            self.horizontal.grid()
