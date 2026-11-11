from datetime import datetime, timezone
from pathlib import Path
import queue
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from rocky.notifications import show_reminder, ReminderDelivery
from rocky.storage import Store
from rocky.toast import show_toast, ToastQueue, toast_position, TOAST_DURATION_MS


class ToastTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk()
        self.root.withdraw()
        self.errors=[]
        self.root.report_callback_exception=lambda *error:self.errors.append(error)

    def tearDown(self):
        # Toplevel timers belong to that widget's Tcl command registry.
        # Let its Destroy binding cancel them before cancelling root timers.
        for child in list(self.root.winfo_children()):
            child.destroy()
        self.root.update_idletasks()
        for timer in self.root.tk.call('after','info'):
            self.root.after_cancel(timer)
        self.root.destroy()

    def test_native_popup_has_no_taskbar_entry_and_does_not_unhide_calendar(self):
        import win32con,win32gui
        popup=show_toast(self.root,'Your reminder is due.',duration_ms=25)
        self.assertTrue(popup.overrideredirect())
        self.assertTrue(popup.attributes('-toolwindow'))
        hwnd=win32gui.GetAncestor(popup.winfo_id(),2)
        flags=win32gui.GetWindowLong(hwnd,win32con.GWL_EXSTYLE)
        self.assertTrue(flags & 0x08000000)
        self.assertTrue(flags & win32con.WS_EX_TOOLWINDOW)
        self.assertFalse(flags & win32con.WS_EX_APPWINDOW)
        self.assertEqual(self.root.state(),'withdrawn')
        self.assertTrue(win32gui.IsWindowVisible(hwnd))
        self.root.after(80,self.root.quit)
        self.root.mainloop()
        self.assertFalse(popup.winfo_exists())
        self.assertEqual(self.errors,[])

    def test_reminder_defaults_to_four_seconds_and_preserves_actions(self):
        snooze,done=Mock(return_value=True),Mock(return_value=True)
        with patch('rocky.notifications.show_toast') as show:
            show_reminder(self.root,dict(id=1,body='Go to sleep',repeat='until_done'),on_snooze=snooze,on_done=done)
        self.assertEqual(TOAST_DURATION_MS,4000)
        self.assertEqual(show.call_args.kwargs['duration_ms'],4000)
        actions=dict(show.call_args.kwargs['actions'])
        actions['Snooze 15 min']()
        snooze.assert_called_once_with(15)
        actions['Done']()
        done.assert_called_once_with()

    def test_action_dismisses_and_cancels_auto_close_timer_without_errors(self):
        action=Mock(return_value=True)
        with patch('rocky.toast.show_without_activation'):
            popup=show_toast(self.root,'Reminder',actions=[('Done',action)],duration_ms=20)
        def descendants(widget):
            return [child for direct in widget.winfo_children() for child in [direct,*descendants(direct)]]
        button=next(child for child in descendants(popup) if child.winfo_class()=='Button' and child.cget('text')=='Done')
        button.invoke()
        self.assertFalse(popup.winfo_exists())
        self.root.after(50,self.root.quit)
        self.root.mainloop()
        action.assert_called_once()
        self.assertEqual(self.errors,[])

    def test_failed_action_keeps_notification_available_until_timeout(self):
        action=Mock(return_value=False)
        with patch('rocky.toast.show_without_activation'):
            popup=show_toast(self.root,'Reminder',actions=[('Snooze',action)])
        controls=popup.winfo_children()[0].winfo_children()[-1]
        controls.winfo_children()[0].invoke()
        self.assertTrue(popup.winfo_exists())

    def test_position_respects_work_area_and_negative_monitor_coordinates(self):
        self.assertEqual(toast_position((0,0,1920,1040),420,200),(1484,824))
        self.assertEqual(toast_position((-1920,-200,0,880),420,200),(-436,664))

    def test_reminder_is_not_acknowledged_while_another_notification_is_visible(self):
        manager=ToastQueue(self.root)
        try:
            with patch('rocky.toast.show_without_activation'):
                manager.show_now('Another notification')
            with tempfile.TemporaryDirectory() as folder:
                store=Store(Path(folder)/'test.db')
                now=datetime.now(timezone.utc)
                store.add_reminder('Durable reminder',now,'UTC','test')
                with self.assertRaises(RuntimeError):
                    ReminderDelivery(store,queue.Queue()).tick(lambda row:show_reminder(self.root,row,presenter=manager.show_now),now=now)
                self.assertEqual(len(store.due(now)),1)
                manager.current.destroy()
                with patch('rocky.toast.show_without_activation'):
                    ReminderDelivery(store,queue.Queue()).tick(lambda row:show_reminder(self.root,row,presenter=manager.show_now),now=now)
                self.assertEqual(store.due(now),[])
        finally:
            manager.close()

    def test_status_queue_coalesces_repeated_messages_and_is_bounded(self):
        manager=ToastQueue(self.root)
        try:
            manager.post('Same message')
            manager.post('Same message')
            self.assertEqual(len(manager.pending),1)
            for i in range(20):
                manager.post(f'Message {i}')
            self.assertEqual(len(manager.pending),8)
            with patch('rocky.toast.show_without_activation'):
                # Cancel the scheduled tick before invoking the same tick manually.
                self.root.after_cancel(manager.timer)
                manager.poll()
            self.assertTrue(manager.current.winfo_exists())
            self.assertFalse(manager.idle())
        finally:
            manager.close()


if __name__=='__main__':
    unittest.main()
