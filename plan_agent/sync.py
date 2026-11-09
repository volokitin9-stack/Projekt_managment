"""Change planning and execution against the dashboard API."""
from __future__ import annotations

import logging
from typing import Optional

from .client import ApiError, AuthError, DashboardClient, DuplicateError, NetworkError, VersionConflict
from .config import OwnershipPolicy
from .matching import ProjectMatcher
from .models import ChangePlan, ChangeType, Conflict, ProjectRecord
from .ownership import plan_field_changes

log = logging.getLogger("plan_agent.sync")


def build_plan(records: list[ProjectRecord], existing: list[dict], policy: OwnershipPolicy,
               writable: set[str]) -> list[ChangePlan]:
    matcher = ProjectMatcher(existing)
    plans: list[ChangePlan] = []
    claimed: dict = {}   # dashboard id -> record that already matched it in this run
    for rec in records:
        payload = {k: v for k, v in rec.to_payload().items() if k in writable}
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
            changes, conflicts, identity = plan_field_changes(m.project, payload, policy, writable)
            older = bool(rec.last_plan_month and m.project.get("last_plan_month")
                         and rec.last_plan_month < m.project["last_plan_month"])
            if older and changes:
                conflicts.append(Conflict("*project fields*", m.project["last_plan_month"], rec.last_plan_month,
                                          "kept_dashboard", "file is an older plan month than the dashboard; only "
                                          "that month's activities are updated"))
                changes = []
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
            ctype = ChangeType.UPDATE if (changes or act_changes) else ChangeType.UNCHANGED
            plans.append(ChangePlan(rec, ctype, pid, m.project.get("version"), changes, conflicts,
                                    reason=f"matched by {m.method}", activity_changes=act_changes))
            continue
        # new
        if rec.needs_review():
            plans.append(ChangePlan(rec, ChangeType.REVIEW, reason="new project with ambiguities (see issues)"))
            continue
        plans.append(ChangePlan(rec, ChangeType.NEW, reason=m.reason))
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
            created = self.c.create_project(data, self.sync_meta, acts, scope)
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
            self.c.update_project(p.dashboard_id, changes, p.dashboard_version, self.sync_meta, acts, scope)
            p.result = "ok"
        except VersionConflict:
            current = self.c.get_project(p.dashboard_id)
            if all(current.get(k) == v for k, v in changes.items()):
                p.result = "ok"          # our retried request had already been applied
                return
            p.result = "failed"
            p.error = ("project changed in the dashboard while syncing; not overwritten - "
                       "will be re-evaluated on the next run")
