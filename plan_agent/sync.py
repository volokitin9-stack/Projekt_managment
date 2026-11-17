"""Change planning and execution against the dashboard API."""
from __future__ import annotations

import logging
from typing import Optional

from .client import ApiError, AuthError, DashboardClient, DuplicateError, NetworkError, VersionConflict
from .config import OwnershipPolicy
from .matching import ProjectMatcher
from .models import ChangePlan, ChangeType, Conflict, ProjectRecord
from .ownership import plan_field_changes
from . import automation

log = logging.getLogger("plan_agent.sync")


def _workflow(rec: ProjectRecord, project: Optional[dict], rules: Optional[dict]) -> Optional[dict]:
    """Stage / priority / next-action decision for a record (activities are normalised in place)."""
    if rules is None or rec.activity_payload is None:
        return None
    src = automation.SOURCE_TO_DASHBOARD_PRIORITY.get(rec.priority) if rec.priority else None
    return automation.build_workflow(rec.record_category or "main_project", rec.activity_payload, project, src,
                                     rec.extra_attributes.get("priority_original"), rules, rec.activity_month)


def _align(payload: dict, wf: Optional[dict]) -> None:
    """Keep the plain project fields consistent with the workflow decision."""
    if not wf:
        return
    stage = (wf.get("stage_decision") or {}).get("proposed_stage")
    if stage:
        payload["planned_stage"] = stage            # the suggestion shown on the card
    else:
        payload.pop("planned_stage", None)          # no usable evidence: keep what the dashboard has
    if wf.get("system_priority") and "priority" in payload:
        payload["priority"] = wf["recommended_priority"]   # approved escalation; a manual priority still wins


def build_plan(records: list[ProjectRecord], existing: list[dict], policy: OwnershipPolicy,
               writable: set[str], rules: Optional[dict] = None) -> list[ChangePlan]:
    """rules: config/workflow_automation.json; None = no workflow automation (generic files)."""
    matcher = ProjectMatcher(existing)
    plans: list[ChangePlan] = []
    claimed: dict = {}   # dashboard id -> record that already matched it in this run
    for rec in records:
        wf_new = _workflow(rec, None, rules) if rules is not None else None
        payload = {k: v for k, v in rec.to_payload().items() if k in writable}
        _align(payload, wf_new)
        if rec.has_errors():
            plans.append(ChangePlan(rec, ChangeType.ERROR, reason="record failed validation"))
            continue
        m = matcher.match(rec)
        if m.kind == "duplicate":
            plans.append(ChangePlan(rec, ChangeType.DUPLICATE, m.project and m.project.get("id"),
                                    candidates=m.candidates, reason=m.reason))
            continue
        if m.kind == "review":
            plans.append(ChangePlan(rec, ChangeType.REVIEW, m.project and m.project.get("id"),
                                    candidates=m.candidates, reason=m.reason))
            continue
        if m.kind == "matched":
            pid = m.project["id"]
            if pid in claimed:
                plans.append(ChangePlan(rec, ChangeType.DUPLICATE, pid,
                                        candidates=[{"id": pid, "also_matched_by": claimed[pid]}],
                                        reason="another record in this file matched the same dashboard project"))
                continue
            claimed[pid] = f"{rec.source_sheet}!{rec.source_row}"
            wf = _workflow(rec, m.project, rules) if rules is not None else None
            _align(payload, wf)
            changes, conflicts, identity = plan_field_changes(m.project, payload, policy, writable)
            older = bool(rec.last_plan_month and m.project.get("last_plan_month")
                         and rec.last_plan_month < m.project["last_plan_month"])
            if older and changes:
                conflicts.append(Conflict("*project fields*", m.project["last_plan_month"], rec.last_plan_month,
                                          "kept_dashboard", "file is an older plan month than the dashboard; only "
                                          "that month's activities are updated"))
                changes = []
            if older and wf:
                wf = None        # the stage/priority decision belongs to the latest plan month only
            act_changes, act_conflicts = diff_activities(m.project, rec)
            conflicts.extend(act_conflicts)
            if identity:
                plans.append(ChangePlan(rec, ChangeType.REVIEW, pid, m.project.get("version"), changes, conflicts,
                                        reason="identifier change: " + "; ".join(identity),
                                        activity_changes=act_changes))
                continue
            if rec.needs_review():
                plans.append(ChangePlan(rec, ChangeType.REVIEW, pid, m.project.get("version"), changes, conflicts,
                                        reason="record has ambiguities that need review (see issues)",
                                        activity_changes=act_changes))
                continue
            wf_changed = bool(wf) and automation.comparable(wf) != automation.comparable(m.project.get("workflow"))
            ctype = ChangeType.UPDATE if (changes or act_changes or wf_changed) else ChangeType.UNCHANGED
            plans.append(ChangePlan(rec, ctype, pid, m.project.get("version"), changes, conflicts,
                                    reason=f"matched by {m.method}" + ("; workflow decision changed" if wf_changed and
                                                                       not (changes or act_changes) else ""),
                                    activity_changes=act_changes, workflow=wf, workflow_changed=wf_changed))
            continue
        # new
        if rec.needs_review():
            plans.append(ChangePlan(rec, ChangeType.REVIEW, reason="new project with ambiguities (see issues)"))
            continue
        plans.append(ChangePlan(rec, ChangeType.NEW, reason=m.reason, workflow=wf_new, workflow_changed=bool(wf_new)))
    for p in plans:
        if p.workflow is None and rules is not None:
            p.workflow = wf_new_for(p.record, rules)
    return plans


def wf_new_for(rec: ProjectRecord, rules: dict) -> Optional[dict]:
    """Report-only decision for records that are not written (review, duplicates)."""
    return _workflow(rec, None, rules)


def restrict_for_auto(plans: list[ChangePlan], policy: OwnershipPolicy) -> list[ChangePlan]:
    """Automatic (unattended) mode: apply only safe changes, hold the rest for review.

    * new projects and projects whose activities were added or removed are not
      written at all (REVIEW);
    * for other updates, only the fields in policy.auto_sync_fields and the data
      of existing activities are applied; any other field change is withheld
      (not written) and, like conflicts with manual edits, sent to review.
    Other projects are processed normally."""
    allowed = set(policy.auto_sync_fields)
    for p in plans:
        if p.change_type == ChangeType.NEW:
            p.change_type = ChangeType.REVIEW
            p.reason = "automatic mode: new project - created only after manual approval"
            p.held_for_review = True
            continue
        if p.change_type not in (ChangeType.UPDATE, ChangeType.UNCHANGED):
            continue
        structural = [a for a in p.activity_changes if a.startswith(("+", "-"))]
        if p.change_type == ChangeType.UPDATE and structural:
            p.change_type = ChangeType.REVIEW
            p.reason = f"automatic mode: activities added/removed ({len(structural)}) - needs approval"
            p.held_for_review = True
            continue
        p.withheld = [c for c in p.changes if c.field not in allowed]
        p.changes = [c for c in p.changes if c.field in allowed]
        notes = []
        if p.withheld:
            notes.append("not applied, needs approval: " + ", ".join(sorted(c.field for c in p.withheld)))
        if p.conflicts and p.change_type == ChangeType.UPDATE:
            notes.append("manual edits kept: " + ", ".join(c.field for c in p.conflicts))
        if notes:
            p.held_for_review = True
            p.reason = "automatic mode: " + "; ".join(notes)
        if p.change_type == ChangeType.UPDATE and not p.changes and not p.activity_changes and not p.workflow_changed:
            p.change_type = ChangeType.REVIEW
    return plans


def diff_activities(existing: dict, rec: ProjectRecord) -> tuple[list[str], list[Conflict]]:
    """Compare the record's activities of its plan month with the dashboard's.
    Other months are never compared (their history is kept as is)."""
    if rec.activity_payload is None:
        return [], []
    month = rec.activity_month
    current = {a["activity_key"]: a for a in existing.get("activities") or [] if a.get("plan_month") == month}
    changes, conflicts = [], []
    keys = set()
    for a in rec.activity_payload:
        key = a["activity_key"]
        keys.add(key)
        cur = current.get(key)
        if cur is None:
            changes.append(f"+{key}")
            continue
        sources = cur.get("field_sources") or {}
        for f, v in a.items():
            if cur.get(f) == v:
                continue
            if sources.get(f) == "manual":
                conflicts.append(Conflict(f"activity[{key}].{f}", cur.get(f), v, "kept_dashboard",
                                          "activity field was edited manually in the dashboard"))
                continue
            changes.append(f"~{key}.{f}")
        if cur.get("removed_from_source"):
            changes.append(f"~{key}.removed_from_source")
    for key, cur in current.items():
        if key not in keys and not cur.get("removed_from_source"):
            changes.append(f"-{key}")
    return changes, conflicts


class SyncExecutor:
    """Executes a plan.  Each project is written in one API call, which the
    backend applies in a single transaction (project row + history rows), so a
    failure never leaves an individual project half-updated."""

    MAX_CONSECUTIVE_NETWORK_FAILURES = 3

    def __init__(self, client: DashboardClient, writable: set[str], run_id: str, source_file: str):
        self.c = client
        self.writable = writable
        self.sync_meta = {"run_id": run_id, "source_file": source_file, "actor": "plan_agent"}

    def execute(self, plans: list[ChangePlan]) -> Optional[str]:
        """Returns a fatal error message if the run had to be aborted."""
        net_failures = 0
        fatal = None
        for p in plans:
            if p.change_type not in (ChangeType.NEW, ChangeType.UPDATE):
                p.result = "skipped" if p.change_type != ChangeType.UNCHANGED else "ok"
                continue
            if fatal:
                p.result, p.error = "not_attempted", fatal
                continue
            try:
                if p.change_type == ChangeType.NEW:
                    self._create(p)
                else:
                    self._update(p)
                net_failures = 0
            except AuthError as e:
                fatal = f"authentication failed: {e}"
                p.result, p.error = "failed", str(e)
            except NetworkError as e:
                net_failures += 1
                p.result, p.error = "failed", str(e)
                if net_failures >= self.MAX_CONSECUTIVE_NETWORK_FAILURES:
                    fatal = "API unreachable (repeated network failures); remaining projects not attempted"
            except ApiError as e:
                p.result, p.error = "failed", str(e)
        return fatal

    # ------------------------------------------------------------------ #
    def _create(self, p: ChangePlan) -> None:
        data = {k: v for k, v in p.record.to_payload().items() if k in self.writable and v not in (None, [], {})}
        acts, scope = self._activities(p)
        try:
            created = self.c.create_project(data, self.sync_meta, acts, scope, workflow=p.workflow)
            p.dashboard_id = created.get("id")
            p.result = "ok"
        except DuplicateError as e:
            # Either a concurrent writer created it, or a retried request of
            # ours already succeeded.  Never create a second record: verify.
            ids = e.existing_ids
            if len(ids) == 1:
                existing = self.c.get_project(ids[0])
                if all(existing.get(k) == v for k, v in data.items() if k not in ("stages", "extra_attributes")):
                    p.dashboard_id, p.result = ids[0], "ok"
                    p.reason += " (already present - confirmed after retry)"
                    return
            p.change_type = ChangeType.DUPLICATE
            p.candidates = [{"id": i} for i in ids]
            p.result, p.error = "skipped", "backend reported an existing project with the same identity"

    @staticmethod
    def _activities(p: ChangePlan):
        if p.record.activity_payload is None:
            return None, None
        return p.record.activity_payload, {"plan_month": p.record.activity_month}

    def _update(self, p: ChangePlan) -> None:
        changes = {c.field: c.new for c in p.changes}
        acts, scope = self._activities(p) if p.activity_changes else (None, None)
        try:
            self.c.update_project(p.dashboard_id, changes, p.dashboard_version, self.sync_meta, acts, scope,
                                  workflow=p.workflow if p.workflow_changed else None)
            p.result = "ok"
        except VersionConflict:
            current = self.c.get_project(p.dashboard_id)
            if all(current.get(k) == v for k, v in changes.items()):
                p.result = "ok"          # our retried request had already been applied
                return
            p.result = "failed"
            p.error = ("project changed in the dashboard while syncing; not overwritten - "
                       "will be re-evaluated on the next run")
