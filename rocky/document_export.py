"""Save derived document results through an atomic, user-selected export."""
import csv
import os
from pathlib import Path
import tempfile


def export_result(destination, answer, hits):
    destination = Path(destination)
    if destination.suffix.casefold() not in {'.docx', '.md', '.txt', '.csv'}:
        raise ValueError('Choose Word (.docx), Markdown (.md), text (.txt), or table CSV (.csv).')
    if any(hit.relative and Path(hit.source).resolve() == destination.resolve() for hit in hits):
        raise ValueError('Save the result under a new filename to preserve the source document.')
    if destination.exists() or destination.is_symlink():
        raise ValueError('Choose a new filename. Rocky exports do not overwrite existing files.')
    sources = '\n\n'.join(f'[S{index}] {hit.source} — {hit.label}\n{hit.excerpt}' for index, hit in enumerate(hits, 1))
    descriptor, temporary = tempfile.mkstemp(prefix='.rocky-export-', suffix=destination.suffix, dir=destination.parent)
    os.close(descriptor)
    try:
        if destination.suffix.casefold() == '.docx':
            from docx import Document
            document = Document()
            document.add_heading('Rocky document result', 0)
            for paragraph in answer.split('\n'):
                document.add_paragraph(paragraph)
            document.add_heading('Source passages', 1)
            for index, hit in enumerate(hits, 1):
                document.add_heading(f'[S{index}] {hit.label}', 2)
                document.add_paragraph(hit.source)
                document.add_paragraph(hit.excerpt)
            document.save(temporary)
        elif destination.suffix.casefold() == '.csv':
            rows = [hit for hit in hits if hit.kind == 'table']
            if not rows:
                raise ValueError('Select Tables first to export structured table rows.')
            with open(temporary, 'w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['Source', 'Location', 'Cells'])
                for hit in rows:
                    values = [hit.source, hit.label, *hit.excerpt.split('\t')]
                    # Opening exported text in a spreadsheet must not run formulas.
                    writer.writerow(["'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value for value in values])
        else:
            Path(temporary).write_text(answer + '\n\nSource passages\n\n' + sources, encoding='utf-8')
        # Publish without replacing a file created while the export was being
        # generated. Windows rename is exclusive; POSIX link has the same guard.
        if os.name == 'nt':
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
