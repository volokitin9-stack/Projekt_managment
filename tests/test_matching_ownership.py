from dataclasses import replace

from plan_agent.matching import ProjectMatcher
from plan_agent.models import ProjectRecord
from plan_agent.ownership import plan_field_changes

EXISTING = [
    {"id": 1, "external_project_id": "ENG-001", "article_number": "HV-JL-01", "manufacturer": "HAVAL",
     "vehicle_model": "Jolion", "product_type": "Защита картера", "project_name": "HAVAL Jolion Защита картера"},
    {"id": 2, "external_project_id": None, "article_number": None, "manufacturer": "HAVAL",
     "vehicle_model": "Jolion", "product_type": "Защита КПП", "project_name": "HAVAL Jolion Защита КПП"},
]


def R(**kw):
    return ProjectRecord(**kw)


def test_match_by_id_article_and_composite():
    m = ProjectMatcher(EXISTING)
    assert m.match(R(external_project_id="eng-001")).project["id"] == 1
    assert m.match(R(article_number="HV JL 01")).project["id"] == 1
    # Cyrillic/Latin look-alikes and spacing do not break the composite match
    r = m.match(R(manufacturer="НAVAL", vehicle_model="Jolion ", product_type="защита  кпп"))
    assert r.kind == "matched" and r.project["id"] == 2


def test_different_products_are_not_merged():
    m = ProjectMatcher(EXISTING)
    r = m.match(R(manufacturer="HAVAL", vehicle_model="Jolion", product_type="Защита бака",
                  project_name="HAVAL Jolion Защита бака"))
    assert r.kind in ("new", "duplicate")
    assert r.kind != "matched"


def test_fuzzy_similarity_only_produces_candidates():
    m = ProjectMatcher(EXISTING)
    r = m.match(R(project_name="HAVAL Jolion Защита картерa"))   # no ids, typo
    assert r.kind == "duplicate" and r.candidates[0]["id"] == 1


def test_conflicting_identifiers_need_review():
    m = ProjectMatcher(EXISTING)
    assert m.match(R(external_project_id="ENG-001", article_number="OTHER-9")).kind == "review"
    assert m.match(R(external_project_id="ENG-999", article_number="HV-JL-01")).kind == "review"


def test_empty_file_values_never_clear_dashboard(policy):
    existing = {"assigned_engineer": "Иванов", "planned_end_date": "2026-11-25", "field_sources": {}}
    ch, conf, _ = plan_field_changes(existing, {"assigned_engineer": None, "planned_end_date": "2026-11-30"},
                                     policy, {"assigned_engineer", "planned_end_date"})
    assert [(c.field, c.new) for c in ch] == [("planned_end_date", "2026-11-30")]


def test_manual_edit_kept_and_reported(policy):
    existing = {"assigned_engineer": "Морозов", "field_sources": {"assigned_engineer": "manual"}}
    ch, conf, _ = plan_field_changes(existing, {"assigned_engineer": "Иванов"}, policy, {"assigned_engineer"})
    assert ch == [] and conf[0].resolution == "kept_dashboard"
    over = replace(policy, on_manual_edit="overwrite_and_report")
    ch, conf, _ = plan_field_changes(existing, {"assigned_engineer": "Иванов"}, over, {"assigned_engineer"})
    assert ch[0].new == "Иванов" and conf[0].resolution == "overwritten"


def test_dashboard_owned_field_only_filled_when_empty(policy):
    ch, _, _ = plan_field_changes({"description": None}, {"description": "из плана"}, policy, {"description"})
    assert ch[0].new == "из плана"
    ch, conf, _ = plan_field_changes({"description": "ручное"}, {"description": "из плана"}, policy,
                                     {"description"})
    assert ch == [] and conf[0].resolution == "kept_dashboard"


def test_identity_change_requires_review(policy):
    _, _, ident = plan_field_changes({"vehicle_model": "Dargo"}, {"vehicle_model": "Jolion"}, policy,
                                     {"vehicle_model"})
    assert ident
    _, _, ident = plan_field_changes({"manufacturer": "Haval"}, {"manufacturer": "HAVAL"}, policy,
                                     {"manufacturer"})
    assert not ident     # case-only difference is not an identity change


def test_extra_attributes_are_merged_not_replaced(policy):
    ch, _, _ = plan_field_changes({"extra_attributes": {"manual_note": "x", "plan_comment": "old"}},
                                  {"extra_attributes": {"plan_comment": "new"}}, policy, {"extra_attributes"})
    assert ch[0].new == {"manual_note": "x", "plan_comment": "new"}


def test_row_move_alone_is_not_an_update(policy):
    ch, _, _ = plan_field_changes({"source_row": 9, "source_file": "a.xlsx"},
                                  {"source_row": 8, "source_file": "b.xlsx"}, policy, {"source_row", "source_file"})
    assert ch == []


def test_similar_names_with_different_complete_identity_are_not_candidates():
    m = ProjectMatcher([{"id": 9, "external_project_id": "PRJ-1", "article_number": None, "manufacturer": "AUDI",
                         "vehicle_model": "Q5", "product_type": "Комплект защит",
                         "project_name": "AUDI Q5 Комплект защит"}])
    r = m.match(R(manufacturer="AUDI", vehicle_model="A5 long", product_type="Комплект защит",
                  project_name="AUDI A5 long Комплект защит"))
    assert r.kind == "new"
