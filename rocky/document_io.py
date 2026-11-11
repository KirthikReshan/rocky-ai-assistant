"""Offline document decoding used only in an isolated, time-bounded worker."""
from dataclasses import asdict, dataclass
import csv
import io
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import zipfile

SUFFIXES = {'.pdf', '.docx', '.txt', '.md', '.csv', '.xlsx', '.pptx', '.html', '.htm', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp'}
MAX_FILE_BYTES = 40_000_000
MAX_TEXT = 600_000
MAX_PAGES = 500
MAX_BLOCKS = 10_000


@dataclass(frozen=True)
class Block:
    number: int
    location: str
    text: str
    section: str = ''
    kind: str = 'text'


@dataclass
class Content:
    blocks: list
    warnings: list

    def to_dict(self):
        return {'blocks': [asdict(block) for block in self.blocks], 'warnings': self.warnings}


class Collector:
    def __init__(self):
        self.blocks = []
        self.length = 0
        self.warnings = []

    def add(self, number, location, text, section='', kind='text'):
        text = text.replace('\x00', '')
        text.encode('utf-8')
        if not text.strip():
            return
        self.length += len(text)
        if self.length > MAX_TEXT or len(self.blocks) >= MAX_BLOCKS:
            raise ValueError('This document exceeds the 600,000-character or 10,000-section reading limit. Split it into smaller files.')
        self.blocks.append(Block(number, location, text, section, kind))

    def finish(self):
        return Content(self.blocks, self.warnings)


def check_archive(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 20_000 or sum(item.file_size for item in entries) > 150_000_000:
            raise ValueError('The expanded Office document is too large.')


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden += 1
        elif tag in {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4'} and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4'} and not self.hidden:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def ocr_text(image, engine):
    import numpy as np
    from PIL import ImageOps
    image = ImageOps.exif_transpose(image).convert('RGB')
    image.thumbnail((2400, 2400))
    rows, _ = engine(np.asarray(image))
    return '\n'.join(row[1] for row in (rows or []) if row[2] >= .45)


def read_document(path, use_ocr=False):
    path = Path(path)
    output = Collector()
    suffix = path.suffix.casefold()
    engine = None

    def recognize(image):
        nonlocal engine
        if engine is None:
            from rapidocr_onnxruntime import RapidOCR
            engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        return ocr_text(image, engine)

    if suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise ValueError('Select an unencrypted PDF copy.')
        if len(reader.pages) > MAX_PAGES:
            raise ValueError(f'This PDF exceeds {MAX_PAGES} pages. Split it into smaller files.')
        rendered = None
        try:
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ''
                kind = 'text'
                if use_ocr and len(text.strip()) < 30:
                    import pypdfium2 as pdfium
                    if rendered is None:
                        rendered = pdfium.PdfDocument(str(path))
                    rendered_page = rendered[number - 1]
                    try:
                        width, height = rendered_page.get_size()
                        scale = min(2.0, 2400 / max(width, height))
                        bitmap = rendered_page.render(scale=scale)
                        try:
                            text = recognize(bitmap.to_pil())
                        finally:
                            bitmap.close()
                    finally:
                        rendered_page.close()
                    kind = 'ocr'
                if not text.strip():
                    output.warnings.append(f'Page {number} has no readable text. Try OCR if it is a scan.')
                output.add(number, f'Page {number}', text, kind=kind)
        finally:
            if rendered is not None:
                rendered.close()
        output.warnings.append('PDF lines follow extracted reading order. Tables and multi-column layouts may need visual checking.')
    elif suffix == '.docx':
        from docx import Document
        from docx.table import Table
        check_archive(path)
        document = Document(path)
        section = ''
        paragraph = table = 0
        for item in document.iter_inner_content():
            if isinstance(item, Table):
                table += 1
                for row_number, row in enumerate(item.rows, 1):
                    output.add(paragraph or 1, f'Table {table}, row {row_number}', '\t'.join(cell.text for cell in row.cells), section, 'table')
            else:
                paragraph += 1
                if item.style and item.style.name.startswith('Heading'):
                    section = item.text
                output.add(paragraph, f'Paragraph {paragraph}', item.text, section, 'paragraph')
        output.warnings.append('Word references use body paragraphs and tables. Text boxes, tracked deletions, headers and footers are not included.')
    elif suffix == '.xlsx':
        from openpyxl import load_workbook
        check_archive(path)
        workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            for sheet in workbook:
                if sheet.max_row > 10_000 or sheet.max_column > 256:
                    raise ValueError('Each worksheet must fit within 10,000 rows and 256 columns. Export the relevant range first.')
                for row_number, row in enumerate(sheet.iter_rows(), 1):
                    values = [f'{cell.column_letter}={cell.value}' for cell in row if cell.value is not None]
                    if values:
                        output.add(row_number, f'Sheet {sheet.title}, row {row_number}', '\t'.join(values), sheet.title, 'table')
        finally:
            workbook.close()
        output.warnings.append('Excel formulas are shown as stored formulas. Rocky does not recalculate them.')
    elif suffix == '.pptx':
        from pptx import Presentation
        check_archive(path)
        presentation = Presentation(path)
        for number, slide in enumerate(presentation.slides, 1):
            title = slide.shapes.title.text if slide.shapes.title else ''
            for shape in slide.shapes:
                if shape.has_text_frame:
                    output.add(number, f'Slide {number}', shape.text, title)
                if shape.has_table:
                    for row_number, row in enumerate(shape.table.rows, 1):
                        output.add(number, f'Slide {number}, table row {row_number}', '\t'.join(cell.text for cell in row.cells), title, 'table')
            if slide.has_notes_slide:
                output.add(number, f'Slide {number}, notes', slide.notes_slide.notes_text_frame.text, title)
        output.warnings.append('Slide text, tables and notes are included. Charts and embedded images need separate interpretation.')
    elif suffix in {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}:
        if not use_ocr:
            raise ValueError('Enable OCR to read text from images.')
        from PIL import Image
        with Image.open(path) as image:
            if image.width * image.height > 30_000_000:
                raise ValueError('Image exceeds 30 megapixels. Resize it first.')
            frames = getattr(image, 'n_frames', 1)
            if frames > MAX_PAGES:
                raise ValueError('Too many image frames.')
            for number in range(frames):
                image.seek(number)
                if image.width * image.height > 30_000_000:
                    raise ValueError('Image frame exceeds 30 megapixels. Resize it first.')
                output.add(number + 1, f'Image {number + 1}', recognize(image), kind='ocr')
    else:
        with path.open('rb') as stream:
            data = stream.read(2_000_001)
        if len(data) > 2_000_000 or b'\x00' in data:
            raise ValueError('Choose a UTF-8 text file under 2 MB.')
        text = data.decode('utf-8-sig')
        if suffix == '.csv':
            for number, row in enumerate(csv.reader(io.StringIO(text, newline='')), 1):
                output.add(number, f'Row {number}', '\t'.join(row), kind='table')
        else:
            if suffix in {'.html', '.htm'}:
                parser = HTMLText()
                parser.feed(text)
                text = ''.join(parser.parts)
                output.warnings.append('HTML references use extracted text lines, without scripts or styling.')
            section = ''
            lines = text.splitlines()
            for start in range(0, len(lines), 30):
                chunk = lines[start:start + 30]
                headings = [line.lstrip('# ').strip() for line in chunk if re.match(r'^#{1,6}\s', line)]
                if headings:
                    section = headings[0]
                output.add(start + 1, f'Lines {start + 1}–{start + len(chunk)}', '\n'.join(chunk), section)
    if any(block.kind == 'ocr' for block in output.blocks):
        output.warnings.append('OCR text can contain recognition errors. Check critical names, numbers and dates against the image.')
    return output.finish()
