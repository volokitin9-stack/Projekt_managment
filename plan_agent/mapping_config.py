"""Proposed / approved mapping configuration.

Lifecycle
---------
1. ``analyze`` writes ``state/mapping.proposed.yaml``: every relevant sheet,
   header rows, the role of every column (with confidence and evidence),
   group-row hierarchy, and every distinct stage/status/priority value seen
   with its proposed interpretation.
2. A person reviews it, edits it if needed (e.g. changes a column role or maps
   an unknown status), and runs ``approve``.  This writes
   ``state/mapping.approved.yaml`` together with the structure *fingerprint*.
3. Later files whose fingerprint matches are processed with the approved
   mapping and may sync automatically.  If the structure changed (columns
   added/renamed/re-interpreted) the fingerprint differs and the agent falls
   back to dry-run + new validation.  New stage/status values that were never
   approved send only the affected projects to review.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .models import Layout, RawTable, TableSchema
from .normalize import norm_header

VALID_ROLES = {
    "row_number", "external_id", "project_name", "manufacturer", "vehicle_model", "vehicle", "product_type",
    "product_category", "article_number", "engineer", "priority", "project_status", "current_stage", "activity",
    "activity_status", "comment", "description", "planned_start", "planned_end", "actual_completion",
    "dependency", "progress", "documentation_status",
}
STAGE_KINDS = {"planned_start", "planned_end", "actual", "status", "engineer", "comment", "dependency", "date",
               "value"}


def col_letter(idx: int) -> str:
    s, n = "", idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def header_signature(schema: TableSchema) -> list:
    return sorted([norm_header(c.header), c.role or ""] for c in schema.columns if c.header)


def fingerprint(schemas: list[TableSchema]) -> str:
    sigs = sorted(json.dumps([s.layout.value, s.group_levels_role, header_signature(s)], ensure_ascii=False)
                  for s in schemas if s.relevant)
    return hashlib.sha256("\n".join(sigs).encode()).hexdigest()[:16]


def validate_role(role: Optional[str]) -> bool:
    if role is None:
        return True
    if role.startswith("stage:"):
        parts = role.split(":")
        return len(parts) == 3 and parts[2] in STAGE_KINDS or (len(parts) == 4 and parts[1] == "extra"
                                                               and parts[3] in STAGE_KINDS)
    return role in VALID_ROLES


def build_proposal(file_name: str, tables: list[RawTable], schemas: list[TableSchema],
                   observed_values: dict, fp: str) -> dict:
    sheets = []
    for t, s in zip(tables, schemas):
        entry = {
            "name": s.source,
            "relevant": s.relevant,
            "reasons": s.reasons,
        }
        if s.header_rows:
            entry["header_rows"] = [t.row_numbers[i] for i in s.header_rows]
        if s.relevant:
            entry["layout"] = s.layout.value
            entry["group_levels"] = s.group_levels_role
            entry["group_rows"] = [t.row_numbers[i] for i in s.group_row_indexes]
            entry["columns"] = [{
                "column": col_letter(c.index),
                "header": c.header,
                "role": c.role,
                "confidence": c.confidence,
                "needs_confirmation": bool(c.role is None and c.header) or c.confidence < 0.7
                                      or any("confirm" in e for e in c.evidence),
                "evidence": c.evidence,
            } for c in s.columns if c.header or c.role or (c.evidence and c.evidence[0] != "empty column")]
        sheets.append(entry)
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "analysed_file": file_name,
        "fingerprint": fp,
        "approved": False,
        "sheets": sheets,
        # Value interpretations.  Edit the right-hand side to correct a mapping;
        # null means "not interpretable" (records using it go to review).
        "value_mappings": {k: dict(sorted(v.items())) for k, v in observed_values.items()},
    }


class MappingStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proposed_path = self.dir / "mapping.proposed.yaml"
        self.approved_path = self.dir / "mapping.approved.yaml"

    def save_proposal(self, proposal: dict) -> Path:
        with open(self.proposed_path, "w", encoding="utf-8") as fh:
            fh.write("# PROPOSED mapping - review, edit if needed, then run: plan-agent approve\n")
            yaml.safe_dump(proposal, fh, allow_unicode=True, sort_keys=False, width=120)
        return self.proposed_path

    def load_approved(self) -> Optional[dict]:
        if not self.approved_path.exists():
            return None
        with open(self.approved_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if data and data.get("approved") else None

    def approve(self, approved_by: str) -> dict:
        if not self.proposed_path.exists():
            raise FileNotFoundError("no proposed mapping; run 'analyze' first")
        with open(self.proposed_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        errors = []
        for sh in data.get("sheets", []):
            for c in sh.get("columns", []) or []:
                if not validate_role(c.get("role")):
                    errors.append(f"sheet '{sh['name']}' column {c['column']}: unknown role {c['role']!r}")
        if errors:
            raise ValueError("cannot approve mapping:\n  " + "\n  ".join(errors))
        data["approved"] = True
        data["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data["approved_by"] = approved_by
        with open(self.approved_path, "w", encoding="utf-8") as fh:
            fh.write("# APPROVED mapping - files with the same structure fingerprint sync automatically\n")
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, width=120)
        return data


def apply_approved(schemas: list[TableSchema], tables: list[RawTable], approved: dict) -> list[str]:
    """Apply human-edited column roles from the approved mapping.

    Matching is by sheet header signature (header texts), not by sheet name or
    file name.  Returns notes describing what was overridden.
    """
    notes = []
    by_headers = {}
    for sh in approved.get("sheets", []):
        if sh.get("relevant") and sh.get("columns"):
            key = tuple(sorted(norm_header(c["header"]) for c in sh["columns"] if c.get("header")))
            by_headers[key] = sh
    for s in schemas:
        if not s.header_rows:
            continue
        key = tuple(sorted(norm_header(c.header) for c in s.columns if c.header))
        sh = by_headers.get(key)
        if sh is None:
            continue
        roles = {norm_header(c["header"]): c.get("role") for c in sh["columns"] if c.get("header")}
        for c in s.columns:
            if c.header and norm_header(c.header) in roles and roles[norm_header(c.header)] != c.role:
                notes.append(f"{s.source}: column '{c.header}' role {c.role!r} -> {roles[norm_header(c.header)]!r} "
                             "(approved mapping)")
                c.role = roles[norm_header(c.header)]
                c.confidence = 1.0
                c.evidence.append("role set by approved mapping")
        s.relevant = bool(sh.get("relevant"))
        if sh.get("layout"):
            s.layout = Layout(sh["layout"])
        if sh.get("group_levels") is not None:
            s.group_levels_role = list(sh["group_levels"])
    return notes
