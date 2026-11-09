"""File -> analysed project records.  Same pipeline for every delivery channel."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Vocabulary
from .extraction import Extractor
from .mapping_config import apply_approved, build_proposal, fingerprint
from .models import ParsedFile, ProjectRecord, Severity, TableSchema
from .normalize import norm_header
from .parsers import parse_file
from .schema_detection import SchemaDetector
from .validation import validate
from .workflow import WorkflowMapper


@dataclass
class AnalysisResult:
    file_name: str
    file_sha256: str
    parsed: ParsedFile
    schemas: list[TableSchema]
    records: list[ProjectRecord]
    row_errors: list[dict]
    fingerprint: str
    proposal: dict
    approved_mapping: Optional[dict]
    structure_matches_approved: bool
    mapping_notes: list[str] = field(default_factory=list)
    new_values: dict = field(default_factory=dict)
    profile: Optional[object] = None        # ProfileResult when a workbook profile was used


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def analyze_file(path: str | Path, vocab: Vocabulary, approved: Optional[dict] = None,
                 display_name: Optional[str] = None, target_month: Optional[str] = None,
                 config_dir: Optional[Path] = None) -> AnalysisResult:
    """Parse + recognise + extract + validate.  Raises parser errors for
    unreadable files (unsupported/empty/corrupt); everything else is reported.

    Workbooks matching a known profile (config/profiles/) are processed by that
    profile; everything else goes through generic structure recognition."""
    path = Path(path)
    name = display_name or path.name
    prof = _try_profile(path, name, vocab, approved, target_month, config_dir)
    if prof is not None:
        return prof
    parsed = parse_file(path)
    detector = SchemaDetector(vocab)
    schemas = [detector.detect(t) for t in parsed.tables]
    fp = fingerprint(schemas)
    matches = bool(approved and approved.get("fingerprint") == fp)

    notes: list[str] = []
    value_overrides = None
    if approved and matches:
        notes = apply_approved(schemas, parsed.tables, approved)
        value_overrides = approved.get("value_mappings") or {}
    mapper = WorkflowMapper(vocab, value_overrides)
    extractor = Extractor(vocab, mapper, name)
    records: list[ProjectRecord] = []
    for t, s in zip(parsed.tables, schemas):
        if s.relevant:
            records.extend(extractor.extract_table(t, s))
    records = extractor.finalize(records)
    for r in records:
        validate(r, vocab.stage_order)

    # Values never seen in the approved mapping -> affected projects to review.
    new_values: dict[str, list[str]] = {}
    if approved and matches:
        known = {k: {norm_header(x) for x in (v or {})} for k, v in (approved.get("value_mappings") or {}).items()}
        for kind, seen in mapper.observed.items():
            fresh = [raw for raw in seen if norm_header(raw) not in known.get(kind, set())]
            if fresh:
                new_values[kind] = fresh
        if new_values:
            fresh_norm = {norm_header(x) for vals in new_values.values() for x in vals}
            for r in records:
                used = [a.name for a in r.activities] + [a.status_raw for a in r.activities if a.status_raw]
                used += [str(r.raw_values.get(k)) for k in ("current_stage", "project_status")
                         if r.raw_values.get(k) is not None]
                if r.extra_attributes.get("priority_original"):
                    used.append(str(r.extra_attributes["priority_original"]))
                hits = sorted({u for u in used if norm_header(u) in fresh_norm})
                if hits:
                    r.add_issue(Severity.REVIEW, "unapproved_value",
                                f"value(s) {hits} not in the approved mapping; needs validation")

    proposal = build_proposal(name, parsed.tables, schemas, mapper.observed, fp)
    return AnalysisResult(name, sha256_file(path), parsed, schemas, records, extractor.row_errors, fp, proposal,
                          approved, matches, notes, new_values)


def _try_profile(path: Path, name: str, vocab: Vocabulary, approved: Optional[dict], target_month: Optional[str],
                 config_dir: Optional[Path]) -> Optional[AnalysisResult]:
    from .config import DEFAULT_CONFIG_DIR
    from .parsers import detect_format
    from .profiles.monthly_plan import MonthlyPlanProfile, load_profile
    from .profiles import xlsx_fast
    cfg = Path(config_dir or DEFAULT_CONFIG_DIR)
    if detect_format(path) != "xlsx" or not (cfg / "profiles" / "monthly_work_plan.yaml").exists():
        return None
    profile = load_profile(cfg)
    if not MonthlyPlanProfile.detect(str(path), profile):
        return None
    result = MonthlyPlanProfile(profile, approved).build(str(path), name, target_month)
    for r in result.records:
        validate(r, vocab.stage_order)
    matches = bool(approved and approved.get("profile") == MonthlyPlanProfile.name
                   and approved.get("fingerprint") == result.fingerprint)
    parsed = ParsedFile(str(path), "xlsx", [], xlsx_fast.list_sheets(str(path)))
    return AnalysisResult(name, sha256_file(path), parsed, [], result.records, [], result.fingerprint,
                          result.proposal, approved, matches, result.notes, result.new_values, result)
