import shutil

import pytest

from plan_agent.parsers import (CorruptFileError, EmptyFileError, UnsupportedFormatError, detect_format,
                                parse_file)


def test_format_detected_from_content_not_name(fixtures_dir, tmp_path):
    renamed = tmp_path / "upload.bin"
    shutil.copy(fixtures_dir / "engineering_plan_v1.xlsx", renamed)
    assert detect_format(renamed) == "xlsx"
    parsed = parse_file(renamed)
    assert parsed.sheet_names == ["План КО", "Справочник"]


def test_merged_cells_are_expanded_and_marked(fixtures_dir):
    t = parse_file(fixtures_dir / "engineering_plan_v1.xlsx").tables[0]
    # header "Сканирование" is merged across two columns (G:H) in row 4
    row4 = t.rows[t.row_numbers.index(4)]
    assert row4[6].text() == row4[7].text() == "Сканирование"
    assert row4[7].merged and not row4[6].merged
    # model "Jolion" merged vertically over rows 7-8
    r8 = t.rows[t.row_numbers.index(8)]
    assert r8[1].text() == "Jolion" and r8[1].merged


def test_empty_file(tmp_path):
    p = tmp_path / "empty.xlsx"
    p.write_bytes(b"")
    with pytest.raises(EmptyFileError):
        parse_file(p)


def test_corrupted_xlsx(tmp_path):
    p = tmp_path / "broken.xlsx"
    p.write_bytes(b"PK\x03\x04" + b"garbage" * 20)
    with pytest.raises(CorruptFileError):
        parse_file(p)


def test_unsupported_binary(tmp_path):
    p = tmp_path / "image.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    with pytest.raises(UnsupportedFormatError):
        parse_file(p)


def test_csv_cp1251_semicolon(tmp_path):
    p = tmp_path / "plan.csv"
    p.write_bytes("Марка;Модель;Изделие;Конструктор\nHAVAL;Jolion;Защита картера;Иванов\n".encode("cp1251"))
    t = parse_file(p).tables[0]
    assert [c.text() for c in t.rows[1]] == ["HAVAL", "Jolion", "Защита картера", "Иванов"]


def test_docx_tables(tmp_path):
    import docx
    d = docx.Document()
    d.add_heading("План КО", level=1)
    tbl = d.add_table(rows=3, cols=4)
    for i, row in enumerate([["Марка", "Модель", "Изделие", "Срок"],
                             ["HAVAL", "Jolion", "Защита картера", "25.11.2026"],
                             ["CHERY", "Tiggo 4", "Пороги", "01.12.2026"]]):
        for j, v in enumerate(row):
            tbl.cell(i, j).text = v
    p = tmp_path / "plan.docx"
    d.save(p)
    parsed = parse_file(p)
    assert parsed.file_format == "docx"
    assert parsed.tables[0].source.startswith("План КО")
    assert parsed.tables[0].rows[1][2].text() == "Защита картера"


def test_pdf_table(tmp_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    p = tmp_path / "plan.pdf"
    data = [["Project ID", "Make", "Model", "Product", "Engineer", "Deadline"],
            ["ENG-1", "HAVAL", "Jolion", "Engine protection", "Ivanov", "2026-11-25"],
            ["ENG-2", "CHERY", "Tiggo 4", "Side steps", "Petrov", "2026-12-01"]]
    doc = SimpleDocTemplate(str(p), pagesize=A4)
    t = Table(data)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "black")]))
    doc.build([t])
    parsed = parse_file(p)
    assert parsed.file_format == "pdf"
    texts = [[c.text() for c in r] for r in parsed.tables[0].rows]
    assert texts[1][:2] == ["ENG-1", "HAVAL"]
