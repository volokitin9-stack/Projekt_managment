"""Profile for the department's monthly work-plan workbook.

One worksheet per month; each row is a task (План на месяц) for a vehicle
(Марка/Модель) and product (Продукт/Задача).  Only the worksheet of the target
month is imported; other worksheets are inspected for classification only.

The month sheet produces three record categories:

* main_project    - task rows of the main section, grouped per vehicle+product
* additional_work - rows of section "ДОП РАБОТЫ" (one record per assignment)
* backlog         - rows of section "БАНК ЗАДАЧ" (one record per item)

Every task row becomes an activity with its own assignee, hours, progress and
source reference.  Every non-empty row of the sheet is accounted for in a
ledger, so nothing can be dropped silently.

All interpretation rules come from config/profiles/monthly_work_plan.yaml or,
once approved, from state/mapping.approved.yaml.  Uncertain decisions become
issues on the record; nothing is guessed.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from openpyxl.utils import column_index_from_string

from ..models import Activity, ProjectRecord, Severity
from ..normalize import clean_text, norm_header, norm_key
from . import xlsx_fast

STAGE_ORDER = ["scanning", "development", "test_prototype", "measurement", "documentation"]
ADDITIONAL_TYPES = ["testing", "transfer_extension", "engineering_change"]
ACTIVITY_TYPES = STAGE_ORDER + ADDITIONAL_TYPES
MONTH_RU = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь", 7: "июль", 8: "август",
            9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"}
IDENTITY_REVIEW_CODES = ("no_vehicle", "incomplete_identity", "operational_work")


def load_profile(config_dir: Path) -> dict:
    with open(Path(config_dir) / "profiles" / "monthly_work_plan.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_month(value: Optional[str], profile: dict, today: Optional[date] = None) -> tuple[int, int]:
    v = value or profile.get("target_month") or "auto"
    if v == "auto":
        t = today or date.today()
        return t.year, t.month
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", str(v))
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"target month must be YYYY-MM or 'auto', got {v!r}")
    return int(m.group(1)), int(m.group(2))


def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _fmt_num(v) -> Optional[str]:
    """Priority-like values: 1 -> '1', 1.1 -> '1.1'; text kept as text."""
    if v is None:
        return None
    n = _num(v)
    if n is None:
        return clean_text(v)
    return str(int(n)) if n.is_integer() else f"{n:g}"


def _hash(*parts) -> str:
    return hashlib.sha1("|".join(norm_key(p) for p in parts).encode()).hexdigest()[:10].upper()


@dataclass
class TaskRow:
    row: int
    section: str
    hidden: bool
    v: dict[str, Any]                  # role -> raw value
    days: dict[int, float]             # day of month -> hours
    unmapped: dict[str, Any]

    def t(self, role: str) -> Optional[str]:
        return clean_text(self.v.get(role))


@dataclass
class ProfileResult:
    records: list[ProjectRecord]
    proposal: dict
    fingerprint: str
    analysis: dict
    blocking: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    new_values: dict = field(default_factory=dict)


class MonthlyPlanProfile:
    name = "monthly_work_plan"

    def __init__(self, profile: dict, approved: Optional[dict] = None):
        self.p = profile
        self.approved = approved if approved and approved.get("profile") == self.name else None
        src = self.approved or {}

        def cfg(key):
            return src.get(key) if src.get(key) is not None else profile.get(key)

        self.rules = cfg("activity_rules")
        self.status_rules = cfg("status_rules")
        self.priority_map = {str(k): v for k, v in cfg("priority_map").items()}
        self.undefined_priorities = {str(x) for x in cfg("undefined_priority_values") or []}
        self.unknown_engineers = {norm_header(x) for x in cfg("unknown_engineer_values") or []}
        self.design_types = set(cfg("design_activity_types") or [])
        self.section_categories = cfg("section_categories")
        self.operational_category = cfg("operational_category") or "review"
        self.id_prefixes = cfg("id_prefixes")
        self.sheet_override = {**(profile.get("sheet_override") or {}), **(src.get("sheet_override") or {})}
        self._norm_rules = [({k: {norm_header(x) for x in v} for k, v in r["when"].items()}, r) for r in self.rules]

    # ------------------------------------------------------------------ #
    # detection & sheet classification
    # ------------------------------------------------------------------ #
    def month_tokens(self, sheet_name: str) -> set[int]:
        tokens = re.findall(r"[a-zа-яё]+", sheet_name.lower().replace("ё", "е"))
        found = set()
        for m, names in self.p["month_names"].items():
            names_n = {n.replace("ё", "е") for n in names}
            if any(t in names_n for t in tokens):
                found.add(int(m))
        return found

    def find_header(self, rows: list[list[Any]]) -> Optional[int]:
        req = {norm_header(h) for h in self.p["required_headers"]}
        for i, row in enumerate(rows[: self.p.get("header_search_rows", 10)]):
            vals = {norm_header(str(v)) for v in row if v is not None}
            if req <= vals:
                return i + 1
        return None

    @classmethod
    def detect(cls, path: str, profile: dict) -> bool:
        try:
            peek = xlsx_fast.peek_rows(path, profile.get("header_search_rows", 10))
        except Exception:
            return False
        inst = cls(profile)
        return any(inst.month_tokens(n) and inst.find_header(rows) for n, rows in peek.items())

    def classify_sheets(self, path: str, year: int, month: int, file_name: str):
        peek = xlsx_fast.peek_rows(path, self.p.get("header_search_rows", 10))
        derived_rx = [re.compile(p, re.I) for p in self.p.get("derived_sheet_patterns", [])]
        sheets, plan_candidates, blocking = [], [], []
        for name, rows in peek.items():
            months = self.month_tokens(name)
            header = self.find_header(rows)
            derived = any(rx.search(name.strip().lower()) for rx in derived_rx)
            if month in months and header and not derived:
                role = "target_month_plan"
                plan_candidates.append(name)
            elif month in months:
                role = "target_month_derived_summary" if derived else "target_month_other"
            elif months and header:
                role = "other_month_plan"
            elif months:
                role = "other_month_summary"
            else:
                role = "other"
            sheets.append({"name": name, "months": sorted(months), "plan_header_row": header, "role": role})
        key = f"{year:04d}-{month:02d}"
        chosen = None
        if self.sheet_override.get(key):
            chosen = self.sheet_override[key]
            if chosen not in peek:
                blocking.append(f"sheet_override for {key} names '{chosen}', which does not exist")
                chosen = None
        elif len(plan_candidates) == 1:
            chosen = plan_candidates[0]
        elif len(plan_candidates) > 1:
            blocking.append(f"several worksheets look like the {MONTH_RU[month]} plan: {plan_candidates}; "
                            f"set sheet_override {{'{key}': '<sheet>'}} in the mapping to choose one")
        else:
            blocking.append(f"no worksheet with the plan headers found for {MONTH_RU[month]} {year}")
        years = {int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", file_name)}
        if years and year not in years:
            blocking.append(f"file name refers to year(s) {sorted(years)} but the target month is {key}; "
                            "sheet names carry no year - verify the target month")
        for s in sheets:
            if s["name"] == chosen:
                s["role"] = "SOURCE (imported)"
        return sheets, chosen, blocking

    # ------------------------------------------------------------------ #
    # rows
    # ------------------------------------------------------------------ #
    def column_map(self, header: dict[str, Any]):
        roles: dict[str, str] = {}
        days: dict[int, str] = {}
        unmapped: dict[str, str] = {}
        by_norm = {}
        for col, val in header.items():
            if val is not None:
                by_norm.setdefault(norm_header(str(val)), col)
        for role, names in self.p["columns"].items():
            for n in names:
                col = by_norm.get(norm_header(n))
                if col and col not in roles.values():
                    roles[role] = col
                    break
        for col, val in header.items():
            if col in roles.values():
                continue
            n = _num(val)
            if self.p.get("day_columns") and n is not None and n.is_integer() and 1 <= n <= 31:
                days[int(n)] = col
            elif clean_text(val):
                unmapped[clean_text(val)] = col
        return roles, days, unmapped

    def read_tasks(self, sheet: xlsx_fast.SheetData, header_row: int):
        roles, days, unmapped = self.column_map(sheet.rows.get(header_row, {}))
        markers = {norm_header(k): v for k, v in self.p["section_markers"].items()}
        end_prefixes = tuple(norm_header(p) for p in self.p["end_of_tasks_prefixes"])
        tasks: list[TaskRow] = []
        ledger: dict[int, dict] = {header_row: {"kind": "header"}}
        section, end_row = "main", None
        sections: dict[str, list[int]] = {"main": []}
        for r in sorted(sheet.rows):
            if r < header_row:
                ledger[r] = {"kind": "above_header"}
                continue
            if r == header_row:
                continue
            raw = sheet.rows[r]
            if end_row is None and any(isinstance(v, str) and norm_header(v).startswith(end_prefixes)
                                       for v in raw.values()):
                end_row = r
            if end_row is not None:
                continue
            v = {role: raw.get(col) for role, col in roles.items()}
            eng = norm_header(str(v.get("engineer") or ""))
            meaningful = [k for k in ("manufacturer", "vehicle_model", "product_type", "task_type",
                                      "project_stage", "comment", "task_group") if clean_text(v.get(k))]
            if eng in markers and not meaningful:
                section = markers[eng]
                sections.setdefault(section, [])
                ledger[r] = {"kind": "section_marker", "section": section, "value": clean_text(v.get("engineer"))}
                continue
            dh = {d: _num(raw.get(c)) for d, c in days.items() if _num(raw.get(c))}
            if not meaningful and not (_num(v.get("actual_hours")) or _num(v.get("planned_hours")) or dh):
                ledger[r] = {"kind": "empty_placeholder", "section": section,
                             "values": {k: clean_text(x) for k, x in v.items() if clean_text(x)}}
                continue
            um = {h: raw.get(c) for h, c in unmapped.items() if raw.get(c) is not None}
            tasks.append(TaskRow(r, section, r in sheet.hidden_rows, v, dh, um))
            sections.setdefault(section, []).append(r)
        head = sheet.rows.get(header_row, {})
        layout = {"header_row": header_row, "columns": roles, "day_columns": len(days),
                  "column_headers": {r: clean_text(head.get(c)) for r, c in roles.items()},
                  "unmapped_headers": unmapped, "end_of_tasks_row": end_row,
                  "section_rows": {k: (min(v), max(v), len(v)) if v else None for k, v in sections.items()}}
        vocab, vocab_start = self.read_vocabulary(sheet, roles, end_row)
        totals = self.read_sheet_totals(sheet, roles, end_row)
        if end_row:
            for r in sorted(sheet.rows):
                if r < end_row:
                    continue
                vals = sheet.rows[r].values()
                if vocab_start and r >= vocab_start:
                    kind = "dropdown_lists"
                elif any(isinstance(x, str) and norm_header(x).startswith(end_prefixes) for x in vals):
                    kind = "totals"
                elif r in totals.get("resource_rows", []):
                    kind = "resource_table"
                else:
                    kind = "summary_area_other"
                ledger[r] = {"kind": kind}
        return tasks, layout, vocab, ledger, totals

    def read_vocabulary(self, sheet, roles, end_row):
        """The dropdown source lists below the tasks (a repeated header row)."""
        if not end_row:
            return {}, None
        eng_col = roles.get("engineer")
        start = next((r for r in sorted(sheet.rows) if r > end_row and eng_col and
                      norm_header(str(sheet.rows[r].get(eng_col, ""))) == norm_header("КО")), None)
        if not start:
            return {}, None
        vocab = {}
        for role in ("engineer", "sales_channel", "product_group", "task_type", "project_stage", "task_group"):
            col = roles.get(role)
            if col:
                vocab[role] = [clean_text(sheet.rows[r][col]) for r in sorted(sheet.rows)
                               if r > start and col in sheet.rows[r] and clean_text(sheet.rows[r][col])]
        return vocab, start

    def read_sheet_totals(self, sheet, roles, end_row) -> dict:
        """The sheet's own per-engineer totals, used as an independent cross-check."""
        out: dict = {"daily_fact": {}, "resource_table": {}, "resource_rows": []}
        if not end_row:
            return out
        ci = column_index_from_string
        fact_col = roles.get("actual_hours")
        for r in sorted(sheet.rows):
            if r <= end_row:
                continue
            for v in sheet.rows[r].values():
                if isinstance(v, str) and norm_header(v).startswith("итого факт в день"):
                    name = clean_text(v.strip()[len("Итого факт в день"):])
                    if name and fact_col:
                        out["daily_fact"][name] = _num(sheet.rows[r].get(fact_col))
        for r in sorted(sheet.rows):                       # resource table: "Сотрудник" + "Ресурс" header
            if r <= end_row:
                continue
            row = sheet.rows[r]
            names = {norm_header(str(v)): c for c, v in row.items() if isinstance(v, str)}
            if "ресурс" not in names or "сотрудник" not in names:
                continue
            res_col = names["ресурс"]
            emp_cols = [c for c, v in row.items() if norm_header(str(v)) == "сотрудник" and ci(c) < ci(res_col)]
            emp_col = max(emp_cols, key=ci) if emp_cols else None
            right = {norm_header(str(v)): c for c, v in row.items() if ci(c) > ci(res_col)}
            out["resource_rows"].append(r)
            for rr in sorted(sheet.rows):
                if rr <= r:
                    continue
                nm = clean_text(sheet.rows[rr].get(emp_col)) if emp_col else None
                if not nm:
                    if out["resource_table"]:
                        break
                    continue
                out["resource_rows"].append(rr)
                out["resource_table"][nm] = {"resource": _num(sheet.rows[rr].get(res_col)),
                                             "plan": _num(sheet.rows[rr].get(right.get("план"))),
                                             "fact": _num(sheet.rows[rr].get(right.get("факт")))}
            break
        return out

    # ------------------------------------------------------------------ #
    # mapping
    # ------------------------------------------------------------------ #
    def map_activity(self, t: TaskRow) -> tuple[Optional[str], Optional[str]]:
        vals = {"task_type": norm_header(t.t("task_type") or ""),
                "project_stage": norm_header(t.t("project_stage") or ""),
                "task_group": norm_header(t.t("task_group") or "")}
        for cond, rule in self._norm_rules:
            if all(vals.get(k) in allowed for k, allowed in cond.items()):
                return rule.get("type"), rule.get("why")
        return None, None

    def progress_of(self, t: TaskRow) -> tuple[Optional[str], Optional[str]]:
        actual = _num(t.v.get("actual_hours")) or 0
        planned = _num(t.v.get("planned_hours")) or 0
        checks = {"actual_hours_positive": actual > 0, "planned_hours_positive": planned > 0}
        for rule in self.status_rules:
            if checks.get(rule["if"]):
                return rule["status"], rule["if"]
        return None, None

    def assignee(self, t: TaskRow) -> tuple[Optional[str], Optional[str]]:
        """(person, original value when it is not a verified person)."""
        e = t.t("engineer")
        if e is None:
            return None, None
        if norm_header(e) in self.unknown_engineers:
            return None, e
        return e, None

    # ------------------------------------------------------------------ #
    def build(self, path: str, file_name: str, target: Optional[str] = None,
              today: Optional[date] = None) -> ProfileResult:
        year, month = resolve_month(target, self.p, today)
        month_key = f"{year:04d}-{month:02d}"
        sheets, chosen, blocking = self.classify_sheets(path, year, month, file_name)
        analysis: dict = {"profile": self.name, "target_month": month_key, "sheets": sheets, "source_sheet": chosen}
        if not chosen:
            fp = hashlib.sha256(f"{self.name}|no-sheet".encode()).hexdigest()[:16]
            return ProfileResult([], self._proposal(file_name, analysis, {}, fp, {}), fp, analysis, blocking)

        sheet = xlsx_fast.read_sheet(path, chosen)
        header_row = next(s["plan_header_row"] for s in sheets if s["name"] == chosen)
        tasks, layout, vocab, ledger, totals = self.read_tasks(sheet, header_row)
        analysis.update({"layout": layout, "department_vocabulary": vocab, "sheet_totals": totals,
                         "autofilter": sheet.autofilter, "hidden_rows_total": len(sheet.hidden_rows),
                         "merged_ranges": sheet.merged, "task_rows": len(tasks),
                         "hidden_task_rows": sum(t.hidden for t in tasks),
                         "nonempty_rows": sorted(sheet.rows), "ledger": ledger})
        notes = []
        missing_cols = [r for r in self.p["columns"] if r not in layout["columns"]]
        if missing_cols:
            notes.append(f"columns not present in '{chosen}': {missing_cols}")
        if sheet.autofilter.get("filters"):
            notes.append(f"worksheet has an active AutoFilter {sheet.autofilter['filters']} hiding "
                         f"{analysis['hidden_task_rows']} task rows - all rows are processed, the filter is ignored")

        records = self._records(tasks, chosen, file_name, month_key)
        combos, combo_type = Counter(), {}
        for r in records:
            for a in r.activities:
                d = a.details
                key = " | ".join([d.get("task_type") or "—", d.get("project_stage") or "—", d.get("task_group") or "—"])
                combos[key] += 1
                combo_type[key] = d.get("activity_type")
        value_maps = {
            "activity_combinations": {k: {"type": combo_type[k], "rows": n} for k, n in combos.most_common()},
            "priority": {raw: (None if raw in self.undefined_priorities else self.priority_map.get(raw))
                         for raw in sorted({_fmt_num(t.v.get("priority")) for t in tasks
                                            if t.v.get("priority") is not None})},
            "task_type": sorted({t.t("task_type") for t in tasks if t.t("task_type")}),
            "project_stage": sorted({t.t("project_stage") for t in tasks if t.t("project_stage")}),
            "task_group": sorted({t.t("task_group") for t in tasks if t.t("task_group")}),
            "engineer": sorted({t.t("engineer") for t in tasks if t.t("engineer")}),
        }
        sig = {"profile": self.name, "version": self.p.get("version"),
               "columns": {r: norm_header(str(sheet.rows[header_row].get(c))) for r, c in layout["columns"].items()},
               "unmapped": sorted(norm_header(h) for h in layout["unmapped_headers"]),
               "sections": sorted(self.p["section_markers"])}
        fp = hashlib.sha256(json.dumps(sig, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        new_values = {}
        if self.approved and self.approved.get("fingerprint") == fp:
            new_values = self._flag_unapproved(records, value_maps)
        proposal = self._proposal(file_name, analysis, value_maps, fp, layout)
        return ProfileResult(records, proposal, fp, analysis, blocking, notes, new_values)

    # ------------------------------------------------------------------ #
    def _records(self, tasks: list[TaskRow], sheet: str, file_name: str, month_key: str) -> list[ProjectRecord]:
        closing = {norm_header(g) for g in self.p.get("cycle_closing_task_groups", [])}
        records: list[ProjectRecord] = []
        by_section: dict[str, list[TaskRow]] = {}
        for t in tasks:
            by_section.setdefault(t.section, []).append(t)

        # ---- main projects: contiguous task cycles, grouped by vehicle+product
        cycles: list[list[TaskRow]] = []
        for t in by_section.get("main", []):
            k = self._key(t)
            prev = cycles[-1][-1] if cycles else None
            if prev is not None and self._key(prev) == k and norm_header(prev.t("task_group") or "") not in closing:
                cycles[-1].append(t)
            else:
                cycles.append([t])
        grouped: "OrderedDict[tuple, list[list[TaskRow]]]" = OrderedDict()
        occ: Counter = Counter()
        for cyc in cycles:
            k = self._key(cyc[0])
            if not k[0] and not k[1]:                     # no vehicle: never merged
                if self._operational(cyc) and self.operational_category == "additional_work":
                    records.append(self._operational_record(cyc, sheet, file_name, month_key, occ))
                    continue
                rec = self._main_record([cyc], sheet, file_name, month_key)
                if self._operational(cyc):
                    rec.add_issue(Severity.REVIEW, "operational_work",
                                  "planned work without a vehicle that looks operational (not a vehicle project); "
                                  "decide its category")
                else:
                    rec.add_issue(Severity.REVIEW, "no_vehicle",
                                  "task rows have no manufacturer/model - project cannot be identified reliably")
                records.append(rec)
                continue
            grouped.setdefault(k, []).append(cyc)
        for cycs in grouped.values():
            records.append(self._main_record(cycs, sheet, file_name, month_key))

        for t in by_section.get("extra_work", []):        # additional work: one record per assignment
            records.append(self._single_record(t, "additional_work", sheet, file_name, month_key, occ))
        for t in by_section.get("backlog", []):           # backlog: one record per item
            records.append(self._single_record(t, "backlog", sheet, file_name, month_key, occ))
        for s in [s for s in by_section if s not in ("main", "extra_work", "backlog")]:
            for t in by_section[s]:                       # never drop rows of an unexpected section
                rec = self._single_record(t, "additional_work", sheet, file_name, month_key, occ)
                rec.add_issue(Severity.REVIEW, "unknown_section", f"row belongs to unknown section '{s}'")
                records.append(rec)
        records.sort(key=lambda r: r.source_row or 0)
        self._flag_related(records)
        return records

    def _key(self, t: TaskRow) -> tuple[str, str, str]:
        return (norm_key(t.v.get("manufacturer")), norm_key(t.v.get("vehicle_model")),
                norm_key(t.v.get("product_type")))

    def _operational(self, rows: list[TaskRow]) -> bool:
        groups = {norm_header(g) for g in self.p["operational_product_groups"]}
        stages = {norm_header(s) for s in self.p["operational_stages"]}
        return all(norm_header(t.t("product_group") or "") in groups or
                   norm_header(t.t("project_stage") or "") in stages for t in rows)

    # ------------------------------------------------------------------ #
    def _activity(self, t: TaskRow, rec: ProjectRecord, month_key: str, seq: int, cycle: int,
                  key_counts: Counter) -> Activity:
        atype, why = self.map_activity(t)
        progress, prog_rule = self.progress_of(t)
        person, original = self.assignee(t)
        planned = _num(t.v.get("planned_hours"))
        actual = _num(t.v.get("actual_hours"))
        day_sum = round(sum(t.days.values()), 4) if t.days else 0
        actual_source = "Факт месяц"
        if day_sum and not actual:
            # The sheet's own per-engineer totals are built from the day columns;
            # use them when "Факт месяц" is empty, and say so.
            actual, actual_source = day_sum, "day columns (Факт месяц empty)"
            rec.add_issue(Severity.WARNING, "fact_from_day_columns",
                          f"row {t.row}: 'Факт месяц' is empty but the day columns hold {day_sum:g} h; "
                          "actual hours taken from the day columns")
        elif day_sum and actual and abs(day_sum - actual) > 0.01:
            rec.add_issue(Severity.WARNING, "fact_mismatch",
                          f"row {t.row}: 'Факт месяц' = {actual:g} h but the day columns sum to {day_sum:g} h; "
                          "'Факт месяц' kept, both preserved")
        task_norm = norm_key(t.t("task_type") or t.t("project_stage") or "task")[:24] or "task"
        base = f"{month_key}#c{cycle}#{task_norm}"
        key_counts[base] += 1
        y, m = map(int, month_key.split("-"))
        extra = {k: (_fmt_num(t.v.get(r)) if isinstance(t.v.get(r), (int, float)) else t.t(r))
                 for k, r in (("order_note", "order_note"), ("delivery_chance", "delivery_chance"),
                              ("probability_note", "probability_note"), ("feasibility", "feasibility"),
                              ("article", "article_number"), ("sales_channel", "sales_channel"),
                              ("product_group", "product_group"))
                 if t.v.get(r) is not None and clean_text(t.v.get(r)) is not None}
        if t.unmapped:
            extra["unmapped_columns"] = {k: (v if isinstance(v, (int, float)) else clean_text(v))
                                         for k, v in t.unmapped.items()}
        if t.hidden:
            extra["hidden_by_source_filter"] = True
        extra["actual_hours_source"] = actual_source
        if day_sum:
            extra["day_columns_total"] = day_sum
        d = {
            "activity_key": f"{base}#{key_counts[base]}",
            "plan_month": month_key, "sequence": seq, "cycle": cycle,
            "activity_type": atype, "stage": atype if atype in STAGE_ORDER else None,
            "task_type": t.t("task_type"), "project_stage": t.t("project_stage"), "task_group": t.t("task_group"),
            "assignee": person, "assignee_original": original,
            "planned_hours": planned, "actual_hours": actual,
            "hours_by_date": {date(y, m, dd).isoformat(): h for dd, h in sorted(t.days.items())} or None,
            "progress_status": progress,
            "effort_ratio": round(actual / planned, 3) if planned and actual is not None else None,
            "items_ok": _fmt_num(t.v.get("items_delivered")), "items_nok": _fmt_num(t.v.get("items_outstanding")),
            "result_raw": t.t("result"), "comment": t.t("comment"),
            "mapping_rule": why, "progress_rule": prog_rule, "extra": extra,
        }
        a = Activity(name=t.t("task_type") or t.t("project_stage") or t.t("comment") or "(no task)",
                     stage=d["stage"], stage_mapped=atype is not None, status=progress, engineer=person,
                     comment=t.t("comment"), source_row=t.row, details=d)
        a.status_raw = f"plan {(planned or 0):g} h / fact {(actual or 0):g} h"
        if original:
            rec.add_issue(Severity.REVIEW, "engineer_unknown_value",
                          f"row {t.row}: engineer value '{original}' has no verified meaning; original kept, "
                          "not used as a person")
        return a

    @staticmethod
    def activity_payload(a: Activity, rec: ProjectRecord) -> dict:
        d = a.details
        details = {k: v for k, v in {**d.get("extra", {}), "mapping_rule": d.get("mapping_rule"),
                                     "progress_rule": d.get("progress_rule")}.items() if v is not None}
        return {
            "activity_key": d["activity_key"], "plan_month": d["plan_month"], "sequence": d["sequence"],
            "cycle": d["cycle"], "name": a.name, "activity_type": d["activity_type"], "stage": d["stage"],
            "task_type": d["task_type"], "project_stage_original": d["project_stage"],
            "task_group": d["task_group"], "assignee": d["assignee"], "assignee_original": d["assignee_original"],
            "planned_hours": d["planned_hours"], "actual_hours": d["actual_hours"],
            "hours_by_date": d["hours_by_date"], "progress_status": d["progress_status"],
            "effort_ratio": d["effort_ratio"], "items_ok": d["items_ok"], "items_nok": d["items_nok"],
            "result_raw": d["result_raw"], "comment": d["comment"], "details": details or None,
            "source_file": rec.source_file, "source_sheet": rec.source_sheet, "source_row": a.source_row,
        }

    def _finish(self, rec: ProjectRecord, month_key: str) -> None:
        rec.last_plan_month = month_key
        rec.activity_month = month_key
        rec.activity_payload = [self.activity_payload(a, rec) for a in rec.activities]
        ex = rec.extra_attributes
        ex["plan_month"] = month_key
        ex["plan_sheet"] = rec.source_sheet
        ex["planned_hours"] = sum(a.details["planned_hours"] or 0 for a in rec.activities)
        ex["actual_hours"] = sum(a.details["actual_hours"] or 0 for a in rec.activities)
        if rec.record_category == "main_project":
            for a in rec.activities:
                if a.details["activity_type"] is None:
                    rec.add_issue(Severity.REVIEW, "unmapped_activity",
                                  f"row {a.source_row}: task '{a.details['task_type']}' / stage "
                                  f"'{a.details['project_stage']}' / group '{a.details['task_group']}' matches no "
                                  "activity rule; original values kept")

    def _priority(self, rec: ProjectRecord, rows: list[TaskRow]) -> None:
        prios = list(dict.fromkeys(_fmt_num(t.v.get("priority")) for t in rows if t.v.get("priority") is not None))
        if not prios:
            return
        rec.extra_attributes["priority_original"] = prios[0] if len(prios) == 1 else prios
        if len(prios) > 1:
            rec.add_issue(Severity.REVIEW, "priority_conflict", f"task rows carry different priorities {prios}",
                          "priority")
            return
        raw = prios[0]
        if raw in self.undefined_priorities:
            rec.add_issue(Severity.WARNING, "priority_undefined",
                          f"priority '{raw}' has no verified meaning; dashboard priority left empty", "priority")
            return
        rec.priority = self.priority_map.get(raw)
        rec.extra_attributes["priority_normalized"] = rec.priority
        if rec.priority is None:
            rec.add_issue(Severity.WARNING, "unmapped_priority", f"priority '{raw}' is not in the priority scale",
                          "priority")

    def _main_record(self, cycles: list[list[TaskRow]], sheet: str, file_name: str, month_key: str) -> ProjectRecord:
        rows = [t for c in cycles for t in c]
        first = rows[0]
        rec = ProjectRecord(source_file=file_name, source_sheet=sheet, source_row=first.row,
                            source_rows=[t.row for t in rows], record_category="main_project")
        rec.manufacturer, rec.vehicle_model, rec.product_type = (first.t("manufacturer"), first.t("vehicle_model"),
                                                                 first.t("product_type"))
        ex = rec.extra_attributes
        spellings = {f: sorted({t.t(f) for t in rows if t.t(f)}) for f in ("manufacturer", "vehicle_model",
                                                                          "product_type")}
        variants = {f: v for f, v in spellings.items() if len(v) > 1}
        if variants:
            ex["spelling_variants"] = variants
        key_counts: Counter = Counter()
        seq = 0
        for ci, cyc in enumerate(cycles, start=1):
            for t in cyc:
                seq += 1
                rec.activities.append(self._activity(t, rec, month_key, seq, ci, key_counts))
        if len(cycles) > 1:
            ex["cycles"] = [[t.row for t in c] for c in cycles]
            rec.add_issue(Severity.INFO, "multiple_cycles",
                          f"{len(cycles)} engineering cycles this month (rows {ex['cycles']}); kept as one project "
                          "with numbered activities")
        if (rec.manufacturer or rec.vehicle_model) and not (rec.manufacturer and rec.vehicle_model
                                                            and rec.product_type):
            rec.add_issue(Severity.REVIEW, "incomplete_identity",
                          f"identity incomplete (manufacturer={rec.manufacturer!r}, model={rec.vehicle_model!r}, "
                          f"product={rec.product_type!r})")
        for t in rows:
            full = t.t("vehicle_full")
            if full and rec.manufacturer and rec.vehicle_model and \
                    norm_key(full) != norm_key(f"{t.t('manufacturer')}{t.t('vehicle_model')}"):
                rec.add_issue(Severity.WARNING, "vehicle_full_mismatch",
                              f"row {t.row}: 'Марка+Модель' = '{full}' does not equal Марка + Модель")
                break
        rx = re.compile(self.p["article_pattern"])
        bad = [norm_header(m) for m in self.p["article_invalid_markers"]]
        raw_articles = list(dict.fromkeys(t.t("article_number") for t in rows if t.t("article_number")))
        valid = [a for a in raw_articles if rx.match(a) and not any(m in norm_header(a) for m in bad)]
        invalid = [a for a in raw_articles if a not in valid]
        if len(valid) == 1:
            rec.article_number = valid[0]
        elif len(valid) > 1:
            rec.add_issue(Severity.REVIEW, "multiple_articles", f"several article numbers: {valid}")
            ex["articles"] = valid
        if invalid:
            ex["article_original"] = invalid
            rec.add_issue(Severity.WARNING, "invalid_article",
                          f"'Артикул' value(s) {invalid} are not a single article number; kept as original text")
        self._priority(rec, rows)
        # responsible DESIGN engineer; scanning/fitment people stay activity-level assignees
        designers = list(dict.fromkeys(a.details["assignee"] for a in rec.activities
                                       if a.details["assignee"] and a.details["activity_type"] in self.design_types))
        assignees = list(dict.fromkeys(a.details["assignee"] for a in rec.activities if a.details["assignee"]))
        if len(designers) == 1:
            rec.assigned_engineer = designers[0]
        elif len(designers) > 1:
            rec.add_issue(Severity.WARNING, "multiple_designers",
                          f"design activities by several engineers {designers}; project-level engineer left empty",
                          "assigned_engineer")
        else:
            rec.add_issue(Severity.INFO, "no_design_engineer",
                          "no responsible design engineer in the file" +
                          (f"; activity assignees: {assignees}" if assignees else ""), "assigned_engineer")
        if assignees:
            ex["activity_assignees"] = assignees
        for role in ("sales_channel", "product_group"):
            vals = list(dict.fromkeys(t.t(role) for t in rows if t.t(role)))
            if vals:
                ex[role] = vals[0] if len(vals) == 1 else vals
        comments = list(dict.fromkeys(t.t("comment") for t in rows if t.t("comment")))
        if comments:
            ex["plan_comment"] = " | ".join(comments)
        ex["project_stage_original"] = list(dict.fromkeys(t.t("project_stage") for t in rows if t.t("project_stage")))
        self._finish(rec, month_key)
        self._derive(rec)
        if rec.manufacturer and rec.vehicle_model and rec.product_type and not any(
                i.code in IDENTITY_REVIEW_CODES for i in rec.issues):
            rec.external_project_id = f"{self.id_prefixes['main_project']}-" + _hash(
                "main", rec.manufacturer, rec.vehicle_model, rec.product_type)
            ex["id_source"] = "derived from manufacturer + model + product type"
        parts = [p for p in (rec.manufacturer, rec.vehicle_model, rec.product_type) if p]
        rec.project_name = " ".join(parts) if parts else None
        ex.setdefault("derived_fields", []).append("project_name")
        return rec

    def _single_record(self, t: TaskRow, category: str, sheet: str, file_name: str, month_key: str,
                       occ: Counter) -> ProjectRecord:
        rec = ProjectRecord(source_file=file_name, source_sheet=sheet, source_row=t.row, source_rows=[t.row],
                            record_category=category)
        rec.manufacturer, rec.vehicle_model, rec.product_type = (t.t("manufacturer"), t.t("vehicle_model"),
                                                                 t.t("product_type"))
        rec.activities.append(self._activity(t, rec, month_key, 1, 1, Counter()))
        ex = rec.extra_attributes
        for role in ("sales_channel", "product_group", "order_note"):
            if t.t(role):
                ex[role] = t.t(role)
        self._priority(rec, [t])
        person, _ = self.assignee(t)
        vehicle = " ".join(p for p in (rec.manufacturer, rec.vehicle_model) if p)
        if category == "additional_work":
            rec.assigned_engineer = person          # the person named on the assignment does it
            title = t.t("comment") or t.t("product_type") or vehicle or t.t("task_type")
            rec.project_name = title
            ident = (month_key, t.t("engineer"), t.t("comment"), vehicle, t.t("product_type"), t.t("task_type"))
            occ[("aw",) + ident] += 1
            rec.external_project_id = f"{self.id_prefixes['additional_work']}-" + _hash(
                "aw", *ident, str(occ[("aw",) + ident]))
            ex["id_source"] = "derived from month + engineer + description + vehicle + product + occurrence"
            if not title:
                rec.add_issue(Severity.REVIEW, "no_description", "additional work row without any description")
            self._finish(rec, month_key)
            self._derive(rec)
            return rec
        # backlog: future / pending work; scheduling information preserved verbatim
        if t.t("comment"):
            ex["scheduling_note"] = t.t("comment")
        if t.t("feasibility"):
            ex["feasibility"] = t.t("feasibility")
        if person and rec.activities[0].details["activity_type"] in self.design_types:
            rec.assigned_engineer = person
        rec.project_name = " ".join(p for p in (rec.manufacturer, rec.vehicle_model, rec.product_type) if p) \
            or t.t("comment") or t.t("task_type")
        if rec.manufacturer and rec.vehicle_model and rec.product_type:
            ident = (rec.manufacturer, rec.vehicle_model, rec.product_type, t.t("task_type"))
            occ[("bl",) + ident] += 1
            rec.external_project_id = f"{self.id_prefixes['backlog']}-" + _hash(
                "bl", *ident, str(occ[("bl",) + ident]))
            ex["id_source"] = "derived from manufacturer + model + product + task + occurrence"
        else:
            rec.add_issue(Severity.REVIEW, "incomplete_identity",
                          "backlog item without manufacturer/model/product cannot be identified reliably")
        ex["backlog_note"] = "future or pending work; not an active project and never marked complete"
        self._finish(rec, month_key)
        return rec

    def _operational_record(self, rows: list[TaskRow], sheet: str, file_name: str, month_key: str,
                            occ: Counter) -> ProjectRecord:
        """Planned operational work of the main section (no vehicle) -> Additional Work."""
        first = rows[0]
        rec = ProjectRecord(source_file=file_name, source_sheet=sheet, source_row=first.row,
                            source_rows=[t.row for t in rows], record_category="additional_work")
        rec.manufacturer, rec.vehicle_model, rec.product_type = None, None, first.t("product_type")
        key_counts: Counter = Counter()
        for seq, t in enumerate(rows, start=1):
            rec.activities.append(self._activity(t, rec, month_key, seq, 1, key_counts))
        ex = rec.extra_attributes
        ex["source_section"] = "main plan section (planned operational work, moved to Additional Work)"
        for role in ("sales_channel", "product_group"):
            vals = list(dict.fromkeys(t.t(role) for t in rows if t.t(role)))
            if vals:
                ex[role] = vals[0] if len(vals) == 1 else vals
        comments = list(dict.fromkeys(t.t("comment") for t in rows if t.t("comment")))
        if comments:
            ex["plan_comment"] = " | ".join(comments)
        self._priority(rec, rows)
        people = list(dict.fromkeys(a.details["assignee"] for a in rec.activities if a.details["assignee"]))
        if len(people) == 1:
            rec.assigned_engineer = people[0]
        elif len(people) > 1:
            rec.add_issue(Severity.WARNING, "multiple_assignees",
                          f"operational work done by several people {people}; record-level engineer left empty")
        rec.project_name = first.t("product_type") or first.t("comment") or first.t("project_stage")
        ident = (month_key, "|".join(people), rec.project_name,
                 "|".join(t.t("task_type") or "" for t in rows))
        occ[("op",) + ident] += 1
        rec.external_project_id = f"{self.id_prefixes['additional_work']}-" + _hash(
            "op", *ident, str(occ[("op",) + ident]))
        ex["id_source"] = "derived from month + assignee + description + tasks + occurrence"
        self._finish(rec, month_key)
        self._derive(rec)
        return rec

    def _derive(self, rec: ProjectRecord) -> None:
        derived = rec.extra_attributes.setdefault("derived_fields", [])
        main = [a for a in rec.activities if a.details["stage"] in STAGE_ORDER]
        ordered = sorted(main, key=lambda a: (STAGE_ORDER.index(a.details["stage"]), a.details["sequence"]))
        active = [a for a in ordered if a.details["progress_status"] == "in_progress"]
        if rec.record_category == "main_project" and ordered:
            rec.current_stage = (active[-1] if active else ordered[0]).details["stage"]
            derived.append("current_stage")
        if any(a.details["progress_status"] == "in_progress" for a in rec.activities):
            rec.project_status = "in_progress"           # effort was logged; never "completed" automatically
            derived.append("project_status")

    def _flag_related(self, records: list[ProjectRecord]) -> None:
        """Related records are reported, never merged: kits and single products
        of the same vehicle, and backlog items of an active main project."""
        main_by_key = {}
        by_vehicle: dict[tuple, list[ProjectRecord]] = {}
        for r in records:
            if r.record_category != "main_project" or not (r.manufacturer and r.vehicle_model):
                continue
            main_by_key[(norm_key(r.manufacturer), norm_key(r.vehicle_model), norm_key(r.product_type))] = r
            group = norm_key(r.extra_attributes.get("product_group") or "")
            by_vehicle.setdefault((norm_key(r.manufacturer), norm_key(r.vehicle_model), group), []).append(r)
        for recs in by_vehicle.values():
            if len(recs) < 2:
                continue
            for a in recs:
                kit = "комплект" in norm_header(a.product_type or "")
                if kit or not a.product_type:
                    others = [f"{b.product_type} (row {b.source_row})" for b in recs if b is not a]
                    a.add_issue(Severity.WARNING, "related_projects_same_vehicle",
                                f"{'kit' if kit else 'record without product'} on the same vehicle as {others}; "
                                "kept as separate projects - confirm they are not the same work")
        for r in records:
            if r.record_category == "backlog" and r.manufacturer and r.vehicle_model:
                m = main_by_key.get((norm_key(r.manufacturer), norm_key(r.vehicle_model), norm_key(r.product_type)))
                if m:
                    r.add_issue(Severity.WARNING, "backlog_matches_active_project",
                                f"same vehicle+product is an active main project (row {m.source_row}); "
                                "kept separate in the backlog")
                    r.extra_attributes["related_main_project"] = m.external_project_id

    def _flag_unapproved(self, records, value_maps) -> dict:
        appr = self.approved.get("value_mappings") or {}
        new: dict[str, list[str]] = {}
        for kind in self.p.get("strict_value_kinds", []):
            seen = set(value_maps["priority"]) if kind == "priority" else set(value_maps.get(kind, []))
            known = {norm_header(k) for k in (appr.get("priority", {}) if kind == "priority" else appr.get(kind, []))}
            fresh = sorted(norm_header(x) for x in seen if norm_header(x) not in known)
            if fresh:
                new[kind] = fresh
        known_combos = set(appr.get("activity_combinations", {}))
        fresh_combos = [c for c in value_maps["activity_combinations"] if c not in known_combos]
        if fresh_combos:
            new["activity_combinations"] = fresh_combos
        if not new:
            return {}
        fresh_all = {x for k, v in new.items() if k != "activity_combinations" for x in v}
        for r in records:
            used = set()
            for a in r.activities:
                for k in ("task_type", "project_stage", "task_group"):
                    if a.details.get(k):
                        used.add(norm_header(a.details[k]))
            po = r.extra_attributes.get("priority_original")
            for p in (po if isinstance(po, list) else [po]):
                if p:
                    used.add(norm_header(p))
            combos = {" | ".join([a.details.get("task_type") or "—", a.details.get("project_stage") or "—",
                                  a.details.get("task_group") or "—"]) for a in r.activities}
            hits = sorted(used & fresh_all) + sorted(c for c in combos if c in fresh_combos)
            if hits:
                r.add_issue(Severity.REVIEW, "unapproved_value",
                            f"value(s) {hits} not in the approved mapping; needs validation")
        return new

    # ------------------------------------------------------------------ #
    def _proposal(self, file_name, analysis, value_maps, fp, layout) -> dict:
        return {
            "version": 2,
            "profile": self.name,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "analysed_file": file_name,
            "fingerprint": fp,
            "approved": False,
            "target_month": analysis.get("target_month"),
            "source_sheet": analysis.get("source_sheet"),
            "sheet_classification": [{"name": s["name"], "role": s["role"], "months": s["months"]}
                                     for s in analysis.get("sheets", [])],
            "sheet_override": self.sheet_override,
            "header_row": layout.get("header_row"),
            "columns": {role: {"column": col, "header": (layout.get("column_headers") or {}).get(role)}
                        for role, col in (layout.get("columns") or {}).items()},
            "unmapped_headers": layout.get("unmapped_headers"),
            "section_categories": self.section_categories,
            "operational_category": self.operational_category,
            "id_prefixes": self.id_prefixes,
            "design_activity_types": sorted(self.design_types),
            "unknown_engineer_values": sorted(self.unknown_engineers),
            "priority_map": self.priority_map,
            "undefined_priority_values": sorted(self.undefined_priorities),
            "status_rules": self.status_rules,
            "activity_rules": self.rules,
            "value_mappings": value_maps,
        }
