"""The processing service: one pipeline for every delivery channel.

    receive -> store in inbox -> parse -> recognise structure -> extract ->
    validate -> (approved mapping?) -> fetch dashboard -> plan -> sync/dry-run
    -> report -> history
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .client import ApiError, AuthError, DashboardClient, NetworkError
from .config import Settings, load_policy, load_vocabulary
from .ingestion import Inbox
from .mapping_config import MappingStore
from .models import PROJECT_FIELDS
from .parsers import CorruptFileError, EmptyFileError, UnsupportedFormatError
from .pipeline import analyze_file
from .report import build_report, render_initial_analysis, render_text
from .sync import SyncExecutor, build_plan, restrict_for_auto

log = logging.getLogger("plan_agent")


@dataclass
class ProcessOutcome:
    run_id: str
    status: str          # completed | completed_with_errors | dry_run | approval_required | failed
    report: dict
    text: str
    analysis_markdown: Optional[str] = None
    report_path: Optional[Path] = None


class PlanAgent:
    def __init__(self, settings: Optional[Settings] = None, client: Optional[DashboardClient] = None):
        self.settings = settings or Settings.from_env()
        self.vocab = load_vocabulary(self.settings.config_dir)
        self.policy = load_policy(self.settings.config_dir)
        self.store = MappingStore(self.settings.state_dir)
        self.inbox = Inbox(self.settings.state_dir)
        self.reports_dir = self.settings.state_dir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.settings.state_dir / "sync_history.jsonl"
        self._client = client

    @property
    def client(self) -> Optional[DashboardClient]:
        if self._client is None and self.settings.api_url:
            self._client = DashboardClient(self.settings)
        return self._client

    # ------------------------------------------------------------------ #
    def receive(self, path: Path, channel: str, original_name: Optional[str] = None,
                requested_mode: str = "auto", metadata: Optional[dict] = None,
                target_month: Optional[str] = None, auto_safe: bool = False) -> ProcessOutcome:
        stored, meta = self.inbox.store(Path(path), channel, original_name, metadata)
        name = original_name or Path(path).name
        return self.process(stored, name, requested_mode, target_month, auto_safe)

    def reprocess(self, sha: str, requested_mode: str = "auto", target_month: Optional[str] = None) -> ProcessOutcome:
        stored, meta = self.inbox.get(sha)
        name = meta["deliveries"][-1]["original_name"]
        meta["deliveries"].append({"channel": "reprocess",
                                   "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "original_name": name})
        (stored.parent / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        return self.process(stored, name, requested_mode, target_month)

    # ------------------------------------------------------------------ #
    def process(self, path: Path, display_name: str, requested_mode: str = "auto",
                target_month: Optional[str] = None, auto_safe: bool = False) -> ProcessOutcome:
        """requested_mode: 'auto' (sync if approved, else dry run), 'dry_run', 'sync'.
        target_month: 'YYYY-MM' for monthly workbooks (default: PLAN_TARGET_MONTH or profile setting)."""
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        approved = self.store.load_approved()
        try:
            a = analyze_file(path, self.vocab, approved, display_name=display_name,
                             target_month=target_month or self.settings.target_month,
                             config_dir=self.settings.config_dir)
        except (UnsupportedFormatError, EmptyFileError, CorruptFileError) as exc:
            return self._failed(run_id, display_name, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # unexpected parser failure: report, never crash the service
            log.exception("analysis failed")
            return self._failed(run_id, display_name, f"unexpected analysis error: {type(exc).__name__}: {exc}")

        notes: list[str] = []
        prof = a.profile
        if prof is not None and not prof.analysis.get("source_sheet"):
            return self._failed(run_id, display_name, "; ".join(prof.blocking) or "no source worksheet", a,
                                status="Verification required (no source worksheet selected)")
        if prof is None and not any(s.relevant for s in a.schemas):
            return self._failed(run_id, display_name,
                                "no worksheet/section with project planning data was recognised: " +
                                " | ".join(f"{s.source}: {'; '.join(s.reasons)}" for s in a.schemas), a)

        initial = approved is None
        if initial:
            mode = "dry_run"
            notes.append("INITIAL ANALYSIS MODE: no approved mapping yet - dry run only; review the analysis and "
                         "run 'approve' to enable synchronisation")
        elif not a.structure_matches_approved:
            mode = "dry_run"
            notes.append(f"STRUCTURE CHANGED: fingerprint {a.fingerprint} differs from approved "
                         f"{approved.get('fingerprint')} - new validation required; dry run only")
        else:
            mode = "dry_run" if requested_mode == "dry_run" else "sync"
        blocked = bool(prof is not None and prof.blocking)
        if blocked:
            mode = "dry_run"
            notes.extend("VERIFICATION REQUIRED: " + b for b in prof.blocking)
        if mode == "dry_run" and (initial or not a.structure_matches_approved):
            self.store.save_proposal(a.proposal)
            notes.append(f"proposed mapping written to {self.store.proposed_path}")

        # ---- compare with dashboard --------------------------------------
        existing, comparison, writable, fatal = [], False, set(PROJECT_FIELDS), None
        if self.client is None:
            notes.append("PM_API_URL not configured - dashboard comparison skipped")
            if mode == "sync":
                mode = "dry_run"
                notes.append("cannot synchronise without PM_API_URL; dry run only")
        else:
            try:
                writable = self.client.writable_fields()
                unknown = writable - set(PROJECT_FIELDS)
                writable &= set(PROJECT_FIELDS)
                if unknown:
                    notes.append(f"backend supports additional fields not produced by the agent: {sorted(unknown)}")
                existing = self.client.list_projects()
                comparison = True
            except (AuthError, NetworkError, ApiError) as exc:
                fatal = f"dashboard unavailable: {exc}"
                notes.append(fatal)
                if mode == "sync":
                    mode = "dry_run"

        if mode == "sync" and self.settings.require_verified_deployment:
            from .deployment import load_status
            dep = load_status(self.settings.state_dir, self.settings.api_url)
            if not (dep and dep.get("ready")):
                mode = "dry_run"
                fatal = fatal or "deployment not verified"
                notes.append("SYNC BLOCKED: no successful deployment verification for this API "
                             "(run 'python -m plan_agent verify-deployment'): " +
                             ("; ".join(dep.get("blocking", [])) if dep else "never verified"))
        plans = build_plan(a.records, existing, self.policy, writable)
        if auto_safe:
            restrict_for_auto(plans, self.policy)
            notes.append("AUTOMATIC MODE: only approved source-owned fields and existing activity data are "
                         "applied; everything else is held for review")
        if mode == "sync":
            fatal = SyncExecutor(self.client, writable, run_id, display_name).execute(plans) or fatal

        report = build_report(a, plans, mode, "", run_id, comparison, notes, fatal)
        report["sync_scope"] = "automatic" if auto_safe else "full"
        report["held_for_review"] = sum(1 for p in plans if p.held_for_review)
        c = report["counts"]
        if mode == "sync":
            status = "Failed" if fatal and c["new"] + c["updated"] == 0 and c["sync_errors"] else \
                "Completed with errors" if c["sync_errors"] or fatal else "Completed"
        elif blocked:
            status = "Verification required (dry run)"
        elif initial or not a.structure_matches_approved:
            status = "Approval required (dry run)"
        elif fatal == "deployment not verified":
            status = "Dry run - deployment not verified"
        else:
            status = "Dry run" if not fatal else "Dry run - dashboard unavailable"
        report["status"] = status
        text = render_text(report)
        md = None
        if initial or not a.structure_matches_approved or blocked:
            if prof is not None:
                from .profiles.report import render_profile_analysis
                md = render_profile_analysis(a, plans, report)
            else:
                md = render_initial_analysis(a, plans, report)
        rp = self._persist(run_id, started, a, report, text, md, plans)
        return ProcessOutcome(run_id, status, report, text, md, rp)

    # ------------------------------------------------------------------ #
    def _persist(self, run_id, started, a, report, text, md, plans=None) -> Path:
        base = self.reports_dir / run_id
        (base.with_suffix(".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        (base.with_suffix(".txt")).write_text(text)
        if md:
            (self.reports_dir / f"{run_id}_analysis.md").write_text(md)
        if a.profile is not None:
            queue = [{"project": r.project_name, "category": r.record_category, "source_file": r.source_file,
                      "source_sheet": r.source_sheet, "source_rows": r.source_rows,
                      "external_project_id": r.external_project_id,
                      "reasons": [i.message for i in r.issues if i.severity.value in ("review", "error")],
                      "activities": r.activity_payload}
                     for r in a.records if r.needs_review()]
            (self.settings.state_dir / "review_queue.json").write_text(
                json.dumps({"run_id": run_id, "source_file": report["input_file"], "items": queue},
                           ensure_ascii=False, indent=2, default=str))
        entry = {"run_id": run_id, "started_at": started,
                 "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "source_file": report["input_file"], "file_sha256": report.get("file_sha256"),
                 "mode": report["mode"], "status": report["status"], "counts": report["counts"],
                 "changes": [{"project": p["project"], "dashboard_id": p["dashboard_id"], "type": p["change_type"],
                              "result": p["result"], "fields": [c["field"] for c in p["changes"]]}
                             for p in report["projects"] if p["change_type"] in ("new", "update")]}
        with open(self.history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if self.client is not None and report["mode"] in ("sync", "dry_run"):
            c = report.get("counts") or {}
            try:
                self.client.save_sync_run({
                    "id": run_id, "started_at": started, "finished_at": entry["finished_at"],
                    "source_file": report["input_file"], "file_sha256": report.get("file_sha256"),
                    "plan_month": a.profile.analysis.get("target_month") if a.profile is not None else None,
                    "mode": report["mode"], "status": report["status"], "counts": c, "report": report,
                    "error": report.get("fatal_error"),
                    "review_status": f"{c.get('review', 0)} records need review" if c.get("review") else "none"})
            except ApiError as exc:
                log.warning("could not store the run on the dashboard: %s", exc)
            if report["mode"] == "sync":
                self._push_review_queue(a, run_id)
                self._push_held(plans or [], a, run_id)
        return base.with_suffix(".txt")

    def _push_review_queue(self, a, run_id: str) -> None:
        """Ambiguous planning records go to the dashboard's review queue (idempotent per record)."""
        for r in a.records:
            if not r.needs_review():
                continue
            month = r.last_plan_month or ""
            key = f"plan:{month}:{r.source_sheet}:{','.join(map(str, r.source_rows or [r.source_row]))}"
            payload = {"project_name": r.project_name, "record_category": r.record_category,
                       "source_file": r.source_file, "source_sheet": r.source_sheet, "source_rows": r.source_rows,
                       "plan_month": month, "manufacturer": r.manufacturer, "vehicle_model": r.vehicle_model,
                       "product_type": r.product_type, "external_project_id": r.external_project_id,
                       "activities": r.activity_payload, "sync_run_id": run_id,
                       "issues": [i.to_dict() for i in r.issues if i.severity.value in ("review", "error")]}
            reason = "; ".join(i.message for i in r.issues if i.severity.value in ("review", "error"))
            try:
                self.client.push_review_item(key, "plan_record", payload, reason[:2000])
            except ApiError as exc:
                log.warning("could not queue review record %s: %s", key, exc)

    def _push_held(self, plans, a, run_id: str) -> None:
        """Changes the automatic mode did not apply go to the review queue (one item per project
        and plan month; a later run updates the same item instead of adding another)."""
        month = a.profile.analysis.get("target_month") if a.profile is not None else ""
        for p in plans:
            if not p.held_for_review:
                continue
            r = p.record
            key = f"auto:{month}:{r.external_project_id or r.identity_key()}"
            payload = {"project_name": r.project_name, "record_category": r.record_category,
                       "dashboard_id": p.dashboard_id, "external_project_id": r.external_project_id,
                       "plan_month": month, "source_file": r.source_file, "source_sheet": r.source_sheet,
                       "source_rows": r.source_rows, "sync_run_id": run_id,
                       "applied_changes": [{"field": c.field, "old": c.old, "new": c.new} for c in p.changes]
                       if p.change_type.value == "update" else [],
                       "changes": [{"field": c.field, "old": c.old, "new": c.new}
                                   for c in (p.withheld if p.change_type.value == "update" else p.changes + p.withheld)],
                       "activity_changes": p.activity_changes,
                       "conflicts": [{"field": c.field, "dashboard": c.dashboard_value, "file": c.file_value}
                                     for c in p.conflicts]}
            try:
                self.client.push_review_item(key, "plan_auto_sync_held", payload, p.reason[:2000])
            except ApiError as exc:
                log.warning("could not queue held change %s: %s", key, exc)

    def _failed(self, run_id, name, error, a=None, status: str = "Failed") -> ProcessOutcome:
        report = {"run_id": run_id, "input_file": name, "mode": "none", "status": status, "fatal_error": error,
                  "counts": {"identified": 0, "new": 0, "updated": 0, "unchanged": 0, "review": 0,
                             "duplicate_candidates": 0, "invalid_records": 0, "sync_errors": 0},
                  "projects": [], "row_errors": [], "notes": [], "file_warnings": []}
        text = ("ENGINEERING PLAN SYNCHRONIZATION\n\n"
                f"Input file: {name}\nRun id: {run_id}\n\nStatus: {status}\n\nError: {error}\n")
        (self.reports_dir / f"{run_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        (self.reports_dir / f"{run_id}.txt").write_text(text)
        with open(self.history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": run_id, "source_file": name, "status": status, "error": error},
                                ensure_ascii=False) + "\n")
        return ProcessOutcome(run_id, status, report, text, report_path=self.reports_dir / f"{run_id}.txt")
