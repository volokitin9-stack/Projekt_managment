"""Generate representative (synthetic) engineering plan files for tests.

These files are NOT the department's real plan - they exercise the structures
the agent must handle: title rows above the header, a two-row header with
merged stage groups, bold section rows (manufacturer), vertically merged
model cells, Russian vocabulary, an unknown stage, invalid dates, a totals
row, an irrelevant lookup sheet, and a long (activity-per-row) CSV variant.

Run:  python tests/fixtures/make_fixtures.py <output_dir>
"""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill

STAGES = ["Сканирование", "Разработка 3D", "Опытный образец", "Примерка", "КД"]


def make_wide_xlsx(path: Path, variant: int = 1) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "План КО"
    ws["A1"] = "План работ конструкторского отдела на 2026 г."
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "Утверждаю: начальник КО"
    head = ["№", "Модель", "Изделие", "Артикул", "Конструктор", "Приоритет"]
    for j, h in enumerate(head, start=1):
        ws.cell(4, j, h)
        ws.merge_cells(start_row=4, start_column=j, end_row=5, end_column=j)
    col = len(head) + 1
    for st in STAGES:
        ws.cell(4, col, st)
        ws.merge_cells(start_row=4, start_column=col, end_row=4, end_column=col + 1)
        ws.cell(5, col, "План")
        ws.cell(5, col + 1, "Факт")
        col += 2
    extra = ["Покраска", "Статус", "Зависит от", "Примечание"]
    for h in extra:
        ws.cell(4, col, h)
        ws.merge_cells(start_row=4, start_column=col, end_row=5, end_column=col)
        col += 1
    last_col = col - 1
    for c in ws[4] + ws[5]:
        c.font = Font(bold=True)

    d = date
    # rows: (no, model, product, article, engineer, prio, [(plan, fact)*5], paint, status, deps, note)
    data = [
        ("group", "HAVAL"),
        ("ENG-001", "Jolion", "Защита картера", "HV-JL-01", "Иванов", "Высокий",
         [(d(2026, 9, 1), d(2026, 9, 3)), (d(2026, 9, 20), d(2026, 9, 25)), (d(2026, 10, 10), None),
          (d(2026, 10, 25), None), (d(2026, 11, 25), None)], None, "В работе", None, "Ждём крепёж"),
        ("ENG-002", "Jolion", "Защита КПП", "HV-JL-02", "Иванов", "Средний",
         [(d(2026, 9, 1), d(2026, 9, 3)), (d(2026, 10, 1), None), (None, None), (None, None),
          (d(2026, 12, 15), None)], None, None, "ENG-001", None),
        ("ENG-003", "Dargo", "Защита картера", "HV-DG-01", "Петров", "Высокий",
         [(d(2026, 6, 1), d(2026, 6, 2)), (d(2026, 6, 20), d(2026, 6, 28)), (d(2026, 7, 10), d(2026, 7, 15)),
          (d(2026, 7, 20), d(2026, 7, 22)), (d(2026, 8, 1), d(2026, 8, 5))], None, "Выполнено", None, None),
        ("group", "CHERY"),
        ("ENG-004", "Tiggo 7 Pro", "Защита картера", None, "Сидоров", "Низкий",
         [(d(2026, 11, 20), None), (None, None), (None, None), (None, None), (d(2027, 2, 1), None)],
         None, None, None, "Авто будет в ноябре"),
        ("ENG-005", "Tiggo 4", "Пороги", "CH-T4-07", None, None,
         [(d(2026, 8, 1), d(2026, 8, 1)), ("32.13.2026", None), (None, None), (None, None), (None, None)],
         "29.10.2026", "Приостановлено", None, None),
        ("ENG-006", "Tiggo 8", "Фаркоп", "CH-T8-11", "Петров", "Высокий",
         [(d(2026, 5, 1), d(2026, 5, 2)), (d(2026, 5, 20), None), (None, None), (None, None), (None, None)],
         None, "Выполнено", None, "Статус противоречит этапам"),
    ]
    if variant == 2:
        # Second delivery of the plan: progress on ENG-001, a new project, one
        # project dropped from the file (must NOT be deleted), changed deadline.
        data[1] = ("ENG-001", "Jolion", "Защита картера", "HV-JL-01", "Иванов", "Высокий",
                   [(d(2026, 9, 1), d(2026, 9, 3)), (d(2026, 9, 20), d(2026, 9, 25)),
                    (d(2026, 10, 10), d(2026, 10, 30)), (d(2026, 11, 5), None), (d(2026, 11, 30), None)],
                   None, "В работе", None, None)
        data.pop(2)  # ENG-002 missing from the new file
        data.append(("ENG-007", "Tiggo 7 Pro", "Защита КПП", "CH-T7-02", "Сидоров", "Средний",
                     [(d(2026, 12, 1), None), (None, None), (None, None), (None, None), (None, None)],
                     None, None, None, None))
    r = 6
    for row in data:
        if row[0] == "group":
            ws.cell(r, 1, row[1]).font = Font(bold=True)
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=last_col)
            ws.cell(r, 1).fill = PatternFill("solid", fgColor="FFDDEBF7")
            r += 1
            continue
        no, model, product, art, eng, prio, stages, paint, status, deps, note = row
        vals = [no, model, product, art, eng, prio]
        for p, f in stages:
            vals += [p, f]
        vals += [paint, status, deps, note]
        for j, v in enumerate(vals, start=1):
            c = ws.cell(r, j, v)
            if isinstance(v, date):
                c.number_format = "DD.MM.YYYY"
        r += 1
    # vertically merged model cell over the two Jolion projects
    if variant == 1:
        ws.merge_cells(start_row=7, start_column=2, end_row=8, end_column=2)
    ws.cell(7, 5).comment = Comment("Иванов в отпуске с 1.12", "PM")
    ws.cell(r, 1, "Итого проектов: 6")
    # irrelevant lookup sheet
    ref = wb.create_sheet("Справочник")
    ref.append(["Конструкторы"])
    for n in ["Иванов", "Петров", "Сидоров"]:
        ref.append([n])
    wb.save(path)


def make_long_csv(path: Path) -> None:
    rows = [
        ["Код проекта", "Автомобиль", "Изделие", "Работа", "Ответственный", "Начало", "Окончание", "Факт",
         "Статус", "Зависит от"],
        ["PRJ-10", "GEELY Monjaro", "Защита картера", "Сканирование автомобиля", "Кузнецов", "01.10.2026",
         "03.10.2026", "03.10.2026", "Выполнено", ""],
        ["PRJ-10", "GEELY Monjaro", "Защита картера", "CAD моделирование", "Кузнецов", "05.10.2026",
         "30.10.2026", "", "В работе", "Сканирование"],
        ["PRJ-10", "GEELY Monjaro", "Защита картера", "Изготовление прототипа", "Смирнов", "01.11.2026",
         "15.11.2026", "", "Не начато", "CAD моделирование"],
        ["PRJ-10", "GEELY Monjaro", "Защита картера", "Согласование с заказчиком", "Кузнецов", "", "20.11.2026",
         "", "", ""],
        ["PRJ-11", "Lixiang L7", "Защита батареи", "Сканирование", "Смирнов", "10.11.2026", "12.11.2026", "",
         "В работе", ""],
        ["PRJ-11", "Lixiang L7", "Защита батареи", "Термоформовка", "Смирнов", "", "", "", "", ""],
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh, delimiter=";").writerows(rows)


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    make_wide_xlsx(out / "engineering_plan_v1.xlsx", 1)
    make_wide_xlsx(out / "engineering_plan_v2.xlsx", 2)
    make_long_csv(out / "engineering_plan_long.csv")
    print("written to", out)
