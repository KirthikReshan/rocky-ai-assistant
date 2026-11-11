"""Windows-exclusive file access held from validation through commit/rollback."""
from contextlib import contextmanager
import os


@contextmanager
def exclusive_file(path):
    if os.name != 'nt':
        raise OSError('Approved editing requires Windows exclusive file locking')
    import msvcrt
    import win32con
    import win32file
    import pywintypes
    try:
        handle = win32file.CreateFile(str(path), win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                      0, None, win32con.OPEN_EXISTING,
                                      win32con.FILE_ATTRIBUTE_NORMAL | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
    except pywintypes.error as exc:
        raise OSError('Cannot exclusively lock this file. Close other editors or writers and request a fresh preview.') from exc
    raw_handle = handle.Detach()
    try:
        descriptor = msvcrt.open_osfhandle(raw_handle, os.O_RDWR | os.O_BINARY)
    except Exception:
        win32file.CloseHandle(raw_handle)
        raise
    try:
        stream = os.fdopen(descriptor, 'r+b')
    except Exception:
        os.close(descriptor)
        raise
    with stream:
        yield stream


def write_exact(stream, data):
    stream.seek(0)
    count = stream.write(data)
    if count != len(data):
        raise OSError('Incomplete source write')
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())
    stream.seek(0)
    if stream.read(len(data) + 1) != data:
        raise OSError('Source write verification failed')
