"""Automatic structure recognition for planning tables.

For every RawTable this module decides:

* whether the table contains project-planning data at all;
* which row(s) form the header (the first row is *not* assumed to be it;
  two/three-row headers such as "Scanning" over "Plan | Actual" are combined);
* what each column means (role + confidence + evidence), using both header
  text and the actual cell contents;
* which rows are group/section rows (e.g. a bold "HAVAL" row above its models,
  or Excel outline groups) and which hierarchy level they represent;
* whether one row is one project (WIDE) or several rows describe activities of
  the same project (LONG).

Nothing here is final: the result is turned into a *proposed* mapping that a
person approves (see mapping_config.py).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from .config import Vocabulary
from .models import Cell, ColumnMapping, Layout, RawTable, TableSchema
from .normalize import clean_text, looks_like_date, norm_header, norm_key

IDENTITY_ROLES = {"external_id", "project_name", "manufacturer", "vehicle_model", "vehicle",
                  "product_type", "article_number"}
PLANNING_ROLES = {"engineer", "planned_start", "planned_end", "actual_completion", "project_status",
                  "current_stage", "activity", "activity_status", "priority", "progress"}
DATE_ROLES = {"planned_start", "planned_end", "actual_completion"}
# Roles that may legitimately occur in several columns.
MULTI_ROLES = {"comment"}
HIERARCHY_ORDER = ["manufacturer", "vehicle_model", "product_category", "product_type"]
TOTAL_ROW = re.compile(r"^(итого|всего|total|sum|сумма)\b", re.I)

# Mapping from a column role found under a stage header to the stage column kind.
ROLE_TO_STAGE_KIND = {
    "planned_start": "planned_start", "planned_end": "planned_end", "actual_completion": "actual",
    "project_status": "status", "activity_status": "status", "progress": "status",
    "engineer": "engineer", "comment": "comment", "dependency": "dependency",
}


class SchemaDetector:
    def __init__(self, vocab: Vocabulary):
        self.v = vocab

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def match_stage(self, text: str) -> tuple[Optional[str], float]:
        """Return (stage_code or 'extra:<code>', confidence)."""
        for book, prefix in ((self.v.stages, ""), (self.v.additional_stages, "extra:")):
            key, conf, _ = book.match(text)
            if key:
                return prefix + key, conf
        # whole-word match for short synonyms such as "КД" or "скан"
        words = re.findall(r"[\w\d]+", norm_header(text))
        for book, prefix in ((self.v.stages, ""), (self.v.additional_stages, "extra:")):
            for w in words:
                key, conf, _ = book.match(w, allow_partial=False)
                if key:
                    return prefix + key, 0.7
        return None, 0.0

    def _row_header_score(self, row: list[Cell]) -> tuple[int, int]:
        seen, matches, data_like = set(), 0, 0
        for c in row:
            if c.is_empty():
                continue
            key = c.merge_origin or id(c)
            if key in seen:
                continue
            seen.add(key)
            if not isinstance(c.value, str):
                if c.value is not None:
                    data_like += 1
                continue
            t = c.text()
            if looks_like_date(t):
                data_like += 1
                continue
            role, conf, _ = self.v.column_roles.match(t)
            stage, _ = self.match_stage(t)
            sub, _, _ = self.v.stage_subheaders.match(t)
            if role or stage or sub:
                matches += 1
        return matches, data_like

    # ------------------------------------------------------------------ #
    def find_header_rows(self, table: RawTable) -> tuple[list[int], list[str]]:
        reasons: list[str] = []
        limit = min(len(table.rows), 40)
        scores = [self._row_header_score(table.rows[i]) for i in range(limit)]
        best = max((s[0] - s[1] for s in scores), default=0)
        if best < 2:
            return [], ["no row with at least two recognisable column headers"]
        start = next(i for i, s in enumerate(scores) if s[0] - s[1] >= max(2, int(best * 0.75)))
        rows = [start]
        reasons.append(f"header row detected at source row {table.row_numbers[start]} "
                       f"({scores[start][0]} recognised headers)")
        # extend with sub-header rows (two/three-row headers)
        i = start + 1
        while i < limit and len(rows) < 3:
            matches, data_like = scores[i]
            if data_like > 0 or matches == 0:
                break
            # A sub-header row is mostly text and contains recognised keywords.
            texts = [c for c in table.rows[i] if not c.is_empty()]
            if not texts or any(not isinstance(c.value, str) for c in texts):
                break
            rows.append(i)
            reasons.append(f"row {table.row_numbers[i]} treated as sub-header ({matches} keywords)")
            i += 1
        if start > 0:
            reasons.append(f"{start} row(s) above the header ignored (titles/notes)")
        return rows, reasons

    def header_parts(self, table: RawTable, header_rows: list[int], col: int) -> list[str]:
        parts: list[str] = []
        for r in header_rows:
            t = clean_text(table.cell(r, col).value)
            if t and (not parts or norm_header(parts[-1]) != norm_header(t)):
                parts.append(t)
        return parts

    # ------------------------------------------------------------------ #
    def classify_column(self, parts: list[str], values: list[Cell]) -> ColumnMapping:
        header = " / ".join(parts)
        evidence: list[str] = []
        nonempty = [c for c in values if not c.is_empty()]
        n = len(nonempty)
        date_frac = (sum(1 for c in nonempty if looks_like_date(c.value)) / n) if n else 0.0
        status_frac = (sum(1 for c in nonempty if self.v.statuses.match(c.text())[0]) / n) if n else 0.0

        if not parts:
            if n == 0:
                return ColumnMapping(-1, "", None, 0.0, ["empty column"])
            return ColumnMapping(-1, "", None, 0.0, ["no header; values preserved as extra attribute"])

        # 1. A stage name in any header part -> stage column.
        stage, stage_conf, stage_part = None, 0.0, None
        for p in parts:
            s, conf = self.match_stage(p)
            if s:
                # A role phrase that is longer than the stage synonym wins
                # ("Статус КД" is documentation_status, not the КД stage).
                role, rconf, phrase = self.v.column_roles.match(p)
                if role and rconf == 1.0 and len(parts) == 1:
                    break
                stage, stage_conf, stage_part = s, conf, p
                break
        if stage:
            kind = None
            for p in parts:
                if p is stage_part:
                    rest = p
                    # kind written in the same cell: "Сканирование (факт)"
                    sub, _, _ = self.v.stage_subheaders.match(rest)
                    if sub and len(parts) == 1:
                        kind = sub
                    continue
                sub, _, _ = self.v.stage_subheaders.match(p)
                role, _, _ = self.v.column_roles.match(p)
                kind = sub or ROLE_TO_STAGE_KIND.get(role or "") or kind
            if kind is None and len(parts) == 1:
                for w in re.findall(r"[\w]+", norm_header(stage_part)):
                    sub, _, _ = self.v.stage_subheaders.match(w, allow_partial=False)
                    if sub:
                        kind = sub
                        break
            conf = stage_conf
            if kind in ("planned_start", "planned_end", "actual") and n and date_frac < 0.5:
                if status_frac >= 0.5:
                    evidence.append(f"header says {kind} but values look like statuses")
                    kind = "status"
                else:
                    evidence.append(f"only {date_frac:.0%} of values are dates")
                    conf *= 0.6
            if kind is None:
                if n and date_frac >= 0.6:
                    kind = "date"
                    conf *= 0.6
                    evidence.append("dates under a stage header without plan/actual sub-header: "
                                    "meaning must be confirmed")
                elif n and status_frac >= 0.5:
                    kind = "status"
                    evidence.append(f"{status_frac:.0%} of values are status words/marks")
                else:
                    kind = "value"
                    conf *= 0.5
                    evidence.append("stage column with free-text values")
            evidence.insert(0, f"stage '{stage}' recognised in header")
            return ColumnMapping(-1, header, f"stage:{stage}:{kind}", round(conf, 2), evidence)

        # 2. Plain column role.
        role, conf = None, 0.0
        for p in [" ".join(parts)] + list(reversed(parts)):
            r, c, ph = self.v.column_roles.match(p)
            if r and c > conf:
                role, conf = r, c
                evidence = [f"header matches '{ph}'"]
        if role is None:
            if n and date_frac >= 0.6:
                return ColumnMapping(-1, header, None, 0.0,
                                     [f"unrecognised header; {date_frac:.0%} dates - meaning unknown, "
                                      "preserved as extra attribute"])
            return ColumnMapping(-1, header, None, 0.0, ["unrecognised header; preserved as extra attribute"])

        # 3. Content validation.
        if role in DATE_ROLES and n:
            if date_frac < 0.5:
                conf *= 0.5
                evidence.append(f"only {date_frac:.0%} of values are valid dates")
            else:
                evidence.append(f"{date_frac:.0%} of values are dates")
        if role == "row_number" and n:
            ints = sum(1 for c in nonempty if re.fullmatch(r"\d+(\.\d+)?", c.text()))
            if ints / n < 0.8:
                role, conf = "external_id", 0.6
                evidence.append("'№' column contains non-numeric codes -> treated as project identifier")
        if role == "engineer" and n:
            names = sum(1 for c in nonempty if isinstance(c.value, str) and not looks_like_date(c.value)
                        and len(c.text().split()) <= 4)
            if names / n < 0.7:
                conf *= 0.5
                evidence.append("values do not look like person names")
        if role == "priority" and n:
            numeric = [c for c in nonempty if re.fullmatch(r"\d+", c.text())]
            if numeric:
                evidence.append("numeric priorities: direction (1 = highest?) must be confirmed")
        return ColumnMapping(-1, header, role, round(conf, 2), evidence)

    # ------------------------------------------------------------------ #
    def detect(self, table: RawTable) -> TableSchema:
        header_rows, reasons = self.find_header_rows(table)
        if not header_rows:
            return TableSchema(table.source, False, [], 0, [], Layout.UNKNOWN, reasons=reasons)
        data_start = header_rows[-1] + 1
        # Content statistics ignore section rows (one value spanning the row)
        # and totals rows, which would otherwise dilute every column.
        data = []
        for r in table.rows[data_start:]:
            filled = [c for c in r if not c.is_empty()]
            if not filled or TOTAL_ROW.match(filled[0].text()):
                continue
            if len(filled) > 1 and len({c.text() for c in filled}) == 1:
                continue
            data.append(r)
        columns: list[ColumnMapping] = []
        for col in range(table.width):
            parts = self.header_parts(table, header_rows, col)
            values = [r[col] if col < len(r) else Cell(None) for r in data]
            m = self.classify_column(parts, values)
            m.index = col
            columns.append(m)

        # Resolve duplicate scalar roles: keep best, preserve others as extra.
        by_role: dict[str, list[ColumnMapping]] = {}
        for m in columns:
            if m.role and m.role not in MULTI_ROLES:
                by_role.setdefault(m.role, []).append(m)
        for role, ms in by_role.items():
            if len(ms) > 1:
                ms.sort(key=lambda m: (-m.confidence, m.index))
                for loser in ms[1:]:
                    loser.evidence.append(f"second column mapped to '{role}' (kept '{ms[0].header}'); "
                                          "preserved as extra attribute")
                    loser.role = None
                    loser.confidence = 0.0

        schema = TableSchema(table.source, True, header_rows, data_start, columns, Layout.UNKNOWN,
                             reasons=reasons)
        roles = schema.roles()
        id_roles = roles & IDENTITY_ROLES
        plan_roles = (roles & PLANNING_ROLES) | {r for r in roles if r.startswith("stage:")}
        if not id_roles or not plan_roles:
            schema.relevant = False
            schema.reasons.append("no project identity columns" if not id_roles
                                  else "no planning columns (engineer/dates/stages/status)")
            return schema
        schema.score = len(id_roles) + len(plan_roles)
        schema.group_row_indexes = self.find_group_rows(table, schema)
        self._assign_group_levels(table, schema)
        schema.layout = self.detect_layout(table, schema)
        schema.reasons.append(f"layout: {schema.layout.value}")
        return schema

    # ------------------------------------------------------------------ #
    def find_group_rows(self, table: RawTable, schema: TableSchema) -> list[int]:
        role_of = {c.index: c.role for c in schema.columns}
        blocked_roles = {"activity", "current_stage", "project_status", "activity_status"} | DATE_ROLES
        out = []
        for i in range(schema.data_start, len(table.rows)):
            row = table.rows[i]
            cells = [(j, c) for j, c in enumerate(row) if not c.is_empty()]
            if not cells:
                continue
            distinct = {c.merge_origin or (i, j) for j, c in cells}
            values = {c.text() for _, c in cells}
            if len(values) != 1 or TOTAL_ROW.match(cells[0][1].text()):
                continue
            j, c = cells[0]
            role = role_of.get(j)
            if role in blocked_roles or (role or "").startswith("stage:"):
                continue
            if looks_like_date(c.value) or self.v.statuses.match(c.text())[0]:
                continue
            horizontal = len(cells) > 1 and len(distinct) == 1
            # The row must not be a vertically-merged continuation of a data row.
            vertical_only = all(cc.merged and cc.merge_origin and cc.merge_origin[0] != table.row_numbers[i]
                                for _, cc in cells)
            if vertical_only:
                continue
            # A lone value in the left-most identity column ("HAVAL" in the
            # Manufacturer column) is a section header; a lone value further
            # right is more likely an incomplete project row and is kept as data.
            leftmost_identity = j == min((cc.index for cc in schema.columns if cc.role in IDENTITY_ROLES),
                                         default=-1)
            if horizontal or c.bold or c.fill or role in (None, "row_number") or leftmost_identity:
                out.append(i)
        return out

    def _assign_group_levels(self, table: RawTable, schema: TableSchema) -> None:
        if not schema.group_row_indexes:
            return
        roles = schema.roles()
        missing = [r for r in HIERARCHY_ORDER if r not in roles
                   and not (r in ("manufacturer", "vehicle_model") and "vehicle" in roles)]
        # count depth: longest run of consecutive group rows
        runs, cur = [], 1
        idx = schema.group_row_indexes
        for a, b in zip(idx, idx[1:]):
            if b == a + 1:
                cur += 1
            else:
                runs.append(cur)
                cur = 1
        runs.append(cur)
        depth = max(runs)
        levels = sorted({table.outline_levels[i] for i in idx}) if table.outline_levels else []
        if len(levels) > depth:
            depth = len(levels)
        schema.group_levels_role = [missing[k] if k < len(missing) else f"group_level_{k + 1}"
                                    for k in range(depth)]
        schema.reasons.append(f"{len(idx)} group/section rows; hierarchy levels -> {schema.group_levels_role}")

    def detect_layout(self, table: RawTable, schema: TableSchema) -> Layout:
        roles = schema.roles()
        has_stage_cols = any(r.startswith("stage:") for r in roles)
        if "activity" in roles:
            return Layout.LONG
        id_cols = [c.index for c in schema.columns if c.role in IDENTITY_ROLES]
        group = set(schema.group_row_indexes)
        keys = []
        for i in range(schema.data_start, len(table.rows)):
            if i in group:
                continue
            k = "|".join(norm_key(table.cell(i, j).value) for j in id_cols)
            if k.strip("|"):
                keys.append(k)
        repeats = sum(n for n in Counter(keys).values() if n > 1)
        if "current_stage" in roles and keys and repeats / len(keys) >= 0.3 and not has_stage_cols:
            return Layout.LONG
        return Layout.WIDE
