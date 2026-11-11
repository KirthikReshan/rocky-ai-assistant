"""Bounded, in-memory source index. Every retrieved excerpt is re-read safely."""

import ast
from collections import Counter
from pathlib import PurePosixPath
import re
import time

STOP = {'the', 'and', 'this', 'that', 'with', 'from', 'what', 'which', 'how', 'why', 'for',
        'my', 'code', 'project', 'file', 'explain', 'please', 'does', 'is', 'in', 'a', 'to', 'of'}


def tokens(text):
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    return {word for word in re.findall(r'[a-z0-9]+', text.casefold()) if len(word) > 1 and word not in STOP}


class ProjectIndex:
    def __init__(self):
        self.clear()

    def clear(self):
        self.signature = None
        self.documents = {}
        self.links = {}
        self.limited = False

    def refresh(self, project, files, limited=False, force=False):
        metadata = []
        for name in files:
            try:
                info = project.safe_path(name).stat()
                metadata.append((name, info.st_mtime_ns, info.st_size))
            except (ValueError, OSError):
                limited = True
        signature = (str(project.root), tuple(metadata))
        if signature == self.signature and not force:
            return
        documents, imports = {}, {}
        budget = 2_000_000
        deadline = time.monotonic() + 3
        for name, _, _ in metadata:
            if budget <= 0 or time.monotonic() > deadline:
                limited = True
                break
            try:
                _, source = project.source(name)
                budget -= len(source.encode('utf-8'))
                if budget < 0:
                    limited = True
                    break
                symbols, dependencies = [], []
                if name.endswith('.py'):
                    try:
                        tree = ast.parse(source)
                        symbols = [node.name for node in ast.walk(tree)
                                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                        for node in ast.walk(tree):
                            if isinstance(node, ast.Import):
                                dependencies.extend(alias.name.replace('.', '/') for alias in node.names)
                            elif isinstance(node, ast.ImportFrom):
                                parent = list(PurePosixPath(name).parent.parts)
                                if node.level:
                                    parent = parent[:max(0, len(parent) - node.level + 1)]
                                else:
                                    parent = []
                                module = parent + (node.module.split('.') if node.module else [])
                                base = '/'.join(module)
                                if base:
                                    dependencies.append(base)
                                dependencies.extend('/'.join(module + [alias.name]) for alias in node.names)
                    except (SyntaxError, ValueError, RecursionError):
                        pass
                separated = re.sub(r'([a-z])([A-Z])', r'\1 \2', source)
                counts = Counter(re.findall(r'[a-z0-9]+', separated.casefold()))
                documents[name] = {'terms': counts, 'symbols': symbols, 'path_terms': tokens(name)}
                imports[name] = dependencies
            except (OSError, ValueError, UnicodeError):
                limited = True
        links = {name: set() for name in documents}
        for name, dependencies in imports.items():
            for dependency in dependencies:
                for candidate in (dependency + '.py', dependency + '/__init__.py'):
                    if candidate in documents and candidate != name:
                        links[name].add(candidate)
                        links[candidate].add(name)
        self.signature, self.documents, self.links = signature, documents, links
        self.limited = limited

    def description(self):
        return (f'Local project index: {len(self.documents)} files, '
                f'{sum(len(doc["symbols"]) for doc in self.documents.values())} Python symbols. '
                f'Index limited: {self.limited}. Excerpts are bounded and checked against current files.')

    def related(self, name):
        return sorted(self.links.get(name.replace('\\', '/'), set()))[:3]

    def select(self, query, preferred=()):
        query_terms = tokens(query)
        scores = {}
        for name, doc in self.documents.items():
            score = sum(min(3, doc['terms'].get(term, 0)) for term in query_terms)
            score += 8 * len(query_terms & doc['path_terms'])
            score += 4 * len(query_terms & tokens(' '.join(doc['symbols'])))
            if score:
                scores[name] = score
        selected = list(dict.fromkeys(name for name in preferred if name in self.documents))
        selected += [name for name in sorted(scores, key=lambda name: (-scores[name], name)) if name not in selected][:4]
        for name in list(selected[:2]):
            selected.extend(candidate for candidate in self.related(name) if candidate not in selected)
        return selected[:5]

    def excerpts(self, project, query, names, budget=8000, focus_lines=()):
        blocks = []
        query_terms = tokens(query)
        for name in names:
            if budget < 100:
                break
            try:
                path, source = project.source(name)
            except (OSError, ValueError, UnicodeError):
                continue
            lines = source.splitlines()
            # Traceback locations are stronger evidence than generic words in
            # the exception text, which often match unrelated earlier lines.
            hits = [line - 1 for line in focus_lines
                    if type(line) is int and 1 <= line <= len(lines)][:8]
            if not hits:
                hits = [i for i, line in enumerate(lines) if tokens(line) & query_terms][:8]
            keep = set()
            for hit in hits or [0]:
                keep.update(range(max(0, hit - 5), min(len(lines), hit + 8)))
            content = '\n'.join(f'{i + 1}: {lines[i]}' for i in sorted(keep))
            block = f'FILE {path.relative_to(project.root).as_posix()}\n{content}'
            if len(block) > budget:
                block = block[:max(0, budget - 30)] + '\n[EXCERPT LIMITED]'
            blocks.append(block)
            budget -= len(block)
        return '\n\n'.join(blocks)
