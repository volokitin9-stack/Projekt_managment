"""Excel parsers (.xlsx via openpyxl, legacy .xls via xlrd).

Merged cells are expanded: every cell of a merged range receives the value of
the top-left cell and is marked ``merged=True`` so later stages know the value
was propagated (e.g. a manufacturer name merged vertically over 6 rows).
Row outline levels (Excel "Group rows") and hidden rows are preserved.
"""
from __future__ import annotations

from datetime import datetime

from ..models import Cell, ParsedFile, RawTable
from . import trim_table


def parse_xlsx(path: str) -> ParsedFile:
    import openpyxl

    # data_only=True -> cached results of formulas instead of formula text.
    # A file handle (not a path) so openpyxl does not insist on an .xlsx
    # extension - inbox files are stored without one.
    with open(path, "rb") as fh:
        wb = openpyxl.load_workbook(fh, data_only=True)
    tables: list[RawTable] = []
    warnings: list[str] = []
    for ws in wb.worksheets:
        if ws.max_row == 0 or ws.max_column == 0:
            continue
        merged_value: dict[tuple[int, int], tuple[int, int]] = {}
        for rng in ws.merged_cells.ranges:
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    if (r, c) != (rng.min_row, rng.min_col):
                        merged_value[(r, c)] = (rng.min_row, rng.min_col)

        rows, row_numbers, levels, hidden_flags = [], [], [], []
        for r in range(1, ws.max_row + 1):
            row: list[Cell] = []
            for c in range(1, ws.max_column + 1):
                origin = merged_value.get((r, c))
                src = ws.cell(origin[0], origin[1]) if origin else ws.cell(r, c)
                fill = None
                try:
                    if src.fill is not None and src.fill.fill_type == "solid":
                        fill = str(src.fill.fgColor.rgb)
                except Exception:
                    fill = None
                row.append(Cell(
                    value=src.value,
                    merged=origin is not None,
                    merge_origin=origin,
                    bold=bool(src.font and src.font.bold),
                    fill=fill,
                    comment=src.comment.text.strip() if src.comment else None,
                ))
            dim = ws.row_dimensions.get(r)
            rows.append(row)
            row_numbers.append(r)
            levels.append(int(dim.outline_level or 0) if dim else 0)
            hidden_flags.append(bool(dim.hidden) if dim else False)

        rows, row_numbers, levels, hidden_flags = trim_table(rows, row_numbers, levels, hidden_flags)
        notes = []
        if ws.sheet_state != "visible":
            notes.append(f"sheet is {ws.sheet_state}")
        t = RawTable(
            source=ws.title, rows=rows, row_numbers=row_numbers,
            outline_levels=levels,
            hidden_rows={row_numbers[i] for i, h in enumerate(hidden_flags) if h},
            notes=notes,
        )
        tables.append(t)
    return ParsedFile(path, "xlsx", tables, wb.sheetnames, warnings)


def parse_xls(path: str) -> ParsedFile:
    import xlrd

    book = xlrd.open_workbook(path, formatting_info=True)
    tables = []
    for sh in book.sheets():
        merged_value = {}
        for rlo, rhi, clo, chi in sh.merged_cells:
            for r in range(rlo, rhi):
                for c in range(clo, chi):
                    if (r, c) != (rlo, clo):
                        merged_value[(r, c)] = (rlo, clo)
        rows, row_numbers, levels = [], [], []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                origin = merged_value.get((r, c))
                cr, cc = origin if origin else (r, c)
                cell = sh.cell(cr, cc)
                value = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    value = datetime(*xlrd.xldate_as_tuple(value, book.datemode))
                elif cell.ctype == xlrd.XL_CELL_EMPTY:
                    value = None
                row.append(Cell(value=value, merged=origin is not None,
                                merge_origin=(cr + 1, cc + 1) if origin else None))
            rows.append(row)
            row_numbers.append(r + 1)
            info = sh.rowinfo_map.get(r)
            levels.append(info.outline_level if info else 0)
        rows, row_numbers, levels = trim_table(rows, row_numbers, levels)
        tables.append(RawTable(sh.name, rows, row_numbers, levels))
    return ParsedFile(path, "xls", tables, book.sheet_names())
