"""PDF parser.  Extracts ruled tables; falls back to whitespace-aligned text.

Tables that continue across pages with an identical header are concatenated
so a multi-page plan becomes one logical table.
"""
from __future__ import annotations

import re

from ..models import Cell, ParsedFile, RawTable
from . import trim_table


def parse_pdf(path: str) -> ParsedFile:
    import pdfplumber

    tables: list[RawTable] = []
    warnings: list[str] = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            found = page.extract_tables() or []
            if not found:
                text = page.extract_text() or ""
                lines = [ln for ln in text.splitlines() if ln.strip()]
                split = [re.split(r"\s{2,}|\t", ln.strip()) for ln in lines]
                if sum(1 for s in split if len(s) >= 3) >= 2:
                    found = [split]
                    warnings.append(f"page {pno}: no ruled table, used text alignment (lower reliability)")
            for tno, data in enumerate(found, start=1):
                rows = [[Cell(v.replace("\n", " ") if isinstance(v, str) else v) for v in r] for r in data]
                numbers = list(range(1, len(rows) + 1))
                rows, numbers = trim_table(rows, numbers)
                if not rows:
                    continue
                prev = tables[-1] if tables else None
                if prev and [c.text() for c in prev.rows[0]] == [c.text() for c in rows[0]]:
                    # continuation of the previous table: skip repeated header
                    offset = prev.row_numbers[-1]
                    prev.rows.extend(rows[1:])
                    prev.row_numbers.extend(offset + n for n in numbers[1:])
                    prev.outline_levels.extend([0] * (len(rows) - 1))
                    prev.notes.append(f"continued on page {pno}")
                    continue
                tables.append(RawTable(f"page {pno} table {tno}", rows, numbers, [0] * len(rows)))
    return ParsedFile(path, "pdf", tables, [t.source for t in tables], warnings)
