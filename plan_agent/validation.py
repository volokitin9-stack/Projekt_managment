"""Record-level validation.  Never raises; problems become Issues on the record
so one bad row cannot stop the rest of the file."""
from __future__ import annotations

import re

from .models import ProjectRecord, Severity

VALID_PRIORITIES = {"high", "medium", "low", None}
VALID_STATUSES = {"not_started", "in_progress", "on_hold", "completed", "cancelled", None}
ID_RX = re.compile(r"^[\w\-./#№ ]{1,64}$", re.U)


def validate(rec: ProjectRecord, stage_codes: list[str]) -> ProjectRecord:
    if rec.external_project_id is not None and not ID_RX.match(rec.external_project_id):
        rec.add_issue(Severity.ERROR, "invalid_project_id",
                      f"project identifier '{rec.external_project_id[:80]}' is not a valid identifier",
                      "external_project_id")
    if not rec.project_name:
        rec.add_issue(Severity.ERROR, "missing_name", "project has no name and none could be composed",
                      "project_name")
    already = any(i.code in ("no_vehicle", "incomplete_identity") for i in rec.issues)
    if not already and not (rec.external_project_id or rec.article_number or
                            (rec.manufacturer and rec.vehicle_model and rec.product_type)):
        rec.add_issue(Severity.REVIEW, "weak_identity",
                      "no project id, article number or full manufacturer/model/product combination; "
                      "matching would rely on the name only", "external_project_id")
    if rec.priority not in VALID_PRIORITIES:
        rec.add_issue(Severity.ERROR, "invalid_priority", f"priority {rec.priority!r}", "priority")
    if rec.project_status not in VALID_STATUSES:
        rec.add_issue(Severity.ERROR, "invalid_status", f"status {rec.project_status!r}", "project_status")
    if rec.current_stage is not None and rec.current_stage not in stage_codes \
            and not rec.current_stage.startswith("extra:"):
        rec.add_issue(Severity.ERROR, "invalid_stage", f"stage {rec.current_stage!r}", "current_stage")
    if rec.planned_start_date and rec.planned_end_date and rec.planned_start_date > rec.planned_end_date:
        rec.add_issue(Severity.WARNING, "date_order",
                      f"planned start {rec.planned_start_date} is after planned end {rec.planned_end_date}",
                      "planned_start_date")
    for a in rec.activities:
        if a.planned_start and a.planned_end and a.planned_start > a.planned_end:
            rec.add_issue(Severity.WARNING, "date_order",
                          f"activity '{a.name}': start {a.planned_start} after end {a.planned_end}")
    if not rec.planned_end_date:
        rec.add_issue(Severity.INFO, "missing_deadline", "no planned completion date in the file (kept unknown)",
                      "planned_end_date")
    if not rec.assigned_engineer:
        rec.add_issue(Severity.INFO, "missing_engineer", "no responsible engineer in the file (kept unknown)",
                      "assigned_engineer")
    return rec
