"""Deployment verification against a fake Штурман API and the reference backend."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plan_agent.deployment import load_status, verify

KEY = "k-123"


def fake_shturman(projects):
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
                self._j(200, [{"key": "scanning"}, {"key": "prototype"}])
            elif self.path == "/api/projects":
                self._j(200, projects)
            else:
                self._j(404, {"error": "Not found"})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                return self._j(401, {"error": "bad key"})
            self._j(400, {"error": "Validation failed"})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def shturman():
    data = [{"id": "a", "created_at": "t1", "source": "seed"}, {"id": "b", "created_at": "t2", "source": "agent"}]
    srv = fake_shturman(data)
    yield f"http://127.0.0.1:{srv.server_address[1]}", data
    srv.shutdown()


def by(res):
    return {c["check"]: c for c in res["checks"]}


def test_not_configured(tmp_path):
    res = verify(None, None, tmp_path)
    assert not res["ready"] and "PM_API_URL is not set" in res["blocking"][0]


def test_shturman_checks_without_writes(tmp_path, shturman):
    url, _ = shturman
    res = verify(url, KEY, tmp_path)
    c = by(res)
    assert res["flavor"] == "shturman"
    assert c["dashboard page"]["status"] == "pass" and c["backend API"]["status"] == "pass"
    assert c["agent authentication"]["status"] == "pass"
    assert "1 are seeded SAMPLE projects" in c["database contents"]["detail"]
    assert c["persistent database (survives restart)"]["status"] == "pending"
    assert c["production host"]["status"] == "fail"                    # localhost
    assert c["agent payload compatibility"]["status"] == "fail"
    assert not res["ready"]
    assert verify(url, "wrong", tmp_path)["checks"][2]["status"] == "fail"


def test_restart_persistence(tmp_path, shturman):
    url, data = shturman
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
    res = verify(backend, "secret-test-key", agent.settings.state_dir)
    assert res["flavor"] == "reference" and not res["ready"]          # no dashboard UI, local host
    assert load_status(agent.settings.state_dir, backend)["ready"] is False
