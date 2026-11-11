"""Exact, expiring GUI approvals for polite window closing and Windows power actions."""
from dataclasses import dataclass
from pathlib import Path
import threading
import time
import uuid
from rocky.tools import APPS, normalize_app_target

_power_lock = threading.Lock()


def claims_pc_action(text):
    import re
    return bool(re.search(r"\bi(?:['’]ve| have)?\s+(?:closed|terminated|restarted|rebooted|shut down)\b|"
                          r"\b(?:shutting down|restarting|rebooting) (?:windows|your (?:pc|computer|laptop)) now\b",
                          text, re.I))


def windows_power(kind):
    """Use the non-forcing Win32 API: shutdown.exe /t > 0 implies /f."""
    import ctypes
    from ctypes import wintypes
    import win32api
    import win32security
    with _power_lock:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                               win32security.TOKEN_ADJUST_PRIVILEGES | win32security.TOKEN_QUERY)
        previous = None
        try:
            privilege = win32security.LookupPrivilegeValue(None, 'SeShutdownPrivilege')
            previous = win32security.AdjustTokenPrivileges(token, False, [(privilege, win32security.SE_PRIVILEGE_ENABLED)])
            api = ctypes.WinDLL('advapi32', use_last_error=True)
            if kind == 'abort_shutdown':
                call = api.AbortSystemShutdownW
                call.argtypes = [wintypes.LPWSTR]
                call.restype = wintypes.BOOL
                accepted = call(None)
            else:
                call = api.InitiateSystemShutdownExW
                call.argtypes = [wintypes.LPWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.BOOL, wintypes.DWORD]
                call.restype = wintypes.BOOL
                accepted = call(None, 'Rocky: user-approved PC action. Save your work.', 30, False, kind == 'restart', 0x80040000)
            if not accepted:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            try:
                if previous is not None:
                    win32security.AdjustTokenPrivileges(token, False, previous)
            finally:
                token.Close()


@dataclass(frozen=True)
class WindowTarget:
    hwnd: int
    pid: int
    executable: str
    created: str
    title: str


def window_identity(hwnd):
    import win32api
    import win32process
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    handle = win32api.OpenProcess(0x0400 | 0x0010, False, pid)
    try:
        return pid, win32process.GetModuleFileNameEx(handle, 0), str(win32process.GetProcessTimes(handle)['CreationTime'])
    finally:
        handle.Close()


def close_targets(name):
    import win32gui
    from pywintypes import error as WindowsError
    import ctypes
    from ctypes import wintypes
    get_shell = ctypes.windll.user32.GetShellWindow
    get_shell.restype = wintypes.HWND
    get_shell.argtypes = []
    shell = get_shell()
    known = {**APPS, 'whatsapp': 'whatsapp.exe', 'codex': 'codex.exe', 'openai codex': 'codex.exe'}
    name = normalize_app_target(name).casefold()
    if name not in known:
        raise ValueError('Use a supported app name: WhatsApp, VS Code, Codex, Chrome, Edge, Notepad, Calculator or Paint.')
    expected = {known[name].casefold()}
    if name == 'calculator':
        expected.add('calculatorapp.exe')
    targets = []
    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or hwnd == shell:
            return
        try:
            pid, executable, created = window_identity(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if (Path(executable).name.casefold() in expected and title and
                    ('explorer.exe' not in expected or win32gui.GetClassName(hwnd) in {'CabinetWClass','ExploreWClass'})):
                targets.append(WindowTarget(hwnd, pid, executable, created, title))
        except (OSError, WindowsError):
            pass
    win32gui.EnumWindows(collect, None)
    if not targets:
        raise ValueError('No accessible window for that app is open.')
    return tuple(targets)


def perform(kind, targets=()):
    if kind == 'close':
        import win32gui
        for target in targets:
            if (not win32gui.IsWindow(target.hwnd) or window_identity(target.hwnd) != (target.pid, target.executable, target.created)
                    or win32gui.GetWindowText(target.hwnd) != target.title):
                raise ValueError('A window changed. Request a new close preview.')
        for target in targets:
            win32gui.PostMessage(target.hwnd, 0x0010, 0, 0)  # WM_CLOSE; no forced termination.
        return 'Asked the selected windows to close. Save any unsaved-work dialogs.'
    if kind not in {'shutdown', 'restart', 'abort_shutdown'}:
        raise ValueError('Unsupported PC action.')
    windows_power(kind)
    return 'Windows countdown cancelled.' if kind == 'abort_shutdown' else 'Windows countdown started: 30 seconds. Say cancel shutdown to abort it.'


@dataclass(frozen=True)
class Approval:
    id: str
    title: str
    details: str


class PCControls:
    def __init__(self, events, clock=time.monotonic):
        self.events, self.clock = events, clock
        self._pending = {}
        self._lock = threading.Lock()
        self._action_lock = threading.RLock()

    def request(self, kind, target=''):
        targets = close_targets(target) if kind == 'close' else ()
        if kind not in {'close', 'shutdown', 'restart'}:
            raise ValueError('That PC action is not supported.')
        details = ('Close only these windows:\n' + '\n'.join(f'{t.title}\n{t.executable} (PID {t.pid})' for t in targets)
                   + '\n\nWindows may ask you to save unsaved work.' if kind == 'close' else
                   f'{kind.title()} Windows after a 30-second countdown. Save your work first. '
                   'You can say cancel shutdown during the countdown. Applications will not be forcibly terminated.')
        proposal = Approval(uuid.uuid4().hex, f'Approve {kind}', details)
        with self._lock:
            self._pending.clear()  # A new request invalidates older approval windows.
            self._pending[proposal.id] = (kind, targets, self.clock() + 60)
        self.events.put(('PCApproval', proposal))
        return 'Review the PC action dialog and click Approve to allow this exact action. Voice or text cannot approve it.'

    def reject(self, token):
        with self._lock:
            self._pending.pop(token, None)

    def cancel_pending(self):
        with self._lock:
            count = sum(item[2] > self.clock() for item in self._pending.values())
            self._pending.clear()
        return bool(count)

    def has_pending(self):
        with self._lock:
            return any(item[2] > self.clock() for item in self._pending.values())

    def approve(self, token):
        with self._action_lock:
            with self._lock:
                pending = self._pending.pop(token, None)
            if pending is None or self.clock() >= pending[2]:
                raise ValueError('This approval expired or was replaced. Request the action again.')
            return perform(pending[0], pending[1])

    def cancel_power(self):
        # Serialize with approval execution: an abort cannot overtake a shutdown
        # that has already been approved but is still entering the Windows API.
        with self._action_lock:
            cancelled = self.cancel_pending()
            try:
                return perform('abort_shutdown')
            except OSError as exc:
                if getattr(exc, 'winerror', None) == 1116:
                    return ('Pending PC approvals cancelled. ' if cancelled else '') + 'No Windows countdown was active.'
                raise
