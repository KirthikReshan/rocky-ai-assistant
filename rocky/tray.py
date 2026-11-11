"""Windows notification-area icon, owned by the Tk UI thread."""

import win32api
import win32con
import win32gui


class TrayIcon:
    CALLBACK = win32con.WM_APP + 1
    OPEN = 1
    QUIT = 2

    def __init__(self, events, name='Rocky',open_label=None,quit_label=None):
        self.events = events
        self.name = name
        self.open_label=open_label or f'Open {name}'
        self.quit_label=quit_label or f'Quit {name}'
        self.visible = False
        self.taskbar_created = win32gui.RegisterWindowMessage('TaskbarCreated')
        window_class = win32gui.WNDCLASS()
        window_class.hInstance = win32api.GetModuleHandle(None)
        window_class.lpszClassName = f'RockyTray{win32api.GetCurrentProcessId()}'
        window_class.lpfnWndProc = self._handle_message
        self.window_class = window_class
        self.icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        atom = win32gui.RegisterClass(window_class)
        try:
            self.window = win32gui.CreateWindow(
                atom, 'Rocky', 0, 0, 0, 0, 0, 0, 0, window_class.hInstance, None
            )
        except Exception:
            win32gui.UnregisterClass(window_class.lpszClassName, window_class.hInstance)
            raise

    def show(self):
        if not self.visible:
            flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_ADD, (self.window, 0, flags, self.CALLBACK, self.icon, self.name)
            )
            self.visible = True

    def close(self):
        try:
            if self.visible:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.window, 0))
        finally:
            win32gui.DestroyWindow(self.window)
            win32gui.UnregisterClass(self.window_class.lpszClassName, self.window_class.hInstance)

    def _handle_message(self, hwnd, message, wparam, lparam):
        if message == self.taskbar_created and self.visible:
            self.visible = False
            try:
                self.show()
            except win32gui.error:
                self.events.put(('tray', 'restore'))
                self.events.put(('info', 'The notification area is unavailable. Rocky reopened.'))
            return 0
        if message == self.CALLBACK:
            if lparam == win32con.WM_LBUTTONDBLCLK:
                self.events.put(('tray', 'restore'))
            elif lparam == win32con.WM_RBUTTONUP:
                self._show_menu(hwnd)
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def _show_menu(self, hwnd):
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING, self.OPEN, self.open_label)
            win32gui.AppendMenu(menu, win32con.MF_STRING, self.QUIT, self.quit_label)
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(hwnd)
            choice = win32gui.TrackPopupMenu(
                menu, win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON, x, y, 0, hwnd, None
            )
            if choice == self.OPEN:
                self.events.put(('tray', 'restore'))
            elif choice == self.QUIT:
                self.events.put(('tray', 'quit'))
        finally:
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
            win32gui.DestroyMenu(menu)
