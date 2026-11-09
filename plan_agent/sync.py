"""Change planning and execution against the dashboard API."""
from __future__ import annotations

import logging
from typing import Optional

from .client import ApiError, AuthError, DashboardClient, DuplicateError, NetworkError, VersionConflict
from .config import OwnershipPolicy
from .matching import ProjectMatcher
from .models import ChangePlan, ChangeType, ProjectRecord
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
            if identity:
                plans.append(ChangePlan(rec, ChangeType.REVIEW, pid, m.project.get("version"), changes, conflicts,
                                        reason="identifier change: " + "; ".join(identity)))
                continue
            if rec.needs_review():
                plans.append(ChangePlan(rec, ChangeType.REVIEW, pid, m.project.get("version"), changes, conflicts,
                                        reason="record has ambiguities that need review (see issues)"))
                continue
            ctype = ChangeType.UPDATE if changes else ChangeType.UNCHANGED
            plans.append(ChangePlan(rec, ctype, pid, m.project.get("version"), changes, conflicts,
                                    reason=f"matched by {m.method}"))
            continue
        # new
        if rec.needs_review():
            plans.append(ChangePlan(rec, ChangeType.REVIEW, reason="new project with ambiguities (see issues)"))
            continue
        plans.append(ChangePlan(rec, ChangeType.NEW, reason=m.reason))
    return plans


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
        try:
            created = self.c.create_project(data, self.sync_meta)
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

    def _update(self, p: ChangePlan) -> None:
        changes = {c.field: c.new for c in p.changes}
        try:
            self.c.update_project(p.dashboard_id, changes, p.dashboard_version, self.sync_meta)
            p.result = "ok"
        except VersionConflict:
            current = self.c.get_project(p.dashboard_id)
            if all(current.get(k) == v for k, v in changes.items()):
                p.result = "ok"          # our retried request had already been applied
                return
            p.result = "failed"
            p.error = ("project changed in the dashboard while syncing; not overwritten - "
                       "will be re-evaluated on the next run")
