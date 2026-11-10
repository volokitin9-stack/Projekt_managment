"""Command line interface:  python -m plan_agent <command> ...

Commands
  analyze FILE          initial analysis + proposed mapping + dry run (never writes)
  approve [--by NAME]   approve the proposed mapping
  sync FILE [--dry-run] process a file (syncs only with an approved, matching mapping)
  reprocess SHA         process a previously received file again
  watch DIR             process files dropped into DIR (--once for cron/scheduled use)
  serve                 HTTP intake for other agents (POST /files)
  status                show approval state and recent runs
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from .config import Settings


def _agent():
    from .service import PlanAgent
    return PlanAgent(Settings.from_env())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="plan_agent", description="Engineering plan -> Projekt_managment sync agent")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("analyze")
    p.add_argument("file")
    p.add_argument("--month", help="target month YYYY-MM for monthly workbooks (default: auto)")
    p.add_argument("--name", help="original file name to record (default: the path's name)")
    p.add_argument("--json", action="store_true", help="print a one-line JSON summary at the end")
    p = sub.add_parser("approve")
    p.add_argument("--by", default=None)
    p.add_argument("--fingerprint", default=None, help="only approve if the proposal has this structure fingerprint")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("sync")
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--auto-safe", action="store_true",
                   help="unattended mode: apply only safe changes, hold the rest for review")
    p.add_argument("--month", help="target month YYYY-MM for monthly workbooks (default: auto)")
    p.add_argument("--name", help="original file name to record (default: the path's name)")
    p.add_argument("--json", action="store_true", help="print a one-line JSON summary at the end")
    p = sub.add_parser("reprocess")
    p.add_argument("sha")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("watch")
    p.add_argument("folder")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--once", action="store_true")
    p = sub.add_parser("serve")
    p.add_argument("--host", default=os.environ.get("PLAN_AGENT_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PLAN_AGENT_PORT", "8090")))
    sub.add_parser("status")
    p = sub.add_parser("verify-deployment", help="check dashboard, API, auth and persistence (no writes)")
    p.add_argument("--after-restart", action="store_true", help="compare with the snapshot taken before a restart")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if a.cmd == "approve":
        from .mapping_config import MappingStore
        store = MappingStore(Settings.from_env().state_dir)
        result = {"approved": False}
        try:
            current = store.load_approved()
            if a.fingerprint and current and current.get("fingerprint") == a.fingerprint:
                result = {"approved": True, "fingerprint": a.fingerprint, "already_approved": True}
            else:
                if a.fingerprint:
                    import yaml
                    proposal = yaml.safe_load(store.proposed_path.read_text()) if store.proposed_path.exists() else {}
                    if (proposal or {}).get("fingerprint") != a.fingerprint:
                        raise ValueError(f"the proposed mapping has fingerprint {(proposal or {}).get('fingerprint')}, "
                                         f"not {a.fingerprint}; analyse the file again")
                data = store.approve(a.by or getpass.getuser())
                result = {"approved": True, "fingerprint": data["fingerprint"]}
        except (FileNotFoundError, ValueError) as exc:
            result = {"approved": False, "error": str(exc)}
        if a.json:
            print(json.dumps(result, ensure_ascii=False))
        elif result["approved"]:
            print(f"mapping approved (fingerprint {result['fingerprint']}) -> {store.approved_path}")
        else:
            print(f"error: {result['error']}", file=sys.stderr)
        return 0 if result["approved"] else 2

    if a.cmd == "verify-deployment":
        from .deployment import render, verify
        st = Settings.from_env()
        res = verify(st.api_url, st.api_key, st.state_dir, st.dashboard_url, a.after_restart, timeout=st.timeout)
        print(render(res))
        return 0 if res["ready"] else 5

    agent = _agent()
    if a.cmd in ("analyze", "sync"):
        mode = "dry_run" if a.cmd == "analyze" or a.dry_run else "auto"
        name = a.name or Path(a.file).name
        out = agent.receive(Path(a.file), "cli", name, mode, target_month=a.month,
                            auto_safe=bool(getattr(a, "auto_safe", False)))
        md = agent.reports_dir / f"{out.run_id}_analysis.md"
        if a.json:
            r = out.report
            print(json.dumps({"run_id": out.run_id, "status": out.status, "mode": r.get("mode"),
                              "counts": r.get("counts"), "fingerprint": r.get("structure_fingerprint"),
                              "fatal_error": r.get("fatal_error"), "notes": r.get("notes"),
                              "sync_scope": r.get("sync_scope"), "held_for_review": r.get("held_for_review"),
                              "report_path": str(out.report_path),
                              "report_json_path": str(out.report_path.with_suffix(".json")),
                              "analysis_path": str(md) if md.exists() else None}, ensure_ascii=False))
        else:
            print(out.text)
            if out.analysis_markdown:
                print(f"Initial analysis report: {md}")
            print(f"Report: {out.report_path}")
        return {"Failed": 1}.get(out.status, 3 if "required" in out.status else
                                 4 if "errors" in out.status else 0)
    if a.cmd == "reprocess":
        out = agent.reprocess(a.sha, "dry_run" if a.dry_run else "auto")
        print(out.text)
        return 1 if out.status == "Failed" else 0
    if a.cmd == "watch":
        from .intake import watch_folder
        watch_folder(agent, Path(a.folder), a.interval, a.once)
        return 0
    if a.cmd == "serve":
        key = os.environ.get("PLAN_AGENT_INTAKE_KEY")
        if not key:
            print("error: set PLAN_AGENT_INTAKE_KEY to accept files from other agents", file=sys.stderr)
            return 2
        from .intake import make_intake_server
        srv = make_intake_server(agent, a.host, a.port, key)
        print(f"plan agent intake listening on http://{a.host}:{srv.server_address[1]}/files", file=sys.stderr)
        srv.serve_forever()
        return 0
    if a.cmd == "status":
        approved = agent.store.load_approved()
        print("approved mapping:", f"yes (fingerprint {approved['fingerprint']}, by {approved.get('approved_by')} "
              f"at {approved.get('approved_at')})" if approved else "no - next file runs in initial analysis mode")
        print("dashboard API:", agent.settings.api_url or "not configured", "| agent key:",
              "set" if agent.settings.api_key else "missing")
        if agent.history_path.exists():
            for line in agent.history_path.read_text().splitlines()[-10:]:
                e = json.loads(line)
                print(f"  {e['run_id']}  {e.get('status'):<30} {e.get('source_file')}  {e.get('counts', '')}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
