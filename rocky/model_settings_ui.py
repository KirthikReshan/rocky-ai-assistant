"""Scrollable model settings. Edits apply on the next Rocky launch."""
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk

from rocky.brain import BrainError
from rocky.model_settings import ModelSettings, CredentialVault
from rocky.model_transport import installed_models


class ModelSettingsPage(ttk.Frame):
    def __init__(self, parent, store, documents, events):
        super().__init__(parent)
        style = ttk.Style(self)
        style.configure('RockyModels.TCombobox', fieldbackground='#25253a', background='#25253a',
                        foreground='#f4f2fc', arrowcolor='#f4f2fc', insertcolor='#f4f2fc')
        style.map('RockyModels.TCombobox', fieldbackground=[('!disabled', '#25253a')],
                  foreground=[('!disabled', '#f4f2fc')])
        style.configure('RockyModels.TCheckbutton', background='#11111f', foreground='#f4f2fc')
        style.map('RockyModels.TCheckbutton', background=[('active', '#191929')])
        self.store, self.documents, self.events = store, documents, events
        self.vault = CredentialVault(store.path)
        self.config = ModelSettings.load(store)
        self.status = tk.StringVar()
        self.variables = {}
        scroll = ttk.Scrollbar(self, orient='vertical')
        scroll.pack(side='right', fill='y')
        canvas = tk.Canvas(self, background='#11111f', highlightthickness=0, yscrollcommand=scroll.set)
        canvas.pack(fill='both', expand=True)
        scroll.configure(command=canvas.yview)
        form = ttk.Frame(canvas, padding=18)
        window = canvas.create_window(0, 0, anchor='nw', window=form)
        form.columnconfigure(1, weight=1)
        form.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind('<MouseWheel>', lambda event: canvas.yview_scroll(-int(event.delta / 120), 'units'))
        ttk.Label(form, text='AI models', font=('Segoe UI', 22, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Label(form, text='Choose models for each task. Save, then reopen Rocky to apply changes.', wraplength=650).grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 12))
        self.pickers = {}
        for row, (name, title) in enumerate((('chat', 'Conversation'), ('code', 'Coding'), ('documents', 'Document answers'),
                                           ('translation', 'Translation specialist'), ('embedding', 'Document search'), ('fallback', 'Local backup')), 2):
            ttk.Label(form, text=title).grid(row=row, column=0, sticky='w', pady=5)
            variable = self.variables[name] = tk.StringVar(value=getattr(self.config, name))
            picker = ttk.Combobox(form, style='RockyModels.TCombobox', textvariable=variable, values=sorted({getattr(self.config, name), 'qwen3.5:4b', 'qwen3:4b', 'translategemma:4b', 'embeddinggemma:latest'}))
            picker.grid(row=row, column=1, sticky='ew', padx=(14, 0))
            self.pickers[name] = picker
        self.use_index = tk.BooleanVar(value=True)
        ttk.Label(form, text='Document memory lasts for this session only.').grid(row=8, column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Label(form, text='Opened documents are prepared in RAM before questions. Closing Rocky or clearing document memory '
                  'removes extracted text, embeddings and document answers. Original files stay on disk. '
                  'Translation language examples: Tamil, or Tamil to English.', wraplength=650).grid(row=9, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Button(form, text='Check installed local models', command=self.check_models).grid(row=10, column=0, sticky='w', pady=(0, 12))
        ttk.Button(form, text='Clear document memory', command=self.clear_index).grid(row=10, column=1, sticky='w', padx=14, pady=(0, 12))
        ttk.Label(form, text='Optional NVIDIA NIM', font=('Segoe UI', 15, 'bold')).grid(row=11, column=0, columnspan=2, sticky='w', pady=(8, 4))
        ttk.Label(form, text='Online AI sends the selected code or document passages and your question to NVIDIA. '
                  'Conversation, translation and search remain local. Enable only the tasks you want online. '
                  'Local models are used if NIM fails. Account limits and availability depend on NVIDIA.', wraplength=650).grid(row=12, column=0, columnspan=2, sticky='w', pady=6)
        self.nim_code = tk.BooleanVar(value=self.config.nim_code)
        self.nim_documents = tk.BooleanVar(value=self.config.nim_documents)
        ttk.Checkbutton(form, style='RockyModels.TCheckbutton', text='Use online NIM for coding and edit proposals', variable=self.nim_code).grid(row=13, column=0, columnspan=2, sticky='w')
        ttk.Checkbutton(form, style='RockyModels.TCheckbutton', text='Use online NIM for document analysis, including study tools', variable=self.nim_documents).grid(row=14, column=0, columnspan=2, sticky='w')
        ttk.Label(form, text='NIM model ID').grid(row=15, column=0, sticky='w', pady=6)
        self.nim_model = tk.StringVar(value=self.config.nim_model)
        ttk.Entry(form, textvariable=self.nim_model).grid(row=15, column=1, sticky='ew', padx=(14, 0))
        ttk.Label(form, text='New NVIDIA API key').grid(row=16, column=0, sticky='w')
        self.key = tk.StringVar()
        ttk.Entry(form, textvariable=self.key, show='•').grid(row=16, column=1, sticky='ew', padx=(14, 0))
        ttk.Label(form, text='Keys are saved in Windows Credential Manager. Leave the field empty to keep the saved key.', wraplength=650).grid(row=17, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Button(form, text='Remove saved NVIDIA key', command=self.remove_key).grid(row=18, column=0, columnspan=2, sticky='w')
        ttk.Button(form, text='Save for next launch', command=self.save, style='Accent.TButton').grid(row=19, column=0, columnspan=2, sticky='w', pady=(16, 8))
        ttk.Label(form, textvariable=self.status, wraplength=650).grid(row=20, column=0, columnspan=2, sticky='w')
        def wheel(event):
            canvas.yview_scroll(-int(event.delta / 120), 'units')
            return 'break'
        def bind_wheel(widget):
            widget.bind('<MouseWheel>', wheel, add='+')
            for child in widget.winfo_children():
                bind_wheel(child)
        bind_wheel(form)

    def save(self):
        try:
            config = ModelSettings(**{name: var.get().strip() for name, var in self.variables.items()},
                use_index=self.use_index.get(), nim_code=self.nim_code.get(), nim_documents=self.nim_documents.get(),
                nim_model=self.nim_model.get().strip()).validate()
            key = self.key.get().strip()
            if (config.nim_code or config.nim_documents) and not key and not self.vault.read():
                raise ValueError('Enter your NVIDIA API key before enabling online tasks.')
            if key:
                self.vault.save(key)
                self.key.set('')
            config.save(self.store)
            self.status.set('Saved. Close and reopen Rocky to use these settings.')
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.status.set(str(exc))

    def remove_key(self):
        try:
            self.vault.delete()
            from dataclasses import replace
            replace(ModelSettings.load(self.store), nim_code=False, nim_documents=False).save(self.store)
            self.key.set('')
            self.nim_code.set(False)
            self.nim_documents.set(False)
            self.status.set('Saved key removed and online tasks disabled for next launch. Current requests may finish; new NIM requests will fall back locally.')
        except (OSError, sqlite3.Error) as exc:
            self.status.set(str(exc))

    def check_models(self):
        self.status.set('Checking the local Ollama installation…')
        def work():
            try:
                names = sorted(installed_models())
                self.events.put(('AISettingsResult', ('models', names)))
            except BrainError as exc:
                self.events.put(('AISettingsResult', ('error', str(exc))))
        threading.Thread(target=work, name='rocky-model-check', daemon=True).start()

    def clear_index(self):
        try:
            self.status.set(self.documents.clear_memory())
        except (sqlite3.Error, OSError):
            self.status.set('Could not clear the search index. Close other Rocky instances and retry.')

    def handle(self, result):
        kind, value = result
        if kind == 'models':
            for picker in self.pickers.values():
                picker.configure(values=value)
            self.status.set('Installed locally: ' + ', '.join(value) if value else 'No local models installed. Download them with ollama pull in PowerShell.')
        else:
            self.status.set(value)
