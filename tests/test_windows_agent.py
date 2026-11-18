"""End-to-end test of plan_agent/windows_agent.py: a real subprocess, polling a real Штурман
server (PLAN_SOURCE_TYPE=remote_agent) over the job-queue API, running the *actual*
`python -m plan_agent analyze/sync` against a synthetic workbook and writing through the server's
existing /api/agent/* API - exactly the production topology, just with a local server standing in
for Railway and a fixture workbook standing in for the real corporate file.
"""
import os
import subprocess
import sys
import time

import pytest
import requests

import make_fixtures
from conftest import ROOT, ShturmanServer

WINKEY = "winagent-e2e-key"


@pytest.fixture
def workbook(tmp_path):
    p = tmp_path / "engineering_plan.xlsx"
    make_fixtures.make_monthly_workbook(p, 1)
    return p


@pytest.fixture
def srv(tmp_path):
    s = ShturmanServer(tmp_path / "srv", extra_env={"PLAN_SOURCE_TYPE": "remote_agent",
                                                      "PLAN_TARGET_MONTH": "2026-11",
                                                      "WINDOWS_AGENT_API_KEY": WINKEY,
                                                      "PLAN_AUTO_MONITOR_ENABLED": "false"}).start()
    yield s
    s.stop()


@pytest.fixture
def agent(tmp_path, srv, workbook):
    env = dict(os.environ)
    env.update({
        "PM_API_URL": srv.url, "PM_AGENT_API_KEY": srv.key, "WINDOWS_AGENT_API_KEY": WINKEY,
        "WINDOWS_AGENT_WORKBOOK_PATH": str(workbook), "WINDOWS_AGENT_POLL_SECONDS": "0.5",
        "WINDOWS_AGENT_SETTLE_SECONDS": "0.2", "WINDOWS_AGENT_RETRY_SECONDS": "0.3",
        "WINDOWS_AGENT_HEARTBEAT_SECONDS": "5", "WINDOWS_AGENT_HTTP_TIMEOUT": "30",
        "PLAN_AGENT_STATE_DIR": str(tmp_path / "agent_state"),
        "WINDOWS_AGENT_LOG_DIR": str(tmp_path / "agent_logs"),
        "PLAN_AGENT_REQUIRE_VERIFIED_DEPLOYMENT": "0",
        "PLAN_PYTHON_EXECUTABLE": "", "PLAN_AGENT_PYTHON": "",
    })
    env.pop("PLAN_SOURCE_TYPE", None)
    proc = subprocess.Popen([sys.executable, "-m", "plan_agent.windows_agent"], cwd=ROOT, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    yield proc
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def status(s):
    return requests.get(s.url + "/api/plan-source/status", timeout=10).json()


def update(s):
    return requests.post(s.url + "/api/plan-source/update", json={}, timeout=10).json()


def wait_for(predicate, timeout=60, interval=0.5, desc="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {desc}; last seen: {last}")


def test_agent_processes_a_real_workbook_end_to_end(srv, agent):
    update(srv)
    wait_for(lambda: status(srv)["status"] == "awaiting_approval", timeout=60, desc="awaiting_approval")

    a = requests.post(srv.url + "/api/plan-source/approve", json={"confirm": True, "approved_by": "e2e"}, timeout=10)
    assert a.status_code == 200, a.text

    wait_for(lambda: status(srv)["status"] == "up_to_date", timeout=60, desc="up_to_date")

    projects = requests.get(srv.url + "/api/projects", timeout=10).json()
    assert len(projects) > 0, "the agent's sync should have written real project data through /api/agent/*"
    assert any(p.get("engineer") == "Петров" for p in projects), projects

    st = status(srv)
    assert st["remote_agent"]["agent"]["online"] is True

    assert agent.poll() is None, "the agent process should still be running (polling), not have crashed"


def test_agent_shuts_down_cleanly_on_sigterm(agent):
    time.sleep(1.5)  # let it get through at least one poll so we know it's actually running
    assert agent.poll() is None
    agent.terminate()
    try:
        code = agent.wait(10)
    except subprocess.TimeoutExpired:
        agent.kill()
        raise AssertionError("agent did not exit within 10s of SIGTERM")
    assert code == 0, agent.stdout.read()
