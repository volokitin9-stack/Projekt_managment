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


# --------------------------------------------------------------------------- #
# Monthly work-plan workbook (structure of the department's real plan; the
# content here is synthetic).
# --------------------------------------------------------------------------- #
MONTH_HEADER = ["№", "КО", "Канал продаж", "ТГ", "Приоритет", "Выполнимо", "Марка", "Модель", "Марка+Модель",
                "Продукт/Задача", "План на месяц", "Стадия проекта", "Группа задач", "Артикул", "Номер заказа",
                "NOK", "ОК", "Вероятность2", "Вероятность", "Шанс", "Факт", "Комментарии", "План месяц",
                "Факт месяц"] + list(range(1, 31))


def _task(eng, ch, tg, prio, mk, model, prod, task, stage, group, art=None, plan=8, fact=0, comment=None,
          ok=None, days=None):
    return dict(eng=eng, ch=ch, tg=tg, prio=prio, mk=mk, model=model, prod=prod, task=task, stage=stage,
                group=group, art=art, plan=plan, fact=fact, comment=comment, ok=ok, days=days or {})


def month_tasks(variant: int = 1):
    fit = ("Примерить", "Примерка", "Замеры")
    kd = ("КД", "Серия", "Разработка")
    c1 = ("1С", "Серия", "Разработка")
    pdm = ("Прочее", "Оформление", "Выгрузка в PDM")
    dev = ("Разработать", "Разработка", "Разработка")
    order = ("Заказать", "Разработка", "Разработка")
    lab = ("Прочее", "Операционка", "Лабаратория, разбор опытников, фотоконтент")
    gac = [("Орлов/Соколов",) + fit, ("Иванов",) + kd, ("Иванов",) + c1, ("Иванов",) + pdm]
    t = []
    for i, (eng, task, stage, group) in enumerate(gac):
        fact = 5 if (variant == 2 and task == "КД") else 0
        t.append(_task(eng, "CIS", "ЗК", 1, "GAC", "S9", "Комплект защит", task, stage, group, plan=8, fact=fact,
                       days={3: 5} if fact else None))
    for eng, task, stage, group in [("Петров",) + dev, ("Петров",) + order, ("Петров",) + lab,
                                    ("Петров",) + kd, ("Петров",) + c1, ("Петров",) + pdm]:
        t.append(_task(eng, "CIS", "ЗК", 3, "Honda", "CR-V (1995-2001)", "Защита картера+КПП", task, stage, group))
    t.append(_task("Сидоров", "CIS", "ЗК", 2, "Belgee", "X80 (P145)", "Защита батареи", "1С", "Серия перевод",
                   "Перевод", art="XX10TEST01", ok=1, plan=4, fact=4))
    t.append(_task("Сидоров", "CIS", "ЗК", 2, "Belgee", "X80 (P145)", "Защита батареи", "Прочее", "Оформление",
                   "Выгрузка в PDM", art="XX10TEST01", plan=1))
    for eng in ("Иванов", "Петров"):     # two vehicle-less blocks of the same task
        for task, stage, group in [("КД", "Серия перевод", "Перевод на ШО"), ("1С", "Серия перевод", "Перевод на ШО"),
                                   pdm]:
            t.append(_task(eng, "CIS", "ЗК ШО", 1, None, None, "Проверка, перевод штампованых защит", task, stage,
                           group, art="ЗАПОЛНИТЬ (T.OSN.1)" if task == "1С" and eng == "Иванов" else None))
    t.append(_task("Н", "CIS", "ЗК", 1, "AUDI ", "A5 long", "Комплект защит", "КД", "Серия", "Разработка"))
    t.append(_task("Иванов", "CIS", "Прочее", 1.1, None, None, "Техподдержка", None,
                   "Улучшения конструктивов (запрос пр-во, акты, рекламации)", "Изменения (запросы, акты, рекламации)"))
    if variant == 2:
        t.append(_task("Петров", "OEM", "ТСУ", 1, "Lada", "Azimut", "ТСУ", "Верификация", "Серия", "Тесты"))
    return t


def write_month_sheet(ws, tasks, filter_engineer=None):
    for j, h in enumerate(MONTH_HEADER, start=1):
        ws.cell(1, j, h)
    col = {h: j for j, h in enumerate(MONTH_HEADER, start=1)}
    r = 2
    for n, t in enumerate(tasks, start=1):
        vals = {"№": n, "КО": t["eng"], "Канал продаж": t["ch"], "ТГ": t["tg"], "Приоритет": t["prio"],
                "Марка": t["mk"], "Модель": t["model"],
                "Марка+Модель": f"{t['mk']} {t['model']}" if t["mk"] else None, "Продукт/Задача": t["prod"],
                "План на месяц": t["task"], "Стадия проекта": t["stage"], "Группа задач": t["group"],
                "Артикул": t["art"], "ОК": t["ok"], "Комментарии": t["comment"], "План месяц": t["plan"],
                "Факт месяц": t["fact"]}
        for k, v in vals.items():
            if v is not None:
                ws.cell(r, col[k], v)
        for d, h in t["days"].items():
            ws.cell(r, col[d], h)
        if filter_engineer and t["eng"] != filter_engineer:
            ws.row_dimensions[r].hidden = True
        r += 1
    ws.cell(r, 2, "ДОП РАБОТЫ"); r += 1
    ws.cell(r, 2, "Иванов"); ws.cell(r, 4, "Доп"); ws.cell(r, col["Комментарии"], "Упаковка защит")
    ws.cell(r, col["Факт месяц"], 3); r += 1
    ws.cell(r, 2, "БАНК ЗАДАЧ"); r += 1
    ws.cell(r, 7, "JETOUR"); ws.cell(r, 8, "X50"); ws.cell(r, 10, "Сетки"); ws.cell(r, 11, "Разработать")
    ws.cell(r, col["Комментарии"], "На Август"); r += 1
    ws.cell(r, 12, "Итого Aftermarket CIS:"); ws.cell(r, col["Факт месяц"], 100); r += 3
    ws.cell(r, 2, "КО"); ws.cell(r, 11, "Продукт/Задача"); r += 1
    for v in ("Иванов", "Петров", "Сидоров", "Орлов/Соколов", "Н"):
        ws.cell(r, 2, v); r += 1
    if filter_engineer:
        ws.auto_filter.ref = f"A1:BD{len(tasks) + 1}"
        ws.auto_filter.add_filter_column(1, [filter_engineer])


def make_monthly_workbook(path: Path, variant: int = 1, duplicate_november: bool = False) -> None:
    wb = openpyxl.Workbook()
    oct_ws = wb.active
    oct_ws.title = "Октябрь"
    write_month_sheet(oct_ws, [_task("Иванов", "CIS", "ЗК", 1, "Chery", "Tiggo 9", "Защита картера", "КД",
                                     "Серия", "Разработка")])
    nov = wb.create_sheet("Ноябрь")
    write_month_sheet(nov, month_tasks(variant), filter_engineer="Петров")
    summ = wb.create_sheet("Сводная Ноябрь")
    summ["A1"] = "ЦЕЛЬ"
    summ["A15"], summ["B15"] = "ТГ", "Марка+Модель"
    if duplicate_november:
        write_month_sheet(wb.create_sheet("Ноябрь (2)"), month_tasks(variant))
    wb.create_sheet("Приоритеты")["B5"] = "Месяц"
    wb.save(path)
