"""Workflow automation: activity status, automatic stage assignment, priority, next action,
stage time, review queue. Unit tests of plan_agent/automation.py and integration tests against
the real Штурман server (synthetic data only)."""
import copy
import json
from datetime import date, timedelta

import pytest
import requests

import make_fixtures
from conftest import ROOT, ShturmanServer
from plan_agent import automation as A
from plan_agent.profiles.monthly_plan import MonthlyPlanProfile, TaskRow, load_profile

RULES = A.load_rules(ROOT / "config")
KEY = {"Authorization": "Bearer secret-test-key"}
TODAY = date(2026, 11, 17)


def act(key, atype, planned=8, actual=0, seq=1, cycle=1, task=None, **kw):
    a = {"activity_key": key, "activity_type": atype, "planned_hours": planned, "actual_hours": actual,
         "sequence": seq, "cycle": cycle, "task_type": task or atype, "plan_month": "2026-11", "source_row": 10 + seq}
    a.update(kw)
    return a


def norm(acts, dashboard=None, rules=RULES):
    dash = {a["activity_key"]: a for a in (dashboard or [])}
    for a in acts:
        a["normalized_status"], a["status_evidence"] = A.normalize_activity(a, dash.get(a["activity_key"]), rules)
    return acts


def rules_with(**changes):
    r = copy.deepcopy(RULES)
    for rid, mode in changes.items():
        next(x for x in r["stage_rules"] if x["id"] == rid)["mode"] = mode
    return r


# ====================================================================== 1. activity -> stage
@pytest.mark.parametrize("task,stage,group,expected", [
    ("Отсканировать", "Сканирование", "Сканирование", "scanning"),
    ("Разработать", "Разработка", "Разработка", "development"),
    ("Доработать", "Разработка", "Разработка", "development"),
    ("Заказать", "Разработка", "Разработка", "test_prototype"),
    ("Прочее", "Разработка", "Лабаратория, разбор опытников, фотоконтент", "test_prototype"),
    ("Примерить", "Примерка", "Замеры", "measurement"),
    ("КД", "Серия", "Разработка", "documentation"),
    ("1С", "Серия", "Разработка", "documentation"),
    ("Прочее", "Оформление", "Выгрузка в PDM", "documentation"),
    ("Расширение", "Серия перевод", "Перевод", "transfer_extension"),
    ("Сварить кофе", "Непонятно", "Неизвестно", None),          # unknown -> review (no rule)
])
def test_activity_to_stage_mapping(task, stage, group, expected):
    prof = MonthlyPlanProfile(load_profile(ROOT / "config"))
    row = TaskRow(row=5, section="main", hidden=False,
                  v={"task_type": task, "project_stage": stage, "task_group": group}, days={}, unmapped={})
    assert prof.map_activity(row)[0] == expected


# ====================================================================== 2-5. activity status
def test_planned_in_progress_and_unknown_statuses():
    p, ip, u = norm([act("a", "development", planned=8), act("b", "development", actual=3),
                     act("c", "development", planned=0, actual=0)])
    assert p["normalized_status"] == "planned" and p["status_evidence"]["source"] == "plan"
    assert ip["normalized_status"] == "in_progress" and "not completion" in ip["status_evidence"]["detail"]
    assert u["normalized_status"] == "unknown" and u["status_evidence"]["source"] == "none"


def test_completion_only_with_verified_evidence():
    # logged hours >= planned hours and ОК > 0 are NOT completion
    a = norm([act("a", "documentation", planned=4, actual=4, items_ok="1", items_nok=None)])[0]
    assert a["normalized_status"] == "in_progress"
    # a person marked it done in Штурман -> completed (manual evidence)
    dash = [{"activity_key": "a", "progress_status": "done", "field_sources": {"progress_status": "manual"}}]
    a = norm([act("a", "documentation", actual=4)], dash)[0]
    assert a["normalized_status"] == "completed" and a["status_evidence"]["source"] == "manual"
    # the agent's own value is not manual evidence
    dash[0]["field_sources"]["progress_status"] = "agent"
    assert norm([act("a", "documentation", actual=4)], dash)[0]["normalized_status"] == "in_progress"
    # a configured (approved) indicator would count - none is configured by default
    r = copy.deepcopy(RULES)
    r["activity_status"]["completion_indicators"] = [{"field": "items_ok", "op": ">=", "value": 1}]
    a = norm([act("a", "documentation", actual=4, items_ok="1")], rules=r)[0]
    assert a["normalized_status"] == "completed" and a["status_evidence"]["source"] == "indicator"
    assert RULES["activity_status"]["completion_indicators"] == []


def test_blocked_only_from_a_person_or_approved_indicator():
    a = norm([act("a", "measurement", actual=2, items_nok="3")])[0]
    assert a["normalized_status"] == "in_progress"                     # NOK is not interpreted
    dash = [{"activity_key": "a", "progress_status": "blocked", "field_sources": {"progress_status": "manual"}}]
    assert norm([act("a", "measurement", actual=2)], dash)[0]["normalized_status"] == "blocked"


# ====================================================================== 6-7, 9. stage decision
def test_single_active_stage_is_assigned_deterministically():
    acts = norm([act("s", "scanning", seq=1), act("d", "development", actual=5, seq=2),
                 act("p", "test_prototype", seq=3)])
    d = A.decide_stage(acts, RULES)
    assert d["type"] == "deterministic" and d["rule_id"] == "active_single_stage"
    assert d["proposed_stage"] == "development" and d["confidence"] == "verified"
    assert all(e["activity_type"] == "development" for e in d["evidence"])


def test_future_planned_work_is_only_a_proposal():
    d = A.decide_stage(norm([act("m", "measurement"), act("k", "documentation", seq=2)]), RULES)
    assert d["type"] == "proposed" and d["rule_id"] == "planned_sequence_earliest" and d["proposed_stage"] == "measurement"
    # planned_single_stage is APPROVED (assign) but marked as inferred from the plan, not verified
    d = A.decide_stage(norm([act("k", "documentation"), act("c", "documentation", seq=2)]), RULES)
    assert d["type"] == "deterministic" and d["rule_id"] == "planned_single_stage" and d["proposed_stage"] == "documentation"
    assert d["confidence"] == "inferred" and d["assignment_source"] == "inferred_from_plan"
    # switched back to propose, the same evidence only proposes
    d = A.decide_stage(norm([act("k", "documentation")]), rules_with(planned_single_stage="propose"))
    assert d["type"] == "proposed"


def test_month_plan_spanning_several_stages_is_not_one_stage():
    # a development cycle and a series cycle in the same month, nothing logged: not "one stage"
    acts = norm([act("d", "development", seq=1, cycle=1, task="Разработать"),
                 act("o", "test_prototype", seq=2, cycle=1, task="Заказать"),
                 act("k", "documentation", seq=3, cycle=2, task="КД"), act("c", "documentation", seq=4, cycle=2, task="1С")])
    d = A.decide_stage(acts, RULES)
    assert d["type"] == "proposed" and d["rule_id"] == "planned_sequence_earliest" and d["proposed_stage"] == "development"
    assert A.next_action(acts, d, None, RULES)["label"] == "Начать: Разработать"
    # an activity without hours in another stage also prevents the one-stage inference
    acts = norm([act("k", "documentation"), act("x", "measurement", planned=0, seq=2)])
    assert A.decide_stage(acts, RULES)["rule_id"] != "planned_single_stage"


def test_conflicting_stage_evidence_is_not_assigned():
    acts = norm([act("d", "development", actual=5), act("o", "test_prototype", actual=2, seq=2)])
    d = A.decide_stage(acts, RULES)
    assert d["type"] == "proposed" and d["rule_id"] == "active_multiple_stages" and d["proposed_stage"] == "prototype"


def test_master_switch_turns_assignment_off():
    r = copy.deepcopy(RULES)
    r["auto_assign_stage"] = False
    assert A.decide_stage(norm([act("d", "development", actual=5)]), r)["type"] == "proposed"


def test_repeated_cycles_use_the_latest_cycle():
    acts = norm([act("d1", "development", actual=10, cycle=1), act("p1", "test_prototype", actual=2, seq=2, cycle=1),
                 act("m1", "measurement", actual=1, seq=3, cycle=1),
                 act("d2", "development", actual=4, seq=4, cycle=2), act("p2", "test_prototype", seq=5, cycle=2)])
    d = A.decide_stage(acts, RULES)
    assert d["proposed_stage"] == "development" and d["cycle"] == 2 and d["type"] == "deterministic"


def test_no_main_stage_evidence_and_other_categories():
    assert A.decide_stage(norm([act("t", "transfer_extension", actual=2)]), RULES)["type"] == "none"
    assert A.decide_stage(norm([act("x", "development", actual=2)]), RULES, "additional_work")["type"] == "none"


def test_completed_stage_proposes_the_next_stage():
    dash = [{"activity_key": "d", "progress_status": "done", "field_sources": {"progress_status": "manual"}}]
    d = A.decide_stage(norm([act("d", "development", actual=10)], dash), RULES)
    assert d["type"] == "proposed" and d["rule_id"] == "completed_previous_stage" and d["proposed_stage"] == "prototype"


# ====================================================================== 13-15. priority
@pytest.mark.parametrize("raw,mapped", [("1", "high"), ("1.1", "high"), ("1.2", "high"), ("1.3", "high"),
                                        ("2", "medium"), ("3", "low"), ("0", None)])
def test_source_priority_mapping(raw, mapped):
    prof = load_profile(ROOT / "config")
    assert prof["priority_map"].get(raw) == mapped
    if raw == "0":
        assert "0" in prof["undefined_priority_values"]


def test_system_priority_only_from_approved_rules():
    p = A.compute_priority("normal", "2", {"planned_deadline": "2026-11-20"}, RULES, TODAY, [])
    assert p["system_priority"] is None and p["recommended_priority"] == "normal"
    assert any("не утверждены" in r for r in p["priority_reasons"]) and p["priority_reasons"][0].endswith("Средний")
    r = copy.deepcopy(RULES)
    r["priority"]["rules"] = [{"id": "soon", "type": "deadline_within_days", "days": 7, "raise_to": "high"},
                              {"id": "late", "type": "deadline_overdue", "raise_to": "critical"},
                              {"id": "meas", "type": "stage_days_exceeded", "stage": "measurement", "days": 30,
                               "raise_to": "high"}]
    p = A.compute_priority("normal", "2", {"planned_deadline": "2026-11-20"}, r, TODAY, [])
    assert p["system_priority"] == "high" and p["recommended_priority"] == "high"
    p = A.compute_priority("low", "3", {"planned_deadline": "2026-11-01"}, r, TODAY, [])
    assert p["system_priority"] == "critical"
    # stage time counts only with a CONFIRMED entry date
    proj = {"status": "measurement", "stage_entered_at": None}
    assert A.compute_priority("normal", "2", proj, r, TODAY, [])["system_priority"] is None
    proj["stage_entered_at"] = (TODAY - timedelta(days=40)).isoformat()
    assert A.compute_priority("normal", "2", proj, r, TODAY, [])["system_priority"] == "high"
    # a source priority higher than the system one is kept (effective = the higher one)
    assert A.compute_priority("high", "1", {"planned_deadline": "2026-11-20"}, r, TODAY, [])["recommended_priority"] == "high"
    # unspecified stays unspecified - never defaulted, a missing engineer never makes it critical
    p = A.compute_priority(None, "0", {}, RULES, TODAY, [])
    assert p["recommended_priority"] is None and "не определено" in p["priority_reasons"][0]


# ====================================================================== 17-18. next action
def test_next_action():
    acts = norm([act("d", "development", actual=5, task="Разработать"), act("o", "test_prototype", seq=2, task="Заказать")])
    n = A.next_action(acts, A.decide_stage(acts, RULES), None, RULES)
    assert n["label"] == "Продолжить: Разработать" and n["confidence"] == "verified"
    acts = norm([act("p", "measurement", task="Примерить")])
    n = A.next_action(acts, A.decide_stage(acts, RULES), None, RULES)
    assert n["label"] == "Начать: Примерить" and n["confidence"] == "planned"
    dash = [{"activity_key": "m", "progress_status": "done", "field_sources": {"progress_status": "manual"}}]
    acts = norm([act("m", "measurement", actual=3, task="Примерить")], dash)
    n = A.next_action(acts, {"proposed_stage": None}, "measurement", RULES)
    assert n["label"] == "Подготовить КД" and n["confidence"] == "verified"


def test_unknown_next_action_is_reported_not_invented():
    acts = norm([act("x", "development", planned=0, actual=0)])
    wf = A.build_workflow("main_project", acts, None, None, None, RULES, "2026-11", TODAY)
    assert wf["next_action"]["label"] == "Следующий шаг требует уточнения" and wf["next_action"]["confidence"] == "none"
    assert [f["code"] for f in wf["review_flags"]] == ["unknown_next_action"]


# ====================================================================== backend integration helpers
def mk(backend, ext, wf=None, **fields):
    project = {"external_project_id": ext, "record_category": "main_project", "manufacturer": "GAC", "vehicle_model": ext,
               "product_type": "Защита картера", "project_name": f"GAC {ext} Защита картера", "source_file": "plan.xlsx",
               "source_sheet": "Ноябрь", "source_rows": [12, 13]}
    project.update(fields)
    body = {"project": project, "sync": {"run_id": "r1", "source_file": "plan.xlsx"}}
    if wf:
        body["workflow"] = wf
    r = requests.post(f"{backend}/api/agent/projects", json=body, headers=KEY, timeout=10)
    assert r.status_code == 201, r.text
    return r.json()


def send(backend, pid, wf, run="r2"):
    r = requests.patch(f"{backend}/api/agent/projects/{pid}", json={"changes": {}, "workflow": wf,
                       "sync": {"run_id": run, "source_file": "plan.xlsx"}}, headers=KEY, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def wf(stage, kind="deterministic", rule="active_single_stage", **extra):
    w = {"version": 1, "plan_month": "2026-11", "source_priority": "normal", "source_priority_raw": "2",
         "system_priority": None, "recommended_priority": "normal", "priority_reasons": ["Исходный приоритет в плане: 2"],
         "stage_decision": {"type": kind, "rule_id": rule if kind != "none" else None, "proposed_stage": stage,
                            "confidence": "verified" if kind == "deterministic" else "probable",
                            "explanation": "test", "evidence": [{"activity_key": "a", "status": "in_progress"}]},
         "next_action": {"label": "Продолжить: Разработать", "source": "engineering_activity", "confidence": "verified",
                         "evidence": []},
         "review_flags": []}
    w.update(extra)
    return w


def get(backend, pid):
    return requests.get(f"{backend}/api/projects/{pid}", timeout=10).json()


def queue(backend, severity=None):
    q = f"?severity={severity}" if severity else ""
    return [i for i in requests.get(f"{backend}/api/review-queue{q}", timeout=10).json() if not i["resolved"]]


# ====================================================================== 6, 11. assignment + unknown entry date
def test_backend_assigns_deterministic_stage_without_inventing_entry_date(backend):
    p = mk(backend, "S1", wf("development"))
    assert p["status"] == "development" and p["stage_source"] == "agent_rule"
    assert p["stage_entered_at"] is None and p["days_in_stage"] is None
    assert p["stage_detected_at"] and p["stage_entry_confidence"] == "unknown"
    assert p["stage_decision"]["rule_id"] == "active_single_stage"
    h = [x for x in p["history"] if x["action"] == "stage_auto_assigned"]
    assert h and h[0]["new_value"]["stage"] == "development" and h[0]["new_value"]["evidence"]
    assert p["next_action"]["label"] == "Продолжить: Разработать"


def test_backend_refuses_unapproved_rules_and_invalid_blocks(backend):
    p = mk(backend, "S2", wf("measurement", rule="planned_sequence_earliest"))   # not approved for assignment
    assert p["status"] is None and p["planned_stage"] == "measurement"
    bad = wf("development")
    bad["stage_decision"]["proposed_stage"] = "invented_stage"
    r = requests.patch(f"{backend}/api/agent/projects/{p['id']}", json={"workflow": bad}, headers=KEY, timeout=10)
    assert r.status_code == 400
    r = requests.patch(f"{backend}/api/agent/projects/{p['id']}", json={"workflow": dict(wf("development"), hack=1)},
                       headers=KEY, timeout=10)
    assert r.status_code == 400 and "unsupported" in r.text


# ====================================================================== 7, 22. review queue
def test_unresolved_stages_go_to_review_and_are_resolved_by_confirmation(backend):
    p = mk(backend, "S3", wf("measurement", kind="proposed", rule="planned_sequence_earliest"))
    items = [i for i in queue(backend) if i["payload"].get("project_id") == p["id"]]
    assert [i["kind"] for i in items] == ["workflow_ambiguous_stage"] and items[0]["severity"] == "review"
    assert items[0]["payload"]["recommended_stage"] == "measurement" and items[0]["payload"]["source_rows"] == [12, 13]
    n = mk(backend, "S4", wf(None, kind="none"))
    assert any(i["kind"] == "workflow_stage_undetermined" and i["payload"]["project_id"] == n["id"] for i in queue(backend))
    # a person confirms the stage -> the item is resolved automatically
    requests.post(f"{backend}/api/projects/{p['id']}/stage", json={"stage": "measurement", "version": p["version"]})
    assert not [i for i in queue(backend) if i["payload"].get("project_id") == p["id"]]
    # repeated sync does not duplicate items
    for _ in range(2):
        send(backend, n["id"], wf(None, kind="none", next_action={"label": "x", "source": "none", "confidence": "none",
                                                                   "evidence": []}))
    assert len([i for i in queue(backend) if i["payload"].get("project_id") == n["id"]]) == 1


def test_warnings_are_separate_from_actionable_items(backend):
    p = mk(backend, "S5", wf("development", review_flags=[{"code": "unknown_next_action", "message": "?"}]))
    assert not [i for i in queue(backend) if i["payload"].get("project_id") == p["id"]]
    warn = [i for i in queue(backend, "all") if i["payload"].get("project_id") == p["id"]]
    assert [i["kind"] for i in warn] == ["workflow_unknown_next_action"] and warn[0]["severity"] == "warning"


# ====================================================================== 8, 12. manual stage and timer
def test_manual_stage_is_never_overwritten_and_timer_is_kept(backend):
    p = mk(backend, "S6")
    requests.post(f"{backend}/api/projects/{p['id']}/stage", json={"stage": "measurement", "version": p["version"]})
    confirmed = get(backend, p["id"])
    assert confirmed["stage_source"] == "manual" and confirmed["stage_entered_at"]
    after = send(backend, p["id"], wf("development"))
    assert after["status"] == "measurement" and after["stage_entered_at"] == confirmed["stage_entered_at"]
    assert after["stage_source"] == "manual"
    conflict = [i for i in queue(backend, "all") if i["payload"].get("project_id") == p["id"]]
    assert [i["kind"] for i in conflict] == ["workflow_manual_stage_conflict"] and conflict[0]["severity"] == "warning"
    # agent-assigned: repeated decisions for the same stage keep the detection time
    q = mk(backend, "S7", wf("development"))
    again = send(backend, q["id"], wf("development", next_action={"label": "Начать: Заказать", "source": "month_plan",
                                                                   "confidence": "planned", "evidence": []}))
    assert again["stage_detected_at"] == q["stage_detected_at"] and again["status"] == "development"


# ====================================================================== 10. rework / backward
def test_backward_transition_is_recorded_as_rework(backend):
    p = mk(backend, "S8", wf("measurement"))
    back = send(backend, p["id"], wf("development"))
    assert back["status"] == "development" and back["stage_source"] == "agent_rule"
    moves = [h for h in back["history"] if h["action"] in ("stage_auto_assigned", "stage_auto_changed")]
    assert [(m["previous_value"], m["new_value"]["stage"]) for m in moves] == [(None, "measurement"),
                                                                                ("measurement", "development")]
    assert moves[1]["new_value"]["direction"] == "backward"
    assert any(i["kind"] == "workflow_backward_transition" for i in queue(backend, "all")
               if i["payload"].get("project_id") == p["id"])
    fwd = send(backend, p["id"], wf("prototype"))                                       # and forward again
    assert fwd["status"] == "prototype" and len(fwd["history"]) > len(back["history"])


def test_backward_transition_can_be_disabled(tmp_path):
    cfg = json.loads((ROOT / "config" / "workflow_automation.json").read_text(encoding="utf-8"))
    cfg["allow_backward_transitions"] = False
    f = tmp_path / "rules.json"
    f.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    s = ShturmanServer(tmp_path / "srv", extra_env={"WORKFLOW_AUTOMATION_CONFIG": str(f)}).start()
    try:
        p = mk(s.url, "S9", wf("measurement"))
        assert send(s.url, p["id"], wf("development"))["status"] == "measurement"
    finally:
        s.stop()


# ====================================================================== 16. priorities
def test_effective_priority_and_manual_override(backend):
    p = mk(backend, "P1", wf("development"), priority="normal")
    assert (p["effective_priority"], p["priority_source"], p["source_priority"]) == ("normal", "source", "normal")
    esc = send(backend, p["id"], wf("development", system_priority="high", recommended_priority="high",
                                    priority_reasons=["срок наступает"]))
    assert (esc["effective_priority"], esc["priority_source"]) == ("high", "system")
    requests.patch(f"{backend}/api/projects/{p['id']}", json={"priority": "low", "version": esc["version"]})
    man = send(backend, p["id"], wf("development", system_priority="critical", recommended_priority="critical"))
    assert (man["effective_priority"], man["priority_source"]) == ("low", "manual")
    assert man["source_priority"] == "normal" and man["system_priority"] == "critical"


def test_manual_next_action_is_preserved(backend):
    p = mk(backend, "N1", wf("development"))
    requests.patch(f"{backend}/api/projects/{p['id']}", json={"next_action": "Ждём поставку стали", "version": p["version"]})
    after = send(backend, p["id"], wf("development", next_action={"label": "Начать: Заказать", "source": "month_plan",
                                                                   "confidence": "planned", "evidence": []}))
    assert after["next_action"]["label"] == "Ждём поставку стали" and after["next_action"]["source"] == "manual"


def test_attention_indicators_do_not_guess(backend):
    p = mk(backend, "A1", wf("development"))
    att = p["attention"]
    assert att["missing_engineer"] is True
    assert p["effective_priority"] == "normal"                 # a missing engineer never raises the priority
    assert att["no_recent_activity"] == {"configured": False} and att["stage_duration"]["configured"] is False
    assert att["confirmed_blocker"] is False and att["deadline_exceeded"] is False


# ====================================================================== 19-21, 23-24. full agent runs
def run_agent(make_agent, backend, path, month="2026-11"):
    agent = make_agent(backend)
    return agent.receive(path, "cli", path.name, target_month=month)


def test_agent_end_to_end_with_real_backend(tmp_path, backend, make_agent):
    src = tmp_path / "work_plan_2026.xlsx"
    make_fixtures.make_monthly_workbook(src, 4)          # GAC: hours logged on КД; Chery: only planned
    agent = make_agent(backend)
    agent.receive(src, "cli", src.name, target_month="2026-11")
    agent.store.approve("pytest")
    out = agent.receive(src, "cli", src.name, target_month="2026-11")
    assert out.status == "Completed", out.text
    ws = out.report["workflow_summary"]
    assert ws["stage_decisions"]["deterministic"] >= 1 and ws["stage_decisions"]["proposed"] >= 1
    db = requests.get(f"{backend}/api/projects", timeout=10).json()
    gac = next(p for p in db if p["project_name"] == "GAC S9 Комплект защит")
    assert gac["status"] == "documentation" and gac["stage_source"] == "agent_rule" and gac["stage_entered_at"] is None
    assert gac["next_action"]["label"] == "Продолжить: КД"
    assert {a["normalized_status"] for a in gac["activities"]} <= {"planned", "in_progress"}
    chery = next(p for p in db if p["project_name"] == "Chery Tiggo 4 Защита картера")
    # only planned development this month -> assigned, but visibly INFERRED from the plan
    assert chery["status"] == "development" and chery["stage_source"] == "inferred_from_plan"
    assert chery["stage_confidence"] == "inferred" and chery["stage_entered_at"] is None
    assert chery["next_action"]["label"] == "Начать: Разработать" and chery["next_action"]["confidence"] == "planned"
    honda = next(p for p in db if p["project_name"].startswith("Honda CR-V"))
    assert honda["status"] is None and honda["planned_stage"] == "development"      # month spans several stages
    # 19: workload preserved exactly
    assert gac["workload"]["planned_hours"] == 32 and gac["workload"]["actual_hours"] == 5
    # 20 + 23: repeated synchronisation - no duplicates, no new decisions, timer untouched
    again = agent.receive(src, "cli", src.name, target_month="2026-11")
    assert again.report["counts"]["new"] == 0 and again.report["counts"]["updated"] == 0, again.text
    db2 = requests.get(f"{backend}/api/projects", timeout=10).json()
    assert len(db2) == len(db)
    gac2 = next(p for p in db2 if p["id"] == gac["id"])
    assert gac2["stage_detected_at"] == gac["stage_detected_at"] and gac2["version"] == gac["version"]
    # a person marks the КД activity done -> the next sync re-decides from that verified evidence
    kd = next(a for a in gac2["activities"] if a["task_type"] == "КД")
    requests.patch(f"{backend}/api/projects/{gac['id']}/activities/{kd['id']}", json={"progress_status": "done"})
    third = agent.receive(src, "cli", src.name, target_month="2026-11")
    assert third.report["counts"]["updated"] >= 1
    gac3 = requests.get(f"{backend}/api/projects/{gac['id']}", timeout=10).json()
    kd3 = next(a for a in gac3["activities"] if a["id"] == kd["id"])
    assert kd3["normalized_status"] == "completed" and kd3["progress_status"] == "done"
    # no hours are logged any more -> only a proposal; the assigned stage is kept, never removed by weaker evidence
    assert gac3["status"] == "documentation" and gac3["stage_source"] == "agent_rule"
    assert gac3["stage_decision"]["type"] == "proposed"
    assert gac3["next_action"]["label"] == "Начать: 1С" and gac3["next_action"]["confidence"] == "planned"


def test_monthly_history_and_december_decision(tmp_path, backend, make_agent):
    src = tmp_path / "plan_dec_2026.xlsx"
    make_fixtures.make_monthly_workbook(src, 2, december=True)
    agent = make_agent(backend)
    agent.receive(src, "cli", src.name, target_month="2026-11")
    agent.store.approve("pytest")
    agent.receive(src, "cli", src.name, target_month="2026-11")
    dec = agent.receive(src, "cli", src.name, target_month="2026-12")
    assert dec.status == "Completed", dec.text
    gac = next(p for p in requests.get(f"{backend}/api/projects", timeout=10).json()
               if p["project_name"] == "GAC S9 Комплект защит")
    assert gac["workflow"]["plan_month"] == "2026-12"
    assert {a["plan_month"] for a in gac["activities"]} == {"2026-11", "2026-12"}       # November kept
    assert gac["status"] == "documentation"                                             # December: 1С hours logged
    moves = [h for h in gac["history"] if h["action"].startswith("stage_auto")]
    assert len(moves) == 1                                                              # same stage: no new transition


# ====================================================================== inferred_from_plan + basis change
def test_inferred_stage_is_distinguishable_and_upgraded_by_logged_work(backend):
    inferred = wf("documentation", rule="planned_single_stage")
    inferred["stage_decision"]["confidence"] = "inferred"
    p = mk(backend, "I1", inferred)
    assert p["status"] == "documentation" and p["stage_source"] == "inferred_from_plan"
    assert p["stage_confidence"] == "inferred" and p["stage_assigned_automatically"] is True
    assert p["stage_entered_at"] is None and p["verified_stage_entered_at"] is None
    # hours are now logged on the same stage: the basis becomes "verified", nothing else moves
    up = send(backend, p["id"], wf("documentation"))
    assert up["stage_source"] == "agent_rule" and up["stage_confidence"] == "verified"
    assert up["stage_detected_at"] == p["stage_detected_at"] and up["stage_entered_at"] is None
    assert [h["action"] for h in up["history"]].count("stage_basis_changed") == 1
    # a person confirms: "confirmed", the entry date is now verified
    requests.post(f"{backend}/api/projects/{p['id']}/stage", json={"stage": "measurement", "version": up["version"]})
    c = get(backend, p["id"])
    assert c["stage_source"] == "manual" and c["stage_confidence"] == "confirmed" and c["verified_stage_entered_at"]
    assert c["stage_assigned_automatically"] is False


# ====================================================================== contract fields for the UI
def test_api_contract_fields(backend):
    p = mk(backend, "C1", wf("development", review_flags=[{"code": "unknown_next_action", "message": "?"}]),
           assigned_engineer="Иванов", priority="normal")
    for k in ("status", "stage_source", "stage_confidence", "stage_detected_at", "verified_stage_entered_at",
              "stage_entered_at", "stage_decision", "source_priority", "system_priority", "effective_priority",
              "priority_source", "priority_reasons", "assigned_engineer", "next_action", "workload", "review_flags",
              "attention", "activities", "history"):
        assert k in p, k
    assert p["next_action"]["evidence"] == [] and "label" in p["next_action"] and "confidence" in p["next_action"]
    assert [f["code"] for f in p["review_flags"]] == ["unknown_next_action"] and p["review_flags"][0]["severity"] == "warning"
    assert set(p["workload"]) >= {"planned_hours", "actual_hours", "remaining_planned_hours", "by_employee", "by_month"}
    schema = requests.get(f"{backend}/api/agent/schema", headers=KEY, timeout=10).json()
    assert set(schema["workflow"]["assign_rules"]) == {"active_single_stage", "planned_single_stage"}


# ====================================================================== recalculation without the file
def test_recalculation_without_reimport(tmp_path, make_agent):
    cfg = json.loads((ROOT / "config" / "workflow_automation.json").read_text(encoding="utf-8"))
    cfg["priority"]["rules"] = [{"id": "deadline_soon", "type": "deadline_within_days", "days": 7, "raise_to": "high"}]
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    cfgdir = tmp_path / "cfg"
    import shutil
    shutil.copytree(ROOT / "config", cfgdir)
    (cfgdir / "workflow_automation.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    s = ShturmanServer(tmp_path / "srv", extra_env={"WORKFLOW_AUTOMATION_CONFIG": str(rules_file),
                                                     "PLAN_AGENT_CONFIG_DIR": str(cfgdir)}).start()
    try:
        src = tmp_path / "work_plan_2026.xlsx"
        make_fixtures.make_monthly_workbook(src, 4)
        agent = make_agent(s.url)
        agent.settings.config_dir = cfgdir
        agent.receive(src, "cli", src.name, target_month="2026-11")
        agent.store.approve("pytest")
        assert agent.receive(src, "cli", src.name, target_month="2026-11").status == "Completed"
        n_runs = len(requests.get(f"{s.url}/api/sync-runs", timeout=10).json())
        db = requests.get(f"{s.url}/api/projects", timeout=10).json()
        honda = next(p for p in db if p["project_name"].startswith("Honda CR-V"))
        gac = next(p for p in db if p["project_name"] == "GAC S9 Комплект защит")
        assert honda["effective_priority"] == "low" and honda["system_priority"] is None
        # a deadline is set in Штурман (manually) - no new Excel version
        soon = (date.today() + timedelta(days=3)).isoformat()
        requests.patch(f"{s.url}/api/projects/{honda['id']}", json={"planned_deadline": soon, "version": honda["version"]})
        # a person marks GAC's КД done
        kd = next(a for a in gac["activities"] if a["task_type"] == "КД")
        requests.patch(f"{s.url}/api/projects/{gac['id']}/activities/{kd['id']}", json={"progress_status": "done"})
        r = requests.post(f"{s.url}/api/plan-source/recalculate", json={}, timeout=180).json()
        assert r["run"]["outcome"] == "recalculated", r
        assert r["run"]["counts"]["workflow_updated"] >= 2 and r["run"]["phases"]["check"] == "not_run"
        h2 = requests.get(f"{s.url}/api/projects/{honda['id']}", timeout=10).json()
        assert (h2["system_priority"], h2["effective_priority"], h2["priority_source"]) == ("high", "high", "system")
        assert any("срок" in x for x in h2["priority_reasons"])
        g2 = requests.get(f"{s.url}/api/projects/{gac['id']}", timeout=10).json()
        assert g2["next_action"]["label"] == "Начать: 1С"
        assert next(a for a in g2["activities"] if a["id"] == kd["id"])["normalized_status"] == "completed"
        assert g2["status"] == gac["status"] and g2["stage_detected_at"] == gac["stage_detected_at"]   # timers kept
        assert g2["stage_entered_at"] == gac["stage_entered_at"]
        # no file was read: no new import run; repeating changes nothing
        assert len(requests.get(f"{s.url}/api/sync-runs", timeout=10).json()) == n_runs
        again = requests.post(f"{s.url}/api/plan-source/recalculate", json={}, timeout=180).json()
        assert again["run"]["counts"]["workflow_updated"] == 0 and again["run"]["counts"]["activities_updated"] == 0
    finally:
        s.stop()


def test_daily_recalculation_runs_on_weekends_without_file_check(tmp_path):
    from test_schedule import at, SAT, make_server, put, tick, imports, runs
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    s = make_server(tmp_path, inbox)
    try:
        put(inbox, 1)
        r = tick(s, at(SAT))
        assert r["action"] == "non_working" and r["recalc"] == "recalculated"
        assert imports(s) == []                                         # no file check on a non-working day
        second = tick(s, at(SAT, "10:00"))                              # same date again: nothing repeated
        assert second["action"] == "already_done" and second["recalc"] == "already_done"
        rec = [x for x in runs(s) if x["trigger"] == "recalc"]
        assert len(rec) == 1 and rec[0]["scheduled_date"] == SAT and rec[0]["phases"]["check"] == "not_run"
    finally:
        s.stop()


def test_additional_work_and_backlog_get_no_stage_review_items(backend):
    for cat, ext in (("additional_work", "AW-1"), ("backlog", "BL-1")):
        p = mk(backend, ext, wf(None, kind="none"), record_category=cat)
        assert p["status"] is None and p["stage_source"] is None
        assert not [i for i in queue(backend, "all") if i["payload"].get("project_id") == p["id"]
                    and i["kind"] in ("workflow_stage_undetermined", "workflow_ambiguous_stage")]
        assert p["next_action"]["label"]                       # the next action is still provided
    # a deterministic decision for a non-main record never places it on the board
    q = mk(backend, "AW-2", wf("development"), record_category="additional_work")
    assert q["status"] is None


def test_manual_choice_that_differs_from_the_agent_is_flagged_not_changed(backend):
    p = mk(backend, "D1", wf("measurement"))
    requests.post(f"{backend}/api/projects/{p['id']}/stage", json={"stage": "development", "version": p["version"]})
    c = get(backend, p["id"])
    assert c["status"] == "development" and c["stage_source"] == "manual"
    flags = [f for f in c["review_flags"] if f["code"] == "manual_stage_differs"]
    assert len(flags) == 1 and flags[0]["severity"] == "info" and "Замер" in flags[0]["reason"]
    # when the person agrees with the agent, there is no flag
    requests.post(f"{backend}/api/projects/{p['id']}/stage", json={"stage": "measurement", "version": c["version"]})
    assert not [f for f in get(backend, p["id"])["review_flags"] if f["code"] == "manual_stage_differs"]
