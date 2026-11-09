"""Initial-analysis / dry-run report for the monthly work-plan profile."""
from __future__ import annotations

from collections import Counter, defaultdict

from ..models import ChangeType, Severity

STAGE_LABEL = {"scanning": "Scanning", "development": "Development", "test_prototype": "Test Prototype",
               "measurement": "Measurement", "documentation": "Documentation",
               "extra:series_transfer": "Series transfer (extra)", "extra:testing": "Testing (extra)",
               "extra:engineering_change": "Engineering change (extra)", None: "— unknown —"}
BOARD = ["scanning", "development", "test_prototype", "measurement", "documentation",
         "extra:series_transfer", "extra:testing", "extra:engineering_change", None]

FIELD_MAP = [
    ("Марка", "manufacturer", "dashboard field (identity)"),
    ("Модель", "vehicle_model", "dashboard field (identity)"),
    ("Продукт/Задача", "product_type", "dashboard field (identity)"),
    ("Марка + Модель + Продукт", "project_name", "composed display name"),
    ("Артикул", "article_number", "only when it is one valid article; otherwise extra_attributes.article_original"),
    ("КО", "assigned_engineer", "designer of the development/КД/1С tasks; all names in extra_attributes.engineers "
                                "and per task in stages[].engineer"),
    ("Приоритет", "priority", "1, 1.1–1.3 → high · 2 → medium · 3 → low · other values kept unknown; "
                              "original in extra_attributes.priority_original"),
    ("План на месяц + Стадия проекта + Группа задач", "stages[] / current_stage",
     "each task row → one activity; mapped by the ordered activity rules"),
    ("Факт месяц / ОК", "stages[].status → project_status",
     "hours logged → in_progress · 'ОК' > 0 → done (to confirm) · planned only → pending"),
    ("План месяц, Факт месяц, days 1–31", "stages[].details",
     "planned/actual hours and hours per calendar date"),
    ("Канал продаж", "extra_attributes.sales_channel", "not a dashboard field"),
    ("ТГ", "extra_attributes.product_group", "not a dashboard field"),
    ("Комментарии", "extra_attributes.plan_comment + stages[].comment", "never written to dashboard comments"),
    ("Номер заказа, NOK, ОК, Факт, Вероятность, Шанс, Выполнимо", "stages[].details", "preserved as is"),
    ("Вероятность2", "stages[].details.unmapped_columns", "duplicate of 'Вероятность'; preserved"),
    ("Марка+Модель", "— (cross-check only)", "formula column; mismatches are reported"),
    ("№", "— (not imported)", "running row counter, changes every month; not an identifier"),
    ("—", "planned_start_date / planned_end_date", "NOT in the file; left empty (no deadlines are invented)"),
    ("—", "actual_completion_date", "NOT in the file; left empty"),
    ("—", "description, comments, photos", "dashboard-owned; never written"),
]


def _cell(v) -> str:
    return str(v if v not in (None, "", []) else "—").replace("|", "/").replace("\n", " ")


def render_profile_analysis(a, plans, report) -> str:
    p = a.profile
    an = p.analysis
    recs = a.records
    plan_of = {id(x.record): x for x in plans}
    L: list[str] = []
    w = L.append
    w(f"# Dry run: {a.file_name} → Projekt_managment")
    w("")
    w(f"*Profile:* `{an['profile']}` · *target month:* **{an['target_month']}** · *source worksheet:* "
      f"**{an.get('source_sheet')}** · *fingerprint:* `{a.fingerprint}` · *mode:* dry run (nothing written)")
    w("")
    w("> Only the worksheet of the target month is imported. Other worksheets were inspected for structure only.")
    w("")

    # ---------------------------------------------------------------- 1
    reviews = [r for r in recs if r.needs_review()]
    ex = an.get("excluded", {})
    w("## 1. Detected projects")
    w("")
    w(f"* **{len(recs)} project records** from {an['task_rows']} task rows of '{an.get('source_sheet')}' "
      f"(of which {an['hidden_task_rows']} rows are hidden by the sheet's AutoFilter; all were read)")
    w(f"* **{len(recs) - len(reviews)}** can be identified reliably, **{len(reviews)}** need verification")
    w(f"* Not imported as projects: {len(ex.get('extra_work', []))} ad-hoc work rows (section ДОП РАБОТЫ), "
      f"{len(ex.get('backlog', []))} backlog rows (section БАНК ЗАДАЧ), {len(ex.get('operational', []))} "
      "operational items without a vehicle")
    w("")

    # ---------------------------------------------------------------- 2
    lay = an["layout"]
    w("## 2. Identified structure")
    w("")
    w("**Worksheets mentioning the target month**")
    w("")
    w("| Worksheet | Classification |")
    w("|---|---|")
    month_no = int(an["target_month"][-2:])
    for s in an["sheets"]:
        if month_no in s["months"]:
            w(f"| {s['name']} | {s['role']} |")
    others = Counter(s["role"] for s in an["sheets"] if month_no not in s["months"])
    w("")
    w(f"Other worksheets (not imported): {dict(others)}")
    w("")
    w(f"**'{an.get('source_sheet')}' layout:** header on row {lay['header_row']}; task rows "
      f"{lay['section_rows']['main'][0]}–{lay['section_rows']['main'][1]} (projects); section **ДОП РАБОТЫ** rows "
      f"{_range(lay['section_rows'].get('extra_work'))}; section **БАНК ЗАДАЧ** rows "
      f"{_range(lay['section_rows'].get('backlog'))}; totals from row {lay['end_of_tasks_row']}; below them the "
      f"resource table and the dropdown source lists. {lay['day_columns']} day columns (hours per day). "
      f"Merged ranges: {len(an.get('merged_ranges', []))}. AutoFilter: {an.get('autofilter') or 'none'}.")
    w("")
    w("**Project = vehicle (Марка + Модель) + product (Продукт/Задача).** Each project has one or more task "
      "cycles in the month, e.g. *Примерить → КД → 1С → Прочее (Выгрузка в PDM)*. Column № is a running "
      "counter and is not used as an identifier. A single valid Артикул is used as the strongest identifier.")
    w("")
    sizes = Counter(len(r.activities) for r in recs)
    w(f"Tasks per project: {dict(sorted(sizes.items()))}")
    w("")

    # ---------------------------------------------------------------- 3
    w("## 3. Engineering workflow and stage mapping")
    w("")
    w("Department workflow found in the file (column *Стадия проекта*, tasks *План на месяц*, "
      "groups *Группа задач*):")
    w("")
    w("* **New product:** Сканирование (Отсканировать) → Разработка (Разработать / Доработать) → "
      "Заказать опытник → Лаборатория, разбор опытников → Примерка (Примерить, «Замеры») → "
      "Серия (КД, 1С) → Оформление (Выгрузка в PDM)")
    w("* **Transfer/extension of an existing product:** Серия перевод (Перевод / Расширение → 1С → PDM)")
    w("* **Other:** Тесты, Улучшения конструктивов / Изменения (change requests), Операционка")
    w("")
    w("| Task \\| Project stage \\| Task group (from the file) | Dashboard stage | Rows |")
    w("|---|---|---|")
    for combo, info in a.proposal["value_mappings"]["activity_combinations"].items():
        w(f"| {combo.replace('|', '·')} | {STAGE_LABEL.get(info['stage'], info['stage'])} | {info['rows']} |")
    w("")
    w("The dashboard's current stage of a project = the furthest stage with hours logged this month, "
      "otherwise the earliest planned stage that is not done. A passed date never marks anything complete.")
    w("")

    # ---------------------------------------------------------------- 4
    w("## 4. Excel → dashboard field mapping")
    w("")
    w("| Excel column | Dashboard field | Rule |")
    w("|---|---|---|")
    for col, fld, rule in FIELD_MAP:
        w(f"| {col} | `{fld}` | {rule} |")
    w("")

    # ---------------------------------------------------------------- 5
    w("## 5. Projects and their stages")
    w("")
    w("| # | Project | Rows | ТГ | Stage | Status | Engineer | Priority | Tasks this month | Action |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(recs, 1):
        tasks = ", ".join(f"{x.name}→{(x.stage or '?').replace('extra:', '+')}" for x in r.activities)
        pl = plan_of.get(id(r))
        rows = f"{r.source_rows[0]}–{r.source_rows[-1]}" if len(r.source_rows) > 1 else str(r.source_row)
        w(f"| {i} | {_cell(r.project_name)} | {rows} | {_cell(r.extra_attributes.get('product_group'))} | "
          f"{STAGE_LABEL.get(r.current_stage, r.current_stage)} | {_cell(r.project_status)} | "
          f"{_cell(r.assigned_engineer)} | {_cell(r.priority)} ({_cell(r.extra_attributes.get('priority_original'))}) "
          f"| {tasks} | {pl.change_type.value if pl else '?'} |")
    w("")

    # ---------------------------------------------------------------- 6
    w("## 6. Responsible engineers")
    w("")
    eng_proj = defaultdict(list)
    for r in recs:
        eng_proj[r.assigned_engineer or "— not assigned —"].append(r)
    task_eng = Counter(x.engineer or "— none —" for r in recs for x in r.activities)
    w("| Engineer (КО) | Projects as responsible | Task rows (all projects) |")
    w("|---|---|---|")
    for e in sorted(set(eng_proj) | set(task_eng), key=lambda k: -len(eng_proj.get(k, []))):
        w(f"| {e} | {len(eng_proj.get(e, []))} | {task_eng.get(e, 0)} |")
    w("")
    vocab = an.get("department_vocabulary", {})
    if vocab.get("engineer"):
        w(f"Engineer list in the sheet's dropdown: {vocab['engineer']}. Values with '/' are two-person teams "
          "(scanning and trial fits). Values in unassigned_engineer_markers are treated as *unassigned* (to confirm).")
        w("")

    # ---------------------------------------------------------------- 7
    w("## 7. Deadlines and priorities")
    w("")
    w("* **Deadlines: the November worksheet contains no date columns and no deadlines.** Planned start/end "
      "and completion dates stay empty. The tasks are planned *within* the month "
      f"({an['target_month']}); this is stored as `plan_month`, not as a deadline. Dates mentioned in "
      "comments (e.g. «ЖДЕМ ОПЫТНИК 06.11») are kept as text only.")
    w("* Hours booked per calendar day (columns 1–31) are kept per task as `hours_by_date` (actual work log).")
    pr = Counter((r.priority, str(r.extra_attributes.get("priority_original"))) for r in recs)
    w("* Priorities found: " + ", ".join(f"{orig} → {mapped or 'unknown'} ({n})" for (mapped, orig), n in
                                         sorted(pr.items(), key=lambda x: str(x[0]))))
    w("")

    # ---------------------------------------------------------------- 8
    w("## 8. Potential duplicates and ambiguous records")
    w("")
    for title, codes in (("Cannot be identified (no vehicle / incomplete)", ("no_vehicle", "incomplete_identity")),
                         ("Possible duplicates / related records", ("related_projects_same_vehicle",
                                                                    "multiple_work_cycles",
                                                                    "possible_separate_projects")),
                         ("Conflicting or inconsistent values", ("priority_conflict", "vehicle_full_mismatch",
                                                                 "invalid_article", "engineer_placeholder",
                                                                 "unmapped_priority", "unmapped_activity"))):
        items = [(r, i) for r in recs for i in r.issues if i.code in codes]
        w(f"**{title}** ({len(items)})")
        w("")
        for r, i in items:
            w(f"* {_cell(r.project_name)} (rows {r.source_rows[0]}–{r.source_rows[-1]}): {i.message}")
        w("")
    spell = [r for r in recs if r.extra_attributes.get("spelling_variants")]
    names = defaultdict(set)
    for r in recs:
        if r.manufacturer:
            names[r.manufacturer.strip().lower()].add(r.manufacturer.strip())
    variants = {k: sorted(v) for k, v in names.items() if len(v) > 1}
    if variants or spell:
        w(f"**Spelling variants** (matched as the same value, displayed as written): {list(variants.values())}")
        w("")

    # ---------------------------------------------------------------- 9
    c = report["counts"]
    w("## 9. Proposed dashboard changes (dry run)")
    w("")
    if not report.get("dashboard_comparison"):
        w("> Dashboard could not be queried; counts assume an empty dashboard.")
        w("")
    w(f"* Create **{c['new']}** projects · update **{c['updated']}** · unchanged {c['unchanged']} · "
      f"held for review **{c['review']}** · invalid {c['invalid_records']}")
    w("* Nothing is deleted. Dashboard comments, description, photos and attachments are never sent.")
    w("* Each created project gets: identity fields, name, engineer, priority, current stage, status (when there "
      "is evidence), `stages` (every November task with hours, status and source row) and `extra_attributes` "
      "(channel, product group, comments, original values, plan month, derived-field list).")
    w("")

    # ---------------------------------------------------------------- 10
    w("## 10. Decisions requiring your verification")
    w("")
    qs = [
        f"**Source worksheet:** '{an.get('source_sheet')}' is the November plan; 'Сводная Ноябрь' is treated as a "
        "derived pivot summary and not imported. Correct?",
        "**Year:** worksheet names carry no year; the target is taken as **"
        f"{an['target_month']}** (file name mentions 2026). Correct?",
        "**Sections:** ДОП РАБОТЫ (ad-hoc work) and БАНК ЗАДАЧ (backlog: items marked «На Июнь/Июль/Август», "
        "«На согласовании») are **not** imported as projects. Import any of them?",
        "**Priority scale:** 1/1.x = high, 2 = medium, 3 = low; value 0 (Lynk & Co 900) left unknown. Correct?",
        "**Status from 'ОК':** 'ОК' > 0 on a task = task done (from 'Факт ОК' in the summary pivot). "
        "Correct? (Only 3 rows are affected; no project becomes 'completed'.)",
        "**Engineer 'Н':** treated as *not assigned* (AUDI A5 long). What does it mean?",
        "**Responsible engineer:** the designer doing development/КД/1С; when only scanning/fitting is planned this "
        "month, the scan/fit team is shown. Acceptable?",
        "**Series transfer (Перевод / Расширение)** is shown as an extra stage *Series transfer* instead of one of "
        "the five main stages. Or should it count as *Documentation*?",
        "**Records without vehicle** (section 8) are held back. Provide the vehicle, or keep them out?",
        "**Multiple task cycles** (Honda CR-V, Jeland C4 бампер, Jeland C4 сетки) are merged into one project each. "
        "Correct, or are they separate variants?",
    ]
    for q in qs:
        w(f"1. {q}")
    w("")

    # ---------------------------------------------------------------- board
    w("## Dashboard distribution preview")
    w("")
    w("How the importable projects will appear on the dashboard (by current stage):")
    w("")
    board = defaultdict(list)
    for r in recs:
        if not r.needs_review():
            board[r.current_stage].append(r)
    for st in BOARD:
        items = board.get(st, [])
        if not items:
            continue
        w(f"**{STAGE_LABEL[st]} ({len(items)})**")
        w("")
        for r in items:
            flag = " · *in progress*" if r.project_status == "in_progress" else ""
            w(f"* {r.project_name} — {r.assigned_engineer or 'no engineer'} · prio {r.priority or '—'}{flag}")
        w("")
    by_group = Counter(r.extra_attributes.get("product_group") if isinstance(r.extra_attributes.get("product_group"), str)
                       else "mixed" for r in recs if not r.needs_review())
    by_status = Counter(r.project_status or "unknown (no hours logged yet)" for r in recs if not r.needs_review())
    w(f"By product group: {dict(by_group)}")
    w("")
    w(f"By status: {dict(by_status)}")
    w("")
    w("## Appendix: rows of the month sheet that are not imported as projects")
    w("")
    for key, title in (("backlog", "БАНК ЗАДАЧ (backlog)"), ("operational", "Operational work without a vehicle"),
                       ("extra_work", "ДОП РАБОТЫ (ad-hoc work)")):
        items = ex.get(key, [])
        w(f"**{title}: {len(items)} rows**")
        w("")
        for it in items:
            label = " · ".join(_cell(x) for x in (it.get("vehicle"), it.get("product"), it.get("task")) if x) or "—"
            w(f"* row {it['row']}: {label} — {_cell(it.get('engineer'))}; {_cell(it.get('comment'))}"
              + (f"; {it['actual_hours']:g} h logged" if it.get("actual_hours") else ""))
        w("")
    w("## Next step")
    w("")
    w("Answer the questions in section 10. Then the mapping in `state/mapping.proposed.yaml` (adjusted to your "
      "answers) is approved with `python -m plan_agent approve`, and the first synchronisation runs only after "
      "that. Later monthly files sync automatically with the approved rules; a changed column structure, a "
      "second plan sheet for the month or unseen task/stage values stop the run for verification.")
    return "\n".join(L) + "\n"


def _range(v):
    return f"{v[0]}–{v[1]}" if v else "—"
