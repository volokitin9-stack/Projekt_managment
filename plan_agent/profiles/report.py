"""Dry-run report for the monthly work-plan profile."""
from __future__ import annotations

from collections import Counter, defaultdict

from ..models import ChangeType, Severity

TYPE_LABEL = {"scanning": "Scanning", "development": "Development", "test_prototype": "Test Prototype",
              "measurement": "Measurement", "documentation": "Documentation", "testing": "Testing (additional)",
              "transfer_extension": "Transfer/Extension (additional)",
              "engineering_change": "Engineering change (additional)", None: "— no activity type —"}
MAIN = ["scanning", "development", "test_prototype", "measurement", "documentation"]
TYPES = MAIN + ["testing", "transfer_extension", "engineering_change", None]
CAT_LABEL = {"main_project": "A. Main engineering projects", "additional_work": "B. Additional work",
             "backlog": "C. Task backlog"}
LEDGER_LABEL = {"header": "header row", "above_header": "rows above the header", "section_marker": "section marker",
                "empty_placeholder": "empty placeholder row (no description, no hours)",
                "totals": "totals rows", "resource_table": "resource table", "dropdown_lists": "dropdown source lists",
                "summary_area_other": "other summary-area row"}


def _c(v) -> str:
    if v in (None, "", []):
        return "—"
    if isinstance(v, float):
        v = f"{v:g}"
    return str(v).replace("|", "/").replace("\n", " ")


def _h(x) -> str:
    return f"{x:g}" if x else "0"


def _rows(r) -> str:
    rs = r.source_rows
    return f"{rs[0]}–{rs[-1]}" if len(rs) > 1 else str(rs[0])


def render_profile_analysis(a, plans, report) -> str:
    p = a.profile
    an = p.analysis
    recs = a.records
    plan_of = {id(x.record): x for x in plans}
    L: list[str] = []
    w = L.append

    def action(r):
        pl = plan_of.get(id(r))
        return pl.change_type.value if pl else "?"

    review = [r for r in recs if r.needs_review()]
    ok = {c: [r for r in recs if r.record_category == c and not r.needs_review()] for c in CAT_LABEL}
    acts = [(r, x) for r in recs for x in r.activities]

    w(f"# November dry run (v2): {a.file_name} → Projekt_managment")
    w("")
    w(f"*Target month:* **{an['target_month']}** · *source worksheet:* **{an.get('source_sheet')}** "
      f"(confirmed) · *fingerprint:* `{a.fingerprint}` · **dry run, nothing written**")
    w("")

    # ------------------------------------------------------------------ 1
    w("## 1. Record counts by category")
    w("")
    w("| Category | Importable | In review queue | Activities | Planned h | Actual h |")
    w("|---|---|---|---|---|---|")
    for c, label in CAT_LABEL.items():
        rs = [r for r in recs if r.record_category == c]
        rv = [r for r in rs if r.needs_review()]
        na = sum(len(r.activities) for r in rs)
        ph = sum(x.details["planned_hours"] or 0 for r in rs for x in r.activities)
        ah = sum(x.details["actual_hours"] or 0 for r in rs for x in r.activities)
        w(f"| {label} | **{len(rs) - len(rv)}** | {len(rv)} | {na} | {_h(ph)} | {_h(ah)} |")
    tot_p = sum(x.details["planned_hours"] or 0 for _, x in acts)
    tot_a = sum(x.details["actual_hours"] or 0 for _, x in acts)
    w(f"| **Total** | **{len(recs) - len(review)}** | **{len(review)}** | **{len(acts)}** | {_h(tot_p)} | {_h(tot_a)} |")
    w("")
    c = report["counts"]
    w(f"Proposed dashboard changes: **create {c['new']}**, update {c['updated']}, unchanged {c['unchanged']}, "
      f"held in review {c['review']}. Nothing is deleted; dashboard comments, photos and attachments are not touched.")
    w("")

    # ------------------------------------------------------------------ 2
    ledger = an["ledger"]
    in_records = defaultdict(list)
    for r in recs:
        for row in r.source_rows:
            in_records[row].append(r)
    unaccounted = [row for row in an["nonempty_rows"] if row not in in_records and row not in ledger]
    double = [row for row, rs in in_records.items() if len(rs) > 1]
    w("## 2. Verification: no task silently discarded")
    w("")
    w(f"Every non-empty row of '{an.get('source_sheet')}' is assigned to exactly one place:")
    w("")
    w("| Rows | Count |")
    w("|---|---|")
    for cat, label in CAT_LABEL.items():
        w(f"| task rows → {label} | {sum(len(r.source_rows) for r in recs if r.record_category == cat)} |")
    for kind, n in Counter(v["kind"] for v in ledger.values()).most_common():
        w(f"| {LEDGER_LABEL.get(kind, kind)} | {n} |")
    w(f"| **total non-empty rows** | **{len(an['nonempty_rows'])}** |")
    w("")
    w(f"* Unaccounted rows: **{len(unaccounted)}** {unaccounted if unaccounted else ''}")
    w(f"* Rows counted twice: **{len(double)}** {double if double else ''}")
    w(f"* Rows hidden by the sheet's AutoFilter ({an.get('autofilter', {}).get('filters')}): "
      f"{an['hidden_task_rows']} task rows - all read")
    empties = [(row, v) for row, v in sorted(ledger.items()) if v["kind"] == "empty_placeholder"]
    if empties:
        w("* Empty placeholder rows (no description, no task, 0 hours) - not imported, listed for completeness: " +
          "; ".join(f"row {row} ({', '.join(f'{k}={x}' for k, x in v['values'].items()) or 'blank'})"
                    for row, v in empties))
    w("")

    # ------------------------------------------------------------------ 3
    ids = [r.external_project_id for r in recs if r.external_project_id]
    idc = Counter(ids)
    w("## 3. Verification: no duplicate projects")
    w("")
    w(f"* Stable identifiers generated: {len(ids)}; **unique: {len(set(ids))}**; collisions: "
      f"{[k for k, n in idc.items() if n > 1] or 'none'}")
    w(f"* Dashboard projects matched by identifier/article/identity: {c['updated'] + c['unchanged']} "
      f"(the dashboard is currently empty); duplicate candidates from the dashboard: {c['duplicate_candidates']}")
    w("* Identity: main project = manufacturer + model + product type (+ article when it is one valid number); "
      "additional work = month + engineer + description (+ vehicle/product); backlog = vehicle + product + task. "
      "The same work appearing in a later month gets the **same identifier** and updates the project.")
    rel = [(r, i) for r in recs for i in r.issues if i.code in ("related_projects_same_vehicle",
                                                                 "backlog_matches_active_project")]
    w(f"* Related records kept **separate** (not merged), for your information ({len(rel)}):")
    for r, i in rel:
        w(f"  * {_c(r.project_name)} [{CAT_LABEL[r.record_category][:2].strip('.')}, rows {_rows(r)}]: {i.message}")
    cyc = [r for r in recs if r.extra_attributes.get("cycles")]
    w(f"* Projects with several engineering cycles this month, kept as **one** project with numbered activities "
      f"({len(cyc)}): " + "; ".join(f"{r.project_name} {r.extra_attributes['cycles']}" for r in cyc))
    w("")

    # ------------------------------------------------------------------ 4
    w("## 4. A. Main engineering projects")
    w("")
    w("| ID | Project | Rows | ТГ | Current stage | Status | Design engineer | Priority (orig → dashboard) | "
      "Activities: type · assignee · plan/fact h | Action |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for r in ok["main_project"]:
        po = r.extra_attributes.get("priority_original")
        al = "; ".join(f"{_c(x.name)}→{TYPE_LABEL.get(x.details['activity_type'], '?').split(' ')[0]} · "
                       f"{_c(x.details['assignee'] or x.details['assignee_original'])} · "
                       f"{_h(x.details['planned_hours'])}/{_h(x.details['actual_hours'])}" for x in r.activities)
        w(f"| {r.external_project_id} | {_c(r.project_name)} | {_rows(r)} | {_c(r.extra_attributes.get('product_group'))} "
          f"| {TYPE_LABEL.get(r.current_stage, '—') if r.current_stage else '—'} | {_c(r.project_status)} | "
          f"{_c(r.assigned_engineer)} | {_c(po)} → {_c(r.priority)} | {al} | {action(r)} |")
    w("")

    # ------------------------------------------------------------------ 5
    w("## 5. B. Additional work (section ДОП РАБОТЫ)")
    w("")
    w("Unplanned assignments: shown separately, counted in the workload.")
    w("")
    w("| ID | Engineer | Description | Vehicle / product | Fact h | Planned h | Row | Action |")
    w("|---|---|---|---|---|---|---|---|")
    for r in ok["additional_work"]:
        x = r.activities[0]
        vp = " / ".join(v for v in (" ".join(p for p in (r.manufacturer, r.vehicle_model) if p), r.product_type) if v)
        w(f"| {r.external_project_id} | {_c(r.assigned_engineer)} | {_c(r.project_name)} | {_c(vp)} | "
          f"{_h(x.details['actual_hours'])} | {_h(x.details['planned_hours'])} | {r.source_row} | {action(r)} |")
    w("")

    # ------------------------------------------------------------------ 6
    w("## 6. C. Task backlog (section БАНК ЗАДАЧ)")
    w("")
    w("Future / pending work, separate from active projects. Scheduling notes are kept verbatim; "
      "a historical label such as «На Июнь» is **not** interpreted as done or overdue.")
    w("")
    w("| ID | Item | Task | Assignee | Scheduling note (original) | Feasibility | Planned h | Related active project | Row |")
    w("|---|---|---|---|---|---|---|---|---|")
    for r in ok["backlog"]:
        x = r.activities[0]
        w(f"| {r.external_project_id} | {_c(r.project_name)} | {_c(x.details['task_type'])} | "
          f"{_c(x.details['assignee'])} | {_c(r.extra_attributes.get('scheduling_note'))} | "
          f"{_c(r.extra_attributes.get('feasibility'))} | {_h(x.details['planned_hours'])} | "
          f"{_c(r.extra_attributes.get('related_main_project'))} | {r.source_row} |")
    w("")

    # ------------------------------------------------------------------ 7
    w("## 7. Review queue (not synchronised)")
    w("")
    w("| Record | Category | Rows | Reason(s) | Hours plan/fact |")
    w("|---|---|---|---|---|")
    for r in review:
        reasons = "; ".join(i.message for i in r.issues if i.severity in (Severity.REVIEW, Severity.ERROR))
        ph = sum(x.details["planned_hours"] or 0 for x in r.activities)
        ah = sum(x.details["actual_hours"] or 0 for x in r.activities)
        w(f"| {_c(r.project_name)} | {CAT_LABEL[r.record_category]} | {_rows(r)} | {reasons} | {_h(ph)}/{_h(ah)} |")
    w("")
    warn = [(r, i) for r in recs for i in r.issues if i.severity == Severity.WARNING
            and i.code not in ("related_projects_same_vehicle", "backlog_matches_active_project")]
    if warn:
        w("Warnings on importable records (imported, values preserved as original):")
        w("")
        for r, i in warn:
            w(f"* {_c(r.project_name)} (rows {_rows(r)}): {i.message}")
        w("")

    # ------------------------------------------------------------------ 8
    w("## 8. Engineering workload by employee (November, all categories)")
    w("")
    w("Hours are effort, not completion. Activity-level assignees are used, so scanning/fitment work is counted "
      "for the people who do it. Backlog hours are future work and shown separately.")
    w("")
    rows = defaultdict(lambda: defaultdict(float))
    for r, x in acts:
        who = x.details["assignee"] or (f"'{x.details['assignee_original']}' (unverified)"
                                        if x.details["assignee_original"] else "— not assigned —")
        col = "review" if r.needs_review() else r.record_category
        rows[who][f"{col}_plan"] += x.details["planned_hours"] or 0
        rows[who][f"{col}_fact"] += x.details["actual_hours"] or 0
        rows[who][f"{col}_n"] += 1
        if r.record_category != "backlog":
            rows[who]["fact_total"] += x.details["actual_hours"] or 0
    resp = Counter(r.assigned_engineer for r in ok["main_project"] if r.assigned_engineer)
    totals = an.get("sheet_totals", {})
    daily, res = totals.get("daily_fact", {}), totals.get("resource_table", {})
    w("| Employee | Responsible for (main) | Main: tasks · plan/fact h | Additional: items · fact h | "
      "Review items: tasks · plan/fact h | Backlog: items · plan h | Fact h total | Sheet's own fact total | "
      "Sheet: resource / plan |")
    w("|---|---|---|---|---|---|---|---|---|")
    for who in sorted(rows, key=lambda k: -rows[k]["main_project_plan"]):
        d = rows[who]
        sheet_fact = daily.get(who)
        diff = ""
        if sheet_fact is not None and abs(sheet_fact - d["fact_total"]) > 0.01:
            diff = f" (Δ {d['fact_total'] - sheet_fact:+g})"
        rt = res.get(who) or next((v for k, v in res.items() if k.split()[0] == who.split()[0]), None)
        w(f"| {who} | {resp.get(who, 0)} | {int(d['main_project_n'])} · {_h(d['main_project_plan'])}/"
          f"{_h(d['main_project_fact'])} | {int(d['additional_work_n'])} · {_h(d['additional_work_fact'])} | "
          f"{int(d['review_n'])} · {_h(d['review_plan'])}/{_h(d['review_fact'])} | {int(d['backlog_n'])} · "
          f"{_h(d['backlog_plan'])} | **{_h(d['fact_total'])}**{diff} | {_c(sheet_fact)} | "
          f"{_c(rt and rt['resource'])} / {_c(rt and rt['plan'])} |")
    w("")
    w("*Sheet's own fact total* = the row «Итого факт в день <name>» of the worksheet (independent cross-check); "
      "Δ shows a difference between the sum of the task rows and that total.")
    w("")
    _reconciliation(w, recs, acts, daily)

    # ------------------------------------------------------------------ 9
    w("## 9. Activities by development stage / activity type")
    w("")
    w("| Activity type | Main projects: activities · plan/fact h | Review items | Backlog | Main projects currently at this stage |")
    w("|---|---|---|---|---|")
    stage_n = Counter(r.current_stage for r in ok["main_project"])
    for t in TYPES:
        m = [x for r, x in acts if x.details["activity_type"] == t and r.record_category == "main_project"
             and not r.needs_review()]
        rv = [x for r, x in acts if x.details["activity_type"] == t and r.needs_review()]
        bl = [x for r, x in acts if x.details["activity_type"] == t and r.record_category == "backlog"]
        if not (m or rv or bl or stage_n.get(t)):
            continue
        w(f"| {TYPE_LABEL[t]} | {len(m)} · {_h(sum(x.details['planned_hours'] or 0 for x in m))}/"
          f"{_h(sum(x.details['actual_hours'] or 0 for x in m))} | {len(rv)} | {len(bl)} | "
          f"{stage_n.get(t, 0) if t in MAIN else '—'} |")
    w(f"| — main projects without a main-stage activity | | | | {stage_n.get(None, 0)} |")
    w("")
    tr = [x for r, x in acts if x.details["project_stage"] == "Серия перевод"]
    w(f"Note: {len(tr)} activities belong to projects in stage «Серия перевод». Their **Перевод/Расширение** tasks "
      f"are typed *Transfer/Extension* ({sum(1 for x in tr if x.details['activity_type'] == 'transfer_extension')}); "
      f"their КД / 1С / PDM tasks are typed by the task itself as *Documentation* "
      f"({sum(1 for x in tr if x.details['activity_type'] == 'documentation')}). The original stage is kept on "
      "every activity (`project_stage_original`).")
    w("")

    # ------------------------------------------------------------------ 10
    w("## 10. Dashboard integration: required changes in the Штурман dashboard")
    w("")
    w("The production dashboard is the **Штурман** app (repository branch `main`). Its current agent API was "
      "tested on a local test copy with synthetic probe records. It cannot yet store what this import produces "
      "without loss. The additions below are needed in Штурман, keeping the existing UI and endpoints unchanged. "
      "The reference backend in this repository implements exactly this contract and can serve as the blueprint.")
    w("")
    w("| Needed | Штурман today | Required addition |")
    w("|---|---|---|")
    w("| Categories A/B/C | none - every record becomes a Kanban card | `record_category` on projects; Kanban shows A, "
      "separate lists for B and C |")
    w("| Activities per project and month (assignee, hours, hours per date, progress, ОК/NOK, source row) | none | "
      "`project_activities` table + agent upsert per plan month |")
    w("| Original values, source references, plan month | unknown fields silently dropped | store `extra_attributes`, "
      "`source_*`, `last_plan_month` |")
    w("| Unknown priority / stage | shown as «normal» / «scanning» | allow empty values (display «—») |")
    w("| Priority scale | critical/high/normal/low | agent maps high→high, medium→normal, low→low; never sends "
      "`critical` (not in the plan) |")
    w("| Stage keys | `prototype` | agent maps test_prototype→prototype |")
    w("| Additional work without vehicle | rejected (manufacturer+model required) | relax the requirement for "
      "category B |")
    w("| 14 seeded sample projects | present on a fresh database | keep or remove (your decision); the agent never "
      "deletes them |")
    w("")

    # ------------------------------------------------------------------ board
    w("## Dashboard distribution preview")
    w("")
    w("**Tab A - Main engineering projects (board by current stage)**")
    w("")
    for t in MAIN + [None]:
        items = [r for r in ok["main_project"] if r.current_stage == t]
        if not items:
            continue
        w(f"* **{TYPE_LABEL[t] if t else 'No main-stage activity this month'} ({len(items)}):** " +
          ", ".join(f"{r.project_name}{' ▶' if r.project_status == 'in_progress' else ''}" for r in items))
    w("")
    w("▶ = hours logged this month.")
    w("")
    aw = Counter(r.assigned_engineer or "—" for r in ok["additional_work"])
    w(f"**Tab B - Additional work ({len(ok['additional_work'])} items, "
      f"{_h(sum(r.extra_attributes['actual_hours'] for r in ok['additional_work']))} h):** "
      + ", ".join(f"{k} {n}" for k, n in aw.most_common()))
    w("")
    w(f"**Tab C - Task backlog ({len(ok['backlog'])} items):** " +
      ", ".join(f"{r.project_name} ({_c(r.extra_attributes.get('scheduling_note'))})" for r in ok["backlog"]))
    w("")
    w(f"**Review queue ({len(review)})** - shown to the reviewer, not on the dashboard until resolved.")
    w("")
    w("## Final number of records proposed for synchronisation")
    w("")
    w("| Category | Records | Activities |")
    w("|---|---|---|")
    for cat, label in CAT_LABEL.items():
        w(f"| {label} | **{len(ok[cat])}** | {sum(len(r.activities) for r in ok[cat])} |")
    w(f"| **Total to synchronise** | **{sum(len(v) for v in ok.values())}** | "
      f"{sum(len(r.activities) for v in ok.values() for r in v)} |")
    w(f"| Review queue (not synchronised, kept in `state/review_queue.json`) | {len(review)} | "
      f"{sum(len(r.activities) for r in review)} |")
    w("")
    w("## Next step")
    w("")
    w("Nothing has been written. The first synchronisation stays **blocked** until `verify-deployment` reports "
      "READY for the production dashboard (reachable, authenticated, persistent across a restart, not a local or "
      "temporary instance, compatible API).")
    return "\n".join(L) + "\n"


def reconcile(recs, acts, daily) -> dict:
    """Two independent partitions of the same activities; every hour is counted once."""
    def bucket_cat(r):
        return "review" if r.needs_review() else r.record_category

    def bucket_person(x):
        if x.details["assignee"]:
            return "identified"
        return "unknown_value" if x.details["assignee_original"] else "no_employee"

    out = {"fact": defaultdict(float), "plan": defaultdict(float), "fact_person": defaultdict(float),
           "plan_person": defaultdict(float), "cross": defaultdict(float), "unassigned_rows": []}
    for r, x in acts:
        f, p_ = x.details["actual_hours"] or 0, x.details["planned_hours"] or 0
        c, b = bucket_cat(r), bucket_person(x)
        out["fact"][c] += f
        out["plan"][c] += p_
        out["fact_person"][b] += f
        out["plan_person"][b] += p_
        out["cross"][(b, c)] += f
        if b != "identified" and (f or p_):
            out["unassigned_rows"].append((x.source_row, r.project_name, c, b, x.details["assignee_original"], p_, f))
    out["total_fact"] = sum(out["fact"].values())
    out["total_plan"] = sum(out["plan"].values())
    out["sheet_named_total"] = sum(v or 0 for v in daily.values()) if daily else None
    return out


def _reconciliation(w, recs, acts, daily) -> None:
    rc = reconcile(recs, acts, daily)
    labels = {"main_project": "Main projects (importable)", "additional_work": "Additional work",
              "backlog": "Task backlog", "review": "Records under review"}
    w("### Workload reconciliation (actual hours; every task row counted exactly once)")
    w("")
    w("| Line | Actual h | Planned h |")
    w("|---|---|---|")
    w(f"| **Total hours from the Excel task rows** | **{_h(rc['total_fact'])}** | **{_h(rc['total_plan'])}** |")
    w("| *Split by category:* | | |")
    for k in ("main_project", "additional_work", "backlog", "review"):
        w(f"| — {labels[k]} | {_h(rc['fact'][k])} | {_h(rc['plan'][k])} |")
    w(f"| = sum of categories | {_h(sum(rc['fact'].values()))} | {_h(sum(rc['plan'].values()))} |")
    w("| *Split by employee:* | | |")
    w(f"| — Assigned to identified employees | {_h(rc['fact_person']['identified'])} | {_h(rc['plan_person']['identified'])} |")
    w(f"| — Without an identified employee (empty КО) | {_h(rc['fact_person']['no_employee'])} | "
      f"{_h(rc['plan_person']['no_employee'])} |")
    w(f"| — Unknown engineer value (e.g. «Н») | {_h(rc['fact_person']['unknown_value'])} | "
      f"{_h(rc['plan_person']['unknown_value'])} |")
    w(f"| = sum of employee split | {_h(sum(rc['fact_person'].values()))} | {_h(sum(rc['plan_person'].values()))} |")
    if rc["sheet_named_total"] is not None:
        w(f"| Sheet's own per-engineer totals («Итого факт в день», named engineers only) | "
          f"{_h(rc['sheet_named_total'])} | |")
    w("")
    gap = rc["total_fact"] - rc["fact_person"]["identified"]
    w(f"**Explanation of the difference:** total {_h(rc['total_fact'])} h − hours of identified employees "
      f"{_h(rc['fact_person']['identified'])} h = **{_h(gap)} h**, which are task-row hours with no identified "
      "employee:")
    w("")
    for row, name, cat, b, orig, p_, f in sorted(rc["unassigned_rows"]):
        if f:
            w(f"* row {row}: {_c(name)} — {labels[cat]}, "
              f"{'engineer value ' + repr(orig) if orig else 'КО column empty'}: {_h(f)} h actual")
    w("")
    if rc["sheet_named_total"] is not None:
        rest = rc["fact_person"]["identified"] - rc["sheet_named_total"]
        w(f"The sheet's own per-engineer totals add up to {_h(rc['sheet_named_total'])} h, exactly the hours of the "
          f"identified employees (remaining difference {_h(rest)} h). Hours without an employee are not in any "
          "per-engineer total of the sheet, which is why the employee table sums to less than the overall total.")
        w("")
    plan_gap = [(row, name, cat, b, orig, p_) for row, name, cat, b, orig, p_, f in rc["unassigned_rows"] if p_]
    w(f"Planned hours without an identified employee: {_h(rc['plan_person']['no_employee'])} h in "
      f"{len([x for x in plan_gap if x[3] == 'no_employee'])} task rows, "
      f"{_h(rc['plan_person']['unknown_value'])} h with the unknown value «Н» (listed per project in section 4/7).")
    w("")
