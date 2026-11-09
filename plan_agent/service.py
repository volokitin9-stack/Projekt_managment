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
from .sync import SyncExecutor, build_plan

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
                requested_mode: str = "auto", metadata: Optional[dict] = None) -> ProcessOutcome:
        stored, meta = self.inbox.store(Path(path), channel, original_name, metadata)
        name = original_name or Path(path).name
        return self.process(stored, name, requested_mode)

    def reprocess(self, sha: str, requested_mode: str = "auto") -> ProcessOutcome:
        stored, meta = self.inbox.get(sha)
        name = meta["deliveries"][-1]["original_name"]
        meta["deliveries"].append({"channel": "reprocess",
                                   "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "original_name": name})
        (stored.parent / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        return self.process(stored, name, requested_mode)

    # ------------------------------------------------------------------ #
    def process(self, path: Path, display_name: str, requested_mode: str = "auto") -> ProcessOutcome:
        """requested_mode: 'auto' (sync if approved, else dry run), 'dry_run', 'sync'."""
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        approved = self.store.load_approved()
        try:
            a = analyze_file(path, self.vocab, approved, display_name=display_name)
        except (UnsupportedFormatError, EmptyFileError, CorruptFileError) as exc:
            return self._failed(run_id, display_name, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # unexpected parser failure: report, never crash the service
            log.exception("analysis failed")
            return self._failed(run_id, display_name, f"unexpected analysis error: {type(exc).__name__}: {exc}")

        notes: list[str] = []
        if not any(s.relevant for s in a.schemas):
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

        plans = build_plan(a.records, existing, self.policy, writable)
        if mode == "sync":
            fatal = SyncExecutor(self.client, writable, run_id, display_name).execute(plans) or fatal

        report = build_report(a, plans, mode, "", run_id, comparison, notes, fatal)
        c = report["counts"]
        if mode == "sync":
            status = "Failed" if fatal and c["new"] + c["updated"] == 0 and c["sync_errors"] else \
                "Completed with errors" if c["sync_errors"] or fatal else "Completed"
        elif initial or not a.structure_matches_approved:
            status = "Approval required (dry run)"
        else:
            status = "Dry run" if not fatal else "Dry run - dashboard unavailable"
        report["status"] = status
        text = render_text(report)
        md = render_initial_analysis(a, plans, report) if (initial or not a.structure_matches_approved) else None
        rp = self._persist(run_id, started, a, report, text, md)
        return ProcessOutcome(run_id, status, report, text, md, rp)

    # ------------------------------------------------------------------ #
    def _persist(self, run_id, started, a, report, text, md) -> Path:
        base = self.reports_dir / run_id
        (base.with_suffix(".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        (base.with_suffix(".txt")).write_text(text)
        if md:
            (self.reports_dir / f"{run_id}_analysis.md").write_text(md)
        entry = {"run_id": run_id, "started_at": started,
                 "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "source_file": report["input_file"], "file_sha256": report.get("file_sha256"),
                 "mode": report["mode"], "status": report["status"], "counts": report["counts"],
                 "changes": [{"project": p["project"], "dashboard_id": p["dashboard_id"], "type": p["change_type"],
                              "result": p["result"], "fields": [c["field"] for c in p["changes"]]}
                             for p in report["projects"] if p["change_type"] in ("new", "update")]}
        with open(self.history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if self.client is not None and report["mode"] == "sync":
            try:
                self.client.save_sync_run({"id": run_id, "started_at": started, "finished_at": entry["finished_at"],
                                           "source_file": report["input_file"],
                                           "file_sha256": report.get("file_sha256"), "mode": report["mode"],
                                           "status": report["status"], "report": report})
            except ApiError as exc:
                log.warning("could not store sync run on the dashboard: %s", exc)
        return base.with_suffix(".txt")

    def _failed(self, run_id, name, error, a=None) -> ProcessOutcome:
        report = {"run_id": run_id, "input_file": name, "mode": "none", "status": "Failed", "fatal_error": error,
                  "counts": {"identified": 0, "new": 0, "updated": 0, "unchanged": 0, "review": 0,
                             "duplicate_candidates": 0, "invalid_records": 0, "sync_errors": 0},
                  "projects": [], "row_errors": [], "notes": [], "file_warnings": []}
        text = ("ENGINEERING PLAN SYNCHRONIZATION\n\n"
                f"Input file: {name}\nRun id: {run_id}\n\nStatus: Failed\n\nError: {error}\n")
        (self.reports_dir / f"{run_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        (self.reports_dir / f"{run_id}.txt").write_text(text)
        with open(self.history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": run_id, "source_file": name, "status": "Failed", "error": error},
                                ensure_ascii=False) + "\n")
        return ProcessOutcome(run_id, "Failed", report, text, report_path=self.reports_dir / f"{run_id}.txt")
