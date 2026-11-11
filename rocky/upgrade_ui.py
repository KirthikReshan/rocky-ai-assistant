"""Reviewable controls for calibration, routines, documents and PC approvals."""
import os
import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from rocky.calibration import advise
from rocky.documents import safe_document


def text_panel(parent, text, height=12):
    output = ScrolledText(parent, wrap='word', height=height, padx=10, pady=10)
    output.pack(fill='both', expand=True, pady=8)
    output.insert('1.0', text)
    output.configure(state='disabled')
    return output


def show_calibration(root, store, worker, events):
    try:
        input_index = worker.device
        try:
            import sounddevice as sd
            device = sd.query_devices(worker.device,'input')['name']
        except Exception:
            device = 'Unavailable input'
        report, changes = advise(store.voice_tests(),device=device,current_pause=worker.silence_seconds)
    except (ValueError, sqlite3.Error) as exc:
        messagebox.showerror('Calibration', str(exc), parent=root)
        return
    popup = tk.Toplevel(root)
    popup.title('Rocky — Personal voice calibration')
    popup.geometry('680x590')
    popup.minsize(520,450)
    panel = ttk.Frame(popup, padding=14)
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text='Personal voice calibration', font=('Segoe UI', 16, 'bold')).pack(anchor='w')
    ttk.Label(panel, text='Record phrases in Test microphone & voice accuracy, then reopen this report.', wraplength=600).pack(anchor='w')
    text_panel(panel, report + '\n\nProposed settings: ' + (str(changes) if changes else 'No changes recommended yet.'))
    def apply():
        try:
            same_device = worker.device == input_index and sd.query_devices(worker.device,'input')['name'] == device
        except Exception:
            same_device = False
        if not same_device:
            messagebox.showerror('Microphone changed','Reopen the calibration report for the current microphone before applying settings.',parent=popup)
            return
        try:
            store.set_settings(changes)
        except sqlite3.Error as exc:
            messagebox.showerror('Could not save calibration', str(exc), parent=popup)
            return
        if 'speech_pause' in changes:
            worker.silence_seconds = changes['speech_pause']
        if 'wake_sensitivity' in changes:
            from rocky.wake import SENSITIVITIES
            worker.wake_threshold = SENSITIVITIES[changes['wake_sensitivity']]
        events.put(('info', 'Calibration recommendations saved. Processor changes apply after restarting Rocky. Repeat the samples to compare.'))
        popup.destroy()
    ttk.Button(panel, text='Apply proposed settings', command=apply,
               state='normal' if changes else 'disabled').pack(anchor='w')


def show_pc_approval(root, proposal, service, events):
    popup = tk.Toplevel(root)
    popup.title('Rocky — ' + proposal.title)
    popup.geometry('620x450')
    popup.minsize(500,320)
    popup.transient(root)
    panel = ttk.Frame(popup, padding=14)
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text=proposal.title, font=('Segoe UI', 16, 'bold')).pack(anchor='w')
    text_panel(panel, proposal.details + '\n\nThis approval expires after 60 seconds and can be used once.')
    def reject():
        service.reject(proposal.id)
        popup.destroy()
    def approve():
        popup.destroy()
        def execute():
            try:
                result = service.approve(proposal.id)
                events.put(('info', result))
            except Exception as exc:
                events.put(('error', f'PC action failed: {exc}'))
        threading.Thread(target=execute, name='rocky-approved-pc-action', daemon=True).start()
    controls = ttk.Frame(panel)
    controls.pack(fill='x')
    ttk.Button(controls, text='Reject', command=reject).pack(side='right')
    ttk.Button(controls, text='Approve this action', command=approve).pack(side='right', padx=8)
    popup.protocol('WM_DELETE_WINDOW', reject)


class UpgradeUI:
    def __init__(self, root, store, worker, extensions, events):
        self.root, self.store, self.worker = root, store, worker
        self.extensions, self.events = extensions, events
        self.routine_window = None
        self.document_window = None
        self.hits = []

    def enqueue(self, command):
        try:
            self.worker.messages.put_nowait(command)
            self.events.put(('You', command))
        except queue.Full:
            self.events.put(('info', 'Please wait; the command queue is full.'))

    def routines(self):
        if self.routine_window is not None and self.routine_window.winfo_exists():
            self.routine_window.lift()
            return
        popup = self.routine_window = tk.Toplevel(self.root)
        popup.title('Rocky — Routines')
        popup.geometry('660x560')
        popup.minsize(520,440)
        form = ttk.Frame(popup, padding=14)
        form.pack(fill='both', expand=True)
        ttk.Label(form, text='Routines', font=('Segoe UI',16,'bold')).pack(anchor='w')
        ttk.Label(form, text='One command per line. App/folder/web opening and Google/YouTube commands only.\nExample: open VS Code\nopen D:\\MyProject\nopen https://github.com', wraplength=600).pack(anchor='w')
        listing = tk.Listbox(form, height=4, exportselection=False)
        listing.pack(fill='x', pady=8)
        name = ttk.Entry(form)
        name.pack(fill='x')
        name.insert(0, 'coding mode')
        commands = ScrolledText(form, height=8)
        commands.pack(fill='both', expand=True, pady=8)
        commands.insert('1.0', 'open VS Code\nopen https://github.com')
        status = tk.StringVar()
        def refresh():
            listing.delete(0,'end')
            for key in sorted(self.extensions.routines.saved()):
                listing.insert('end', key)
        def selected(event=None):
            if listing.curselection():
                key = listing.get(listing.curselection()[0])
                name.delete(0,'end')
                name.insert(0,key)
                commands.delete('1.0','end')
                saved = self.extensions.routines.saved().get(key)
                if isinstance(saved,list) and all(isinstance(value,str) for value in saved):
                    commands.insert('1.0','\n'.join(saved))
        def save():
            try:
                self.extensions.routines.save(name.get(), [line.strip() for line in commands.get('1.0','end').splitlines() if line.strip()])
                refresh()
                status.set('Routine saved. Say run routine ' + name.get().strip() + '.')
            except (ValueError, sqlite3.Error) as exc:
                status.set(str(exc))
        def remove():
            key = name.get().strip().casefold()
            try:
                saved = self.extensions.routines.saved()
                if key in saved and messagebox.askyesno('Remove routine', f'Remove {key}?',parent=popup):
                    self.extensions.routines.remove(key)
                    refresh()
            except sqlite3.Error as exc:
                status.set(str(exc))
        controls = ttk.Frame(form)
        controls.pack(fill='x')
        ttk.Button(controls,text='Save routine',command=save).pack(side='left')
        ttk.Button(controls,text='Run saved routine',command=lambda:self.enqueue('run routine '+name.get().strip())).pack(side='left',padx=6)
        ttk.Button(controls,text='Remove',command=remove).pack(side='left')
        ttk.Label(form,textvariable=status,wraplength=600).pack(anchor='w',pady=6)
        listing.bind('<<ListboxSelect>>',selected)
        refresh()

    def documents(self):
        if getattr(self, 'open_documents_page', None) is not None:
            self.open_documents_page()
            return
        if self.document_window is not None and self.document_window.winfo_exists():
            self.document_window.deiconify()
            self.document_window.lift()
            return
        popup = self.document_window = tk.Toplevel(self.root)
        popup.title('Rocky — Documents and notes')
        popup.geometry('850x650')
        popup.minsize(640,500)
        panel = ttk.Frame(popup,padding=14)
        panel.pack(fill='both',expand=True)
        self.folder = tk.StringVar(value=str(self.extensions.documents.workspace.root or 'No document folder selected. Saved notes can still be searched.'))
        ttk.Label(panel,text='Documents and notes',font=('Segoe UI',16,'bold')).pack(anchor='w')
        ttk.Label(panel,textvariable=self.folder,wraplength=780).pack(anchor='w',pady=6)
        controls = ttk.Frame(panel)
        controls.pack(fill='x')
        def choose():
            path = filedialog.askdirectory(parent=popup,title='Choose a document folder')
            if path:
                try:
                    root = self.extensions.documents.workspace.select(path)
                    self.folder.set(str(root))
                    self.clear_results()
                except (OSError,ValueError) as exc:
                    self.document_status.set(str(exc))
        def clear():
            self.extensions.documents.workspace.clear()
            self.folder.set('No document folder selected. Saved notes can still be searched.')
            self.clear_results()
        ttk.Button(controls,text='Choose folder',command=choose).pack(side='left')
        ttk.Button(controls,text='Clear folder',command=clear).pack(side='left',padx=6)
        query = ttk.Entry(panel)
        query.pack(fill='x',pady=8)
        actions = ttk.Frame(panel)
        actions.pack(fill='x')
        def submit_document(prefix):
            subject = query.get().strip()
            if not subject or len(subject) > 500:
                self.document_status.set('Enter a subject, question or relative filename, up to 500 characters.')
                return
            self.enqueue(prefix+subject)
        ttk.Button(actions,text='Search',command=lambda:submit_document('search documents for ')).pack(side='left')
        ttk.Button(actions,text='Ask AI using sources',command=lambda:submit_document('ask documents: ')).pack(side='left',padx=6)
        ttk.Button(actions,text='Summarize relative file',command=lambda:submit_document('summarize document ')).pack(side='left')
        ttk.Label(panel,text='Search by subject, or enter a relative filename for a summary. PDF, UTF-8 text and Markdown; no OCR.',wraplength=780).pack(anchor='w',pady=6)
        self.results = tk.Listbox(panel,height=5,exportselection=False)
        self.results.pack(fill='x')
        self.excerpt = text_panel(panel,'Search results and source excerpts will appear here.',height=10)
        self.document_status = tk.StringVar()
        ttk.Label(panel,textvariable=self.document_status,wraplength=780).pack(anchor='w')
        ttk.Button(panel,text='Open selected source',command=self.open_source).pack(anchor='w',pady=6)
        def select(event=None):
            chosen = self.results.curselection()
            if chosen and chosen[0] < len(self.hits):
                self.excerpt.configure(state='normal')
                self.excerpt.delete('1.0','end')
                hit = self.hits[chosen[0]]
                self.excerpt.insert('1.0',f'{hit.source} — page {hit.page}\n\n{hit.excerpt}')
                self.excerpt.configure(state='disabled')
        self.results.bind('<<ListboxSelect>>',select)

    def clear_results(self):
        self.hits = []
        self.results.delete(0,'end')
        self.excerpt.configure(state='normal')
        self.excerpt.delete('1.0','end')
        self.excerpt.configure(state='disabled')

    def document_results(self, value):
        identity, hits, status = value
        if identity != self.extensions.documents.workspace.identity:
            return
        self.documents()
        self.clear_results()
        self.hits = hits
        for hit in hits:
            self.results.insert('end',f'{hit.source} — page {hit.page}')
        self.document_status.set(status)
        if hits:
            self.results.selection_set(0)
            self.results.event_generate('<<ListboxSelect>>')

    def open_source(self):
        selected = self.results.curselection()
        if not selected:
            return
        hit = self.hits[selected[0]]
        if hit.relative is None:
            popup = tk.Toplevel(self.document_window)
            popup.title(hit.source)
            text_panel(popup,hit.excerpt)
            return
        try:
            root = self.extensions.documents.workspace.root
            if root is None:
                raise ValueError('Select the document folder again.')
            path = safe_document(root,hit.relative)
            if str(path) != hit.source:
                raise ValueError('Document selection changed. Search again.')
            os.startfile(path)
        except (OSError,ValueError) as exc:
            self.document_status.set(str(exc))
