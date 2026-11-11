"""Bounded project reading and edit-proposal routing. No direct source writes."""
from pathlib import Path
import os
import re
import stat
import threading
import time
import json
from contextlib import contextmanager

from rocky.brain import BrainError
from rocky.project_index import ProjectIndex

EXTENSIONS = {'.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css', '.scss', '.json', '.toml',
              '.yaml', '.yml', '.md', '.txt', '.sql', '.java', '.c', '.cpp', '.h', '.cs', '.go', '.rs'}
SKIP_DIRS = {'venv', 'node_modules', '__pycache__', 'dist', 'build', 'releases', 'models', 'site-packages',
             'coverage', 'target', 'vendor', 'local-data'}
SECRET_NAMES = {'secrets.json', 'credentials.json', 'token.json', 'tokens.json', 'secrets.yaml',
                'secrets.yml', 'credentials.yaml', 'credentials.yml', 'service-account.json'}


def linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def allowed_name(path):
    return (not path.name.startswith('.') and path.name.lower() not in SECRET_NAMES
            and path.suffix.lower() in EXTENSIONS)


class Workspace:
    def __init__(self):
        self._root = None
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def root(self):
        with self._lock:
            return self._root

    def select(self, folder):
        raw = Path(folder).absolute()
        if linked(raw):
            raise ValueError('Choose a real project folder, not a folder link or junction.')
        path = raw.resolve()
        if not path.is_dir() or path == Path(path.anchor) or path == Path.home().resolve():
            raise ValueError('Choose a specific project folder, rather than a drive or home folder.')
        with self._lock:
            self._root = path
            self._generation += 1
        return path

    def clear(self):
        with self._lock:
            self._root = None
            self._generation += 1

    @property
    def identity(self):
        with self._lock:
            return self._root, self._generation

    @contextmanager
    def guard(self, identity):
        with self._lock:
            if (self._root, self._generation) != identity:
                raise ValueError('Code folder selection changed. Request a new edit preview.')
            yield

    def snapshot(self):
        root = self.root
        if root is None:
            raise ValueError('Choose a code folder in the window first.')
        return Project(root)


class Project:
    def __init__(self, root):
        self.root = root

    def safe_path(self, relative):
        raw = Path(relative.strip().strip('"'))
        if raw.is_absolute() or '..' in raw.parts or ':' in str(raw):
            raise ValueError('Use a relative file path inside the selected code folder.')
        path = self.root
        if linked(path):
            raise ValueError('The project folder became a link. Select the project again.')
        for component in raw.parts:
            if component.startswith('.') or component.lower() in SKIP_DIRS:
                raise ValueError('That file is excluded from project reading.')
            path = path / component
            if linked(path):
                raise ValueError('Links and junctions are excluded from project reading.')
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or not allowed_name(resolved) or not resolved.is_file():
            raise ValueError('Choose a supported source file inside the selected code folder.')
        return resolved

    def source(self, relative):
        path = self.safe_path(relative)
        # Bound bytes even if a file grows after checking its size.
        with path.open('rb') as source:
            data = source.read(100001)
        if len(data) > 100000 or b'\x00' in data:
            raise ValueError('That file is binary or exceeds the 100 KB reading limit.')
        text = data.decode('utf-8-sig')
        return path, text

    def read(self, relative, limit=6000):
        path, text = self.source(relative)
        numbered = '\n'.join(f'{i}: {line}' for i, line in enumerate(text.splitlines(), 1))
        truncated = len(numbered) > limit
        return f'FILE {path.relative_to(self.root).as_posix()}\n{numbered[:limit]}' + ('\n[TRUNCATED]' if truncated else '')

    def files(self):
        if linked(self.root):
            raise ValueError('The project folder became a link. Select the project again.')
        found = []
        deadline = time.monotonic() + 2
        visited = 0
        for directory, folders, names in os.walk(self.root, followlinks=False):
            if time.monotonic() > deadline or visited >= 10000:
                return found, True
            folders[:] = sorted(name for name in folders if not name.startswith('.') and name.lower() not in SKIP_DIRS
                                and not linked(Path(directory) / name))
            for name in sorted(names):
                visited += 1
                path = Path(directory) / name
                if allowed_name(path) and not linked(path):
                    found.append(path.relative_to(self.root).as_posix())
                if len(found) >= 2000 or visited >= 10000:
                    return found, True
        return found, False

    def search(self, query):
        files, incomplete = self.files()
        matches = []
        budget = 2000000
        for name in files:
            try:
                path = self.safe_path(name)
                if path.stat().st_size > 100000:
                    continue
                with path.open('rb') as source:
                    data = source.read(min(100001, budget + 1))
                budget -= len(data)
                if budget < 0:
                    return matches, True
                if b'\x00' in data:
                    continue
                for line_no, line in enumerate(data.decode('utf-8-sig').splitlines(), 1):
                    if query.lower() in line.lower():
                        matches.append(f'{name}:{line_no}: {line[:180]}')
                        if len(matches) == 30:
                            return matches, True
            except (OSError, ValueError, UnicodeError):
                continue
        return matches, incomplete


class CodeCommands:
    def __init__(self, workspace, brain=None, display=None, editor=None, preview=None):
        self.workspace = workspace
        self.brain = brain
        self.display = display
        self.editor = editor
        self.preview = preview
        self.index = ProjectIndex()

    def status_context(self):
        root = self.workspace.root
        if root is None:
            return 'Runtime state: No code folder is selected. User must choose a code folder before reading project files.'
        return ('Runtime state: The selected code project is ' + json.dumps(str(root)) +
                '. Rocky can read supported files and propose edits inside it; writes require approval of the exact UI diff. Selection is already complete; '
                'do not ask the user to select a folder again. Source content has not been provided for this chat.')

    def present(self, content, summary):
        if self.display:
            self.display(content)
            return summary
        return content

    def handle(self, text):
        raw = re.sub(r'^(?:(?:hey )?rocky[, ]+)?(?:(?:please|can you|could you|you can|would you)\s+)*',
                     '', text.strip(), flags=re.I)
        command = raw.lower().rstrip('.!?')
        review_requested = bool(re.search(r'\b(?:review|bugs?|errors?|audit)\b', command))
        edit = re.fullmatch(r'(?:propose edit|edit|fix) file (.+?)(?::\s*|\s+to\s+)(.+)', raw, re.I | re.S)
        if edit:
            if self.editor is None:
                return 'Edit previews are not enabled in this session.'
            try:
                proposal = self.editor.propose(edit[1].strip(), edit[2].strip())
                if self.preview:
                    self.preview(proposal)
                else:
                    self.present(proposal.diff, 'Edit preview prepared.')
                return f'Edit preview ready for {proposal.relative}. Review the diff and click Approve and apply to permit this exact change. No source file has been changed.'
            except (ValueError, OSError, BrainError) as exc:
                return f'I could not prepare that edit: {exc}'
        # Only conversational command punctuation is optional. The requested
        # replacement text above must retain characters such as ! and ?.
        raw = raw.rstrip('.!?')
        if command in {'approve edit', 'apply edit', 'confirm edit', 'apply the edit', 'yes apply it'}:
            return 'Review the exact diff and click Approve and apply in the edit preview. Voice or text alone cannot approve file changes.'
        if command in {'access my code', 'do you have access to my code', 'what code folder is selected',
                       'which folder is selected', 'what folder did i select'} or text.strip().lower().rstrip('.!?') == 'can you read my code':
            root = self.workspace.root
            return (f'Yes. I can read supported code in the selected project, {root}. Edits require approval in the preview; I cannot run project code.'
                    if root else 'Choose a code folder in the window first, then I can inspect it read-only.')
        # Route natural inspection requests only into the already selected project.
        # A misheard project name never changes the authorized root.
        if (re.match(r'^(?:go through|look through|look at|see through|check|review|read|explain|analy[sz]e|summari[sz]e)\b', command)
                and re.search(r'\b(?:code|codes|codebase|project)\b|\bselected folder\b', command)
                and not re.match(r'^(?:read|explain|review) file\b', command)
                and not command.startswith('explain error')):
            command = 'explain my project'
        match = re.fullmatch(r'(read file|explain file|review file|find file|search code for|explain error|ask my code|ask my project)[: ]+(.+)', raw, re.I | re.S)
        if not match and command not in {'list code files', 'explain my project', 'code folder', 'which code folder',
                                        'index my project', 'refresh project index'}:
            return None
        try:
            project = self.workspace.snapshot()
            if command in {'code folder', 'which code folder'}:
                return f'Selected code folder: {project.root}'
            files, incomplete = project.files()
            if command in {'index my project', 'refresh project index'}:
                self.index.refresh(project, files, incomplete, force=True)
                return self.present(self.index.description(), 'Project index refreshed. Details are in the Code tab.')
            if command == 'list code files':
                listing = '\n'.join(files[:100]) or 'No supported source files found.'
                if incomplete or len(files) > 100:
                    listing += '\n[File list limited; narrow your project folder if needed.]'
                return self.present(listing, f'Found {len(files)} supported files. The list is shown in the window.')
            kind, argument = (match[1].lower(), match[2].strip()) if match else ('project', '')
            if kind == 'find file':
                found = [name for name in files if argument.lower() in name.lower()][:30]
                return self.present('\n'.join(found) or 'No matching files found.', f'Found {len(found)} matching files; see the window.')
            if kind == 'search code for':
                found, limited = project.search(argument)
                result = '\n'.join(found) or 'No matching code found.'
                if limited:
                    result += '\n[Search limited; narrow the project folder for complete results.]'
                return self.present(result, f'Found {len(found)} matching lines; see the window.')
            if kind in {'read file', 'explain file', 'review file'}:
                context = project.read(argument)
                if kind == 'read file':
                    return self.present(context, f'Loaded {argument}. The code is shown in the window.')
                self.index.refresh(project, files, incomplete)
                related = self.index.related(project.safe_path(argument).relative_to(project.root).as_posix())
                context += '\n\n' + self.index.excerpts(project, raw, related, budget=3000)
            else:
                preferred = []
                traceback_lines = {}
                if kind == 'explain error':
                    for location in re.finditer(r'''File ["']([^"']+)["'](?:,\s*line\s+(\d+))?''', argument):
                        candidate = location[1]
                        path = Path(candidate)
                        if path.is_absolute():
                            try:
                                candidate = path.resolve().relative_to(project.root).as_posix()
                            except ValueError:
                                continue
                        else:
                            candidate = path.as_posix()
                        if candidate in files:
                            preferred.append(candidate)
                            if location[2]:
                                traceback_lines.setdefault(candidate, []).append(int(location[2]))
                else:
                    # Inventory spans all eligible folders; source analysis remains bounded.
                    for basename in ('architecture.md', 'pyproject.toml', 'package.json', 'requirements.txt',
                                     '__main__.py', 'main.py', 'app.py', 'assistant.py', 'voice.py', 'readme.md'):
                        preferred += [name for name in files if Path(name).name.lower() == basename][:1]
                    preferred += [name for name in files if name.endswith(('.py', '.ts', '.js'))][:3]
                self.index.refresh(project, files, incomplete)
                if kind in {'ask my code', 'ask my project'}:
                    preferred = self.index.select(argument)
                elif kind == 'explain error':
                    lookup = re.sub(r'''File ["'][^"']+["']''', '', argument)
                    preferred = self.index.select(lookup, preferred)
                blocks = []
                for name in dict.fromkeys(preferred):
                    try:
                        if kind in {'ask my code', 'ask my project', 'explain error'}:
                            blocks.append(self.index.excerpts(project, argument, [name], budget=2000,
                                                              focus_lines=traceback_lines.get(name, ())))
                        else:
                            blocks.append(project.read(name, limit=900 if kind == 'project' else 2500))
                    except (OSError, ValueError, UnicodeError):
                        continue
                    if sum(map(len, blocks)) > (5500 if kind == 'project' else 7000):
                        break
                context = '\n\n'.join(blocks)[:9000] or 'No relevant file context was identified. Explain the error generally and state that limitation.'
                if kind == 'project':
                    inventory = '\n'.join(files)[:2800]
                    scope = (f'Selected project: {project.root}\nEligible files found: {len(files)}. '
                             f'Inventory scan limited: {incomplete}. Source excerpts supplied: {len(blocks)}. '
                             'This is a bounded project overview, not a complete audit of every file.')
                    context = f'{scope}\nFile inventory (paths only):\n{inventory}\n\nSource excerpts:\n{context}'
            context = self.index.description() + '\n\n' + context
            if self.brain is None:
                return 'Start Ollama and launch Rocky without --offline for code explanations. File reading and search still work offline.'
            project_task = ('Review the supplied source excerpts for concrete bugs and explain the affected behavior. '
                            'Use file and line evidence; do not invent problems in uninspected code. '
                            if review_requested else
                            'Give an overview of this selected project and its modules using the inventory and excerpts. ')
            question = (project_task + 'Explain what was inspected and what remains unreviewed. '
                        'Do not ask for selection or deny file access. Original user request: ' + raw) if kind == 'project' else raw
            answer = self.brain.code_chat(question, context)
            if kind == 'project':
                answer = scope + '\n\n' + answer
                summary = f'I inspected the selected project, {project.root.name}. Found {len(files)} supported files and reviewed {len(blocks)} excerpts. The overview is in the Code tab.'
            else:
                summary = 'The code explanation is shown in the window.'
            return self.present(answer, summary)
        except (ValueError, OSError, UnicodeError, BrainError) as exc:
            return f'I could not inspect that code: {exc}'
