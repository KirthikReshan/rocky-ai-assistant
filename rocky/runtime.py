"""Launch a Rocky companion from source or from the Windows bundle."""

import sys
from pathlib import Path


def calendar_command(database, *, show=False):
    if getattr(sys, 'frozen', False):
        command = [sys.executable, '--calendar']
    else:
        command = [str(Path(sys.executable).with_name('pythonw.exe')), '-m', 'rocky.calendar_service']
    command.extend(['--database', str(database.resolve())])
    if show:
        command.append('--show')
    return command


def calendar_page_command(database):
    if getattr(sys,'frozen',False):
        command=[sys.executable]
    else:
        command=[str(Path(sys.executable).with_name('pythonw.exe')),'-m','rocky']
    return command+['--calendar-page','--database',str(Path(database).resolve())]
