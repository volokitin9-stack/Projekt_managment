"""POST /api/telegram/webhook/connect - the "Подключить webhook" button on "Интеграции →
Telegram" (server/routes/telegramAdmin.js). Registers TELEGRAM_WEBHOOK_URL with Telegram's
setWebhook, then verifies with getWebhookInfo. Synthetic bot token/secret/dashboard credentials
only - no real Telegram calls.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from conftest import ShturmanServer

BOTKEY = "tg-connect-test-key"
WEBHOOK_SECRET = "wh-connect-secret"
WEBHOOK_URL = "https://projektmanagment-production.up.railway.app/api/telegram/webhook"
DASH_USER, DASH_PASS = "admin", "s3cret!"


def fake_telegram(set_webhook_fails=None, info=None):
    """set_webhook_fails: None for success, or a Telegram-style error description string.
    info: the result dict getWebhookInfo returns (defaults to a healthy, connected-looking one)."""
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            calls.append({"path": self.path, "body": body})
            if self.path.endswith("/setWebhook"):
                if set_webhook_fails:
                    return self._send(400, {"ok": False, "error_code": 400, "description": set_webhook_fails})
                return self._send(200, {"ok": True, "result": True})
            if self.path.endswith("/getWebhookInfo"):
                result = info if info is not None else {
                    "url": WEBHOOK_URL, "has_custom_certificate": False, "pending_update_count": 0}
                return self._send(200, {"ok": True, "result": result})
            self._send(200, {"ok": True, "result": {}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.calls = calls
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def telegram():
    srv = fake_telegram()
    yield srv
    srv.shutdown()


def _server(tmp_path, telegram_srv, **env):
    e = {
        "TELEGRAM_BOT_TOKEN": BOTKEY, "TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{telegram_srv.server_address[1]}",
        "DASHBOARD_USERNAME": DASH_USER, "DASHBOARD_PASSWORD": DASH_PASS,
        "DASHBOARD_SESSION_SECRET": "test-session-secret",
    }
    e.update(env)
    return ShturmanServer(tmp_path / "srv", extra_env=e).start()


def login(srv):
    sess = requests.Session()
    r = sess.post(srv.url + "/api/login", json={"username": DASH_USER, "password": DASH_PASS}, timeout=10)
    assert r.status_code == 200, r.text
    csrf = sess.get(srv.url + "/api/whoami", timeout=10).json()["csrf_token"]
    assert csrf
    return sess, csrf


def connect(sess, srv, csrf=True, token=None):
    headers = {}
    if csrf:
        headers["X-CSRF-Token"] = token
    return sess.post(srv.url + "/api/telegram/webhook/connect", headers=headers, timeout=10)


def status(sess, srv):
    return sess.get(srv.url + "/api/telegram/status", timeout=10).json()


def test_successful_registration_sets_the_right_parameters_and_shows_connected_status(tmp_path, telegram):
    srv = _server(tmp_path, telegram)
    try:
        sess, csrf = login(srv)
        r = connect(sess, srv, token=csrf)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connected"] is True and body["url"] == WEBHOOK_URL
        assert body["last_error"] is None

        set_calls = [c for c in telegram.calls if c["path"].endswith("/setWebhook")]
        assert len(set_calls) == 1
        sent = set_calls[0]["body"]
        assert sent["url"] == WEBHOOK_URL
        assert sent["allowed_updates"] == ["message"]
        assert sent["drop_pending_updates"] is False
        assert sent["secret_token"] == WEBHOOK_SECRET

        st = status(sess, srv)["webhook"]
        assert st["connected"] is True and st["last_checked_at"]

        # the bot token and the webhook secret must never appear in any response body
        full = json.dumps(body) + json.dumps(status(sess, srv))
        assert BOTKEY not in full and WEBHOOK_SECRET not in full
    finally:
        srv.stop()


def test_telegram_api_rejection_is_reported_not_crashed(tmp_path):
    srv_telegram = fake_telegram(set_webhook_fails="Bad Request: wrong HTTP URL specified")
    try:
        srv = _server(tmp_path, srv_telegram)
        try:
            sess, csrf = login(srv)
            r = connect(sess, srv, token=csrf)
            assert r.status_code == 502
            body = r.json()
            assert body["connected"] is False
            assert "wrong HTTP URL" in body["last_error"]

            st = status(sess, srv)["webhook"]
            assert st["connected"] is False and "wrong HTTP URL" in st["last_error"]
        finally:
            srv.stop()
    finally:
        srv_telegram.shutdown()


def fake_telegram_with_broken_getwebhookinfo():
    """setWebhook succeeds; getWebhookInfo itself fails (simulates a network blip or a Telegram-side
    error on the verification call, right after a successful registration)."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if self.path.endswith("/setWebhook"):
                raw = json.dumps({"ok": True, "result": True}).encode()
                self.send_response(200)
            else:
                raw = json.dumps({"ok": False, "error_code": 500, "description": "Internal Server Error"}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_telegram_getwebhookinfo_failure_after_a_successful_setwebhook_is_also_reported(tmp_path):
    """setWebhook can succeed while the verification step itself fails - must not be reported as a
    plain success, since "подключено" без подтверждения через getWebhookInfo is exactly what
    критическое требование №6 asks to avoid."""
    srv_telegram = fake_telegram_with_broken_getwebhookinfo()
    try:
        srv = _server(tmp_path, srv_telegram)
        try:
            sess, csrf = login(srv)
            r = connect(sess, srv, token=csrf)
            assert r.status_code == 502
            body = r.json()
            assert body["connected"] is False
            assert "getWebhookInfo" in body["last_error"]
        finally:
            srv.stop()
    finally:
        srv_telegram.shutdown()


def test_missing_bot_token_returns_501_without_calling_telegram(tmp_path, telegram):
    srv = _server(tmp_path, telegram, TELEGRAM_BOT_TOKEN="")
    try:
        sess, csrf = login(srv)
        r = connect(sess, srv, token=csrf)
        assert r.status_code == 501
        assert r.json()["connected"] is False
        assert telegram.calls == []
    finally:
        srv.stop()


def test_missing_webhook_secret_returns_501_without_calling_telegram(tmp_path, telegram):
    srv = _server(tmp_path, telegram, TELEGRAM_WEBHOOK_SECRET="")
    try:
        sess, csrf = login(srv)
        r = connect(sess, srv, token=csrf)
        assert r.status_code == 501
        assert telegram.calls == []
    finally:
        srv.stop()


def test_unauthenticated_request_is_refused(tmp_path, telegram):
    srv = _server(tmp_path, telegram)
    try:
        r = requests.post(srv.url + "/api/telegram/webhook/connect", timeout=10)
        assert r.status_code == 401
        assert telegram.calls == []
    finally:
        srv.stop()


def test_request_without_a_valid_csrf_token_is_refused_even_with_a_valid_session(tmp_path, telegram):
    srv = _server(tmp_path, telegram)
    try:
        sess, csrf = login(srv)
        no_token = connect(sess, srv, csrf=False)
        assert no_token.status_code == 403

        wrong_token = connect(sess, srv, token="not-the-real-token")
        assert wrong_token.status_code == 403

        assert telegram.calls == []

        # a stale token from a *different* session must also be refused
        sess2, csrf2 = login(srv)
        assert csrf2 != csrf
        cross_session = connect(sess, srv, token=csrf2)
        assert cross_session.status_code == 403
        assert telegram.calls == []
    finally:
        srv.stop()


def test_without_dashboard_auth_configured_the_route_is_open_but_still_works(tmp_path, telegram):
    """Matches every other dashboard route's existing behaviour (server/dashboardAuth.js): with no
    DASHBOARD_USERNAME/PASSWORD set, the whole dashboard - including this action - is unauthenticated,
    exactly like the current local/single-machine install."""
    srv = _server(tmp_path, telegram, DASHBOARD_USERNAME="", DASHBOARD_PASSWORD="")
    try:
        r = requests.post(srv.url + "/api/telegram/webhook/connect", timeout=10)
        assert r.status_code == 200
        assert r.json()["connected"] is True
    finally:
        srv.stop()
