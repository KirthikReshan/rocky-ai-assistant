"""Local document reading, source retrieval and cancellable document analysis."""
from collections import Counter, OrderedDict
from bisect import bisect_right
from dataclasses import dataclass, replace
import difflib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from rocky.coding import Workspace, linked, SKIP_DIRS, SECRET_NAMES
from rocky.brain import BrainError, BrainCancelled, OllamaBrain
from rocky.document_io import Block, Content, SUFFIXES, MAX_FILE_BYTES, read_document
from rocky.document_memory import DocumentText, remove_legacy_index


def safe_document(root, relative):
    root = Path(root)
    raw = Path(relative)
    if raw.is_absolute() or '..' in raw.parts or ':' in str(raw) or not raw.parts:
        raise ValueError('Use a relative document path inside the selected folder.')
    current = root
    for component in (None, *raw.parts):
        if component is not None:
            if component.startswith('.') or component.casefold() in SKIP_DIRS:
                raise ValueError('That document location is excluded.')
            current /= component
        if linked(current):
            raise ValueError('Document links and junctions are excluded.')
    path = current.resolve()
    if (not path.is_relative_to(root.resolve()) or not path.is_file() or path.suffix.casefold() not in SUFFIXES
            or path.name.casefold() in SECRET_NAMES or re.search(r'^(?:secrets?|credentials?|tokens?)[._-]', path.name, re.I)):
        raise ValueError('Choose a supported document inside the selected folder.')
    if path.stat().st_nlink != 1 or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Document is linked or exceeds the 40 MB file limit.')
    return path


def extract_pdf(root, relative):
    content = read_document(safe_document(root, relative))
    return [(block.number, block.text) for block in content.blocks]


def pdf_worker(destination, root, relative, ocr='0'):
    # Keep the existing entry point for old launchers and packaged smoke checks.
    try:
        content = read_document(safe_document(root, relative), use_ocr=ocr == '1')
        result = content.to_dict()
        result['pages'] = [(block.number, block.text) for block in content.blocks]
    except Exception as exc:
        result = {'error': str(exc)[:500]}
    Path(destination).write_text(json.dumps(result), encoding='utf-8')


def read_content(root, relative, use_ocr=False, cancel=None, timeout=180):
    safe_document(root, relative)
    with tempfile.TemporaryDirectory() as folder:
        result_file = Path(folder) / 'document.json'
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--document-worker']
        else:
            command = [str(Path(sys.executable).with_name('python.exe')), '-m', 'rocky.documents']
        command += [str(result_file), str(root), relative, '1' if use_ocr else '0']
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                   cwd=Path(__file__).resolve().parents[1])
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel is not None and cancel.wait(.1):
                    raise BrainCancelled('Document task cancelled.')
                if cancel is None:
                    time.sleep(.1)
                if time.monotonic() > deadline:
                    raise ValueError('Document reading timed out. Try a smaller file or fewer scanned pages.')
            if process.returncode or not result_file.is_file():
                raise ValueError('Document extraction failed.')
            if result_file.stat().st_size > 12_000_000:
                raise ValueError('Extracted document exceeds the reading limit.')
            result = json.loads(result_file.read_text(encoding='utf-8'))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    if 'error' in result:
        raise ValueError(result['error'])
    return Content([Block(**block) for block in result['blocks']], result['warnings'])


def read_pages(root, relative):
    return [(block.number, block.text) for block in read_content(root, relative).blocks]


@dataclass(frozen=True)
class DocumentHit:
    source: str
    page: int
    excerpt: str
    relative: str | None = None
    location: str = ''
    section: str = ''
    kind: str = 'text'

    @property
    def label(self):
        return self.location or f'Page {self.page}'


@dataclass
class DocumentResult:
    answer: str
    hits: list
    status: str = ''


def select_scope(hits, scope):
    if not scope.strip():
        return hits
    value = scope.strip()
    match = re.fullmatch(r'(pages?|paragraphs?|lines?|slides?|rows?)\s+(\d+)(?:\s*(?:-|to|–)\s*(\d+))?', value, re.I)
    if match:
        unit, first, last = match[1].lower().rstrip('s'), int(match[2]), int(match[3] or match[2])
        if first < 1 or last < first:
            raise ValueError('Use an increasing range, such as pages 2-5.')
        selected = []
        for hit in hits:
            if unit == 'line' and hit.label.startswith('Lines '):
                start = hit.page
                lines = hit.excerpt.splitlines()
                a, b = max(first, start), min(last, start + len(lines) - 1)
                if a <= b:
                    selected.append(replace(hit, excerpt='\n'.join(lines[a-start:b-start+1]), location=f'Lines {a}–{b}', page=a))
            elif hit.label.casefold().startswith(unit + ' ') and first <= hit.page <= last:
                selected.append(hit)
        return selected
    heading = re.sub(r'^(?:section|heading|chapter)\s+', '', value, flags=re.I).strip(' "')
    return [hit for hit in hits if heading.casefold() in hit.section.casefold()]


def batches(hits, limit=6500):
    batch, size = [], 0
    for index, hit in enumerate(hits, 1):
        # Every character is covered, including very long PDF pages or paragraphs.
        for offset in range(0, len(hit.excerpt), 4500):
            text = hit.excerpt[offset:offset + 4500]
            item = f'[S{index}] {hit.source} — {hit.label}\n{text}\n'
            if batch and size + len(item) > limit:
                yield '\n'.join(batch)
                batch, size = [], 0
            batch.append(item)
            size += len(item)
    if batch:
        yield '\n'.join(batch)


def translation_chunks(text, limit=1000):
    while len(text) > limit:
        boundaries = list(re.finditer(r'(?<=[.!?])\s+|\n', text[:limit]))
        end = boundaries[-1].end() if boundaries else 0
        if end < limit // 2:
            end = text.rfind(' ', limit // 2, limit)
        end = end if end > 0 else limit
        yield text[:end]
        text = text[end:]
    if text:
        yield text


def needs_full_document(question):
    return bool(re.search(r'\b(?:all|every|each|entire|whole|overall|complete|total|totals|count|'
                          r'sum|summari[sz]e|overview|list|enumerate|compare|differences?|earliest|latest|'
                          r'highest|lowest|maximum|minimum|largest|smallest|biggest|most|least|cheapest)\b|\bhow many\b',
                          question, re.I))


def exact_passages(hits, query, limit=201):
    """Match wrapped phrases across adjacent source blocks without losing locations."""
    grouped = OrderedDict()
    for hit in hits:
        grouped.setdefault(hit.source, []).append(hit)
    pattern = re.compile(r'\s+'.join(re.escape(part) for part in query.split()), re.I)
    found = []
    for blocks in grouped.values():
        offsets, parts, size = [], [], 0
        for block in blocks:
            offsets.append(size)
            parts.append(block.excerpt)
            size += len(block.excerpt) + 1
        text = '\n'.join(parts)
        for match in pattern.finditer(text):
            first = blocks[bisect_right(offsets, match.start()) - 1]
            last = blocks[bisect_right(offsets, match.end() - 1) - 1]
            location = first.label if first is last else f'{first.label} through {last.label}'
            found.append(replace(first, excerpt=text[max(0, match.start()-180):match.end()+300], location=location))
            if len(found) >= limit:
                return found
    return found


class Documents:
    def __init__(self, store, brain, events):
        self.store, self.events = store, events
        self.memory_generation = 0
        self.closed = False
        if getattr(brain, 'supports_task_models', False) is True:
            self.brain = brain.fork_documents()
        else:
            self.brain = OllamaBrain(model=brain.model, timeout=brain.timeout) if isinstance(brain, OllamaBrain) else brain
        self.workspace = Workspace()
        self.selected = None
        self.use_ocr = False
        self._cache = OrderedDict()
        self._busy = threading.Lock()
        self._owner_lock = threading.RLock()
        self._owner = None
        self._cancel = threading.Event()
        self.index = None
        try:
            remove_legacy_index(store)
        except (OSError, sqlite3.Error):
            events.put(('info', 'The previous document index could not be removed. Close older Rocky instances and use Clear document memory.'))
        if getattr(self.brain, 'supports_task_models', False) is True:
            from rocky.document_index import DocumentIndex
            try:
                self.index = DocumentIndex(store, self.brain.config.embedding)
            except (sqlite3.Error, OSError):
                events.put(('info', 'The document search index is unavailable. Full-text reading remains available.'))

    def clear_memory(self, notify=True):
        with self._owner_lock:
            self.cancel()
            self.memory_generation += 1
            self.workspace.clear()
            self.selected = None
            self._cache.clear()
            if self.index is not None:
                self.index.clear()
        try:
            remove_legacy_index(self.store)
            status = 'Document memory cleared. Open a document to prepare it again.'
        except (OSError, sqlite3.Error):
            status = 'Session memory cleared. The previous disk index is busy or unrecognised; close older Rocky instances and retry.'
        if notify:
            self.events.put(('DocumentMemoryCleared', (self.workspace.identity, status)))
        return status

    def close(self):
        self.clear_memory(notify=False)
        self.closed = True
        if self.index is not None:
            self.index.close()

    def cancel(self):
        self._cancel.set()
        if self.brain is not None and callable(getattr(self.brain, 'cancel_current', None)):
            self.brain.cancel_current()

    def cancel_for_thread(self, owner):
        with self._owner_lock:
            if owner is not None and owner == self._owner:
                self.cancel()

    def begin_task(self, cancel=None):
        with self._owner_lock:
            self._owner = threading.get_ident()
            self._cancel = cancel if cancel is not None else threading.Event()

    def end_task(self):
        with self._owner_lock:
            self._owner = None

    def check(self, identity):
        if self.closed:
            raise BrainCancelled('The document session was closed.')
        if self._cancel.is_set():
            raise BrainCancelled('Document task cancelled.')
        if self.workspace.identity != identity:
            raise ValueError('Document folder selection changed. Please search again.')

    def library(self, limit=500):
        root = self.workspace.root
        if root is None:
            return []
        if linked(root):
            raise ValueError('Select a real document folder again.')
        found = []
        for folder, directories, files in os.walk(root, followlinks=False):
            directories[:] = sorted(d for d in directories if not d.startswith('.') and d.casefold() not in SKIP_DIRS
                                     and not linked(Path(folder) / d))
            for name in sorted(files):
                if Path(name).suffix.casefold() not in SUFFIXES or name.startswith('.'):
                    continue
                relative = (Path(folder) / name).relative_to(root).as_posix()
                try:
                    safe_document(root, relative)
                except (OSError, ValueError):
                    continue
                found.append(relative)
                if len(found) >= limit:
                    return found
        return found

    def load(self, relative, identity, timeout=180):
        self.check(identity)
        if identity[0] is None:
            raise ValueError('Choose a document folder or file on the Documents page.')
        path = safe_document(identity[0], relative)
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size, self.use_ocr)
        content = self._cache.get(key)
        if content is None:
            content = read_content(identity[0], relative, self.use_ocr, self._cancel, timeout)
            if path.stat().st_mtime_ns != stat.st_mtime_ns or path.stat().st_size != stat.st_size:
                raise ValueError('The document changed while reading it. Open it again.')
            self.check(identity)
            with self.workspace.guard(identity):
                self._cache[key] = content
                while len(self._cache) > 5:
                    self._cache.popitem(last=False)
        hits = [DocumentHit(str(path), block.number, block.text, relative, block.location, block.section, block.kind)
                for block in content.blocks]
        return hits, content.warnings

    def publish(self, identity, hits, status):
        self.check(identity)
        with self.workspace.guard(identity):
            self.events.put(('DocumentResults', (identity, hits, status)))

    def search(self, query, include_documents=True, relative=None, exact=False, scope=''):
        identity = self.workspace.identity
        if not query.strip() or len(query) > 500:
            raise ValueError('Enter a document question of 1–500 characters.')
        pattern = r'[^\W_]+(?:\+\+|#)?'
        terms = set(re.findall(pattern, query.casefold())) - {'the','and','what','how','for','are','with','about',
                'when','where','which','is','it','to','do','does','in','on','at','this','that','me','please'}
        if not exact and not terms:
            raise ValueError('Include a subject to search for.')
        pool, skipped, warnings = [], 0, []
        if relative is None:
            pool.extend(DocumentHit(f'Note {note["id"]}', 1, note['body']) for note in self.store.notes(limit=2000))
        count, deadline = 0, time.monotonic() + 25
        if identity[0] and include_documents:
            for name in [relative] if relative else self.library(limit=100):
                self.check(identity)
                if time.monotonic() > deadline and not relative:
                    warnings.append('Folder search reached its time limit. Select one file to search it fully.')
                    break
                try:
                    hits, notes = self.load(name, identity, timeout=180 if relative else 20)
                    pool.extend(select_scope(hits, scope))
                    warnings.extend(notes)
                except (OSError, ValueError) as exc:
                    if relative:
                        raise
                    skipped += 1
                count += 1
        if exact:
            found = exact_passages(pool, query)
            hits = found[:200]
            status = f'{len(hits)} matches. Read {count} documents. Skipped {skipped}.'
            if len(found) > 200:
                status += ' Showing the first 200 matches.'
            self.publish(identity, hits, status + ' ' + ' '.join(dict.fromkeys(warnings)))
            return hits
        ranked = []
        for hit in pool:
            self.check(identity)
            matches = list(re.finditer(pattern, hit.excerpt.casefold()))
            counts = Counter(match[0] for match in matches)
            score = sum(counts[word] for word in terms)
            if score:
                first = next(match.start() for match in matches if match[0] in terms)
                start = max(0, first - 160)
                ranked.append((score, replace(hit, excerpt=hit.excerpt[start:start + 1600])))
        limit = 40
        hits = [hit for _, hit in sorted(ranked, key=lambda item: -item[0])[:limit]]
        status = f'{len(hits)} matches. Read {count} documents. Skipped {skipped}.'
        if len(ranked) > limit:
            status += f' Showing the first {limit} matches.'
        if warnings:
            status += ' ' + ' '.join(dict.fromkeys(warnings))
        self.publish(identity, hits, status)
        return hits

    def ai(self, question, context, identity):
        self.check(identity)
        if self.brain is None:
            raise ValueError('Start Ollama for summaries and document questions.')
        answer = self.brain.document_chat(question, context)
        self.check(identity)
        supplied = set(re.findall(r'\[S\d+\]', context))
        cited = set(re.findall(r'\[S\d+\]', answer))
        if not cited.issubset(supplied):
            raise ValueError('The AI returned an unknown source reference. Please retry the question.')
        return answer

    def analyze(self, mode, query, hits, identity):
        if not hits:
            return 'No readable source text found for that selection.'
        if mode == 'Translate' and getattr(self.brain, 'supports_task_models', False) is True:
            from rocky.task_brain import translation_languages
            translation_languages(query or 'English')
            # The specialist receives document text, never filenames or citation
            # metadata. References are appended by Rocky after each translation.
            parts = [(index, text) for index, hit in enumerate(hits, 1)
                     for text in translation_chunks(hit.excerpt) if text.strip()]
            translated = []
            for number, (reference, text) in enumerate(parts, 1):
                self.check(identity)
                self.events.put(('DocumentProgress', (identity, f'Translating passage {number} of {len(parts)} locally…')))
                answer = self.brain.translate_text(query or 'English', text)
                self.check(identity)
                translated.append(answer + f'\n\nSource text: [S{reference}]')
            return '\n\n'.join(translated)
        questions = {
            'Summarize': 'Summarize all supplied content. ' + (query or 'Give a clear overview and the key points.'),
            'Section summaries': 'Summarize each named section separately, retaining headings. ' + query,
            'Ask': 'Answer this question using only supported evidence. Say when the answer is absent. ' + query,
            'Extract details': 'Extract the requested details faithfully, with citations. ' + query,
            'Find questions': 'List questions or exercises present in the source, retaining their numbering. ' + query,
            'Quiz': 'Create practice questions with a separate answer key and source citations. ' + query,
            'Flashcards': 'Create study flashcards as Question / Answer pairs with source citations. ' + query,
            'Explain': 'Explain the source in simple English with citations. ' + query,
            'Translate': 'Translate all supplied source text faithfully into ' + (query or 'English') + '. Preserve names, numbers, structure and source references.',
        }
        instruction = questions[mode]
        chunks = list(batches(hits))
        answers = []
        for index, context in enumerate(chunks, 1):
            self.events.put(('DocumentProgress', (identity, f'Reading source group {index} of {len(chunks)}…')))
            part_instruction = instruction
            if len(chunks) > 1 and mode not in {'Translate', 'Section summaries'}:
                part_instruction = ('This is an intermediate evidence extraction from ONE PART of a larger document. '
                    'Collect every fact in this part relevant to the request below, with source references. '
                    'Do not require complete coverage of the request in this part. Do not calculate a whole-document '
                    'total or conclude that facts missing from this part are absent from the other parts. '
                    'Keep exact dates, names and numbers. Request: ' + instruction)
            answers.append(self.ai(part_instruction, context, identity))
        if mode in {'Translate', 'Section summaries'}:
            return '\n\n'.join(answers)
        while len(answers) > 1:
            groups, group, length = [], [], 0
            for answer in answers:
                if group and length + len(answer) > 6500:
                    groups.append('\n\n'.join(group))
                    group, length = [], 0
                group.append(answer)
                length += len(answer)
            if group:
                groups.append('\n\n'.join(group))
            if len(groups) >= len(answers):
                # A model produced unusually long notes. Retain complete output.
                return '\n\n'.join(answers)
            self.events.put(('DocumentProgress', (identity, 'Combining the section results…')))
            answers = [self.ai('Combine evidence extracted from DIFFERENT PARTS of one document into one answer. '
                               'Take the union of supported facts from all parts. A fact absent in one part may '
                               'appear in another. Preserve every relevant named item, date and [S#] reference. ' + instruction,
                               group, identity) for group in groups]
        return answers[0]

    def semantic(self, query, hits, identity):
        if self.index is not None:
            try:
                if getattr(self.brain, 'supports_task_models', False) is True:
                    self.brain.label('Local · ' + self.brain.config.embedding + ' · document search')
                return self.index.search(hits, query, self._cancel,
                    progress=lambda text: self.events.put(('DocumentProgress', (identity, text))),
                    check=lambda: self.check(identity))
            except BrainCancelled:
                raise
            except (BrainError, sqlite3.Error, OSError):
                self.events.put(('DocumentProgress', (identity, 'Indexed search is unavailable. Checking passages with the local model…')))
        if self.brain is None:
            raise ValueError('Start Ollama to find passages by meaning.')
        if not hasattr(self.brain, 'document_matches'):
            raise ValueError('The current AI provider does not support passage matching.')
        chosen = []
        for index, context in enumerate(batches(hits), 1):
            self.check(identity)
            self.events.put(('DocumentProgress', (identity, f'Finding passages by meaning, group {index}…')))
            matches = self.brain.document_matches(query, context)
            self.check(identity)
            if not isinstance(matches, list):
                raise ValueError('The AI returned an invalid passage list. Please retry.')
            for match in matches:
                if not isinstance(match, dict):
                    continue
                source = match.get('source', '')
                quote = match.get('quote', '')
                if not isinstance(source, str):
                    continue
                number = re.fullmatch(r'S(\d+)', source)
                if not number or not isinstance(quote, str) or len(quote.strip()) < 3:
                    continue
                position = int(number[1]) - 1
                if 0 <= position < len(hits):
                    found = re.search(r'\s+'.join(re.escape(part) for part in quote.split()), hits[position].excerpt)
                    if found is None:
                        continue
                    result = replace(hits[position], excerpt=found[0])
                    if result not in chosen:
                        chosen.append(result)
        return chosen

    def perform(self, operation, relative='', query='', scope='', other='', identity=None, cancel=None):
        if not self._busy.acquire(blocking=False):
            raise ValueError('Another document task is running. Cancel it or wait for it to finish.')
        self.begin_task(cancel)
        identity = identity or self.workspace.identity
        try:
            self.check(identity)
            if operation in {'Search words', 'Exact phrase'}:
                hits = self.search(query, relative=relative or None, exact=operation == 'Exact phrase', scope=scope)
                return DocumentResult(f'Found {len(hits)} matching passages.', hits)
            if not relative:
                raise ValueError('Select a document first.')
            hits, warnings = self.load(relative, identity)
            prepared = None
            if self.index is not None and getattr(self.brain, 'supports_grounded_documents', False) is True and operation in {'Prepare document', 'Ask', 'Find by meaning'}:
                prepared = self.index.prepare(hits, self._cancel,
                    progress=lambda text: self.events.put(('DocumentProgress', (identity, text))),
                    check=lambda: self.check(identity))
                self.check(identity)
            hits = select_scope(hits, scope)
            if not hits:
                raise ValueError('No readable text matches that page, paragraph, line or section selection.')
            status = ' '.join(dict.fromkeys(warnings))
            if operation == 'Prepare document':
                if prepared is not None:
                    status += f' Ready: {len(prepared.chunks)} passages prepared in temporary memory. Ask a question below.'
                else:
                    status += ' Text preview only. Enable local AI to prepare the document for questions.'
                answer = ''
            elif operation == 'Read / jump':
                answer = '\n\n'.join(f'[S{i}] {hit.label}\n{hit.excerpt}' for i, hit in enumerate(hits, 1))
            elif operation == 'Tables':
                hits = [hit for hit in hits if hit.kind == 'table']
                answer = '\n'.join(f'[S{i}] {hit.label}\n{hit.excerpt}' for i, hit in enumerate(hits, 1)) or 'No structured tables found. For PDFs, use Extract details to request table contents from the page text.'
            elif operation == 'Find by meaning':
                if not query.strip():
                    raise ValueError('Enter the subject to find.')
                hits = self.semantic(query, hits, identity)
                answer = f'Found {len(hits)} candidate passages from the source text. Check their relevance to your question.'
            elif operation == 'Compare':
                if not other:
                    raise ValueError('Choose the second document to compare.')
                right, right_warnings = self.load(other, identity)
                left_text = '\n'.join(hit.excerpt for hit in hits).splitlines()
                right_text = '\n'.join(hit.excerpt for hit in right).splitlines()
                diff = '\n'.join(difflib.unified_diff(left_text, right_text, fromfile=relative, tofile=other, lineterm=''))
                answer = diff or 'The extracted text is identical.'
                hits += right
                status += ' Text comparison. Layout-only differences are not included. ' + ' '.join(right_warnings)
            else:
                if operation == 'Ask' and getattr(self.brain, 'supports_grounded_documents', False) is True:
                    if self.index is None:
                        raise BrainError('The document must be prepared before questions. Check the embedding model in AI models.')
                    from rocky.document_index import passage_chunks
                    from rocky.document_qa import GroundedQA
                    if needs_full_document(query) or sum(len(hit.excerpt) for hit in hits) <= 6500:
                        status += ' Reading the complete selection for this question.'
                        candidates = passage_chunks(hits)
                    else:
                        candidates = self.index.search(hits, query, self._cancel, limit=6, neighbors=True,
                            progress=lambda text: self.events.put(('DocumentProgress', (identity, text))),
                            check=lambda: self.check(identity))
                    answer, hits, detail = GroundedQA(self.brain).answer(query, candidates,
                        check=lambda: self.check(identity),
                        progress=lambda text: self.events.put(('DocumentProgress', (identity, text))))
                    self.check(identity)
                    return DocumentResult(answer, hits, status + ' ' + detail + ' ' + self.brain.last_label)
                use_retrieval = (operation == 'Ask' and self.index is not None
                                 and not needs_full_document(query)
                                 and sum(len(hit.excerpt) for hit in hits) > 6500)
                if operation == 'Ask' and needs_full_document(query):
                    status += ' Reading the complete selection for this whole-document question.'
                if use_retrieval:
                    try:
                        if getattr(self.brain, 'supports_task_models', False) is True:
                            self.brain.label('Local · ' + self.brain.config.embedding + ' · document search')
                        selected = self.index.search(hits, query, self._cancel,
                            progress=lambda text: self.events.put(('DocumentProgress', (identity, text))),
                            check=lambda: self.check(identity))
                        if selected:
                            hits = selected
                            status += ' Answer uses the top indexed passages. Summarize reads the whole selection.'
                    except BrainCancelled:
                        raise
                    except (BrainError, sqlite3.Error, OSError):
                        status += ' Indexed search unavailable; reading the full selection.'
                answer = self.analyze(operation, query, hits, identity)
                if operation == 'Translate':
                    status += ' Translation is generated by the local model. Check meaning, names and numbers before reuse.'
            self.check(identity)
            if getattr(self.brain, 'supports_task_models', False) is True and operation not in {'Read / jump', 'Tables', 'Compare', 'Prepare document'}:
                status += ' ' + self.brain.last_label
            return DocumentResult(answer, hits, status)
        finally:
            self.end_task()
            self._busy.release()

    def handle(self, text):
        raw = text.strip()
        if raw.casefold().rstrip('.!?') in {'clear document memory', 'clear pdf memory', 'forget uploaded documents'}:
            return self.clear_memory()
        search = re.fullmatch(r'(?:search|find) (?:my )?(documents|docs|notes) for (.+)', raw, re.I)
        ask = re.fullmatch(r'ask (?:my )?(?:documents|docs)[: ]+(.+)', raw, re.I)
        summary = re.fullmatch(r'summari[sz]e document[: ]+(.+)', raw, re.I)
        current = re.fullmatch(r'(summari[sz]e|explain|read|find questions in|make flashcards from|make a quiz from) (?:this|the selected) document', raw, re.I)
        selected_ask = re.fullmatch(r'(?:ask|question) (?:this|the selected) document[: ]+(.+)', raw, re.I)
        if not (search or ask or summary or current or selected_ask):
            if re.match(r'^(?:(?:search|find) (?:my )?(?:documents|docs|notes) for|ask (?:my )?(?:documents|docs)[: ]|summari[sz]e document)', raw, re.I):
                return 'Include a subject, document question or relative filename.'
            return None
        identity = self.workspace.identity
        generation = self.memory_generation
        tag = lambda value: DocumentText(value, self, generation)
        try:
            if ask and getattr(self.brain, 'supports_grounded_documents', False) is True:
                if not self.selected:
                    return 'Open a document first so Rocky can prepare it before answering.'
                result = self.perform('Ask', self.selected, ask[1], identity=identity)
                self.events.put(('DocumentAnswer', (identity, result)))
                return tag(result.answer if len(result.answer) < 1800 else 'The supported answer and its evidence are on the Documents page.')
            if search or ask:
                if not self._busy.acquire(blocking=False):
                    return 'Another document task is running. Cancel it or wait for it to finish.'
                try:
                    self.begin_task()
                    hits = self.search(search[2] if search else ask[1], include_documents=not search or search[1].casefold() != 'notes')
                    if search:
                        return tag(f'Found {len(hits)} text matches. Sources and excerpts are on the Documents page.')
                    if not hits:
                        return 'No matching text found. Try Find by meaning on the Documents page.'
                    answer = self.analyze('Ask', ask[1], hits, identity)
                    self.events.put(('DocumentAnswer', (identity, DocumentResult(answer, hits, 'Answer uses the displayed search passages.'))))
                    return tag(answer)
                finally:
                    self.end_task()
                    self._busy.release()
            modes = {'summarize': 'Summarize', 'summarise': 'Summarize', 'explain': 'Explain', 'read': 'Read / jump',
                     'find questions in': 'Find questions', 'make flashcards from': 'Flashcards', 'make a quiz from': 'Quiz'}
            operation = 'Summarize' if summary else 'Ask' if selected_ask else modes[current[1].lower()]
            relative = summary[1].strip().strip('"') if summary else self.selected
            result = self.perform(operation, relative, selected_ask[1] if selected_ask else '', identity=identity)
            self.events.put(('DocumentAnswer', (identity, result)))
            return tag(result.answer if len(result.answer) < 1800 else 'The document result is ready on the Documents page. Select Read answer to hear it.')
        except (OSError, ValueError, BrainError) as exc:
            if self.workspace.identity != identity:
                return tag('Document folder selection changed. Please search again.')
            return tag(f'Could not read documents: {exc}')


if __name__ == '__main__':
    pdf_worker(*sys.argv[1:5])
