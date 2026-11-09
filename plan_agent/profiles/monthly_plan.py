"""Profile for the department's monthly work-plan workbook.

One worksheet per month; each row is a task (План на месяц) for a
vehicle (Марка/Модель) and product (Продукт/Задача).  A project is the set of
task rows with the same vehicle + product.  Only the worksheet of the target
month is imported; other worksheets are inspected for classification only.

All interpretation rules come from config/profiles/monthly_work_plan.yaml or,
once approved, from state/mapping.approved.yaml.  Nothing is inferred
silently: every derived field is listed in extra_attributes.derived_fields
and every uncertain decision becomes an issue on the record.
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

from ..models import Activity, ProjectRecord, Severity
from ..normalize import clean_text, norm_header, norm_key
from . import xlsx_fast

STAGE_ORDER = ["scanning", "development", "test_prototype", "measurement", "documentation"]
DESIGN_STAGES = {"development", "test_prototype", "documentation", "extra:series_transfer", "extra:testing",
                 "extra:engineering_change"}
MONTH_RU = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь", 7: "июль", 8: "август",
            9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"}


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
    """Priority-like values: 1 -> '1', 1.1 -> '1.1'."""
    if v is None:
        return None
    n = _num(v)
    if n is None:
        return clean_text(v)
    return str(int(n)) if n.is_integer() else f"{n:g}"


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
        self.rules = src.get("activity_rules") or profile["activity_rules"]
        self.status_rules = src.get("status_rules") or profile["status_rules"]
        self.priority_map = {str(k): v for k, v in (src.get("priority_map") or profile["priority_map"]).items()}
        self.unassigned = {norm_header(x) for x in (src.get("unassigned_engineer_markers")
                                                    or profile["unassigned_engineer_markers"])}
        self.import_sections = src.get("import_sections") or profile["import_sections"]
        self.sheet_override = {**profile.get("sheet_override", {}), **(src.get("sheet_override") or {})}
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

    def classify_sheets(self, path: str, year: int, month: int, file_name: str) -> tuple[list[dict], Optional[str],
                                                                                         list[str]]:
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
    def column_map(self, header: dict[str, Any]) -> tuple[dict[str, str], dict[int, str], dict[str, str]]:
        roles: dict[str, str] = {}
        days: dict[int, str] = {}
        unmapped: dict[str, str] = {}
        by_norm = {}
        for col, val in header.items():
            if val is None:
                continue
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

    def read_tasks(self, sheet: xlsx_fast.SheetData, header_row: int) -> tuple[list[TaskRow], dict, dict]:
        roles, days, unmapped = self.column_map(sheet.rows.get(header_row, {}))
        markers = {norm_header(k): v for k, v in self.p["section_markers"].items()}
        end_prefixes = [norm_header(p) for p in self.p["end_of_tasks_prefixes"]]
        tasks, section, end_row = [], "main", None
        sections: dict[str, list[int]] = {"main": []}
        for r in sorted(sheet.rows):
            if r <= header_row:
                continue
            raw = sheet.rows[r]
            if any(isinstance(v, str) and norm_header(v).startswith(tuple(end_prefixes)) for v in raw.values()):
                end_row = r
                break
            v = {role: raw.get(col) for role, col in roles.items()}
            eng = norm_header(str(v.get("engineer") or ""))
            meaningful = [k for k in ("manufacturer", "vehicle_model", "product_type", "task_type",
                                      "project_stage", "comment", "task_group") if clean_text(v.get(k))]
            if eng in markers and not meaningful:
                section = markers[eng]
                sections.setdefault(section, []).append(r)
                continue
            if not meaningful and not (clean_text(v.get("engineer")) and _num(v.get("actual_hours"))):
                continue
            dh = {d: _num(raw.get(c)) for d, c in days.items() if _num(raw.get(c))}
            um = {h: raw.get(c) for h, c in unmapped.items() if raw.get(c) is not None}
            tasks.append(TaskRow(r, section, r in sheet.hidden_rows, v, dh, um))
            sections.setdefault(section, []).append(r)
        head = sheet.rows.get(header_row, {})
        layout = {"header_row": header_row, "columns": roles, "day_columns": len(days),
                  "column_headers": {r: clean_text(head.get(c)) for r, c in roles.items()},
                  "unmapped_headers": unmapped, "end_of_tasks_row": end_row,
                  "section_rows": {k: (min(v), max(v), len(v)) if v else None for k, v in sections.items()}}
        vocab = self.read_vocabulary(sheet, roles, end_row)
        return tasks, layout, vocab

    def read_vocabulary(self, sheet, roles, end_row) -> dict:
        """The dropdown source lists below the tasks (a repeated header row)."""
        if not end_row:
            return {}
        eng_col = roles.get("engineer")
        start = next((r for r in sorted(sheet.rows) if r > end_row and eng_col and
                      norm_header(str(sheet.rows[r].get(eng_col, ""))) == norm_header("КО")), None)
        if not start:
            return {}
        vocab = {}
        for role in ("engineer", "sales_channel", "product_group", "task_type", "project_stage", "task_group"):
            col = roles.get(role)
            if not col:
                continue
            vals = []
            for r in sorted(sheet.rows):
                if r > start and col in sheet.rows[r] and clean_text(sheet.rows[r][col]):
                    vals.append(clean_text(sheet.rows[r][col]))
            vocab[role] = vals
        return vocab

    # ------------------------------------------------------------------ #
    # mapping
    # ------------------------------------------------------------------ #
    def map_activity(self, t: TaskRow) -> tuple[Optional[str], Optional[str]]:
        vals = {"task_type": norm_header(t.t("task_type") or ""), "project_stage": norm_header(t.t("project_stage") or ""),
                "task_group": norm_header(t.t("task_group") or "")}
        for cond, rule in self._norm_rules:
            if all(vals.get(k) in allowed for k, allowed in cond.items()):
                return rule["stage"], rule.get("why")
        return None, None

    def status_of(self, t: TaskRow) -> tuple[Optional[str], Optional[str]]:
        delivered = _num(t.v.get("items_delivered")) or 0
        actual = _num(t.v.get("actual_hours")) or 0
        planned = _num(t.v.get("planned_hours")) or 0
        checks = {"items_delivered_positive": delivered > 0, "actual_hours_positive": actual > 0,
                  "planned_hours_positive": planned > 0}
        for rule in self.status_rules:
            if checks.get(rule["if"]):
                return rule["status"], rule["if"]
        return None, None

    def engineer(self, t: TaskRow) -> Optional[str]:
        e = t.t("engineer")
        if e is None or norm_header(e) in self.unassigned:
            return None
        return e

    # ------------------------------------------------------------------ #
    def build(self, path: str, file_name: str, target: Optional[str] = None,
              today: Optional[date] = None) -> ProfileResult:
        year, month = resolve_month(target, self.p, today)
        month_key = f"{year:04d}-{month:02d}"
        sheets, chosen, blocking = self.classify_sheets(path, year, month, file_name)
        analysis: dict = {"profile": self.name, "target_month": month_key, "sheets": sheets,
                          "source_sheet": chosen}
        if not chosen:
            fp = hashlib.sha256(f"{self.name}|no-sheet".encode()).hexdigest()[:16]
            return ProfileResult([], self._proposal(file_name, analysis, {}, fp, {}, []), fp, analysis, blocking)

        sheet = xlsx_fast.read_sheet(path, chosen)
        header_row = next(s["plan_header_row"] for s in sheets if s["name"] == chosen)
        tasks, layout, vocab = self.read_tasks(sheet, header_row)
        analysis.update({"layout": layout, "department_vocabulary": vocab,
                         "autofilter": sheet.autofilter, "hidden_rows_total": len(sheet.hidden_rows),
                         "merged_ranges": sheet.merged, "task_rows": len(tasks),
                         "hidden_task_rows": sum(t.hidden for t in tasks)})
        missing_cols = [r for r in self.p["columns"] if r not in layout["columns"]]
        notes = []
        if missing_cols:
            notes.append(f"columns not present in '{chosen}': {missing_cols}")
        if sheet.autofilter.get("filters"):
            notes.append(f"worksheet has an active AutoFilter {sheet.autofilter['filters']} hiding "
                         f"{analysis['hidden_task_rows']} task rows - all rows are processed, the filter is ignored")

        records, excluded = self._projects(tasks, chosen, file_name, month_key)
        combos = Counter()
        combo_stage = {}
        for r in records:
            for a in r.activities:
                d = a.details
                key = " | ".join([d.get("task_type") or "—", d.get("project_stage") or "—", d.get("task_group") or "—"])
                combos[key] += 1
                combo_stage[key] = a.stage
        value_maps = {
            "activity_combinations": {k: {"stage": combo_stage[k], "rows": n} for k, n in combos.most_common()},
            "priority": {raw: self.priority_map.get(raw) for raw in sorted({_fmt_num(t.v.get("priority"))
                                                                          for t in tasks if t.v.get("priority") is not None})},
            "task_type": sorted({t.t("task_type") for t in tasks if t.t("task_type")}),
            "project_stage": sorted({t.t("project_stage") for t in tasks if t.t("project_stage")}),
            "task_group": sorted({t.t("task_group") for t in tasks if t.t("task_group")}),
            "engineer": sorted({t.t("engineer") for t in tasks if t.t("engineer")}),
        }
        sig = {"profile": self.name, "version": self.p.get("version"),
               "columns": {r: norm_header(str(sheet.rows[header_row].get(c))) for r, c in layout["columns"].items()},
               "unmapped": sorted(norm_header(h) for h in layout["unmapped_headers"])}
        fp = hashlib.sha256(json.dumps(sig, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]

        new_values = {}
        if self.approved and self.approved.get("fingerprint") == fp:
            new_values = self._flag_unapproved(records, tasks, value_maps)
        analysis["excluded"] = excluded
        proposal = self._proposal(file_name, analysis, value_maps, fp, layout, excluded)
        return ProfileResult(records, proposal, fp, analysis, blocking, notes, new_values)

    # ------------------------------------------------------------------ #
    def _projects(self, tasks: list[TaskRow], sheet: str, file_name: str, month_key: str
                  ) -> tuple[list[ProjectRecord], dict]:
        excluded: dict[str, list[dict]] = {"extra_work": [], "backlog": [], "operational": [], "other_sections": []}
        main = [t for t in tasks if t.section in self.import_sections]
        for t in tasks:
            if t.section not in self.import_sections:
                bucket = t.section if t.section in excluded else "other_sections"
                excluded[bucket].append(self._brief(t))

        # contiguous task cycles with the same vehicle + product
        closing = {norm_header(g) for g in self.p.get("cycle_closing_task_groups", [])}
        blocks: list[list[TaskRow]] = []
        for t in main:
            k = self._key(t)
            prev = blocks[-1][-1] if blocks else None
            if prev is not None and self._key(prev) == k and norm_header(prev.t("task_group") or "") not in closing:
                blocks[-1].append(t)
            else:
                blocks.append([t])
        grouped: "OrderedDict[tuple, list[list[TaskRow]]]" = OrderedDict()
        records: list[ProjectRecord] = []
        for b in blocks:
            k = self._key(b[0])
            mfr, model, product = k
            if not mfr and not model:
                if self._operational(b):
                    excluded["operational"].append({**self._brief(b[0]), "rows": [t.row for t in b]})
                    continue
                records.append(self._record([b], sheet, file_name, month_key, weak=True))
                continue
            grouped.setdefault(k, []).append(b)
        for k, bs in grouped.items():
            records.append(self._record(bs, sheet, file_name, month_key, weak=False))
        records.sort(key=lambda r: r.source_row or 0)
        self._flag_related(records)
        return records, excluded

    def _key(self, t: TaskRow) -> tuple[str, str, str]:
        return (norm_key(t.v.get("manufacturer")), norm_key(t.v.get("vehicle_model")),
                norm_key(t.v.get("product_type")))

    def _operational(self, block: list[TaskRow]) -> bool:
        groups = {norm_header(g) for g in self.p["operational_product_groups"]}
        stages = {norm_header(s) for s in self.p["operational_stages"]}
        return all(norm_header(t.t("product_group") or "") in groups or
                   norm_header(t.t("project_stage") or "") in stages for t in block)

    @staticmethod
    def _brief(t: TaskRow) -> dict:
        return {"row": t.row, "engineer": t.t("engineer"), "product_group": t.t("product_group"),
                "vehicle": " ".join(x for x in (t.t("manufacturer"), t.t("vehicle_model")) if x) or None,
                "product": t.t("product_type"), "task": t.t("task_type"),
                "comment": t.t("comment"), "actual_hours": _num(t.v.get("actual_hours"))}

    # ------------------------------------------------------------------ #
    def _record(self, blocks: list[list[TaskRow]], sheet: str, file_name: str, month_key: str,
                weak: bool) -> ProjectRecord:
        rows = [t for b in blocks for t in b]
        first = rows[0]
        rec = ProjectRecord(source_file=file_name, source_sheet=sheet, source_row=first.row,
                            source_rows=[t.row for t in rows])
        rec.manufacturer = first.t("manufacturer")
        rec.vehicle_model = first.t("vehicle_model")
        rec.product_type = first.t("product_type")
        ex = rec.extra_attributes
        ex["plan_month"] = month_key
        ex["plan_sheet"] = sheet
        spellings = {f: sorted({t.t(f) for t in rows if t.t(f)}) for f in ("manufacturer", "vehicle_model",
                                                                          "product_type")}
        variants = {f: v for f, v in spellings.items() if len(v) > 1}
        if variants:
            ex["spelling_variants"] = variants
            rec.add_issue(Severity.INFO, "spelling_variants", f"same project written differently: {variants}")

        # activities -------------------------------------------------------
        for seq, t in enumerate(rows, start=1):
            stage, why = self.map_activity(t)
            status, status_reason = self.status_of(t)
            a = Activity(name=t.t("task_type") or t.t("project_stage") or "(no task)", stage=stage,
                         stage_mapped=stage is not None, status=status, engineer=self.engineer(t),
                         comment=t.t("comment"), source_row=t.row)
            a.status_raw = (f"plan {(_num(t.v.get('planned_hours')) or 0):g} h / fact "
                            f"{(_num(t.v.get('actual_hours')) or 0):g} h")
            details = {"sequence": seq, "task_type": t.t("task_type"), "project_stage": t.t("project_stage"),
                       "task_group": t.t("task_group"), "planned_hours": _num(t.v.get("planned_hours")),
                       "actual_hours": _num(t.v.get("actual_hours")), "plan_month": month_key,
                       "status_rule": status_reason, "mapping_rule": why}
            if t.days:
                y, m = map(int, month_key.split("-"))
                details["hours_by_date"] = {date(y, m, d).isoformat(): h for d, h in sorted(t.days.items())}
            for role in ("items_outstanding", "items_delivered", "result", "order_note", "delivery_chance",
                         "probability_note", "feasibility"):
                if t.v.get(role) is not None and clean_text(t.v.get(role)) is not None:
                    details[role] = t.v.get(role) if isinstance(t.v.get(role), (int, float)) else clean_text(t.v.get(role))
            if t.unmapped:
                details["unmapped_columns"] = {k: (v if isinstance(v, (int, float)) else clean_text(v))
                                               for k, v in t.unmapped.items()}
            a.details = {k: v for k, v in details.items() if v is not None}
            if stage is None:
                rec.add_issue(Severity.REVIEW, "unmapped_activity",
                              f"row {t.row}: task '{t.t('task_type')}' / stage '{t.t('project_stage')}' / group "
                              f"'{t.t('task_group')}' matches no activity rule; original values kept")
            if clean_text(t.v.get("engineer")) and norm_header(t.t("engineer")) in self.unassigned:
                rec.add_issue(Severity.WARNING, "engineer_placeholder",
                              f"row {t.row}: engineer value '{t.t('engineer')}' is not a person; kept unknown")
            rec.activities.append(a)

        # identity ----------------------------------------------------------
        if weak:
            rec.add_issue(Severity.REVIEW, "no_vehicle",
                          "task rows have no manufacturer/model - project cannot be identified reliably")
        elif not rec.vehicle_model or not rec.manufacturer or not rec.product_type:
            rec.add_issue(Severity.REVIEW, "incomplete_identity",
                          f"identity incomplete (manufacturer={rec.manufacturer!r}, model={rec.vehicle_model!r}, "
                          f"product={rec.product_type!r})")
        if len(blocks) > 1:
            design = [{a.engineer for a in self._acts_of(rec, b) if a.engineer and a.stage in DESIGN_STAGES}
                      for b in blocks]
            distinct = {frozenset(d) for d in design if d}
            ex["work_cycles"] = [[t.row for t in b] for b in blocks]
            if len(distinct) > 1:
                rec.add_issue(Severity.REVIEW, "possible_separate_projects",
                              f"{len(blocks)} separate task blocks (rows {ex['work_cycles']}) with different "
                              f"designers {[sorted(d) for d in distinct]} - one project or several?")
            else:
                rec.add_issue(Severity.WARNING, "multiple_work_cycles",
                              f"{len(blocks)} task blocks for the same vehicle+product (rows {ex['work_cycles']}); "
                              "merged into one project")
        # vehicle_full cross-check (formula column)
        for t in rows:
            full = t.t("vehicle_full")
            if full and rec.manufacturer and rec.vehicle_model and \
                    norm_key(full) != norm_key(f"{t.t('manufacturer')}{t.t('vehicle_model')}"):
                rec.add_issue(Severity.WARNING, "vehicle_full_mismatch",
                              f"row {t.row}: 'Марка+Модель' = '{full}' does not equal Марка + Модель")
                break

        # article -------------------------------------------------------------
        rx = re.compile(self.p["article_pattern"])
        invalid_markers = [norm_header(m) for m in self.p["article_invalid_markers"]]
        raw_articles = list(dict.fromkeys(clean_text(t.v.get("article_number")) for t in rows
                                          if clean_text(t.v.get("article_number"))))
        valid = [a for a in raw_articles if rx.match(a) and not any(m in norm_header(a) for m in invalid_markers)]
        invalid = [a for a in raw_articles if a not in valid]
        if len(valid) == 1:
            rec.article_number = valid[0]
        elif len(valid) > 1:
            rec.add_issue(Severity.REVIEW, "multiple_articles", f"several article numbers: {valid}")
            ex["articles"] = valid
        if invalid:
            ex["article_original"] = invalid
            rec.add_issue(Severity.WARNING, "invalid_article",
                          f"'Артикул' value(s) {invalid} are not a single article number (placeholder or list); "
                          "kept as original text")

        # priority ----------------------------------------------------------------
        prios = list(dict.fromkeys(_fmt_num(t.v.get("priority")) for t in rows if t.v.get("priority") is not None))
        if len(prios) == 1:
            rec.priority = self.priority_map.get(prios[0])
            ex["priority_original"] = prios[0]
            if rec.priority is None:
                rec.add_issue(Severity.WARNING, "unmapped_priority",
                              f"priority '{prios[0]}' is not in the priority scale; kept unknown", "priority")
        elif len(prios) > 1:
            ex["priority_original"] = prios
            rec.add_issue(Severity.REVIEW, "priority_conflict", f"task rows carry different priorities {prios}",
                          "priority")

        # engineer ----------------------------------------------------------------
        designers = list(dict.fromkeys(a.engineer for a in rec.activities if a.engineer and a.stage in DESIGN_STAGES))
        everyone = list(dict.fromkeys(a.engineer for a in rec.activities if a.engineer))
        if designers:
            rec.assigned_engineer = designers[0]
            if len(designers) > 1:
                rec.add_issue(Severity.INFO, "multiple_designers",
                              f"design tasks by {designers}; first listed is used", "assigned_engineer")
        elif everyone:
            rec.assigned_engineer = everyone[0]
            rec.add_issue(Severity.INFO, "engineer_from_fit_scan",
                          f"only scanning/fitting tasks this month; engineer '{everyone[0]}' taken from them",
                          "assigned_engineer")
        if everyone:
            ex["engineers"] = everyone
        if rec.assigned_engineer and self.p.get("team_separator") and self.p["team_separator"] in rec.assigned_engineer:
            ex["engineer_team"] = [x.strip() for x in rec.assigned_engineer.split(self.p["team_separator"])]

        # other facts ------------------------------------------------------------
        for role, key in (("sales_channel", "sales_channel"), ("product_group", "product_group")):
            vals = list(dict.fromkeys(t.t(role) for t in rows if t.t(role)))
            if vals:
                ex[key] = vals[0] if len(vals) == 1 else vals
        comments = list(dict.fromkeys(t.t("comment") for t in rows if t.t("comment")))
        if comments:
            ex["plan_comment"] = " | ".join(comments)
        ex["planned_hours"] = sum(_num(t.v.get("planned_hours")) or 0 for t in rows)
        ex["actual_hours"] = sum(_num(t.v.get("actual_hours")) or 0 for t in rows)
        ex["project_stage_original"] = list(dict.fromkeys(t.t("project_stage") for t in rows if t.t("project_stage")))
        if any(t.hidden for t in rows):
            ex["hidden_in_source_filter"] = True

        self._derive(rec)
        if not rec.project_name:
            parts = [p for p in (rec.manufacturer, rec.vehicle_model, rec.product_type) if p]
            if parts:
                rec.project_name = " ".join(parts)
                ex.setdefault("derived_fields", []).append("project_name")
        return rec

    @staticmethod
    def _acts_of(rec, block):
        rows = {t.row for t in block}
        return [a for a in rec.activities if a.source_row in rows]

    def _derive(self, rec: ProjectRecord) -> None:
        derived = []
        main = [a for a in rec.activities if a.stage in STAGE_ORDER]
        extra = [a for a in rec.activities if a.stage and a.stage.startswith("extra:")]
        ordered = sorted(main, key=lambda a: (STAGE_ORDER.index(a.stage), a.details.get("sequence", 0)))
        active = [a for a in ordered if a.status == "in_progress"]
        open_ = [a for a in ordered if a.status != "done"]
        if active:
            rec.current_stage = active[-1].stage
        elif open_:
            rec.current_stage = open_[0].stage
        elif ordered:
            rec.current_stage = ordered[-1].stage
        elif extra:
            rec.current_stage = extra[0].stage
        if rec.current_stage:
            derived.append("current_stage")
        mapped = main + extra
        if mapped and all(a.status == "done" for a in mapped):
            rec.project_status = "completed"
            derived.append("project_status")
            rec.add_issue(Severity.REVIEW, "completed_by_ok_column",
                          "every task of the month has 'ОК' > 0 - confirm the project is really complete")
        elif any(a.status in ("done", "in_progress") for a in rec.activities):
            rec.project_status = "in_progress"
            derived.append("project_status")
        rec.extra_attributes["derived_fields"] = derived

    def _flag_related(self, records: list[ProjectRecord]) -> None:
        """Potential duplicates inside the file: within one product group (ТГ),
        a kit ("Комплект защит") or a record without product next to other
        products of the same vehicle."""
        by_vehicle: dict[tuple, list[ProjectRecord]] = {}
        for r in records:
            if r.manufacturer and r.vehicle_model:
                group = norm_key(r.extra_attributes.get("product_group") or "")
                by_vehicle.setdefault((norm_key(r.manufacturer), norm_key(r.vehicle_model), group), []).append(r)
        for recs in by_vehicle.values():
            if len(recs) < 2:
                continue
            for a in recs:
                others = [b for b in recs if b is not a]
                kit = "комплект" in norm_header(a.product_type or "")
                empty = not a.product_type
                if kit or empty:
                    names = [f"{b.product_type} (row {b.source_row})" for b in others]
                    a.add_issue(Severity.WARNING, "related_projects_same_vehicle",
                                f"{'kit' if kit else 'record without product'} on the same vehicle as {names}: "
                                "check that these are not the same project")

    def _flag_unapproved(self, records, tasks, value_maps) -> dict:
        appr = self.approved.get("value_mappings") or {}
        new: dict[str, list[str]] = {}
        known_combo = set(appr.get("activity_combinations", {}))
        for kind in self.p.get("strict_value_kinds", []):
            if kind == "priority":
                seen, known = set(value_maps["priority"]), set(appr.get("priority", {}))
            else:
                seen, known = set(value_maps.get(kind, [])), set(appr.get(kind, []))
            fresh = sorted(norm_header(x) for x in seen if norm_header(x) not in {norm_header(k) for k in known})
            if fresh:
                new[kind] = fresh
        fresh_combos = [c for c in value_maps["activity_combinations"] if c not in known_combo]
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
    def _proposal(self, file_name, analysis, value_maps, fp, layout, excluded) -> dict:
        return {
            "version": 1,
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
            "import_sections": self.import_sections,
            "excluded_counts": {k: len(v) for k, v in (excluded or {}).items()},
            "unassigned_engineer_markers": sorted(self.unassigned),
            "priority_map": self.priority_map,
            "status_rules": self.status_rules,
            "activity_rules": self.rules,
            "value_mappings": value_maps,
        }
