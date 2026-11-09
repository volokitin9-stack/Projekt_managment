from __future__ import annotations

import csv
import io

from ..models import Cell, ParsedFile, RawTable
from . import trim_table


def parse_csv(path: str) -> ParsedFile:
    raw = open(path, "rb").read()
    text, encoding = None, None
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text, encoding = raw.decode(enc), enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("unknown text encoding")
    try:
        dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if text.count(";") > text.count(",") else ","
    reader = csv.reader(io.StringIO(text), dialect)
    rows, numbers = [], []
    for i, rec in enumerate(reader, start=1):
        rows.append([Cell(v if v != "" else None) for v in rec])
        numbers.append(i)
    rows, numbers = trim_table(rows, numbers)
    table = RawTable("csv", rows, numbers, [0] * len(rows),
                     notes=[f"encoding={encoding}, delimiter={dialect.delimiter!r}"])
    return ParsedFile(path, "csv", [table], ["csv"])
