"""Summary of the workflow decisions of a run (stage, priority, next action)."""
from __future__ import annotations

from collections import Counter

from .models import ChangePlan, ChangeType


def summarize_workflow(plans: list[ChangePlan]) -> dict:
    main = [p for p in plans if p.workflow and (p.record.record_category or "main_project") == "main_project"]
    written = [p for p in main if p.change_type not in (ChangeType.REVIEW, ChangeType.DUPLICATE, ChangeType.ERROR)]
    dec = Counter()
    stage_assigned, stage_proposed = Counter(), Counter()
    rules, nexts, src, sysp, rec = Counter(), Counter(), Counter(), Counter(), Counter()
    missing_engineer, uncertain = [], []
    for p in written:
        wf = p.workflow
        d = wf["stage_decision"]
        dec[d["type"]] += 1
        rules[d.get("rule_id") or "none"] += 1
        (stage_assigned if d["type"] == "deterministic" else stage_proposed)[d.get("proposed_stage") or "—"] += 1
        nexts[wf["next_action"]["confidence"]] += 1
        src[wf.get("source_priority") or "unspecified"] += 1
        sysp[wf.get("system_priority") or "none"] += 1
        rec[wf.get("recommended_priority") or "unspecified"] += 1
        if not p.record.assigned_engineer:
            missing_engineer.append(p.record.project_name)
        if d["type"] != "deterministic":
            uncertain.append({"project": p.record.project_name, "decision": d["type"], "rule": d.get("rule_id"),
                              "proposed_stage": d.get("proposed_stage"), "why": d.get("explanation")})
    return {
        "main_projects_analyzed": len(main),
        "main_projects_written": len(written),
        "main_projects_in_review_queue": len(main) - len(written),
        "stage_decisions": dict(dec),
        "stage_rules": dict(rules),
        "stages_assigned_automatically": dict(stage_assigned),
        "stages_proposed_only": dict(stage_proposed),
        "next_action_confidence": dict(nexts),
        "source_priority": dict(src),
        "system_priority": dict(sysp),
        "recommended_priority": dict(rec),
        "missing_engineer": missing_engineer,
        "uncertain_stage_decisions": uncertain,
        "stage_entry_date_unknown": "all automatically assigned stages (the true entry date is not in the plan)",
    }
