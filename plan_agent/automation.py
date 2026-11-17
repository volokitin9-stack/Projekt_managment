"""Workflow automation: activity status, current stage, priority and next action.

Pure, deterministic functions. Every result carries the rule that produced it
and the evidence it is based on. Nothing is invented:

* hours logged = work in progress, never completion;
* "completed" / "blocked" only from a person's mark in Штурман or from a
  configured, approved indicator (none by default; ОК/NOK are not interpreted);
* a stage is ASSIGNED only by a rule whose mode is "assign" in
  config/workflow_automation.json; every other result is a PROPOSAL;
* priority escalation only by approved rules (none by default).

The backend validates and applies the result (see server/workflowModel.js):
the agent never writes a stage itself, and a manually confirmed stage is
never changed by the automation.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import DEFAULT_CONFIG_DIR

STAGES = ["scanning", "development", "test_prototype", "measurement", "documentation"]
TO_DASHBOARD = {"test_prototype": "prototype"}
FROM_DASHBOARD = {"prototype": "test_prototype"}
PRIORITY_RANK = {"critical": 4, "high": 3, "normal": 2, "low": 1}
PRIORITY_LABEL = {"critical": "Критично", "high": "Высокий", "normal": "Средний", "low": "Низкий"}
SOURCE_TO_DASHBOARD_PRIORITY = {"high": "high", "medium": "normal", "low": "low", "critical": "critical"}
STAGE_LABEL = {"scanning": "Сканирование", "development": "Разработка", "test_prototype": "Тестовый образец",
               "measurement": "Замер", "documentation": "Оформление"}


def load_rules(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    return json.loads((Path(config_dir) / "workflow_automation.json").read_text(encoding="utf-8"))


def dash_stage(stage: Optional[str]) -> Optional[str]:
    return TO_DASHBOARD.get(stage, stage) if stage else None


def agent_stage(stage: Optional[str]) -> Optional[str]:
    return FROM_DASHBOARD.get(stage, stage) if stage else None


# --------------------------------------------------------------------------- activity status
def _indicator(act: dict, ind: dict) -> bool:
    v = act.get(ind.get("field"))
    try:
        v = float(str(v).replace(",", ".")) if v is not None else None
    except ValueError:
        return False
    if v is None:
        return False
    target = float(ind.get("value", 0))
    return {">=": v >= target, ">": v > target, "==": v == target}.get(ind.get("op", ">="), False)


def normalize_activity(act: dict, dashboard_act: Optional[dict], rules: dict) -> tuple[str, dict]:
    """(status, evidence) - status: planned | in_progress | completed | blocked | unknown."""
    cfg = rules.get("activity_status", {})
    if dashboard_act and (dashboard_act.get("field_sources") or {}).get("progress_status") == "manual":
        manual = dashboard_act.get("progress_status")
        mapped = {"done": "completed", "blocked": "blocked", "in_progress": "in_progress", "planned": "planned"}.get(manual)
        if mapped:
            return mapped, {"source": "manual", "detail": f"set to «{manual}» by a person in Штурман"}
        if manual == "cancelled":
            return "unknown", {"source": "manual", "detail": "cancelled by a person in Штурман", "cancelled": True}
    for ind in cfg.get("completion_indicators") or []:
        if _indicator(act, ind):
            return "completed", {"source": "indicator", "detail": f"{ind.get('field')} {ind.get('op', '>=')} {ind.get('value')}"}
    for ind in cfg.get("blocker_indicators") or []:
        if _indicator(act, ind):
            return "blocked", {"source": "indicator", "detail": f"{ind.get('field')} {ind.get('op', '>=')} {ind.get('value')}"}
    actual = act.get("actual_hours") or 0
    planned = act.get("planned_hours") or 0
    if cfg.get("in_progress_if_actual_hours", True) and actual > 0:
        return "in_progress", {"source": "hours", "detail": f"{actual:g} h logged in {act.get('plan_month')} "
                                                            "(effort, not completion)"}
    if cfg.get("planned_if_planned_hours", True) and planned > 0:
        return "planned", {"source": "plan", "detail": f"{planned:g} h planned for {act.get('plan_month')}, none logged"}
    return "unknown", {"source": "none", "detail": "no planned or logged hours and no status mark"}


# --------------------------------------------------------------------------- stage decision
def _ev(a: dict) -> dict:
    return {"activity_key": a.get("activity_key"), "task": a.get("task_type") or a.get("name"),
            "activity_type": a.get("activity_type"), "status": a.get("normalized_status"),
            "evidence": (a.get("status_evidence") or {}).get("detail"), "source_row": a.get("source_row"),
            "cycle": a.get("cycle")}


def decide_stage(activities: list[dict], rules: dict, category: str = "main_project") -> dict:
    """activities: this month's activities with normalized_status / status_evidence set."""
    if category != "main_project":
        return {"type": "none", "rule_id": None, "proposed_stage": None, "confidence": "none",
                "explanation": "not a main engineering project (no Kanban stage)", "evidence": []}
    main = [a for a in activities if a.get("activity_type") in STAGES
            and not (a.get("status_evidence") or {}).get("cancelled")]
    if not main:
        return {"type": "none", "rule_id": None, "proposed_stage": None, "confidence": "none",
                "explanation": "no main-stage activity this month", "evidence": []}
    cycle = max(a.get("cycle") or 1 for a in main)
    cur = sorted([a for a in main if (a.get("cycle") or 1) == cycle], key=lambda a: a.get("sequence") or 0)
    by = lambda st: sorted({a["activity_type"] for a in cur if a["normalized_status"] in st}, key=STAGES.index)  # noqa: E731
    active, planned, done = by(("in_progress", "blocked")), by(("planned",)), by(("completed",))
    open_ = [a for a in cur if a["normalized_status"] not in ("completed",)]
    candidates = []
    if len(active) == 1:
        candidates.append(("active_single_stage", active[0], "verified",
                           f"work logged only on «{STAGE_LABEL[active[0]]}» activities"))
    elif len(active) > 1:
        candidates.append(("active_multiple_stages", active[-1], "probable",
                           "work logged on several stages: " + ", ".join(STAGE_LABEL[s] for s in active) +
                           "; the most advanced is proposed"))
    if not open_ and done:
        last = done[-1]
        nxt = STAGES[STAGES.index(last) + 1] if STAGES.index(last) + 1 < len(STAGES) else None
        if nxt:
            candidates.append(("completed_previous_stage", nxt, "probable",
                               f"all activities of the cycle are completed up to «{STAGE_LABEL[last]}»"))
    if not active:
        if len(planned) == 1:
            candidates.append(("planned_single_stage", planned[0], "probable",
                               f"no work logged yet; this month's plan covers only «{STAGE_LABEL[planned[0]]}»"))
        elif len(planned) > 1:
            candidates.append(("planned_sequence_earliest", planned[0], "low",
                               "no work logged yet; planned stages " + " → ".join(STAGE_LABEL[s] for s in planned) +
                               "; the earliest is proposed"))
    enabled = {r["id"]: r for r in rules.get("stage_rules", []) if r.get("enabled")}
    for rule_id, stage, confidence, why in candidates:
        r = enabled.get(rule_id)
        if not r:
            continue
        assign = r.get("mode") == "assign" and rules.get("auto_assign_stage", False)
        ev = [_ev(a) for a in cur if a["activity_type"] == stage] or [_ev(a) for a in cur]
        return {"type": "deterministic" if assign else "proposed", "rule_id": rule_id,
                "proposed_stage": dash_stage(stage), "confidence": "verified" if assign else confidence,
                "explanation": why, "evidence": ev[:12], "cycle": cycle}
    return {"type": "none", "rule_id": None, "proposed_stage": None, "confidence": "none",
            "explanation": "the activities give no usable stage evidence (statuses unknown)",
            "evidence": [_ev(a) for a in cur][:12], "cycle": cycle}


# --------------------------------------------------------------------------- priority
def _parse_day(v) -> Optional[date]:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def compute_priority(source_priority: Optional[str], source_raw: Any, project: Optional[dict], rules: dict,
                     today: date, activities: list[dict]) -> dict:
    """source_priority in dashboard vocabulary (high/normal/low) or None."""
    reasons = []
    if source_raw not in (None, "", []):
        reasons.append(f"Исходный приоритет в плане: {source_raw}" +
                       (f" → {PRIORITY_LABEL[source_priority]}" if source_priority else " (значение не определено)"))
    else:
        reasons.append("Исходный приоритет в плане не указан")
    cfg = (rules.get("priority") or {}).get("rules") or []
    raised = []
    project = project or {}
    deadline = _parse_day(project.get("planned_deadline"))
    for r in cfg:
        t, to = r.get("type"), r.get("raise_to")
        if to not in PRIORITY_RANK:
            continue
        if t == "deadline_within_days" and deadline and 0 <= (deadline - today).days <= int(r.get("days", 0)):
            raised.append((to, f"срок {deadline.isoformat()} наступает через {(deadline - today).days} дн. (правило {r.get('id')})"))
        elif t == "deadline_overdue" and deadline and deadline < today:
            raised.append((to, f"срок {deadline.isoformat()} прошёл (правило {r.get('id')})"))
        elif t == "stage_days_exceeded":
            entered = _parse_day(project.get("stage_entered_at"))
            if entered and agent_stage(project.get("status")) == r.get("stage") and (today - entered).days > int(r.get("days", 0)):
                raised.append((to, f"на этапе «{STAGE_LABEL.get(r.get('stage'), r.get('stage'))}» "
                                   f"{(today - entered).days} дн. (подтверждённая дата входа; правило {r.get('id')})"))
        elif t == "verified_blocker" and any(a.get("normalized_status") == "blocked" and
                                             (a.get("status_evidence") or {}).get("source") in ("manual", "indicator")
                                             for a in activities):
            raised.append((to, f"подтверждённая блокировка работы (правило {r.get('id')})"))
    system = max((p for p, _ in raised), key=PRIORITY_RANK.get) if raised else None
    reasons.extend(why for _, why in raised)
    if not cfg:
        reasons.append("Правила повышения приоритета не утверждены — используется исходный приоритет")
    elif not raised:
        reasons.append("Ни одно утверждённое правило повышения приоритета не сработало")
    cands = [p for p in (source_priority, system) if p]
    recommended = max(cands, key=PRIORITY_RANK.get) if cands else None
    return {"source_priority": source_priority, "source_priority_raw": source_raw, "system_priority": system,
            "recommended_priority": recommended, "priority_reasons": reasons}


# --------------------------------------------------------------------------- next action
def next_action(activities: list[dict], decision: dict, confirmed_stage: Optional[str], rules: dict,
                category: str = "main_project") -> dict:
    cfg = rules.get("next_action", {})
    unknown = {"label": cfg.get("unknown_label", "Следующий шаг требует уточнения"), "source": "none",
               "confidence": "none", "evidence": []}
    usable = [a for a in activities if not (a.get("status_evidence") or {}).get("cancelled")]
    if category == "main_project":
        stage = agent_stage(confirmed_stage) or agent_stage(decision.get("proposed_stage"))
        cycle = decision.get("cycle") or max([a.get("cycle") or 1 for a in usable] or [1])
        usable = [a for a in usable if (a.get("cycle") or 1) == cycle]
        scope = [a for a in usable if a.get("activity_type") == stage] if stage else []
    else:
        stage, scope = None, usable
    scope = sorted(scope, key=lambda a: a.get("sequence") or 0)
    generic = {str(x).strip().lower() for x in cfg.get("generic_task_names") or []}

    def task(a: dict) -> str:
        t = a.get("task_type") or a.get("name") or "работа"
        if str(t).strip().lower() in generic and a.get("task_group"):
            return a["task_group"]
        return t
    active = [a for a in scope if a["normalized_status"] in ("in_progress",)]
    if active:
        return {"label": cfg.get("continue_template", "Продолжить: {task}").format(task=task(active[0])),
                "source": "engineering_activity", "confidence": "verified", "evidence": [_ev(active[0])]}
    if scope and all(a["normalized_status"] == "completed" for a in scope) and stage:
        label = (cfg.get("after_stage_completed") or {}).get(stage)
        if label:
            return {"label": label, "source": "workflow", "confidence": "verified", "evidence": [_ev(a) for a in scope][:6]}
    planned = [a for a in scope if a["normalized_status"] == "planned"]
    if not planned and category == "main_project" and stage in STAGES:
        later = [a for a in usable if a.get("activity_type") in STAGES and a["normalized_status"] == "planned"
                 and STAGES.index(a["activity_type"]) >= STAGES.index(stage)]
        planned = sorted(later, key=lambda a: (STAGES.index(a["activity_type"]), a.get("sequence") or 0))
    if planned:
        return {"label": cfg.get("start_template", "Начать: {task}").format(task=task(planned[0])),
                "source": "month_plan", "confidence": "planned", "evidence": [_ev(planned[0])]}
    return unknown


# --------------------------------------------------------------------------- assembly
def build_workflow(category: str, activities: list[dict], dashboard_project: Optional[dict], source_priority: Optional[str],
                   source_raw: Any, rules: dict, plan_month: Optional[str], today: Optional[date] = None) -> dict:
    """Normalise the activities IN PLACE (normalized_status, status_evidence) and return the workflow block."""
    today = today or datetime.now(timezone.utc).date()
    dash_acts = {a.get("activity_key"): a for a in (dashboard_project or {}).get("activities") or []
                 if a.get("plan_month") == plan_month}
    for a in activities:
        a["normalized_status"], a["status_evidence"] = normalize_activity(a, dash_acts.get(a.get("activity_key")), rules)
    decision = decide_stage(activities, rules, category)
    confirmed = (dashboard_project or {}).get("status")
    prio = compute_priority(source_priority, source_raw, dashboard_project, rules, today, activities)
    nxt = next_action(activities, decision, confirmed, rules, category)
    flags = []
    if category == "main_project" and nxt["source"] == "none":
        flags.append({"code": "unknown_next_action", "message": "следующий шаг не удалось определить по данным плана"})
    return {"version": 1, "plan_month": plan_month, "stage_decision": decision, **prio, "next_action": nxt,
            "review_flags": flags}


def comparable(wf: Optional[dict]) -> Optional[str]:
    """What must change for the backend to need an update (timestamps and wording of evidence excluded)."""
    if not wf:
        return None
    d = wf.get("stage_decision") or {}
    n = wf.get("next_action") or {}
    return json.dumps([d.get("type"), d.get("rule_id"), d.get("proposed_stage"), d.get("confidence"),
                       wf.get("source_priority"), wf.get("system_priority"), wf.get("recommended_priority"),
                       n.get("label"), n.get("confidence"), sorted(f["code"] for f in wf.get("review_flags") or [])],
                      ensure_ascii=False)
