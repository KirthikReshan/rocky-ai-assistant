"""Embedded document workspace. Tk stays responsive during extraction and AI calls."""
import queue
import os
from pathlib import Path
import re
import threading
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter.scrolledtext import ScrolledText

from rocky.document_io import SUFFIXES
from rocky.document_export import export_result
from rocky.documents import DocumentResult, safe_document
from rocky.document_memory import DocumentText

OPERATIONS = ('Read / jump', 'Search words', 'Exact phrase', 'Find by meaning', 'Ask', 'Summarize',
              'Section summaries', 'Explain', 'Extract details', 'Find questions', 'Quiz', 'Flashcards',
              'Translate', 'Tables', 'Compare')


class DocumentsPage(ttk.Frame):
    def __init__(self, parent, service, worker, events):
        super().__init__(parent, padding=14)
        self.service, self.worker, self.events = service, worker, events
        self.running = False
        self.job = 0
        self.cancel_event = threading.Event()
        self.task_thread = None
        self.hits = []
        self.result = None
        self.files = []
        self.folder = tk.StringVar(value='Choose a folder or open a document to begin.')
        self.status = tk.StringVar()
        self.operation = tk.StringVar(value='Ask')
        self.query = tk.StringVar()
        self.scope = tk.StringVar()
        self.other = tk.StringVar()
        self.ocr = tk.BooleanVar(value=False)
        style = ttk.Style(self)
        style.configure('RockyDocuments.Treeview', background='#191929', fieldbackground='#191929',
                        foreground='#f4f2fc', rowheight=26)
        style.configure('RockyDocuments.Treeview.Heading', background='#303046', foreground='#f4f2fc')
        style.map('RockyDocuments.Treeview', background=[('selected', '#614a96')])
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky='ew')
        ttk.Label(header, text='Documents', font=('Segoe UI', 22, 'bold')).pack(side='left')
        self.open_button = ttk.Button(header, text='Open file', command=self.open_file)
        self.open_button.pack(side='right')
        self.folder_button = ttk.Button(header, text='Choose folder', command=self.choose_folder)
        self.folder_button.pack(side='right', padx=6)
        ttk.Label(self, textvariable=self.folder, wraplength=900).grid(row=1, column=0, sticky='ew', pady=6)
        controls = ttk.Frame(self)
        controls.grid(row=2, column=0, sticky='ew')
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text='Task').grid(row=0, column=0, sticky='w')
        ttk.Combobox(controls, textvariable=self.operation, values=OPERATIONS, state='readonly', width=22).grid(row=0, column=1, sticky='w', padx=8)
        ttk.Checkbutton(controls, text='OCR for scans / images', variable=self.ocr).grid(row=0, column=2, sticky='e')
        ttk.Label(controls, text='Question / phrase / language').grid(row=1, column=0, sticky='w', pady=5)
        query = ttk.Entry(controls, textvariable=self.query)
        query.grid(row=1, column=1, columnspan=2, sticky='ew', padx=(8, 0))
        query.bind('<Return>', lambda event: self.run())
        ttk.Label(controls, text='Part of document (optional)').grid(row=2, column=0, sticky='w')
        ttk.Entry(controls, textvariable=self.scope).grid(row=2, column=1, sticky='ew', padx=8)
        ttk.Label(controls, text='pages 2-5 · paragraph 3 · lines 10-20 · heading', wraplength=440).grid(row=3, column=1, columnspan=2, sticky='w', padx=8)
        ttk.Label(controls, text='Compare with').grid(row=4, column=0, sticky='w', pady=5)
        self.compare = ttk.Combobox(controls, textvariable=self.other, state='readonly')
        self.compare.grid(row=4, column=1, columnspan=2, sticky='ew', padx=(8, 0))
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky='ew', pady=(4, 8))
        self.run_button = ttk.Button(actions, text='Run task', style='Accent.TButton', command=self.run)
        self.run_button.grid(row=0, column=0, sticky='w')
        self.cancel_button = ttk.Button(actions, text='Cancel task', command=self.cancel, state='disabled')
        self.cancel_button.grid(row=0, column=1, padx=6, sticky='w')
        ttk.Button(actions, text='Search entire folder', command=self.search_folder).grid(row=0, column=2, sticky='w')
        ttk.Button(actions, text='Read selection / answer', command=self.read_aloud).grid(row=1, column=0, pady=5, sticky='w')
        ttk.Button(actions, text='Stop speaking', command=worker.stop_speaking).grid(row=1, column=1, padx=6, sticky='w')
        ttk.Button(actions, text='Export result', command=self.export).grid(row=1, column=2, sticky='w')
        ttk.Button(actions, text='Open original file', command=self.open_original).grid(row=2, column=0, sticky='w')
        ttk.Button(actions, text='Clear document memory', command=self.clear_folder).grid(row=2, column=1, columnspan=2, padx=6, sticky='w')
        panes = ttk.Panedwindow(self, orient='horizontal')
        panes.grid(row=4, column=0, sticky='nsew')
        left = ttk.Frame(panes)
        self.library = tk.Listbox(left, exportselection=False, width=26, background='#191929', foreground='#f4f2fc', selectbackground='#614a96')
        library_scroll = ttk.Scrollbar(left, command=self.library.yview)
        self.library.configure(yscrollcommand=library_scroll.set)
        library_scroll.pack(side='right', fill='y')
        self.library.pack(fill='both', expand=True)
        self.library.bind('<<ListboxSelect>>', self.choose_file)
        self.library.bind('<Double-Button-1>', lambda event: self.run(operation='Read / jump'))
        panes.add(left, weight=1)
        right = ttk.Notebook(panes)
        panes.add(right, weight=4)
        self.tabs = right
        source_tab, answer_tab = ttk.Frame(right), ttk.Frame(right)
        right.add(source_tab, text='Source passages')
        right.add(answer_tab, text='Answer / comparison')
        self.sources = ttk.Treeview(source_tab, columns=('location', 'source'), show='headings', height=4, selectmode='browse', style='RockyDocuments.Treeview')
        self.sources.heading('location', text='Source reference')
        self.sources.heading('source', text='File / section')
        self.sources.column('location', width=180, stretch=False)
        self.sources.column('source', width=350)
        self.sources.pack(fill='x')
        self.sources.bind('<<TreeviewSelect>>', self.show_source)
        self.source = ScrolledText(source_tab, wrap='word', height=12, background='#191929', foreground='#f4f2fc', insertbackground='white', font=('Segoe UI', 11))
        self.source.pack(fill='both', expand=True, pady=(6, 0))
        self.source.configure(state='disabled')
        self.source.tag_configure('match', background='#62438f')
        self.answer = ScrolledText(answer_tab, wrap='word', height=12, background='#191929', foreground='#f4f2fc', font=('Segoe UI', 11))
        self.answer.pack(fill='both', expand=True)
        self.answer.configure(state='disabled')
        self.answer.bind('<ButtonRelease-1>', self.follow_citation)
        ttk.Label(self, textvariable=self.status, wraplength=1100).grid(row=5, column=0, sticky='ew', pady=(8, 0))
        self.bind('<Configure>', self.resize)

    def resize(self, event):
        if event.widget is self:
            self.winfo_children()[-1].configure(wraplength=max(350, self.winfo_width() - 30))

    def set_folder(self, path, selected=None):
        if self.running:
            self.status.set('Cancel the current task before changing the folder.')
            return
        try:
            root = self.service.workspace.select(path)
            self.folder.set(str(root))
            self.service.selected = None
            self.service._cache.clear()
            self.hits, self.result = [], None
            self.sources.delete(*self.sources.get_children())
            self.put_text(self.source, '')
            self.put_text(self.answer, '')
            self.files = self.service.library()
            self.library.delete(0, 'end')
            for filename in self.files:
                self.library.insert('end', filename)
            self.compare.configure(values=self.files)
            self.other.set('')
            self.status.set(f'{len(self.files)} files shown. Folder listing is limited to 500 files.')
            if selected in self.files:
                index = self.files.index(selected)
                self.library.selection_set(index)
                self.library.see(index)
                self.choose_file()
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))

    def open_file(self):
        filename = filedialog.askopenfilename(parent=self, title='Open a document', filetypes=[('Supported documents', ' '.join('*' + suffix for suffix in sorted(SUFFIXES))), ('All files', '*.*')])
        if filename:
            path = Path(filename)
            if path.suffix.casefold() in {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp'}:
                self.ocr.set(True)
            self.set_folder(path.parent, path.name)

    def choose_folder(self):
        path = filedialog.askdirectory(parent=self, title='Choose a document folder')
        if path:
            self.set_folder(path)

    def clear_folder(self):
        self.cancel_event.set()
        status = self.service.clear_memory()
        self.worker.clear_document_messages() if callable(getattr(self.worker, 'clear_document_messages', None)) else None
        self.reset_memory_view(status)

    def reset_memory_view(self, status):
        self.files = []
        self.library.configure(state='normal')
        self.library.delete(0, 'end')
        self.compare.configure(values=[])
        self.other.set('')
        self.folder.set('Choose a folder or open a document to begin.')
        self.query.set('')
        self.scope.set('')
        self.display(DocumentResult('', [], status))

    def open_original(self):
        selected = self.sources.selection()
        relative = self.hits[int(selected[0]) - 1].relative if selected else self.service.selected
        if not relative or self.service.workspace.root is None:
            self.status.set('Select a source document first.')
            return
        try:
            path = safe_document(self.service.workspace.root, relative)
            os.startfile(path)
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))

    def choose_file(self, event=None):
        chosen = self.library.curselection()
        if chosen and chosen[0] < len(self.files):
            self.service.selected = self.files[chosen[0]]
            if not self.running:
                self.run(operation='Prepare document')

    def search_folder(self):
        if self.operation.get() not in {'Search words', 'Exact phrase'}:
            self.operation.set('Search words')
        self.run(folder=True)

    def run(self, operation=None, folder=False):
        if self.running:
            return
        operation = operation or self.operation.get()
        relative = '' if folder else self.service.selected or ''
        query, scope, other = self.query.get().strip(), self.scope.get().strip(), self.other.get()
        if len(query) > 500 or len(scope) > 200:
            self.status.set('Keep the request under 500 characters and the page/section selection under 200.')
            return
        if operation in {'Ask', 'Exact phrase', 'Search words', 'Find by meaning', 'Extract details', 'Translate'} and not query:
            self.status.set('Enter a question, phrase, subject or translation language first.')
            return
        identity = self.service.workspace.identity
        self.service.use_ocr = self.ocr.get()
        self.job += 1
        job = self.job
        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        self.set_busy(True)
        self.status.set('Reading documents…')

        def work():
            try:
                result = self.service.perform(operation, relative, query, scope, other, identity, cancel)
            except Exception as exc:
                result = str(exc)
            self.events.put(('DocumentTaskDone', (identity, job, result)))

        self.task_thread = threading.Thread(target=work, name='rocky-documents', daemon=True)
        self.task_thread.start()

    def set_busy(self, value):
        self.running = value
        for button in (self.open_button, self.folder_button, self.run_button):
            button.configure(state='disabled' if value else 'normal')
        self.library.configure(state='disabled' if value else 'normal')
        self.cancel_button.configure(state='normal' if value else 'disabled')

    def cancel(self):
        self.cancel_event.set()
        self.service.cancel()
        self.status.set('Cancelling the document task…')

    def close(self):
        self.cancel_event.set()
        self.service.cancel()
        # Give extraction its polling interval to stop its child process.
        # A model request may still be waiting on HTTP, so never block exit on it.
        if self.task_thread is not None:
            self.task_thread.join(timeout=1)
        self.service.close()

    def handle(self, kind, value):
        identity = value[0]
        if kind == 'DocumentTaskDone' and value[1] == self.job:
            self.set_busy(False)
        if identity != self.service.workspace.identity:
            return
        if kind == 'DocumentProgress':
            self.status.set(value[1])
        elif kind == 'DocumentTaskDone':
            if value[1] != self.job:
                return
            if isinstance(value[2], str):
                self.status.set(value[2])
            else:
                self.display(value[2])
        elif kind == 'DocumentAnswer':
            self.display(value[1])
        elif kind == 'DocumentResults':
            self.display(DocumentResult('', value[1], value[2]))

    @staticmethod
    def put_text(widget, text):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text)
        widget.configure(state='disabled')

    def display(self, result):
        self.hits = result.hits
        self.sources.delete(*self.sources.get_children())
        for index, hit in enumerate(self.hits, 1):
            self.sources.insert('', 'end', iid=str(index), values=(f'[S{index}] {hit.label}', f'{Path(hit.source).name} {hit.section}'))
        self.status.set(result.status or f'{len(self.hits)} source passages. Click an [S#] reference in the answer to view its source.')
        if result.answer:
            self.result = result
            self.put_text(self.answer, result.answer)
            self.tabs.select(1)
        else:
            self.result = result
            self.put_text(self.answer, '')
            self.tabs.select(0)
        if self.hits:
            self.sources.selection_set('1')
            self.show_source()
        else:
            self.put_text(self.source, 'No source passages found.')

    def show_source(self, event=None):
        selected = self.sources.selection()
        if not selected:
            return
        hit = self.hits[int(selected[0]) - 1]
        self.put_text(self.source, f'{hit.source}\n{hit.label} {hit.section}\n\n{hit.excerpt}')
        phrase = self.query.get().strip()
        if phrase:
            position = self.source.search(phrase, '1.0', nocase=True, stopindex='end')
            while position:
                end = f'{position}+{len(phrase)}c'
                self.source.tag_add('match', position, end)
                position = self.source.search(phrase, end, nocase=True, stopindex='end')

    def follow_citation(self, event):
        position = self.answer.index(f'@{event.x},{event.y}')
        line = self.answer.get(f'{position} linestart', f'{position} lineend')
        column = int(position.split('.')[1])
        for match in re.finditer(r'\[S(\d+)\]', line):
            if match.start() <= column <= match.end() and 1 <= int(match[1]) <= len(self.hits):
                self.sources.selection_set(match[1])
                self.sources.see(match[1])
                self.show_source()
                self.tabs.select(0)
                return

    def read_aloud(self):
        widget = self.source if self.tabs.index('current') == 0 else self.answer
        try:
            text = widget.get('sel.first', 'sel.last')
        except tk.TclError:
            text = widget.get('1.0', 'end-1c')
        if not text.strip():
            return
        if len(text) > 5000:
            self.status.set('Select up to 5,000 characters to read aloud.')
            return
        try:
            self.worker.announcements.put_nowait(DocumentText(text, self.service))
        except queue.Full:
            self.status.set('The speech queue is full. Try again after Rocky finishes.')

    def export(self):
        if self.result is None:
            self.status.set('Run a document task before exporting.')
            return
        destination = filedialog.asksaveasfilename(parent=self, title='Export document result', defaultextension='.docx',
                    filetypes=[('Word document', '*.docx'), ('Markdown', '*.md'), ('Text', '*.txt'), ('Table CSV', '*.csv')])
        if destination:
            try:
                export_result(destination, self.result.answer, self.result.hits)
                self.status.set(f'Saved {destination}')
            except (OSError, ValueError) as exc:
                self.status.set(str(exc))
