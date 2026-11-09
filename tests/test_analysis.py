"""Structure recognition, extraction and workflow interpretation."""
import pytest

from plan_agent.models import Layout, Severity
from plan_agent.pipeline import analyze_file


@pytest.fixture(scope="module")
def wide(fixtures_dir, vocab):
    return analyze_file(fixtures_dir / "engineering_plan_v1.xlsx", vocab)


@pytest.fixture(scope="module")
def long_(fixtures_dir, vocab):
    return analyze_file(fixtures_dir / "engineering_plan_long.csv", vocab)


def rec(result, ext_id):
    return next(r for r in result.records if r.external_project_id == ext_id)


def test_sheets_and_header_detection(wide):
    plan, ref = wide.schemas
    assert plan.relevant and not ref.relevant
    t = wide.parsed.tables[0]
    assert [t.row_numbers[i] for i in plan.header_rows] == [4, 5]      # title rows skipped, 2-row header
    roles = {c.header: c.role for c in plan.columns}
    assert roles["Сканирование / План"] == "stage:scanning:planned_end"
    assert roles["Сканирование / Факт"] == "stage:scanning:actual"
    assert roles["КД / Факт"] == "stage:documentation:actual"
    assert roles["Конструктор"] == "engineer"
    assert roles["Покраска"] is None                                    # unknown -> preserved, not guessed
    assert plan.layout == Layout.WIDE
    assert plan.group_levels_role == ["manufacturer"]


def test_projects_identified_and_hierarchy(wide):
    assert len(wide.records) == 6                                       # totals row & group rows excluded
    r = rec(wide, "ENG-002")
    assert (r.manufacturer, r.vehicle_model, r.product_type) == ("HAVAL", "Jolion", "Защита КПП")
    assert rec(wide, "ENG-004").manufacturer == "CHERY"
    # two products on the same vehicle are separate projects
    assert rec(wide, "ENG-001").product_type != rec(wide, "ENG-002").product_type


def test_stage_derivation(wide):
    r = rec(wide, "ENG-001")       # scanning + development done (actual dates), prototype not
    assert r.current_stage == "test_prototype"
    assert r.project_status == "in_progress"
    assert r.planned_end_date == "2026-11-25"
    assert rec(wide, "ENG-003").project_status == "completed"
    assert rec(wide, "ENG-003").actual_completion_date == "2026-08-05"


def test_passed_deadline_does_not_mean_complete(wide):
    # ENG-002: development planned 2026-10-01 (in the past), no actual date
    r = rec(wide, "ENG-002")
    assert r.current_stage == "development"
    assert r.project_status != "completed"
    dev = next(a for a in r.activities if a.stage == "development")
    assert dev.status is None and dev.actual_end is None


def test_missing_facts_stay_unknown(wide):
    r = rec(wide, "ENG-004")       # only plan dates, no status, no article
    assert r.current_stage is None and r.project_status is None and r.article_number is None
    assert rec(wide, "ENG-005").assigned_engineer is None
    assert rec(wide, "ENG-005").priority is None


def test_invalid_date_and_unmapped_column_preserved(wide):
    r = rec(wide, "ENG-005")
    assert any(i.code == "invalid_date" for i in r.issues)
    assert r.extra_attributes["invalid_values"]["development:planned_end"] == "32.13.2026"
    assert r.extra_attributes["unmapped_columns"]["Покраска"] == "29.10.2026"


def test_conflicting_status_goes_to_review(wide):
    r = rec(wide, "ENG-006")       # "Выполнено" but only scanning is done
    assert r.needs_review()
    assert any(i.code == "status_conflict" and i.severity == Severity.REVIEW for i in r.issues)


def test_comments_notes_dependencies(wide):
    r1 = rec(wide, "ENG-001")
    assert r1.extra_attributes["plan_comment"] == "Ждём крепёж"
    assert "Иванов в отпуске с 1.12" in r1.extra_attributes["cell_notes"]
    assert rec(wide, "ENG-002").dependencies == [{"raw": "ENG-001", "resolved": "ENG-001"}]


def test_long_layout(long_):
    s = long_.schemas[0]
    assert s.layout == Layout.LONG
    assert len(long_.records) == 2
    p10 = rec(long_, "PRJ-10")
    assert (p10.manufacturer, p10.vehicle_model) == ("GEELY", "Monjaro")   # split by known manufacturer list
    stages = [a.stage for a in p10.activities]
    assert stages == ["scanning", "development", "test_prototype", "extra:approval"]   # extra stage kept
    assert p10.current_stage == "development"
    assert p10.assigned_engineer == "Кузнецов"
    assert p10.planned_start_date == "2026-10-01"
    p11 = rec(long_, "PRJ-11")
    unknown = next(a for a in p11.activities if a.name == "Термоформовка")
    assert unknown.stage is None and not unknown.stage_mapped          # preserved, not dropped
    assert p11.needs_review()


def test_row_without_identity_does_not_break_file(tmp_path, vocab):
    p = tmp_path / "plan.csv"
    p.write_text("ID;Марка;Модель;Изделие;Конструктор;Срок\n"
                 "A-1;HAVAL;Jolion;Защита картера;Иванов;25.11.2026\n"
                 ";;;;Петров;01.12.2026\n"
                 "A-2;CHERY;Tiggo 4;Пороги;Сидоров;01.12.2026\n", encoding="utf-8")
    r = analyze_file(p, vocab)
    assert [x.external_project_id for x in r.records] == ["A-1", "A-2"]
    assert len(r.row_errors) == 1 and r.row_errors[0]["row"] == 3


def test_english_flat_plan(tmp_path, vocab):
    p = tmp_path / "plan.csv"
    p.write_text("Project ID,Make,Model,Product,Engineer,Priority,Stage,Status,Deadline\n"
                 "ENG-001,HAVAL,Jolion,Engine protection,Ivanov,High,CAD modeling,In progress,2026-11-25\n"
                 "ENG-002,HAVAL,Jolion,Transmission protection,Ivanov,1,Prototype production,On hold,2026-12-01\n",
                 encoding="utf-8")
    r = analyze_file(p, vocab)
    a, b = r.records
    assert (a.current_stage, a.project_status, a.priority) == ("development", "in_progress", "high")
    assert (b.current_stage, b.project_status) == ("test_prototype", "on_hold")
    assert b.priority == "high" and b.extra_attributes["priority_original"] == "1"
