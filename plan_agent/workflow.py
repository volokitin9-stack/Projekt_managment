"""Engineering workflow mapping.

Maps the department's own vocabulary to the dashboard workflow
(scanning -> development -> test_prototype -> measurement -> documentation)
and derives a project's current stage / overall status from its activities.

Rules (from the specification, enforced here):
* never fabricate: unknown stays ``None``;
* a passed planned date never marks anything as complete - only an explicit
  status or an actual-completion date does;
* unmappable stage names are preserved (stage=None, stage_mapped=False) and
  flagged for review;
* additional recognised stages are preserved as ``extra:<code>``.
"""
from __future__ import annotations

import re
from typing import Optional

from .config import Vocabulary
from .models import Activity, ProjectRecord, Severity
from .normalize import clean_text, norm_header

DONE, IN_PROGRESS, PENDING, BLOCKED, CANCELLED = "done", "in_progress", "pending", "blocked", "cancelled"


class WorkflowMapper:
    def __init__(self, vocab: Vocabulary, approved_values: Optional[dict] = None):
        """``approved_values`` comes from the approved mapping config and
        overrides vocabulary matching, e.g. {"stage": {"обмер": "measurement"}}."""
        self.v = vocab
        self.overrides = {k: {norm_header(a): b for a, b in (vals or {}).items()}
                          for k, vals in (approved_values or {}).items()}
        self.observed: dict[str, dict[str, Optional[str]]] = {"stage": {}, "status": {}, "priority": {}}

    def _override(self, kind: str, raw: str):
        table = self.overrides.get(kind) or {}
        key = norm_header(raw)
        if key in table:
            return True, table[key]
        return False, None

    # ------------------------------------------------------------------ #
    def map_stage(self, raw) -> tuple[Optional[str], bool]:
        """Return (stage_code, mapped).  stage_code may be 'extra:<code>'."""
        text = clean_text(raw)
        if text is None:
            return None, False
        hit, val = self._override("stage", text)
        if hit:
            self.observed["stage"][text] = val
            return val, val is not None
        from .schema_detection import SchemaDetector  # reuse the stage matcher
        stage, _ = SchemaDetector(self.v).match_stage(text)
        self.observed["stage"][text] = stage
        return stage, stage is not None

    def map_status(self, raw) -> Optional[str]:
        text = clean_text(raw)
        if text is None:
            return None
        hit, val = self._override("status", text)
        if hit:
            self.observed["status"][text] = val
            return val
        m = re.fullmatch(r"(\d{1,3})(?:[.,]\d+)?\s*%", text)
        if m or isinstance(raw, float) and 0 <= raw <= 1 and not float(raw).is_integer():
            pct = int(m.group(1)) if m else round(raw * 100)
            status = DONE if pct >= 100 else PENDING if pct == 0 else IN_PROGRESS
        else:
            status, _, _ = self.v.statuses.match(text)
        self.observed["status"][text] = status
        return status

    def map_priority(self, raw) -> tuple[Optional[str], bool]:
        """Return (priority, numeric_assumption_used)."""
        text = clean_text(raw)
        if text is None:
            return None, False
        hit, val = self._override("priority", text)
        if hit:
            self.observed["priority"][text] = val
            return val, False
        if re.fullmatch(r"\d+", text):
            val = self.v.numeric_priority.get(int(text))
            self.observed["priority"][text] = val
            return val, True
        val, _, _ = self.v.priorities.match(text, allow_partial=False)
        if val is None:
            val, _, _ = self.v.priorities.match(text)
        self.observed["priority"][text] = val
        return val, False

    # ------------------------------------------------------------------ #
    def stage_index(self, stage: Optional[str]) -> int:
        try:
            return self.v.stage_order.index(stage)
        except ValueError:
            return -1

    def derive(self, rec: ProjectRecord, explicit_stage_raw=None, explicit_status_raw=None) -> None:
        """Fill current_stage / project_status / dates from activities + explicit columns."""
        derived: list[str] = []
        main = [a for a in rec.activities if a.stage in self.v.stage_order]
        main.sort(key=lambda a: self.stage_index(a.stage))

        # ---- explicit project-level status -------------------------------
        explicit_status = self.map_status(explicit_status_raw) if explicit_status_raw is not None else None
        if explicit_status_raw is not None and clean_text(explicit_status_raw) and explicit_status is None:
            rec.add_issue(Severity.REVIEW, "unmapped_status",
                          f"project status '{clean_text(explicit_status_raw)}' could not be interpreted; "
                          "original value preserved", "project_status")
            rec.extra_attributes["status_original"] = clean_text(explicit_status_raw)

        # ---- explicit current stage --------------------------------------
        explicit_stage = None
        if explicit_stage_raw is not None and clean_text(explicit_stage_raw):
            explicit_stage, ok = self.map_stage(explicit_stage_raw)
            if not ok:
                rec.add_issue(Severity.REVIEW, "unmapped_stage",
                              f"stage '{clean_text(explicit_stage_raw)}' does not correspond to a known "
                              "workflow stage; original value preserved", "current_stage")
                rec.extra_attributes["stage_original"] = clean_text(explicit_stage_raw)

        # ---- stage derived from activities -------------------------------
        derived_stage = None
        if main:
            active = [a for a in main if a.status in (IN_PROGRESS, BLOCKED)]
            unfinished = [a for a in main if a.status not in (DONE, CANCELLED)]
            if active:
                derived_stage = active[-1].stage
            elif unfinished:
                derived_stage = unfinished[0].stage
            else:
                derived_stage = main[-1].stage
        if explicit_stage and explicit_stage in self.v.stage_order:
            rec.current_stage = explicit_stage
            if derived_stage and derived_stage != explicit_stage and any(a.status for a in main):
                rec.add_issue(Severity.WARNING, "stage_conflict",
                              f"explicit stage '{explicit_stage}' differs from stage implied by activity "
                              f"statuses '{derived_stage}'; explicit value used", "current_stage")
        elif explicit_stage and explicit_stage.startswith("extra:"):
            rec.current_stage = explicit_stage
        elif derived_stage and any(a.status or a.actual_end for a in main):
            rec.current_stage = derived_stage
            derived.append("current_stage")

        # ---- overall project status --------------------------------------
        derived_status = None
        known = [a for a in main if a.status]
        if main and known:
            if all(a.status == CANCELLED for a in main):
                derived_status = "cancelled"
            elif len(known) == len(main) and all(a.status in (DONE, CANCELLED) for a in main):
                derived_status = "completed"
            elif any(a.status == BLOCKED for a in main if a.stage == rec.current_stage):
                derived_status = "on_hold"
            elif any(a.status in (DONE, IN_PROGRESS) for a in main):
                derived_status = "in_progress"
            elif all(a.status == PENDING for a in main):
                derived_status = "not_started"
        if explicit_status:
            rec.project_status = self.v.project_status_map.get(explicit_status, explicit_status)
            if derived_status and derived_status != rec.project_status:
                if rec.project_status == "completed" or derived_status == "completed":
                    rec.add_issue(Severity.REVIEW, "status_conflict",
                                  f"project status '{rec.project_status}' contradicts activity statuses "
                                  f"(imply '{derived_status}')", "project_status")
                else:
                    rec.add_issue(Severity.WARNING, "status_conflict",
                                  f"project status '{rec.project_status}' differs from activity statuses "
                                  f"(imply '{derived_status}'); explicit value used", "project_status")
        elif derived_status:
            rec.project_status = derived_status
            derived.append("project_status")

        # ---- dates ---------------------------------------------------------
        starts = [a.planned_start for a in rec.activities if a.planned_start]
        ends = [a.planned_end for a in rec.activities if a.planned_end]
        if not rec.planned_start_date and starts:
            rec.planned_start_date = min(starts)
            derived.append("planned_start_date")
        if not rec.planned_end_date and ends:
            rec.planned_end_date = max(ends)
            derived.append("planned_end_date")
        if not rec.actual_completion_date and rec.project_status == "completed":
            actuals = [a.actual_end for a in main if a.actual_end]
            all_done = all(a.status in (DONE, CANCELLED) for a in main)
            if all_done and actuals and len(actuals) == len([a for a in main if a.status == DONE]):
                rec.actual_completion_date = max(actuals)
                derived.append("actual_completion_date")
        if rec.actual_completion_date and rec.project_status not in (None, "completed", "cancelled"):
            rec.add_issue(Severity.WARNING, "completion_conflict",
                          "actual completion date present but project is not marked completed",
                          "actual_completion_date")
        if derived:
            rec.extra_attributes["derived_fields"] = derived
