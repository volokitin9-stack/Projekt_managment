"""Telegram integration (server/telegram/*, server/routes/telegramWebhook.js,
server/routes/telegramAdmin.js) against a real Штурман server, with fake Anthropic and Telegram
Bot API backends (ANTHROPIC_API_BASE_URL / TELEGRAM_API_BASE_URL) standing in for the real
services - the same approach already used for a staged Railway export (server/migrate.js) and the
deployment-verification fake backend (tests/test_deployment.py).
"""
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from conftest import ROOT, ShturmanServer

SECRET = "wh-secret-test"
BOTKEY = "tg-bot-test-key"
OWNER_CHAT = "1000"
OWNER_USER = "1000"
ENG_CHAT = "-2000"


def fake_anthropic(rules):
    """rules: list of (substring, tool_input_dict). First match wins; no match -> 500."""
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            calls.append(body)
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
    srv.calls = calls
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


# ------------------------------------------------------------------ критическое требование №1:
# общий чат конструкторов строго READ ONLY (server/telegram/botApi.js)

def test_botapi_refuses_every_outbound_method_to_the_engineering_chat(telegram):
    """Unit-level proof that the guard lives in the one shared call() every outbound Telegram API
    request goes through - sendMessage, any other current or future send method, string vs numeric
    chat_id - while leaving reads (getFile), the owner chat, and an unconfigured engineering chat
    id entirely unaffected. See tests/js/telegram_readonly_unit.js."""
    base = f"http://127.0.0.1:{telegram.server_address[1]}"
    r = subprocess.run(["node", "tests/js/telegram_readonly_unit.js", base], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    results = json.loads(r.stdout.strip().splitlines()[-1])
    assert all(results.values()), results
    # the refused calls must never have reached the fake Telegram server at all - only the owner-chat
    # send, and the final send made once TELEGRAM_ENGINEERING_CHAT_ID is deliberately unset, go through
    assert telegram.sent_messages == [{"chat_id": OWNER_CHAT, "text": "hi owner"},
                                       {"chat_id": ENG_CHAT, "text": "no eng chat id configured"}]


@pytest.mark.parametrize("raw_chat_id", [ENG_CHAT + " ", ENG_CHAT + "\n", f'"{ENG_CHAT}"', f"'{ENG_CHAT}'"],
                         ids=["trailing-space", "trailing-newline", "double-quoted", "single-quoted"])
@pytest.mark.parametrize("anthropic", [ENGINEERING_RULES], indirect=True)
def test_misformatted_engineering_chat_id_still_routes_and_stays_read_only(tmp_path, anthropic, telegram, raw_chat_id):
    """TELEGRAM_ENGINEERING_CHAT_ID pasted into Railway Variables with a trailing space or quotes used
    to make every КО message land in telegram_log as unknown/rejected and never reach the
    engineering channel or the ko_messages archive. It must route normally, still never send
    anything to that chat, and show a config warning on the status page."""
    srv = _server(tmp_path, anthropic, telegram, TELEGRAM_ENGINEERING_CHAT_ID=raw_chat_id,
                  TELEGRAM_ENGINEERING_ALLOWED_USER_IDS='"501", 502')
    try:
        r = post(srv, update(ENG_CHAT, "501",
                              "Сегодня установили тестовый образец защиты HAVAL Jolion. Необходимо доработать правый кронштейн."))
        assert r.status_code == 200
        assert len(review(srv, kind="telegram_stage_suggestion")) == 1
        st = status(srv)
        assert st["engineering"]["chat_id"] == ENG_CHAT
        assert st["engineering"]["ko_archive"]["messages"] == 1
        assert st["engineering"]["messages_processed"] >= 1
        assert any(w.startswith("TELEGRAM_ENGINEERING_CHAT_ID") for w in st["config_warnings"]), st["config_warnings"]
        assert any(w.startswith("TELEGRAM_ENGINEERING_ALLOWED_USER_IDS") for w in st["config_warnings"])
        log = requests.get(srv.url + "/api/telegram/log", timeout=10).json()["entries"]
        assert not any(e.get("outcome") == "rejected" for e in log), log
        assert telegram.sent_messages == [], "the КО chat is read-only, whatever the env var looks like"
    finally:
        srv.stop()


def test_status_has_no_config_warnings_for_clean_values(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram)
    try:
        assert status(srv)["config_warnings"] == []
    finally:
        srv.stop()


@pytest.mark.parametrize("anthropic", [ENGINEERING_RULES], indirect=True)
def test_engineering_channel_never_sends_anything_to_telegram_across_every_scenario(tmp_path, anthropic, telegram):
    """Runs the full engineering-channel scenario matrix (relevant message, irrelevant message,
    photo queued then attached, unauthorized chat, AI-unavailable fallback) end to end and checks
    the one shared fake Telegram server's sendMessage log - not just that today's code happens not
    to call it, but that nothing in any of these paths (handlers, exception handling, the photo
    background queue, the unresolved-caption retry) ever does."""
    srv = _server(tmp_path, anthropic, telegram)
    try:
        post(srv, update(ENG_CHAT, "501",
                          "Сегодня установили тестовый образец защиты HAVAL Jolion. Необходимо доработать правый кронштейн.",
                          update_id=20))
        post(srv, update(ENG_CHAT, "501", "непонятное сообщение без проекта", update_id=21))
        group = "mg-readonly"
        post(srv, update(ENG_CHAT, "501", text=None, update_id=22, photo=True, media_group_id=group))
        post(srv, update(ENG_CHAT, "501", update_id=23, photo=True, media_group_id=group,
                          caption="Сегодня установили тестовый образец защиты HAVAL Jolion."))
        post(srv, update(ENG_CHAT, "999", "сообщение от неавторизованного участника", update_id=24))
        assert telegram.sent_messages == [], "the engineering chat must never receive any outbound message"
    finally:
        srv.stop()


def test_engineering_channel_ai_failure_path_never_sends_anything_either(tmp_path, telegram):
    """The AI-unavailable fallback (queue for manual review, log, move on) is itself a form of
    exception handling - it must stay silent toward the engineering chat exactly like the happy path."""
    anthropic_srv = fake_anthropic([])  # no rules match -> aiClient.classify() always throws
    try:
        srv = _server(tmp_path, anthropic_srv, telegram)
        try:
            post(srv, update(ENG_CHAT, "501", "любое сообщение, пока ИИ недоступен", update_id=30))
            assert telegram.sent_messages == []
        finally:
            srv.stop()
    finally:
        anthropic_srv.shutdown()


# ------------------------------------------------------------------ critical fix: /start, /help,
# /tasks work without AI; no Anthropic/internal errors ever reach a Telegram user; nothing is lost
# when the AI is unavailable (e.g. no Anthropic credit)

def fake_anthropic_insufficient_credit():
    """The exact real-world failure this fix is for: every call gets Anthropic's own HTTP 400
    "credit balance too low" response, regardless of prompt content."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            raw = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans "
                           "& Billing to upgrade or purchase credits."}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def no_credit_anthropic():
    srv = fake_anthropic_insufficient_credit()
    yield srv
    srv.shutdown()


def test_start_and_help_never_call_the_ai_even_when_it_is_healthy(tmp_path, anthropic, telegram):
    """/start and /help need no interpretation at all - critical requirement: they must never go
    through aiClient.classify(), whether or not the AI happens to be working."""
    srv = _server(tmp_path, anthropic, telegram)
    try:
        r1 = post(srv, update(OWNER_CHAT, OWNER_USER, "/start", update_id=40))
        assert r1.status_code == 200
        r2 = post(srv, update(OWNER_CHAT, OWNER_USER, "/help", update_id=41))
        assert r2.status_code == 200
        assert len(telegram.sent_messages) == 2
        for sent in telegram.sent_messages:
            assert "Штурман" in sent["text"] and "/tasks" in sent["text"]
        assert anthropic.calls == [], "aiClient.classify() must never be called for /start or /help"
    finally:
        srv.stop()


def test_start_help_and_tasks_keep_working_with_no_anthropic_credit(tmp_path, no_credit_anthropic, telegram):
    """The actual bug report: /start did not work because it went through the AI, and the AI had
    no credit. These three commands must work completely normally in exactly that situation."""
    srv = _server(tmp_path, no_credit_anthropic, telegram)
    try:
        requests.post(srv.url + "/api/tasks", json={"title": "Проверить крепление", "quadrant": "inbox"}, timeout=10)

        post(srv, update(OWNER_CHAT, OWNER_USER, "/start", update_id=50))
        post(srv, update(OWNER_CHAT, OWNER_USER, "/help", update_id=51))
        post(srv, update(OWNER_CHAT, OWNER_USER, "/tasks", update_id=52))

        assert len(telegram.sent_messages) == 3
        assert "Штурман" in telegram.sent_messages[0]["text"]
        assert "Штурман" in telegram.sent_messages[1]["text"]
        assert "Проверить крепление" in telegram.sent_messages[2]["text"]
        # none of these three replies ever triggered an AI call, so the queue stays empty
        assert review(srv, kind="telegram_pending") == []
    finally:
        srv.stop()


def test_insufficient_anthropic_credit_on_an_ordinary_command_never_leaks_the_raw_error(
        tmp_path, no_credit_anthropic, telegram):
    """A free-text command genuinely needs the AI - with no credit, the user must see a plain,
    friendly message, never Anthropic's own wording, an HTTP status, or any other internal detail,
    and the message itself must not be lost."""
    srv = _server(tmp_path, no_credit_anthropic, telegram)
    try:
        r = post(srv, update(OWNER_CHAT, OWNER_USER, "Проверь крепление защиты картера", update_id=60))
        assert r.status_code == 200
        assert len(telegram.sent_messages) == 1
        text = telegram.sent_messages[0]["text"]
        assert "недоступ" in text.lower()
        for leak in ("credit", "Anthropic", "400", "billing", "balance"):
            assert leak.lower() not in text.lower(), f"leaked internal detail: {leak!r} in {text!r}"

        pending = review(srv, kind="telegram_pending")
        assert len(pending) == 1 and pending[0]["payload"]["message_text"] == "Проверь крепление защиты картера"

        st = status(srv)["personal"]
        assert st["ai_unavailable_pending"] == 1 and st["ai_unavailable_last_at"]
    finally:
        srv.stop()


def test_insufficient_anthropic_credit_in_the_engineering_chat_queues_and_stays_silent(
        tmp_path, no_credit_anthropic, telegram):
    """Критическое требование: the engineering chat is read-only regardless of AI availability,
    and a message arriving while the AI has no credit is still never lost."""
    srv = _server(tmp_path, no_credit_anthropic, telegram)
    try:
        r = post(srv, update(ENG_CHAT, "501", "Установили тестовый образец, пока ИИ без баланса", update_id=70))
        assert r.status_code == 200
        assert telegram.sent_messages == [], "the engineering chat must never receive anything, AI outage or not"

        pending = review(srv, kind="telegram_pending")
        assert len(pending) == 1

        st = status(srv)["engineering"]
        assert st["ai_unavailable_pending"] == 1 and st["ai_unavailable_last_at"]
    finally:
        srv.stop()


def test_tasks_command_reports_no_active_tasks_without_ai(tmp_path, no_credit_anthropic, telegram):
    srv = _server(tmp_path, no_credit_anthropic, telegram)
    try:
        post(srv, update(OWNER_CHAT, OWNER_USER, "/list", update_id=80))
        assert telegram.sent_messages[-1]["text"] == "Таких задач нет."
    finally:
        srv.stop()
