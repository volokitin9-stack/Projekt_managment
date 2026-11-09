"""Word parser.  Every table becomes a RawTable; merged cells are detected by
python-docx returning the same underlying ``<w:tc>`` element several times.
The nearest preceding heading is used as the table's section name."""
from __future__ import annotations

from ..models import Cell, ParsedFile, RawTable
from . import trim_table


def parse_docx(path: str) -> ParsedFile:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(path)
    tables = []
    heading = None
    tno = 0
    for block in document.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(block, document)
            if p.style is not None and p.style.name.lower().startswith(("heading", "заголовок")) and p.text.strip():
                heading = p.text.strip()
        elif tag == "tbl":
            tno += 1
            tbl = Table(block, document)
            rows, numbers = [], []
            for rno, row in enumerate(tbl.rows, start=1):
                seen = {}
                cells = []
                for cno, cell in enumerate(row.cells):
                    key = id(cell._tc)
                    merged = key in seen
                    seen.setdefault(key, cno)
                    cells.append(Cell(cell.text.strip() or None, merged=merged))
                rows.append(cells)
                numbers.append(rno)
            rows, numbers = trim_table(rows, numbers)
            name = f"{heading} (table {tno})" if heading else f"table {tno}"
            tables.append(RawTable(name, rows, numbers, [0] * len(rows)))
    return ParsedFile(path, "docx", tables, [t.source for t in tables])
