"""Import of personal tasks from an old board export through the admin interface
(server/taskImport.js, /api/admin/task-import). SYNTHETIC data only - shaped like the real export
(34 tasks: 13 active, 21 done, 18 subtasks), no real board content."""
import json

import pytest
import requests

from conftest import ShturmanServer
from test_ko_integration import fake_telegram, fake_anthropic

USER, PASSWORD = "operator", "pass-word-123"
QUADS = ["inbox", "do", "plan", "delegate", "drop"]


def synthetic_export(n=34, active=13, subtasks=18):
    tasks = []
    left = subtasks
    for i in range(n):
        done = i >= active
        t = {"id": f"t{i:03d}", "title": f"Тестовая задача {i}", "quadrant": QUADS[i % 5], "done": done,
             "direction": ["E-com", "ОПТ", None][i % 3], "responsible": ["Я", "Петров", None][i % 3],
             "from": "самостоятельно" if i % 4 == 0 else None, "due": f"2026-0{1 + i % 9}-1{i % 9}" if i % 2 else None,
             "telegramNotified": i % 3 == 0, "calendarSynced": False, "source": "telegram" if i % 5 == 0 else None,
             "created_at": f"2026-0{1 + i % 9}-0{1 + i % 8}T08:00:00.000Z", "updated_at": "2026-11-19T10:00:00.000Z",
             "subtasks": []}
        take = 2 if left >= 2 and i % 4 == 1 else (1 if left and i % 4 == 3 else 0)
        for j in range(take):
            t["subtasks"].append({"id": f"s{i}-{j}", "title": f"Шаг {j}", "responsible": None, "due": None,
                                  "done": j == 0 and done, "createdAt": "2026-10-01T09:00:00.000Z"})
        left -= take
        tasks.append(t)
    assert left == 0 and sum(len(t["subtasks"]) for t in tasks) == subtasks
    return {"export_meta": {"exported_at": "2026-11-20T09:00:00Z", "source": {"type": "Claude Artifact (ArtifactData)",
            "collection": "tasks"}, "record_count": n}, "tasks": tasks}


@pytest.fixture
def srv(tmp_path):
    tg, ai = fake_telegram({}), fake_anthropic()
    s = ShturmanServer(tmp_path / "srv", seed_demo=True, extra_env={
        "DASHBOARD_USERNAME": USER, "DASHBOARD_PASSWORD": PASSWORD, "DASHBOARD_SESSION_SECRET": "s" * 40,
        "TELEGRAM_BOT_TOKEN": "tg-test", "TELEGRAM_OWNER_CHAT_ID": "1000", "TELEGRAM_ENGINEERING_CHAT_ID": "-4000",
        "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{tg.server_address[1]}", "ANTHROPIC_API_KEY": "sk-x",
        "ANTHROPIC_API_BASE_URL": f"http://127.0.0.1:{ai.server_address[1]}"}).start()
    s.tg, s.ai = tg, ai
    yield s
    s.stop()
    tg.shutdown()
    ai.shutdown()


def login(srv):
    s = requests.Session()
    assert s.post(srv.url + "/api/login", json={"username": USER, "password": PASSWORD}, timeout=10).status_code == 200
    s.headers["X-CSRF-Token"] = s.get(srv.url + "/api/whoami", timeout=10).json()["csrf_token"]
    return s


def analyze(s, srv, payload, name="shturman_tasks_export_test.json"):
    r = s.post(srv.url + "/api/admin/task-import/analyze", json={"export": payload, "file_name": name}, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


def apply(s, srv, payload, plan_hash, name="shturman_tasks_export_test.json"):
    return s.post(srv.url + "/api/admin/task-import/apply", json={"export": payload, "file_name": name, "plan_hash": plan_hash}, timeout=20)


def board(s, srv):
    return {t["id"]: t for t in s.get(srv.url + "/api/tasks", timeout=10).json()}


def test_dry_run_reports_everything_and_writes_nothing(srv):
    s = login(srv)
    s.post(srv.url + "/api/tasks", json={"title": "Тестовая задача 5", "quadrant": "plan"}, timeout=10)
    before = srv.data_file.read_text()
    rep = analyze(s, srv, synthetic_export())
    assert rep["source"]["tasks"] == 34 and rep["source"]["active"] == 13 and rep["source"]["done"] == 21
    assert rep["source"]["subtasks"] == 18
    assert len(rep["new"]) == 34 and rep["new_subtasks"] == 18 and rep["already_imported"] == [] and rep["conflicts"] == []
    assert rep["target"]["existing_tasks"] == 1
    assert [m["source_id"] for m in rep["title_matches"]] == ["t005"], "same title as an existing task is reported"
    assert set(rep["quadrant_mapping"]) == set(QUADS) and all(m["lossless"] for m in rep["quadrant_mapping"].values())
    assert sum(m["count"] for m in rep["quadrant_mapping"].values()) == 34
    lossy = {f["field"] for f in rep["lossy_fields"]}
    assert {"updated_at", "telegramNotified / calendarSynced", "история изменений артефакта"} <= lossy
    assert rep["can_apply"] is True and len(rep["plan_hash"]) == 64
    assert srv.data_file.read_text() == before, "dry run never writes"


def test_access_requires_session_csrf_and_confirmed_plan(srv):
    payload = synthetic_export()
    anon = requests.post(srv.url + "/api/admin/task-import/analyze", json={"export": payload}, timeout=10)
    assert anon.status_code == 401
    agent = requests.post(srv.url + "/api/admin/task-import/apply", headers={"Authorization": "Bearer secret-test-key"},
                          json={"export": payload}, timeout=10)
    assert agent.status_code == 401, "agent keys do not open the admin import"
    s = login(srv)
    rep = analyze(s, srv, payload)
    no_csrf = requests.Session()
    no_csrf.post(srv.url + "/api/login", json={"username": USER, "password": PASSWORD}, timeout=10)
    assert no_csrf.post(srv.url + "/api/admin/task-import/apply", json={"export": payload, "plan_hash": rep["plan_hash"]}, timeout=10).status_code == 403
    assert apply(s, srv, payload, None).status_code == 409
    assert apply(s, srv, payload, "0" * 64).status_code == 409
    # the board changed after the dry run -> the old plan is refused
    s.post(srv.url + "/api/tasks", json={"title": "появилась после проверки"}, timeout=10)
    assert apply(s, srv, payload, rep["plan_hash"]).status_code == 409
    assert len(board(s, srv)) == 1


def test_import_preserves_fields_dates_done_and_touches_nothing_else(srv):
    s = login(srv)
    manual = s.post(srv.url + "/api/tasks", json={"title": "Ручная", "quadrant": "do", "done": False}, timeout=10).json()
    projects_before = s.get(srv.url + "/api/projects", timeout=10).json()
    payload = synthetic_export()
    rep = analyze(s, srv, payload)
    r = apply(s, srv, payload, rep["plan_hash"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["imported"] == 34 and body["subtasks"] == 18 and body["import_id"].startswith("imp-")
    b = board(s, srv)
    assert len(b) == 35 and b[manual["id"]] == manual, "an existing task is not modified"
    src = {t["id"]: t for t in payload["tasks"]}
    for tid, t in src.items():
        got = b[tid]
        for k in ("title", "quadrant", "done", "direction", "responsible", "from", "due", "subtasks", "calendarSynced", "telegramNotified"):
            assert got[k] == t[k], (tid, k)
        assert got["createdAt"] == t["created_at"] and got["importedUpdatedAt"] == t["updated_at"], "original dates kept"
        assert "priority" not in got and "description" not in got and "project_id" not in got, "nothing invented"
        assert got["import"]["source_id"] == tid and got["import"]["import_id"] == body["import_id"]
    assert sum(1 for t in b.values() if t.get("import") and t["done"]) == 21, "done tasks stay done"
    assert sum(1 for t in b.values() if t.get("import") and not t["done"]) == 13
    assert s.get(srv.url + "/api/projects", timeout=10).json() == projects_before
    assert srv.tg.calls == [] and srv.ai.calls == [], "no Telegram notification, no AI"
    raw = json.loads(srv.data_file.read_text())
    rows = {d["id"]: d for d in raw["docs"] if d["collection"] == "tasks"}
    assert rows["t001"]["created_at"] == src["t001"]["created_at"], "board order keeps the original creation date"


def test_reimport_creates_nothing_and_later_export_adds_only_new(srv):
    s = login(srv)
    payload = synthetic_export()
    apply(s, srv, payload, analyze(s, srv, payload)["plan_hash"])
    s.patch(srv.url + "/api/tasks/t000", json={"version": 1, "title": "Изменена на доске"}, timeout=10)
    rep = analyze(s, srv, payload)
    assert rep["new"] == [] and len(rep["already_imported"]) == 34 and rep["new_subtasks"] == 0
    assert [x for x in rep["already_imported"] if x["modified_since_import"]] == [{"id": "t000", "modified_since_import": True}]
    r = apply(s, srv, payload, rep["plan_hash"])
    assert r.status_code == 200 and r.json()["imported"] == 0
    later = synthetic_export()
    later["tasks"][0]["title"] = "В артефакте переименовали"
    later["tasks"].append({"id": "t999", "title": "Новая после экспорта", "quadrant": "plan", "done": False,
                           "subtasks": [{"id": "x", "title": "шаг", "done": False}]})
    rep2 = analyze(s, srv, later)
    assert rep2["new"] == ["t999"] and rep2["new_subtasks"] == 1
    assert apply(s, srv, later, rep2["plan_hash"]).json()["imported"] == 1
    b = board(s, srv)
    assert len(b) == 35 and b["t000"]["title"] == "Изменена на доске", "never overwritten by a re-import"
    assert sum(len(t["subtasks"]) for t in b.values()) == 19, "no duplicated subtasks"


def test_conflicts_unknown_category_and_extra_fields(srv):
    s = login(srv)
    s.post(srv.url + "/api/tasks", json={"id": "t001", "title": "Совсем другая задача", "quadrant": "plan"}, timeout=10)
    payload = synthetic_export()
    payload["tasks"][2]["quadrant"] = "someday"
    payload["tasks"][3]["color"] = "red"
    payload["tasks"][4]["subtasks"] = [{"id": "z", "title": "шаг", "done": False, "note": "x"}]
    rep = analyze(s, srv, payload)
    assert rep["conflicts"] == [{"index": 1, "id": "t001", "type": "id_used_by_existing_task", "existing_title": "Совсем другая задача"}]
    assert rep["quadrant_mapping"]["someday"]["target"] == "inbox" and rep["quadrant_mapping"]["someday"]["lossless"] is False
    fields = {f["field"] for f in rep["lossy_fields"]}
    assert {"quadrant", "color", "subtasks[].note"} <= fields
    assert rep["can_apply"] is True
    assert apply(s, srv, payload, rep["plan_hash"]).json()["imported"] == 33
    b = board(s, srv)
    assert b["t001"]["title"] == "Совсем другая задача", "the conflicting existing task is untouched"
    assert b["t002"]["quadrant"] == "inbox" and b["t002"]["import"]["original_quadrant"] == "someday"
    assert b["t003"]["import"]["extra"] == {"color": "red"} and "color" not in b["t003"]
    assert b["t004"]["subtasks"][0]["note"] == "x"


def test_invalid_file_is_atomic_nothing_written(srv):
    s = login(srv)
    payload = synthetic_export()
    payload["tasks"].append(dict(payload["tasks"][0]))         # duplicate id inside the file
    rep = analyze(s, srv, payload)
    assert any(c["type"] == "duplicate_id_in_file" for c in rep["conflicts"]) and rep["can_apply"] is False
    assert apply(s, srv, payload, rep["plan_hash"]).status_code == 409
    assert board(s, srv) == {}
    bad = s.post(srv.url + "/api/admin/task-import/analyze", json={"export": {"no": "tasks"}}, timeout=10)
    assert bad.status_code == 400
    assert s.get(srv.url + "/api/admin/task-import", timeout=10).json()["imports"] == []


def test_rollback_removes_only_that_import_and_keeps_edited_tasks(srv):
    s = login(srv)
    first = synthetic_export(n=5, active=2, subtasks=2)
    id1 = apply(s, srv, first, analyze(s, srv, first)["plan_hash"]).json()["import_id"]
    second = {"tasks": [{"id": "n1", "title": "Второй импорт", "quadrant": "do", "done": False}]}
    id2 = apply(s, srv, second, analyze(s, srv, second)["plan_hash"]).json()["import_id"]
    manual = s.post(srv.url + "/api/tasks", json={"title": "Ручная"}, timeout=10).json()
    s.patch(srv.url + "/api/tasks/t001", json={"version": 1, "done": True}, timeout=10)   # edited after import
    url = srv.url + f"/api/admin/task-import/{id1}/rollback"
    assert requests.post(url, json={}, timeout=10).status_code == 401
    dry = s.post(url, json={"dry_run": True}, timeout=10).json()
    assert sorted(dry["would_remove"]) == ["t000", "t002", "t003", "t004"] and [k["id"] for k in dry["kept_modified"]] == ["t001"]
    assert len(board(s, srv)) == 7, "dry run removes nothing"
    r = s.post(url, json={}, timeout=10).json()
    assert r["removed"] == 4
    assert set(board(s, srv)) == {"t001", "n1", manual["id"]}, "other import and manual task untouched"
    again = s.post(url, json={}, timeout=10).json()
    assert again["removed"] == 0, "repeat rollback is a no-op"
    forced = s.post(url, json={"force": True}, timeout=10).json()
    assert forced["removed"] == 1 and set(board(s, srv)) == {"n1", manual["id"]}
    hist = {x["id"]: x for x in s.get(srv.url + "/api/admin/task-import", timeout=10).json()["imports"]}
    assert hist[id1]["rolled_back_at"] and hist[id2]["rolled_back_at"] is None
    # after a rollback the same file can be imported again
    rep = analyze(s, srv, first)
    assert len(rep["new"]) == 5
