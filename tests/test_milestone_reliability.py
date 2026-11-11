"""Regression checks for cross-feature races; no real PC actions or personal data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from rocky.documents import Documents
from rocky.extensions import Extensions
from rocky.routines import Routines
from rocky.storage import Store
from rocky.system_actions import PCControls, WindowTarget


class MilestoneReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        self.store=Store(self.root/'state.db')
        self.due=datetime(2027,1,31,12,tzinfo=timezone.utc)

    def test_cancel_shutdown_still_aborts_when_a_close_preview_is_pending(self):
        events=queue.Queue()
        extensions=Extensions(self.store,Mock(),None,events)
        with patch('rocky.system_actions.perform',return_value='Windows countdown cancelled.') as action, \
             patch('rocky.system_actions.close_targets',return_value=(WindowTarget(1,2,'app.exe','time','App'),)):
            extensions.pc.request('shutdown')
            extensions.pc.approve(events.get()[1].id)
            extensions.pc.request('close','notepad')
            self.assertEqual(extensions.handle('cancel shutdown',Mock()),'Windows countdown cancelled.')
            self.assertEqual(action.call_args.args,('abort_shutdown',))
            self.assertFalse(extensions.pc.has_pending())

    def test_abort_waits_for_already_approved_power_request_to_reach_windows(self):
        controls=PCControls(queue.Queue())
        controls.request('restart')
        token=controls.events.get()[1].id
        entered,release,aborted=threading.Event(),threading.Event(),threading.Event()
        order=[]
        def action(kind,targets=()):
            if kind=='restart':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('Test release not signalled')
            order.append(kind)
            if kind=='abort_shutdown':
                aborted.set()
            return kind
        with patch('rocky.system_actions.perform',side_effect=action),ThreadPoolExecutor(max_workers=2) as executor:
            approved=executor.submit(controls.approve,token)
            try:
                self.assertTrue(entered.wait(2))
                cancelled=executor.submit(controls.cancel_power)
                self.assertFalse(aborted.wait(.05))
            finally:
                release.set()
            approved.result(timeout=3)
            cancelled.result(timeout=3)
        self.assertEqual(order,['restart','abort_shutdown'])

    def test_cancelling_unapproved_request_without_countdown_is_harmless(self):
        controls=PCControls(queue.Queue())
        controls.request('shutdown')
        error=OSError('No active countdown')
        error.winerror=1116
        with patch('rocky.system_actions.perform',side_effect=error):
            self.assertIn('No Windows countdown',controls.cancel_power())
        self.assertFalse(controls.has_pending())

    def test_body_edit_preserves_monthly_day_31_after_february_clamp(self):
        event=self.store.add_reminder('Pay bill',self.due,'UTC','test',repeat='monthly')
        self.store.acknowledge(event,now=self.due)
        row=self.store.reminder(event)
        february=datetime.fromtimestamp(row['due_at'],timezone.utc)
        self.assertEqual(february.day,28)
        self.assertTrue(self.store.update_reminder(event,'Pay new bill',february,'UTC',created_at=row['created_at'],expected=row))
        self.assertEqual(self.store.reminder(event)['repeat_anchor'],'2027-01-31')
        self.store.acknowledge(event,now=february)
        march=datetime.fromtimestamp(self.store.reminder(event)['due_at'],timezone.utc)
        self.assertEqual((march.month,march.day),(3,31))

    def test_same_day_time_correction_preserves_yearly_leap_day_anchor(self):
        leap=datetime(2028,2,29,12,tzinfo=timezone.utc)
        event=self.store.add_reminder('Birthday',leap,'UTC','test',repeat='yearly')
        self.store.acknowledge(event,now=leap)
        row=self.store.reminder(event)
        due=datetime.fromtimestamp(row['due_at'],timezone.utc)+timedelta(hours=1)
        self.assertTrue(self.store.reschedule_reminder(row,due))
        self.assertEqual(self.store.reminder(event)['repeat_anchor'],'2028-02-29')

    def test_explicit_date_change_resets_monthly_anchor(self):
        event=self.store.add_reminder('Bill',self.due,'UTC','test',repeat='monthly')
        row=self.store.reminder(event)
        self.assertTrue(self.store.reschedule_reminder(row,self.due+timedelta(days=1)))
        self.assertEqual(self.store.reminder(event)['repeat_anchor'],'2027-02-01')

    def test_update_without_repeat_parameter_keeps_weekday_rule(self):
        friday=datetime(2027,1,1,12,tzinfo=timezone.utc)
        event=self.store.add_reminder('Work',friday,'UTC','test',repeat='weekdays')
        row=self.store.reminder(event)
        self.assertTrue(self.store.update_reminder(event,'Work',friday+timedelta(days=2),'UTC',created_at=row['created_at']))
        due=datetime.fromtimestamp(self.store.reminder(event)['due_at'],timezone.utc)
        self.assertEqual((due.weekday(),due.day),(0,4))

    def until_done_alert(self):
        event=self.store.add_reminder('Take medicine',self.due,'UTC','test',repeat='until_done')
        self.store.acknowledge(event,now=self.due)
        return event,self.store.recent_alerts()[0]

    def test_snoozing_until_done_postpones_original_instead_of_creating_a_duplicate(self):
        event,alert=self.until_done_alert()
        snoozed=self.store.snooze_alert(alert['id'],30,now=self.due)
        self.assertEqual(snoozed,event)
        self.assertEqual(len(self.store.reminders()),1)
        self.assertEqual(self.store.due(self.due+timedelta(minutes=10)),[])
        self.assertEqual(self.store.reminder(event)['due_at'],(self.due+timedelta(minutes=30)).timestamp())
        self.store.complete_reminder(event)
        with self.assertRaises(ValueError):
            self.store.snooze_alert(alert['id'],5,now=self.due)

    def test_done_retires_legacy_snooze_children(self):
        event,alert=self.until_done_alert()
        legacy=self.store.add_reminder('Take medicine',self.due+timedelta(minutes=30),'UTC','Snoozed alert')
        with self.store.connect() as db:
            db.execute('UPDATE reminders SET snoozed_from=? WHERE id=?',(alert['id'],legacy))
        self.store.complete_reminder(event)
        self.assertEqual(self.store.due(self.due+timedelta(hours=1)),[])

    def test_old_document_answer_is_discarded_after_folder_clear(self):
        (self.root/'launch.txt').write_text('Launch is on Friday.',encoding='utf-8')
        brain=Mock()
        docs=Documents(self.store,brain,queue.Queue())
        docs.workspace.select(self.root)
        def answer(*args):
            docs.workspace.clear()
            return 'Old source answer that must not appear'
        brain.document_chat.side_effect=answer
        result=docs.handle('ask documents: When is launch?')
        self.assertIn('Document folder selection changed',result)
        self.assertNotIn('Old source answer',result)

    def test_search_matches_words_instead_of_unrelated_substrings(self):
        self.store.add_note('Buy a chair and paint it.')
        self.store.add_note('AI project launch')
        self.store.add_note('Use C++ for this module')
        self.store.add_note('Use C# for another module')
        docs=Documents(self.store,None,queue.Queue())
        self.assertEqual(len(docs.search('AI')),1)
        self.assertEqual(len(docs.search('C++')),1)
        self.assertIn('C++',docs.search('C++')[0].excerpt)

    def test_concurrent_routine_saves_cannot_lose_another_name(self):
        barrier=threading.Barrier(2)
        original=Store.setting
        def stale_read(store,name,default=None):
            value=original(store,name,default)
            if name=='routines':
                barrier.wait(timeout=2)
            return value
        # Force legacy read-then-write code to see identical old snapshots.
        with patch.object(Store,'setting',stale_read),ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(Routines(Store(self.store.path),Mock()).save,name,['open calculator'])
                     for name in ('morning','evening')]
            for future in futures:
                future.result(timeout=4)
        self.assertEqual(set(self.store.setting('routines')),{'morning','evening'})

    def test_calibration_for_an_old_microphone_cannot_overwrite_current_settings(self):
        import tkinter as tk
        from rocky.upgrade_ui import show_calibration
        root=tk.Tk()
        root.withdraw()
        try:
            worker=Mock(device=3,silence_seconds=1.5)
            audio=Mock()
            audio.query_devices.return_value={'name':'Headset A'}
            with patch.dict('sys.modules',{'sounddevice':audio}), \
                 patch('rocky.upgrade_ui.advise',return_value=('Measured Headset A',{'speech_pause':2.0})), \
                 patch('rocky.upgrade_ui.messagebox.showerror') as error:
                show_calibration(root,self.store,worker,queue.Queue())
                popup=root.winfo_children()[-1]
                worker.device=4
                button=next(widget for widget in popup.winfo_children()[0].winfo_children()
                            if widget.winfo_class()=='TButton')
                button.invoke()
                error.assert_called_once()
                self.assertIsNone(self.store.setting('speech_pause'))
                self.assertEqual(worker.silence_seconds,1.5)
        finally:
            root.destroy()


if __name__=='__main__':
    unittest.main()
