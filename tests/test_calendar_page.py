from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo

from rocky.app_instance import AppInstance
from rocky.calendar_model import month_days,month_shift,view_bounds,schedule_events,group_days,load_calendar
from rocky.calendar_page import CalendarPage
from rocky.runtime import calendar_page_command
from rocky.storage import Store


class CalendarModelTests(unittest.TestCase):
    def test_real_month_has_monday_first_week_and_leap_day(self):
        days=month_days(2024,2)
        self.assertEqual(len(days),42)
        self.assertEqual(days[0],date(2024,1,29))
        self.assertEqual(days[0].weekday(),0)
        self.assertIn(date(2024,2,29),days)
        self.assertEqual(month_shift(2027,1,-1),(2026,12))
        self.assertEqual(month_shift(2026,12,1),(2027,1))

    def test_projection_retains_monthly_anchor_and_never_mutates_saved_schedule(self):
        due=datetime(2027,1,31,12,tzinfo=timezone.utc)
        row=dict(id=1,body='Bill',timezone='UTC',due_at=due.timestamp(),repeat='monthly',repeat_time='12:00:00',repeat_anchor='2027-01-31')
        events=schedule_events([row],[],datetime(2027,2,1,tzinfo=timezone.utc),datetime(2027,4,1,tzinfo=timezone.utc))
        self.assertEqual([(event.due.month,event.due.day) for event in events],[(2,28),(3,31)])
        self.assertTrue(all(event.preview for event in events))
        self.assertEqual(row['due_at'],due.timestamp())

    def test_until_done_previews_only_the_next_actual_alert(self):
        due=datetime(2027,1,1,12,tzinfo=timezone.utc)
        row=dict(id=1,body='Task',timezone='UTC',due_at=due.timestamp(),repeat='until_done',repeat_time='12:00:00',repeat_anchor='2027-01-01')
        events=schedule_events([row],[],due,due+timedelta(days=42))
        self.assertEqual(len(events),1)

    def test_display_days_use_view_timezone_instead_of_utc_date(self):
        due=datetime(2027,1,2,1,tzinfo=timezone.utc)
        row=dict(id=1,body='Meeting',timezone='UTC',due_at=due.timestamp(),repeat='once')
        events=schedule_events([row],[],due-timedelta(days=2),due+timedelta(days=2))
        grouped=group_days(events,'America/Los_Angeles')
        self.assertIn(date(2027,1,1),grouped)
        self.assertNotIn(date(2027,1,2),grouped)

    def test_calendar_day_bounds_and_year_limits(self):
        start,end=view_bounds(month_days(2026,11),'Asia/Kolkata')
        self.assertEqual(start.astimezone(ZoneInfo('Asia/Kolkata')).weekday(),0)
        self.assertEqual((end-start).days,42)
        self.assertIsNone(month_days(9999,12)[-1])
        self.assertEqual(view_bounds(month_days(9999,12),'UTC')[1].year,9999)


class CalendarPageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=tk.Tk()
        self.root.withdraw()
        self.now=datetime(2027,1,1,10,tzinfo=timezone.utc)
        self.clock=patch('rocky.calendar_page.utc_now',return_value=self.now)
        self.editor_clock=patch('rocky.calendar_ui.utc_now',return_value=self.now)
        self.clock.start();self.editor_clock.start()
        self.store=Store(Path(self.temp.name)/'calendar.db')
        self.page=CalendarPage(self.root,self.store,'UTC')
        self.page.pack(fill='both',expand=True)
        self.root.update_idletasks()

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()
        self.clock.stop();self.editor_clock.stop()
        self.temp.cleanup()

    def create(self,day=5,repeat='once'):
        due=datetime(2027,1,day,12,tzinfo=timezone.utc)
        event=self.store.add_reminder('Review project',due,'UTC','test',repeat=repeat)
        self.page.refresh(force=True)
        return event,due

    def choose(self,event,day=5):
        selected=date(2027,1,day)
        key=next(event_row.key for event_row in self.page.grouped[selected] if event_row.reminder_id==event)
        self.page.select_day(selected,key)

    def test_add_and_edit_are_inline_and_persist_on_the_selected_date(self):
        self.page.select_day(date(2027,1,7))
        self.page.body.set('Call Mom')
        self.page.clock.set('17:30')
        self.assertTrue(self.page.save_event())
        self.assertFalse(any(isinstance(child,tk.Toplevel) for child in self.root.winfo_children()))
        event=self.store.latest_reminder()
        due=datetime.fromtimestamp(event['due_at'],timezone.utc)
        self.assertEqual((due.day,due.hour,due.minute),(7,17,30))
        self.page.body.set('Call family')
        self.assertTrue(self.page.save_event())
        self.assertEqual(self.store.reminder(event['id'])['body'],'Call family')
        self.assertEqual(len(Store(self.store.path).reminders()),1)

    def test_cancel_requires_confirmation_and_a_current_snapshot(self):
        event,due=self.create()
        self.choose(event)
        with patch('rocky.calendar_page.messagebox.askyesno',return_value=False):
            self.page.cancel_event()
        self.assertIsNone(self.store.reminder(event)['notified_at'])
        with patch('rocky.calendar_page.messagebox.askyesno',return_value=True):
            self.page.cancel_event()
        self.assertEqual(self.store.reminders(),[])

    def test_delivery_during_edit_cannot_rewind_the_series(self):
        event,due=self.create(repeat='daily')
        self.choose(event)
        self.page.body.set('Unsaved new title')
        self.store.acknowledge(event,now=due)
        self.assertFalse(self.page.save_event())
        self.assertEqual(self.store.reminder(event)['due_at'],(due+timedelta(days=1)).timestamp())

    def test_new_event_clears_old_selection_and_does_not_overwrite_existing_event(self):
        event,due=self.create()
        self.choose(event)
        self.page.new_event(reveal=False)
        self.root.update()
        self.assertIsNone(self.page.editor_row)
        self.assertEqual(self.page.body.get(),'')
        self.assertEqual(self.store.reminder(event)['body'],'Review project')

    def test_restored_width_stacks_editor_below_the_month(self):
        with patch.object(self.page.viewport,'winfo_width',return_value=1300),patch.object(self.page.viewport,'winfo_height',return_value=800):
            self.page.layout()
        self.assertFalse(self.page.narrow)
        self.assertEqual(self.page.inspector.grid_info()['column'],1)
        with patch.object(self.page.viewport,'winfo_width',return_value=700):
            self.page.layout()
        self.assertTrue(self.page.narrow)
        self.assertEqual(self.page.inspector.grid_info()['row'],1)

    def test_changing_month_requires_discarding_unsaved_edits(self):
        self.page.body.set('Unsaved task')
        with patch('rocky.calendar_page.messagebox.askyesno',return_value=False):
            self.page.change_month(1)
        self.assertEqual(self.page.month,1)
        self.assertEqual(self.page.body.get(),'Unsaved task')

    def test_until_done_history_is_grouped_per_local_day_and_recent_alerts_remain(self):
        event,due=self.create(repeat='until_done')
        with self.store.connect() as db:
            for timestamp in (datetime(2027,1,5,22,tzinfo=timezone.utc),datetime(2027,1,6,3,tzinfo=timezone.utc),datetime(2027,1,6,10,tzinfo=timezone.utc)):
                db.execute('INSERT INTO reminder_alerts(reminder_id,body,due_at,timezone,notified_at,repeat) VALUES (?,?,?,?,?,?)',
                           (event,'Task',timestamp.timestamp(),'UTC',timestamp.timestamp(),'until_done'))
        start,end=view_bounds(month_days(2027,1),'America/Los_Angeles')
        pending,alerts,recent=load_calendar(self.store,start,end,'America/Los_Angeles')
        self.assertEqual(len(alerts),2)
        self.assertEqual(len(recent),3)

    def test_completed_history_shows_its_actual_alert_time_not_the_next_due_time(self):
        event,due=self.create(repeat='until_done')
        self.store.acknowledge(event,now=due)
        self.store.complete_reminder(event)
        self.page.refresh(force=True)
        self.choose(event)
        self.assertEqual(self.page.clock.get(),'12:00')
        self.assertTrue(self.page.save_button.instate(['disabled']))

    def test_mark_done_does_not_accept_a_stale_editor_snapshot(self):
        event,due=self.create(repeat='until_done')
        row=self.store.reminder(event)
        self.store.update_reminder(event,'Changed task',due,'UTC',created_at=row['created_at'])
        self.assertFalse(self.store.complete_reminder(event,expected=row))
        self.assertIsNone(self.store.reminder(event)['notified_at'])


class MainInstanceTests(unittest.TestCase):
    def test_calendar_launcher_focuses_existing_instance_instead_of_starting_another(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'calendar.db'
            first=AppInstance(path)
            try:
                self.assertTrue(first.primary)
                def secondary():
                    second=AppInstance(path)
                    try:
                        self.assertFalse(second.primary)
                        second.request(calendar=True)
                    finally:
                        second.close()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(secondary).result(timeout=3)
                self.assertTrue(first.take(calendar=True))
                self.assertFalse(first.take(calendar=True))
            finally:
                first.close()
            again=AppInstance(path)
            try:
                self.assertTrue(again.primary)
            finally:
                again.close()

    def test_calendar_page_launcher_preserves_an_exact_custom_database(self):
        with tempfile.TemporaryDirectory() as folder:
            database=Path(folder)/'custom events.db'
            command=calendar_page_command(database)
            self.assertEqual(command[-3:],['--calendar-page','--database',str(database.resolve())])


if __name__=='__main__':
    unittest.main()
