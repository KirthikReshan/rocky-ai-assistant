"""Single-file edit proposals. The model has no path selection or write authority."""
from dataclasses import dataclass
from pathlib import Path
import codecs
import difflib
import hashlib
import io
import json
import os
import stat
import threading
import time
import tokenize
import uuid

from rocky.coding import Project
from rocky.file_lock import exclusive_file, write_exact


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_python_encoding(data):
    """The preview and committed Python file must use the same text encoding."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        if codecs.lookup(encoding).name not in {'utf-8', 'utf-8-sig'}:
            raise ValueError('Python edit previews require a UTF-8 encoding declaration. Convert the file to UTF-8 first.')
    except (SyntaxError, LookupError) as exc:
        raise ValueError('The Python encoding declaration is invalid.') from exc


def reject_json_constant(value):
    raise ValueError(f'{value} is not a valid JSON number.')


def snapshot(project, relative):
    path = project.safe_path(relative)
    info = path.stat()
    if info.st_nlink != 1:
        raise ValueError('Hard-linked files cannot be edited.')
    if not info.st_mode & stat.S_IWUSR:
        raise ValueError('That file is read-only. Change its permissions yourself before requesting an edit.')
    with path.open('rb') as source:
        data = source.read(12001)
    if len(data) > 12000 or b'\x00' in data:
        raise ValueError('Edit previews currently support UTF-8 text files up to 12 KB. Choose a smaller file.')
    data.decode('utf-8-sig')
    if path.suffix.lower() == '.py':
        validate_python_encoding(data)
    return path, data, (info.st_dev, info.st_ino), stat.S_IMODE(info.st_mode)


def replacement(original, edits, suffix):
    if not isinstance(edits, list) or not 1 <= len(edits) <= 10:
        raise ValueError('Expected one to ten exact text replacements.')
    if suffix.lower() == '.py':
        validate_python_encoding(original)
    text = original.decode('utf-8-sig')
    newline = '\r\n' if '\r\n' in text else '\n'
    canonical = text.replace('\r\n', '\n')
    if '\r' in canonical or ('\r\n' in text and '\n' in text.replace('\r\n', '')):
        raise ValueError('Mixed or unsupported line endings. Normalize the file manually before requesting an edit.')
    spans = []
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'old', 'new'}:
            raise ValueError('Invalid replacement structure.')
        old, new = edit['old'], edit['new']
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError('Replacements must contain text.')
        old, new = old.replace('\r\n', '\n'), new.replace('\r\n', '\n')
        if '\r' in old or '\r' in new:
            raise ValueError('Replacement snippets contain unsupported line endings.')
        if not old or old == new or canonical.count(old) != 1:
            raise ValueError('Each original snippet must match exactly once and make a real change. Request a more specific edit.')
        start = canonical.index(old)
        spans.append((start, start + len(old), new))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError('Overlapping replacements are not allowed.')
    result = canonical
    for start, end, new in reversed(spans):
        result = result[:start] + new + result[end:]
    if not result.strip() or '\x00' in result:
        raise ValueError('Empty or binary replacements are not allowed.')
    if suffix.lower() == '.json':
        json.loads(result, parse_constant=reject_json_constant)
    encoded = result.replace('\n', newline).encode('utf-8')
    if original.startswith(b'\xef\xbb\xbf'):
        encoded = b'\xef\xbb\xbf' + encoded
    if len(encoded) > 100000:
        raise ValueError('The proposed file is too large.')
    if suffix.lower() == '.py':
        validate_python_encoding(encoded)
        try:
            compile(encoded, '<proposed code>', 'exec', dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f'Proposed Python code has a syntax error: {exc}') from exc
    return encoded


@dataclass(frozen=True)
class Proposal:
    id: str
    identity: tuple
    relative: str
    file_id: tuple
    mode: int
    original: bytes
    updated: bytes
    summary: str
    diff: str
    created: float


class EditService:
    def __init__(self, workspace, brain, backup_dir, clock=time.monotonic):
        self.workspace = workspace
        self.brain = brain
        self.backup_dir = Path(backup_dir)
        self.clock = clock
        self._pending = None
        self._lock = threading.Lock()
        self._serial = 0

    def discard(self, proposal_id):
        with self._lock:
            if self._pending and self._pending.id == proposal_id:
                self._pending = None

    def propose(self, relative, instruction):
        with self._lock:
            self._pending = None  # old approvals must not authorize a new proposal
            self._serial += 1
            serial = self._serial
        if self.brain is None:
            raise ValueError('Start Ollama and disable --offline to propose code edits.')
        if not instruction or len(instruction) > 2000:
            raise ValueError('Describe the requested change in 1–2,000 characters.')
        identity = self.workspace.identity
        if identity[0] is None:
            raise ValueError('Choose a code folder first.')
        project = Project(identity[0])
        with self.workspace.guard(identity):
            path, original, file_id, mode = snapshot(project, relative)
            relative = path.relative_to(project.root).as_posix()
        response = self.brain.propose_edit(instruction, relative, original.decode('utf-8-sig').replace('\r\n', '\n'))
        if not isinstance(response, dict) or set(response) != {'summary', 'edits'} or not isinstance(response['summary'], str):
            raise ValueError('Model returned an invalid edit proposal.')
        summary = response['summary'][:1000]
        summary.encode('utf-8')
        updated = replacement(original, response['edits'], path.suffix)
        if updated == original:
            raise ValueError('The proposed edits make no net change.')
        before = original.decode('utf-8-sig').replace('\r\n', '\n')
        after = updated.decode('utf-8-sig').replace('\r\n', '\n')
        lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                     fromfile=relative + ' (current)', tofile=relative + ' (proposed)')
        diff = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)
        proposal = Proposal(uuid.uuid4().hex, identity, relative, file_id, mode, original, updated,
                            summary, diff, self.clock())
        with self.workspace.guard(identity), self._lock:
            if serial != self._serial:
                raise ValueError('A newer edit request superseded this preview.')
            self._pending = proposal
        return proposal

    def apply(self, proposal_id):
        """Called only by the exact proposal's approval button, never the command router."""
        with self._lock:
            proposal = self._pending
            if proposal is None or proposal.id != proposal_id:
                raise ValueError('This edit preview is no longer current. Request a new one.')
            self._pending = None  # one-shot approval, including failed attempts
        if self.clock() - proposal.created > 600:
            raise ValueError('This edit preview expired. Request a fresh preview.')
        with self.workspace.guard(proposal.identity):
            project = Project(proposal.identity[0])

            path = project.safe_path(proposal.relative)
            # Deny other readers/writers and renames. An atomic path replacement
            # would require releasing this lock, reopening the lost-update race.
            with exclusive_file(path) as source:
                info = os.fstat(source.fileno())
                current = source.read(12001)
                if ((info.st_dev, info.st_ino) != proposal.file_id or current != proposal.original
                        or stat.S_IMODE(info.st_mode) != proposal.mode or info.st_nlink != 1
                        or not stat.S_ISREG(info.st_mode)):
                    raise ValueError('The source file changed after preview. Nothing was applied; request a new preview.')
                backup = self.backup_dir / proposal.id
                backup.mkdir(parents=True, exist_ok=False)
                with (backup / 'original.bin').open('xb') as output:
                    output.write(proposal.original)
                    output.flush()
                    os.fsync(output.fileno())
                manifest = {'path': str(path), 'proposal_id': proposal.id,
                            'commit_strategy': 'exclusive-handle-write',
                            'original_sha256': digest(proposal.original), 'proposed_sha256': digest(proposal.updated)}
                with (backup / 'manifest.json').open('x', encoding='utf-8') as output:
                    json.dump(manifest, output, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    write_exact(source, proposal.updated)
                except Exception as exc:
                    try:
                        write_exact(source, proposal.original)
                    except Exception as restore_error:
                        raise OSError(f'Edit and automatic restore failed. Recover original bytes from {backup / "original.bin"}') from restore_error
                    raise OSError(f'Edit failed; original file restored. Backup: {backup}') from exc
                return backup
