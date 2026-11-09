"""Projekt_managment reference backend API (standard library only).

The GitHub repository contained no backend when the planning agent was built,
so this module provides the operational API + database the dashboard and the
agent share.  If a different backend is adopted later, it only needs to expose
the same endpoints (see docs/API.md) - the agent does not depend on this code.

Run:
    PM_AGENT_API_KEY=... python -m backend.server --db data/projekt.db --port 8080

Security notes:
* /api/agent/* requires ``Authorization: Bearer <PM_AGENT_API_KEY>``.
* The key is compared in constant time and never logged.
* The manual endpoints (PATCH /api/projects/:id, activities, attachments) are
  where the dashboard's own user authentication must be enforced in production.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCHEMA = Path(__file__).with_name("schema.sql")

# Fields the planning agent may write.  Exposed by GET /api/agent/schema so the
# agent never sends a field the backend does not support.
AGENT_WRITABLE = [
    "external_project_id", "record_category", "project_name", "manufacturer", "vehicle_model", "product_type",
    "article_number", "assigned_engineer", "priority", "current_stage", "project_status", "planned_start_date",
    "planned_end_date", "actual_completion_date", "description", "stages", "dependencies", "extra_attributes",
    "source_file", "source_row", "last_plan_month",
]
MANUAL_WRITABLE = AGENT_WRITABLE + ["comments"]
JSON_FIELDS = {"stages", "dependencies", "extra_attributes"}
DATE_FIELDS = {"planned_start_date", "planned_end_date", "actual_completion_date"}
STAGES = ["scanning", "development", "test_prototype", "measurement", "documentation"]
ACTIVITY_TYPES = STAGES + ["testing", "transfer_extension", "engineering_change"]
CATEGORIES = ["main_project", "additional_work", "backlog"]
PRIORITIES = {"high", "medium", "low"}
STATUSES = {"not_started", "in_progress", "on_hold", "completed", "cancelled"}
PROGRESS = {"planned", "in_progress", "done", "blocked", "cancelled"}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH = re.compile(r"^\d{4}-\d{2}$")

ACTIVITY_AGENT_FIELDS = [
    "activity_key", "plan_month", "sequence", "cycle", "name", "activity_type", "stage", "task_type",
    "project_stage_original", "task_group", "assignee", "assignee_original", "planned_hours", "actual_hours",
    "hours_by_date", "progress_status", "effort_ratio", "items_ok", "items_nok", "result_raw", "comment",
    "details", "source_file", "source_sheet", "source_row",
]
ACTIVITY_MANUAL_FIELDS = ["progress_status", "manual_note", "assignee"]
ACTIVITY_JSON = {"hours_by_date", "details"}

# Columns added after the first schema version: (table, column, DDL).
MIGRATIONS = [
    ("projects", "record_category", "TEXT NOT NULL DEFAULT 'main_project'"),
    ("projects", "last_plan_month", "TEXT"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _j(v):
    return None if v is None else json.dumps(v, ensure_ascii=False)


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.migrate()

    def migrate(self) -> None:
        c = self.conn()
        # older databases: add new columns before the schema script creates
        # indexes/tables that refer to them
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "projects" in tables:
            cols = {r[1] for r in c.execute("PRAGMA table_info(projects)")}
            for table, col, ddl in MIGRATIONS:
                if table == "projects" and col not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        c.executescript(SCHEMA.read_text())

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA journal_mode=WAL")
            self._local.c = c
        return c

    # ------------------------------------------------------------------ #
    @staticmethod
    def row_to_project(row: sqlite3.Row, attachments: list | None = None, activities: list | None = None) -> dict:
        d = dict(row)
        for f in JSON_FIELDS | {"field_sources"}:
            d[f] = json.loads(d[f]) if d.get(f) else ({} if f == "field_sources" else None)
        if attachments is not None:
            d["attachments"] = attachments
        if activities is not None:
            d["activities"] = activities
        return d

    @staticmethod
    def row_to_activity(row: sqlite3.Row) -> dict:
        d = dict(row)
        for f in ACTIVITY_JSON | {"field_sources"}:
            d[f] = json.loads(d[f]) if d.get(f) else ({} if f == "field_sources" else None)
        d["removed_from_source"] = bool(d["removed_from_source"])
        return d

    def activities(self, pid: int, c=None) -> list[dict]:
        c = c or self.conn()
        rows = c.execute("SELECT * FROM project_activities WHERE project_id = ? "
                         "ORDER BY plan_month, sequence, id", (pid,))
        return [self.row_to_activity(r) for r in rows]

    def list_projects(self, filters: dict, with_activities: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM projects", []
        where = []
        for key, col in (("external_project_id", "external_project_id"), ("article_number", "article_number"),
                         ("category", "record_category")):
            if filters.get(key):
                where.append(f"{col} = ?")
                args.append(filters[key])
        if where:
            sql += " WHERE " + " AND ".join(where)
        c = self.conn()
        rows = c.execute(sql + " ORDER BY id", args).fetchall()
        if not with_activities:
            return [self.row_to_project(r) for r in rows]
        acts: dict[int, list] = {}
        for a in c.execute("SELECT * FROM project_activities ORDER BY plan_month, sequence, id"):
            acts.setdefault(a["project_id"], []).append(self.row_to_activity(a))
        return [self.row_to_project(r, activities=acts.get(r["id"], [])) for r in rows]

    def get_project(self, pid: int) -> dict:
        c = self.conn()
        row = c.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise ApiError(404, "not_found", f"project {pid} not found")
        att = [dict(a) for a in c.execute("SELECT * FROM attachments WHERE project_id = ? ORDER BY id", (pid,))]
        return self.row_to_project(row, att, self.activities(pid, c))

    # ------------------------------------------------------------------ #
    @staticmethod
    def validate_fields(data: dict, allowed: list[str]) -> dict:
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_body", "project data must be an object")
        unknown = sorted(set(data) - set(allowed))
        if unknown:
            raise ApiError(400, "unsupported_fields", f"unsupported fields: {unknown}", fields=unknown)
        out = {}
        for k, v in data.items():
            if k in DATE_FIELDS and v is not None and not (isinstance(v, str) and ISO_DATE.match(v)):
                raise ApiError(422, "invalid_value", f"{k} must be YYYY-MM-DD", field=k)
            if k == "priority" and v is not None and v not in PRIORITIES:
                raise ApiError(422, "invalid_value", f"priority must be one of {sorted(PRIORITIES)}", field=k)
            if k == "project_status" and v is not None and v not in STATUSES:
                raise ApiError(422, "invalid_value", f"project_status must be one of {sorted(STATUSES)}", field=k)
            if k == "current_stage" and v is not None and v not in STAGES and not str(v).startswith("extra:"):
                raise ApiError(422, "invalid_value", f"current_stage must be one of {STAGES} or extra:<code>",
                               field=k)
            if k == "record_category" and v not in CATEGORIES:
                raise ApiError(422, "invalid_value", f"record_category must be one of {CATEGORIES}", field=k)
            if k == "last_plan_month" and v is not None and not (isinstance(v, str) and MONTH.match(v)):
                raise ApiError(422, "invalid_value", "last_plan_month must be YYYY-MM", field=k)
            if k == "project_name" and not (v and str(v).strip()):
                raise ApiError(422, "invalid_value", "project_name cannot be empty", field=k)
            if k == "source_row" and v is not None and not isinstance(v, int):
                raise ApiError(422, "invalid_value", "source_row must be an integer", field=k)
            out[k] = _j(v) if k in JSON_FIELDS and v is not None else v
        return out

    @staticmethod
    def validate_activity(a: dict, allowed: list[str]) -> dict:
        if not isinstance(a, dict):
            raise ApiError(400, "invalid_body", "activity must be an object")
        unknown = sorted(set(a) - set(allowed))
        if unknown:
            raise ApiError(400, "unsupported_fields", f"unsupported activity fields: {unknown}", fields=unknown)
        if "activity_key" in allowed and not (isinstance(a.get("activity_key"), str) and a["activity_key"]):
            raise ApiError(422, "invalid_value", "activity_key is required", field="activity_key")
        if a.get("activity_type") is not None and a["activity_type"] not in ACTIVITY_TYPES:
            raise ApiError(422, "invalid_value", f"activity_type must be one of {ACTIVITY_TYPES}", field="activity_type")
        if a.get("stage") is not None and a["stage"] not in STAGES:
            raise ApiError(422, "invalid_value", f"stage must be one of {STAGES}", field="stage")
        if a.get("progress_status") is not None and a["progress_status"] not in PROGRESS:
            raise ApiError(422, "invalid_value", f"progress_status must be one of {sorted(PROGRESS)}",
                           field="progress_status")
        if a.get("plan_month") is not None and not MONTH.match(str(a["plan_month"])):
            raise ApiError(422, "invalid_value", "plan_month must be YYYY-MM", field="plan_month")
        for f in ("planned_hours", "actual_hours", "effort_ratio"):
            if a.get(f) is not None and not isinstance(a[f], (int, float)):
                raise ApiError(422, "invalid_value", f"{f} must be a number", field=f)
        return a

    def find_duplicates(self, c, data: dict, exclude_id: int | None = None) -> list[int]:
        ids = set()
        if data.get("external_project_id"):
            ids |= {r[0] for r in c.execute("SELECT id FROM projects WHERE external_project_id = ?",
                                            (data["external_project_id"],))}
        if data.get("article_number"):
            ids |= {r[0] for r in c.execute("SELECT id FROM projects WHERE article_number = ?",
                                            (data["article_number"],))}
        if not ids and all(data.get(k) for k in ("manufacturer", "vehicle_model", "product_type")) \
                and not data.get("external_project_id") and not data.get("article_number"):
            # SQLite lower() is ASCII-only, so compare Cyrillic text in Python.
            want = tuple(norm(data[k]) for k in ("manufacturer", "vehicle_model", "product_type"))
            cat = data.get("record_category") or "main_project"
            ids |= {r[0] for r in c.execute("SELECT id, manufacturer, vehicle_model, product_type, record_category "
                                            "FROM projects")
                    if (norm(r[1]), norm(r[2]), norm(r[3])) == want and r[4] == cat}
        ids.discard(exclude_id)
        return sorted(ids)

    def _history(self, c, pid, event, field, old, new, source_type, sync):
        c.execute("INSERT INTO project_history (project_id, event_type, field, previous_value, new_value, "
                  "source_type, source, actor, sync_run_id, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (pid, event, field, _j(old), _j(new), source_type,
                   sync.get("source_file"), sync.get("actor"), sync.get("run_id"), now()))

    # ------------------------------------------------------------------ #
    def _apply_activities(self, c, pid: int, activities: list, scope: dict, sync: dict) -> dict:
        """Upsert the activities of ONE plan month (scope.plan_month).  Activities
        of other months are never touched.  An activity of that month that is
        no longer in the file is flagged removed_from_source, not deleted.
        Fields last edited manually are kept and reported."""
        if not isinstance(activities, list):
            raise ApiError(400, "invalid_body", "activities must be a list")
        month = (scope or {}).get("plan_month")
        if month is None or not MONTH.match(str(month)):
            raise ApiError(422, "invalid_value", "activity_scope.plan_month (YYYY-MM) is required with activities")
        seen, summary = set(), {"added": 0, "updated": 0, "removed_from_source": 0, "kept_manual": []}
        existing = {r["activity_key"]: r for r in c.execute(
            "SELECT * FROM project_activities WHERE project_id = ? AND plan_month = ?", (pid, month))}
        ts = now()
        for a in activities:
            self.validate_activity(a, ACTIVITY_AGENT_FIELDS)
            if a.get("plan_month", month) != month:
                raise ApiError(422, "invalid_value", f"activity {a['activity_key']} is outside plan month {month}")
            a = {**a, "plan_month": month}
            key = a["activity_key"]
            if key in seen:
                raise ApiError(422, "invalid_value", f"duplicate activity_key {key}")
            seen.add(key)
            vals = {k: (_j(v) if k in ACTIVITY_JSON and v is not None else v) for k, v in a.items()}
            row = existing.get(key)
            if row is None:
                cols = list(vals) + ["project_id", "field_sources", "created_at", "updated_at"]
                c.execute(f"INSERT INTO project_activities ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                          list(vals.values()) + [pid, json.dumps({k: "agent" for k in vals}), ts, ts])
                self._history(c, pid, "activity_added", f"activity[{key}]", None, a, "agent", sync)
                summary["added"] += 1
                continue
            cur = self.row_to_activity(row)
            sources = cur["field_sources"]
            sets, args = [], []
            for k, v in a.items():
                if cur.get(k) == v:
                    continue
                if sources.get(k) == "manual":
                    summary["kept_manual"].append({"activity_key": key, "field": k, "dashboard_value": cur.get(k),
                                                   "file_value": v})
                    continue
                sets.append(f"{k} = ?")
                args.append(vals[k])
                sources[k] = "agent"
                self._history(c, pid, "activity_updated", f"activity[{key}].{k}", cur.get(k), v, "agent", sync)
            if cur["removed_from_source"]:
                sets.append("removed_from_source = 0")
                self._history(c, pid, "activity_updated", f"activity[{key}].removed_from_source", True, False,
                              "agent", sync)
            if sets:
                c.execute(f"UPDATE project_activities SET {', '.join(sets)}, field_sources = ?, updated_at = ? "
                          "WHERE id = ?", args + [json.dumps(sources), ts, row["id"]])
                summary["updated"] += 1
        for key, row in existing.items():
            if key not in seen and not row["removed_from_source"]:
                c.execute("UPDATE project_activities SET removed_from_source = 1, updated_at = ? WHERE id = ?",
                          (ts, row["id"]))
                self._history(c, pid, "activity_removed_from_source", f"activity[{key}]", None, None, "agent", sync)
                summary["removed_from_source"] += 1
        return summary

    def create_project(self, data: dict, sync: dict, source_type: str = "agent", activities: list | None = None,
                       activity_scope: dict | None = None) -> dict:
        fields = self.validate_fields(data, AGENT_WRITABLE)
        if not fields.get("project_name"):
            raise ApiError(422, "invalid_value", "project_name is required", field="project_name")
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            dups = self.find_duplicates(c, data)
            if dups:
                raise ApiError(409, "duplicate", "a project with the same identity already exists",
                               existing_ids=dups)
            ts = now()
            fields = {k: v for k, v in fields.items() if v is not None}
            sources = {k: source_type for k in fields}
            cols = list(fields) + ["field_sources", "created_by", "created_at", "updated_at", "last_sync"]
            vals = list(fields.values()) + [json.dumps(sources), source_type, ts, ts,
                                            ts if source_type == "agent" else None]
            cur = c.execute(f"INSERT INTO projects ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
            pid = cur.lastrowid
            self._history(c, pid, "project_created", None, None,
                          {k: data[k] for k in data if data[k] is not None}, source_type, sync)
            summary = self._apply_activities(c, pid, activities, activity_scope, sync) if activities is not None \
                else None
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        out = self.get_project(pid)
        if summary is not None:
            out["activity_summary"] = summary
        return out

    def update_project(self, pid: int, changes: dict, sync: dict, expected_version: int | None,
                       source_type: str, activities: list | None = None, activity_scope: dict | None = None) -> dict:
        allowed = AGENT_WRITABLE if source_type == "agent" else MANUAL_WRITABLE
        fields = self.validate_fields(changes, allowed)
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
            if not row:
                raise ApiError(404, "not_found", f"project {pid} not found")
            current = self.row_to_project(row)
            if expected_version is not None and current["version"] != expected_version:
                raise ApiError(409, "version_conflict", "project was modified since it was read",
                               current_version=current["version"])
            if any(k in changes for k in ("external_project_id", "article_number")):
                dups = self.find_duplicates(c, {k: changes.get(k) for k in ("external_project_id",
                                                                            "article_number")}, exclude_id=pid)
                if dups:
                    raise ApiError(409, "duplicate", "identifier already used by another project",
                                   existing_ids=dups)
            sources = current["field_sources"]
            sets, args = [], []
            for k, v in fields.items():
                new_val = changes[k]
                if current.get(k) == new_val:
                    continue
                sets.append(f"{k} = ?")
                args.append(v)
                sources[k] = source_type
                self._history(c, pid, "project_updated", k, current.get(k), new_val, source_type, sync)
            summary = None
            if activities is not None:
                if source_type != "agent":
                    raise ApiError(400, "invalid_body", "use PATCH /api/projects/:id/activities/:aid for manual edits")
                summary = self._apply_activities(c, pid, activities, activity_scope, sync)
            act_changed = bool(summary and (summary["added"] or summary["updated"] or summary["removed_from_source"]))
            if sets or act_changed:
                ts = now()
                sets += ["field_sources = ?", "version = version + 1", "updated_at = ?"]
                args += [json.dumps(sources), ts]
                if source_type == "agent":
                    sets.append("last_sync = ?")
                    args.append(ts)
                c.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", args + [pid])
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        out = self.get_project(pid)
        if summary is not None:
            out["activity_summary"] = summary
        return out

    def update_activity_manual(self, pid: int, aid: int, changes: dict, actor: str) -> dict:
        self.validate_activity(changes, ACTIVITY_MANUAL_FIELDS)
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT * FROM project_activities WHERE id = ? AND project_id = ?", (aid, pid)).fetchone()
            if not row:
                raise ApiError(404, "not_found", f"activity {aid} not found in project {pid}")
            cur = self.row_to_activity(row)
            sources = cur["field_sources"]
            sets, args = [], []
            for k, v in changes.items():
                if cur.get(k) == v:
                    continue
                sets.append(f"{k} = ?")
                args.append(v)
                sources[k] = "manual"
                self._history(c, pid, "activity_updated", f"activity[{cur['activity_key']}].{k}", cur.get(k), v,
                              "manual", {"actor": actor})
            if sets:
                ts = now()
                c.execute(f"UPDATE project_activities SET {', '.join(sets)}, field_sources = ?, updated_at = ? "
                          "WHERE id = ?", args + [json.dumps(sources), ts, aid])
                c.execute("UPDATE projects SET version = version + 1, updated_at = ? WHERE id = ?", (ts, pid))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return self.row_to_activity(c.execute("SELECT * FROM project_activities WHERE id = ?", (aid,)).fetchone())

    def add_attachment(self, pid: int, body: dict) -> dict:
        self.get_project(pid)
        c = self.conn()
        cur = c.execute("INSERT INTO attachments (project_id, kind, url, uploaded_by, created_at) VALUES (?,?,?,?,?)",
                        (pid, body.get("kind", "photo"), body["url"], body.get("uploaded_by", "manual"), now()))
        return dict(c.execute("SELECT * FROM attachments WHERE id = ?", (cur.lastrowid,)).fetchone())

    def history(self, pid: int) -> list[dict]:
        rows = self.conn().execute("SELECT * FROM project_history WHERE project_id = ? ORDER BY id", (pid,))
        out = []
        for r in rows:
            d = dict(r)
            for f in ("previous_value", "new_value"):
                d[f] = json.loads(d[f]) if d[f] is not None else None
            out.append(d)
        return out

    def save_sync_run(self, body: dict) -> dict:
        c = self.conn()
        c.execute("INSERT OR REPLACE INTO sync_runs (id, started_at, finished_at, source_file, file_sha256, mode, "
                  "status, report) VALUES (?,?,?,?,?,?,?,?)",
                  (body["id"], body["started_at"], body.get("finished_at"), body.get("source_file"),
                   body.get("file_sha256"), body["mode"], body["status"],
                   json.dumps(body.get("report"), ensure_ascii=False)))
        return {"id": body["id"]}

    def list_sync_runs(self) -> list[dict]:
        rows = self.conn().execute("SELECT id, started_at, finished_at, source_file, file_sha256, mode, status "
                                   "FROM sync_runs ORDER BY started_at DESC LIMIT 100")
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    store: Store
    agent_key: str | None
    server_version = "ProjektManagment/1.1"

    def log_message(self, fmt, *args):  # never log headers (they carry the key)
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20 * 1024 * 1024:
            raise ApiError(413, "too_large", "request body too large")
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_json", str(exc))

    def _require_agent(self) -> None:
        if not self.agent_key:
            raise ApiError(503, "agent_api_disabled", "PM_AGENT_API_KEY is not configured on the server")
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token.encode(), self.agent_key.encode()):
            raise ApiError(401, "unauthorized", "invalid or missing agent credentials")

    def _route(self, method: str):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        s = self.store
        if parts[:1] != ["api"]:
            raise ApiError(404, "not_found", "unknown path")
        p = parts[1:]
        if method == "GET" and p == ["health"]:
            return 200, {"status": "ok"}
        if p[:1] == ["projects"]:
            if method == "GET" and len(p) == 1:
                return 200, {"projects": s.list_projects(q, with_activities=q.get("include") == "activities")}
            pid = self._int(p[1]) if len(p) > 1 else None
            if method == "GET" and len(p) == 2:
                return 200, s.get_project(pid)
            if method == "PATCH" and len(p) == 2:
                b = self._body()
                return 200, s.update_project(pid, b.get("changes", b), {"actor": b.get("actor", "dashboard_user"),
                                                                       "source_file": None},
                                             b.get("expected_version"), "manual")
            if method == "GET" and p[2:] == ["history"]:
                return 200, {"history": s.history(pid)}
            if method == "GET" and p[2:] == ["activities"]:
                return 200, {"activities": s.activities(pid)}
            if method == "PATCH" and len(p) == 4 and p[2] == "activities":
                b = self._body()
                return 200, s.update_activity_manual(pid, self._int(p[3]), b.get("changes", b),
                                                     b.get("actor", "dashboard_user"))
            if method == "POST" and p[2:] == ["attachments"]:
                return 201, s.add_attachment(pid, self._body())
        if p[:1] == ["agent"]:
            self._require_agent()
            if method == "GET" and p == ["agent", "schema"]:
                return 200, {"version": 2, "writable_fields": AGENT_WRITABLE, "stages": STAGES,
                             "activity_types": ACTIVITY_TYPES, "activity_fields": ACTIVITY_AGENT_FIELDS,
                             "record_categories": CATEGORIES, "priorities": sorted(PRIORITIES),
                             "statuses": sorted(STATUSES), "progress_statuses": sorted(PROGRESS),
                             "supports": ["expected_version", "duplicate_detection", "field_sources",
                                          "history", "sync_runs", "activities"]}
            if method == "GET" and p == ["agent", "projects"]:
                return 200, {"projects": s.list_projects(q, with_activities=True)}
            if method == "POST" and p == ["agent", "projects"]:
                b = self._body()
                return 201, s.create_project(b.get("project") or {}, self._sync_meta(b),
                                             activities=b.get("activities"), activity_scope=b.get("activity_scope"))
            if method == "PATCH" and len(p) == 3 and p[1] == "projects":
                b = self._body()
                return 200, s.update_project(self._int(p[2]), b.get("changes") or {}, self._sync_meta(b),
                                             b.get("expected_version"), "agent", activities=b.get("activities"),
                                             activity_scope=b.get("activity_scope"))
            if method == "POST" and p == ["agent", "sync-runs"]:
                return 201, s.save_sync_run(self._body())
            if method == "GET" and p == ["agent", "sync-runs"]:
                return 200, {"sync_runs": s.list_sync_runs()}
        raise ApiError(404, "not_found", f"no route for {method} {url.path}")

    @staticmethod
    def _sync_meta(b: dict) -> dict:
        m = b.get("sync") or {}
        return {"run_id": m.get("run_id"), "source_file": m.get("source_file"), "actor": m.get("actor", "plan_agent")}

    @staticmethod
    def _int(v: str) -> int:
        try:
            return int(v)
        except ValueError:
            raise ApiError(404, "not_found", "invalid id")

    def _handle(self, method: str):
        try:
            status, body = self._route(method)
            self._send(status, body)
        except ApiError as e:
            self._send(e.status, {"error": e.code, "message": e.message, **e.extra})
        except sqlite3.IntegrityError as e:
            self._send(409, {"error": "integrity_error", "message": str(e)})
        except Exception as e:  # pragma: no cover
            self._send(500, {"error": "internal_error", "message": type(e).__name__})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")


def make_server(db: str, host: str, port: int, agent_key: str | None) -> ThreadingHTTPServer:
    store = Store(db)
    handler = type("BoundHandler", (Handler,), {"store": store, "agent_key": agent_key})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Projekt_managment reference API")
    ap.add_argument("--db", default=os.environ.get("PM_DB_PATH", "data/projekt.db"))
    ap.add_argument("--host", default=os.environ.get("PM_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PM_PORT", "8080")))
    a = ap.parse_args(argv)
    key = os.environ.get("PM_AGENT_API_KEY")
    if not key:
        print("warning: PM_AGENT_API_KEY not set - /api/agent/* is disabled", file=sys.stderr)
    srv = make_server(a.db, a.host, a.port, key)
    print(f"Projekt_managment API on http://{a.host}:{srv.server_address[1]} (db={a.db})", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
