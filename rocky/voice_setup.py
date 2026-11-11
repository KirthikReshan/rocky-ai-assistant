"""Microphone check and repeatable speech benchmark UI."""

import queue
import sqlite3
import tkinter as tk
from tkinter import ttk

from rocky.voice_benchmark import PHRASES, words


class VoiceSetup:
    def __init__(self, parent, worker, store):
        self.worker = worker
        self.store = store
        self.busy = False
        self.guided = None
        self.guided_index = 0
        self.prompt = tk.StringVar(value=PHRASES[0])
        self.environment = tk.StringVar(value='Quiet room')
        self.status = tk.StringVar(value='Choose a phrase, click Record phrase, then speak when the status says speak now.')
        self.summary = tk.StringVar()
        self.audio = tk.StringVar(value='Microphone level appears during recording.')
        scroll = ttk.Scrollbar(parent, orient='vertical')
        scroll.pack(side='right', fill='y')
        canvas = tk.Canvas(parent, highlightthickness=0, background=parent.winfo_toplevel().cget('background'),
                           yscrollcommand=scroll.set)
        canvas.pack(fill='both', expand=True)
        scroll.configure(command=canvas.yview)
        parent = ttk.Frame(canvas, padding=4)
        window = canvas.create_window(0, 0, anchor='nw', window=parent)
        parent.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        ttk.Label(parent, text='Test your microphone', font=('Segoe UI', 16, 'bold')).pack(anchor='w', pady=(20, 6))
        ttk.Label(parent, text='Each test records six seconds locally. Test phrases do not run commands.',
                  wraplength=430).pack(anchor='w')
        self.meter = ttk.Progressbar(parent, maximum=100)
        self.meter.pack(fill='x', pady=6)
        ttk.Label(parent, textvariable=self.audio).pack(anchor='w')
        self.phrases = ttk.Combobox(parent, textvariable=self.prompt, values=PHRASES)
        self.phrases.current(0)
        self.phrases.pack(fill='x', pady=(10, 6))
        self.mode = ttk.Combobox(parent, state='readonly',
                                 values=('Transcript and command', 'Wake word: say Hey Rocky', 'Wake word: unrelated speech'))
        self.mode.current(0)
        self.mode.pack(fill='x', pady=(0, 6))

        def select_mode(event=None):
            if self.mode.current() == 1:
                self.prompt.set('Hey Rocky')
            elif self.mode.current() == 2:
                self.prompt.set('Could you tell me the time?')

        self.mode.bind('<<ComboboxSelected>>', select_mode)
        self.room = ttk.Combobox(parent, textvariable=self.environment,
                                 values=('Quiet room', 'Background noise'), state='readonly')
        self.room.pack(anchor='w')
        controls = ttk.Frame(parent)
        controls.pack(fill='x', pady=8)
        self.level_button = ttk.Button(controls, text='Check mic level', command=lambda: self.start(False))
        self.level_button.pack(side='left')
        self.record_button = ttk.Button(controls, text='Record phrase', command=lambda: self.start(True))
        self.record_button.pack(side='left', padx=6)
        ttk.Button(controls, text='Next phrase', command=self.next_phrase).pack(side='left')
        ttk.Button(parent,text='Start guided personal calibration',command=self.start_guided).pack(anchor='w',pady=4)
        ttk.Label(parent,text='The guide covers five commands in quiet and noisy conditions, then positive and negative wake samples. '
                  'Click Record phrase for each step; audio is discarded after scoring.',wraplength=440).pack(anchor='w')
        ttk.Label(parent, textvariable=self.status, wraplength=440).pack(anchor='w')
        ttk.Label(parent, textvariable=self.summary, wraplength=440).pack(anchor='w', pady=8)
        ttk.Label(parent, text='Command scores compare the interpreted action without executing it. '
                  'Wake tests compare the baseline detector with your current confidence and verification settings.',
                  wraplength=440).pack(anchor='w')
        self.refresh_summary()

    def next_phrase(self):
        if self.busy:
            return
        if self.guided is not None:
            self.advance_guided()
            return
        # The list deliberately repeats the wake phrase. Looking up its text
        # always returns the first occurrence and traps Next at that position.
        self.phrases.current((self.phrases.current() + 1) % len(PHRASES))

    def start_guided(self):
        if self.busy:
            return
        self.guided = [(0,phrase,room) for room in ('Quiet room','Background noise') for phrase in PHRASES[:5]]
        self.guided += [(1,'Hey Rocky','Quiet room')] * 4
        self.guided += [(2,phrase,'Quiet room') for phrase in ('Could you tell me the time?', 'Open the window please',
                                                             'That song sounds nice', 'I am going for a walk')]
        self.guided_index = 0
        self.show_guided_step()

    def show_guided_step(self):
        mode, phrase, room = self.guided[self.guided_index]
        self.mode.current(mode)
        self.prompt.set(phrase)
        self.environment.set(room)
        self.status.set(f'Calibration {self.guided_index + 1}/{len(self.guided)} — {room}. Click Record phrase, then speak when ready.')

    def advance_guided(self):
        self.guided_index += 1
        if self.guided_index >= len(self.guided):
            self.guided = None
            self.status.set('Guide completed. Open Personal calibration report in Settings to review the measurements.')
        else:
            self.show_guided_step()

    def start(self, recognition):
        if self.busy:
            return
        expected = self.prompt.get().strip() if recognition else ''
        if recognition and (not words(expected) or len(expected) > 500):
            self.status.set('Enter a short phrase with words, up to 500 characters.')
            return
        kind = ('command', 'wake_positive', 'wake_negative')[self.mode.current()]
        try:
            aliases = self.store.setting('app_nicknames', {})
        except sqlite3.Error:
            aliases = {}
        request = {'expected': expected, 'environment': self.environment.get(), 'test_kind': kind,
                   'nicknames': aliases if isinstance(aliases, dict) else {}}
        try:
            self.worker.tests.put_nowait(request)
        except queue.Full:
            self.status.set('A microphone test is already queued.')
            return
        self.busy = True
        self.level_button.state(['disabled'])
        self.record_button.state(['disabled'])
        for control in (self.phrases, self.mode, self.room):
            control.state(['disabled'])
        self.status.set('Queued. Wait for “speak now” before speaking.')

    def handle(self, kind, value):
        if kind == 'TestStarted':
            self.status.set('Speak now. Recording six seconds.')
            return
        if kind == 'TestLevel':
            self.meter['value'] = min(100, value['peak'] * 100)
            self.audio.set(f"Peak {value['peak'] * 100:.0f}% · Clipped samples {value['clipped'] * 100:.1f}%")
            self.status.set('Speak now. Recording six seconds.')
            return
        self.busy = False
        self.level_button.state(['!disabled'])
        self.record_button.state(['!disabled'])
        for control in (self.phrases, self.mode, self.room):
            control.state(['!disabled'])
        if kind == 'TestError':
            self.status.set('Test failed. ' + value)
            return
        warning = ('Audio was interrupted. Fix the input device and repeat.' if value['overflow'] else
                   'Input is clipping. Reduce microphone gain and repeat.' if value['clipped'] > .01 else
                   'Input was very quiet. Move closer or check microphone gain.' if value['peak'] < .02 else
                   'Recording completed.')
        if not value['expected']:
            self.status.set(warning)
            return
        try:
            self.store.add_voice_test(value)
            self.refresh_summary()
            saved = 'Result saved.'
            recorded = True
        except sqlite3.Error:
            saved = 'Could not save the result.'
            recorded = False
        self.status.set(f"Result “{value['heard'] or '(no speech recognized)'}”. "
                        f"{value.get('engine', 'CPU')}, {value['latency']:.1f}s transcription. {warning} {saved}")
        if (self.guided is not None and recorded and not value['overflow']
                and value['clipped'] <= .01 and value['peak'] >= .02):
            self.advance_guided()

    def refresh_summary(self):
        try:
            rows = self.store.voice_tests()
        except sqlite3.Error:
            self.summary.set('Saved results are unavailable.')
            return
        if not rows:
            self.summary.set('No recorded phrases yet. Read several phrases in each room condition.')
            return
        commands = [row for row in rows if row['test_kind'] == 'command']
        errors = sum(row['word_errors'] for row in commands)
        total = sum(row['reference_words'] for row in commands)
        accuracy = max(0, 1 - errors / max(1, total)) * 100
        exact = sum(row['exact'] for row in commands)
        timings = []
        for engine in sorted({row['engine'] for row in rows}):
            subset = [row for row in rows if row['engine'] == engine]
            timings.append(f"{engine}: {sum(row['latency'] for row in subset) / len(subset):.2f}s average "
                           f"({len(subset)} tests)")
        scored = [row for row in commands if row['action_correct'] is not None]
        action_text = (f"Interpreted commands: {sum(row['action_correct'] for row in scored)}/{len(scored)} correct."
                       if scored else 'No command outcomes measured yet.')
        transcript = (f'Word accuracy {accuracy:.0f}% · Exact transcripts {exact}/{len(commands)}.'
                      if commands else 'No transcript tests yet.')
        wake = []
        for kind, label in [('wake_positive', 'Hey Rocky detected'), ('wake_negative', 'False activations')]:
            for config in sorted({row['wake_config'] for row in rows if row['test_kind'] == kind}):
                subset = [row for row in rows if row['test_kind'] == kind and row['wake_config'] == config]
                wake.append(f"{label}: {sum(row['wake_detected'] or 0 for row in subset)}/{len(subset)}; "
                            f"baseline {sum(row['wake_baseline'] or 0 for row in subset)}/{len(subset)} ({config}).")
        self.summary.set(f'Last {len(rows)} tests. {transcript}\n{action_text}\n' +
                         '\n'.join(wake) + '\n' + ' · '.join(timings))
