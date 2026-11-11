"""One Rocky window per database; launchers can request its Calendar page."""
import hashlib
import os
from pathlib import Path


def database_key(database):
    return hashlib.sha256(os.path.normcase(str(Path(database).resolve())).encode('utf-8')).hexdigest()


class AppInstance:
    def __init__(self,database):
        import win32api,win32event,winerror
        key=database_key(database)
        self.show=win32event.CreateEvent(None,False,False,f'Local\\RockyMainShow-{key}')
        self.calendar=win32event.CreateEvent(None,False,False,f'Local\\RockyMainCalendar-{key}')
        self.mutex=win32event.CreateMutex(None,True,f'Local\\RockyMain-{key}')
        existing=win32api.GetLastError()==winerror.ERROR_ALREADY_EXISTS
        self.primary=not existing or win32event.WaitForSingleObject(self.mutex,0) in (win32event.WAIT_OBJECT_0,win32event.WAIT_ABANDONED)

    def request(self,calendar=False):
        import win32event
        win32event.SetEvent(self.calendar if calendar else self.show)

    def take(self,calendar=False):
        import win32event
        return win32event.WaitForSingleObject(self.calendar if calendar else self.show,0)==win32event.WAIT_OBJECT_0

    def close(self):
        import win32event
        try:
            if self.primary:
                win32event.ReleaseMutex(self.mutex)
        finally:
            self.mutex.Close()
            self.calendar.Close()
            self.show.Close()
