"""Four related fixes/features on the engineering-project side of «Доска проектов»:

1. Readable project history journal (server/historyLabels.js) - no raw JSON, grouped events,
   technical fields admin-only.
2. Stage-duration breakdown on the project card (server/projectsModel.js's buildStageHistory).
3. Morning Telegram digest: schedule fixed to 09:00 Asia/Yekaterinburg, plus a new КО-changes
   section (server/telegram/engineeringDigest.js).

See tests/js/history_journal_unit.js for the pure-logic checks; this file additionally proves the
server wires it all together correctly, with the real role-gating (admin vs ko_viewer), and that
none of it mutates any stored data.
"""
import json
import subprocess

import pytest
import requests

from conftest import ROOT, ShturmanServer

ADMIN_USER, ADMIN_PASS = "admin", "s3cret-admin!"
AGENT_KEY = "secret-test-key"
H = {"Authorization": f"Bearer {AGENT_KEY}"}


def test_history_journal_unit_scenarios(tmp_path):
    r = subprocess.run(["node", "tests/js/history_journal_unit.js", str(tmp_path)], cwd=ROOT,
                        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == "ok"


def make_server(tmp_path, **env):
    e = {"DASHBOARD_USERNAME": ADMIN_USER, "DASHBOARD_PASSWORD": ADMIN_PASS,
         "DASHBOARD_SESSION_SECRET": "test-session-secret"}
    e.update(env)
    return ShturmanServer(tmp_path / "srv", extra_env=e).start()


def admin_session(srv):
    s = requests.Session()
    r = s.post(srv.url + "/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=10)
    assert r.status_code == 200, r.text
    return s


def csrf_of(session, srv):
    return session.get(srv.url + "/api/whoami", timeout=10).json()["csrf_token"]


def create_manager(session, srv, full_name="Менеджер", username="manager1"):
    body = {"full_name": full_name, "username": username}
    r = session.post(srv.url + "/api/users", json=body, headers={"X-CSRF-Token": csrf_of(session, srv)}, timeout=10)
    assert r.status_code == 201, r.text
    created = r.json()
    manager = requests.Session()
    lr = manager.post(srv.url + "/api/login", json={"username": username, "password": created["generated_password"]}, timeout=10)
    assert lr.status_code == 200, lr.text
    return manager


def create_project(srv, **fields):
    body = {"manufacturer": "TEST", "vehicle_model": "M", "product_type": "Деталь"}
    body.update(fields)
    r = requests.post(srv.url + "/api/agent/projects", headers=H, json={"project": body}, timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def confirm_stage(admin, srv, project_id, stage, version=1):
    r = admin.post(srv.url + f"/api/projects/{project_id}/stage", json={"stage": stage, "version": version}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def sync_activity(srv, project_id, activity, run_id="run-1"):
    r = requests.patch(srv.url + f"/api/agent/projects/{project_id}", headers=H, json={
        "sync": {"run_id": run_id, "source_file": "AfM CIS+OEM 2026.xlsx"},
        "activity_scope": {"plan_month": "2026-11"}, "activities": [activity]}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ 1: readable journal, no raw JSON

def test_journal_hides_technical_fields_from_everyone_but_shows_the_rest(tmp_path):
    srv = make_server(tmp_path)
    try:
        admin = admin_session(srv)
        pid = create_project(srv)
        activity = {"activity_key": "a1", "plan_month": "2026-11", "name": "Разработка защиты",
                    "planned_hours": 10, "actual_hours": 5, "details": {"mapping_rule": "v1"}}
        sync_activity(srv, pid, activity, run_id="run-1")
        changed = dict(activity, actual_hours=7, details={
            "delivery_chance": "высокий", "probability_note": "90%", "sales_channel": "OEM",
            "mapping_rule": "v2", "unmapped_columns": {"X": 1}, "actual_hours_source": "day columns"})
        sync_activity(srv, pid, changed, run_id="run-2")

        padmin = admin.get(srv.url + f"/api/projects/{pid}", timeout=10).json()
        assert "history" in padmin and "activity" in padmin, "admin keeps the raw fields"
        assert padmin["activities"][0]["details"], "admin keeps the raw per-activity details"
        latest = padmin["history_events"][0]
        labels = [c["label"] for c in latest["changes"]]
        assert "Фактические часы (Разработка защиты)" in labels
        assert latest["hidden_count"] == 1
        assert latest["raw"], "admin gets the raw rows for the diagnostic toggle"

        manager = create_manager(admin, srv)
        pview = manager.get(srv.url + f"/api/projects/{pid}", timeout=10).json()
        assert "history" not in pview and "activity" not in pview
        assert "details" not in pview["activities"][0]
        full_text = json.dumps(pview)
        for leak in ("delivery_chance", "probability_note", "sales_channel", "mapping_rule",
                     "unmapped_columns", "actual_hours_source"):
            assert leak not in full_text, f"leaked technical field into manager's response: {leak}"
        mlatest = pview["history_events"][0]
        assert "raw" not in mlatest
        assert [c["label"] for c in mlatest["changes"]] == ["Фактические часы (Разработка защиты)"]

        # also check the list endpoint (/api/projects) for the same manager - same redaction
        plist = manager.get(srv.url + "/api/projects", timeout=10).json()
        list_text = json.dumps(plist)
        assert "mapping_rule" not in list_text and '"history"' not in list_text
    finally:
        srv.stop()


def test_history_endpoint_gates_raw_technical_data_by_role(tmp_path):
    """GET /api/projects/:id/history had no role check at all - any authenticated session, ko_viewer
    included, got the raw project.history rows (the same technical/diagnostic fields
    presentHistoryEvents/redactTechnicalForRole exist specifically to keep out of a manager's
    hands on every other endpoint). The administrator must keep full raw access; a manager must
    get the same readable, technical-field-free journal the "История" tab already shows via
    GET /:id; and a request with no session at all must be refused before it ever reaches the
    route (dashboardAuth.requireDashboardSession, mounted ahead of every /api/projects route)."""
    srv = make_server(tmp_path)
    try:
        admin = admin_session(srv)
        pid = create_project(srv)
        activity = {"activity_key": "a1", "plan_month": "2026-11", "name": "Разработка защиты",
                    "planned_hours": 10, "actual_hours": 5, "details": {"mapping_rule": "v1"}}
        sync_activity(srv, pid, activity, run_id="run-1")
        changed = dict(activity, actual_hours=7, details={
            "delivery_chance": "высокий", "probability_note": "90%", "sales_channel": "OEM",
            "mapping_rule": "v2", "unmapped_columns": {"X": 1}, "actual_hours_source": "day columns"})
        sync_activity(srv, pid, changed, run_id="run-2")

        # administrator: unchanged, full raw rows (same array GET /:id/history always returned)
        r_admin = admin.get(srv.url + f"/api/projects/{pid}/history", timeout=10)
        assert r_admin.status_code == 200
        admin_rows = r_admin.json()
        assert isinstance(admin_rows, list) and len(admin_rows) >= 2
        assert any("actual_hours" in row.get("action", "") for row in admin_rows), "raw rows, not a readable journal"
        admin_text = json.dumps(admin_rows)
        assert "mapping_rule" in admin_text, "admin must keep full technical detail here, same as before"

        # ko_viewer manager: readable journal (same shape/content as GET /:id's history_events),
        # never the raw technical rows
        manager = create_manager(admin, srv)
        r_mgr = manager.get(srv.url + f"/api/projects/{pid}/history", timeout=10)
        assert r_mgr.status_code == 200
        mgr_events = r_mgr.json()
        assert isinstance(mgr_events, list) and mgr_events, "still gets a readable journal, not a 403/empty body"
        assert all("raw" not in e for e in mgr_events), "no raw diagnostic rows leaked to a manager"
        mgr_text = json.dumps(mgr_events)
        for leak in ("delivery_chance", "probability_note", "sales_channel", "mapping_rule",
                     "unmapped_columns", "actual_hours_source"):
            assert leak not in mgr_text, f"leaked technical field into manager's /history response: {leak}"
        # same readable content the manager already sees on the card's own История tab via GET /:id
        pview = manager.get(srv.url + f"/api/projects/{pid}", timeout=10).json()
        assert [e["changes"] for e in mgr_events] == [e["changes"] for e in pview["history_events"]]

        # no session at all: refused before reaching the route, same as every other /api/projects GET
        r_anon = requests.get(srv.url + f"/api/projects/{pid}/history", timeout=10)
        assert r_anon.status_code == 401
    finally:
        srv.stop()


def test_journal_groups_one_sync_into_one_expandable_event(tmp_path):
    srv = make_server(tmp_path)
    try:
        admin = admin_session(srv)
        pid = create_project(srv)
        r = requests.patch(srv.url + f"/api/agent/projects/{pid}", headers=H, json={
            "sync": {"run_id": "run-1", "source_file": "AfM.xlsx"},
            "changes": {"assigned_engineer": "Иванов", "planned_deadline": "2026-12-01"},
            "activity_scope": {"plan_month": "2026-11"},
            "activities": [{"activity_key": "a1", "plan_month": "2026-11", "name": "Разработка", "planned_hours": 5}]
        }, timeout=10)
        assert r.status_code == 200, r.text
        events = admin.get(srv.url + f"/api/projects/{pid}", timeout=10).json()["history_events"]
        sync_event = next(e for e in events if "Синхронизация" in e["title"])
        assert len(sync_event["changes"]) >= 2, "the field update and the new activity are one grouped event"
    finally:
        srv.stop()


def test_journal_never_mutates_stored_history(tmp_path):
    srv = make_server(tmp_path)
    try:
        admin = admin_session(srv)
        pid = create_project(srv)
        confirm_stage(admin, srv, pid, "scanning")
        before = admin.get(srv.url + f"/api/projects/{pid}", timeout=10).json()["history"]
        for _ in range(3):
            admin.get(srv.url + f"/api/projects/{pid}", timeout=10)
        after = admin.get(srv.url + f"/api/projects/{pid}", timeout=10).json()["history"]
        assert before == after, "repeatedly reading the journal must never change the stored history"
    finally:
        srv.stop()


# ------------------------------------------------------------------ 2: stage duration

def test_stage_history_on_the_project_card_tracks_repeat_visits(tmp_path):
    srv = make_server(tmp_path)
    try:
        admin = admin_session(srv)
        pid = create_project(srv)
        confirm_stage(admin, srv, pid, "scanning", version=1)
        confirm_stage(admin, srv, pid, "development", version=2)
        confirm_stage(admin, srv, pid, "scanning", version=3)
        confirm_stage(admin, srv, pid, "development", version=4)

        p = admin.get(srv.url + f"/api/projects/{pid}", timeout=10).json()
        sh = p["stage_history"]
        scanning_visits = [v for v in sh["completed"] if v["stage"] == "scanning"]
        assert len(scanning_visits) == 2
        assert scanning_visits[0]["visit"] == 1 and scanning_visits[1]["visit"] == 2
        assert sh["current"]["stage"] == "development" and sh["current"]["visit"] == 2
        assert sh["current"]["days"] == 0
        assert p["days_in_stage"] == sh["current"]["days"], "must agree with the existing live field"

        # a ko_viewer manager sees the exact same breakdown (view-only data, not an admin secret)
        manager = create_manager(admin, srv)
        pv = manager.get(srv.url + f"/api/projects/{pid}", timeout=10).json()
        assert pv["stage_history"] == sh
    finally:
        srv.stop()


def test_stage_history_no_data_when_nothing_recorded(tmp_path):
    srv = make_server(tmp_path)
    try:
        admin = admin_session(srv)
        pid = create_project(srv)  # never confirmed into any stage
        p = admin.get(srv.url + f"/api/projects/{pid}", timeout=10).json()
        assert p["stage_history"] == {"completed": [], "current": None}
        assert p["days_in_stage"] is None
    finally:
        srv.stop()


# ------------------------------------------------------------------ Kanban/filtering unaffected

def run_browser(srv, project_id, cookie):
    out = subprocess.run(["node", "tests/js/history_journal_browser.js", srv.url, project_id, cookie], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    if out.returncode == 3:
        pytest.skip("Playwright/Chromium not available: " + out.stdout + out.stderr)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_browser_admin_sees_diagnostic_toggle_manager_does_not(tmp_path):
    srv = make_server(tmp_path)
    try:
        admin = admin_session(srv)
        pid = create_project(srv, manufacturer="GEELY", vehicle_model="Coolray")
        confirm_stage(admin, srv, pid, "scanning", version=1)
        activity = {"activity_key": "a1", "plan_month": "2026-11", "name": "Разработка защиты",
                    "planned_hours": 10, "actual_hours": 5, "details": {"mapping_rule": "v1"}}
        sync_activity(srv, pid, activity, run_id="run-1")
        changed = dict(activity, actual_hours=7, details={"delivery_chance": "высокий", "mapping_rule": "v2"})
        sync_activity(srv, pid, changed, run_id="run-2")

        admin_cookie = "shturman_session=" + admin.cookies.get("shturman_session")
        r_admin = run_browser(srv, pid, admin_cookie)
        assert not r_admin["page_errors"]
        assert r_admin["has_stage_duration_heading"] and r_admin["has_history_heading"]
        assert not r_admin["looks_like_raw_json"], "no raw JSON braces anywhere in the visible card text"
        assert "Фактические часы (Разработка защиты)" in r_admin["visible_change_labels"]
        assert r_admin["admin_raw_toggle_count"] >= 1, "admin must have a diagnostic JSON toggle for the technical change"
        assert "delivery_chance" in r_admin["raw_json_after_click"], "admin's toggle reveals the real technical data"

        manager = create_manager(admin, srv)
        manager_cookie = "shturman_session=" + manager.cookies.get("shturman_session")
        r_mgr = run_browser(srv, pid, manager_cookie)
        assert not r_mgr["page_errors"]
        assert r_mgr["has_stage_duration_heading"] and r_mgr["has_history_heading"]
        assert not r_mgr["looks_like_raw_json"]
        assert "Фактические часы (Разработка защиты)" in r_mgr["visible_change_labels"]
        assert r_mgr["admin_raw_toggle_count"] == 0, "a ko_viewer manager must never see the diagnostic JSON toggle"
    finally:
        srv.stop()


def test_board_list_and_filtering_unaffected_by_the_new_fields(tmp_path):
    srv = make_server(tmp_path)
    try:
        admin = admin_session(srv)
        pid = create_project(srv, manufacturer="GEELY")
        confirm_stage(admin, srv, pid, "scanning")
        listed = admin.get(srv.url + "/api/projects?status=scanning", timeout=10).json()
        assert any(p["id"] == pid for p in listed)
        listed2 = admin.get(srv.url + "/api/projects?make=GEELY", timeout=10).json()
        assert any(p["id"] == pid for p in listed2)
    finally:
        srv.stop()
