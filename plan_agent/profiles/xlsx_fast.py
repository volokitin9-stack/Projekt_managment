"""Fast, read-only access to individual worksheets of large workbooks.

openpyxl's full load of a 30-sheet planning workbook takes > 1 minute.  This
reader uses openpyxl's read-only mode for cell values (cached formula
results) and reads row visibility, merged ranges and the AutoFilter straight
from the sheet XML.  The source file is only ever opened for reading.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from openpyxl.utils import get_column_letter


@dataclass
class SheetData:
    name: str
    rows: dict[int, dict[str, Any]]          # row number -> {column letter: value}
    hidden_rows: set[int] = field(default_factory=set)
    merged: list[str] = field(default_factory=list)
    autofilter: dict = field(default_factory=dict)  # {"ref": "A1:BD288", "filters": {"B": ["<value>"]}}
    max_col: int = 0


def _sheet_xml_paths(zf: zipfile.ZipFile) -> dict[str, str]:
    wb = zf.read("xl/workbook.xml").decode("utf-8")
    rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    rel_target = {}
    for m in re.finditer(r"<Relationship\b([^>]*)/?>", rels):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        rel_target[attrs.get("Id")] = attrs.get("Target", "")
    out = {}
    for m in re.finditer(r"<sheet\b([^>]*)/?>", wb):
        attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', m.group(1)))
        target = rel_target.get(attrs.get("r:id"), "")
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        name = (attrs.get("name", "").replace("&amp;", "&").replace("&quot;", '"')
                .replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'"))
        out[name] = target
    return out


def _xml_meta(path: str, sheet: str) -> tuple[set[int], list[str], dict]:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read(_sheet_xml_paths(zf)[sheet]).decode("utf-8")
    hidden: set[int] = set()
    for m in re.finditer(r"<row\b([^>]*)>", xml):      # attribute order varies
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if attrs.get("hidden") in ("1", "true") and attrs.get("r"):
            hidden.add(int(attrs["r"]))
    merged = re.findall(r'<mergeCell ref="([^"]+)"', xml)
    af = {}
    m = re.search(r"<autoFilter\b([^>]*?)(/>|>(.*?)</autoFilter>)", xml, re.S)
    if m:
        ref = re.search(r'ref="([^"]+)"', m.group(1))
        af["ref"] = ref.group(1) if ref else None
        filters = {}
        for fc in re.finditer(r'<filterColumn colId="(\d+)">(.*?)</filterColumn>', m.group(3) or "", re.S):
            start_col = 1
            if af["ref"]:
                letters = re.match(r"([A-Z]+)", af["ref"]).group(1)
                start_col = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(letters)))
            col = get_column_letter(start_col + int(fc.group(1)))
            filters[col] = re.findall(r'<filter val="([^"]*)"', fc.group(2))
        af["filters"] = filters
    return hidden, merged, af


def list_sheets(path: str) -> list[str]:
    import openpyxl
    with open(path, "rb") as fh:
        wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()


def peek_rows(path: str, max_rows: int) -> dict[str, list[list[Any]]]:
    """First ``max_rows`` rows (values) of every worksheet."""
    import openpyxl
    out = {}
    with open(path, "rb") as fh:
        wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                rows = []
                for r in ws.iter_rows(min_row=1, max_row=max_rows, values_only=True):
                    rows.append(list(r))
                out[ws.title] = rows
        finally:
            wb.close()
    return out


def read_sheet(path: str, sheet: str) -> SheetData:
    import openpyxl
    rows: dict[int, dict[str, Any]] = {}
    max_col = 0
    with open(path, "rb") as fh:
        wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
        try:
            ws = wb[sheet]
            for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                vals = {}
                for c_idx, v in enumerate(row, start=1):
                    if v is None or (isinstance(v, str) and v.strip() == ""):
                        continue
                    vals[get_column_letter(c_idx)] = v
                    max_col = max(max_col, c_idx)
                if vals:
                    rows[r_idx] = vals
        finally:
            wb.close()
    hidden, merged, af = _xml_meta(path, sheet)
    return SheetData(sheet, rows, hidden, merged, af, max_col)
