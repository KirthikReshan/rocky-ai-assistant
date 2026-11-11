"""Opt-in shortcut in the current Windows user's Startup folder."""

import subprocess
from pathlib import Path

from rocky.runtime import calendar_command


class CalendarStartup:
    def __init__(self, database, key):
        from win32com.client import Dispatch

        self.shell = Dispatch('WScript.Shell')
        self.database = database.resolve()
        self.path = Path(self.shell.SpecialFolders('Startup')) / f'Rocky Calendar {key[:12]}.lnk'

    def enabled(self):
        return self.path.is_file()

    def set_enabled(self, enabled):
        if not enabled:
            self.path.unlink(missing_ok=True)
            return
        shortcut = self.shell.CreateShortcut(str(self.path))
        command = calendar_command(self.database)
        shortcut.TargetPath = command[0]
        shortcut.Arguments = subprocess.list2cmdline(command[1:])
        shortcut.WorkingDirectory = str(Path(__file__).resolve().parents[1])
        shortcut.Description = 'Start Rocky Calendar reminders at sign-in'
        shortcut.Save()
