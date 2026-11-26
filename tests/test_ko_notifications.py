"""КО notification bell (server/koNotificationsModel.js, GET/POST /api/ko/notifications*) - the
Kanban board's "Центр уведомлений агента КО". Reuses the КО archive/proposal harness from
test_ko_integration.py (fake Telegram, real server, KO_ANALYST_API_KEY submissions) rather than
re-building it: a notification only ever exists for a real proposal, so creating one here means
going through the same /api/agent/ko/proposals path the agent itself uses.
"""
import json
import subprocess

import pytest
import requests

from conftest import ROOT
from test_ko_integration import AGENT_KEY, ENG_CHAT, KO_KEY, PASSWORD, USER, env, ev, find_project, hook, ko, msg, project, session, stage_proposal, submit  # noqa: F401
from test_user_accounts import create_manager, csrf_of, manager_session

BOARD_STAGE = "scanning"
BOARD_CARD_COUNT = 14


def notifications(s, srv, status=None):
    url = srv.url + "/api/ko/notifications" + (f"?status={status}" if status else "")
    r = s.get(url, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def mark_read(s, srv, notif_id):
    return s.post(srv.url + f"/api/ko/notifications/{notif_id}/read", json={}, timeout=10)


def mark_all_read(s, srv):
    return s.post(srv.url + "/api/ko/notifications/read-all", json={}, timeout=10)


def _one_proposal(srv):
    """Submits exactly one real КО proposal (stage_change on a seeded project) and returns its
    project. Shared setup for most tests below."""
    hook(srv, msg(50, "Поставили образец защиты радиатора на Jolion"))
    p = find_project(srv, "HAVAL", "Защита радиатора")
    r = submit(srv, [stage_proposal(p["id"], "prototype", p["status"], 50, "образец")])
    assert r.status_code == 201, r.text
    return p


# ------------------------------------------------------------------ 1-2: creation, dedup


def test_a_new_proposal_creates_exactly_one_unread_notification(env):
    srv = env()
    _one_proposal(srv)
    s = session(srv)
    res = notifications(s, srv)
    assert len(res["notifications"]) == 1
    n = res["notifications"][0]
    assert n["read"] is False and n["resolved"] is False
    assert n["payload"]["kind"] == "stage_change"
    assert res["unread_count"] == 1 and res["pending_count"] == 1


def test_resubmitting_the_same_proposal_never_creates_a_second_notification(env):
    srv = env()
    hook(srv, msg(60, "Поставили образец защиты радиатора на Jolion"))
    hook(srv, msg(61, "Образец стоит, всё ок"))
    p = find_project(srv, "HAVAL", "Защита радиатора")
    prop = stage_proposal(p["id"], "prototype", p["status"], 60, "поставили образец", dedupe_key="agent-key")
    assert submit(srv, [prop]).status_code == 201
    s = session(srv)
    first = notifications(s, srv)
    assert len(first["notifications"]) == 1
    notif_id = first["notifications"][0]["notification_id"]
    # re-submission that merges new evidence into the SAME proposal (critical requirement:
    # "повторное получение предложения не должно создавать дубль")
    prop2 = dict(prop, evidence=[ev(60, "поставили образец"), ev(61, "образец стоит")])
    assert submit(srv, [prop2]).json()["counts"] == {"updated": 1}
    second = notifications(s, srv)
    assert len(second["notifications"]) == 1
    assert second["notifications"][0]["notification_id"] == notif_id, "the same notification row, not a new one"
    # a plain duplicate re-submission (nothing new at all) is equally a no-op
    assert submit(srv, [prop]).json()["counts"] == {"duplicate": 1}
    assert len(notifications(s, srv)["notifications"]) == 1


def test_two_different_proposals_create_two_separate_notifications(env):
    srv = env()
    hook(srv, msg(70, "Поставили образец защиты радиатора на Jolion"))
    hook(srv, msg(71, "Ответственный по буксировочному на Jolion теперь Сидоров"))
    radiator = find_project(srv, "HAVAL", "Защита радиатора")
    tow = find_project(srv, "HAVAL", "Буксировочное устройство")
    submit(srv, [stage_proposal(radiator["id"], "prototype", radiator["status"], 70, "поставили образец")])
    submit(srv, [{"kind": "assignee_change", "project_ref": {"project_id": tow["id"]}, "change": {"to": "Сидоров"},
                  "summary": "Новый ответственный", "rationale": "в чате", "confidence": "high",
                  "evidence": [ev(71, "теперь Сидоров")]}])
    res = notifications(session(srv), srv)
    assert len(res["notifications"]) == 2 and res["unread_count"] == 2 and res["pending_count"] == 2


# ------------------------------------------------------------------ 3-4: read state


def test_marking_one_notification_read_does_not_affect_the_other(env):
    srv = env()
    hook(srv, msg(80, "Поставили образец защиты радиатора на Jolion"))
    hook(srv, msg(81, "Ответственный по буксировочному на Jolion теперь Сидоров"))
    radiator = find_project(srv, "HAVAL", "Защита радиатора")
    tow = find_project(srv, "HAVAL", "Буксировочное устройство")
    submit(srv, [stage_proposal(radiator["id"], "prototype", radiator["status"], 80, "поставили образец")])
    submit(srv, [{"kind": "assignee_change", "project_ref": {"project_id": tow["id"]}, "change": {"to": "Сидоров"},
                  "summary": "Новый ответственный", "rationale": "в чате", "confidence": "high",
                  "evidence": [ev(81, "теперь Сидоров")]}])
    s = session(srv)
    ids = [n["notification_id"] for n in notifications(s, srv)["notifications"]]
    r = mark_read(s, srv, ids[0])
    assert r.status_code == 200 and r.json()["read"] is True
    res = notifications(s, srv)
    by_id = {n["notification_id"]: n for n in res["notifications"]}
    assert by_id[ids[0]]["read"] is True and by_id[ids[0]]["read_at"]
    assert by_id[ids[1]]["read"] is False
    assert res["unread_count"] == 1, "only the other one is still unread"
    assert res["pending_count"] == 2, "reading a notification never resolves its proposal"
    # re-marking an already-read one, and an unknown id, behave sanely
    assert mark_read(s, srv, ids[0]).status_code == 200
    assert mark_read(s, srv, "does-not-exist").status_code == 404


def test_mark_all_as_read(env):
    srv = env()
    hook(srv, msg(90, "Поставили образец защиты радиатора на Jolion"))
    hook(srv, msg(91, "Ответственный по буксировочному на Jolion теперь Сидоров"))
    radiator = find_project(srv, "HAVAL", "Защита радиатора")
    tow = find_project(srv, "HAVAL", "Буксировочное устройство")
    submit(srv, [stage_proposal(radiator["id"], "prototype", radiator["status"], 90, "поставили образец")])
    submit(srv, [{"kind": "assignee_change", "project_ref": {"project_id": tow["id"]}, "change": {"to": "Сидоров"},
                  "summary": "Новый ответственный", "rationale": "в чате", "confidence": "high",
                  "evidence": [ev(91, "теперь Сидоров")]}])
    s = session(srv)
    assert notifications(s, srv)["unread_count"] == 2
    r = mark_all_read(s, srv)
    assert r.status_code == 200 and r.json()["marked"] == 2
    res = notifications(s, srv)
    assert res["unread_count"] == 0
    assert all(n["read"] for n in res["notifications"])
    assert mark_all_read(s, srv).json()["marked"] == 0, "nothing left to mark"


# ------------------------------------------------------------------ 5: open a specific proposal


def test_each_notification_carries_the_full_proposal_for_click_through(env):
    srv = env()
    p = _one_proposal(srv)
    n = notifications(session(srv), srv)["notifications"][0]
    # everything the dashboard's "Предложения агента КО" row (and the panel's click-through) needs
    assert n["id"], "the proposal id - used to scroll/highlight the matching row (#ko-row-<id>)"
    assert n["payload"]["summary"] and n["payload"]["rationale"]
    assert n["payload"]["resolved_project_id"] == p["id"]
    assert n["payload"]["evidence"][0]["quote"]


def test_viewing_a_notification_never_applies_or_rejects_its_proposal(env):
    srv = env()
    p = _one_proposal(srv)
    s = session(srv)
    res = notifications(s, srv)
    notif_id = res["notifications"][0]["notification_id"]
    mark_read(s, srv, notif_id)
    after = notifications(s, srv)["notifications"][0]
    assert after["resolved"] is False and after["resolution"] is None
    assert s.get(srv.url + f"/api/projects/{p['id']}", timeout=10).json()["status"] == p["status"]


# ------------------------------------------------------------------ 6: status follows the proposal


def test_notification_status_updates_after_apply_and_after_reject(env):
    srv = env()
    hook(srv, msg(110, "Поставили образец защиты радиатора на Jolion"))
    hook(srv, msg(111, "По буксировочному на Jolion проблема с отверстиями"))
    radiator = find_project(srv, "HAVAL", "Защита радиатора")
    tow = find_project(srv, "HAVAL", "Буксировочное устройство")
    r1 = submit(srv, [stage_proposal(radiator["id"], "prototype", radiator["status"], 110, "поставили образец")])
    r2 = submit(srv, [{"kind": "problem", "project_ref": {"project_id": tow["id"]}, "summary": "Проблема с отверстиями",
                       "rationale": "в чате", "confidence": "high", "evidence": [ev(111, "проблема с отверстиями")]}])
    s = session(srv)
    proposals_by_kind = {n["payload"]["kind"]: n for n in notifications(s, srv)["notifications"]}
    stage_id = proposals_by_kind["stage_change"]["id"]
    problem_id = proposals_by_kind["problem"]["id"]
    assert s.post(srv.url + f"/api/ko/proposals/{stage_id}/apply", json={"version": radiator["version"]}, timeout=10).status_code == 200
    assert s.post(srv.url + f"/api/ko/proposals/{problem_id}/reject", json={"reason": "дубликат"}, timeout=10).status_code == 200
    by_id = {n["id"]: n for n in notifications(s, srv)["notifications"]}
    assert by_id[stage_id]["resolved"] is True and by_id[stage_id]["resolution"] == "applied"
    assert by_id[problem_id]["resolved"] is True and by_id[problem_id]["resolution"] == "rejected"
    assert notifications(s, srv)["pending_count"] == 0, "both resolved - nothing left pending"
    # the notification row itself is never deleted once a decision is made (критическое
    # требование: "не удалять уведомления после прочтения" / история уведомлений)
    assert len(notifications(s, srv, status="all")["notifications"]) == 2


# ------------------------------------------------------------------ 7: ko_viewer is denied


def test_ko_viewer_cannot_see_or_act_on_notifications(env):
    srv = env()
    _one_proposal(srv)
    admin = session(srv)
    created = create_manager(admin, srv, "Петров Пётр", "petrov_ko")
    mgr, login = manager_session(srv, "petrov_ko", created["generated_password"])
    assert login.status_code == 200
    assert mgr.get(srv.url + "/api/ko/notifications", timeout=10).status_code == 403
    notif_id = notifications(admin, srv)["notifications"][0]["notification_id"]
    assert mgr.post(srv.url + f"/api/ko/notifications/{notif_id}/read", json={}, timeout=10).status_code == 403
    assert mgr.post(srv.url + "/api/ko/notifications/read-all", json={}, timeout=10).status_code == 403
    # no session at all (not even a manager login) - same 403/401 boundary as every other /api/ko route
    assert requests.get(srv.url + "/api/ko/notifications", timeout=10).status_code == 401


# ------------------------------------------------------------------ 8: persists across a restart


def test_read_state_survives_a_server_restart(env):
    srv = env()
    hook(srv, msg(120, "Поставили образец защиты радиатора на Jolion"))
    hook(srv, msg(121, "Ответственный по буксировочному на Jolion теперь Сидоров"))
    radiator = find_project(srv, "HAVAL", "Защита радиатора")
    tow = find_project(srv, "HAVAL", "Буксировочное устройство")
    submit(srv, [stage_proposal(radiator["id"], "prototype", radiator["status"], 120, "поставили образец")])
    submit(srv, [{"kind": "assignee_change", "project_ref": {"project_id": tow["id"]}, "change": {"to": "Сидоров"},
                  "summary": "Новый ответственный", "rationale": "в чате", "confidence": "high",
                  "evidence": [ev(121, "теперь Сидоров")]}])
    s = session(srv)
    ids = [n["notification_id"] for n in notifications(s, srv)["notifications"]]
    mark_read(s, srv, ids[0])
    srv.restart()
    s2 = session(srv)
    res = notifications(s2, srv)
    by_id = {n["notification_id"]: n for n in res["notifications"]}
    assert by_id[ids[0]]["read"] is True
    assert by_id[ids[1]]["read"] is False
    assert res["unread_count"] == 1


# ------------------------------------------------------------------ 9: backfill for pre-existing proposals


def test_startup_backfill_covers_proposals_from_before_this_feature_and_never_duplicates(env, tmp_path):
    """Simulates the real upgrade scenario (критическое требование: "обеспечить совместимость с
    уже существующими предложениями агента ... не потерять ожидающие решения предложения и не
    создать дубли"): a ko_proposal review_queue item that predates ko_notifications entirely (no
    matching row at all), injected directly into the data file the way an old deploy would have
    left it, then the server is restarted twice to prove the backfill is idempotent."""
    srv = env()
    _one_proposal(srv)  # one proposal created the normal way, with its own notification already
    srv.stop()
    data = json.loads(srv.data_file.read_text())
    assert len(data["ko_notifications"]) == 1
    pre_existing = {
        "id": "pre-upgrade-proposal-1", "kind": "ko_proposal", "payload": {"kind": "problem", "summary": "Старое предложение",
            "rationale": "до обновления", "evidence": [], "photos": [], "confidence": "medium",
            "resolved_project_id": None, "project_ref": {}}, "reason": "Старое предложение", "source": "ko_analyst",
        "dedupe_key": "ko:v1:pre-existing", "severity": "review", "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:00.000Z", "resolved": False, "resolved_at": None, "resolution": None
    }
    data["review_queue"].append(pre_existing)
    srv.data_file.write_text(json.dumps(data))
    srv.start()
    s = session(srv)
    res = notifications(s, srv, status="all")
    by_proposal = {n["id"]: n for n in res["notifications"]}
    assert "pre-upgrade-proposal-1" in by_proposal, "the pre-existing proposal was not lost"
    assert by_proposal["pre-upgrade-proposal-1"]["read"] is True, "backfilled as already-seen, not a new alert"
    assert res["pending_count"] == 2, "both the old and the new proposal are still awaiting a decision"
    assert res["unread_count"] == 1, "only the genuinely new one counts as unread"
    count_after_first_boot = len(json.loads(srv.data_file.read_text())["ko_notifications"])
    srv.restart()
    count_after_second_boot = len(json.loads(srv.data_file.read_text())["ko_notifications"])
    assert count_after_second_boot == count_after_first_boot == 2, "re-running the backfill never creates a duplicate"


# ------------------------------------------------------------------ 10: real browser (UI + scroll)


def _seed_board_stage(srv, admin, count=BOARD_CARD_COUNT, stage=BOARD_STAGE):
    """Enough cards in one Kanban column to need scrolling - same recipe as test_kanban_scroll.py."""
    h = {"Authorization": f"Bearer {AGENT_KEY}"}
    for i in range(count):
        r = requests.post(srv.url + "/api/agent/projects", headers=h,
                           json={"project": {"manufacturer": "TEST", "vehicle_model": f"M{i}", "product_type": "Защита картера"}},
                           timeout=10)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        mv = admin.post(srv.url + f"/api/projects/{pid}/stage", json={"stage": stage, "version": 1}, timeout=10)
        assert mv.status_code == 200, mv.text


def _run_browser(srv, role, cookie, stage=BOARD_STAGE, proposal_id=""):
    out = subprocess.run(["node", "tests/js/ko_notifications_browser.js", srv.url, role, cookie, stage, proposal_id],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    if out.returncode == 3:
        pytest.skip("Playwright/Chromium not available: " + out.stdout + out.stderr)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_browser_admin_bell_panel_and_click_through_preserve_kanban_scroll(env):
    srv = env()
    admin = session(srv)
    _seed_board_stage(srv, admin)
    p = _one_proposal(srv)
    # a second, unrelated proposal stays unread through the click-through below, so "Отметить все
    # как прочитанные" has something real left to do.
    hook(srv, msg(51, "Ответственный по буксировочному на Jolion теперь Сидоров"))
    tow = find_project(srv, "HAVAL", "Буксировочное устройство")
    submit(srv, [{"kind": "assignee_change", "project_ref": {"project_id": tow["id"]}, "change": {"to": "Сидоров"},
                  "summary": "Новый ответственный", "rationale": "в чате", "confidence": "high",
                  "evidence": [ev(51, "теперь Сидоров")]}])
    proposal_id = [n["id"] for n in notifications(admin, srv)["notifications"] if n["payload"]["kind"] == "stage_change"][0]
    cookie = "shturman_session=" + admin.cookies.get("shturman_session")
    r = _run_browser(srv, "admin", cookie, BOARD_STAGE, proposal_id)
    assert r["bell_visible"] is True, "the bell must be visible for the administrator"
    assert r["scroll_set"] > 0, "test setup: the column must actually be tall enough to scroll"
    assert r["panel_has_target_item"] is True
    assert r["scroll_after_board_refresh"] == r["scroll_set"], "a full board refresh while the panel is open must not move the column"
    assert r["panel_closed_after_click"] is True
    assert r["landed_on_ko_subview"] is True, "clicking the notification opened «Предложения агента КО»"
    assert r["scroll_after_notif_click_through"] == r["scroll_set"], "критическое требование: не сбрасывать прокрутку Канбана"
    assert r["clicked_item_unread_class_after"] is False, "сохранять прочитанность на сервере"
    assert r["badge_after_click"] == "1", "the second, untouched proposal is still unread"
    assert r["badge_after_mark_all"] == "0"
    assert r["scroll_final"] == r["scroll_set"]
    assert not r["page_errors"]
    assert project(srv, admin, p["id"])["status"] == p["status"], "просмотр не подтверждает предложение"


def test_browser_ko_viewer_has_no_bell_and_api_refuses_it(env):
    srv = env()
    admin = session(srv)
    created = create_manager(admin, srv, "Петров Пётр", "petrov_bell")
    mgr, login = manager_session(srv, "petrov_bell", created["generated_password"])
    assert login.status_code == 200, login.text
    cookie = "shturman_session=" + mgr.cookies.get("shturman_session")
    r = _run_browser(srv, "manager", cookie)
    assert r["bell_display"] == "none", "критическое требование: менеджеры ko_viewer не видят колокольчик"
    assert r["api_status"] == 403, "критическое требование: серверные API уведомлений недоступны ko_viewer"
    assert not r["page_errors"]
