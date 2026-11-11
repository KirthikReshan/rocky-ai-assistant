"""Background setup check with results delivered through the main UI queue."""

import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from rocky.readiness import check_setup


class ReadinessWindow:
    def __init__(self, root, events, worker, model, wake_path, offline, stt, voice_status):
        self.events = events
        self.worker = worker
        self.model = model
        self.wake_path = wake_path
        self.offline = offline
        self.stt = stt
        self.busy = False
        self.window = tk.Toplevel(root)
        self.window.title('Rocky — Setup status')
        self.window.geometry('680x580')
        self.window.minsize(520, 400)
        self.window.configure(background=root.cget('background'))
        self.window.protocol('WM_DELETE_WINDOW', self.window.withdraw)
        panel = ttk.Frame(self.window, padding=16)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='Setup status', font=('Segoe UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(panel, textvariable=voice_status, wraplength=470).pack(anchor='w', pady=8)
        self.output = ScrolledText(panel, wrap='word', state='disabled', height=16,
                                   font=('Segoe UI', 10), padx=10, pady=10)
        self.output.pack(fill='both', expand=True)
        self.output.tag_configure('heading', font=('Segoe UI', 10, 'bold'))
        self.button = ttk.Button(panel, text='Check again', command=self.refresh)
        self.button.pack(anchor='w', pady=(10, 0))
        ttk.Label(panel, text='These checks verify availability. Use the voice accuracy test to measure recognition.',
                  wraplength=470).pack(anchor='w', pady=(8, 0))
        self.refresh()

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        self.button.state(['disabled'])
        self.write([('Checking', 'Checking local AI, audio devices and speech files…')])
        input_device, output_device = self.worker.device, self.worker.output_device

        def check():
            try:
                result = check_setup(self.model, self.wake_path, input_device, output_device,
                                     self.offline, self.stt)
            except Exception as exc:
                result = [('Check failed', f'{exc}. Click Check again to retry.')]
            self.events.put(('ReadinessResult', result))

        threading.Thread(target=check, name='rocky-setup-check', daemon=True).start()

    def write(self, rows):
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        for title, detail in rows:
            self.output.insert('end', title + '\n', 'heading')
            self.output.insert('end', detail + '\n\n')
        self.output.configure(state='disabled')

    def handle(self, result):
        self.busy = False
        self.button.state(['!disabled'])
        self.write(result)

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.refresh()
