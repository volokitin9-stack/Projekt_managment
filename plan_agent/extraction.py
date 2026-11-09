"""Turn detected table schemas into ProjectRecords."""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from .config import Vocabulary
from .models import Activity, Layout, ProjectRecord, RawTable, Severity, TableSchema
from .normalize import DateParseError, clean_text, norm_header, norm_key, parse_date
from .schema_detection import IDENTITY_ROLES, TOTAL_ROW
from .workflow import DONE, WorkflowMapper

SCALAR_ROLES = {
    "external_id": "external_project_id", "project_name": "project_name", "manufacturer": "manufacturer",
    "vehicle_model": "vehicle_model", "product_type": "product_type", "article_number": "article_number",
    "engineer": "assigned_engineer", "description": "description",
    "planned_start": "planned_start_date", "planned_end": "planned_end_date",
    "actual_completion": "actual_completion_date",
}
DATE_FIELDS = {"planned_start_date", "planned_end_date", "actual_completion_date"}
STAGE_DATE_KINDS = {"planned_start": "planned_start", "planned_end": "planned_end", "actual": "actual_end",
                    "date": "planned_end"}


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


class Extractor:
    def __init__(self, vocab: Vocabulary, mapper: WorkflowMapper, source_file: str):
        self.v = vocab
        self.mapper = mapper
        self.source_file = source_file
        self.row_errors: list[dict] = []     # rows that could not become a record

    # ------------------------------------------------------------------ #
    def split_vehicle(self, text: str) -> tuple[Optional[str], Optional[str]]:
        low = norm_header(text)
        for m in self.v.known_manufacturers:
            if low == m:
                return text, None
            if low.startswith(m + " "):
                return text[:len(m)].strip(), text[len(m):].strip(" /-") or None
        return None, text

    def _date(self, rec: ProjectRecord, value, field_name: str) -> Optional[str]:
        try:
            iso = parse_date(value, allow_serial=False)
        except DateParseError as exc:
            rec.add_issue(Severity.WARNING, "invalid_date", f"{field_name}: {exc}; value kept as unknown",
                          field_name)
            rec.extra_attributes[f"{field_name}_original"] = clean_text(value)
            return None
        if iso and not ("2000-01-01" <= iso <= "2100-12-31"):
            rec.add_issue(Severity.WARNING, "implausible_date", f"{field_name}: {iso} outside 2000-2100; ignored",
                          field_name)
            rec.extra_attributes[f"{field_name}_original"] = iso
            return None
        return iso

    # ------------------------------------------------------------------ #
    def extract_table(self, table: RawTable, schema: TableSchema) -> list[ProjectRecord]:
        role_cols: dict[str, list[int]] = {}
        for c in schema.columns:
            if c.role:
                role_cols.setdefault(c.role, []).append(c.index)
        header_of = {c.index: c.header or f"column_{c.index + 1}" for c in schema.columns}
        unmapped_cols = [c.index for c in schema.columns
                         if not c.role and not (c.evidence and c.evidence[0] == "empty column")]
        stage_cols = [(c.index, c.role.split(":")) for c in schema.columns if c.role and c.role.startswith("stage:")]

        group_rows = set(schema.group_row_indexes)
        group_ctx: dict[int, str] = {}
        levels_used = sorted({table.outline_levels[i] for i in group_rows}) if table.outline_levels else []
        use_outline = len(levels_used) > 1
        depth = len(schema.group_levels_role)

        records: list[ProjectRecord] = []
        by_key: dict[str, ProjectRecord] = {}
        last_identity: dict[str, Any] = {}
        run_start = None

        def val(i: int, role: str):
            cols = role_cols.get(role)
            if not cols:
                return None
            return table.cell(i, cols[0]).value

        for i in range(schema.data_start, len(table.rows)):
            src_row = table.row_numbers[i]
            row = table.rows[i]
            if all(c.is_empty() for c in row):
                continue
            # ---- group rows -> hierarchy context ---------------------------
            if i in group_rows:
                text = next(c.text() for c in row if not c.is_empty())
                if use_outline:
                    level = min(levels_used.index(table.outline_levels[i]), depth - 1)
                else:
                    if run_start is None or (i - 1) not in group_rows:
                        run_start = i
                    run_len = 1
                    while (run_start + run_len) in group_rows:
                        run_len += 1
                    level = depth - run_len + (i - run_start)
                group_ctx[level] = text
                for deeper in [lv for lv in group_ctx if lv > level]:
                    del group_ctx[deeper]
                continue
            first_text = next((c.text() for c in row if not c.is_empty()), "")
            if TOTAL_ROW.match(first_text):
                continue

            rec = ProjectRecord(source_file=self.source_file, source_sheet=table.source, source_row=src_row,
                                source_rows=[src_row])
            # ---- identity & scalar fields ---------------------------------
            for role, fld in SCALAR_ROLES.items():
                raw = val(i, role)
                if raw is None:
                    continue
                rec.raw_values[fld] = _jsonable(raw)
                if fld in DATE_FIELDS:
                    setattr(rec, fld, self._date(rec, raw, fld))
                else:
                    setattr(rec, fld, clean_text(raw))
            if val(i, "vehicle") is not None and clean_text(val(i, "vehicle")):
                vtext = clean_text(val(i, "vehicle"))
                mfr, model = self.split_vehicle(vtext)
                rec.manufacturer = rec.manufacturer or mfr
                rec.vehicle_model = rec.vehicle_model or model
                if mfr is None:
                    rec.add_issue(Severity.INFO, "vehicle_not_split",
                                  f"vehicle '{vtext}': manufacturer not recognised, kept whole as model",
                                  "manufacturer")
            for level, text in group_ctx.items():
                role = schema.group_levels_role[level] if level < depth else f"group_level_{level + 1}"
                fld = SCALAR_ROLES.get(role, role)
                if fld in ("manufacturer", "vehicle_model", "product_type"):
                    if getattr(rec, fld) is None:
                        setattr(rec, fld, text)
                else:
                    rec.extra_attributes.setdefault(fld, text)
            if val(i, "product_category") is not None and clean_text(val(i, "product_category")):
                rec.extra_attributes["product_category"] = clean_text(val(i, "product_category"))

            # ---- priority ----------------------------------------------------
            praw = val(i, "priority")
            if clean_text(praw):
                pr, numeric = self.mapper.map_priority(praw)
                rec.priority = pr
                if pr is None:
                    rec.add_issue(Severity.WARNING, "unmapped_priority",
                                  f"priority '{clean_text(praw)}' not recognised; kept as unknown", "priority")
                    rec.extra_attributes["priority_original"] = clean_text(praw)
                elif numeric:
                    rec.extra_attributes["priority_original"] = clean_text(praw)

            # ---- comments / notes / progress / documentation status ---------
            comments = [clean_text(table.cell(i, j).value) for j in role_cols.get("comment", [])]
            comments = [c for c in comments if c]
            if comments:
                rec.extra_attributes["plan_comment"] = " | ".join(comments)
            for role in ("progress", "documentation_status"):
                if clean_text(val(i, role)):
                    rec.extra_attributes[role] = _jsonable(val(i, role))
            notes = [c.comment for c in row if c.comment]
            if notes:
                rec.extra_attributes["cell_notes"] = notes
            if src_row in table.hidden_rows:
                rec.add_issue(Severity.INFO, "hidden_row", "row is hidden in the source sheet")
            for j in unmapped_cols:
                cell = table.cell(i, j)
                if not cell.is_empty():
                    rec.extra_attributes.setdefault("unmapped_columns", {})[header_of[j]] = _jsonable(cell.value)

            # ---- dependencies (project level) -------------------------------
            if schema.layout != Layout.LONG and clean_text(val(i, "dependency")):
                rec.dependencies = [{"raw": d.strip(), "resolved": None}
                                    for d in str(clean_text(val(i, "dependency"))).replace(";", ",").split(",")
                                    if d.strip()]

            # ---- wide layout: stage columns ---------------------------------
            acts: dict[str, Activity] = {}
            for j, (_, stage, kind) in stage_cols:
                cell = table.cell(i, j)
                if cell.is_empty():
                    continue
                act = acts.setdefault(stage, Activity(name=stage, stage=stage, stage_mapped=True,
                                                      source_row=src_row))
                if kind in STAGE_DATE_KINDS or kind == "value":
                    target = STAGE_DATE_KINDS.get(kind, "planned_end")
                    try:
                        iso = parse_date(cell.value)
                    except DateParseError:
                        iso = None
                    if iso:
                        setattr(act, target, iso)
                        continue
                    if self.mapper.v.statuses.match(cell.text())[0]:
                        kind = "status"          # e.g. "+" or "готово" in a date column
                    elif kind == "value":
                        act.comment = cell.text()
                        continue
                    else:
                        rec.add_issue(Severity.WARNING, "invalid_date",
                                      f"{stage} {kind}: '{cell.text()}' is not a date; kept as unknown "
                                      "(original preserved)")
                        rec.extra_attributes.setdefault("invalid_values", {})[f"{stage}:{kind}"] = cell.text()
                        continue
                if kind == "status":
                    act.status_raw = cell.text()
                    act.status = self.mapper.map_status(cell.value)
                    if act.status is None:
                        rec.add_issue(Severity.REVIEW, "unmapped_status",
                                      f"{stage}: status '{cell.text()}' not recognised; original kept")
                elif kind == "engineer":
                    act.engineer = cell.text()
                elif kind == "comment":
                    act.comment = cell.text()
                elif kind == "dependency":
                    act.depends_on.append(cell.text())
            for act in acts.values():
                if act.actual_end and act.status is None:
                    act.status = DONE
                elif act.actual_end and act.status not in (DONE, None):
                    rec.add_issue(Severity.WARNING, "status_conflict",
                                  f"{act.stage}: actual date {act.actual_end} but status '{act.status_raw}'")
            rec.activities = list(acts.values())

            # ---- long layout: one activity per row --------------------------
            if schema.layout == Layout.LONG:
                act_col = "activity" if "activity" in role_cols else "current_stage"
                status_col = "activity_status" if "activity_status" in role_cols else "project_status"
                araw = val(i, act_col)
                if clean_text(araw):
                    stage, mapped = self.mapper.map_stage(araw)
                    act = Activity(name=clean_text(araw), stage=stage, stage_mapped=mapped, source_row=src_row)
                    if not mapped:
                        rec.add_issue(Severity.REVIEW, "unmapped_activity",
                                      f"activity '{clean_text(araw)}' does not map to a workflow stage; "
                                      "preserved with original name")
                    sraw = val(i, status_col)
                    if clean_text(sraw):
                        act.status_raw = clean_text(sraw)
                        act.status = self.mapper.map_status(sraw)
                        if act.status is None:
                            rec.add_issue(Severity.REVIEW, "unmapped_status",
                                          f"status '{act.status_raw}' not recognised; original kept")
                    act.planned_start = rec.planned_start_date
                    act.planned_end = rec.planned_end_date
                    act.actual_end = rec.actual_completion_date
                    if act.actual_end and act.status is None:
                        act.status = DONE
                    act.engineer = rec.assigned_engineer
                    if clean_text(val(i, "dependency")):
                        act.depends_on = [d.strip() for d in str(clean_text(val(i, "dependency")))
                                          .replace(";", ",").split(",") if d.strip()]
                    if comments:
                        act.comment = " | ".join(comments)
                    rec.activities = [act]
                    # in a long layout these columns describe the activity, not the project
                    rec.planned_start_date = rec.planned_end_date = rec.actual_completion_date = None
                    rec.assigned_engineer = None
                    rec.extra_attributes.pop("plan_comment", None)
                # inherit identity for continuation rows without identity cells
                id_fields = ["external_project_id", "project_name", "manufacturer", "vehicle_model",
                             "product_type", "article_number"]
                if not any(getattr(rec, f) for f in id_fields) and last_identity and rec.activities:
                    for f in id_fields:
                        setattr(rec, f, last_identity.get(f))
                    rec.add_issue(Severity.INFO, "identity_inherited",
                                  "row has no project identity; continues the project above")
            else:
                rec.raw_values["current_stage"] = _jsonable(val(i, "current_stage"))
                rec.raw_values["project_status"] = _jsonable(val(i, "project_status"))

            if not rec.identity_key():
                if any(not c.is_empty() for c in row):
                    self.row_errors.append({"source": table.source, "row": src_row,
                                            "error": "row has data but no project identity "
                                                     "(no id, article, name or manufacturer/model/product)",
                                            "values": [c.text() for c in row if not c.is_empty()][:8]})
                continue
            last_identity = {f: getattr(rec, f) for f in ["external_project_id", "project_name", "manufacturer",
                                                          "vehicle_model", "product_type", "article_number"]}

            key = rec.identity_key()
            if schema.layout == Layout.LONG and key in by_key:
                self._merge_long(by_key[key], rec)
                continue
            if key in by_key:
                rec.add_issue(Severity.REVIEW, "duplicate_in_file",
                              f"same project identity as row {by_key[key].source_row} in '{table.source}'")
                records.append(rec)
                continue
            by_key[key] = rec
            records.append(rec)
        return records

    # ------------------------------------------------------------------ #
    @staticmethod
    def _merge_long(base: ProjectRecord, rec: ProjectRecord) -> None:
        base.source_rows.extend(rec.source_rows)
        base.activities.extend(rec.activities)
        base.issues.extend(rec.issues)
        for f in ("priority", "description", "manufacturer", "vehicle_model", "product_type", "article_number",
                  "external_project_id", "project_name"):
            if getattr(base, f) is None and getattr(rec, f) is not None:
                setattr(base, f, getattr(rec, f))
        for k, v in rec.extra_attributes.items():
            base.extra_attributes.setdefault(k, v)

    def finalize(self, records: list[ProjectRecord]) -> list[ProjectRecord]:
        """Merge the same project across sheets, derive state, build names."""
        merged: dict[str, ProjectRecord] = {}
        out: list[ProjectRecord] = []
        # Identity across sheets: a record keyed by article/composite may match
        # an ext-id record from a master sheet; use every available key.
        index: dict[str, ProjectRecord] = {}
        for rec in records:
            if any(i.code == "duplicate_in_file" for i in rec.issues):
                out.append(rec)
                continue
            keys = self._all_keys(rec)
            target = next((index[k] for k in keys if k in index), None)
            if target is None or target.source_sheet == rec.source_sheet:
                out.append(rec)
                for k in keys:
                    index.setdefault(k, rec)
                continue
            self._merge_cross_sheet(target, rec)
            for k in self._all_keys(target):
                index.setdefault(k, target)

        for rec in out:
            engineers = [a.engineer for a in rec.activities if a.engineer]
            if not rec.assigned_engineer and engineers:
                uniq = list(dict.fromkeys(engineers))
                if len(uniq) == 1:
                    rec.assigned_engineer = uniq[0]
                else:
                    rec.extra_attributes["engineers"] = uniq
                    rec.add_issue(Severity.INFO, "multiple_engineers",
                                  f"activities have different engineers {uniq}; project engineer taken from the "
                                  "current stage", "assigned_engineer")
            self.mapper.derive(rec, rec.raw_values.get("current_stage"), rec.raw_values.get("project_status"))
            if rec.assigned_engineer is None and rec.extra_attributes.get("engineers"):
                cur = [a.engineer for a in rec.activities if a.stage == rec.current_stage and a.engineer]
                rec.assigned_engineer = cur[0] if cur else None
            if not rec.project_name:
                parts = [p for p in (rec.manufacturer, rec.vehicle_model, rec.product_type) if p]
                if parts:
                    rec.project_name = " ".join(parts)
                    rec.extra_attributes.setdefault("derived_fields", []).append("project_name")
        # resolve dependencies against identifiers found in the file
        ids = {norm_key(r.external_project_id): r.external_project_id for r in out if r.external_project_id}
        for rec in out:
            for dep in rec.dependencies:
                dep["resolved"] = ids.get(norm_key(dep["raw"]))
            for a in rec.activities:
                for d in a.depends_on:
                    s, ok = self.mapper.map_stage(d)
                    if not ok and norm_key(d) not in ids:
                        rec.add_issue(Severity.INFO, "unresolved_dependency",
                                      f"activity '{a.name}' depends on '{d}' which is not a known stage or project")
        return out

    @staticmethod
    def _all_keys(rec: ProjectRecord) -> list[str]:
        keys = []
        if rec.external_project_id:
            keys.append("ext:" + norm_key(rec.external_project_id))
        if rec.article_number:
            keys.append("art:" + norm_key(rec.article_number))
        if rec.manufacturer and rec.vehicle_model and rec.product_type:
            keys.append("cmp:" + "|".join(norm_key(x) for x in (rec.manufacturer, rec.vehicle_model,
                                                                 rec.product_type)))
        return keys

    @staticmethod
    def _merge_cross_sheet(base: ProjectRecord, rec: ProjectRecord) -> None:
        base.source_rows.extend(rec.source_rows)
        base.extra_attributes.setdefault("also_in", []).append(f"{rec.source_sheet}!{rec.source_row}")
        for f in ("external_project_id", "project_name", "manufacturer", "vehicle_model", "product_type",
                  "article_number", "assigned_engineer", "priority", "planned_start_date", "planned_end_date",
                  "actual_completion_date", "description"):
            a, b = getattr(base, f), getattr(rec, f)
            if a is None:
                setattr(base, f, b)
            elif b is not None and norm_key(a) != norm_key(b):
                base.add_issue(Severity.REVIEW, "cross_sheet_conflict",
                               f"{f}: '{a}' ({base.source_sheet}) vs '{b}' ({rec.source_sheet})", f)
        have = {a.stage for a in base.activities}
        base.activities.extend(a for a in rec.activities if a.stage not in have or a.stage is None)
        base.issues.extend(rec.issues)
        base.dependencies.extend(rec.dependencies)
        for k, v in rec.extra_attributes.items():
            base.extra_attributes.setdefault(k, v)
        for k in ("current_stage", "project_status"):
            if base.raw_values.get(k) is None:
                base.raw_values[k] = rec.raw_values.get(k)
