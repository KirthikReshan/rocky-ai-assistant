"""Session-only document embeddings. This module never writes a new index file."""
from collections import OrderedDict
from dataclasses import replace
from dataclasses import dataclass
import hashlib
import json
import threading

import numpy as np

from rocky.brain import BrainError, BrainCancelled
from rocky.model_transport import installed_models, local_json, local_slot
from rocky.document_retrieval import hybrid_order

MAX_CHUNKS = 1600
MAX_CACHE_BYTES = 64_000_000


def passage_chunks(hits):
    # Pack short adjacent rows/paragraphs so a spreadsheet with thousands of
    # small cells does not require one embedding for each row. Physical PDF
    # pages and slide boundaries retain their own citations.
    packed, pending = [], []
    length = 0
    def flush():
        nonlocal pending, length
        if pending:
            first, last = pending[0], pending[-1]
            packed.append(replace(first, excerpt='\n'.join(item.excerpt for item in pending),
                                  location=first.label if len(pending) == 1 else f'{first.label} through {last.label}'))
        pending, length = [], 0
    for hit in hits:
        physical = hit.label.startswith(('Page ', 'Slide ', 'Image '))
        compatible = (pending and pending[0].source == hit.source and pending[0].section == hit.section
                      and pending[0].kind == hit.kind)
        if physical or len(hit.excerpt) > 500:
            flush()
            packed.append(hit)
        else:
            if pending and (not compatible or length + len(hit.excerpt) + 1 > 1000):
                flush()
            pending.append(hit)
            length += len(hit.excerpt) + 1
    flush()
    chunks = []
    for hit in packed:
        for start in range(0, len(hit.excerpt), 800):
            excerpt = hit.excerpt[start:start + 1000]
            if excerpt.strip():
                chunks.append(replace(hit, excerpt=excerpt))
            if len(chunks) > MAX_CHUNKS:
                raise BrainError('Too much text for indexed search. Select a smaller document section.')
    return chunks


def normalized_vectors(values, count):
    try:
        vectors = np.asarray(values, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != count or not 1 <= vectors.shape[1] <= 4096:
            raise ValueError('Invalid embedding shape')
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if not np.isfinite(vectors).all() or not np.isfinite(norms).all() or (norms <= 0).any():
            raise ValueError('Invalid vectors')
        return vectors / norms
    except (ValueError, TypeError, OverflowError):
        raise BrainError('The embedding model returned invalid search vectors.') from None


class EmbeddingClient:
    def __init__(self, model):
        self.model = model

    def digest(self):
        models = installed_models()
        digest = models.get(self.model) or models.get(self.model + ':latest')
        if not isinstance(digest, str) or not digest:
            raise BrainError(f'Install {self.model} in Ollama to use indexed search.')
        return digest

    def embed(self, texts, cancel):
        with local_slot(self.model, cancel, generative=False):
            # Search runs on CPU so the main model retains the GPU's 6 GB.
            result = local_json('embed', {'model': self.model, 'input': texts, 'truncate': False,
                                         'keep_alive': '5m', 'options': {'num_gpu': 0}}, timeout=90)
        if cancel.is_set():
            raise BrainCancelled('Document task cancelled.')
        return normalized_vectors(result.get('embeddings'), len(texts))


class DocumentIndex:
    def __init__(self, store, model, client=None):
        self.client = client or EmbeddingClient(model)
        self.model = model
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.generation = 0
        self.closed = False

    @property
    def memory_bytes(self):
        with self.lock:
            return sum(entry.size for entry in self.entries.values())

    def clear(self):
        with self.lock:
            self.generation += 1
            self.entries.clear()

    def close(self):
        with self.lock:
            self.clear()
            self.closed = True

    def prepare(self, hits, cancel, progress=lambda text: None, check=lambda: None):
        with self.lock:
            generation = self.generation
        def current():
            if cancel.is_set():
                raise BrainCancelled('Document task cancelled.')
            with self.lock:
                if self.closed or generation != self.generation:
                    raise BrainCancelled('Document memory was cleared.')
            check()
        current()
        chunks = passage_chunks(hits)
        if not chunks:
            raise BrainError('The document has no readable text to prepare.')
        digest = self.client.digest()
        signature = json.dumps([self.model, digest,
            [(hit.source, hit.location, hit.section, hit.kind, hit.excerpt) for hit in chunks]], ensure_ascii=False)
        key = hashlib.sha256(signature.encode('utf-8')).hexdigest()
        with self.lock:
            cached = self.entries.get(key)
            if cached is not None:
                self.entries.move_to_end(key)
        if cached is not None:
            try:
                normalized_vectors(cached.vectors, len(chunks))
            except BrainError:
                with self.lock:
                    self.entries.pop(key, None)
            else:
                current()
                return cached
        rows = []
        for start in range(0, len(chunks), 8):
            current()
            progress(f'Preparing passages {start + 1}–{min(start + 8, len(chunks))} of {len(chunks)} in temporary memory…')
            texts = ['title: ' + (hit.section or 'none')[:100] + ' | text: ' + hit.excerpt for hit in chunks[start:start+8]]
            rows.append(self.client.embed(texts, cancel))
        try:
            vectors = normalized_vectors(np.concatenate(rows), len(chunks))
        except ValueError:
            raise BrainError('Embedding dimensions changed while preparing the document. Retry.') from None
        current()
        if self.client.digest() != digest:
            raise BrainError('The embedding model changed while preparing the document. Retry.')
        size = vectors.nbytes + sum(len(hit.excerpt.encode('utf-8')) for hit in chunks)
        vectors.setflags(write=False)
        entry = PreparedDocument(key, chunks, vectors, size, generation)
        with self.lock:
            current()
            while self.entries and (len(self.entries) >= 20 or self.memory_bytes + size > MAX_CACHE_BYTES):
                self.entries.popitem(last=False)
            if size > MAX_CACHE_BYTES:
                raise BrainError('This document exceeds the temporary search-memory limit. Select a smaller section.')
            self.entries[key] = entry
        return entry

    def search(self, hits, query, cancel, progress=lambda text: None, check=lambda: None, limit=8, neighbors=False):
        if not query.strip() or len(query) > 500:
            raise BrainError('Enter a search question of 1–500 characters.')
        entry = self.prepare(hits, cancel, progress, check)
        chunks, vectors = entry.chunks, entry.vectors
        def current():
            if cancel.is_set():
                raise BrainCancelled('Document task cancelled.')
            with self.lock:
                if self.closed or entry.generation != self.generation:
                    raise BrainCancelled('Document memory was cleared.')
            check()
        current()
        progress('Finding the closest source passages locally…')
        question = self.client.embed(['task: search result | query: ' + query], cancel)[0]
        if vectors.shape[1] != len(question):
            raise BrainError('The embedding model changed. Clear its search index and retry.')
        scores = vectors @ question
        order = hybrid_order(query, chunks, scores)[:limit]
        if neighbors:
            expanded = set(order)
            for index in order:
                for adjacent in (index - 1, index + 1):
                    if 0 <= adjacent < len(chunks) and chunks[adjacent].source == chunks[index].source:
                        expanded.add(adjacent)
            order = sorted(expanded)
        current()
        # These are retrieval candidates, not a model's assertion of relevance.
        return [chunks[index] for index in order]


@dataclass
class PreparedDocument:
    key: str
    chunks: list
    vectors: np.ndarray
    size: int
    generation: int
