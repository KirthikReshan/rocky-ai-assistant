import argparse
import queue
import sqlite3
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter.scrolledtext import ScrolledText
from pathlib import Path
from rocky.text_view import TextView

from rocky.voice import VoiceWorker
from rocky.assistant import Assistant
from rocky.brain import OllamaBrain
from rocky.task_brain import TaskBrain
from rocky.model_settings import ModelSettings
from rocky.model_settings_ui import ModelSettingsPage
from rocky.storage import Store
from rocky.reminders import ReminderCommands
from rocky.calendar_service import start_service, service_compatible,stop_service
from rocky.calendar_startup import CalendarStartup
from rocky.calendar_page import CalendarPage
from rocky.documents_page import DocumentsPage
from rocky.document_memory import DocumentText, clear_document_transcript
from rocky.app_instance import AppInstance,database_key
from rocky.calendar_ui import display_time
from rocky.coding import Workspace, CodeCommands
from rocky.ui_state import MicrophoneControl
from rocky.editing import EditService
from rocky.editing_ui import show_edit_preview
from rocky.voice_setup import VoiceSetup
from rocky.audio_preferences import PAUSE_CHOICES, VOCABULARY_LIMIT, device_key, resolve_device, speech_pause, speech_vocabulary
from rocky.tools import DesktopTools
from rocky.app_nicknames import show_nicknames
from rocky.readiness_ui import ReadinessWindow
from rocky.memory_ui import add_memory_controls
from rocky.version import VERSION
from rocky.wake import SENSITIVITIES, is_stop_phrase
from rocky.extensions import Extensions
from rocky.upgrade_ui import UpgradeUI, show_pc_approval, show_calibration
from rocky.toast import ToastQueue


def arguments():
    parser = argparse.ArgumentParser(description="Rocky local voice and desktop assistant")
    parser.add_argument("--model", type=Path, default=Path(__file__).resolve().parents[1] / "models/vosk-model-small-en-us-0.15")
    parser.add_argument("--device", type=int, help="Input device index from python -m sounddevice")
    parser.add_argument("--brain-model", help="Override the saved conversation model for this launch")
    parser.add_argument("--offline", action="store_true", help="Basic replies only, without Ollama")
    parser.add_argument("--stt", choices=("auto", "vosk"), default="auto", help="Use local Whisper when installed, or force Vosk")
    parser.add_argument("--timezone", default="Asia/Kolkata", help="Reminder timezone (IANA name)")
    database=parser.add_mutually_exclusive_group()
    database.add_argument("--data-dir", type=Path, help="Optional local folder for notes/reminders database")
    database.add_argument('--database',type=Path,help='Exact reminders database for the Calendar launcher')
    parser.add_argument('--calendar-page',action='store_true',help='Open the Calendar page inside Rocky')
    parser.add_argument("--code-folder", type=Path, help="Optional project folder for read-only code assistance")
    parser.add_argument("--follow-up", type=int, default=30, choices=range(5, 301), metavar="5-300", help="Seconds to listen for follow-up speech")
    return parser.parse_args()


def main():
    args=arguments()
    store=Store(args.database or (args.data_dir/'rocky.db' if args.data_dir else None))
    instance=AppInstance(store.path)
    try:
        if not instance.primary:
            instance.request(calendar=args.calendar_page)
            return
        run_app(args,store,instance)
    finally:
        instance.close()


def run_app(args,store,instance):
    root = tk.Tk()
    root.title(f"Rocky {VERSION} — Local AI assistant")
    root.geometry("900x620")
    root.minsize(700, 500)
    root.state('zoomed')
    background, surface, card = '#11111f', '#191929', '#25253a'
    foreground, muted, violet, mint = '#f4f2fc', '#b9b7d2', '#a98aff', '#72eac8'
    root.configure(background=background)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('TFrame', background=background)
    style.configure('TLabel', background=background, foreground=foreground)
    style.configure('TButton', background=card, foreground=foreground, bordercolor='#42415e',
                    padding=(10, 6), font=('Segoe UI', 10))
    style.map('TButton', background=[('pressed', '#504275'), ('active', '#393550')])
    style.configure('Accent.TButton', background='#6244a4', foreground='#ffffff')
    style.map('Accent.TButton', background=[('pressed', '#4f3589'), ('active', '#7956c3')])
    style.configure('TEntry', fieldbackground=card, foreground=foreground, insertcolor=foreground,
                    padding=7)
    style.configure('TCombobox', foreground=foreground, background=card, arrowcolor=foreground)
    style.map('TCombobox', fieldbackground=[('readonly', card)])
    events = queue.Queue()
    notifications = ToastQueue(root)
    reminders = ReminderCommands(store, args.timezone, require_advanced_calendar=True)
    brain = None if args.offline else TaskBrain(
        store, chat_override=args.brain_model, on_partial=lambda text: events.put(('ReplyDraft', text)), memory_store=store,
        notice=lambda kind, text: events.put((kind, text)))
    workspace = Workspace()
    if args.code_folder:
        try:
            workspace.select(args.code_folder)
        except (ValueError, OSError) as exc:
            events.put(('error', f'Could not select the code folder: {exc}'))
    editor = EditService(workspace, brain, store.path.parent / 'code-backups')
    coding = CodeCommands(workspace, brain, display=lambda content: events.put(('Code', content)),
                          editor=editor, preview=lambda proposal: events.put(('EditProposal', proposal)))
    tools = DesktopTools(store)
    extensions = Extensions(store, tools, brain, events)
    assistant = Assistant(brain, tools=tools, reminders=reminders, coding=coding, extensions=extensions)
    worker = VoiceWorker(events, args.model, args.device, assistant, args.follow_up, args.stt)
    upgrades = UpgradeUI(root, store, worker, extensions, events)
    microphone_control = MicrophoneControl(worker)
    status = tk.StringVar(value="Starting")
    voice_status = tk.StringVar(value='Voice is starting…')
    recognition_backend = tk.StringVar(value='Recognition engine is starting…')
    readiness = None

    def open_readiness():
        nonlocal readiness
        if readiness is None:
            readiness = ReadinessWindow(root, events, worker, brain.model if brain else (args.brain_model or ModelSettings.load(store).chat), args.model,
                                        args.offline, args.stt, voice_status)
        else:
            readiness.show()

    def open_calendar(show=True):
        try:
            start_service(store.path, show=False)
            if show:
                select_page('Calendar',calendar_panel)
                calendar_view.refresh(force=True)
        except OSError as exc:
            events.put(('error', f'Could not start reminder alerts: {exc}'))

    root.columnconfigure(1, weight=1)
    root.rowconfigure(0, weight=1)
    sidebar = tk.Frame(root, background=surface, padx=12, pady=18)
    sidebar.grid(row=0, column=0, sticky='ns')
    tk.Label(sidebar, text='Rocky', background=surface, foreground=violet,
             font=('Segoe UI', 25, 'bold')).pack(anchor='w', pady=(0, 20), padx=6)
    tk.Label(sidebar, text='v' + VERSION, background=surface, foreground=muted,
             font=('Segoe UI', 9)).pack(side='bottom', anchor='w', pady=6)
    content = ttk.Frame(root)
    content.grid(row=0, column=1, sticky='nsew')
    content.columnconfigure(0, weight=1)
    content.rowconfigure(0, weight=1)
    conversation_panel, code_panel, audio_page = [ttk.Frame(content, padding=16) for _ in range(3)]
    calendar_panel=ttk.Frame(content)
    document_panel = DocumentsPage(content, extensions.documents, worker, events)
    ai_panel = ModelSettingsPage(content, store, extensions.documents, events)
    model_status = tk.StringVar(value=('Local · ' + brain.model) if brain else 'AI disabled for this launch')
    ttk.Label(root, textvariable=model_status, foreground=mint, wraplength=1000, padding=(12, 4)).grid(row=1, column=0, columnspan=3, sticky='ew')
    for panel in (conversation_panel, code_panel, audio_page,calendar_panel, document_panel, ai_panel):
        panel.grid(row=0, column=0, sticky='nsew')
    try:
        calendar_startup=CalendarStartup(store.path,database_key(store.path))
    except Exception:
        calendar_startup=None
    def pause_reminders():
        events.put(('info','Reminder alerts paused.' if stop_service(store.path) else 'Reminder alerts are already stopped.'))
    calendar_view=CalendarPage(calendar_panel,store,args.timezone,startup=calendar_startup,
                               on_notify=lambda message:notifications.post(message,title='Rocky · Calendar'),
                               on_start=lambda:open_calendar(show=False),on_pause=pause_reminders,
                               service_status=lambda:service_compatible(store.path))
    calendar_view.pack(fill='both',expand=True)
    audio_scroll = ttk.Scrollbar(audio_page, orient='vertical')
    audio_scroll.pack(side='right', fill='y')
    audio_canvas = tk.Canvas(audio_page, background=background, highlightthickness=0,
                              yscrollcommand=audio_scroll.set)
    audio_canvas.pack(fill='both', expand=True)
    audio_scroll.configure(command=audio_canvas.yview)
    audio_panel = ttk.Frame(audio_canvas)
    audio_window = audio_canvas.create_window(0, 0, anchor='nw', window=audio_panel)
    audio_panel.bind('<Configure>', lambda event: audio_canvas.configure(scrollregion=audio_canvas.bbox('all')))
    audio_canvas.bind('<Configure>', lambda event: audio_canvas.itemconfigure(audio_window, width=event.width))
    audio_canvas.bind('<MouseWheel>', lambda event: audio_canvas.yview_scroll(-int(event.delta / 120), 'units'))

    upcoming = tk.Frame(root, background=surface, width=260, padx=14, pady=20)
    upcoming.grid(row=0, column=2, sticky='ns')
    upcoming.pack_propagate(False)
    tk.Label(upcoming, text='Upcoming', background=surface, foreground=foreground,
             font=('Segoe UI', 17, 'bold')).pack(anchor='w')
    agenda = tk.Frame(upcoming, background=surface)
    agenda.pack(fill='x', pady=14)
    ttk.Button(upcoming, text='Open Calendar', command=open_calendar).pack(fill='x')
    navigation = {}
    page = 'Dashboard'

    def resize(event=None):
        if event is not None and event.widget is not root:
            return
        if page == 'Dashboard' and root.winfo_width() >= 1000:
            upcoming.grid()
        else:
            upcoming.grid_remove()

    def select_page(name, panel):
        nonlocal page
        page = name
        panel.tkraise()
        for title, control in navigation.items():
            control.configure(style='Accent.TButton' if title == name else 'TButton')
        resize()

    for name, panel in [('Dashboard', conversation_panel), ('Chat', conversation_panel),
                        ('Code', code_panel), ('Settings', audio_page),('Calendar',calendar_panel), ('Documents', document_panel), ('AI models', ai_panel)]:
        navigation[name] = ttk.Button(sidebar, text=name, width=11,
                                      command=lambda n=name, p=panel: select_page(n, p))
        navigation[name].pack(fill='x', pady=(0, 6))
    root.bind('<Configure>', resize)
    select_page('Dashboard', conversation_panel)
    upgrades.open_documents_page = lambda: select_page('Documents', document_panel)
    def refresh_calendar():
        if calendar_panel.winfo_ismapped():
            calendar_view.refresh()
        root.after(1500,refresh_calendar)

    agenda_items = None

    def refresh_agenda():
        nonlocal agenda_items
        if upcoming.winfo_ismapped():
            try:
                rows = store.reminders(limit=3)
                items = tuple((row['body'][:100] + (' · ' + row['repeat'].title() if row['repeat'] != 'once' else '') +
                               (' · Snoozed' if row['snoozed_from'] is not None else ''),
                               display_time(row['due_at'], row['timezone'])) for row in rows)
            except (sqlite3.Error, ValueError, KeyError):
                items = (('Could not load reminders.', ''),)
            if items != agenda_items:
                for child in agenda.winfo_children():
                    child.destroy()
                for title, when in items or (('No upcoming reminders.', ''),):
                    tile = tk.Frame(agenda, background=card, padx=10, pady=8)
                    tile.pack(fill='x', pady=(0, 7))
                    tk.Label(tile, text=title, background=card, foreground=foreground, anchor='w',
                             justify='left', wraplength=210, font=('Segoe UI', 11, 'bold')).pack(fill='x')
                    if when:
                        tk.Label(tile, text=when, background=card, foreground=muted,
                                 anchor='w', font=('Segoe UI', 9)).pack(fill='x')
                agenda_items = items
        root.after(2000, refresh_agenda)

    ttk.Label(conversation_panel, text="Hey, I’m Rocky", font=('Segoe UI', 25, 'bold')).pack(anchor='w')
    voice_bar = ttk.Frame(conversation_panel)
    voice_bar.pack(fill='x', pady=(8, 10))
    wave = tk.Canvas(voice_bar, width=118, height=32, background=background, highlightthickness=0)
    wave.pack(side='left')
    bars = [wave.create_line(6 + index * 8, 16 - height, 6 + index * 8, 16 + height,
                             width=4, capstyle='round')
            for index, height in enumerate((2, 4, 7, 5, 9, 12, 7, 13, 9, 5, 7, 4, 2))]
    ttk.Label(voice_bar, textvariable=status, foreground=mint, wraplength=280).pack(side='left', padx=8)

    def update_wave(*_):
        state = status.get()
        active = state.startswith(('Listening', 'Speaking'))
        for index, bar in enumerate(bars):
            wave.itemconfigure(bar, fill=(mint if 4 <= index <= 8 else violet) if active else '#514867')

    status.trace_add('write', update_wave)
    update_wave()
    ttk.Label(code_panel, text='Code & previews', font=('Segoe UI', 19, 'bold')).pack(anchor='w', pady=(0, 12))
    ttk.Label(audio_panel, text='Audio settings', font=('Segoe UI', 19, 'bold')).pack(anchor='w', pady=(0, 12))

    try:
        import sounddevice as sd
        devices = list(sd.query_devices())
        hostapis = sd.query_hostapis()
        inputs = [(i, device['name']) for i, device in enumerate(devices)
                  if device['max_input_channels'] > 0]

        def read_preference(name, default=None):
            try:
                return store.setting(name, default)
            except sqlite3.Error:
                events.put(('info', 'Saved audio settings could not be read. Using defaults.'))
                return default

        def save_preference(name, value):
            try:
                store.set_setting(name, value)
                return True
            except (sqlite3.Error, OSError):
                events.put(('error', 'Could not save this setting. It will not persist after restart.'))
                return False

        saved_input = read_preference('microphone')
        saved_output = read_preference('speaker')
        if args.device is None:
            worker.device = resolve_device(saved_input, devices, hostapis, 'input')
        worker.output_device = resolve_device(saved_output, devices, hostapis, 'output')
        if (saved_input is not None and worker.device is None and args.device is None
                or saved_output is not None and worker.output_device is None):
            events.put(('info', 'A saved audio device is unavailable or ambiguous. Using Windows default for it.'))
        worker.silence_seconds = speech_pause(read_preference('speech_pause', 1.5))
        worker.speech_vocabulary = speech_vocabulary(read_preference('speech_vocabulary', ''))
        saved_engine = read_preference('recognition_device', 'cpu')
        worker.recognition_device = saved_engine if saved_engine in ('cpu', 'cuda') else 'cpu'
        sensitivity = read_preference('wake_sensitivity', 'Balanced')
        if not isinstance(sensitivity, str) or sensitivity not in SENSITIVITIES:
            sensitivity = 'Balanced'
        worker.wake_threshold = SENSITIVITIES[sensitivity]
        worker.verify_wake = read_preference('verify_wake', False) is True
        worker.voice_interrupt_enabled = read_preference('voice_interrupt', False) is True
        ttk.Label(audio_panel, text="Microphone").pack(anchor="w")
        picker = ttk.Combobox(audio_panel, state="readonly",
                             values=['Windows default'] + [f"{i}: {name}" for i, name in inputs])
        picker.pack(fill="x", pady=(4, 14))
        picker.current(0)
        for pos, (index, _) in enumerate(inputs):
            if index == worker.device:
                picker.current(pos + 1)

        def select_microphone(event=None):
            position = picker.current()
            index, name = inputs[position - 1] if position > 0 else (None, 'Windows default')
            worker.device = index
            save_preference('microphone', device_key(devices[index], hostapis) if index is not None else None)
            events.put(("info", f"Selected microphone: {name}"))

        picker.bind("<<ComboboxSelected>>", select_microphone)
        outputs = [(i, device['name']) for i, device in enumerate(devices)
                   if device['max_output_channels'] > 0]
        ttk.Label(audio_panel, text="Speaker output").pack(anchor="w")
        output_picker = ttk.Combobox(audio_panel, state="readonly",
                                    values=['Windows default'] + [f"{i}: {name}" for i, name in outputs])
        output_picker.pack(fill="x", pady=4)
        output_picker.current(0)
        for pos, (index, _) in enumerate(outputs):
            if index == worker.output_device:
                output_picker.current(pos + 1)

        def select_output(event=None):
            position = output_picker.current()
            index, name = outputs[position - 1] if position > 0 else (None, 'Windows default')
            worker.output_device = index
            save_preference('speaker', device_key(devices[index], hostapis) if index is not None else None)
            events.put(("info", f"Selected speaker output: {name}"))

        output_picker.bind("<<ComboboxSelected>>", select_output)
        ttk.Label(audio_panel, text='Wait after speech').pack(anchor='w', pady=(14, 4))
        pause_picker = ttk.Combobox(audio_panel, state='readonly',
                                   values=[f'{value:.1f} seconds' for value in PAUSE_CHOICES], width=18)
        pause_picker.current(PAUSE_CHOICES.index(worker.silence_seconds))
        pause_picker.pack(anchor='w')

        def select_pause(event=None):
            worker.silence_seconds = PAUSE_CHOICES[pause_picker.current()]
            save_preference('speech_pause', worker.silence_seconds)
            events.put(('info', f'Speech pause set to {worker.silence_seconds:.1f} seconds for the next sentence.'))

        pause_picker.bind('<<ComboboxSelected>>', select_pause)
        ttk.Label(audio_panel, text='Longer waits leave room for pauses inside a sentence. '
                  'Applies to Whisper speech recognition. Device choices and this setting save automatically.',
                  wraplength=420).pack(anchor='w', pady=(6, 0))
        ttk.Label(audio_panel, text='Speech recognition processor').pack(anchor='w', pady=(14, 4))
        engine_picker = ttk.Combobox(audio_panel, state='readonly', width=32,
                                    values=['CPU', 'NVIDIA GPU with CPU fallback'])
        engine_picker.current(1 if worker.recognition_device == 'cuda' else 0)
        engine_picker.pack(anchor='w')
        ttk.Label(audio_panel, textvariable=recognition_backend, wraplength=420).pack(anchor='w', pady=4)

        def select_engine(event=None):
            requested = 'cuda' if engine_picker.current() == 1 else 'cpu'
            if save_preference('recognition_device', requested):
                events.put(('info', 'Recognition processor saved. Restart Rocky to apply it.'))

        engine_picker.bind('<<ComboboxSelected>>', select_engine)
        ttk.Label(audio_panel, text='Restart Rocky after changing the processor. '
                  'GPU mode uses video memory alongside Ollama and other apps.',
                  wraplength=420).pack(anchor='w')
        ttk.Label(audio_panel, text='Speech vocabulary').pack(anchor='w', pady=(14, 4))
        vocabulary = tk.StringVar(value=worker.speech_vocabulary)
        vocabulary_entry = ttk.Entry(audio_panel, textvariable=vocabulary, width=40)
        vocabulary_entry.pack(fill='x')
        vocabulary_status = tk.StringVar(value='Add names or phrases Rocky often mishears, separated by commas.')

        def save_vocabulary(event=None):
            value = vocabulary.get()
            normalized = speech_vocabulary(value)
            if max(len(value), len(normalized)) > VOCABULARY_LIMIT:
                vocabulary_status.set(f'Use up to {VOCABULARY_LIMIT} characters for the most useful terms.')
                return
            if save_preference('speech_vocabulary', normalized):
                worker.speech_vocabulary = normalized
                vocabulary.set(normalized)
                vocabulary_status.set('Saved for upcoming speech.' if normalized else 'Custom hints cleared.')
            else:
                vocabulary_status.set('Could not save speech vocabulary.')

        vocabulary_entry.bind('<Return>', save_vocabulary)
        ttk.Button(audio_panel, text='Save vocabulary', command=save_vocabulary).pack(anchor='w', pady=4)
        ttk.Label(audio_panel, textvariable=vocabulary_status, wraplength=420).pack(anchor='w')
        ttk.Label(audio_panel, text='Wake-word sensitivity').pack(anchor='w', pady=(14, 4))
        wake_picker = ttk.Combobox(audio_panel, state='readonly', values=list(SENSITIVITIES), width=20)
        wake_picker.set(sensitivity)
        wake_picker.pack(anchor='w')

        def select_sensitivity(event=None):
            worker.wake_threshold = SENSITIVITIES[wake_picker.get()]
            save_preference('wake_sensitivity', wake_picker.get())

        wake_picker.bind('<<ComboboxSelected>>', select_sensitivity)
        verify = tk.BooleanVar(value=worker.verify_wake)
        interruption = tk.BooleanVar(value=worker.voice_interrupt_enabled)

        def select_verification():
            worker.verify_wake = verify.get()
            save_preference('verify_wake', verify.get())

        def select_interruption():
            worker.voice_interrupt_enabled = interruption.get()
            save_preference('voice_interrupt', interruption.get())

        ttk.Checkbutton(audio_panel, text='Double-check Hey Rocky with Whisper', variable=verify,
                        command=select_verification).pack(anchor='w', pady=(6, 0))
        ttk.Label(audio_panel, text='Verification adds a recognition pass after the wake word. '
                  'Use the positive and negative wake tests to compare it with the baseline.',
                  wraplength=420).pack(anchor='w')
        ttk.Checkbutton(audio_panel, text='Voice stop while speaking (headset)', variable=interruption,
                        command=select_interruption).pack(anchor='w', pady=(10, 0))
        ttk.Label(audio_panel, text='Say “wait”, “change that” or “stop speaking” during a reply. '
                  'For a new question, say “Hey Rocky” and the question together. Applies to the next reply. '
                  'The echo filter ignores stop phrases in Rocky’s own reply. '
                  'With loud speakers, the Stop speaking button is the more predictable control.',
                  wraplength=420).pack(anchor='w')
    except Exception as exc:
        events.put(("info", f"Microphone picker unavailable: {exc}"))
    voice_setup = None
    setup_window = None

    def open_voice_setup():
        nonlocal voice_setup, setup_window
        if setup_window is None:
            setup_window = tk.Toplevel(root)
            setup_window.title('Rocky — Voice accuracy')
            setup_window.geometry('580x630')
            setup_window.minsize(510, 600)
            setup_window.configure(background=background)
            setup_window.protocol('WM_DELETE_WINDOW', setup_window.withdraw)
            setup_panel = ttk.Frame(setup_window, padding=18)
            setup_panel.pack(fill='both', expand=True)
            voice_setup = VoiceSetup(setup_panel, worker, store)
        setup_window.deiconify()
        setup_window.lift()

    ttk.Button(audio_panel, text='Test microphone & voice accuracy', command=open_voice_setup).pack(anchor='w', pady=16)
    ttk.Button(audio_panel, text='Test notification',
               command=lambda:notifications.post('This message popup closes automatically after four seconds.',
                                                 title='Rocky · Notification test')).pack(anchor='w',pady=(0,8))
    ttk.Button(audio_panel, text='Personal calibration report',
               command=lambda: show_calibration(root,store,worker,events)).pack(anchor='w',pady=(0,8))
    ttk.Button(audio_panel, text='Routines',command=upgrades.routines).pack(anchor='w',pady=(0,8))
    ttk.Button(audio_panel, text='Documents & notes',command=upgrades.documents).pack(anchor='w',pady=(0,8))
    ttk.Label(audio_panel,text='PC controls: say close Notepad, restart Windows, or shut down Windows. '
              'Each request opens an approval dialog. Cancel shutdown aborts a Windows countdown.',wraplength=420).pack(anchor='w',pady=8)
    ttk.Button(audio_panel, text='App nicknames', command=lambda: show_nicknames(root, store)).pack(anchor='w')
    ttk.Button(audio_panel, text='Check setup status', command=open_readiness).pack(anchor='w', pady=(10, 0))
    ttk.Button(audio_panel, text='AI models', command=lambda: select_page('AI models', ai_panel)).pack(anchor='w', pady=(10, 0))
    add_memory_controls(audio_panel, root, brain, store)
    folder_info = tk.StringVar(value=str(workspace.root) if workspace.root else 'No code folder selected')
    code_controls = ttk.Frame(code_panel)
    code_controls.pack(fill='x', pady=(0, 8))

    def choose_code_folder():
        chosen = filedialog.askdirectory(parent=root, title='Choose a code project (edits require approval)')
        if chosen:
            try:
                selected = workspace.select(chosen)
                folder_info.set(str(selected))
                events.put(('info', f'Code folder selected: {selected}. Edits require preview approval.'))
            except (OSError, ValueError) as exc:
                events.put(('error', str(exc)))

    def clear_code_folder():
        workspace.clear()
        coding.index.clear()
        folder_info.set('No code folder selected')
        code_output.configure(state='normal')
        code_output.delete('1.0', 'end')
        code_output.configure(state='disabled')

    ttk.Button(code_controls, text='Choose code folder', command=choose_code_folder).pack(side='left')
    ttk.Button(code_controls, text='Clear selection', command=clear_code_folder).pack(side='left', padx=6)
    def refresh_index():
        try:
            worker.messages.put_nowait('refresh project index')
            events.put(('You', 'Refresh project index'))
        except queue.Full:
            status.set('Please wait; message queue is full')

    ttk.Button(code_controls, text='Refresh index', command=refresh_index).pack(side='left')
    code_wrap = tk.BooleanVar(value=True)
    ttk.Checkbutton(code_controls, text='Wrap text', variable=code_wrap,
                    command=lambda: code_view.set_wrap(code_wrap.get())).pack(side='right')
    ttk.Label(code_panel, textvariable=folder_info, wraplength=570).pack(fill='x', pady=(0, 8))
    code_question_bar = ttk.Frame(code_panel)
    code_question_bar.pack(side='bottom', fill='x', pady=(10, 0))
    code_question = ttk.Entry(code_question_bar)

    def ask_project(event=None):
        question = code_question.get().strip()
        if not question:
            return
        try:
            worker.messages.put_nowait('ask my code: ' + question)
        except queue.Full:
            status.set('Please wait; message queue is full')
            return
        events.put(('You', 'Ask my code: ' + question))
        code_question.delete(0, 'end')

    ttk.Button(code_question_bar, text='Ask project', command=ask_project).pack(side='right', padx=(6, 0))
    code_question.pack(side='left', fill='x', expand=True)
    code_question.bind('<Return>', ask_project)
    quick_actions = ttk.Frame(conversation_panel)
    quick_actions.pack(fill='x', pady=(0, 10))
    composer = ttk.Frame(conversation_panel)
    composer.pack(side='bottom', fill='x', pady=(12, 0))
    log = ScrolledText(conversation_panel, state="disabled", wrap="word", background=surface,
                       foreground=foreground, font=('Segoe UI', 11), padx=14, pady=12,
                       relief='flat', spacing3=8, height=8)
    log.tag_configure('you', foreground='#f4edff', background='#4c3676', justify='right',
                      lmargin1=35, lmargin2=35, rmargin=10, spacing1=8)
    log.tag_configure('rocky', foreground='#d5fff1', background='#23413e',
                      lmargin1=10, lmargin2=10, rmargin=35, spacing1=8)
    log.tag_configure('error', foreground='#ffa7b2')
    log.pack(fill="both", expand=True)
    draft_panel = ttk.Frame(conversation_panel)
    ttk.Label(draft_panel, text='Rocky is replying…', foreground=mint).pack(anchor='w')
    draft_output = ScrolledText(draft_panel, height=3, state='disabled', wrap='word',
                                font=('Segoe UI', 11), background=surface, foreground=foreground,
                                relief='flat', padx=10, pady=6)
    draft_output.pack(fill='x')
    code_view = TextView(code_panel, state='disabled', font=('Consolas', 10),
                         background=surface, foreground=foreground, relief='flat', padx=8, pady=6)
    code_output = code_view.text
    code_view.pack(fill='both', expand=True)
    entry = ttk.Entry(composer, font=('Segoe UI', 11))

    def submit(text):
        if is_stop_phrase(text):
            worker.stop_speaking()
            events.put(('You', text))
            return True
        try:
            worker.messages.put_nowait(text)
        except queue.Full:
            status.set("Please wait; message queue is full")
            return False
        events.put(("You", text))
        return True

    def send(event=None):
        text = entry.get().strip()
        if text and submit(text):
            entry.delete(0, "end")

    def pause():
        microphone_control.toggle()
        button.configure(text=microphone_control.label)

    for app in ('WhatsApp', 'VS Code', 'Codex'):
        ttk.Button(quick_actions, text=app, command=lambda name=app: submit(f'Open {name}')).pack(side='left', padx=(0, 6))
    stop_button = ttk.Button(quick_actions, text='Stop speaking', command=worker.stop_speaking, state='disabled')
    stop_button.pack(side='right')
    ttk.Button(composer, text='Send', command=send, style='Accent.TButton').pack(side='right', padx=(6, 0))
    button = ttk.Button(composer, text=microphone_control.label, command=pause)
    button.pack(side='right', padx=(6, 0))
    entry.pack(side='left', fill='x', expand=True)
    entry.bind("<Return>", send)
    entry.focus_set()

    open_calendar(show=False)
    if args.calendar_page:
        select_page('Calendar',calendar_panel)
    def check_calendar_version():
        if not service_compatible(store.path):
            events.put(('info','An older reminder worker is still running. Quit it from the Calendar tray menu, then choose Start alerts on the Calendar page.'))
    root.after(5000,check_calendar_version)

    def quit_app():
        extensions.pc.cancel_pending()
        worker.stopping.set()
        root.quit()

    app_menu = tk.Menu(root)
    rocky_menu = tk.Menu(app_menu, tearoff=False)
    rocky_menu.add_command(label='Quit Rocky', command=quit_app)
    app_menu.add_cascade(label='Rocky', menu=rocky_menu)
    root.configure(menu=app_menu)

    def poll():
        if instance.take(calendar=True):
            select_page('Calendar',calendar_panel)
            calendar_view.refresh(force=True)
            root.deiconify()
            root.lift()
            root.focus_force()
        if instance.take():
            root.deiconify()
            root.lift()
            root.focus_force()
        for _ in range(100):
            try:
                kind, text = events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                status.set(text)
            elif kind == 'AIModel':
                model_status.set(text)
            elif kind == 'AISettingsResult':
                ai_panel.handle(text)
            elif kind == 'DocumentMemoryCleared':
                identity, detail = text
                if identity == extensions.documents.workspace.identity:
                    document_panel.reset_memory_view(detail)
                    worker.clear_document_messages()
                    clear_document_transcript(log)
            elif kind == 'microphone':
                microphone_control.update(text)
                button.configure(text=microphone_control.label)
            elif kind == 'speaking':
                stop_button.state(['!disabled'] if text else ['disabled'])
            elif kind == 'VoiceReady':
                voice_status.set(f"Voice: {text['speech']} · Recognition: {text['recognition']} · "
                                 f"Wake word: {'loaded' if text['wake'] else 'unavailable'}")
            elif kind == 'SpeechEngine':
                recognition_backend.set(f'Active speech recognition: {text}')
            elif kind == 'ReadinessResult':
                if readiness is not None:
                    readiness.handle(text)
            elif kind == 'PCApproval':
                show_pc_approval(root,text,extensions.pc,events)
            elif kind in {'DocumentResults', 'DocumentProgress', 'DocumentAnswer', 'DocumentTaskDone'}:
                document_panel.handle(kind, text)
                if kind == 'DocumentAnswer' and text[0] == extensions.documents.workspace.identity:
                    select_page('Documents', document_panel)
            elif kind == 'ReplyDraft':
                if text:
                    draft_panel.pack(side='bottom', fill='x', pady=(8, 0), before=log)
                    draft_output.configure(state='normal')
                    draft_output.delete('1.0', 'end')
                    draft_output.insert('1.0', text)
                    draft_output.see('end')
                    draft_output.configure(state='disabled')
                else:
                    draft_panel.pack_forget()
            elif kind in {'TestStarted', 'TestLevel', 'TestResult', 'TestError'}:
                if voice_setup is not None:
                    voice_setup.handle(kind, text)
            elif kind == 'EditProposal':
                try:
                    show_edit_preview(root, text, editor, events)
                except tk.TclError as exc:
                    editor.discard(text.id)
                    events.put(('error', f'Could not show edit preview; nothing applied: {exc}'))
            elif kind == 'Code':
                code_output.configure(state='normal')
                code_output.delete('1.0', 'end')
                code_output.insert('1.0', text)
                code_output.configure(state='disabled')
                select_page('Code', code_panel)
            else:
                if isinstance(text, DocumentText) and not text.current():
                    continue
                if kind == 'error':
                    notifications.post(text,title='Rocky · Attention',error=True)
                elif kind == 'info' and text.startswith(('Calibration recommendations saved','Windows countdown',
                                                         'Asked the selected windows','Code folder selected')):
                    notifications.post(text,title='Rocky')
                log.configure(state="normal")
                tag = kind.lower()
                tags = (tag, 'document-memory') if isinstance(text, DocumentText) else (tag,)
                log.insert("end", f"{kind}: {text}\n", tags)
                log.see("end")
                log.configure(state="disabled")
        root.after(100, poll)

    root.protocol("WM_DELETE_WINDOW", quit_app)
    worker.start()
    poll()
    refresh_agenda()
    refresh_calendar()
    try:
        root.mainloop()
    finally:
        worker.stopping.set()
        document_panel.close()
        notifications.close()
        root.destroy()


if __name__ == "__main__":
    main()
