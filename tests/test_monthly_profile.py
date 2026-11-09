"""Monthly work-plan profile (structure of the department's real workbook; synthetic content)."""
import hashlib
from datetime import date

import pytest
import requests

import make_fixtures
from plan_agent.config import load_vocabulary
from plan_agent.pipeline import analyze_file

KEY = "secret-test-key"


@pytest.fixture(scope="module")
def wb(tmp_path_factory):
    d = tmp_path_factory.mktemp("monthly")
    make_fixtures.make_monthly_workbook(d / "work_plan_2026.xlsx", 1)
    make_fixtures.make_monthly_workbook(d / "work_plan_2026_v2.xlsx", 2)
    make_fixtures.make_monthly_workbook(d / "two_novembers_2026.xlsx", 1, duplicate_november=True)
    make_fixtures.make_monthly_workbook(d / "work_plan_2026_dec.xlsx", 1, december=True)
    return d


def run(path, month="2026-11", approved=None):
    return analyze_file(path, load_vocabulary(), approved, display_name=path.name, target_month=month)


def one(a, name, category="main_project"):
    found = [r for r in a.records if r.project_name == name and r.record_category == category]
    assert len(found) == 1, (name, [r.project_name for r in a.records])
    return found[0]


# --------------------------------------------------------------------------- sheet selection
def test_selects_only_november_sheet(wb):
    a = run(wb / "work_plan_2026.xlsx")
    p = a.profile
    assert p.analysis["source_sheet"] == "Ноябрь" and not p.blocking
    roles = {s["name"]: s["role"] for s in p.analysis["sheets"]}
    assert roles["Сводная Ноябрь"] == "target_month_derived_summary" and roles["Октябрь"] == "other_month_plan"
    assert not any("Tiggo 9" in (r.project_name or "") for r in a.records)
    assert all(r.source_sheet == "Ноябрь" and r.last_plan_month == "2026-11" for r in a.records)


def test_month_is_configurable_and_auto(wb):
    a = run(wb / "work_plan_2026.xlsx", month="2026-10")
    assert a.profile.analysis["source_sheet"] == "Октябрь"
    assert [r.project_name for r in a.records if r.record_category == "main_project"] == \
        ["Chery Tiggo 9 Защита картера"]
    from plan_agent.profiles.monthly_plan import resolve_month
    assert resolve_month("auto", {}, today=date(2026, 11, 9)) == (2026, 11)
    assert resolve_month(None, {"target_month": "auto"}, today=date(2027, 1, 3)) == (2027, 1)


def test_two_november_plan_sheets_block_synchronisation(wb):
    a = run(wb / "two_novembers_2026.xlsx")
    assert a.profile.analysis["source_sheet"] is None
    assert "several worksheets look like the ноябрь plan" in a.profile.blocking[0]


def test_year_mismatch_is_flagged(wb):
    assert any("year" in b for b in run(wb / "work_plan_2026.xlsx", month="2027-11").profile.blocking)


# --------------------------------------------------------------------------- categories & accounting
def test_three_categories_and_every_row_accounted(wb):
    a = run(wb / "work_plan_2026.xlsx")
    cats = {c: [r for r in a.records if r.record_category == c] for c in ("main_project", "additional_work",
                                                                          "backlog")}
    assert [r.project_name for r in cats["additional_work"]] == ["Упаковка защит"]
    assert cats["additional_work"][0].assigned_engineer == "Иванов"
    assert cats["additional_work"][0].extra_attributes["actual_hours"] == 3
    bl = cats["backlog"][0]
    assert bl.project_name == "JETOUR X50 Сетки" and bl.extra_attributes["scheduling_note"] == "На Август"
    assert bl.project_status is None and bl.current_stage is None          # not active, never "done"
    an = a.profile.analysis
    covered = {row for r in a.records for row in r.source_rows}
    assert not [row for row in an["nonempty_rows"] if row not in covered and row not in an["ledger"]]
    assert sum(len(r.source_rows) for r in a.records) == len(covered)         # no row counted twice
    assert any(v["kind"] == "empty_placeholder" for v in an["ledger"].values())
    ids = [r.external_project_id for r in a.records if r.external_project_id]
    assert len(ids) == len(set(ids))


def test_identity_cycles_kits_and_review(wb):
    a = run(wb / "work_plan_2026.xlsx")
    honda = one(a, "Honda CR-V (1995-2001) Защита картера+КПП")
    assert [x.details["cycle"] for x in honda.activities] == [1, 1, 1, 2, 2, 2]
    assert [x.details["sequence"] for x in honda.activities] == [1, 2, 3, 4, 5, 6]
    shtamp = [r for r in a.records if r.project_name == "Проверка, перевод штампованых защит"]
    assert len(shtamp) == 2 and all(r.needs_review() for r in shtamp)          # never merged
    tech = [r for r in a.records if r.project_name == "Техподдержка"][0]
    assert tech.needs_review() and any(i.code == "operational_work" for i in tech.issues)
    assert one(a, "GAC S9 Комплект защит").external_project_id.startswith("PRJ-")


# --------------------------------------------------------------------------- engineers, priority, progress
def test_engineers_are_design_engineers_only(wb):
    a = run(wb / "work_plan_2026.xlsx")
    gac = one(a, "GAC S9 Комплект защит")
    assert gac.assigned_engineer == "Иванов"
    assert gac.activities[0].details["assignee"] == "Орлов/Соколов"            # fit team on the activity
    fit_only = one(a, "Tenet T4 Защита картера")
    assert fit_only.assigned_engineer is None and fit_only.activities[0].details["assignee"] == "Орлов/Соколов"
    audi = one(a, "AUDI A5 long Комплект защит")
    assert audi.needs_review() and audi.assigned_engineer is None
    assert audi.activities[0].details["assignee_original"] == "Н"


def test_priorities(wb):
    a = run(wb / "work_plan_2026.xlsx")
    assert one(a, "GAC S9 Комплект защит").priority == "high"
    assert one(a, "Honda CR-V (1995-2001) Защита картера+КПП").priority == "low"
    lynk = one(a, "Lynk & Co 900 Комплект защит")
    assert lynk.priority is None and lynk.extra_attributes["priority_original"] == "0"
    assert any(i.code == "priority_undefined" for i in lynk.issues) and not lynk.needs_review()


def test_no_completion_from_hours_or_ok(wb):
    a = run(wb / "work_plan_2026.xlsx")
    belgee = one(a, "Belgee X80 (P145) Защита батареи")
    x = belgee.activities[0]
    assert x.details["items_ok"] == "1" and x.details["actual_hours"] == x.details["planned_hours"] == 4
    assert x.details["progress_status"] == "in_progress" and x.details["effort_ratio"] == 1.0
    assert belgee.project_status == "in_progress"
    assert all(xx.details["progress_status"] != "done" for r in a.records for xx in r.activities)
    assert all(r.project_status != "completed" for r in a.records)
    assert belgee.article_number == "XX10TEST01"


def test_transfer_extension_is_additional_type(wb):
    a = run(wb / "work_plan_2026.xlsx")
    tenet = one(a, "Tenet L4 Комплект защит")
    ext = tenet.activities[0]
    assert ext.details["activity_type"] == "transfer_extension" and ext.details["stage"] is None
    assert tenet.activities[1].details["activity_type"] == "documentation"     # the 1С task itself
    assert tenet.current_stage == "documentation"


# --------------------------------------------------------------------------- dashboard flow
def projects(url):
    return requests.get(url + "/api/projects?include=activities").json()["projects"]


def test_dry_run_sync_idempotency_and_updates(wb, backend, make_agent):
    src = wb / "work_plan_2026.xlsx"
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    agent = make_agent(backend)
    first = agent.receive(src, "cli", src.name, target_month="2026-11")
    assert first.status == "Approval required (dry run)"
    assert "Verification: no task silently discarded" in first.analysis_markdown
    assert projects(backend) == []
    agent.store.approve("pytest")

    synced = agent.receive(src, "cli", src.name, target_month="2026-11")
    c = synced.report["counts"]
    assert synced.status == "Completed" and c["sync_errors"] == 0
    db = projects(backend)
    assert len(db) == c["new"]
    assert {p["record_category"] for p in db} == {"main_project", "additional_work", "backlog"}
    gac = next(p for p in db if p["project_name"] == "GAC S9 Комплект защит")
    assert [x["task_type"] for x in gac["activities"]] == ["Примерить", "КД", "1С", "Прочее"]
    assert gac["activities"][0]["assignee"] == "Орлов/Соколов" and gac["activities"][0]["source_sheet"] == "Ноябрь"
    assert gac["stages"] is None                                                  # activities live in their table

    again = agent.receive(src, "cli", src.name, target_month="2026-11")
    assert (again.report["counts"]["new"], again.report["counts"]["updated"]) == (0, 0)
    assert len(projects(backend)) == len(db)

    # manual progress on an activity is protected
    kd = gac["activities"][1]
    r = requests.patch(f"{backend}/api/projects/{gac['id']}/activities/{kd['id']}",
                       json={"changes": {"progress_status": "done", "manual_note": "КД сдана"}})
    assert r.status_code == 200
    v2 = agent.receive(wb / "work_plan_2026_v2.xlsx", "cli", "work_plan_2026_v2.xlsx", target_month="2026-11")
    assert v2.status == "Completed"
    rep = {(p["category"], p["project"]): p for p in v2.report["projects"]}
    assert rep[("main_project", "GAC S9 Комплект защит")]["change_type"] == "update"
    assert rep[("main_project", "Lada Azimut ТСУ")]["change_type"] == "review"   # unseen task value
    gac = requests.get(f"{backend}/api/projects/{gac['id']}").json()
    kd = gac["activities"][1]
    assert kd["actual_hours"] == 5 and kd["progress_status"] == "done" and kd["manual_note"] == "КД сдана"
    assert any(cf["field"].endswith(".progress_status") and cf["resolution"] == "kept_dashboard"
               for p in v2.report["projects"] for cf in p["conflicts"])
    hist = requests.get(f"{backend}/api/projects/{gac['id']}/history").json()["history"]
    assert {h["event_type"] for h in hist} >= {"project_created", "activity_added", "activity_updated"}
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


def test_december_keeps_november_history(wb, backend, make_agent):
    agent = make_agent(backend)
    src = wb / "work_plan_2026_dec.xlsx"
    agent.receive(src, "cli", src.name, target_month="2026-11")
    agent.store.approve("pytest")
    nov = agent.receive(src, "cli", src.name, target_month="2026-11")
    n_nov = nov.report["counts"]["new"]
    dec = agent.receive(src, "cli", src.name, target_month="2026-12")
    assert dec.status == "Completed", dec.text
    rep = {(p["category"], p["project"]): p for p in dec.report["projects"]}
    assert rep[("main_project", "GAC S9 Комплект защит")]["change_type"] == "update"       # same project
    assert rep[("main_project", "Chery Tiggo 4 Защита картера")]["change_type"] == "new"
    db = projects(backend)
    assert len([p for p in db if p["project_name"] == "GAC S9 Комплект защит"]) == 1
    gac = next(p for p in db if p["project_name"] == "GAC S9 Комплект защит")
    months = [x["plan_month"] for x in gac["activities"]]
    assert months.count("2026-11") == 4 and months.count("2026-12") == 2
    assert gac["last_plan_month"] == "2026-12"
    assert any(p["project_name"].startswith("Honda CR-V") for p in db)       # absent in December, kept
    assert len(db) == n_nov + dec.report["counts"]["new"]
    # re-processing November afterwards must not overwrite December's project fields
    late = agent.receive(src, "cli", src.name, target_month="2026-11")
    gac2 = requests.get(f"{backend}/api/projects/{gac['id']}").json()
    assert gac2["last_plan_month"] == "2026-12" and gac2["current_stage"] == gac["current_stage"]
    assert late.report["counts"]["new"] == 0
