"""Synchronisation report and initial-analysis report."""
from __future__ import annotations

from collections import Counter
from typing import Optional

from .mapping_config import col_letter
from .models import ChangePlan, ChangeType, Severity
from .pipeline import AnalysisResult


def summarize(plans: list[ChangePlan], executed: bool) -> dict:
    """Counts reflect what actually happened (when executed) or what would
    happen (dry run).  A failed create is an error, not a new project."""
    c = Counter()
    for p in plans:
        t = p.change_type
        if executed and t in (ChangeType.NEW, ChangeType.UPDATE) and p.result != "ok":
            c["errors"] += 1
            continue
        c[{ChangeType.NEW: "new", ChangeType.UPDATE: "updated", ChangeType.UNCHANGED: "unchanged",
           ChangeType.DUPLICATE: "duplicates", ChangeType.REVIEW: "review", ChangeType.ERROR: "invalid"}[t]] += 1
    return {"identified": len(plans), "new": c["new"], "updated": c["updated"], "unchanged": c["unchanged"],
            "review": c["review"] + c["duplicates"], "duplicate_candidates": c["duplicates"],
            "invalid_records": c["invalid"], "sync_errors": c["errors"]}


def build_report(a: AnalysisResult, plans: list[ChangePlan], mode: str, status: str, run_id: str,
                 comparison_available: bool, notes: list[str], fatal: Optional[str] = None) -> dict:
    executed = mode == "sync"
    return {
        "run_id": run_id,
        "input_file": a.file_name,
        "file_sha256": a.file_sha256,
        "file_format": a.parsed.file_format,
        "mode": mode,
        "status": status,
        "structure_fingerprint": a.fingerprint,
        "approved_mapping_used": a.structure_matches_approved,
        "dashboard_comparison": comparison_available,
        "counts": summarize(plans, executed),
        "row_errors": a.row_errors,
        "file_warnings": a.parsed.warnings,
        "notes": notes + a.mapping_notes,
        "fatal_error": fatal,
        "unapproved_values": a.new_values,
        "projects": [p.to_dict() for p in plans],
    }


def render_text(r: dict) -> str:
    c = r["counts"]
    dry = r["mode"] != "sync"
    lines = [
        "ENGINEERING PLAN SYNCHRONIZATION" + ("  [DRY RUN - nothing was written]" if dry else ""),
        "",
        f"Input file:                 {r['input_file']} ({r['file_format']}, sha256 {r['file_sha256'][:12]})",
        f"Run id:                     {r['run_id']}",
        f"Projects identified:        {c['identified']}",
        f"New projects:               {c['new']}" + ("  (would be created)" if dry else ""),
        f"Existing projects updated:  {c['updated']}" + ("  (would be updated)" if dry else ""),
        f"Projects unchanged:         {c['unchanged']}",
        f"Projects requiring review:  {c['review']}  (of which duplicate candidates: {c['duplicate_candidates']})",
        f"Invalid records:            {c['invalid_records'] + len(r['row_errors'])}",
        f"Synchronization errors:     {c['sync_errors']}",
        "",
        f"Status: {r['status']}",
    ]
    if not r["dashboard_comparison"]:
        lines += ["", "WARNING: dashboard could not be queried; 'new' counts assume an empty dashboard."]
    if r.get("fatal_error"):
        lines += ["", f"FATAL: {r['fatal_error']}"]
    for n in r["notes"]:
        lines.append(f"NOTE: {n}")

    def section(title, items):
        if items:
            lines.extend(["", title, "-" * len(title)])
            lines.extend(items)

    projects = r["projects"]
    section("Created" if not dry else "Would create",
            [f"  + {p['project']}  [{p['source']}]" for p in projects if p["change_type"] == "new"
             and (dry or p["result"] == "ok")])
    upd = []
    for p in projects:
        if p["change_type"] == "update" and (dry or p["result"] == "ok"):
            upd.append(f"  ~ {p['project']} (#{p['dashboard_id']})  [{p['source']}]")
            for ch in p["changes"]:
                if ch["field"] in ("stages", "extra_attributes"):
                    upd.append(f"      {ch['field']}: updated")
                else:
                    upd.append(f"      {ch['field']}: {ch['old']!r} -> {ch['new']!r}")
    section("Updated" if not dry else "Would update", upd)
    rev = []
    for p in projects:
        if p["change_type"] in ("review", "duplicate_candidate"):
            rev.append(f"  ? {p['project']}  [{p['source']}]  {p['change_type']}: {p['reason']}")
            for cand in p["candidates"]:
                rev.append(f"      candidate: {cand}")
            for i in p["issues"]:
                if i["severity"] in ("review", "error"):
                    rev.append(f"      - {i['message']}")
    section("Requires review (not synchronised)", rev)
    conf = [f"  ! {p['project']}: {c['field']} dashboard={c['dashboard_value']!r} file={c['file_value']!r} "
            f"-> {c['resolution']} ({c['reason']})" for p in projects for c in p["conflicts"]]
    section("Conflicts", conf)
    failed = [f"  x {p['project']}  [{p['source']}]: {p['error']}" for p in projects
              if p["result"] in ("failed", "not_attempted")]
    failed += [f"  x invalid record {p['project']} [{p['source']}]: " +
               "; ".join(i["message"] for i in p["issues"] if i["severity"] == "error")
               for p in projects if p["change_type"] == "error"]
    failed += [f"  x {e['source']} row {e['row']}: {e['error']} {e['values']}" for e in r["row_errors"]]
    section("Errors", failed)
    warn = [f"  - {p['project']} [{p['source']}]: {i['message']}" for p in projects for i in p["issues"]
            if i["severity"] == "warning"]
    section("Warnings", warn + [f"  - {w}" for w in r["file_warnings"]])
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
ROLE_LABELS = {
    "engineer": "responsible engineer", "planned_start": "planned start", "planned_end": "planned completion",
    "actual_completion": "actual completion", "external_id": "project identifier",
}


def render_initial_analysis(a: AnalysisResult, plans: list[ChangePlan], sync_report: dict) -> str:
    """Markdown report for the first file (initial analysis mode)."""
    L: list[str] = [f"# Initial analysis: {a.file_name}", ""]
    L += [f"* Format: **{a.parsed.file_format}**, sheets/sections: {', '.join(a.parsed.sheet_names)}",
          f"* Structure fingerprint: `{a.fingerprint}`",
          f"* Projects detected: **{len(a.records)}**", ""]
    L += ["## 1. Worksheets", ""]
    for s in a.schemas:
        mark = "relevant" if s.relevant else "ignored"
        L.append(f"* **{s.source}** - {mark}: " + "; ".join(s.reasons))
    for s, t in zip(a.schemas, a.parsed.tables):
        if not s.relevant:
            continue
        L += ["", f"## 2. Column interpretation - sheet '{s.source}'", "",
              f"Header rows: {[t.row_numbers[i] for i in s.header_rows]}; data from row "
              f"{t.row_numbers[s.data_start] if s.data_start < len(t.row_numbers) else '-'}; layout **{s.layout.value}** "
              + ("(one row per project, stages as columns)" if s.layout.value == "wide"
                 else "(several activity rows per project)"), ""]
        if s.group_levels_role:
            L.append(f"Section/group rows ({len(s.group_row_indexes)}) are interpreted as hierarchy levels: "
                     f"{s.group_levels_role}. Example values: "
                     + ", ".join(sorted({t.rows[i][0].text() or next(c.text() for c in t.rows[i] if c.text())
                                         for i in s.group_row_indexes})[:6]))
            L.append("")
        L += ["| Col | Header | Interpreted as | Confidence | Evidence |", "|---|---|---|---|---|"]
        for c in s.columns:
            if not c.header and not c.role:
                continue
            role = c.role or "*(unmapped - preserved in extra_attributes)*"
            flag = " ⚠" if (c.role is None or c.confidence < 0.7 or any("confirm" in e for e in c.evidence)) else ""
            L.append(f"| {col_letter(c.index)} | {c.header} | {role}{flag} | {c.confidence:.2f} | "
                     f"{'; '.join(c.evidence)} |")
        eng = [c.header for c in s.columns if c.role == "engineer" or (c.role or "").endswith(":engineer")]
        dl = [c.header for c in s.columns if c.role in ("planned_end",) or (c.role or "").endswith(":planned_end")
              or (c.role or "").endswith(":date")]
        act = [c.header for c in s.columns if c.role in ("actual_completion",) or (c.role or "").endswith(":actual")]
        L += ["", f"* Engineer columns: {eng or 'none found'}",
              f"* Deadline (planned) columns: {dl or 'none found'}",
              f"* Actual-completion columns: {act or 'none found'}"]

    L += ["", "## 3. How projects are identified", ""]
    methods = Counter()
    for r in a.records:
        k = r.identity_key() or "none"
        methods[k.split(":")[0]] += 1
    names = {"ext": "project identifier column", "art": "article number", "cmp": "manufacturer + model + product",
             "name": "project name only (weak)", "none": "not identifiable"}
    for k, n in methods.items():
        L.append(f"* {n} project(s) by {names.get(k, k)}")
    L.append("* Matching against the dashboard uses: project id → article number → manufacturer+model+product "
             "(exact). Similar names only produce duplicate candidates for review.")

    L += ["", "## 4. Workflow and status interpretation", ""]
    vm = a.proposal["value_mappings"]
    for kind in ("stage", "status", "priority"):
        if vm.get(kind):
            L.append(f"**{kind}** values found in the file:")
            L.append("")
            for raw, mapped in vm[kind].items():
                L.append(f"* `{raw}` → {mapped if mapped else '**not interpretable - review**'}")
            L.append("")
    L += ["Rules applied:",
          "* A stage is *done* only with an explicit status or an actual date. A passed planned date never "
          "marks anything complete.",
          "* Current stage = furthest stage in progress, otherwise the first unfinished stage.",
          "* Project status comes from the explicit status column when present; it is compared with the stage "
          "progress and contradictions are flagged.",
          "* Unknown stages/activities are kept with their original names (never discarded).", ""]

    L += ["## 5. Mapping to dashboard fields", "",
          "| Dashboard field | Source |", "|---|---|",
          "| external_project_id | identifier column |",
          "| manufacturer / vehicle_model / product_type | columns or section rows |",
          "| assigned_engineer | engineer column (or the current stage's engineer in activity layouts) |",
          "| current_stage / project_status | derived as described above |",
          "| planned_start_date / planned_end_date | explicit columns, else earliest/latest stage plan date |",
          "| actual_completion_date | explicit column, else last actual date when all stages are done |",
          "| stages | every activity with plan/actual dates, status, engineer |",
          "| extra_attributes | comments from the file, unmapped columns, original values |", ""]

    L += ["## 6. Fields that cannot be interpreted reliably", ""]
    unreliable = []
    for s in a.schemas:
        for c in s.columns:
            if s.relevant and (c.header or c.role) and (c.role is None or c.confidence < 0.7
                                                        or any("confirm" in e for e in c.evidence)):
                unreliable.append(f"* sheet '{s.source}', column {col_letter(c.index)} '{c.header}': "
                                  f"{c.role or 'unmapped'} ({'; '.join(c.evidence)})")
    issue_counts = Counter((i.severity.value, i.code) for r in a.records for i in r.issues)
    for (sev, code), n in sorted(issue_counts.items()):
        if sev in ("review", "error", "warning"):
            unreliable.append(f"* {n} × {code} ({sev})")
    L += unreliable or ["* none"]

    c = sync_report["counts"]
    L += ["", "## 7. Proposed changes (dry run)", "",
          f"New: {c['new']} · Update: {c['updated']} · Unchanged: {c['unchanged']} · Review: {c['review']} · "
          f"Invalid: {c['invalid_records']}", "",
          "| Project | Source | Action | Stage | Status | Engineer | Deadline | Notes |",
          "|---|---|---|---|---|---|---|---|"]
    for p in plans:
        r = p.record
        note = p.reason if p.change_type not in (ChangeType.NEW, ChangeType.UNCHANGED) else ""
        if p.change_type == ChangeType.UPDATE:
            note = ", ".join(ch.field for ch in p.changes)
        sev = [i.message for i in r.issues if i.severity in (Severity.REVIEW, Severity.ERROR)]
        if sev:
            note = (note + "; " if note else "") + "; ".join(sev)
        L.append(f"| {r.project_name} | {r.source_sheet}!{r.source_row} | {p.change_type.value} | "
                 f"{r.current_stage or '?'} | {r.project_status or '?'} | {r.assigned_engineer or '?'} | "
                 f"{r.planned_end_date or '?'} | {note} |")
    L += ["", "## Next step", "",
          "Review `state/mapping.proposed.yaml` (edit column roles or value mappings if needed), then approve:",
          "", "```bash", "python -m plan_agent approve --by \"<your name>\"", "```", "",
          "After approval, files with the same structure fingerprint synchronise automatically; a changed "
          "structure triggers a new dry run and approval."]
    return "\n".join(L) + "\n"
