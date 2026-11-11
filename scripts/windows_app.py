"""Windowed entry point for the Rocky Windows build."""

import json
import os
import sys
from pathlib import Path


def prepare_windowed_streams():
    """Pythonw/PyInstaller provide no console; library diagnostics still need write()."""
    for name in ('stdout', 'stderr'):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))


def youtube_worker(result_file, query):
    # The frozen exe is windowed: an uncaught worker exception otherwise opens
    # a PyInstaller error dialog, leaving the parent waiting for its timeout.
    try:
        from rocky.youtube_search import search

        result = search(query)
    except Exception:
        return 1
    Path(result_file).write_text(json.dumps(result), encoding='utf-8')
    return 0


if __name__ == '__main__':
    prepare_windowed_streams()
    if sys.argv[1:2] == ['--calendar']:
        del sys.argv[1]
        from rocky.calendar_service import main

        main()
    elif sys.argv[1:2] == ['--youtube-worker']:
        sys.exit(youtube_worker(sys.argv[2], sys.argv[3]))
    elif sys.argv[1:2] == ['--document-worker']:
        from rocky.documents import pdf_worker
        pdf_worker(*sys.argv[2:6])
    elif sys.argv[1:2] == ['--code-view-check']:
        # Exercise layout and scrollbar commands without opening a visible window.
        import tkinter as tk
        from rocky.text_view import TextView
        from rocky.version import VERSION
        root = tk.Tk()
        root.withdraw()
        checks = {}
        try:
            view = TextView(root, font=('Consolas', 10))
            content = ('A long explanation with words ' * 100 + '\n' + 'x' * 2000 + '\n' +
                       '\n'.join(f'Line {number}' for number in range(200)))
            view.text.insert('1.0', content)
            view.text.configure(state='disabled')
            lines = {}
            for width in (640, 900, 1600):
                view.place(x=0, y=0, width=width, height=400)
                view.set_wrap(True)
                root.update_idletasks()
                lines[width] = view.text.count('1.0', '2.0', 'displaylines')[0]
                checks[f'wrapped_{width}'] = view.text.xview() == (0.0, 1.0)
                view.set_wrap(False)
                root.update_idletasks()
                command = root.tk.splitlist(view.horizontal.cget('command'))
                root.tk.call(*command, 'moveto', 1)
                root.update_idletasks()
                left, right = view.text.xview()
                checks[f'horizontal_scroll_{width}'] = left > 0 and right >= .999
            command = root.tk.splitlist(view.vertical.cget('command'))
            root.tk.call(*command, 'moveto', 1)
            root.update_idletasks()
            checks['vertical_scroll'] = view.text.yview()[0] > 0 and view.text.yview()[1] >= .999
            checks['reflows_on_resize'] = lines[640] > lines[900] > lines[1600] > 1
            checks['text_preserved'] = view.text.get('1.0', 'end-1c') == content
            checks['read_only'] = view.text.cget('state') == 'disabled'
        finally:
            root.destroy()
        Path(sys.argv[2]).write_text(json.dumps(dict(version=VERSION, checks=checks)), encoding='utf-8')
        if not all(checks.values()):
            sys.exit(1)
    elif sys.argv[1:2] == ['--models-check']:
        # Isolated local check: no user documents or cloud credentials.
        import queue
        import re
        import tempfile
        import tkinter as tk
        import win32cred
        from rocky.storage import Store
        from rocky.task_brain import TaskBrain
        from rocky.documents import Documents
        from rocky.model_settings_ui import ModelSettingsPage
        from rocky.model_settings import ModelSettings
        from rocky.version import VERSION
        checks = {}
        try:
            with tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                store = Store(base / 'test.db')
                brain = TaskBrain(store)
                events = queue.Queue()
                service = Documents(store, brain, events)
                service.workspace.select(base)
                (base / 'project.txt').write_text('The delivery deadline is Sunday, 15 November 2026. '
                    'Parts arrived late. The budget is 500 dollars.', encoding='utf-8')
                value = service.perform('Translate', 'project.txt', 'Tamil')
                checks['tamil_specialist'] = bool(re.search('[\u0b80-\u0bff]', value.answer)) and '500' in value.answer
                value = service.perform('Find by meaning', 'project.txt', 'Why was delivery delayed?')
                checks['embedding_search'] = bool(value.hits) and any('Parts arrived late' in hit.excerpt for hit in value.hits)
                value = service.perform('Ask', 'project.txt', 'What is the deadline and budget?')
                checks['document_answer'] = '500' in value.answer and '[S1]' in value.answer
                checks['cloud_disabled'] = not brain.config.nim_code and not brain.config.nim_documents
                checks['credential_library'] = callable(win32cred.CredRead)
                from rocky.assistant import Assistant
                from rocky.reminders import ReminderCommands
                reminders = ReminderCommands(store)
                reminders.handle('remind me in two minutes')
                answer = Assistant(brain, reminders=reminders).respond('Can you please tell me what time it is?')
                checks['clock_preserves_reminder_draft'] = answer.startswith('It is ') and not store.reminders() and reminders.expects_body
                checks['date_uses_clock'] = Assistant(brain).respond("What's today's date?").startswith('Today is ')
                code_answer = brain.code_chat('Why does this divide by zero?', 'FILE calc.py\n1: def ratio(a):\n2:     return a / 0')
                checks['code_explains_zero_divisor'] = 'ZeroDivisionError' in code_answer and 'not an error' not in code_answer.casefold()
                root = tk.Tk()
                root.withdraw()
                try:
                    page = ModelSettingsPage(root, store, service, events)
                    page.save()
                    checks['settings_persist'] = ModelSettings.load(Store(store.path)).translation == 'translategemma:4b'
                    checks['settings_page'] = page.variables['embedding'].get() == 'embeddinggemma:latest'
                finally:
                    root.destroy()
                checks['session_index_in_ram'] = service.index.memory_bytes > 0 and not (base / 'test-document-index.sqlite3').exists()
                service.clear_memory()
                checks['document_memory_cleared'] = not service._cache and service.index.memory_bytes == 0 and service.workspace.root is None
                checks['original_document_preserved'] = (base / 'project.txt').is_file()
                service.close()
                checks['document_session_closed'] = service.closed and service.index.closed
            Path(sys.argv[2]).write_text(json.dumps(dict(version=VERSION, checks=checks)), encoding='utf-8')
        except Exception as exc:
            Path(sys.argv[2]).write_text(json.dumps(dict(version=VERSION, checks=checks, error=str(exc))), encoding='utf-8')
            sys.exit(1)
        if not all(checks.values()):
            sys.exit(1)
    elif sys.argv[1:2] == ['--documents-check']:
        import tempfile
        import tkinter as tk
        import queue
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation
        from PIL import Image, ImageDraw, ImageFont
        from rocky.documents import Documents
        from rocky.documents_page import DocumentsPage
        from rocky.document_export import export_result
        from rocky.storage import Store
        from rocky.version import VERSION
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            document = Document()
            document.add_heading('Delivery', 1)
            document.add_paragraph('The deadline is Friday.')
            document.save(base / 'plan.docx')
            workbook = Workbook()
            workbook.active['B2'] = 'Budget 500'
            workbook.save(base / 'table.xlsx')
            slides = Presentation()
            slide = slides.slides.add_slide(slides.slide_layouts[1])
            slide.shapes.title.text = 'Delivery Friday'
            slides.save(base / 'slides.pptx')
            picture = Image.new('RGB', (1000, 250), 'white')
            font = ImageFont.truetype(str(Path(os.environ['WINDIR']) / 'Fonts' / 'arial.ttf'), 40)
            ImageDraw.Draw(picture).text((45, 80), 'Invoice 12345 due Friday', font=font, fill='black')
            picture.save(base / 'scan.png')
            picture.save(base / 'scan.pdf')
            events = queue.Queue()
            service = Documents(Store(base / 'test.db'), None, events)
            service.workspace.select(base)
            service.use_ocr = True
            checks = {}
            for name, expected in [('plan.docx', 'Friday'), ('table.xlsx', '500'), ('slides.pptx', 'Friday'),
                                   ('scan.png', '12345'), ('scan.pdf', '12345')]:
                value = service.perform('Read / jump', name)
                checks[name] = expected in value.answer
            value = service.perform('Read / jump', 'plan.docx')
            export_result(base / 'result.docx', 'Summary', value.hits)
            checks['export'] = (base / 'result.docx').is_file()
            root = tk.Tk()
            root.withdraw()
            try:
                worker = type('TestWorker', (), {'announcements': queue.Queue(), 'stop_speaking': lambda self: None})()
                page = DocumentsPage(root, service, worker, events)
                page.display(value)
                root.update_idletasks()
                checks['embedded_page'] = len(page.sources.get_children()) == len(value.hits)
            finally:
                root.destroy()
            Path(sys.argv[2]).write_text(json.dumps(dict(version=VERSION, checks=checks)), encoding='utf-8')
            if not all(checks.values()):
                sys.exit(1)
    elif sys.argv[1:2] == ['--calendar-check']:
        # Exercise the packaged page against temporary data, without voice or a worker.
        import tempfile
        import tkinter as tk
        from datetime import datetime, timedelta, timezone
        from rocky.calendar_page import CalendarPage
        from rocky.calendar_model import month_days
        from rocky.storage import Store
        from rocky.version import VERSION
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk()
            root.withdraw()
            try:
                store = Store(Path(folder) / 'calendar-check.db')
                page = CalendarPage(root, store, 'UTC')
                day = (datetime.now(timezone.utc) + timedelta(days=2)).date()
                page.select_day(day)
                page.body.set('Packaged calendar check')
                page.clock.set('17:30')
                added = bool(page.save_event())
                page.body.set('Edited calendar check')
                edited = bool(page.save_event())
                saved = Store(store.path).latest_reminder()
                result = dict(version=VERSION, added=added, edited=edited,
                              persisted=saved['body'] == 'Edited calendar check',
                              monday_first=month_days(day.year,day.month)[0].weekday() == 0,
                              inline=not any(isinstance(child,tk.Toplevel) for child in root.winfo_children()))
                Path(sys.argv[2]).write_text(json.dumps(result),encoding='utf-8')
            finally:
                root.destroy()
    elif sys.argv[1:2] == ['--notification-check']:
        # Real UI smoke check, isolated from the user's reminder database.
        import time
        import tkinter as tk
        import win32gui
        import win32con
        from rocky.toast import show_toast, TOAST_DURATION_MS
        root = tk.Tk()
        root.withdraw()
        before = win32gui.GetForegroundWindow()
        started = time.monotonic()
        card = show_toast(root,'This is a test message. It disappears automatically after four seconds.',
                          title='Rocky · Notification test')
        hwnd = win32gui.GetAncestor(card.winfo_id(),2)
        flags = win32gui.GetWindowLong(hwnd,win32con.GWL_EXSTYLE)
        result = dict(taskbar_hidden=bool(flags & win32con.WS_EX_TOOLWINDOW) and not bool(flags & win32con.WS_EX_APPWINDOW),
                      no_activate=bool(flags & 0x08000000),focus_preserved=before==win32gui.GetForegroundWindow(),
                      duration_ms=TOAST_DURATION_MS)
        def dismissed(event):
            if event.widget is card:
                result['dismissed_after_seconds'] = round(time.monotonic()-started,3)
        card.bind('<Destroy>',dismissed,add='+')
        def finished():
            result['dismissed'] = not bool(card.winfo_exists())
            Path(sys.argv[2]).write_text(json.dumps(result),encoding='utf-8')
            root.quit()
        root.after(4500,finished)
        try:
            root.mainloop()
        finally:
            root.destroy()
    elif sys.argv[1:2] == ['--recognition-check']:
        # Used by the Windows build smoke check with a temporary test WAV file.
        import time
        import numpy as np
        from faster_whisper.audio import decode_audio
        from rocky.transcription import Transcriber

        samples = decode_audio(sys.argv[2], sampling_rate=16000)
        pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
        transcriber = Transcriber(device=sys.argv[4])
        started = time.monotonic()
        heard = transcriber.transcribe(pcm)
        Path(sys.argv[3]).write_text(json.dumps({'heard': heard, 'engine': transcriber.engine,
                                                'seconds': time.monotonic() - started}), encoding='utf-8')
    else:
        from rocky.__main__ import main

        main()
