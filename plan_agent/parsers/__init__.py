"""File parsers.  :func:`parse_file` is the single entry point.

Format detection uses file *content* (magic bytes), never the file name, so a
file delivered as ``upload.bin`` or ``plan (3).XLSX`` is handled the same way.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from ..models import ParsedFile


class UnsupportedFormatError(Exception):
    pass


class CorruptFileError(Exception):
    pass


class EmptyFileError(Exception):
    pass


def detect_format(path: str | Path) -> str:
    p = Path(path)
    size = p.stat().st_size
    if size == 0:
        raise EmptyFileError(f"{p.name} is empty (0 bytes)")
    with open(p, "rb") as fh:
        head = fh.read(8)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls"          # OLE2 compound document (legacy Excel)
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(p) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile as exc:
            raise CorruptFileError(f"{p.name}: damaged ZIP container ({exc})") from exc
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("word/") for n in names):
            return "docx"
        raise UnsupportedFormatError(f"{p.name}: ZIP archive is neither .xlsx nor .docx")
    # Text?  Accept as CSV only if it decodes and looks delimited.
    with open(p, "rb") as fh:
        sample = fh.read(65536)
    if b"\x00" in sample:
        raise UnsupportedFormatError(f"{p.name}: binary file of unknown type")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = sample.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UnsupportedFormatError(f"{p.name}: text encoding not recognised")
    if any(d in text for d in (",", ";", "\t")):
        return "csv"
    raise UnsupportedFormatError(f"{p.name}: plain text without a table structure")


def parse_file(path: str | Path) -> ParsedFile:
    fmt = detect_format(path)
    if fmt == "xlsx":
        from .excel import parse_xlsx as fn
    elif fmt == "xls":
        from .excel import parse_xls as fn
    elif fmt == "csv":
        from .csv_parser import parse_csv as fn
    elif fmt == "pdf":
        from .pdf_parser import parse_pdf as fn
    elif fmt == "docx":
        from .docx_parser import parse_docx as fn
    else:  # pragma: no cover - detect_format raises first
        raise UnsupportedFormatError(fmt)
    try:
        parsed = fn(str(path))
    except (UnsupportedFormatError, EmptyFileError):
        raise
    except Exception as exc:  # library-specific corruption errors
        raise CorruptFileError(f"could not read {Path(path).name} as {fmt}: {exc}") from exc
    if not any(t.rows for t in parsed.tables):
        raise EmptyFileError(f"{Path(path).name}: no tabular data found")
    return parsed


def trim_table(rows, row_numbers, *extra_lists):
    """Drop fully-empty trailing columns and fully-empty rows."""
    keep = [i for i, r in enumerate(rows) if any(not c.is_empty() for c in r)]
    rows = [rows[i] for i in keep]
    row_numbers = [row_numbers[i] for i in keep]
    extras = [[lst[i] for i in keep] if lst else lst for lst in extra_lists]
    width = 0
    for r in rows:
        for j in range(len(r) - 1, -1, -1):
            if not r[j].is_empty():
                width = max(width, j + 1)
                break
    rows = [r[:width] for r in rows]
    return (rows, row_numbers, *extras)
