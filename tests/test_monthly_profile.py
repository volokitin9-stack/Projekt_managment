"""Monthly work-plan profile (structure of the department's real workbook)."""
import hashlib
from datetime import date

import pytest
import requests

import make_fixtures
from plan_agent.config import load_vocabulary
from plan_agent.pipeline import analyze_file


@pytest.fixture(scope="module")
def wb(tmp_path_factory):
    d = tmp_path_factory.mktemp("monthly")
    make_fixtures.make_monthly_workbook(d / "work_plan_2026.xlsx", 1)
    make_fixtures.make_monthly_workbook(d / "work_plan_2026_v2.xlsx", 2)
    make_fixtures.make_monthly_workbook(d / "two_novembers_2026.xlsx", 1, duplicate_november=True)
    return d


def run(path, month="2026-11", approved=None):
    return analyze_file(path, load_vocabulary(), approved, display_name=path.name, target_month=month)


def by_name(a, name):
    return [r for r in a.records if r.project_name == name]


def test_selects_only_november_sheet(wb):
    a = run(wb / "work_plan_2026.xlsx")
    p = a.profile
    assert p.analysis["source_sheet"] == "Ноябрь" and not p.blocking
    roles = {s["name"]: s["role"] for s in p.analysis["sheets"]}
    assert roles["Сводная Ноябрь"] == "target_month_derived_summary"
    assert roles["Октябрь"] == "other_month_plan"
    assert not any("Tiggo 9" in (r.project_name or "") for r in a.records)     # October project not imported
    assert all(r.source_sheet == "Ноябрь" and r.extra_attributes["plan_month"] == "2026-11" for r in a.records)


def test_month_is_configurable_and_auto(wb):
    a = run(wb / "work_plan_2026.xlsx", month="2026-10")
    assert a.profile.analysis["source_sheet"] == "Октябрь"
    assert [r.project_name for r in a.records] == ["Chery Tiggo 9 Защита картера"]
    from plan_agent.profiles.monthly_plan import resolve_month
    assert resolve_month("auto", {}, today=date(2026, 11, 9)) == (2026, 11)
    assert resolve_month(None, {"target_month": "auto"}, today=date(2027, 1, 3)) == (2027, 1)


def test_two_november_plan_sheets_block_synchronisation(wb):
    a = run(wb / "two_novembers_2026.xlsx")
    assert a.profile.analysis["source_sheet"] is None
    assert "several worksheets look like the ноябрь plan" in a.profile.blocking[0]


def test_year_mismatch_is_flagged(wb):
    a = run(wb / "work_plan_2026.xlsx", month="2027-11")
    assert any("year" in b for b in a.profile.blocking)


def test_rows_hidden_by_autofilter_are_read(wb):
    a = run(wb / "work_plan_2026.xlsx")
    assert a.profile.analysis["hidden_task_rows"] > 0
    assert by_name(a, "GAC S9 Комплект защит")                                 # all its rows are hidden


def test_project_identity_cycles_and_sections(wb):
    a = run(wb / "work_plan_2026.xlsx")
    names = [r.project_name for r in a.records]
    assert names.count("Honda CR-V (1995-2001) Защита картера+КПП") == 1        # two cycles merged
    honda = by_name(a, "Honda CR-V (1995-2001) Защита картера+КПП")[0]
    assert any(i.code == "multiple_work_cycles" for i in honda.issues)
    shtamp = by_name(a, "Проверка, перевод штампованых защит")
    assert len(shtamp) == 2 and all(r.needs_review() for r in shtamp)          # no vehicle -> review, not merged
    ex = a.profile.analysis["excluded"]
    assert len(ex["extra_work"]) == 1 and len(ex["backlog"]) == 1 and len(ex["operational"]) == 1
    assert "Техподдержка" not in names


def test_stage_status_engineer_priority(wb):
    a = run(wb / "work_plan_2026.xlsx")
    gac = by_name(a, "GAC S9 Комплект защит")[0]
    assert [x.stage for x in gac.activities] == ["measurement", "documentation", "documentation", "documentation"]
    assert gac.current_stage == "measurement" and gac.project_status is None   # nothing logged -> unknown
    assert gac.assigned_engineer == "Иванов"                                   # designer, not the fit team
    assert gac.priority == "high" and gac.planned_end_date is None             # no deadlines invented
    belgee = by_name(a, "Belgee X80 (P145) Защита батареи")[0]
    assert belgee.article_number == "XX10TEST01"
    assert belgee.activities[0].stage == "documentation"                     # 1С task in a "Серия перевод" project
    assert belgee.activities[0].status == "done" and belgee.project_status == "in_progress"
    audi = by_name(a, "AUDI A5 long Комплект защит")[0]
    assert audi.assigned_engineer is None
    assert any(i.code == "engineer_placeholder" for i in audi.issues)
    honda = by_name(a, "Honda CR-V (1995-2001) Защита картера+КПП")[0]
    assert honda.priority == "low" and honda.current_stage == "development"


def test_original_file_not_modified_and_dashboard_flow(wb, backend, make_agent):
    src = wb / "work_plan_2026.xlsx"
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    agent = make_agent(backend)
    first = agent.receive(src, "cli", src.name, target_month="2026-11")
    assert first.status == "Approval required (dry run)"
    assert "Excel → dashboard field mapping" in first.analysis_markdown
    assert requests.get(backend + "/api/projects").json()["projects"] == []    # dry run wrote nothing
    agent.store.approve("pytest")

    synced = agent.receive(src, "cli", src.name, target_month="2026-11")
    c = synced.report["counts"]
    assert synced.status == "Completed" and c["sync_errors"] == 0
    created = requests.get(backend + "/api/projects").json()["projects"]
    assert len(created) == c["new"] and c["review"] >= 2
    gac = next(p for p in created if p["project_name"] == "GAC S9 Комплект защит")
    assert gac["stages"][0]["details"]["task_type"] == "Примерить"

    again = agent.receive(src, "cli", src.name, target_month="2026-11")
    assert again.report["counts"]["new"] == 0 and again.report["counts"]["updated"] == 0
    assert len(requests.get(backend + "/api/projects").json()["projects"]) == len(created)

    v2 = agent.receive(wb / "work_plan_2026_v2.xlsx", "cli", "work_plan_2026_v2.xlsx", target_month="2026-11")
    assert v2.status == "Completed"
    upd = {p["project"]: p for p in v2.report["projects"]}
    assert upd["GAC S9 Комплект защит"]["change_type"] == "update"
    assert upd["Lada Azimut ТСУ"]["change_type"] == "review"                   # unseen task 'Верификация'
    gac = requests.get(f"{backend}/api/projects/{gac['id']}").json()
    assert gac["current_stage"] == "documentation" and gac["project_status"] == "in_progress"
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
