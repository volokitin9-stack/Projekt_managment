"""Cross-feature check: imported personal tasks (server/taskImport.js) work in the personal
Telegram bot (server/telegram/personalBot.js), done tasks stay out of the active list, and the
import touches neither engineering projects nor the КО archive/photos and sends nothing to
Telegram. SYNTHETIC export shaped like the real one (34 tasks: 13 active, 21 done, 18 subtasks)."""
import json

import pytest
import requests

from conftest import ShturmanServer
from test_ko_integration import fake_telegram, fake_anthropic, JPEG
from test_task_import import synthetic_export

USER, PASSWORD = "operator", "pass-word-123"
OWNER, ENG, SECRET, KO = "1000", "-4000", "wh-secret", "ko-analyst-key-0123456789abcdef"


@pytest.fixture
def srv(tmp_path):
    tg, ai = fake_telegram({"p1": JPEG}), fake_anthropic()
    s = ShturmanServer(tmp_path / "srv", seed_demo=True, extra_env={
        "DASHBOARD_USERNAME": USER, "DASHBOARD_PASSWORD": PASSWORD, "DASHBOARD_SESSION_SECRET": "s" * 40,
        "TELEGRAM_BOT_TOKEN": "tg-test", "TELEGRAM_WEBHOOK_SECRET": SECRET, "TELEGRAM_OWNER_CHAT_ID": OWNER,
        "TELEGRAM_OWNER_USER_ID": OWNER, "TELEGRAM_ENGINEERING_CHAT_ID": ENG, "TELEGRAM_ENGINEERING_AI_ENABLED": "false",
        "KO_ANALYST_API_KEY": KO, "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{tg.server_address[1]}",
        "ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_API_BASE_URL": f"http://127.0.0.1:{ai.server_address[1]}"}).start()
    s.tg, s.ai, s.n = tg, ai, 0
    yield s
    s.stop()
    tg.shutdown()
    ai.shutdown()


def hook(srv, chat, **m):
    srv.n += 1
    msg = {"message_id": srv.n, "date": 1795300000 + srv.n, "chat": {"id": int(chat), "type": "private" if chat == OWNER else "group"},
           "from": {"id": int(OWNER) if chat == OWNER else 555, "first_name": "U"}}
    msg.update(m)
    assert requests.post(srv.url + "/api/telegram/webhook", json={"update_id": srv.n, "message": msg},
                         headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}, timeout=20).status_code == 200


def say(srv, text):
    before = len(sent(srv))
    hook(srv, OWNER, text=text)
    return sent(srv)[before:]


def sent(srv):
    return [b for (m, b) in srv.tg.calls if m == "sendMessage"]


def login(srv):
    s = requests.Session()
    s.post(srv.url + "/api/login", json={"username": USER, "password": PASSWORD}, timeout=10)
    s.headers["X-CSRF-Token"] = s.get(srv.url + "/api/whoami", timeout=10).json()["csrf_token"]
    return s


def test_imported_tasks_in_bot_and_nothing_else_touched(srv):
    s = login(srv)
    # КО archive with a stored photo, before the import
    hook(srv, ENG, caption="фото КО", photo=[{"file_id": "p1", "file_unique_id": "u-p1", "width": 9, "height": 9, "file_size": 68}])
    ko_before = requests.get(srv.url + "/api/agent/ko/messages", headers={"Authorization": f"Bearer {KO}"}, timeout=10).json()
    files_before = sorted(p.name for p in (srv.data_dir / "ko-files").rglob("*") if p.is_file())
    projects_before = s.get(srv.url + "/api/projects", timeout=10).json()
    calls_before = list(srv.tg.calls)

    payload = synthetic_export()
    rep = s.post(srv.url + "/api/admin/task-import/analyze", json={"export": payload}, timeout=20).json()
    res = s.post(srv.url + "/api/admin/task-import/apply", json={"export": payload, "plan_hash": rep["plan_hash"]}, timeout=20)
    assert res.status_code == 201 and res.json()["imported"] == 34
    import_id = res.json()["import_id"]

    # 7. no Telegram traffic and no AI caused by the import
    assert srv.tg.calls == calls_before and srv.ai.calls == []
    # 6. projects and КО archive/photos unchanged
    assert s.get(srv.url + "/api/projects", timeout=10).json() == projects_before
    assert requests.get(srv.url + "/api/agent/ko/messages", headers={"Authorization": f"Bearer {KO}"}, timeout=10).json() == ko_before
    assert sorted(p.name for p in (srv.data_dir / "ko-files").rglob("*") if p.is_file()) == files_before

    # 4/5. /tasks lists exactly the 13 active imported tasks, never the 21 done ones
    out = say(srv, "/tasks")
    text = "\n".join(m["text"] for m in out)
    assert text.startswith("Активные задачи (13)")
    active = [t for t in payload["tasks"] if not t["done"]]
    done = [t for t in payload["tasks"] if t["done"]]
    assert all(t["title"] in text for t in active)
    assert not any(f"{t['title']} " in text or text.endswith(t["title"]) for t in done)
    code = lambda tid: "".join(c for c in tid.lower() if c.isalnum())[:8]
    assert all(f"/t_{code(t['id'])}" in text for t in active)

    # open an imported task with subtasks, edit it, mark it done
    target = next(t for t in active if t["subtasks"])
    out = say(srv, f"/t_{code(target['id'])}")
    assert out[0]["text"].startswith(f"📌 {target['title']}") and f"Подзадачи: 0/{len(target['subtasks'])}" in out[0]["text"]
    say(srv, "✏️ Изменить")
    say(srv, "Ответственный")
    out = say(srv, "Сидоров")
    assert "Ответственный: Сидоров" in out[0]["text"]
    out = say(srv, "✅ Выполнено")
    assert "выполненной" in out[0]["text"]
    t = s.get(srv.url + f"/api/tasks/{target['id']}", timeout=10).json()
    assert t["responsible"] == "Сидоров" and t["done"] is True
    assert t["subtasks"] == target["subtasks"] and t["createdAt"] == target["created_at"] and t["import"]["source_id"] == target["id"]
    assert say(srv, "/tasks")[0]["text"].startswith("Активные задачи (12)")
    # a done imported task opens as done and can be reopened only on purpose
    d = done[0]
    out = say(srv, f"/t_{code(d['id'])}")
    assert "Статус: ✅ выполнена" in out[0]["text"] and "↩️ Вернуть в работу" in [b["text"] for r in out[0]["reply_markup"]["keyboard"] for b in r]
    assert all(str(m.get("chat_id")) == OWNER for m in sent(srv)), "the bot only ever writes to the owner chat"

    # 8. re-import and rollback still work after bot edits; the edited task survives the rollback
    rep2 = s.post(srv.url + "/api/admin/task-import/analyze", json={"export": payload}, timeout=20).json()
    assert rep2["new"] == [] and len(rep2["already_imported"]) == 34
    rb = s.post(srv.url + f"/api/admin/task-import/{import_id}/rollback", json={}, timeout=20).json()
    assert rb["removed"] == 33 and [k["id"] for k in rb["kept_modified"]] == [target["id"]]
    left = {x["id"] for x in s.get(srv.url + "/api/tasks", timeout=10).json()}
    assert left == {target["id"]}
    assert s.get(srv.url + "/api/projects", timeout=10).json() == projects_before
