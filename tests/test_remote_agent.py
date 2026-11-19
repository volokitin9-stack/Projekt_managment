"""Railway + Windows-laptop split: the job queue between the dashboard's «Обновить» button /
09:00 scheduler and the Windows agent (server/agentJobs.js, server/routes/agentJobs.js,
server/planSource/monitor.js's applyRemoteAnalyze/applyRemoteSync), plus the Windows-agent auth
guard (server/auth.js) and the optional dashboard session auth (server/dashboardAuth.js).

No real Excel file or Python plan_agent process is involved here: PLAN_SOURCE_TYPE=remote_agent
takes analyze/sync reports straight from the request body, exactly as the real Windows agent would
submit them after running the unmodified plan_agent CLI locally - so these tests exercise the same
server-side code a real laptop drives, without needing one.
"""
import time

import pytest
import requests

from conftest import ShturmanServer

WINKEY = "winagent-secret-test-key"


def _server(tmp_path, auto_sync=False, **env):
    # PLAN_AUTO_MONITOR_ENABLED=false: these tests drive the job queue directly: the 09:00 scheduler
    # itself (including its own startup recalculation, which shares the same import/check lock) is
    # covered by tests/test_schedule.py and is left disabled here so it cannot race a test's own
    # enqueue/claim calls right after boot.
    e = {"PLAN_SOURCE_TYPE": "remote_agent", "PLAN_TARGET_MONTH": "2026-11",
         "WINDOWS_AGENT_API_KEY": WINKEY, "PLAN_AUTO_SYNC_ENABLED": "true" if auto_sync else "false",
         "PLAN_AUTO_MONITOR_ENABLED": "false", "PLAN_SCHEDULER_TEST_HOOKS": "1"}
    e.update(env)
    return ShturmanServer(tmp_path / "srv", extra_env=e).start()


@pytest.fixture
def srv(tmp_path):
    s = _server(tmp_path)
    yield s
    s.stop()


def auth_headers(nonce_suffix=""):
    return {"Authorization": f"Bearer {WINKEY}", "X-Agent-Timestamp": str(int(time.time() * 1000)),
            "X-Agent-Nonce": f"nonce-{time.time_ns()}{nonce_suffix}"}


def update(s):
    return requests.post(s.url + "/api/plan-source/update", json={}, timeout=30).json()


def status(s):
    return requests.get(s.url + "/api/plan-source/status", timeout=10).json()


def next_job(s, nonce_suffix=""):
    r = requests.get(s.url + "/api/agent-jobs/agent/next", headers=auth_headers(nonce_suffix), timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["job"]


def submit_result(s, job_id, body, nonce_suffix=""):
    return requests.post(s.url + f"/api/agent-jobs/agent/{job_id}/result", json=body,
                          headers=auth_headers(nonce_suffix), timeout=10)


ANALYZE_READY = {"type": "analyze", "sha256": "sha-a1", "size": 100, "mtime": "2026-11-18T00:00:00Z",
                  "name": "plan.xlsx", "month": "2026-11",
                  "analyzeJson": {"status": "Dry run", "mode": "dry_run", "counts": {"updated": 2},
                                  "run_id": "r1", "fingerprint": "fp1"}}

SYNC_OK = {"type": "sync", "syncJson": {"status": "Completed", "run_id": "r2", "counts": {"updated": 2},
                                        "held_for_review": 0}}


# --------------------------------------------------------------------------- Python status

def test_status_reports_python_not_applicable_in_remote_agent_mode(srv):
    """In this mode the Excel file is read and plan_agent runs entirely on the Windows laptop
    (this test file's own module docstring) - this server never spawns Python, so whether one
    happens to be resolvable on the Railway container is meaningless. Before the fix,
    /api/plan-source/status always reported server/pythonResolver.js's result regardless of mode,
    which on a real Railway container (no Python installed) rendered as a red "Python not found"
    error for a deployment that was working exactly as intended."""
    st = status(srv)
    assert st["python"]["not_applicable"] is True
    assert st["python"]["ok"] is True
    assert st["python"]["error"] is None
    assert st["python"]["executable"] is None


# --------------------------------------------------------------------------- auth guard

def test_agent_jobs_require_auth(srv):
    r = requests.get(srv.url + "/api/agent-jobs/agent/next", timeout=10)
    assert r.status_code == 401, r.text

    r = requests.get(srv.url + "/api/agent-jobs/agent/next", headers={"Authorization": "Bearer wrong-key"}, timeout=10)
    assert r.status_code == 401, r.text

    # right key, but no replay headers
    r = requests.get(srv.url + "/api/agent-jobs/agent/next", headers={"Authorization": f"Bearer {WINKEY}"}, timeout=10)
    assert r.status_code == 401, r.text
    assert "Timestamp" in r.json()["error"]

    # stale timestamp (outside the default 5-minute window)
    h = auth_headers()
    h["X-Agent-Timestamp"] = str(int(time.time() * 1000) - 10 * 60 * 1000)
    r = requests.get(srv.url + "/api/agent-jobs/agent/next", headers=h, timeout=10)
    assert r.status_code == 401, r.text

    # nonce too short
    h = auth_headers()
    h["X-Agent-Nonce"] = "short"
    r = requests.get(srv.url + "/api/agent-jobs/agent/next", headers=h, timeout=10)
    assert r.status_code == 401, r.text

    # the project-data AGENT_API_KEY must NOT work here - it is a separate credential
    r = requests.get(srv.url + "/api/agent-jobs/agent/next", headers={"Authorization": f"Bearer {srv.key}"}, timeout=10)
    assert r.status_code == 401, r.text


def test_nonce_replay_rejected(srv):
    h = auth_headers("-replay")
    r1 = requests.get(srv.url + "/api/agent-jobs/agent/next", headers=h, timeout=10)
    assert r1.status_code == 200, r1.text
    r2 = requests.get(srv.url + "/api/agent-jobs/agent/next", headers=h, timeout=10)  # same nonce again
    assert r2.status_code == 401, r2.text
    assert "replayed" in r2.json()["error"]


def test_rate_limit_enforced(tmp_path):
    s = _server(tmp_path, AGENT_RATE_LIMIT_MAX="3", AGENT_RATE_LIMIT_WINDOW_MS="60000")
    try:
        codes = []
        for i in range(5):
            r = requests.get(s.url + "/api/agent-jobs/agent/next", headers=auth_headers(f"-rl{i}"), timeout=10)
            codes.append(r.status_code)
        assert codes[:3] == [200, 200, 200], codes
        assert 429 in codes[3:], codes
    finally:
        s.stop()


def test_windows_agent_key_unconfigured_returns_501(tmp_path):
    s = ShturmanServer(tmp_path / "srv", extra_env={"PLAN_SOURCE_TYPE": "remote_agent",
                                                      "PLAN_TARGET_MONTH": "2026-11"}).start()
    try:
        r = requests.get(s.url + "/api/agent-jobs/agent/next", headers=auth_headers(), timeout=10)
        assert r.status_code == 501, r.text
    finally:
        s.stop()


# --------------------------------------------------------------------------- manual approval flow

def test_job_carries_the_resolved_month_for_the_agent(srv):
    # The agent has no other authenticated way to learn which month PLAN_TARGET_MONTH=auto resolved
    # to server-side - it must read it straight off the job it claims, not recompute its own guess.
    update(srv)
    job = next_job(srv, "-jm1")
    assert job["month"] == "2026-11"


def test_manual_flow_awaiting_approval_then_approve(srv):
    r = update(srv)
    assert r["ui"]["state"] == "busy" and r["run"]["job_id"], r
    job = next_job(srv, "-m1")
    assert job["type"] == "check"

    resp = submit_result(srv, job["id"], ANALYZE_READY, "-m2")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["status"] == "awaiting_approval"

    st = status(srv)
    assert st["status"] == "awaiting_approval"
    assert st["remote_agent"]["enabled"] is True

    a = requests.post(srv.url + "/api/plan-source/approve", json={"confirm": True, "approved_by": "pytest"}, timeout=10)
    assert a.status_code == 200, a.text
    assert a.json()["result"]["status"] == "queued"

    sync_job = next_job(srv, "-m3")
    assert sync_job["type"] == "sync"
    assert sync_job["version_id"]

    resp2 = submit_result(srv, sync_job["id"], SYNC_OK, "-m4")
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["result"]["status"] == "synced"
    assert status(srv)["status"] == "up_to_date"


# --------------------------------------------------------------------------- auto-sync chained flow

def test_auto_sync_chains_a_second_job(tmp_path):
    s = _server(tmp_path, auto_sync=True)
    try:
        update(s)
        check_job = next_job(s, "-a1")
        resp = submit_result(s, check_job["id"], ANALYZE_READY, "-a2")
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"]["status"] == "awaiting_agent_sync"

        # the check job must be terminal now (completed), not still "running" -
        # otherwise a second /result submission on it would be the old, broken design.
        sync_job = next_job(s, "-a3")
        assert sync_job["id"] != check_job["id"]
        assert sync_job["type"] == "sync"
        assert sync_job["version_id"]
        assert sync_job["month"] == check_job["month"] == "2026-11"

        resp2 = submit_result(s, sync_job["id"], SYNC_OK, "-a4")
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["result"]["status"] == "synced"
        assert status(s)["status"] == "up_to_date"

        # the lock held across the whole chain must be released: a fresh click works, not "busy" forever
        r2 = update(s)
        assert r2["run"]["outcome"] in ("queued", "unchanged"), r2
    finally:
        s.stop()


# --------------------------------------------------------------------------- idempotency / concurrency

def test_second_click_while_job_active_is_busy_not_a_duplicate(srv):
    r1 = update(srv)
    job_id_1 = r1["run"]["job_id"]
    assert job_id_1

    r2 = update(srv)
    assert r2["run"]["outcome"] == "busy", r2
    # claiming must still yield exactly the one job that was queued
    job = next_job(srv, "-dup1")
    assert job["id"] == job_id_1
    r3 = requests.get(srv.url + "/api/agent-jobs/agent/next", headers=auth_headers("-dup2"), timeout=10)
    assert r3.json()["job"] is None  # no second job was ever created


def test_unchanged_file_acknowledged_without_reimport(srv):
    update(srv)
    job = next_job(srv, "-u1")
    resp = submit_result(srv, job["id"], ANALYZE_READY, "-u2")
    assert resp.json()["result"]["status"] == "awaiting_approval"
    requests.post(srv.url + "/api/plan-source/approve", json={"confirm": True, "approved_by": "pytest"}, timeout=10)
    sync_job = next_job(srv, "-u3")
    submit_result(srv, sync_job["id"], SYNC_OK, "-u4")
    assert status(srv)["status"] == "up_to_date"

    # the laptop reports the *same* workbook (same sha256/month) again later, unchanged
    update(srv)
    job2 = next_job(srv, "-u5")
    resp2 = submit_result(srv, job2["id"], ANALYZE_READY, "-u6")
    assert resp2.json()["result"]["status"] == "unchanged"
    assert status(srv)["status"] == "up_to_date"


# --------------------------------------------------------------------------- lease expiry / recovery

def test_expired_lease_recovers_and_can_be_retried(tmp_path):
    s = _server(tmp_path, AGENT_JOB_LEASE_MS="200")
    try:
        update(s)
        job = next_job(s, "-l1")
        time.sleep(0.5)  # outlive the 200ms lease without heartbeating or submitting a result

        st = status(s)  # status() reconciles a stuck remote lock from a timed-out job
        assert st["status"] == "error"
        assert st["remote_agent"]["active_job"] is None or st["remote_agent"]["active_job"]["status"] != "running"

        r2 = update(s)  # must not be stuck "busy" forever - a fresh job can be queued
        assert r2["run"]["outcome"] == "queued", r2
        job2 = next_job(s, "-l2")
        assert job2["id"] != job["id"]
    finally:
        s.stop()


def test_heartbeat_extends_lease(tmp_path):
    s = _server(tmp_path, AGENT_JOB_LEASE_MS="400")
    try:
        update(s)
        job = next_job(s, "-h1")
        time.sleep(0.25)
        r = requests.post(s.url + f"/api/agent-jobs/agent/{job['id']}/heartbeat", headers=auth_headers("-h2"), timeout=10)
        assert r.status_code == 200, r.text
        time.sleep(0.25)  # total 0.5s elapsed, but the heartbeat reset the 0.4s lease at t=0.25s
        st = status(s)
        assert st["remote_agent"]["active_job"]["status"] == "running"
    finally:
        s.stop()


def test_agent_reported_failure(srv):
    update(srv)
    job = next_job(srv, "-f1")
    r = requests.post(srv.url + f"/api/agent-jobs/agent/{job['id']}/fail", json={"error": "workbook locked"},
                       headers=auth_headers("-f2"), timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["job"]["status"] == "failed"


def test_blocked_outcome_raises_review_item(srv):
    update(srv)
    job = next_job(srv, "-b1")
    body = dict(ANALYZE_READY, sha256="sha-blocked")
    body["analyzeJson"] = {"status": "Verification required: new product code", "mode": "dry_run"}
    resp = submit_result(srv, job["id"], body, "-b2")
    assert resp.json()["result"]["status"] == "blocked"
    items = requests.get(srv.url + "/api/review-queue?kind=plan_source_issue", timeout=10).json()
    assert any(not i["resolved"] for i in items)


# --------------------------------------------------------------------------- dashboard session auth

def test_dashboard_auth_is_noop_when_unconfigured(srv):
    r = requests.get(srv.url + "/", timeout=10)
    assert r.status_code == 200
    r = requests.get(srv.url + "/api/whoami", timeout=10)
    assert r.json() == {"auth_required": False}


def test_dashboard_auth_protects_when_configured(tmp_path):
    s = ShturmanServer(tmp_path / "srv", extra_env={"DASHBOARD_USERNAME": "admin",
                                                      "DASHBOARD_PASSWORD": "s3cret!",
                                                      "DASHBOARD_SESSION_SECRET": "test-session-secret"}).start()
    try:
        r = requests.get(s.url + "/", timeout=10, allow_redirects=False)
        assert r.status_code == 302 and "/login.html" in r.headers["location"]

        r = requests.get(s.url + "/api/projects", timeout=10)
        assert r.status_code == 401

        bad = requests.post(s.url + "/api/login", json={"username": "admin", "password": "wrong"}, timeout=10)
        assert bad.status_code == 401

        sess = requests.Session()
        ok = sess.post(s.url + "/api/login", json={"username": "admin", "password": "s3cret!"}, timeout=10)
        assert ok.status_code == 200, ok.text
        r2 = sess.get(s.url + "/api/projects", timeout=10)
        assert r2.status_code == 200

        who = sess.get(s.url + "/api/whoami", timeout=10).json()
        assert who == {"auth_required": True, "authenticated": True, "username": "admin"}

        sess.post(s.url + "/api/logout", timeout=10)
        r3 = sess.get(s.url + "/api/projects", timeout=10)
        assert r3.status_code == 401
    finally:
        s.stop()


def test_dashboard_auth_does_not_gate_agent_or_health_routes(tmp_path):
    s = ShturmanServer(tmp_path / "srv", extra_env={"DASHBOARD_USERNAME": "admin", "DASHBOARD_PASSWORD": "s3cret!",
                                                      "DASHBOARD_SESSION_SECRET": "x",
                                                      "PLAN_SOURCE_TYPE": "remote_agent", "PLAN_TARGET_MONTH": "2026-11",
                                                      "WINDOWS_AGENT_API_KEY": WINKEY}).start()
    try:
        assert requests.get(s.url + "/api/health", timeout=10).status_code == 200
        r = requests.get(s.url + "/api/agent-jobs/agent/next", headers=auth_headers(), timeout=10)
        assert r.status_code == 200  # agent routes use their own auth, not the dashboard session
    finally:
        s.stop()
