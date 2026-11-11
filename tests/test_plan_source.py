"""Automatic plan retrieval: file source, change detection, manual/automatic modes,
review handling and reliability.  Synthetic workbooks only."""
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import requests

import make_fixtures
from conftest import ROOT, ShturmanServer

PLAN = "work_plan_2026.xlsx"


def _server(tmp_path, inbox, **env):
    # Scheduler under test control: no real timer, start-up before 09:00 local time.
    e = {"PLAN_SOURCE_TYPE": "local", "PLAN_SOURCE_PATH": str(inbox), "PLAN_TARGET_MONTH": "2026-11",
         "PLAN_TIMEZONE": "Asia/Yekaterinburg", "PLAN_SOURCE_SETTLE_SECONDS": "0", "PLAN_WORK_CALENDAR": "RU",
         "PLAN_SCHEDULER_TEST_HOOKS": "1", "PLAN_SCHEDULER_TEST_NOW": "2026-11-11T02:00:00Z"}
    e.update(env)
    return ShturmanServer(tmp_path / "srv", extra_env=e).start()


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    return d


@pytest.fixture
def srv(tmp_path, inbox):
    s = _server(tmp_path, inbox)
    yield s
    s.stop()


def put(inbox, variant=1, **kw):
    """Replace the planning file the way a copy tool would: write elsewhere, then rename."""
    tmp = inbox.parent / "staging.xlsx"
    make_fixtures.make_monthly_workbook(tmp, variant, **kw)
    shutil.move(tmp, inbox / PLAN)


def check(s, **body):
    r = requests.post(s.url + "/api/plan-source/check", json=body, timeout=180)
    return r.status_code, r.json()


def status(s):
    return requests.get(s.url + "/api/plan-source/status", timeout=10).json()


def projects(s):
    return requests.get(s.url + "/api/projects", timeout=10).json()


def review(s, kind):
    return [r for r in requests.get(s.url + "/api/review-queue?kind=" + kind, timeout=10).json() if not r["resolved"]]


def first_sync(s, inbox):
    put(inbox, 1)
    code, r = check(s)
    assert code == 200 and r["result"]["status"] == "awaiting_approval", r
    assert projects(s) == []                       # analysis only
    a = requests.post(s.url + "/api/plan-source/approve", json={"confirm": True, "approved_by": "pytest"}, timeout=180)
    assert a.status_code == 200, a.text
    assert a.json()["result"]["status"] == "synced"
    return projects(s)


def by_name(ps, name):
    return next(p for p in ps if p["project_name"] == name)


# ---------------------------------------------------------------- unit level
def test_adapter_and_month_unit_checks(tmp_path):
    out = subprocess.run(["node", "tests/js/plan_source_unit.js", str(tmp_path)], cwd=ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert all(res.values()) and len(res) == 10


# ---------------------------------------------------------------- configuration
def test_not_configured_by_default(shturman):
    st = status(shturman)
    assert st["configured"] is False and st["status"] == "not_configured"
    assert st["schedule"]["schedule_active"] is False
    code, r = check(shturman)
    assert r["result"]["status"] == "not_configured"


def test_status_shows_configuration_without_secrets(srv, inbox):
    st = status(srv)
    assert st["configured"] and st["source"] == {"type": "local", "location": str(inbox), "pattern": None}
    assert st["auto_sync_enabled"] is False and st["current_month"] == "2026-11" and st["timezone"] == "Asia/Yekaterinburg"
    assert "secret-test-key" not in json.dumps(st)


# ---------------------------------------------------------------- manual mode (A)
def test_manual_mode_detects_waits_and_syncs_once(srv, inbox):
    ps = first_sync(srv, inbox)
    n = len(ps)
    assert {p["record_category"] for p in ps} == {"main_project", "additional_work", "backlog"}
    assert all(p["status"] is None for p in ps)                     # no stage confirmed by the import
    st = status(srv)
    assert st["status"] == "up_to_date" and st["last_sync_at"] and st["current_version"]["status"] == "synced"

    # unchanged file (same SHA-256, even with a new modification time) is skipped
    (inbox / PLAN).touch()
    code, r = check(srv)
    assert r["result"]["status"] == "unchanged"
    assert len(requests.get(srv.url + "/api/plan-imports").json()) == 1      # no new analysis run
    assert len(projects(srv)) == n

    # a changed file is analysed and waits for approval; nothing is written before
    put(inbox, 4)
    code, r = check(srv)
    assert r["result"]["status"] == "awaiting_approval"
    gac = by_name(projects(srv), "GAC S9 Комплект защит")
    assert gac["workload"]["actual_hours"] == 0
    pv = requests.get(srv.url + "/api/plan-source/preview").json()
    kinds = {(p["project"], p["change_type"]) for p in pv["projects"]}
    assert ("GAC S9 Комплект защит", "update") in kinds and ("Chery Tiggo 4 Защита картера", "new") in kinds
    gac_change = next(p for p in pv["projects"] if p["project"] == "GAC S9 Комплект защит")
    assert any(a.startswith("~") for a in gac_change["activity_changes"])

    a = requests.post(srv.url + "/api/plan-source/approve", json={"confirm": True}, timeout=180).json()
    assert a["result"]["status"] == "synced"
    after = projects(srv)
    assert len(after) == n + 1                                      # Lada added, nothing duplicated
    assert by_name(after, "GAC S9 Комплект защит")["workload"]["actual_hours"] == 5
    ids = [p["external_project_id"] for p in after]
    assert len(ids) == len(set(ids))
    # history records the change
    hist = by_name(after, "GAC S9 Комплект защит")["history"]
    assert any(h["source"] == "agent" for h in hist)


def test_approve_requires_confirmation_and_pending_version(srv, inbox):
    assert requests.post(srv.url + "/api/plan-source/approve", json={}).status_code == 400
    r = requests.post(srv.url + "/api/plan-source/approve", json={"confirm": True})
    assert r.status_code == 409 and r.json()["result"]["status"] == "nothing_pending"


def test_dry_run_button_never_writes(srv, inbox):
    ps = first_sync(srv, inbox)
    synced_bytes = (inbox / PLAN).read_bytes()
    put(inbox, 4)
    code, r = check(srv, dry_run=True)
    assert r["result"]["status"] == "dry_run"
    assert len(projects(srv)) == len(ps)
    # the synced version again (same bytes): a forced dry run keeps it synced
    (inbox / PLAN).write_bytes(synced_bytes)
    code, r = check(srv, dry_run=True)
    assert r["result"]["version"]["status"] == "synced"


# ---------------------------------------------------------------- automatic mode (B)
def test_automatic_mode_applies_only_safe_changes(srv, inbox):
    ps = first_sync(srv, inbox)
    gac = by_name(ps, "GAC S9 Комплект защит")
    honda = by_name(ps, "Honda CR-V (1995-2001) Защита картера+КПП")
    # a person confirms a stage and edits a planning-owned field
    requests.post(f"{srv.url}/api/projects/{gac['id']}/stage", json={"stage": "development", "version": gac["version"]})
    h = requests.get(f"{srv.url}/api/projects/{honda['id']}").json()
    assert requests.patch(f"{srv.url}/api/projects/{honda['id']}", json={"priority": "high", "version": h["version"]}).ok
    gac = requests.get(f"{srv.url}/api/projects/{gac['id']}").json()
    entered = gac["stage_entered_at"]

    assert requests.post(srv.url + "/api/plan-source/auto-sync", json={"enabled": True}).status_code == 400
    st = requests.post(srv.url + "/api/plan-source/auto-sync", json={"enabled": True, "confirm": True}).json()
    assert st["auto_sync_enabled"] is True

    put(inbox, 4)
    code, r = check(srv)
    assert r["result"]["status"] == "synced", r
    after = projects(srv)
    g2 = by_name(after, "GAC S9 Комплект защит")
    assert g2["workload"]["actual_hours"] == 5                       # hours of an existing activity applied
    assert g2["status"] == "development" and g2["stage_entered_at"] == entered   # stage untouched
    assert by_name(after, "Honda CR-V (1995-2001) Защита картера+КПП")["priority"] == "high"   # manual edit kept
    assert not any(p["project_name"] == "Chery Tiggo 4 Защита картера" for p in after)   # new project NOT created
    held = review(srv, "plan_auto_sync_held")
    assert any(i["payload"]["project_name"] == "Chery Tiggo 4 Защита картера" for i in held)
    v = status(srv)["current_version"]
    assert v["status"] == "synced" and v["sync_scope"] == "automatic" and v["held_for_review"] >= 1

    # the same file again: nothing new, no duplicate review items
    code, r = check(srv)
    assert r["result"]["status"] == "unchanged"
    assert len(review(srv, "plan_auto_sync_held")) == len(held)


def test_automatic_mode_never_creates_the_first_import(tmp_path, inbox):
    s = _server(tmp_path, inbox, PLAN_AUTO_SYNC_ENABLED="true")
    try:
        put(inbox, 1)
        code, r = check(s)
        assert r["result"]["status"] == "awaiting_approval"          # no approved mapping yet
        assert projects(s) == []
        assert review(s, "plan_source_issue")
        code, r = check(s)                                           # still waiting, still no writes
        assert r["result"]["status"] == "unchanged" and projects(s) == []
    finally:
        s.stop()


def test_new_month_keeps_previous_month_and_needs_review_in_auto_mode(srv, inbox):
    first_sync(srv, inbox)
    requests.post(srv.url + "/api/plan-source/auto-sync", json={"enabled": True, "confirm": True})
    srv.extra_env["PLAN_TARGET_MONTH"] = "2026-12"
    srv.restart()
    put(inbox, 1, december=True)
    code, r = check(srv)
    assert r["result"]["status"] == "synced", r
    gac = by_name(projects(srv), "GAC S9 Комплект защит")
    months = {a["plan_month"] for a in gac["activities"]}
    assert months == {"2026-11"}                                     # December activities held, November kept
    assert any(i["payload"]["plan_month"] == "2026-12" for i in review(srv, "plan_auto_sync_held"))


# ---------------------------------------------------------------- review / structure problems
def test_ambiguous_month_sheets_stop_and_go_to_review(srv, inbox):
    put(inbox, 1, duplicate_november=True)
    code, r = check(srv)
    assert r["result"]["status"] == "blocked"
    assert projects(srv) == []
    items = review(srv, "plan_source_issue")
    assert items and "Ноябрь" in items[0]["reason"]
    assert status(srv)["status"] == "blocked"


def test_summary_sheet_is_never_imported(srv, inbox):
    ps = first_sync(srv, inbox)
    sheets = {a["source_sheet"] for p in ps for a in p["activities"]}
    assert sheets == {"Ноябрь"}


@pytest.mark.parametrize("content,code", [(b"", "empty"), (b"not a workbook at all", "corrupt")])
def test_empty_and_corrupt_files_do_not_touch_data(srv, inbox, content, code):
    ps = first_sync(srv, inbox)
    before = srv.data_file.read_bytes()
    (inbox / PLAN).write_bytes(content)
    c, r = check(srv)
    assert r["result"]["status"] == "error" and r["result"]["code"] == code
    assert len(projects(srv)) == len(ps)
    data_before, data_after = json.loads(before), json.loads(srv.data_file.read_text())
    assert data_after["projects"] == data_before["projects"]
    assert status(srv)["last_error"]
    if code == "corrupt":
        assert review(srv, "plan_source_issue")


def test_unexpected_structure_fails_safely(srv, inbox):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "Лист1"
    wb.active["A1"] = "что-то совсем другое"
    wb.save(inbox / PLAN)
    c, r = check(srv)
    assert r["result"]["status"] == "failed"
    assert projects(srv) == []
    assert review(srv, "plan_source_issue")
    assert status(srv)["current_version"] is None or status(srv)["status"] == "failed"


def test_missing_file_and_locked_file(srv, inbox):
    c, r = check(srv)
    assert r["result"]["status"] == "error" and r["result"]["code"] == "not_found"
    put(inbox, 1)
    (inbox / ("~$" + PLAN)).write_text("owner")
    c, r = check(srv)
    assert r["result"]["status"] == "waiting" and r["result"]["code"] == "locked"
    assert requests.get(srv.url + "/api/plan-imports").json() == []      # file not read
    (inbox / ("~$" + PLAN)).unlink()
    c, r = check(srv)
    assert r["result"]["status"] == "awaiting_approval"


def test_file_still_being_written_is_not_read(tmp_path, inbox):
    s = _server(tmp_path, inbox, PLAN_SOURCE_SETTLE_SECONDS="3600")
    try:
        put(inbox, 1)
        c, r = check(s)
        assert r["result"]["status"] == "waiting" and r["result"]["code"] == "unstable"
        assert requests.get(s.url + "/api/plan-imports").json() == []
    finally:
        s.stop()


# ---------------------------------------------------------------- concurrency and restarts
def test_no_overlapping_runs(srv, inbox):
    put(inbox, 1)
    results = []

    def go():
        results.append(check(srv))
    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(r[1]["result"]["status"] for r in results)
    assert statuses.count("awaiting_approval") == 1, statuses
    assert all(s in ("awaiting_approval", "busy", "unchanged") for s in statuses)
    assert len(requests.get(srv.url + "/api/plan-imports").json()) == 1


def test_restart_during_processing_is_recovered(srv, inbox):
    put(inbox, 1)
    check(srv)
    srv.stop()
    data = json.loads(srv.data_file.read_text())
    data["plan_source_versions"][0]["status"] = "syncing"            # crash in the middle of a sync
    data["plan_source"]["running"] = {"phase": "sync"}
    imp = next(i for i in data["plan_imports"] if i["id"] == data["plan_source_versions"][0]["import_id"])
    imp["status"] = "syncing"
    srv.data_file.write_text(json.dumps(data))
    srv.start()
    st = status(srv)
    assert st["current_version"]["status"] == "interrupted" and st["running"] is None
    assert projects(srv) == []                                       # never marked synced
    a = requests.post(srv.url + "/api/plan-source/approve", json={"confirm": True}, timeout=180).json()
    assert a["result"]["status"] == "synced"
    n = len(projects(srv))
    c, r = check(srv)
    assert r["result"]["status"] == "unchanged" and len(projects(srv)) == n


def test_versions_and_status_survive_restart(srv, inbox):
    first_sync(srv, inbox)
    before = status(srv)
    srv.restart()
    after = status(srv)
    assert after["current_version"]["sha256"] == before["current_version"]["sha256"]
    assert after["last_sync_at"] == before["last_sync_at"] and after["status"] == "up_to_date"
    assert check(srv)[1]["result"]["status"] == "unchanged"


def test_import_view_has_sync_status_section():
    html = (ROOT / "public" / "index.html").read_text()
    for text in ("Автоматическое обновление плана", "Проверить обновления", "Пробный анализ", "Просмотреть изменения",
                 "Утвердить синхронизацию", "Включить автосинхронизацию", "/api/plan-source/status"):
        assert text in html
