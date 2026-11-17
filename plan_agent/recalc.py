"""Recalculation of workflow decisions WITHOUT reading the planning file.

Time passes and people mark activities done or blocked, but the Excel file may
not change. This re-decides every agent-managed project from the data already
stored in Штурман (activities of its latest plan month, manual marks, verified
stage-entry dates) with today's date, and sends only what changed:

* the workflow block (stage decision, priority with approved time rules, next
  action) when it differs;
* the normalised activity statuses when a manual mark changed them.

It never reads or re-imports the source file, never touches a confirmed stage,
and never resets a stage timer (the server applies the same rules as for a sync).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from . import automation
from .client import ApiError, DashboardClient, VersionConflict

log = logging.getLogger("plan_agent.recalc")


def recalculate(client: DashboardClient, config_dir, today=None) -> dict:
    rules = automation.load_rules(config_dir)
    schema = client.schema()
    if "workflow" not in schema.get("supports", []):
        return {"status": "not_supported", "error": "the backend does not support workflow decisions"}
    allowed = set(schema.get("activity_fields") or [])
    run_id = "recalc-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sync = {"run_id": run_id, "source_file": None, "actor": "plan_agent"}
    today = today or automation.local_today()
    out = {"run_id": run_id, "status": "Completed", "today": today.isoformat(), "projects_checked": 0,
           "workflow_updated": 0, "activities_updated": 0, "unchanged": 0, "conflicts": 0, "errors": []}
    for p in client.list_projects():
        old = p.get("workflow")
        if not old:
            continue                          # not managed by the planning agent
        month = p.get("last_plan_month") or old.get("plan_month")
        if not month:
            continue
        out["projects_checked"] += 1
        stored = [a for a in p.get("activities") or [] if a.get("plan_month") == month and not a.get("removed_from_source")]
        acts = [{k: v for k, v in a.items() if k in allowed} for a in stored]
        wf = automation.build_workflow(p.get("record_category") or "main_project", acts, p, old.get("source_priority"),
                                       old.get("source_priority_raw"), rules, month, today)
        wf_changed = automation.comparable(wf) != automation.comparable(old)
        acts_changed = any((a.get("normalized_status"), a.get("status_evidence")) !=
                           (s.get("normalized_status"), s.get("status_evidence")) for a, s in zip(acts, stored))
        if not (wf_changed or acts_changed):
            out["unchanged"] += 1
            continue
        try:
            client.update_project(p["id"], {}, p.get("version"), sync,
                                  activities=acts if acts_changed else None,
                                  activity_scope={"plan_month": month} if acts_changed else None,
                                  workflow=wf if (wf_changed or acts_changed) else None)
            out["workflow_updated"] += int(wf_changed)
            out["activities_updated"] += int(acts_changed)
        except VersionConflict:
            out["conflicts"] += 1             # changed meanwhile: decided again at the next recalculation
        except ApiError as exc:
            out["errors"].append(f"{p.get('project_name')}: {exc}")
    if out["errors"]:
        out["status"] = "Completed with errors"
    return out
