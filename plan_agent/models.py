"""Core data structures shared by every stage of the pipeline.

The parsers produce :class:`RawTable` objects (format-independent grids),
schema detection produces :class:`TableSchema`, extraction produces
:class:`ProjectRecord` objects, and the sync engine produces :class:`ChangePlan`
entries.  Nothing in here talks to the network or the filesystem.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Parsing layer
# --------------------------------------------------------------------------- #
@dataclass
class Cell:
    value: Any
    merged: bool = False          # value was propagated from a merged range
    merge_origin: Optional[tuple[int, int]] = None
    bold: bool = False
    fill: Optional[str] = None    # background colour (useful for group rows)
    comment: Optional[str] = None  # spreadsheet cell note

    def text(self) -> str:
        if self.value is None:
            return ""
        return str(self.value).strip()

    def is_empty(self) -> bool:
        return self.text() == ""


@dataclass
class RawTable:
    """A rectangular grid of cells extracted from one worksheet/section."""
    source: str                    # sheet name, "page 2 table 1", ...
    rows: list[list[Cell]]
    row_numbers: list[int]         # original 1-based row numbers in the source
    outline_levels: list[int] = field(default_factory=list)  # Excel row grouping
    hidden_rows: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def cell(self, r: int, c: int) -> Cell:
        row = self.rows[r]
        return row[c] if c < len(row) else Cell(None)


@dataclass
class ParsedFile:
    path: str
    file_format: str
    tables: list[RawTable]
    sheet_names: list[str]
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Schema detection
# --------------------------------------------------------------------------- #
@dataclass
class ColumnMapping:
    index: int
    header: str                    # combined header text ("Scanning / Plan")
    role: Optional[str]            # e.g. "engineer", "stage_date:scanning:planned"
    confidence: float
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Layout(str, Enum):
    WIDE = "wide"          # one row = one project, stages laid out as columns
    LONG = "long"          # several rows (activities) per project
    UNKNOWN = "unknown"


@dataclass
class TableSchema:
    source: str
    relevant: bool
    header_rows: list[int]          # indexes into RawTable.rows
    data_start: int
    columns: list[ColumnMapping]
    layout: Layout
    group_row_indexes: list[int] = field(default_factory=list)
    group_levels_role: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0

    def role_index(self, role: str) -> Optional[int]:
        for c in self.columns:
            if c.role == role:
                return c.index
        return None

    def roles(self) -> set[str]:
        return {c.role for c in self.columns if c.role}


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    REVIEW = "review"      # record must not be synced automatically
    ERROR = "error"        # record is invalid


@dataclass
class Issue:
    severity: Severity
    code: str
    message: str
    field: Optional[str] = None
    source: Optional[str] = None
    row: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class Activity:
    """One engineering activity/stage occurrence for a project."""
    name: str                      # original activity text from the file
    stage: Optional[str]           # mapped dashboard stage or None if unmapped
    stage_mapped: bool
    status: Optional[str] = None   # normalised: done / in_progress / pending / blocked / cancelled
    status_raw: Optional[str] = None
    planned_start: Optional[str] = None
    planned_end: Optional[str] = None
    actual_end: Optional[str] = None
    engineer: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)
    comment: Optional[str] = None
    source_row: Optional[int] = None
    details: dict = field(default_factory=dict)   # profile-specific facts (hours, task group, ...)

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items()}
        if not d["details"]:
            d.pop("details")
        return d


# Dashboard fields the agent may write.  Must stay in sync with the backend
# schema; the client additionally verifies against GET /api/agent/schema.
PROJECT_FIELDS = [
    "external_project_id", "project_name", "manufacturer", "vehicle_model",
    "product_type", "article_number", "assigned_engineer", "priority",
    "current_stage", "project_status", "planned_start_date", "planned_end_date",
    "actual_completion_date", "description", "stages", "dependencies",
    "extra_attributes", "source_file", "source_row", "record_category", "last_plan_month",
]
RECORD_CATEGORIES = ["main_project", "additional_work", "backlog"]


@dataclass
class ProjectRecord:
    external_project_id: Optional[str] = None
    project_name: Optional[str] = None
    manufacturer: Optional[str] = None
    vehicle_model: Optional[str] = None
    product_type: Optional[str] = None
    article_number: Optional[str] = None
    assigned_engineer: Optional[str] = None
    priority: Optional[str] = None
    current_stage: Optional[str] = None
    project_status: Optional[str] = None
    planned_start_date: Optional[str] = None
    planned_end_date: Optional[str] = None
    actual_completion_date: Optional[str] = None
    description: Optional[str] = None
    activities: list[Activity] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    extra_attributes: dict[str, Any] = field(default_factory=dict)
    source_file: Optional[str] = None
    source_sheet: Optional[str] = None
    source_row: Optional[int] = None
    source_rows: list[int] = field(default_factory=list)
    raw_values: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    record_category: str = "main_project"          # main_project | additional_work | backlog
    last_plan_month: Optional[str] = None          # YYYY-MM of a monthly plan
    # Month-scoped activities for the backend's project_activities table.  When
    # set, the legacy "stages" JSON is not written.
    activity_payload: Optional[list[dict]] = None
    activity_month: Optional[str] = None

    # ------------------------------------------------------------------ #
    def identity_key(self) -> Optional[str]:
        """Key used to group rows into one project inside a file."""
        from .normalize import norm_key  # local import to avoid a cycle
        if self.external_project_id:
            return "ext:" + norm_key(self.external_project_id)
        if self.article_number:
            return "art:" + norm_key(self.article_number)
        parts = [self.manufacturer, self.vehicle_model, self.product_type]
        if all(parts):
            return "cmp:" + "|".join(norm_key(p) for p in parts)
        if self.project_name:
            return "name:" + norm_key(self.project_name)
        return None

    def needs_review(self) -> bool:
        return any(i.severity in (Severity.REVIEW, Severity.ERROR) for i in self.issues)

    def has_errors(self) -> bool:
        return any(i.severity == Severity.ERROR for i in self.issues)

    def add_issue(self, severity: Severity, code: str, message: str, field_name: str | None = None):
        self.issues.append(Issue(severity, code, message, field_name, self.source_sheet, self.source_row))

    def to_payload(self) -> dict:
        """Dashboard payload (only fields the dashboard understands)."""
        payload = {
            "external_project_id": self.external_project_id,
            "project_name": self.project_name,
            "manufacturer": self.manufacturer,
            "vehicle_model": self.vehicle_model,
            "product_type": self.product_type,
            "article_number": self.article_number,
            "assigned_engineer": self.assigned_engineer,
            "priority": self.priority,
            "current_stage": self.current_stage,
            "project_status": self.project_status,
            "planned_start_date": self.planned_start_date,
            "planned_end_date": self.planned_end_date,
            "actual_completion_date": self.actual_completion_date,
            "description": self.description,
            # activity source rows are left out so that moving rows in the
            # file is not mistaken for a change of the project's stages
            "stages": None if self.activity_payload is not None else
                      ([{k: v for k, v in a.to_dict().items() if k != "source_row"} for a in self.activities] or None),
            "dependencies": self.dependencies or None,
            "extra_attributes": self.extra_attributes or None,
            "source_file": self.source_file,
            "source_row": self.source_row,
            "record_category": self.record_category,
            "last_plan_month": self.last_plan_month,
        }
        return payload

    def to_dict(self) -> dict:
        d = self.to_payload()
        d["source_sheet"] = self.source_sheet
        d["source_rows"] = self.source_rows
        d["issues"] = [i.to_dict() for i in self.issues]
        return d


# --------------------------------------------------------------------------- #
# Synchronisation
# --------------------------------------------------------------------------- #
class ChangeType(str, Enum):
    NEW = "new"
    UPDATE = "update"
    UNCHANGED = "unchanged"
    DUPLICATE = "duplicate_candidate"
    REVIEW = "review"
    ERROR = "error"


@dataclass
class FieldChange:
    field: str
    old: Any
    new: Any


@dataclass
class Conflict:
    field: str
    dashboard_value: Any
    file_value: Any
    resolution: str              # "kept_dashboard" | "overwritten" | "skipped"
    reason: str


@dataclass
class ChangePlan:
    record: ProjectRecord
    change_type: ChangeType
    dashboard_id: Optional[Any] = None
    dashboard_version: Optional[int] = None
    changes: list[FieldChange] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)   # duplicate candidates
    activity_changes: list[str] = field(default_factory=list)  # "+key", "~key.field", "-key"
    reason: str = ""
    result: Optional[str] = None     # filled after execution: ok / failed / skipped
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "project": self.record.project_name,
            "identity": self.record.identity_key(),
            "source": f"{self.record.source_sheet}!{self.record.source_row}",
            "category": self.record.record_category,
            "external_project_id": self.record.external_project_id,
            "change_type": self.change_type.value,
            "activity_changes": self.activity_changes,
            "dashboard_id": self.dashboard_id,
            "changes": [asdict(c) for c in self.changes],
            "conflicts": [asdict(c) for c in self.conflicts],
            "candidates": self.candidates,
            "reason": self.reason,
            "result": self.result,
            "error": self.error,
            "issues": [i.to_dict() for i in self.record.issues],
        }
