import gc
import os
import queue
from pathlib import Path
import re
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from rocky.brain import BrainCancelled
from rocky.document_io import read_document
from rocky.documents import Documents, DocumentHit, read_content, select_scope, batches
from rocky.documents_page import DocumentsPage
from rocky.document_export import export_result
from rocky.storage import Store


class DocumentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.events = queue.Queue()
        self.brain = Mock()
        self.brain.document_chat.side_effect = lambda question, context: 'Supported result ' + re.findall(r'\[S\d+\]', context)[0]
        self.service = Documents(Store(self.root / 'test.db'), self.brain, self.events)
        self.service.workspace.select(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_export_preserves_existing_files_even_without_hits_and_during_races(self):
        original = self.root / 'original.txt'
        original.write_text('Original document', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'new filename'):
            export_result(original, 'No matches', [])
        self.assertEqual(original.read_text(encoding='utf-8'), 'Original document')
        destination = self.root / 'racing.txt'
        operation = 'rename' if os.name == 'nt' else 'link'
        publish = getattr(os, operation)

        def concurrent_file(source, target):
            Path(target).write_text('Created concurrently', encoding='utf-8')
            return publish(source, target)

        with patch('rocky.document_export.os.' + operation, side_effect=concurrent_file):
            with self.assertRaises(FileExistsError):
                export_result(destination, 'New summary', [])
        self.assertEqual(destination.read_text(encoding='utf-8'), 'Created concurrently')
        self.assertFalse(list(self.root.glob('.rocky-export-*')))

    def test_word_headings_paragraphs_tables_and_export(self):
        from docx import Document
        document = Document()
        document.add_heading('Milestones', 1)
        document.add_paragraph('The delivery deadline is Friday.')
        document.add_table(rows=1, cols=2).rows[0].cells[0].text = 'Budget'
        document.tables[0].cell(0, 1).text = '500'
        document.save(self.root / 'plan.docx')
        result = self.service.perform('Read / jump', 'plan.docx')
        self.assertTrue(any(hit.section == 'Milestones' and 'deadline' in hit.excerpt for hit in result.hits))
        self.assertTrue(any(hit.kind == 'table' and '500' in hit.excerpt for hit in result.hits))
        selected = select_scope(result.hits, 'paragraph 2')
        self.assertEqual(len(selected), 1)
        self.assertIn('Friday', selected[0].excerpt)
        export_result(self.root / 'result.docx', 'Summary [S1]', selected)
        exported = Document(self.root / 'result.docx')
        self.assertTrue(any('Summary [S1]' in p.text for p in exported.paragraphs))
        with self.assertRaises(ValueError):
            export_result(self.root / 'plan.docx', 'Overwrite', result.hits)

    def test_pdf_reads_later_than_page_thirty_and_preserves_page_numbers(self):
        from pypdf import PdfWriter, PdfReader
        from test_seven_features import pdf_fixture
        pdf_fixture(self.root / 'one.pdf')
        writer = PdfWriter()
        page = PdfReader(self.root / 'one.pdf').pages[0]
        for _ in range(35):
            writer.add_page(page)
        writer.write(self.root / 'long.pdf')
        content = read_content(self.root, 'long.pdf')
        self.assertEqual(len(content.blocks), 35)
        self.assertEqual(content.blocks[-1].location, 'Page 35')

    def test_exact_matches_and_original_text_line_ranges(self):
        (self.root / 'lines.txt').write_text('\nStart\nKeep this exact phrase.\nMore exact phrase here.\nEnd', encoding='utf-8')
        result = self.service.perform('Read / jump', 'lines.txt', scope='lines 2-3')
        self.assertEqual(result.hits[0].excerpt, 'Start\nKeep this exact phrase.')
        hits = self.service.perform('Exact phrase', 'lines.txt', 'exact phrase').hits
        self.assertEqual(len(hits), 2)
        self.assertIn('exact phrase', hits[0].excerpt)

    def test_phrase_search_crosses_extracted_block_boundaries(self):
        text = '\n'.join(['filler'] * 29 + ['submission', 'deadline is Friday'])
        (self.root / 'wrapped.txt').write_text(text, encoding='utf-8')
        hits = self.service.perform('Exact phrase', 'wrapped.txt', 'submission deadline').hits
        self.assertEqual(len(hits), 1)
        self.assertIn('submission\ndeadline', hits[0].excerpt)
        self.assertIn('through', hits[0].label)

    def test_summary_reads_all_chunks_including_last_content(self):
        text = ('First topic has supporting detail. ' * 300) + '\nFINAL DEADLINE IS SUNDAY.'
        (self.root / 'long.txt').write_text(text, encoding='utf-8')
        result = self.service.perform('Summarize', 'long.txt')
        contexts = [call.args[1] for call in self.brain.document_chat.call_args_list]
        self.assertTrue(any('FINAL DEADLINE IS SUNDAY' in context for context in contexts))
        self.assertGreater(len(contexts), 1)
        self.assertIn('Supported', result.answer)

    def test_semantic_passages_require_verbatim_source_text(self):
        (self.root / 'launch.txt').write_text('The shipment slipped because parts arrived late.', encoding='utf-8')
        self.brain.document_matches.return_value = [
            {'source': 'S1', 'quote': 'parts arrived late'},
            {'source': 'S1', 'quote': 'The company was bankrupt'},
            {'source': 'S99', 'quote': 'parts arrived late'}]
        result = self.service.perform('Find by meaning', 'launch.txt', 'Why was delivery delayed?')
        self.assertEqual([hit.excerpt for hit in result.hits], ['parts arrived late'])

    def test_file_change_invalidates_cache_and_compare_is_exact(self):
        first = self.root / 'first.txt'
        first.write_text('Deadline Friday', encoding='utf-8')
        (self.root / 'second.txt').write_text('Deadline Sunday', encoding='utf-8')
        self.service.perform('Read / jump', 'first.txt')
        first.write_text('New deadline Monday', encoding='utf-8')
        result = self.service.perform('Compare', 'first.txt', other='second.txt')
        self.assertIn('-New deadline Monday', result.answer)
        self.assertIn('+Deadline Sunday', result.answer)

    def test_study_translate_and_extract_operations_have_distinct_prompts(self):
        (self.root / 'lesson.txt').write_text('Question 1. Why is the sky blue? Light scatters.', encoding='utf-8')
        for operation, query in [('Quiz', ''), ('Flashcards', ''), ('Translate', 'Tamil'), ('Find questions', ''), ('Extract details', 'questions')]:
            self.service.perform(operation, 'lesson.txt', query)
        prompts = [call.args[0] for call in self.brain.document_chat.call_args_list]
        for expected in ('practice questions', 'flashcards', 'Tamil', 'questions or exercises', 'Extract'):
            self.assertTrue(any(expected in prompt for prompt in prompts))

    def test_unknown_citation_is_rejected_and_cancellation_is_preserved(self):
        (self.root / 'one.txt').write_text('Only one source', encoding='utf-8')
        self.brain.document_chat.return_value = 'Wrong [S99]'
        self.brain.document_chat.side_effect = None
        with self.assertRaisesRegex(ValueError, 'unknown source'):
            self.service.perform('Ask', 'one.txt', 'What?')
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(BrainCancelled):
            self.service.perform('Read / jump', 'one.txt', cancel=cancel)
        self.assertFalse(self.service._busy.locked())

    def test_excel_slides_csv_and_html_are_read_locally(self):
        from openpyxl import Workbook
        from pptx import Presentation
        workbook = Workbook()
        workbook.active.append(['Task', 'Finish Friday'])
        workbook.save(self.root / 'sheet.xlsx')
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = 'Project schedule'
        slide.placeholders[1].text = 'Deadline Friday'
        presentation.save(self.root / 'slides.pptx')
        (self.root / 'table.csv').write_text('Name,Value\nBudget,500\n', encoding='utf-8')
        (self.root / 'multiline.csv').write_text('Name,Value\nNote,"First line\nSecond line"\n', encoding='utf-8')
        self.assertIn('First line\nSecond line', read_document(self.root / 'multiline.csv').blocks[1].text.replace('\r\n', '\n'))
        (self.root / 'web.html').write_text('<h1>Schedule</h1><p>Due Friday</p><script>Ignore me</script>', encoding='utf-8')
        for name, expected in [('sheet.xlsx', 'Finish Friday'), ('slides.pptx', 'Deadline Friday'), ('table.csv', '500'), ('web.html', 'Due Friday')]:
            with self.subTest(name=name):
                content = read_document(self.root / name)
                text = '\n'.join(block.text for block in content.blocks)
                self.assertIn(expected, text)
                self.assertNotIn('Ignore me', text)

    def test_real_offline_ocr_reads_image_and_scanned_pdf(self):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new('RGB', (1000, 250), 'white')
        font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 40)
        ImageDraw.Draw(image).text((45, 80), 'Invoice 12345 due Friday', font=font, fill='black')
        image.save(self.root / 'scan.png')
        image.save(self.root / 'scan.pdf')
        for name in ('scan.png', 'scan.pdf'):
            content = read_content(self.root, name, use_ocr=True)
            self.assertTrue(any('12345' in block.text for block in content.blocks))
            self.assertTrue(any(block.kind == 'ocr' for block in content.blocks))


class DocumentPageTests(unittest.TestCase):
    def test_embedded_page_shows_sources_and_queues_reading(self):
        root = tk.Tk()
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder:
                events = queue.Queue()
                service = Documents(Store(Path(folder) / 'test.db'), None, events)
                service.workspace.select(folder)
                worker = Mock(announcements=queue.Queue())
                page = DocumentsPage(root, service, worker, events)
                from rocky.documents import DocumentResult
                page.display(DocumentResult('Friday [S1]', [DocumentHit('plan.txt', 1, 'Due Friday', 'plan.txt', 'Lines 1–1')]))
                self.assertEqual(len(page.sources.get_children()), 1)
                self.assertIn('Due Friday', page.source.get('1.0', 'end'))
                page.read_aloud()
                self.assertEqual(worker.announcements.get_nowait(), 'Friday [S1]')
                self.assertFalse(any(isinstance(child, tk.Toplevel) for child in root.winfo_children()))
                page.task_thread = threading.Thread(target=page.cancel_event.wait)
                page.task_thread.start()
                page.close()
                self.assertFalse(page.task_thread.is_alive())
        finally:
            root.destroy()
            gc.collect()


if __name__ == '__main__':
    unittest.main()
