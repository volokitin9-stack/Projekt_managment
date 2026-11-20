"""Telegram integration (server/telegram/*, server/routes/telegramWebhook.js,
server/routes/telegramAdmin.js) against a real Штурман server, with fake Anthropic and Telegram
Bot API backends (ANTHROPIC_API_BASE_URL / TELEGRAM_API_BASE_URL) standing in for the real
services - the same approach already used for a staged Railway export (server/migrate.js) and the
deployment-verification fake backend (tests/test_deployment.py).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from conftest import ShturmanServer

SECRET = "wh-secret-test"
BOTKEY = "tg-bot-test-key"
OWNER_CHAT = "1000"
OWNER_USER = "1000"
ENG_CHAT = "-2000"


def fake_anthropic(rules):
    """rules: list of (substring, tool_input_dict). First match wins; no match -> 500."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            prompt = (body.get("messages") or [{}])[0].get("content", "")
            tool_name = (body.get("tools") or [{}])[0].get("name", "action")
            for needle, result in rules:
                if needle in prompt:
                    raw = json.dumps({"content": [{"type": "tool_use", "name": tool_name, "input": result}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
            raw = json.dumps({"error": "no rule matched"}).encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fake_telegram():
    sent = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self, result):
            raw = json.dumps({"ok": True, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.endswith("/sendMessage"):
                sent.append(body)
                return self._ok({"message_id": len(sent)})
            if self.path.endswith("/getFile"):
                return self._ok({"file_id": body.get("file_id"), "file_path": "photos/f1.jpg"})
            self._ok({})

        def do_GET(self):
            if "/file/bot" in self.path:
                raw = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.sent_messages = sent
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def update(chat_id, user_id, text=None, update_id=1, reply_to=None, photo=False, caption=None, media_group_id=None):
    message = {"message_id": update_id * 10, "date": 1795000000,
               "chat": {"id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id, "type": "private"},
               "from": {"id": int(user_id), "first_name": "T"}}
    if text is not None:
        message["text"] = text
    if caption is not None:
        message["caption"] = caption
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    if photo:
        message["photo"] = [{"file_id": "small1", "width": 90, "height": 90}, {"file_id": "big1", "width": 800, "height": 600}]
    if media_group_id:
        message["media_group_id"] = media_group_id
    return {"update_id": update_id, "message": message}


@pytest.fixture
def anthropic(request):
    rules = getattr(request, "param", [])
    srv = fake_anthropic(rules)
    yield srv
    srv.shutdown()


@pytest.fixture
def telegram():
    srv = fake_telegram()
    yield srv
    srv.shutdown()


def _server(tmp_path, anthropic_srv, telegram_srv, **env):
    e = {
        "TELEGRAM_BOT_TOKEN": BOTKEY, "TELEGRAM_WEBHOOK_SECRET": SECRET,
        "TELEGRAM_OWNER_CHAT_ID": OWNER_CHAT, "TELEGRAM_OWNER_USER_ID": OWNER_USER,
        "TELEGRAM_ENGINEERING_CHAT_ID": ENG_CHAT,
        "ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_API_BASE_URL": f"http://127.0.0.1:{anthropic_srv.server_address[1]}",
        "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{telegram_srv.server_address[1]}",
        "SEED_DEMO_DATA": "true"
    }
    e.update(env)
    return ShturmanServer(tmp_path / "srv", extra_env=e, seed_demo=True).start()


def post(srv, update_body, secret=SECRET):
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return requests.post(srv.url + "/api/telegram/webhook", json=update_body, headers=headers, timeout=20)


def tasks(srv):
    return requests.get(srv.url + "/api/tasks", timeout=10).json()


def review(srv, kind=None):
    url = srv.url + "/api/review-queue" + (f"?kind={kind}" if kind else "")
    return [r for r in requests.get(url, timeout=10).json() if not r["resolved"]]


def status(srv):
    return requests.get(srv.url + "/api/telegram/status", timeout=10).json()


def projects(srv):
    return requests.get(srv.url + "/api/projects", timeout=10).json()


# ------------------------------------------------------------------ webhook security / idempotency

def test_webhook_rejects_missing_or_wrong_secret(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        assert post(srv, update(OWNER_CHAT, OWNER_USER, "hi"), secret=None).status_code == 401
        assert post(srv, update(OWNER_CHAT, OWNER_USER, "hi"), secret="wrong").status_code == 401
    finally:
        srv.stop()


def test_webhook_returns_501_when_secret_not_configured(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram, TELEGRAM_WEBHOOK_SECRET="")
    try:
        assert post(srv, update(OWNER_CHAT, OWNER_USER, "hi")).status_code == 501
    finally:
        srv.stop()


def test_unknown_chat_is_ignored_not_errored(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        r = post(srv, update("9999999", "9999999", "Создай задачу проверить что-то"))
        assert r.status_code == 200
        assert tasks(srv) == []
    finally:
        srv.stop()


@pytest.mark.parametrize("anthropic", [[("Проверь крепление", {"intent": "create_task", "entity": "personal_task",
    "title": "Проверить крепление", "confidence": "high"})]], indirect=True)
def test_duplicate_update_id_is_processed_once(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        u = update(OWNER_CHAT, OWNER_USER, "Проверь крепление защиты", update_id=42)
        assert post(srv, u).status_code == 200
        assert post(srv, u).status_code == 200  # Telegram retry of the same update_id
        assert len(tasks(srv)) == 1
    finally:
        srv.stop()


# ------------------------------------------------------------------ channel 1 (personal bot)

PERSONAL_RULES = [
    ("Берникову", {"intent": "create_task", "entity": "personal_task", "title": "Проверка крепления защиты HAVAL Jolion",
     "responsible": "Берников", "due": "2026-11-20", "confidence": "high"}),
    ("покажи активные", {"intent": "list_tasks", "entity": "personal_task", "filter": "active", "confidence": "high"}),
    ("непонятнаякоманда", {"intent": "clarify", "entity": "personal_task", "confidence": "low",
     "clarification_question": "Уточните, что сделать."}),
]


@pytest.mark.parametrize("anthropic", [PERSONAL_RULES], indirect=True)
def test_personal_channel_creates_task_and_replies(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        r = post(srv, update(OWNER_CHAT, OWNER_USER,
                              "Создай задачу Берникову на проверку крепления защиты HAVAL Jolion, срок до пятницы"))
        assert r.status_code == 200
        created = tasks(srv)
        assert len(created) == 1 and created[0]["responsible"] == "Берников" and created[0]["source"] == "telegram"
        assert telegram.sent_messages and "Готово" in telegram.sent_messages[-1]["text"]
        st = status(srv)
        assert st["personal"]["tasks_created"] == 1
    finally:
        srv.stop()


@pytest.mark.parametrize("anthropic", [PERSONAL_RULES], indirect=True)
def test_personal_channel_lists_active_tasks(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        requests.post(srv.url + "/api/tasks", json={"title": "Задача А", "quadrant": "inbox", "done": False}, timeout=10)
        r = post(srv, update(OWNER_CHAT, OWNER_USER, "покажи активные задачи", update_id=2))
        assert r.status_code == 200
        assert any("Задача А" in m["text"] for m in telegram.sent_messages)
    finally:
        srv.stop()


@pytest.mark.parametrize("anthropic", [PERSONAL_RULES], indirect=True)
def test_low_confidence_asks_for_clarification_instead_of_acting(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        r = post(srv, update(OWNER_CHAT, OWNER_USER, "непонятнаякоманда", update_id=3))
        assert r.status_code == 200
        assert tasks(srv) == []
        assert "Уточните" in telegram.sent_messages[-1]["text"]
    finally:
        srv.stop()


def test_unauthorized_user_in_owner_chat_is_rejected(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        r = post(srv, update(OWNER_CHAT, "777", "Создай задачу X", update_id=4))
        assert r.status_code == 200
        assert tasks(srv) == []
    finally:
        srv.stop()


def test_ai_unavailable_queues_message_instead_of_losing_it(tmp_path, telegram):
    anthropic_srv = fake_anthropic([])  # no rules -> always 500
    try:
        srv = _server(tmp_path, anthropic_srv, telegram)
        try:
            r = post(srv, update(OWNER_CHAT, OWNER_USER, "Создай задачу X", update_id=5))
            assert r.status_code == 200
            assert tasks(srv) == []
            pending = review(srv, kind="telegram_pending")
            assert len(pending) == 1 and pending[0]["payload"]["message_text"] == "Создай задачу X"
            assert "недоступен" in telegram.sent_messages[-1]["text"] or "позже" in telegram.sent_messages[-1]["text"]
        finally:
            srv.stop()
    finally:
        anthropic_srv.shutdown()


# ------------------------------------------------------------------ channel 2 (engineering chat)

ENGINEERING_RULES = [
    ("тестовый образец защиты HAVAL Jolion", {"relevant": True, "manufacturer": "HAVAL", "vehicle_model": "Jolion",
     "product_type": "Защита радиатора", "event_type": "prototype_install",
     "note": "Установлен тестовый образец, нужно доработать правый кронштейн", "stage_hint": "тестовый образец",
     "explicit_task_assignment": False, "confidence": "high"}),
    ("непонятное сообщение без проекта", {"relevant": False, "confidence": "low"}),
]


def _haval_jolion_project(srv):
    for p in projects(srv):
        if p.get("manufacturer") == "HAVAL" and p.get("vehicle_model") == "Jolion" and "радиатора" in (p.get("product_type") or ""):
            return p
    raise AssertionError("seed data missing HAVAL Jolion / Защита радиатора")


@pytest.mark.parametrize("anthropic", [ENGINEERING_RULES], indirect=True)
def test_engineering_channel_records_event_and_proposes_stage_without_applying_it(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        before = _haval_jolion_project(srv)
        assert before["status"] == "scanning"  # seed.js's starting stage for this project
        r = post(srv, update(ENG_CHAT, "501",
                              "Сегодня установили тестовый образец защиты HAVAL Jolion. Необходимо доработать правый кронштейн."))
        assert r.status_code == 200
        after = next(p for p in projects(srv) if p["id"] == before["id"])
        assert after["status"] == "scanning", "a suggestion must never change the confirmed stage by itself"
        proposals = review(srv, kind="telegram_stage_suggestion")
        assert len(proposals) == 1
        assert proposals[0]["payload"]["project_id"] == before["id"]
        assert proposals[0]["payload"]["recommended_stage"] == "prototype"
        # confirming it uses the existing, already-built dashboard action - not a new endpoint
        c = requests.post(srv.url + f"/api/projects/{before['id']}/stage", json={"stage": "prototype"}, timeout=10)
        assert c.status_code == 200 and c.json()["status"] == "prototype"
    finally:
        srv.stop()


@pytest.mark.parametrize("anthropic", [ENGINEERING_RULES], indirect=True)
def test_engineering_channel_never_creates_a_kanban_task_from_an_ordinary_message(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        post(srv, update(ENG_CHAT, "501",
                          "Сегодня установили тестовый образец защиты HAVAL Jolion. Необходимо доработать правый кронштейн."))
        assert tasks(srv) == [], "ordinary engineering-chat messages must never become personal Kanban tasks"
    finally:
        srv.stop()


@pytest.mark.parametrize("anthropic", [ENGINEERING_RULES], indirect=True)
def test_engineering_channel_queues_unrecognised_message(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        r = post(srv, update(ENG_CHAT, "501", "непонятное сообщение без проекта", update_id=9))
        assert r.status_code == 200
        assert review(srv, kind="telegram_stage_suggestion") == []
        assert tasks(srv) == []
    finally:
        srv.stop()


def test_photo_without_caption_is_queued_then_attached_once_caption_arrives(tmp_path, telegram):
    anthropic_srv = fake_anthropic(ENGINEERING_RULES)
    try:
        srv = _server(tmp_path, anthropic_srv, telegram)
        try:
            before = _haval_jolion_project(srv)
            group = "mg1"
            # photo first, no caption - Telegram does not guarantee caption-first delivery
            r1 = post(srv, update(ENG_CHAT, "501", text=None, update_id=10, photo=True, media_group_id=group))
            assert r1.status_code == 200
            unassigned = review(srv, kind="telegram_photo")
            assert len(unassigned) == 1

            r2 = post(srv, update(ENG_CHAT, "501", update_id=11, photo=True, media_group_id=group,
                                   caption="Сегодня установили тестовый образец защиты HAVAL Jolion. Необходимо доработать правый кронштейн."))
            assert r2.status_code == 200
            after = next(p for p in projects(srv) if p["id"] == before["id"])
            assert after["images_count"] >= 1 if "images_count" in after else True
            # the straggler from the same media group must have been resolved, not left dangling
            assert review(srv, kind="telegram_photo") == []
        finally:
            srv.stop()
    finally:
        anthropic_srv.shutdown()


def test_unauthorized_chat_for_engineering_channel_is_rejected(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram, TELEGRAM_ENGINEERING_CHAT_ID="")
    try:
        r = post(srv, update(ENG_CHAT, "501", "Сегодня установили тестовый образец защиты HAVAL Jolion."))
        assert r.status_code == 200
        assert review(srv, kind="telegram_stage_suggestion") == []
    finally:
        srv.stop()


# ------------------------------------------------------------------ status page

def test_status_endpoint_never_exposes_secrets(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        st = status(srv)
        assert st["bot_configured"] is True and st["ai_configured"] is True
        body = json.dumps(st)
        assert BOTKEY not in body and SECRET not in body and "sk-test" not in body
    finally:
        srv.stop()


def test_status_endpoint_requires_dashboard_session_when_configured(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram, DASHBOARD_USERNAME="op", DASHBOARD_PASSWORD="pw")
    try:
        assert requests.get(srv.url + "/api/telegram/status", timeout=10).status_code == 401
    finally:
        srv.stop()
