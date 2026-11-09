"""Compute the safe set of field changes for an existing project.

Applies the configurable field ownership policy (config/ownership_policy.yaml)
and the non-negotiable protection rules:
* never overwrite a value with an empty one;
* never touch fields owned by other systems (photos, attachments, ...);
* manual dashboard edits are not silently overwritten - they are reported.
"""
from __future__ import annotations

from typing import Any

from .config import OwnershipPolicy
from .models import Conflict, FieldChange
from .normalize import norm_key

TRACKING_FIELDS = {"source_file", "source_row"}


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip() == b.strip()
    return a == b


def plan_field_changes(existing: dict, payload: dict, policy: OwnershipPolicy, writable: set[str]
                       ) -> tuple[list[FieldChange], list[Conflict], list[str]]:
    """Return (changes, conflicts, identity_problems)."""
    changes: list[FieldChange] = []
    conflicts: list[Conflict] = []
    identity: list[str] = []
    sources = existing.get("field_sources") or {}
    for f, new in payload.items():
        if f not in writable or _empty(new):
            continue
        old = existing.get(f)
        owner = policy.owner(f)
        if owner not in ("planning_file", "dashboard"):
            continue
        if f == "extra_attributes" and isinstance(old, dict):
            new = {**old, **new}          # keep keys the file no longer mentions
        if _same(old, new):
            continue
        if f in policy.identity_fields and not _empty(old) and norm_key(old) != norm_key(new):
            identity.append(f"{f}: dashboard '{old}' vs file '{new}'")
            conflicts.append(Conflict(f, old, new, "skipped", "identifier change requires review"))
            continue
        if owner == "dashboard":
            if _empty(old):
                changes.append(FieldChange(f, old, new))
            else:
                conflicts.append(Conflict(f, old, new, "kept_dashboard",
                                          "field is owned by the dashboard; file value not applied"))
            continue
        # planning_file owns it
        if sources.get(f) == "manual" and not _empty(old):
            if policy.on_manual_edit == "overwrite_and_report":
                changes.append(FieldChange(f, old, new))
                conflicts.append(Conflict(f, old, new, "overwritten",
                                          "value was edited manually; overwritten per ownership policy"))
            else:
                conflicts.append(Conflict(f, old, new, "kept_dashboard",
                                          "value was edited manually in the dashboard; kept (policy "
                                          "on_manual_edit=report_and_keep)"))
            continue
        changes.append(FieldChange(f, old, new))
    # Moving rows or renaming the file alone is not a project change.
    if all(c.field in TRACKING_FIELDS for c in changes):
        changes = []
    return changes, conflicts, identity
