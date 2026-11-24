"""Kanban board scroll position must survive the board's auto-refresh.

public/index.html's renderEngBoard() ("Конструкторский отдел" tab) rebuilds every .eng-col-body
node from scratch on each refresh (the 20s poll, or any filter/КPI/drag change) - without saving
and restoring scrollTop around that rebuild, this reset a reader's position to the top of the
column mid-browse. Especially noticeable for a ko_viewer manager, who spends all their time on
this one board. Covered here for both the administrator and a manager, with a real browser
(Playwright) driving the real app - server/routes and server/projectsModel.js are untouched by
this fix, only public/index.html's rendering.
"""
import json
import subprocess

import pytest
import requests

from conftest import ROOT, ShturmanServer

ADMIN_USER, ADMIN_PASS = "admin", "s3cret-admin!"
KEY = "secret-test-key"
H = {"Authorization": f"Bearer {KEY}"}
STAGE = "scanning"
CARD_COUNT = 14


def make_server(tmp_path, **env):
    e = {"DASHBOARD_USERNAME": ADMIN_USER, "DASHBOARD_PASSWORD": ADMIN_PASS,
         "DASHBOARD_SESSION_SECRET": "test-session-secret"}
    e.update(env)
    return ShturmanServer(tmp_path / "srv", extra_env=e)


def admin_session(srv):
    s = requests.Session()
    r = s.post(srv.url + "/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=10)
    assert r.status_code == 200, r.text
    return s


def csrf_of(session, srv):
    return session.get(srv.url + "/api/whoami", timeout=10).json()["csrf_token"]


def seed_one_stage(srv, admin, count=CARD_COUNT, stage=STAGE):
    """Create `count` projects (via the agent API, unconfirmed) and confirm every one of them into
    the same Kanban stage (via the dashboard route, as createProject's "project" envelope may not
    set a confirmed stage directly) - enough cards in one column to overflow and scroll."""
    for i in range(count):
        r = requests.post(srv.url + "/api/agent/projects", headers=H,
                           json={"project": {"manufacturer": "TEST", "vehicle_model": f"M{i}",
                                              "product_type": "Защита картера"}}, timeout=10)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        mv = admin.post(srv.url + f"/api/projects/{pid}/stage", json={"stage": stage, "version": 1}, timeout=10)
        assert mv.status_code == 200, mv.text


@pytest.fixture
def srv(tmp_path):
    s = make_server(tmp_path).start()
    yield s
    s.stop()


def run_browser(srv, cookie, stage=STAGE):
    out = subprocess.run(["node", "tests/js/kanban_scroll_browser.js", srv.url, cookie, stage], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    if out.returncode == 3:
        pytest.skip("Playwright/Chromium not available: " + out.stdout + out.stderr)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_scroll_position_survives_auto_refresh_for_admin(srv):
    admin = admin_session(srv)
    seed_one_stage(srv, admin)
    cookie = "shturman_session=" + admin.cookies.get("shturman_session")
    r = run_browser(srv, cookie)
    assert r["draggable"] == "true", "the administrator must still be able to drag cards"
    assert r["scroll_set"] > 0, "test setup: the column must actually be tall enough to scroll"
    assert all(top == r["scroll_set"] for top in r["after_refresh"]), r["after_refresh"]
    assert r["after_detail_cycle"] == r["scroll_set"]
    assert not r["page_errors"]


def test_scroll_position_survives_auto_refresh_for_ko_viewer(srv):
    admin = admin_session(srv)
    seed_one_stage(srv, admin)
    created = admin.post(srv.url + "/api/users", json={"full_name": "Петров Пётр", "username": "petrov"},
                          headers={"X-CSRF-Token": csrf_of(admin, srv)}, timeout=10).json()
    manager = requests.Session()
    lr = manager.post(srv.url + "/api/login",
                       json={"username": "petrov", "password": created["generated_password"]}, timeout=10)
    assert lr.status_code == 200, lr.text
    cookie = "shturman_session=" + manager.cookies.get("shturman_session")
    r = run_browser(srv, cookie)
    assert r["draggable"] == "false", "a ko_viewer manager must still be unable to drag cards"
    assert r["scroll_set"] > 0, "test setup: the column must actually be tall enough to scroll"
    assert all(top == r["scroll_set"] for top in r["after_refresh"]), r["after_refresh"]
    assert r["after_detail_cycle"] == r["scroll_set"]
    assert not r["page_errors"]
