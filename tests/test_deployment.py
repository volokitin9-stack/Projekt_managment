"""Deployment verification against a fake Штурман API and the reference backend."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plan_agent.deployment import load_status, verify

KEY = "k-123"
# The real /api/agent/schema's "supports" list (server/routes/agent.js) - a build with the full
# planning integration advertises all of these.
FULL_SUPPORTS = ["expected_version", "duplicate_detection", "field_sources", "history", "sync_runs", "activities",
                 "review_queue", "stage_transitions", "telegram_photos", "create_only", "workflow", "review_severity"]


def fake_shturman(projects, supports=None, gate_dashboard_endpoints=False):
    """A fake Штурман backend.

    gate_dashboard_endpoints simulates a Railway deployment with DASHBOARD_USERNAME/PASSWORD set:
    /api/stages and /api/projects (dashboard-session-gated, server/dashboardAuth.js) always 401
    without a session, which this script never has - only /api/agent/* (Authorization: Bearer,
    never covered by that session check) works. The checks under test must still pass in this mode.
    """
    def agent_ok(handler):
        return handler.headers.get("Authorization") == f"Bearer {KEY}"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _j(self, code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/":
                raw = b"<html><title>\xd0\xa8\xd1\x82\xd1\x83\xd1\x80\xd0\xbc\xd0\xb0\xd0\xbd</title></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/api/health":
                self._j(200, {"ok": True})
            elif self.path == "/api/stages":
                if gate_dashboard_endpoints:
                    self._j(401, {"error": "Not authenticated. POST /api/login first."})
                else:
                    self._j(200, [{"key": "scanning"}, {"key": "prototype"}])
            elif self.path == "/api/projects":
                if gate_dashboard_endpoints:
                    self._j(401, {"error": "Not authenticated. POST /api/login first."})
                else:
                    self._j(200, projects)
            elif self.path == "/api/agent/schema":
                if not agent_ok(self):
                    return self._j(401, {"error": "Missing or invalid Authorization: Bearer <AGENT_API_KEY> header"})
                self._j(200, {"version": 2, "app": "shturman", "schema_version": 3,
                               "stages": ["scanning", "prototype"], "supports": supports or []})
            elif self.path == "/api/agent/projects":
                if not agent_ok(self):
                    return self._j(401, {"error": "Missing or invalid Authorization: Bearer <AGENT_API_KEY> header"})
                self._j(200, {"projects": projects})
            else:
                self._j(404, {"error": "Not found"})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/api/agent/projects":
                if not agent_ok(self):
                    return self._j(401, {"error": "bad key"})
                return self._j(400, {"error": "Validation failed"})
            self._j(404, {"error": "Not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def fake_api():
    data = [{"id": "a", "created_at": "t1", "source": "seed"}, {"id": "b", "created_at": "t2", "source": "agent"}]
    srv = fake_shturman(data)
    yield f"http://127.0.0.1:{srv.server_address[1]}", data
    srv.shutdown()


def by(res):
    return {c["check"]: c for c in res["checks"]}


def test_not_configured(tmp_path):
    res = verify(None, None, tmp_path)
    assert not res["ready"] and "PM_API_URL is not set" in res["blocking"][0]


def test_shturman_checks_without_writes(tmp_path, fake_api):
    url, _ = fake_api
    res = verify(url, KEY, tmp_path)
    c = by(res)
    assert res["flavor"] == "shturman"
    assert c["dashboard page"]["status"] == "pass" and c["backend API"]["status"] == "pass"
    assert c["agent authentication"]["status"] == "pass"
    assert "1 are seeded SAMPLE projects" in c["database contents"]["detail"]
    assert c["persistent database (survives restart)"]["status"] == "pending"
    assert c["production host"]["status"] == "fail"                    # localhost
    assert c["agent payload compatibility"]["status"] == "fail"           # Штурман without the integration
    assert not res["ready"]
    assert verify(url, "wrong", tmp_path)["checks"][2]["status"] == "fail"


def test_restart_persistence(tmp_path, fake_api):
    url, data = fake_api
    verify(url, KEY, tmp_path)
    ok = by(verify(url, KEY, tmp_path, after_restart=True))
    assert ok["persistent database (survives restart)"]["status"] == "pass"
    data.pop(0)                                                       # a project vanished: not persistent
    bad = by(verify(url, KEY, tmp_path, after_restart=True))
    assert bad["persistent database (survives restart)"]["status"] in ("fail", "pending")


def test_sync_blocked_until_deployment_verified(backend, make_agent, fixtures_dir, tmp_path):
    agent = make_agent(backend)
    agent.settings.require_verified_deployment = True
    agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    agent.store.approve("pytest")
    out = agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    assert out.report["mode"] == "dry_run" and out.status == "Dry run - deployment not verified"
    assert any("SYNC BLOCKED" in n for n in out.report["notes"])
    # the deployment-verification failure only downgrades sync to dry_run - it must not suppress
    # the analysis itself: the report/rendered text still reflect the real, completed comparison.
    assert out.text and out.report.get("projects") and "counts" in out.report
    res = verify(backend, "secret-test-key", agent.settings.state_dir)
    c = by(res)
    assert res["flavor"] == "shturman"
    assert c["dashboard page"]["status"] == "pass" and c["agent authentication"]["status"] == "pass"
    assert c["agent payload compatibility"]["status"] == "pass"           # integrated Штурман API
    assert c["production host"]["status"] == "fail" and not res["ready"]  # local test instance
    assert load_status(agent.settings.state_dir, backend)["ready"] is False


def test_railway_dashboard_session_auth_does_not_block_detection(tmp_path):
    """The actual bug report this guards against: a Railway deployment with DASHBOARD_USERNAME/
    PASSWORD set gates /api/stages and /api/projects behind a human operator's browser session
    (server/dashboardAuth.js) that this unattended script never has and never acquires. Flavour
    detection, agent authentication and the project/persistence checks must all still work, using
    only the agent-protected /api/agent/* endpoints - none of them may depend on those two
    session-gated endpoints to pass."""
    data = [{"id": "a", "created_at": "t1", "source": "agent"}]
    srv = fake_shturman(data, supports=FULL_SUPPORTS, gate_dashboard_endpoints=True)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        res = verify(url, KEY, tmp_path)
        c = by(res)
        assert res["flavor"] == "shturman", "flavour detection must not depend on the session-gated /api/stages"
        assert c["agent authentication"]["status"] == "pass"
        assert c["database contents"]["status"] == "pass"
        assert c["persistent database (survives restart)"]["status"] == "pending"
        assert c["agent payload compatibility"]["status"] == "pass"
        # a wrong key must still be reported correctly even though neither session-gated endpoint helps
        bad = by(verify(url, "wrong", tmp_path))
        assert bad["agent authentication"]["status"] == "fail"
        assert "key rejected" in bad["agent authentication"]["detail"]
    finally:
        srv.shutdown()
