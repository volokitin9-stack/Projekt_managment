"""Daily personal-task digest to TELEGRAM_OWNER_CHAT_ID (server/telegram/dailyDigest.js) -
критическое требование №2, "Дополнение к ТЗ Telegram-интеграции «Штурмана»". Schedule and content
rules are copied verbatim from the old Claude Artifact's own scheduled-trigger prompt
(CRON_TZ=Europe/Moscow 53 7 * * 1-5) - see the module's own header comment. Synthetic task data
only - no real board content.

07:53 Europe/Moscow is 04:53 UTC in November (no daylight saving time).
"""
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from conftest import ROOT, ShturmanServer

OWNER_CHAT = "1000"
FRI = "2026-11-20"   # an ordinary working Friday
SAT, SUN = "2026-11-21", "2026-11-22"
MON = "2026-11-23"


def at(date, hhmm="07:53"):
    h, m = map(int, hhmm.split(":"))
    return f"{date}T{h - 3:02d}:{m:02d}:00Z"


def fake_telegram():
    sent = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            raw = json.dumps({"ok": True, "result": {"message_id": len(sent) + 1}}).encode()
            if self.path.endswith("/sendMessage"):
                sent.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.sent_messages = sent
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def telegram():
    srv = fake_telegram()
    yield srv
    srv.shutdown()


def make_server(tmp_path, telegram_srv, start_now, **env):
    e = {
        "TELEGRAM_BOT_TOKEN": "tg-bot-test-key", "TELEGRAM_OWNER_CHAT_ID": OWNER_CHAT,
        "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{telegram_srv.server_address[1]}",
        "TELEGRAM_DIGEST_TEST_HOOKS": "1", "TELEGRAM_DIGEST_TEST_NOW": start_now,
    }
    e.update(env)
    srv = ShturmanServer(tmp_path / "srv", extra_env=e)
    clock_at = start_now
    srv._digest_start_now = clock_at
    return srv


def start(srv):
    srv.start()
    clock(srv, srv._digest_start_now)
    return srv


def tick(s, now):
    return requests.post(s.url + "/api/telegram/digest/_test/tick", json={"now": now}, timeout=30).json()


def clock(s, now):
    requests.post(s.url + "/api/telegram/digest/_test/clock", json={"now": now}, timeout=10)


def make_task(srv, **fields):
    r = requests.post(srv.url + "/api/tasks", json=fields, timeout=10)
    assert r.status_code in (200, 201), r.text
    return r.json()


def tasks(srv):
    return requests.get(srv.url + "/api/tasks", timeout=10).json()


def digest_status(srv):
    return requests.get(srv.url + "/api/telegram/status", timeout=10).json()["daily_digest"]


def test_not_due_before_schedule_time_then_sends_at_schedule_time(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "06:00"))
    try:
        start(srv)
        make_task(srv, title="Проверить крепление", quadrant="inbox")
        r = tick(srv, at(FRI, "07:00"))
        assert r == {"action": "not_due", "date": FRI}
        assert telegram.sent_messages == []

        r = tick(srv, at(FRI, "07:53"))
        assert r == {"action": "sent", "date": FRI}
        assert len(telegram.sent_messages) == 1
        sent = telegram.sent_messages[0]
        assert sent["chat_id"] == OWNER_CHAT
        assert "Проверить крепление" in sent["text"]
        assert "Доброе утро!" in sent["text"]
    finally:
        srv.stop()


def test_never_sent_twice_the_same_day_even_across_a_restart(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "07:53"))
    try:
        start(srv)
        make_task(srv, title="Задача дня", quadrant="do")
        assert tick(srv, at(FRI, "07:53"))["action"] == "sent"
        assert tick(srv, at(FRI, "09:00"))["action"] == "already_done"
        assert len(telegram.sent_messages) == 1

        srv.restart()
        clock(srv, at(FRI, "10:00"))
        # a restart later the same day must not resend - startup tick sees last_sent_date == today
        assert len(telegram.sent_messages) == 1
    finally:
        srv.stop()


def test_never_creates_modifies_or_deletes_a_task(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "07:53"))
    try:
        start(srv)
        make_task(srv, title="Единственная задача", quadrant="inbox")
        before = tasks(srv)
        assert tick(srv, at(FRI, "07:53"))["action"] == "sent"
        after = tasks(srv)
        assert before == after, "the digest must only read tasks, never write them"
    finally:
        srv.stop()


def test_weekend_is_non_working_and_never_sends(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(SAT, "07:53"))
    try:
        start(srv)
        make_task(srv, title="Выходная задача", quadrant="inbox")
        assert tick(srv, at(SAT, "07:53")) == {"action": "non_working", "date": SAT}
        assert tick(srv, at(SUN, "07:53")) == {"action": "non_working", "date": SUN}
        assert telegram.sent_messages == []
    finally:
        srv.stop()


def test_a_run_more_than_the_grace_window_late_is_missed_not_replayed(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "07:53"))
    try:
        start(srv)
        r = tick(srv, at(FRI, "09:00"))  # 67 minutes late > 30-minute grace window
        assert r == {"action": "missed", "date": FRI}
        assert telegram.sent_messages == []
        # not replayed later the same day either
        assert tick(srv, at(FRI, "12:00")) == {"action": "missed", "date": FRI}
        assert telegram.sent_messages == []
    finally:
        srv.stop()


def test_server_started_after_schedule_time_does_not_replay_todays_digest(tmp_path, telegram):
    """startup=true always counts as a missed run, same rule as planSource/scheduler.js - a
    restart just before/after the scheduled time must not suddenly fire an unattended send."""
    srv = make_server(tmp_path, telegram, at(FRI, "08:30"))
    try:
        start(srv)
        assert digest_status(srv)["last_missed_date"] == FRI
        assert telegram.sent_messages == []
    finally:
        srv.stop()


def test_status_endpoint_reports_digest_state(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "07:53"))
    try:
        start(srv)
        make_task(srv, title="t", quadrant="inbox")
        st0 = digest_status(srv)
        assert st0["active"] is True and st0["schedule_time"] == "07:53" and st0["timezone"] == "Europe/Moscow"
        assert st0["last_sent_date"] is None
        tick(srv, at(FRI, "07:53"))
        st1 = digest_status(srv)
        assert st1["last_sent_date"] == FRI and st1["last_active_count"] == 1
    finally:
        srv.stop()


def test_build_digest_text_pure_function():
    """Grouping order, per-line fields, overdue marker, header/empty-state wording - see
    tests/js/telegram_digest_unit.js for the individual assertions."""
    r = subprocess.run(["node", "tests/js/telegram_digest_unit.js"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    results = json.loads(r.stdout.strip().splitlines()[-1])
    assert all(results.values()), results


def test_does_not_interfere_with_the_excel_planning_agent_schedule(tmp_path, telegram):
    """Критическое требование №2 п.7: the two schedules are independent state (telegram_digest vs.
    plan_source/plan_source_runs - server/store.js OBJECTS/COLLECTIONS) and independent timers
    (server/telegram/dailyDigest.js vs. server/planSource/scheduler.js) - ticking one must never
    record a run, send, or change next-run-date for the other."""
    import test_schedule as excel

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    excel.put(inbox, 1)
    e = {
        "TELEGRAM_BOT_TOKEN": "tg-bot-test-key", "TELEGRAM_OWNER_CHAT_ID": OWNER_CHAT,
        "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{telegram.server_address[1]}",
        "TELEGRAM_DIGEST_TEST_HOOKS": "1", "TELEGRAM_DIGEST_TEST_NOW": at(FRI, "06:00"),
    }
    srv = excel.make_server(tmp_path, inbox, start_now=at(FRI, "06:00"), **e)
    try:
        clock(srv, at(FRI, "06:00"))
        make_task(srv, title="Задача для сводки", quadrant="inbox")

        # tick the digest well before the Excel agent's own 09:00 Yekaterinburg schedule time -
        # the Excel run history must stay untouched
        d = tick(srv, at(FRI, "07:53"))
        assert d["action"] == "sent"
        assert excel.runs(srv) == []
        assert telegram.sent_messages and "Задача для сводки" in telegram.sent_messages[0]["text"]

        # now tick the Excel agent's own schedule (09:00 Asia/Yekaterinburg = 04:00 UTC) - the
        # digest's already-sent state for today must stay exactly as it was, and no second
        # Telegram message goes out
        x = excel.tick(srv, excel.at(FRI, "09:00"))
        assert x["action"] == "ran"
        assert digest_status(srv)["last_sent_date"] == FRI
        assert len(telegram.sent_messages) == 1, "the Excel agent's own run must never touch Telegram"
    finally:
        srv.stop()


def test_inactive_without_owner_chat_id_configured(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "07:53"), TELEGRAM_OWNER_CHAT_ID="")
    try:
        start(srv)
        assert tick(srv, at(FRI, "07:53")) == {"action": "inactive"}
        assert telegram.sent_messages == []
        assert digest_status(srv)["active"] is False
    finally:
        srv.stop()
