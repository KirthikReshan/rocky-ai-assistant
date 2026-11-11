"""Session lifetime, evidence grounding and hybrid retrieval checks."""
from dataclasses import replace
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import tkinter as tk
import gc
import unittest
from unittest.mock import Mock

import numpy as np

from rocky.brain import BrainCancelled, BrainError
from rocky.document_index import DocumentIndex, normalized_vectors
from rocky.document_memory import DocumentText, remove_legacy_index, clear_document_transcript
from rocky.document_qa import GroundedQA, NOT_FOUND
from rocky.documents import Documents, DocumentHit
from rocky.storage import Store
from rocky.task_brain import TaskBrain
from rocky.voice import VoiceWorker


class Embeddings:
    def __init__(self):
        self.calls = []
        self.revision = 'stable'
    def digest(self):
        return self.revision
    def embed(self, texts, cancel):
        self.calls.append(texts)
        return normalized_vectors([[1., .1] for text in texts], len(texts))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'session.db')
        self.service = Documents(self.store, TaskBrain(self.store), queue.Queue())
        self.service.workspace.select(self.root)
        self.client = Embeddings()
        self.service.index = DocumentIndex(self.store, 'embeddinggemma', self.client)
        self.path = self.root / 'plan.txt'
        self.path.write_text('The budget is 500 dollars.', encoding='utf-8')
        self.brain = self.service.brain
        self.brain.document_evidence = Mock(return_value=[{'source':'S1','quote':'The budget is 500 dollars.'}])
        self.brain.document_answer = Mock(return_value={'found':True,'answer':'The budget is 500 dollars. [S1]'})
        self.brain.document_answer_supported = Mock(return_value=True)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_prepare_precedes_answers_and_is_reused_in_the_session(self):
        sequence = []
        original = self.client.embed
        self.client.embed = lambda *args: (sequence.append('embedding'), original(*args))[1]
        self.brain.document_evidence.side_effect = lambda *args: (sequence.append('evidence'), [{'source':'S1','quote':'The budget is 500 dollars.'}])[1]
        result = self.service.perform('Ask', 'plan.txt', 'What is the budget?')
        self.assertEqual(sequence[:2], ['embedding', 'evidence'])
        self.assertIn('500', result.answer)
        self.assertIn('Evidence:', result.answer)
        self.service.perform('Ask', 'plan.txt', 'What is the budget?')
        self.assertEqual(len(self.client.calls), 1)
        self.assertFalse(self.root.joinpath('session-document-index.sqlite3').exists())

    def test_missing_embedding_prevents_an_unprepared_answer(self):
        self.client.digest = Mock(side_effect=BrainError('Embedding model missing'))
        with self.assertRaises(BrainError):
            self.service.perform('Ask', 'plan.txt', 'Budget?')
        self.brain.document_evidence.assert_not_called()
        self.brain.document_answer.assert_not_called()

    def test_clear_removes_index_text_and_selection_but_preserves_originals_and_notes(self):
        note = self.store.add_note('Keep this note')
        self.service.perform('Prepare document', 'plan.txt')
        self.assertTrue(self.service.index.entries)
        self.assertTrue(self.service._cache)
        message = DocumentText('A document answer', self.service)
        self.assertTrue(message.current())
        self.service.clear_memory()
        self.assertFalse(message.current())
        self.assertFalse(self.service.index.entries)
        self.assertFalse(self.service._cache)
        self.assertIsNone(self.service.workspace.root)
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'The budget is 500 dollars.')
        self.assertEqual(self.store.notes()[0]['id'], note)

    def test_close_releases_memory_and_next_session_starts_empty(self):
        self.service.perform('Prepare document', 'plan.txt')
        self.service.close()
        self.assertEqual(self.service.index.memory_bytes, 0)
        another = DocumentIndex(self.store, 'embeddinggemma', self.client)
        self.assertFalse(another.entries)
        with self.assertRaises(BrainCancelled):
            self.service.perform('Ask', 'plan.txt', 'Budget?')

    def test_clear_during_embedding_does_not_allow_late_repopulation(self):
        entered, release = threading.Event(), threading.Event()
        def slow(texts, cancel):
            entered.set()
            release.wait(3)
            return normalized_vectors([[1, 0]] * len(texts), len(texts))
        self.client.embed = slow
        errors = []
        def work():
            try:
                self.service.perform('Prepare document', 'plan.txt')
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.service.clear_memory()
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertFalse(self.service.index.entries)
        self.assertFalse(self.service._cache)
        self.brain.document_answer.assert_not_called()

    def test_only_document_speech_is_removed_from_pending_announcements(self):
        worker = VoiceWorker(queue.Queue(), self.root)
        worker.announcements.put(DocumentText('Budget 500', self.service))
        worker.announcements.put('Reminder: drink water')
        worker.clear_document_messages()
        self.assertEqual(worker.announcements.get_nowait(), 'Reminder: drink water')
        self.assertTrue(worker.announcements.empty())

    def test_recognised_legacy_index_is_removed_without_touching_source_files(self):
        path = self.store.path.with_name('session-document-index.sqlite3')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE vectors (key TEXT PRIMARY KEY,created REAL,rows INTEGER,columns INTEGER,data BLOB)')
        db.close()
        self.assertTrue(remove_legacy_index(self.store))
        self.assertFalse(path.exists())
        self.assertTrue(self.path.is_file())
        self.assertTrue(self.store.path.is_file())

    def test_unrelated_database_at_legacy_filename_is_preserved(self):
        path = self.store.path.with_name('session-document-index.sqlite3')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE important (text TEXT)')
        db.close()
        with self.assertRaises(OSError):
            remove_legacy_index(self.store)
        self.assertTrue(path.is_file())
        path.unlink()


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.brain = Mock()
        self.hits = [DocumentHit('policy.pdf', 1, 'The refund period is 30 days. Except for damaged goods, which have 60 days.', 'policy.pdf', 'Page 1')]
        self.brain.document_evidence.return_value = [{'source':'S1','quote':'The refund period is 30 days.'}]
        self.brain.document_answer.return_value = {'found':True, 'answer':'The refund period is 30 days, or 60 days for damaged goods. [S1]'}
        self.brain.document_answer_supported.return_value = True
        self.qa = GroundedQA(self.brain)
    def ask(self):
        return self.qa.answer('What is the refund period?', self.hits, lambda:None, lambda text:None)

    def test_verified_answer_includes_qualifying_exception_and_clickable_evidence(self):
        answer, hits, status = self.ask()
        self.assertIn('60 days', answer)
        self.assertIn('Except for damaged goods', hits[0].excerpt)
        self.assertIn('[S1]', answer)
        self.brain.document_answer_supported.assert_called_once()

    def test_quoted_exception_keeps_the_previous_sentence_for_context(self):
        self.brain.document_evidence.return_value = [{'source':'S1','quote':'Except for damaged goods, which have 60 days.'}]
        self.assertIn('The refund period is 30 days.', self.ask()[1][0].excerpt)

    def test_identical_text_on_different_pages_keeps_both_locations(self):
        self.hits = [DocumentHit('policy.pdf', 1, 'A signature is required.', 'policy.pdf', 'Page 1'),
                     DocumentHit('policy.pdf', 2, 'A signature is required.', 'policy.pdf', 'Page 2')]
        self.brain.document_evidence.return_value = [{'source':'S1','quote':'A signature is required.'},
                                                    {'source':'S2','quote':'A signature is required.'}]
        self.brain.document_answer.return_value = {'found':False,'answer':''}
        self.assertEqual([hit.label for hit in self.ask()[1]], ['Page 1', 'Page 2'])

    def test_whole_document_answer_cannot_omit_a_verified_item_even_if_model_approves(self):
        hits = [DocumentHit('plan.pdf', 1, 'First delivery is Friday.', 'plan.pdf', 'Page 1'),
                DocumentHit('plan.pdf', 2, 'Second delivery is Sunday.', 'plan.pdf', 'Page 2')]
        self.brain.document_evidence.return_value = [{'source':'S1','quote':hits[0].excerpt},
                                                    {'source':'S2','quote':hits[1].excerpt}]
        self.brain.document_answer.return_value = {'found':True,'answer':'First delivery is Friday. [S1]'}
        answer, _, _ = self.qa.answer('List all delivery dates.', hits, lambda:None, lambda text:None)
        self.assertIn('Sunday', answer)
        self.assertIn('Friday', answer)
        self.assertTrue(answer.startswith('Relevant source passages'))

    def test_saturated_evidence_keeps_every_page_in_the_source_group(self):
        hits = [DocumentHit('plan.pdf', page, f'Delivery {page} is Friday.', 'plan.pdf', f'Page {page}')
                for page in range(1, 31)]
        self.brain.document_evidence.return_value = [
            {'source':f'S{page}','quote':hits[page-1].excerpt} for page in range(1, 25)]
        self.brain.document_answer.return_value = {'found':False, 'answer':''}
        answer, evidence, _ = self.qa.answer('List all delivery dates.', hits, lambda:None, lambda text:None)
        self.assertEqual(len(evidence), 30)
        self.assertIn('Delivery 30 is Friday.', answer)

    def test_source_batches_include_metadata_in_the_context_budget(self):
        import json
        from rocky.document_qa import source_groups
        hits = [DocumentHit('plan.pdf', page, 'Friday.', 'plan.pdf', f'Page {page}', section='A'*100)
                for page in range(1, 101)]
        groups = list(source_groups(hits, limit=1000))
        self.assertEqual(sum(map(len, groups)), 100)
        self.assertTrue(all(sum(len(json.dumps(row, ensure_ascii=False)) for row in group) <= 1000
                            for group in groups))

    def test_fabricated_quote_is_rejected_before_answer_generation(self):
        self.brain.document_evidence.return_value = [{'source':'S1','quote':'The refund period is 900 days.'}]
        answer, _, _ = self.ask()
        self.assertEqual(answer, NOT_FOUND)
        self.brain.document_answer.assert_not_called()

    def test_unknown_source_is_rejected(self):
        self.brain.document_evidence.return_value = [{'source':'S99','quote':'The refund period is 30 days.'}]
        self.assertEqual(self.ask()[0], NOT_FOUND)

    def test_invented_number_falls_back_to_original_source_evidence(self):
        self.brain.document_answer.return_value = {'found':True,'answer':'The refund period is 900 days. [S1]'}
        answer, _, _ = self.ask()
        self.assertNotIn('900', answer)
        self.assertIn('Relevant source passages', answer)
        self.brain.document_answer_supported.assert_not_called()

    def test_reversed_negation_cannot_pass_the_support_check(self):
        self.hits[0] = replace(self.hits[0], excerpt='Refunds are not allowed for opened items.')
        self.brain.document_evidence.return_value = [{'source':'S1','quote':'allowed for opened items'}]
        self.brain.document_answer.return_value = {'found':True,'answer':'Refunds are allowed for opened items. [S1]'}
        self.brain.document_answer_supported.return_value = False
        answer, hits, _ = self.ask()
        self.assertIn('not allowed', answer)
        self.assertEqual(hits[0].excerpt, 'Refunds are not allowed for opened items.')
        self.assertTrue(answer.startswith('Relevant source passages'))

    def test_model_cannot_mark_itself_supported_with_a_string_boolean(self):
        self.brain.document_answer_supported.return_value = 'true'
        self.assertTrue(self.ask()[0].startswith('Relevant source passages'))


class HybridTests(unittest.TestCase):
    def test_exact_identifier_is_retrieved_even_when_dense_ranking_is_poor(self):
        from rocky.document_retrieval import hybrid_order
        hits = [DocumentHit('a.pdf', 1, 'Ordinary unrelated text.'),
                DocumentHit('a.pdf', 2, 'Ticket ZX-42 has a deadline of Friday.')]
        self.assertEqual(hybrid_order('What is the deadline for ticket ZX-42?', hits, [1., 0.])[0], 1)

    def test_programming_language_names_remain_distinct(self):
        from rocky.document_retrieval import terms
        self.assertEqual(terms('C++ C# ZX-42'), ['c++', 'c#', 'zx-42'])

    def test_small_rows_are_packed_without_losing_text_or_pdf_page_locations(self):
        from rocky.document_index import passage_chunks
        rows = [DocumentHit('sheet.csv', row, f'Item {row}\t{row}', 'sheet.csv', f'Row {row}', kind='table')
                for row in range(1, 4001)]
        chunks = passage_chunks(rows)
        self.assertLess(len(chunks), 200)
        text = '\n'.join(hit.excerpt for hit in chunks)
        self.assertIn('Item 4000\t4000', text)
        pages = [DocumentHit('doc.pdf', page, 'Short page.', 'doc.pdf', f'Page {page}') for page in (1, 2)]
        self.assertEqual([hit.label for hit in passage_chunks(pages)], ['Page 1', 'Page 2'])


class SessionInterfaceTests(unittest.TestCase):
    def test_selecting_a_file_prepares_it_and_clear_removes_the_visible_data(self):
        from rocky.documents_page import DocumentsPage
        root = tk.Tk()
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                (base/'plan.txt').write_text('The budget is 500 dollars.', encoding='utf-8')
                store = Store(base/'test.db')
                events = queue.Queue()
                service = Documents(store, TaskBrain(store), events)
                service.index = DocumentIndex(store, 'embeddinggemma', Embeddings())
                page = DocumentsPage(root, service, Mock(announcements=queue.Queue()), events)
                page.set_folder(base, 'plan.txt')
                self.assertTrue(page.running)
                page.task_thread.join(4)
                self.assertFalse(page.task_thread.is_alive())
                while not events.empty():
                    kind, value = events.get_nowait()
                    if kind.startswith('Document'):
                        page.handle(kind, value)
                self.assertIn('Ready:', page.status.get())
                self.assertTrue(service.index.entries)
                self.assertEqual(page.operation.get(), 'Ask')
                page.clear_folder()
                self.assertFalse(page.hits)
                self.assertNotIn('500', page.source.get('1.0','end'))
                self.assertEqual(service.index.memory_bytes, 0)
                page.close()
        finally:
            root.destroy()
            gc.collect()

    def test_document_transcript_clear_preserves_other_chat_messages(self):
        root = tk.Tk()
        root.withdraw()
        try:
            text = tk.Text(root)
            text.insert('end', 'Hello\n', 'rocky')
            text.insert('end', 'Private PDF answer\n', ('rocky','document-memory'))
            text.insert('end', 'Reminder: stretch\n', 'reminder')
            text.insert('end', 'Second PDF answer\n', ('rocky','document-memory'))
            clear_document_transcript(text)
            self.assertEqual(text.get('1.0','end-1c'), 'Hello\nReminder: stretch\n')
        finally:
            root.destroy()
            gc.collect()


if __name__ == '__main__':
    unittest.main()
