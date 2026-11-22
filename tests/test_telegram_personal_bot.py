"""Personal Telegram bot of «Доска проектов» without AI (server/telegram/personalBot.js): menu,
step-by-step task creation, task card, status/edit/done, read-only projects, access control,
channel separation and duplicate-update protection. Real server, fake Telegram + fake Anthropic
(the latter must never be called)."""
import pytest
import requests

from test_telegram import (fake_anthropic, fake_telegram, _server, post, tasks, projects, update,
                           OWNER_CHAT, OWNER_USER, ENG_CHAT)


@pytest.fixture
def anthropic():
    srv = fake_anthropic([])
    yield srv
    srv.shutdown()


@pytest.fixture
def telegram():
    srv = fake_telegram()
    yield srv
    srv.shutdown()


@pytest.fixture
def bot(tmp_path, anthropic, telegram):
    srv = _server(tmp_path, anthropic, telegram, TELEGRAM_PERSONAL_AI_ENABLED="", TELEGRAM_ENGINEERING_AI_ENABLED="false")
    srv.uid = 1000
    yield srv
    srv.stop()


def say(srv, telegram, text, update_id=None, message_id=None, chat=OWNER_CHAT, user=OWNER_USER):
    srv.uid += 1
    u = update(chat, user, text, update_id=update_id or srv.uid)
    if message_id is not None:
        u["message"]["message_id"] = message_id
    n = len(telegram.sent_messages)
    assert post(srv, u).status_code == 200
    return telegram.sent_messages[n:]


def last(telegram):
    return telegram.sent_messages[-1]


def buttons(msg):
    kb = ((msg.get("reply_markup") or {}).get("keyboard")) or []
    return [b["text"] for row in kb for b in row]


def code_of(task_id):
    return "".join(c for c in task_id.lower() if c.isalnum())[:8]


def test_start_help_menu_and_no_ai(bot, telegram, anthropic):
    for cmd in ("/start", "/help", "❓ Помощь"):
        out = say(bot, telegram, cmd)
        assert len(out) == 1 and "Доска проектов" in out[0]["text"] and "/tasks" in out[0]["text"]
        assert set(buttons(out[0])) == {"📋 Задачи", "➕ Новая задача", "🛠 Проекты", "❓ Помощь"}
    assert anthropic.calls == []


def test_create_task_step_by_step_saved_in_the_board(bot, telegram, anthropic):
    say(bot, telegram, "➕ Новая задача")
    assert "Название" in last(telegram)["text"]
    say(bot, telegram, "Подготовить КД на защиту картера")
    say(bot, telegram, "Для Tiggo 4, версия 2")
    out = say(bot, telegram, "31.02.2026")
    assert "Не понял дату" in out[0]["text"], "an impossible date is rejected, the step is repeated"
    say(bot, telegram, "25.12.2026 10:30")
    assert set(buttons(last(telegram))) >= {"Высокий", "Обычный", "Низкий"}
    say(bot, telegram, "Высокий")
    say(bot, telegram, "Петров")
    summary = last(telegram)["text"]
    assert "Подготовить КД" in summary and "25.12.2026 10:30" in summary and "Высокий" in summary
    assert tasks(bot) == [], "nothing is saved before confirmation"
    out = say(bot, telegram, "✅ Создать", update_id=5555, message_id=777)
    assert "создана" in out[0]["text"] and "Доска проектов" in out[0]["text"]
    board = tasks(bot)
    assert len(board) == 1
    t = board[0]
    assert t["title"] == "Подготовить КД на защиту картера" and t["description"] == "Для Tiggo 4, версия 2"
    assert t["due"] == "2026-12-25T10:30" and t["priority"] == "high" and t["responsible"] == "Петров"
    assert t["quadrant"] == "inbox" and t["done"] is False and t["source"] == "telegram"
    # duplicate Telegram delivery: same update_id, then the same message under a new update_id
    say(bot, telegram, "✅ Создать", update_id=5555, message_id=777)
    say(bot, telegram, "✅ Создать", update_id=5556, message_id=777)
    assert len(tasks(bot)) == 1
    assert anthropic.calls == []


def test_cancel_and_free_text_offer(bot, telegram):
    say(bot, telegram, "/new")
    say(bot, telegram, "Черновик")
    out = say(bot, telegram, "✖️ Отмена")
    assert "отменено" in out[0]["text"] and tasks(bot) == []
    out = say(bot, telegram, "заказать крепёж для стенда")
    assert "Создать задачу «заказать крепёж для стенда»" in out[0]["text"]
    say(bot, telegram, "✖️ Нет")
    assert tasks(bot) == []
    say(bot, telegram, "заказать крепёж для стенда")
    say(bot, telegram, "✅ Да, создать")
    for step in ("Пропустить", "завтра", "Пропустить", "Я"):
        say(bot, telegram, step)
    say(bot, telegram, "✅ Создать")
    [t] = tasks(bot)
    assert t["title"] == "заказать крепёж для стенда" and t["responsible"] == "Я" and t["priority"] is None
    assert len(t["due"]) == 10
    out = say(bot, telegram, "/new Быстрая задача")
    assert "Шаг 2/5" in out[0]["text"], "/new <title> skips the title step"


def test_card_status_edit_done_reopen(bot, telegram):
    created = requests.post(bot.url + "/api/tasks", json={"title": "Проверить стенд", "quadrant": "inbox", "done": False}, timeout=10).json()
    out = say(bot, telegram, "/tasks")
    assert f"/t_{code_of(created['id'])}" in out[0]["text"]
    out = say(bot, telegram, "1")
    assert out[0]["text"].startswith("📌 Проверить стенд") and "Статус: Распредели" in out[0]["text"]
    assert set(buttons(out[0])) == {"✅ Выполнено", "🔁 Статус", "✏️ Изменить", "⬅️ Меню"}
    say(bot, telegram, "🔁 Статус")
    out = say(bot, telegram, "Срочно")
    assert "статус «Срочно»" in out[0]["text"]
    say(bot, telegram, "✏️ Изменить")
    say(bot, telegram, "Срок")
    out = say(bot, telegram, "2026-12-01")
    assert "срок" in out[0]["text"] and "01.12.2026" in out[0]["text"]
    say(bot, telegram, "✏️ Изменить")
    say(bot, telegram, "Название")
    say(bot, telegram, "Проверить стенд №2")
    say(bot, telegram, "✏️ Изменить")
    say(bot, telegram, "Описание")
    say(bot, telegram, "Очистить")
    out = say(bot, telegram, "✅ Выполнено")
    assert "выполненной" in out[0]["text"] and "↩️ Вернуть в работу" in buttons(out[0])
    t = requests.get(bot.url + f"/api/tasks/{created['id']}", timeout=10).json()
    assert t["quadrant"] == "do" and t["due"] == "2026-12-01" and t["title"] == "Проверить стенд №2" and t["done"] is True
    assert t["description"] is None and t["version"] == created["version"] + 5
    say(bot, telegram, "↩️ Вернуть в работу")
    assert requests.get(bot.url + f"/api/tasks/{created['id']}", timeout=10).json()["done"] is False
    # open by code from anywhere
    out = say(bot, telegram, f"/t_{code_of(created['id'])}")
    assert out[0]["text"].startswith("📌 Проверить стенд №2")


def test_projects_are_read_only(bot, telegram):
    before = projects(bot)
    out = say(bot, telegram, "/projects")
    main = [p for p in before if p["record_category"] == "main_project" and not p.get("actual_completion_date")]
    assert f"Инженерные проекты в работе: {len(main)}" in out[0]["text"]
    stage_btn = next(b for b in buttons(out[0]) if b.startswith("Сканирование ("))
    out = say(bot, telegram, stage_btn)
    assert "/p_" in out[0]["text"]
    out = say(bot, telegram, "/projects haval jolion")
    assert "HAVAL Jolion" in out[0]["text"]
    code = out[0]["text"].split("/p_")[1].split()[0]
    out = say(bot, telegram, f"/p_{code}")
    assert out[0]["text"].startswith("🛠 HAVAL Jolion") and "Только просмотр" in out[0]["text"]
    say(bot, telegram, "🔎 Найти проект")
    out = say(bot, telegram, "несуществующая марка")
    assert "ничего не найдено" in out[0]["text"]
    assert projects(bot) == before, "the bot never changes projects"


def test_access_control_and_channel_separation(bot, telegram, anthropic):
    # a stranger, another user in the owner chat, and the КО group: no reply, no data
    say(bot, telegram, "/start", chat="999", user="999")
    say(bot, telegram, "/new", user="4242")
    out = say(bot, telegram, "/tasks", chat=ENG_CHAT, user="555")
    assert out == [] and telegram.sent_messages == []
    say(bot, telegram, "➕ Новая задача", chat=ENG_CHAT, user="555")
    assert tasks(bot) == []
    # a dialog started in the КО group does not exist for the owner (separate state per chat)
    out = say(bot, telegram, "Произвольный текст")
    assert "Создать задачу" in out[0]["text"]
    assert all(str(m.get("chat_id")) == OWNER_CHAT for m in telegram.sent_messages)
    assert anthropic.calls == []
    log = requests.get(bot.url + "/api/telegram/log?limit=50", timeout=10).json()["entries"]
    outcomes = {(e["channel"], e["outcome"]) for e in log}
    assert ("unknown", "rejected") in outcomes and ("personal", "rejected") in outcomes and ("engineering", "archived") in outcomes


@pytest.mark.parametrize("text,expected", [
    ("2026-12-25", "2026-12-25"), ("25.12.2026", "2026-12-25"), ("25.12.26", "2026-12-25"),
    ("25.12.2026 9:05", "2026-12-25T09:05"), ("2026-02-30", None), ("сегодня 25:00", None), ("abc", None)])
def test_due_parser(text, expected):
    import json
    import subprocess
    from conftest import ROOT
    r = subprocess.run(["node", "-e", f"process.env.DATA_DIR=require('os').tmpdir()+'/pb-'+process.pid;"
                        f"console.log(JSON.stringify(require('./server/telegram/personalBot').parseDue({json.dumps(text)})))"],
                       cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert json.loads(r.stdout.strip().splitlines()[-1]) == expected, r.stderr
