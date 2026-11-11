"""Document-session markers and removal of Rocky's old derived index only."""
from pathlib import Path
import sqlite3
import weakref


class DocumentText(str):
    def __new__(cls, value, service, generation=None):
        item = super().__new__(cls, value)
        item.service = weakref.ref(service)
        item.generation = service.memory_generation if generation is None else generation
        return item

    def current(self):
        service = self.service()
        return service is not None and not service.closed and service.memory_generation == self.generation


def clear_document_transcript(widget):
    widget.configure(state='normal')
    ranges = list(widget.tag_ranges('document-memory'))
    for index in range(len(ranges)-2, -1, -2):
        widget.delete(ranges[index], ranges[index+1])
    widget.configure(state='disabled')


def remove_legacy_index(store):
    """Only remove the known vectors-only database produced by Rocky 0.13.x."""
    from rocky.coding import linked
    path = store.path.with_name(store.path.stem + '-document-index.sqlite3')
    if not path.exists():
        return False
    if linked(path) or path.stat().st_nlink != 1 or not path.is_file():
        raise OSError('The old document index is linked and could not be removed safely.')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=.2)
    try:
        db.execute('BEGIN EXCLUSIVE')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = [row[1] for row in db.execute('PRAGMA table_info(vectors)')]
        if tables != {'vectors'} or columns != ['key', 'created', 'rows', 'columns', 'data']:
            raise OSError('The old index filename contains an unrecognised database; it was preserved.')
        db.rollback()
    finally:
        db.close()
    path.unlink()
    # The old implementation used SQLite's default rollback journal, not WAL.
    journal = Path(str(path) + '-journal')
    if journal.is_file() and not linked(journal) and journal.stat().st_nlink == 1:
        journal.unlink()
    return True
