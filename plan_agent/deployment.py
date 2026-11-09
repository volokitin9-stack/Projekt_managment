"""Deployment verification - run BEFORE the first synchronisation.

    python -m plan_agent verify-deployment                 # first check, takes a persistence snapshot
    (restart the dashboard application)
    python -m plan_agent verify-deployment --after-restart # confirms data survived the restart

Nothing is written to the dashboard.  The agent-authentication check sends an
EMPTY create request, which the Штурман API rejects with 400 "Validation
failed" after authenticating (401 = wrong key, 501 = server has no key).

A real synchronisation is refused (see service.PlanAgent) until a check for the
same API URL has status "ready": dashboard page reachable, API healthy, agent
authenticated, not a local/temporary host, data confirmed to survive a restart,
and an agent payload adapter compatible with the detected API.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# Gaps of a Штурман build WITHOUT the planning integration (the original 'main'
# branch, measured on a local test copy with synthetic probe records).  The
# integrated application closes all of them and advertises that through
# GET /api/agent/schema.
REQUIRED_SUPPORT = {"activities", "create_only", "review_queue", "sync_runs", "field_sources"}
SHTURMAN_GAPS = [
    "no record categories: additional work and backlog would appear as Kanban projects",
    "no activities: per-task assignee, hours, hours per date, progress and source rows would be lost",
    "unknown fields are silently dropped (extra_attributes, record_category, last_plan_month, source_row)",
    "priority vocabulary is critical/high/normal/low; a missing priority is displayed as 'normal'",
    "stage is 'status' with key 'prototype' (not 'test_prototype'); a missing stage is displayed as 'scanning'",
    "create requires manufacturer + vehicle_model + product_type: vehicle-less additional work is rejected (400)",
    "the project list is a public endpoint; there is no authenticated agent read endpoint",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key(api_url: str) -> str:
    return hashlib.sha1(api_url.rstrip("/").encode()).hexdigest()[:12]


def status_path(state_dir: Path, api_url: str) -> Path:
    return Path(state_dir) / "deployment" / f"{_key(api_url)}.json"


def load_status(state_dir: Path, api_url: Optional[str]) -> Optional[dict]:
    if not api_url:
        return None
    p = status_path(state_dir, api_url)
    return json.loads(p.read_text()) if p.exists() else None


class Check:
    def __init__(self, name: str):
        self.name, self.status, self.detail = name, "fail", ""

    def set(self, status: str, detail: str) -> "Check":
        self.status, self.detail = status, detail
        return self

    def to_dict(self):
        return {"check": self.name, "status": self.status, "detail": self.detail}


def verify(api_url: Optional[str], api_key: Optional[str], state_dir: Path, dashboard_url: Optional[str] = None,
           after_restart: bool = False, session: Optional[requests.Session] = None, timeout: float = 15) -> dict:
    s = session or requests.Session()
    checks: list[Check] = []
    result = {"verified_at": _now(), "api_url": api_url, "dashboard_url": dashboard_url or api_url,
              "flavor": None, "checks": checks, "ready": False, "blocking": []}
    if not api_url:
        c = Check("configuration").set("fail", "PM_API_URL is not set - no dashboard deployment is configured")
        checks.append(c)
        return _finish(result, state_dir, None)
    base = api_url.rstrip("/")
    host = urlparse(base).hostname or ""

    def get(path, **kw):
        return s.get(base + path, timeout=timeout, **kw)

    # 1. dashboard page
    c = Check("dashboard page")
    try:
        r = s.get((dashboard_url or base).rstrip("/") + "/", timeout=timeout)
        title = re.search(r"<title>([^<]*)</title>", r.text or "")
        if r.status_code == 200 and title:
            c.set("pass", f"HTTP 200, title «{title.group(1).strip()}»")
        else:
            c.set("fail", f"HTTP {r.status_code}, no dashboard page found")
    except requests.RequestException as exc:
        c.set("fail", f"not reachable ({type(exc).__name__})")
    checks.append(c)

    # 2. API health
    c = Check("backend API")
    try:
        r = get("/api/health")
        body = r.json() if r.content else {}
        ok = r.status_code == 200 and (body.get("ok") is True or body.get("status") == "ok")
        c.set("pass" if ok else "fail", f"GET /api/health → HTTP {r.status_code} {body}")
    except (requests.RequestException, ValueError) as exc:
        c.set("fail", f"not reachable ({type(exc).__name__})")
    checks.append(c)
    if c.status == "fail":
        return _finish(result, state_dir, base)

    # 3. detect API flavour (Штурман serves the configurable workflow stages)
    flavor = None
    try:
        r = get("/api/stages")
        if r.status_code == 200 and isinstance(r.json(), list):
            flavor = "shturman"
            result["stages"] = [x.get("key") for x in r.json()]
    except (requests.RequestException, ValueError):
        pass
    result["flavor"] = flavor

    # 4. agent authentication (no write)
    c = Check("agent authentication")
    try:
        hdr = {"Authorization": f"Bearer {api_key or ''}", "Content-Type": "application/json"}
        if flavor == "shturman":
            r = s.post(base + "/api/agent/projects", data="{}", headers=hdr, timeout=timeout)
            if r.status_code == 400:
                c.set("pass", "authenticated (empty probe rejected with 400 validation error; nothing written)")
            elif r.status_code == 401:
                c.set("fail", "key rejected (401) - PM_AGENT_API_KEY does not match the server's AGENT_API_KEY")
            elif r.status_code == 501:
                c.set("fail", "the server has no AGENT_API_KEY configured (501)")
            else:
                c.set("fail", f"unexpected HTTP {r.status_code} - the probe may have been accepted; check the server")
        else:
            c.set("fail", "unknown API: the Штурман dashboard API was not detected")
    except requests.RequestException as exc:
        c.set("fail", f"request failed ({type(exc).__name__})")
    checks.append(c)

    # 5. project data + persistence snapshot
    c = Check("database contents")
    projects = None
    try:
        r = get("/api/projects")
        body = r.json()
        projects = body if isinstance(body, list) else body.get("projects")
        seeds = sum(1 for p in projects if p.get("source") == "seed")
        result["project_count"] = len(projects)
        result["seed_projects"] = seeds
        c.set("pass", f"{len(projects)} projects readable" + (f"; {seeds} are seeded SAMPLE projects (placeholder "
                                                               "engineers) - decide whether to keep them" if seeds else ""))
    except (requests.RequestException, ValueError, AttributeError, TypeError) as exc:
        c.set("fail", f"project list not readable ({type(exc).__name__})")
    checks.append(c)

    c = Check("persistent database (survives restart)")
    snap = {str(p.get("id")): p.get("created_at") for p in projects} if projects is not None else None
    previous = load_status(state_dir, base)
    if snap is None:
        c.set("fail", "no data to snapshot")
    elif after_restart and previous and previous.get("snapshot") is not None:
        before = previous["snapshot"]
        missing = [k for k in before if k not in snap]
        changed = [k for k in before if k in snap and snap[k] != before[k]]
        if not before:
            c.set("fail", "the snapshot before the restart was empty - nothing to compare")
        elif missing or changed:
            c.set("fail", f"after restart {len(missing)} projects missing, {len(changed)} changed - the database is "
                          "NOT persistent (ephemeral disk or reset on start)")
        else:
            c.set("pass", f"all {len(before)} projects from {previous.get('snapshot_at')} are still present after "
                          "the restart")
    elif previous and previous.get("restart_confirmed") and previous.get("snapshot") is not None and \
            all(k in snap for k in previous["snapshot"]):
        c.set("pass", f"confirmed earlier ({previous.get('restart_confirmed')}); data still present")
    else:
        c.set("pending", "snapshot taken; restart the dashboard application, then run "
                         "'python -m plan_agent verify-deployment --after-restart'")
    checks.append(c)
    result["snapshot"] = snap if (c.status != "pass" or not previous) else previous.get("snapshot", snap)
    result["snapshot_at"] = _now() if (c.status != "pass" or not previous) else previous.get("snapshot_at")
    if c.status == "pass":
        result["restart_confirmed"] = (previous or {}).get("restart_confirmed") or _now()

    # 6. production host
    c = Check("production host")
    if host in LOCAL_HOSTS or host.endswith(".local"):
        c.set("fail", f"'{host}' is a local address - this is a test instance, not the production dashboard")
    else:
        c.set("pass", f"remote host {host}")
    checks.append(c)

    # 7. agent <-> API compatibility
    c = Check("agent payload compatibility")
    if flavor == "shturman":
        try:
            r = get("/api/agent/schema", headers={"Authorization": f"Bearer {api_key or ''}"})
            sch = r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            sch = {}
        missing = REQUIRED_SUPPORT - set(sch.get("supports") or [])
        if sch and not missing:
            c.set("pass", f"integrated Штурман API (schema v{sch.get('schema_version')}): categories, activities, "
                          "history, review queue and sync runs supported")
        else:
            c.set("fail", "this Штурман build has no planning integration; it cannot store the import without loss: "
                          + "; ".join(SHTURMAN_GAPS))
    else:
        c.set("fail", "API not identified")
    checks.append(c)
    return _finish(result, state_dir, base)


def _finish(result: dict, state_dir: Path, base: Optional[str]) -> dict:
    checks = result["checks"]
    result["blocking"] = [f"{c.name}: {c.detail}" for c in checks if c.status in ("fail", "pending")]
    result["ready"] = bool(checks) and all(c.status in ("pass", "warn") for c in checks) and \
        result.get("flavor") is not None
    result["checks"] = [c.to_dict() for c in checks]
    if base:
        p = status_path(state_dir, base)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def render(result: dict) -> str:
    mark = {"pass": "OK  ", "fail": "FAIL", "warn": "WARN", "pending": "WAIT"}
    lines = ["DEPLOYMENT VERIFICATION", "", f"API:        {result.get('api_url') or '— not configured —'}",
             f"Dashboard:  {result.get('dashboard_url') or '—'}", f"API type:   {result.get('flavor') or 'unknown'}", ""]
    for c in result["checks"]:
        lines.append(f"[{mark.get(c['status'], c['status'])}] {c['check']}: {c['detail']}")
    lines += ["", "READY FOR FIRST SYNCHRONISATION" if result["ready"] else
              "NOT READY - synchronisation stays blocked"]
    return "\n".join(lines) + "\n"
