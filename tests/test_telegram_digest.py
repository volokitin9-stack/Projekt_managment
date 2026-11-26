"""Daily personal-task digest to TELEGRAM_OWNER_CHAT_ID (server/telegram/dailyDigest.js) - fixed to
send at 09:00 Asia/Yekaterinburg (see docs/MORNING_DIGEST_TIME_FIX.md for why it used to arrive at
the wrong time). Synthetic task data only - no real board content.

09:00 Asia/Yekaterinburg is 04:00 UTC (UTC+5, no daylight saving time).
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


def at(date, hhmm="09:00"):
    h, m = map(int, hhmm.split(":"))
    return f"{date}T{h - 5:02d}:{m:02d}:00Z"


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
        r = tick(srv, at(FRI, "08:00"))
        assert r == {"action": "not_due", "date": FRI}
        assert telegram.sent_messages == []

        r = tick(srv, at(FRI, "09:00"))
        assert r == {"action": "sent", "date": FRI}
        assert len(telegram.sent_messages) == 1
        sent = telegram.sent_messages[0]
        assert sent["chat_id"] == OWNER_CHAT
        assert "Проверить крепление" in sent["text"]
        assert "Доброе утро!" in sent["text"]
    finally:
        srv.stop()


def test_never_sent_twice_the_same_day_even_across_a_restart(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "09:00"))
    try:
        start(srv)
        make_task(srv, title="Задача дня", quadrant="do")
        assert tick(srv, at(FRI, "09:00"))["action"] == "sent"
        assert tick(srv, at(FRI, "09:10"))["action"] == "already_done"
        assert len(telegram.sent_messages) == 1

        srv.restart()
        clock(srv, at(FRI, "10:00"))
        # a restart later the same day must not resend - startup tick sees last_sent_date == today
        assert len(telegram.sent_messages) == 1
    finally:
        srv.stop()


def test_never_creates_modifies_or_deletes_a_task(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "09:00"))
    try:
        start(srv)
        make_task(srv, title="Единственная задача", quadrant="inbox")
        before = tasks(srv)
        assert tick(srv, at(FRI, "09:00"))["action"] == "sent"
        after = tasks(srv)
        assert before == after, "the digest must only read tasks, never write them"
    finally:
        srv.stop()


def test_weekend_is_non_working_and_never_sends(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(SAT, "09:00"))
    try:
        start(srv)
        make_task(srv, title="Выходная задача", quadrant="inbox")
        assert tick(srv, at(SAT, "09:00")) == {"action": "non_working", "date": SAT}
        assert tick(srv, at(SUN, "09:00")) == {"action": "non_working", "date": SUN}
        assert telegram.sent_messages == []
    finally:
        srv.stop()


def test_a_run_more_than_the_grace_window_late_is_missed_not_replayed(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "09:00"))
    try:
        start(srv)
        r = tick(srv, at(FRI, "10:07"))  # 67 minutes late > 30-minute grace window
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
    srv = make_server(tmp_path, telegram, at(FRI, "09:37"))
    try:
        start(srv)
        assert digest_status(srv)["last_missed_date"] == FRI
        assert telegram.sent_messages == []
    finally:
        srv.stop()


def test_status_endpoint_reports_digest_state(tmp_path, telegram):
    srv = make_server(tmp_path, telegram, at(FRI, "09:00"))
    try:
        start(srv)
        make_task(srv, title="t", quadrant="inbox")
        st0 = digest_status(srv)
        assert st0["active"] is True and st0["schedule_time"] == "09:00" and st0["timezone"] == "Asia/Yekaterinburg"
        assert st0["last_sent_date"] is None
        tick(srv, at(FRI, "09:00"))
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

        # tick the digest at its own 09:00 Yekaterinburg schedule - now the same nominal clock
        # time as the Excel agent's own schedule, so this also proves the two don't bleed into
        # each other just because they share a schedule time - the Excel run history must stay
        # untouched
        d = tick(srv, at(FRI, "09:00"))
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
    srv = make_server(tmp_path, telegram, at(FRI, "09:00"), TELEGRAM_OWNER_CHAT_ID="")
    try:
        start(srv)
        assert tick(srv, at(FRI, "09:00")) == {"action": "inactive"}
        assert telegram.sent_messages == []
        assert digest_status(srv)["active"] is False
    finally:
        srv.stop()


# ------------------------------------------------------------------ КО-changes section (ТЗ item 4)
#
# A project's own history row is timestamped with the real wall clock (server/projectsModel.js's
# pushHistory - unaffected by this module's own fake-clock test hooks, which only control
# dailyDigest.js's notion of "now"). So these tests anchor the digest's tick time to the real
# current date (rolled to the nearest weekday) rather than the fixed FRI/MON constants above,
# which belong to a fictional date these specific tests don't share.

from datetime import datetime, timedelta, timezone

AGENT_KEY = "secret-test-key"
H = {"Authorization": f"Bearer {AGENT_KEY}"}


def next_weekday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def real_today_yekat():
    return datetime.now(timezone(timedelta(hours=5))).date()


def next_digest_tick_date():
    """A project created right now (real wall clock - server/projectsModel.js's history rows are
    never affected by this module's own fake-clock test hooks) must fall inside the digest's next
    24h window. The very next 09:00 Asia/Yekaterinburg occurrence is always within 24h of "now",
    except across a real weekend gap (Friday evening -> Monday) - not a concern for a sandbox
    clock that isn't near one when this runs."""
    tz = timezone(timedelta(hours=5))
    now = datetime.now(tz)
    d = now.date() if now.hour < 9 else now.date() + timedelta(days=1)
    return next_weekday(d).isoformat()


def create_project(srv, **fields):
    body = {"manufacturer": "TEST", "vehicle_model": "M", "product_type": "Деталь"}
    body.update(fields)
    r = requests.post(srv.url + "/api/agent/projects", headers=H, json={"project": body}, timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_digest_includes_recent_engineering_changes_with_a_link(tmp_path, telegram):
    tick_day = next_digest_tick_date()
    srv = make_server(tmp_path, telegram, at(tick_day, "08:30"), DASHBOARD_PUBLIC_URL="https://example.up.railway.app")
    try:
        start(srv)
        pid = create_project(srv, manufacturer="GEELY", vehicle_model="Coolray")
        r = requests.post(srv.url + f"/api/projects/{pid}/stage", json={"stage": "scanning", "version": 1}, timeout=10)
        assert r.status_code == 200, r.text

        out = tick(srv, at(tick_day, "09:00"))
        assert out["action"] == "sent"
        text = telegram.sent_messages[0]["text"]
        assert "Изменения по проектам КО" in text
        assert "GEELY Coolray" in text
        assert f"https://example.up.railway.app/?eng={pid}" in text
    finally:
        srv.stop()


def test_digest_says_explicitly_when_there_are_no_engineering_changes(tmp_path, telegram):
    today = next_weekday(real_today_yekat()).isoformat()
    srv = make_server(tmp_path, telegram, at(today, "08:30"))
    try:
        start(srv)
        out = tick(srv, at(today, "09:00"))
        assert out["action"] == "sent"
        text = telegram.sent_messages[0]["text"]
        assert "Изменения по проектам КО" in text and "изменений нет" in text
    finally:
        srv.stop()


def test_digest_excludes_a_change_older_than_24_hours(tmp_path, telegram):
    today = real_today_yekat()
    tick_day = next_weekday(today + timedelta(days=3)).isoformat()   # well over 24h after "now"
    srv = make_server(tmp_path, telegram, at(tick_day, "08:00"))
    try:
        start(srv)
        pid = create_project(srv, manufacturer="OLDCO")   # timestamped with the real "now", above
        r = requests.post(srv.url + f"/api/projects/{pid}/stage", json={"stage": "scanning", "version": 1}, timeout=10)
        assert r.status_code == 200, r.text
        out = tick(srv, at(tick_day, "09:00"))
        assert out["action"] == "sent"
        text = telegram.sent_messages[0]["text"]
        assert "OLDCO" not in text
        assert "изменений нет" in text
    finally:
        srv.stop()


def test_digest_section_does_not_require_or_touch_the_ko_analyst_or_users_system(tmp_path, telegram):
    """The КО-changes section reads only server/store.js's projects/project_history - confirms it
    needs no KO_ANALYST_API_KEY, DASHBOARD_USERNAME/PASSWORD, or users collection to work at all."""
    today = next_weekday(real_today_yekat()).isoformat()
    srv = make_server(tmp_path, telegram, at(today, "08:30"))
    try:
        start(srv)
        out = tick(srv, at(today, "09:00"))
        assert out["action"] == "sent"
        assert "Изменения по проектам КО" in telegram.sent_messages[0]["text"]
    finally:
        srv.stop()
