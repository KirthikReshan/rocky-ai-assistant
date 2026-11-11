from datetime import datetime, timedelta, timezone
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch
import io
import json
import sys
import types
import threading

from rocky.calibration import advise
from rocky.clarification import AppClarifier
from rocky.documents import Documents, safe_document, read_pages
from rocky.extensions import Extensions
from rocky.recurrence import next_occurrence
from rocky.reminders import ReminderCommands
from rocky.routines import Routines, validate_routine
from rocky.storage import Store
from rocky.system_actions import PCControls, WindowTarget, perform, windows_power
from rocky.tools import Action, parse_action
from rocky.brain import OllamaBrain, BrainCancelled
from rocky.voice import VoiceWorker, _InterruptAudio
from rocky.voice_interrupt import VoiceInterrupt
from rocky.system_actions import close_targets
import tkinter as tk


def pdf_fixture(path):
    # Small self-contained PDF fixture with real searchable text and a valid xref.
    content = b'BT /F1 12 Tf 72 720 Td (Rocky document launch is on Friday.) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream']
    data, offsets = b'%PDF-1.4\n', [0]
    for number, obj in enumerate(objects,1):
        offsets.append(len(data))
        data += f'{number} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref = len(data)
    data += b'xref\n0 6\n0000000000 65535 f \n'
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    data += f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()
    path.write_bytes(data)


class StoredFeatureTests(unittest.TestCase):
    def test_advanced_schedules_wait_for_a_compatible_calendar_without_saving(self):
        service=ReminderCommands(self.store,'UTC',require_advanced_calendar=True)
        with patch('rocky.calendar_service.service_compatible',return_value=False):
            self.assertIn('not saved yet',service.handle('remind me every weekday in ten minutes to stretch'))
        self.assertEqual(self.store.reminders(),[])

    def test_calendar_compatibility_event_exists_only_while_the_service_handle_lives(self):
        import hashlib,os,win32event
        from rocky.calendar_service import service_compatible, SERVICE_PROTOCOL
        key=hashlib.sha256(os.path.normcase(str(self.store.path.resolve())).encode()).hexdigest()
        self.assertFalse(service_compatible(self.store.path))
        handle=win32event.CreateEvent(None,True,True,f'Local\\RockyCalendarV{SERVICE_PROTOCOL}-{key}')
        try:
            self.assertTrue(service_compatible(self.store.path))
        finally:
            handle.Close()
        self.assertFalse(service_compatible(self.store.path))

    def test_incomplete_document_search_does_not_fall_through_to_google(self):
        docs=Documents(self.store,None,queue.Queue())
        self.assertIn('Include a subject',docs.handle('search documents for'))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root/'rocky.db')

    def test_routines_persist_and_never_allow_system_or_code_actions(self):
        tools = Mock()
        tools.execute.return_value = 'Opening app.'
        routines = Routines(self.store,tools)
        routines.save('coding mode',['open VS Code','open https://github.com'])
        restored = Routines(Store(self.store.path),tools)
        self.assertIn('Ran coding mode',restored.handle('run routine coding mode'))
        self.assertEqual([c.args[0] for c in tools.execute.call_args_list],
                         [Action('open','VS Code'),Action('open','https://github.com')])
        for command in ('shutdown','close chrome','edit file main.py: do something','powershell -Command x'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                validate_routine('unsafe',[command])

    def test_routine_stops_after_first_failed_action(self):
        tools = Mock()
        tools.execute.return_value = "I couldn't open that."
        routines = Routines(self.store,tools)
        routines.save('work',['open VS Code','open github'])
        self.assertIn('stopped',routines.handle('run routine work'))
        self.assertEqual(tools.execute.call_count,1)

    def test_fuzzy_app_match_needs_an_explicit_answer_and_expires(self):
        clock = Mock(return_value=0)
        clarifier = AppClarifier(self.store,clock)
        tools = Mock()
        tools.execute.return_value = 'Opening calculator.'
        with patch('rocky.clarification.installed_app_id',side_effect=FileNotFoundError):
            self.assertIn('calculator',clarifier.handle('open calcultor',parse_action('open calcultor'),tools))
            tools.execute.assert_not_called()
            self.assertEqual(clarifier.handle('yes',None,tools),'Opening calculator.')
            tools.execute.assert_called_once_with(Action('open','calculator'))
            tools.reset_mock()
            clarifier.handle('open calcultor',parse_action('open calcultor'),tools)
            clock.return_value = 31
            self.assertIsNone(clarifier.handle('yes',None,tools))
            tools.execute.assert_not_called()

    def test_exact_installed_app_is_never_replaced_by_a_fuzzy_guess(self):
        clarifier = AppClarifier(self.store)
        with patch('rocky.clarification.installed_app_id',return_value='Custom!App'):
            self.assertIsNone(clarifier.handle('open custom',Action('open','custom'),Mock()))

    def test_new_reminder_modes_survive_restart_and_completion_stops_repeats(self):
        due = datetime(2027,1,1,12,tzinfo=timezone.utc)
        event = self.store.add_reminder('Take a break',due,'UTC','test',repeat='until_done')
        self.assertTrue(self.store.acknowledge(event,now=due))
        reopened = Store(self.store.path)
        self.assertEqual(reopened.reminder(event)['due_at'],(due+timedelta(minutes=5)).timestamp())
        self.assertTrue(reopened.complete_reminder(event))
        self.assertFalse(reopened.complete_reminder(event))
        self.assertEqual(reopened.due(now=due+timedelta(hours=1)),[])
        sunday = datetime(2027,1,3,12,tzinfo=timezone.utc)
        weekday = reopened.add_reminder('Work',sunday,'UTC','test',repeat='weekdays')
        self.assertEqual(datetime.fromtimestamp(reopened.reminder(weekday)['due_at'],timezone.utc).weekday(),0)

    def test_voice_weekday_month_year_and_until_done_routing(self):
        now = datetime(2027,1,1,8,tzinfo=timezone.utc)
        with patch('rocky.reminders.utc_now',return_value=now):
            service = ReminderCommands(self.store,'UTC')
            for phrase, repeat in [('every weekday at 5 PM','weekdays'),('every month on 5 January 2027 at 5 PM','monthly'),
                                   ('every year on 5 January 2027 at 5 PM','yearly'),('until acknowledged in five minutes','until_done')]:
                with self.subTest(phrase=phrase):
                    result = service.handle('remind me '+phrase+' to call Mom')
                    self.assertIn('saved',result)
                    self.assertEqual(self.store.latest_reminder()['repeat'],repeat)
            event = self.store.latest_reminder()['id']
            self.assertIn('marked done',service.handle(f'mark reminder {event} done'))

    def test_document_search_returns_real_paths_pages_and_local_notes(self):
        (self.root/'plans.txt').write_text('The Rocky launch plan is ready.',encoding='utf-8')
        self.store.add_note('Remember the Rocky launch checklist.')
        events = queue.Queue()
        documents = Documents(self.store,None,events)
        documents.workspace.select(self.root)
        hits = documents.search('Rocky launch')
        self.assertTrue(any(hit.source.endswith('plans.txt') and hit.page==1 for hit in hits))
        self.assertTrue(any(hit.source.startswith('Note ') for hit in hits))
        self.assertIn('Found',documents.handle('search documents for launch'))
        self.assertIn('Start Ollama',documents.handle('ask documents: what is ready for launch?'))
        documents.workspace.clear()
        self.assertTrue(all(hit.source.startswith('Note ') for hit in documents.search('launch')))

    def test_pdf_text_is_extracted_by_the_real_isolated_worker(self):
        pdf_fixture(self.root/'sample.pdf')
        pages = read_pages(self.root,'sample.pdf')
        self.assertEqual(pages[0][0],1)
        self.assertIn('launch is on Friday',pages[0][1])
        for relative in ('../sample.pdf',str(self.root/'sample.pdf'),'secrets.json'):
            with self.subTest(relative=relative), self.assertRaises((ValueError,OSError)):
                safe_document(self.root,relative)


class RecurrenceExtensionTests(unittest.TestCase):
    def row(self,due,repeat,anchor=None):
        return dict(timezone='UTC',due_at=due.timestamp(),repeat_time='12:00:00',repeat=repeat,
                    repeat_anchor=anchor or due.date().isoformat())

    def test_monthly_anchor_is_not_lost_after_short_month_or_missed_events(self):
        january = datetime(2027,1,31,12,tzinfo=timezone.utc)
        february = next_occurrence(self.row(january,'monthly'),january)
        self.assertEqual((february.month,february.day),(2,28))
        march = next_occurrence(self.row(february,'monthly','2027-01-31'),february)
        self.assertEqual((march.month,march.day),(3,31))
        missed = next_occurrence(self.row(january,'monthly'),datetime(2027,3,1,tzinfo=timezone.utc))
        self.assertEqual((missed.month,missed.day),(3,31))

    def test_yearly_leap_birthday_returns_to_leap_day(self):
        leap = datetime(2028,2,29,12,tzinfo=timezone.utc)
        next_year = next_occurrence(self.row(leap,'yearly'),leap)
        self.assertEqual((next_year.year,next_year.month,next_year.day),(2029,2,28))
        later = next_occurrence(self.row(next_year,'yearly','2028-02-29'),datetime(2032,1,1,tzinfo=timezone.utc))
        self.assertEqual((later.year,later.day),(2032,29))

    def test_weekdays_skip_weekends(self):
        friday = datetime(2027,1,1,12,tzinfo=timezone.utc)
        due = next_occurrence(self.row(friday,'weekdays'),friday)
        self.assertEqual((due.weekday(),due.day),(0,4))


class PCApprovalTests(unittest.TestCase):
    def test_chat_cannot_claim_a_pc_action_happened_without_a_tool_result(self):
        from rocky.assistant import Assistant
        brain=Mock()
        brain.chat.return_value="I've closed Chrome."
        answer=Assistant(brain).respond('Tell me something')
        self.assertIn('No PC action was performed',answer)
        brain.correct_last_reply.assert_called_once_with(answer)
    def test_closing_explorer_can_never_target_the_taskbar(self):
        def enumerate_windows(callback, value):
            callback(10,value)
            callback(11,value)
        with patch('ctypes.windll.user32.GetShellWindow',return_value=1), \
             patch('win32gui.EnumWindows',side_effect=enumerate_windows),patch('win32gui.IsWindowVisible',return_value=True), \
             patch('win32gui.GetWindowText',return_value='Explorer'), \
             patch('win32gui.GetClassName',side_effect=lambda hwnd:'Shell_TrayWnd' if hwnd==10 else 'CabinetWClass'), \
             patch('rocky.system_actions.window_identity',return_value=(20,r'C:\Windows\explorer.exe','created')):
            self.assertEqual([target.hwnd for target in close_targets('file explorer')],[11])
    def test_text_approval_is_never_a_gui_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            extensions = Extensions(Store(Path(folder)/'rocky.db'),Mock(),None,queue.Queue())
            with patch('rocky.system_actions.perform') as execute:
                extensions.handle('shut down Windows',Mock())
                self.assertIn('cannot approve',extensions.handle('approve action',Mock()))
                execute.assert_not_called()
    def test_request_reject_expiry_and_text_cannot_execute_pc_actions(self):
        clock = Mock(return_value=0)
        service = PCControls(queue.Queue(),clock)
        with patch('rocky.system_actions.perform') as execute:
            self.assertIn('Review',service.request('shutdown'))
            proposal = service.events.get()[1]
            execute.assert_not_called()
            clock.return_value = 61
            with self.assertRaises(ValueError):
                service.approve(proposal.id)
            execute.assert_not_called()
            clock.return_value = 0
            service.request('restart')
            proposal = service.events.get()[1]
            service.approve(proposal.id)
            execute.assert_called_once_with('restart',())
            with self.assertRaises(ValueError):
                service.approve(proposal.id)
            execute.reset_mock()
            service.request('shutdown')
            proposal = service.events.get()[1]
            service.reject(proposal.id)
            with self.assertRaises(ValueError):
                service.approve(proposal.id)
            execute.assert_not_called()

    def test_reused_window_handle_cannot_close_a_different_process(self):
        target = WindowTarget(10,20,'app.exe','creation','App')
        with patch('win32gui.IsWindow',return_value=True), patch('rocky.system_actions.window_identity',return_value=(99,'app.exe','new')), \
             patch('win32gui.PostMessage') as close:
            with self.assertRaises(ValueError):
                perform('close',(target,))
            close.assert_not_called()

    def test_power_api_uses_no_force_and_restores_privileges_without_real_shutdown(self):
        token, api = Mock(), Mock()
        api.InitiateSystemShutdownExW.return_value=1
        with patch('win32security.OpenProcessToken',return_value=token), \
             patch('win32security.LookupPrivilegeValue',return_value=7), \
             patch('win32security.AdjustTokenPrivileges',return_value=[(7,0)]) as adjust, \
             patch('ctypes.WinDLL',return_value=api):
            windows_power('restart')
        arguments = api.InitiateSystemShutdownExW.call_args.args
        self.assertEqual(arguments[2:5],(30,False,True))
        self.assertEqual(adjust.call_args.args,(token,False,[(7,0)]))
        token.Close.assert_called_once()


class CalibrationTests(unittest.TestCase):
    def test_clipped_and_quiet_recordings_cannot_drive_calibration_changes(self):
        rows=[self.row(f'open app {i}') for i in range(5)]
        for row in rows:
            row['clipped']=.5
            row['peak']=1
        self.assertEqual(advise(rows)[1],{})
        for row in rows:
            row.update(clipped=0,peak=.001)
        self.assertEqual(advise(rows)[1],{})

    def test_wake_advice_requires_positive_and_negative_recordings(self):
        rows=[]
        for kind in ('wake_positive','wake_negative'):
            for index in range(4):
                row=self.row('Hey Rocky')
                row.update(test_kind=kind,reference_words=0,wake_detected=int(kind=='wake_positive' or index==0),
                           wake_config='threshold 0.55, verification False')
                rows.append(row)
        self.assertEqual(advise(rows)[1]['wake_sensitivity'],'Strict')
        self.assertNotIn('wake_sensitivity',advise(rows[:4])[1])
    def row(self,phrase,**changes):
        return dict(expected=phrase,heard='open',reference_words=3,word_errors=2,overflow=False,
                    device='Headset',test_kind='command',environment='Quiet room',engine='CPU',latency=1,
                    wake_config='',wake_detected=0,**changes)

    def test_advice_requires_samples_and_does_not_mix_microphones(self):
        self.assertEqual(advise([])[1],{})
        rows=[self.row(f'open app {i}') for i in range(5)]
        self.assertEqual(advise(rows)[1]['speech_pause'],2.0)
        for row in rows:
            row['device']='Other headset'
        self.assertEqual(advise(rows,device='Headset')[1],{})

    def test_processor_comparison_requires_matching_phrases_and_conditions(self):
        rows=[]
        for i in range(3):
            first=self.row(f'open app {i}')
            first.update(word_errors=0,heard=first['expected'])
            second=dict(first,engine='NVIDIA GPU',latency=.2)
            rows.extend([first,second])
        self.assertEqual(advise(rows)[1]['recognition_device'],'cuda')
        for row in rows:
            if row['engine']=='NVIDIA GPU':
                row['environment']='Background noise'
        self.assertNotIn('recognition_device',advise(rows)[1])


class NaturalInterruptionTests(unittest.TestCase):
    def test_wake_during_reply_hands_audio_off_without_running_tools_on_listener_thread(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        audio = Mock(RawInputStream=Mock(return_value=stream))
        interrupt, handoff = Mock(), Mock()
        listener = VoiceInterrupt(audio,Mock(),None,lambda:True,interrupt,Mock(),on_command=handoff)
        listener.audio.put(bytes(3200))
        stop, wake = Mock(), Mock()
        stop.feed.return_value=False
        wake.feed.return_value=True
        wake.audio=bytearray(b'captured wake and command')
        with patch('rocky.voice_interrupt.PhraseDetector',side_effect=[stop,wake]):
            listener.run()
        interrupt.assert_called_once_with()
        handoff.assert_called_once_with(b'captured wake and command')

    def test_interruption_transcript_is_verified_before_it_reaches_assistant(self):
        with tempfile.TemporaryDirectory() as folder:
            assistant=Mock(supports_sentence_speech=False)
            assistant.respond.return_value='New answer'
            worker=VoiceWorker(queue.Queue(),Path(folder),assistant=assistant)
            worker.messages.put(_InterruptAudio(bytes(3200),0))
            original=worker.emit
            def emit(kind,value):
                original(kind,value)
                if kind=='Rocky':
                    worker.stopping.set()
            worker.emit=emit
            audio=types.SimpleNamespace(query_devices=Mock(return_value={'name':'Headset'}))
            with patch.dict(sys.modules,{'sounddevice':audio,'pythoncom':Mock(),'winsound':Mock(),
                                         'vosk':types.SimpleNamespace(Model=Mock(),KaldiRecognizer=Mock())}), \
                 patch('rocky.speech.Speech'), patch.object(Path,'is_dir',return_value=True), \
                 patch.object(Path,'is_file',return_value=True),patch('rocky.voice.Transcriber') as transcriber:
                transcriber.return_value.transcribe.return_value='Hey Rocky, explain Python lists.'
                worker.run()
            assistant.respond.assert_called_once_with('explain Python lists.')

    def test_cancelled_model_stream_closes_and_cannot_poison_the_next_request(self):
        brain=OllamaBrain()
        parts=[{'message':{'content':'{"reply":"First sentence. More'}},
               {'done':True,'message':{'content':' words"}'}}]
        response=io.BytesIO(('\n'.join(json.dumps(p) for p in parts)+'\n').encode())
        brain.http.open=Mock(return_value=response)
        with self.assertRaises(BrainCancelled):
            brain.chat('Old question',on_draft=lambda value:brain.cancel_current())
        self.assertTrue(response.closed)
        self.assertEqual(brain.history,[])
        brain.http.open.return_value=io.BytesIO(json.dumps({'message':{'content':'{"reply":"New answer"}'}}).encode())
        self.assertEqual(brain.chat('New question'),'New answer')


class UpgradeUITests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk()
        self.root.withdraw()
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'rocky.db')

    def tearDown(self):
        for timer in self.root.tk.call('after','info'):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.temp.cleanup()

    def test_guided_calibration_advances_only_after_user_recordings(self):
        from rocky.voice_setup import VoiceSetup
        setup=VoiceSetup(self.root,Mock(tests=queue.Queue()),self.store)
        setup.start_guided()
        self.assertEqual(len(self.store.voice_tests()),0)
        for _ in range(18):
            mode=setup.mode.current()
            setup.handle('TestResult',dict(expected=setup.prompt.get(),heard=setup.prompt.get(),peak=.2,rms=.1,clipped=0,
                         overflow=False,latency=.1,environment=setup.environment.get(),device='Headset',engine='CPU',
                         word_errors=0,reference_words=2 if mode==0 else 0,exact=True,
                         test_kind=('command','wake_positive','wake_negative')[mode],wake_detected=int(mode==1),
                         wake_baseline=int(mode==1),wake_config='threshold 0.55, verification False'))
        self.assertIsNone(setup.guided)
        self.assertEqual(len(self.store.voice_tests()),18)

    def test_documents_and_routines_controls_queue_commands_without_running_them(self):
        from rocky.upgrade_ui import UpgradeUI
        events=queue.Queue()
        tools=Mock()
        extensions=Extensions(self.store,tools,None,events)
        worker=Mock(messages=queue.Queue())
        view=UpgradeUI(self.root,self.store,worker,extensions,events)
        view.documents()
        self.store.add_note('My launch plan')
        extensions.documents.search('launch')
        view.document_results(events.get()[1])
        self.assertEqual(view.results.size(),1)
        self.assertTrue(view.hits[0].source.startswith('Note'))
        view.routines()
        def children(widget):
            return [child for direct in widget.winfo_children() for child in [direct,*children(direct)]]
        controls={str(widget.cget('text')):widget for widget in children(view.routine_window) if widget.winfo_class()=='TButton'}
        controls['Save routine'].invoke()
        controls['Run saved routine'].invoke()
        self.assertEqual(worker.messages.get_nowait(),'run routine coding mode')
        tools.execute.assert_not_called()

    def test_pc_dialog_rejection_cannot_execute(self):
        from rocky.upgrade_ui import show_pc_approval
        events=queue.Queue()
        pc=PCControls(events)
        pc.request('shutdown')
        proposal=events.get()[1]
        with patch('rocky.system_actions.perform') as execute:
            show_pc_approval(self.root,proposal,pc,events)
            popup=self.root.winfo_children()[-1]
            # Closing the preview is rejection, not an implicit approval.
            callback=popup.protocol('WM_DELETE_WINDOW')
            popup.tk.call(callback)
            with self.assertRaises(ValueError):
                pc.approve(proposal.id)
            execute.assert_not_called()


if __name__=='__main__':
    unittest.main()
