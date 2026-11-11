"""Daily working-day schedule (09:00 Asia/Yekaterinburg, official RU production calendar)
and the board's «Обновить» button.  Synthetic workbooks only.

09:00 in Yekaterinburg is 04:00 UTC (UTC+5, no daylight saving time)."""
import json
import shutil
import subprocess
import threading
import time

import pytest
import requests

import make_fixtures
from conftest import ROOT, ShturmanServer

PLAN = "work_plan_2026.xlsx"
WED = "2026-11-11"          # ordinary working Wednesday
SAT, SUN = "2026-11-14", "2026-11-15"
HOLIDAY = "2026-11-04"      # День народного единства (Wednesday)
TRANSFER_OFF = "2026-01-09"  # Friday made a day off by Resolution No. 1466 (from Saturday 3 January)


def at(date, hhmm="09:00"):
    h, m = map(int, hhmm.split(":"))
    return f"{date}T{h - 5:02d}:{m:02d}:00Z" if h >= 5 else None


def make_server(tmp_path, inbox, start_now="2026-11-11T02:00:00Z", **env):
    e = {"PLAN_SOURCE_TYPE": "local", "PLAN_SOURCE_PATH": str(inbox), "PLAN_TARGET_MONTH": "2026-11",
         "PLAN_TIMEZONE": "Asia/Yekaterinburg", "PLAN_WORK_CALENDAR": "RU", "PLAN_SCHEDULE_TIME": "09:00",
         "PLAN_AUTO_MONITOR_ENABLED": "true", "PLAN_SCHEDULE_ENABLED": "true", "PLAN_AUTO_SYNC_ENABLED": "true",
         "PLAN_REQUIRE_APPROVAL": "true", "PLAN_SOURCE_SETTLE_SECONDS": "0",
         "PLAN_SCHEDULER_TEST_HOOKS": "1", "PLAN_SCHEDULER_TEST_NOW": start_now}
    e.update(env)
    return ShturmanServer(tmp_path / "srv", extra_env=e).start()


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    return d


@pytest.fixture
def srv(tmp_path, inbox):
    s = make_server(tmp_path, inbox)
    yield s
    s.stop()


def put(inbox, variant=1, **kw):
    tmp = inbox.parent / "staging.xlsx"
    make_fixtures.make_monthly_workbook(tmp, variant, **kw)
    shutil.move(tmp, inbox / PLAN)


def tick(s, now):
    return requests.post(s.url + "/api/plan-source/_test/tick", json={"now": now}, timeout=180).json()


def clock(s, now):
    requests.post(s.url + "/api/plan-source/_test/clock", json={"now": now}, timeout=10)


def update(s):
    r = requests.post(s.url + "/api/plan-source/update", json={}, timeout=180)
    assert r.status_code == 200, r.text
    return r.json()


def status(s):
    return requests.get(s.url + "/api/plan-source/status", timeout=10).json()


def runs(s, trigger=None):
    h = status(s)["schedule"]["history"]
    return [r for r in h if trigger is None or r["trigger"] == trigger]


def projects(s):
    return requests.get(s.url + "/api/projects", timeout=10).json()


def imports(s):
    return requests.get(s.url + "/api/plan-imports", timeout=10).json()


def first_sync(s, inbox):
    """Initial import: the mapping of a new file structure is always approved by a person."""
    put(inbox, 1)
    r = update(s)
    assert r["ui"]["state"] == "review_required" and projects(s) == []
    a = requests.post(s.url + "/api/plan-source/approve", json={"confirm": True, "approved_by": "pytest"}, timeout=180)
    assert a.json()["result"]["status"] == "synced", a.text
    return projects(s)


# ------------------------------------------------------------------ calendar and time zone
def test_calendar_2026_matches_resolution_1466():
    out = subprocess.run(["node", "-e", """
const c = require('./server/planSource/workCalendar');
const y = c.load('RU').years['2026'];
const d = (x) => c.dayInfo('RU', x).working;
let e2027 = null; try { c.dayInfo('RU', '2027-01-11'); } catch (e) { e2027 = e.code; }
console.log(JSON.stringify({ days: y.working_days, problem: y.problem,
  jan9: d('2026-01-09'), jan12: d('2026-01-12'), dec31: d('2026-12-31'), mar9: d('2026-03-09'), may11: d('2026-05-11'),
  nov4: d('2026-11-04'), nov11: d('2026-11-11'), sat: d('2026-11-14'), e2027,
  utc: c.zonedInstant('2026-11-11', '09:00', 'Asia/Yekaterinburg').toISOString() }));
"""], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["days"] == 247 and r["problem"] is None                      # official total for a five-day week
    assert not r["jan9"] and not r["dec31"]                                # transfers of 3 and 4 January
    assert not r["mar9"] and not r["may11"] and not r["nov4"] and not r["sat"]
    assert r["jan12"] and r["nov11"]
    assert r["e2027"] == "calendar_unavailable"                            # 2027 not approved yet: no guessing
    assert r["utc"] == "2026-11-11T04:00:00.000Z"


# ------------------------------------------------------------------ 1-5: schedule rules
def test_working_day_run_at_0900_yekaterinburg(srv, inbox):
    put(inbox, 1)
    assert tick(srv, "2026-11-11T03:59:00Z")["action"] == "not_due"      # 08:59 local
    r = tick(srv, at(WED))
    assert r["action"] == "ran" and r["date"] == WED
    rec = runs(srv, "schedule")[0]
    assert rec["scheduled_date"] == WED and rec["scheduled_for"] == "2026-11-11T04:00:00.000Z"
    assert rec["phases"]["check"] == "ok" and rec["phases"]["analysis"] == "awaiting_approval"
    assert rec["phases"]["sync"] == "not_run" and rec["outcome"] == "review_required"   # analysis is not a sync
    assert status(srv)["last_sync_at"] is None
    clock(srv, "2026-11-11T05:00:00Z")
    nxt = status(srv)["schedule"]
    assert nxt["next_run_date"] == "2026-11-12" and nxt["next_run_at"] == "2026-11-12T04:00:00.000Z"


@pytest.mark.parametrize("day,reason", [(SAT, "суббота"), (SUN, "воскресенье"), (HOLIDAY, "праздн"),
                                        (TRANSFER_OFF, "перенос")])
def test_no_automatic_run_on_non_working_days(srv, inbox, day, reason):
    put(inbox, 1)
    r = tick(srv, at(day))
    assert r["action"] == "non_working" and reason in r["reason"]
    rec = runs(srv, "schedule")[0]
    assert rec["outcome"] == "skipped_non_working" and rec["phases"]["check"] == "not_run"
    assert imports(srv) == []                                            # the file was not even read


def test_next_run_skips_weekend_and_holidays(srv):
    clock(srv, "2026-11-13T05:00:00Z")                                   # Friday 10:00
    assert status(srv)["schedule"]["next_run_date"] == "2026-11-16"     # Monday
    clock(srv, "2026-11-03T05:00:00Z")                                   # Tuesday before 4 November
    assert status(srv)["schedule"]["next_run_date"] == "2026-11-05"
    clock(srv, "2026-12-30T05:00:00Z")                                   # 31 Dec off, 2027 not approved
    sc = status(srv)["schedule"]
    assert sc["next_run_at"] is None and "2027" in sc["schedule_error"]


def test_government_transferred_working_weekend(tmp_path, inbox):
    """2026 has no working Saturdays; a calendar that designates one must be honoured."""
    cal = json.loads((ROOT / "config" / "work_calendar" / "RU.json").read_text())
    y = cal["years"]["2026"]
    y["working_weekend_dates"] = [{"date": SAT, "reason": "synthetic transfer (test)"}]
    y["expected_working_days"] = 248
    f = tmp_path / "cal_test.json"
    f.write_text(json.dumps(cal, ensure_ascii=False))
    s = make_server(tmp_path, inbox, PLAN_WORK_CALENDAR=str(f))
    try:
        put(inbox, 1)
        clock(s, "2026-11-13T05:00:00Z")                                  # Friday 10:00
        assert status(s)["schedule"]["next_run_date"] == SAT
        r = tick(s, at(SAT))
        assert r["action"] == "ran"
        assert tick(s, at(SUN))["action"] == "non_working"
    finally:
        s.stop()


def test_unverified_or_missing_year_stops_the_run(tmp_path, inbox, srv):
    put(inbox, 1)
    r = tick(srv, at("2027-01-11"))
    assert r["action"] == "calendar_unavailable"
    assert runs(srv, "schedule")[0]["outcome"] == "calendar_unavailable" and imports(srv) == []
    cal = json.loads((ROOT / "config" / "work_calendar" / "RU.json").read_text())
    cal["years"]["2026"]["verified"] = False
    f = tmp_path / "cal_unverified.json"
    f.write_text(json.dumps(cal, ensure_ascii=False))
    s2 = make_server(tmp_path / "b", inbox, PLAN_WORK_CALENDAR=str(f))
    try:
        assert tick(s2, at(WED))["action"] == "calendar_unavailable"
    finally:
        s2.stop()


# ------------------------------------------------------------------ 6-7: restarts and missed runs
def test_no_duplicate_run_after_restart(srv, inbox):
    put(inbox, 1)
    assert tick(srv, at(WED))["action"] == "ran"
    srv.extra_env["PLAN_SCHEDULER_TEST_NOW"] = "2026-11-11T04:10:00Z"    # restart at 09:10 the same day
    srv.restart()
    assert tick(srv, "2026-11-11T04:15:00Z")["action"] == "already_done"
    sched = [r for r in runs(srv, "schedule") if r["scheduled_date"] == WED]
    assert len(sched) == 1 and sched[0]["outcome"] != "missed"
    assert len(imports(srv)) == 1


def test_interrupted_scheduled_run_is_not_repeated(srv, inbox):
    put(inbox, 1)
    srv.stop()
    data = json.loads(srv.data_file.read_text())
    data["plan_source_runs"].append({"id": "x", "trigger": "schedule", "scheduled_date": WED, "outcome": "running",
                                     "phases": {"check": "ok", "analysis": "not_run", "sync": "not_run"}})
    srv.data_file.write_text(json.dumps(data))
    srv.extra_env["PLAN_SCHEDULER_TEST_NOW"] = "2026-11-11T04:05:00Z"
    srv.start()
    assert tick(srv, "2026-11-11T04:06:00Z")["action"] == "already_done"
    assert runs(srv, "schedule")[0]["outcome"] == "interrupted"


def test_missed_run_is_not_replayed(tmp_path, inbox):
    put(inbox, 1)
    s = make_server(tmp_path, inbox, start_now="2026-11-11T06:30:00Z")   # server starts at 11:30 local
    try:
        rec = runs(s, "schedule")
        assert rec and rec[0]["outcome"] == "missed" and rec[0]["scheduled_date"] == WED
        assert tick(s, "2026-11-11T06:31:00Z")["action"] == "already_done"
        assert imports(s) == []                                           # nothing processed for the missed run
        # a timer that fires far too late (> grace period) is not replayed either
        assert tick(s, "2026-11-12T05:00:00Z")["action"] == "missed"
        assert imports(s) == []
    finally:
        s.stop()


# ------------------------------------------------------------------ 8-12: manual «Обновить»
@pytest.mark.parametrize("day", [SAT, HOLIDAY])
def test_manual_update_ignores_the_calendar(srv, inbox, day):
    clock(srv, at(day, "13:00"))
    put(inbox, 1)
    r = update(srv)
    assert r["run"]["trigger"] == "manual" and r["run"]["phases"]["check"] == "ok"
    assert r["run"]["phases"]["analysis"] == "awaiting_approval"
    assert r["ui"]["state"] == "review_required"
    assert runs(srv, "schedule") == []                                    # no scheduled run involved


def test_unchanged_file(srv, inbox):
    first_sync(srv, inbox)
    n_imports = len(imports(srv))
    r = update(srv)
    assert r["ui"]["state"] == "no_changes" and r["ui"]["message"].startswith("Изменений нет")
    assert r["run"]["phases"] == {"check": "ok", "analysis": "not_run", "sync": "not_run"}
    assert len(imports(srv)) == n_imports
    # the scheduled run records a successful check without another import
    assert tick(srv, at(WED))["outcome"] == "unchanged"
    assert len(imports(srv)) == n_imports
    assert status(srv)["last_check_at"] and status(srv)["last_sync_at"]


def test_changed_file_with_safe_updates(srv, inbox):
    ps = first_sync(srv, inbox)
    honda = next(p for p in ps if p["project_name"].startswith("Honda"))
    put(inbox, 5)                                                         # only planned hours change
    r = tick(srv, at(WED))
    assert r["outcome"] == "synced"
    rec = runs(srv, "schedule")[0]
    assert rec["phases"] == {"check": "ok", "analysis": "ok", "sync": "ok"}
    h2 = requests.get(f"{srv.url}/api/projects/{honda['id']}").json()
    assert h2["workload"]["planned_hours"] == honda["workload"]["planned_hours"] + 4
    assert len(projects(srv)) == len(ps)
    put(inbox, 1)                                                         # back to 8 h via the button
    u = update(srv)
    assert u["ui"]["state"] == "updated" and u["ui"]["message"].startswith("Обновлено")


def test_changed_file_requiring_review(srv, inbox):
    ps = first_sync(srv, inbox)
    put(inbox, 4)                                                         # hours + a new project + stage suggestion
    r = update(srv)
    assert r["ui"]["state"] == "review_required"
    after = projects(srv)
    assert not any(p["project_name"] == "Chery Tiggo 4 Защита картера" for p in after)   # not created
    gac = next(p for p in after if p["project_name"] == "GAC S9 Комплект защит")
    assert gac["workload"]["actual_hours"] == 5                           # the permitted part was applied
    held = [i for i in requests.get(srv.url + "/api/review-queue?kind=plan_auto_sync_held").json() if not i["resolved"]]
    assert {i["payload"]["project_name"] for i in held} >= {"Chery Tiggo 4 Защита картера", "GAC S9 Комплект защит"}
    assert len(after) == len(ps)


# ------------------------------------------------------------------ 13-14: concurrency, access errors
def test_simultaneous_manual_and_scheduled_runs(srv, inbox):
    put(inbox, 1)
    out = {}

    def manual():
        out["manual"] = update(srv)
    t = threading.Thread(target=manual)
    t.start()
    time.sleep(0.3)
    out["schedule"] = tick(srv, at(WED))
    t.join()
    outcomes = sorted([out["manual"]["run"]["outcome"], out["schedule"].get("outcome")])
    assert outcomes.count("busy") == 1, outcomes                          # exactly one of them ran
    assert len(imports(srv)) == 1
    busy_run = next(r for r in runs(srv) if r["outcome"] == "busy")
    assert busy_run["phases"]["check"] == "not_run"


def test_file_source_access_errors_keep_data(tmp_path, inbox, srv):
    ps = first_sync(srv, inbox)
    before = json.loads(srv.data_file.read_text())["projects"]
    (inbox / PLAN).unlink()
    r = update(srv)
    assert r["ui"]["state"] == "error" and r["run"]["error_code"] == "not_found"
    assert r["run"]["phases"]["check"] == "failed"
    assert json.loads(srv.data_file.read_text())["projects"] == before and len(projects(srv)) == len(ps)
    # a Windows network path on a non-Windows host is reported, not silently misread
    s2 = make_server(tmp_path / "unc", inbox,
                     PLAN_SOURCE_PATH="\\\\s017\\Отделы\\Конструкторский\\1. Планы и отчеты\\AfM СIS+OEM 2026")
    try:
        r2 = update(s2)
        assert r2["ui"]["state"] == "error" and r2["run"]["error_code"] == "unavailable"
        assert "Windows" in r2["ui"]["message"]
    finally:
        s2.stop()


def test_extensionless_source_path_resolves_to_the_workbook(tmp_path, inbox):
    put(inbox, 1)
    (inbox / PLAN).rename(inbox / "AfM СIS+OEM 2026.xlsx")
    s = make_server(tmp_path, inbox, PLAN_SOURCE_PATH=str(inbox / "AfM СIS+OEM 2026"))
    try:
        r = update(s)
        assert r["run"]["phases"]["check"] == "ok"
        assert status(s)["last_file"]["name"] == "AfM СIS+OEM 2026.xlsx"
    finally:
        s.stop()


# ------------------------------------------------------------------ 16: manual data and confirmed stages
def test_manual_edits_and_confirmed_stages_survive_updates(srv, inbox):
    ps = first_sync(srv, inbox)
    honda = next(p for p in ps if p["project_name"].startswith("Honda"))
    requests.post(f"{srv.url}/api/projects/{honda['id']}/stage", json={"stage": "development", "version": honda["version"]})
    h = requests.get(f"{srv.url}/api/projects/{honda['id']}").json()
    requests.patch(f"{srv.url}/api/projects/{honda['id']}",
                   json={"priority": "critical", "description": "ручное описание", "version": h["version"]})
    h = requests.get(f"{srv.url}/api/projects/{honda['id']}").json()
    put(inbox, 5)
    # the planned hours are applied; the manual priority differs from the file -> kept and sent to review
    assert tick(srv, at(WED))["outcome"] == "synced_review_required"
    held = requests.get(srv.url + "/api/review-queue?kind=plan_auto_sync_held").json()
    assert any(i["payload"]["project_name"].startswith("Honda") and i["payload"]["conflicts"] for i in held)
    update(srv)
    h2 = requests.get(f"{srv.url}/api/projects/{honda['id']}").json()
    assert h2["status"] == "development" and h2["stage_entered_at"] == h["stage_entered_at"]
    assert h2["priority"] == "critical" and h2["description"] == "ручное описание"
    assert h2["id"] == honda["id"] and h2["external_project_id"] == honda["external_project_id"]
    unconfirmed = [p for p in projects(srv) if p["id"] != honda["id"]]
    assert all(p["status"] is None for p in unconfirmed)                  # no stage confirmed by an update


def test_configuration_and_status_report(srv):
    st = status(srv)
    sc = st["schedule"]
    assert sc["schedule_time"] == "09:00" and sc["timezone"] == "Asia/Yekaterinburg" and sc["work_calendar"] == "RU"
    assert sc["schedule_active"] and st["require_approval"] is True and st["config_warnings"] == []
    env = (ROOT / ".env.example").read_text()
    for line in ("PLAN_AUTO_MONITOR_ENABLED=true", "PLAN_SCHEDULE_ENABLED=true", "PLAN_SCHEDULE_TIME=09:00",
                 "PLAN_TIMEZONE=Asia/Yekaterinburg", "PLAN_WORK_CALENDAR=RU", "PLAN_AUTO_SYNC_ENABLED=true",
                 "PLAN_REQUIRE_APPROVAL=true", "PLAN_TARGET_MONTH=auto"):
        assert line in env
    assert "PLAN_CHECK_INTERVAL_MINUTES" not in env


def test_require_approval_cannot_be_switched_off(tmp_path, inbox):
    s = make_server(tmp_path, inbox, PLAN_REQUIRE_APPROVAL="false")
    try:
        st = status(s)
        assert st["require_approval"] is True and "PLAN_REQUIRE_APPROVAL=false" in st["config_warnings"][0]
    finally:
        s.stop()


# ------------------------------------------------------------------ 15: browser
def test_board_button_in_browser(srv, inbox):
    first_sync(srv, inbox)
    put(inbox, 5)
    out = subprocess.run(["node", "tests/js/board_update_browser.js", srv.url], cwd=ROOT, capture_output=True,
                         text=True, timeout=180)
    if out.returncode == 3:
        pytest.skip("Playwright/Chromium not available: " + out.stdout + out.stderr)
    assert out.returncode == 0, out.stdout + out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["initial"] == "Обновить" and r["during"] == "Обновление..." and r["disabled_during"]
    assert r["final"] == "Обновлено" and r["projects_reloaded_after_click"]
    assert "Последняя проверка" in r["status_text"] and "Следующее обновление" in r["status_text"]
    assert r["second"] == "Изменений нет"
    assert r["toolbar_has_button"] and r["kanban_columns"] == 5
    assert not r["page_errors"]
