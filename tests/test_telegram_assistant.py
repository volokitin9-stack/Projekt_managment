"""Natural-language layer of the personal Telegram bot (server/telegram/nlu/*) - rule-based,
no paid LLM API. Builds on tests/test_telegram_personal_bot.py's fixtures (same fake Telegram +
fake Anthropic backends, same real server) - every test here additionally asserts the fake
Anthropic backend was never called and every outbound message stayed inside the owner's own chat,
exactly like that file already does for the scripted engine.
"""
import json
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

from conftest import ROOT
from test_telegram import ENG_CHAT, OWNER_CHAT, projects as list_projects, tasks as list_tasks
from test_telegram_personal_bot import anthropic, bot, buttons, code_of, last, say, telegram  # noqa: F401

AGENT_KEY = "secret-test-key"
H = {"Authorization": f"Bearer {AGENT_KEY}"}


def test_nlu_unit_scenarios(tmp_path):
    r = subprocess.run(["node", "tests/js/nlu_unit.js", str(tmp_path)], cwd=ROOT,
                        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == "ok"


def create_project(srv, **fields):
    body = {"manufacturer": "TEST", "vehicle_model": "M", "product_type": "Деталь"}
    body.update(fields)
    r = requests.post(srv.url + "/api/agent/projects", headers=H, json={"project": body}, timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def confirm_stage(srv, project_id, stage):
    r = requests.post(srv.url + f"/api/projects/{project_id}/stage", json={"stage": stage, "version": 1}, timeout=10)
    assert r.status_code == 200, r.text


def create_task(srv, **fields):
    body = {"title": "Задача", "quadrant": "inbox", "done": False}
    body.update(fields)
    r = requests.post(srv.url + "/api/tasks", json=body, timeout=10)
    assert r.status_code == 201, r.text
    return r.json()


def assert_owner_only_no_ai(telegram, anthropic):
    assert anthropic.calls == [], "the NLU layer must never call the paid AI backend"
    assert all(str(m.get("chat_id")) == OWNER_CHAT for m in telegram.sent_messages), \
        "no outbound message may ever reach the КО group chat"


# ------------------------------------------------------------------ 1-2: brand synonyms & search

def test_brand_synonyms_and_stage_search(bot, telegram, anthropic):
    baw_id = create_project(bot, manufacturer="BAW", vehicle_model="K", product_type="Защита картера",
                             assigned_engineer="Калиткин")
    confirm_stage(bot, baw_id, "development")
    before = list_projects(bot)

    out = say(bot, telegram, "Какие проекты Джили сейчас в работе?")
    assert "GEELY" in out[0]["text"] and "Monjaro" in out[0]["text"] and "Coolray" in out[0]["text"]

    out = say(bot, telegram, "Покажи все проекты GEELY.")
    assert "GEELY" in out[0]["text"]

    out = say(bot, telegram, "Какие проекты HAVAL на замере?")
    assert "ничего не найдено" in out[0]["text"], "HAVAL only exists at other stages in the seed data - must say so, not invent a match"

    out = say(bot, telegram, "Что сейчас разрабатываем для BAW?")
    assert "BAW" in out[0]["text"] and "K" in out[0]["text"]

    out = say(bot, telegram, "Какие проекты у Калиткина?")
    assert "BAW" in out[0]["text"], "a surname's genitive form must still match the stored nominative"

    out = say(bot, telegram, "Что сейчас на оформлении?")
    assert "CHANGAN" in out[0]["text"] and "TOYOTA" in out[0]["text"]

    out = say(bot, telegram, "Покажи проекты, которые долго находятся на стадии.")
    assert "ничего не найдено" in out[0]["text"], "every seeded project just entered its stage - none should qualify yet"

    assert list_projects(bot) == before, "the assistant must never write to a project"
    assert_owner_only_no_ai(telegram, anthropic)


# ------------------------------------------------------------------ 3: short multi-turn dialogues

def test_followup_context_and_ordinal_pick(bot, telegram, anthropic):
    out = say(bot, telegram, "Какие проекты GEELY в работе?")
    assert "Monjaro" in out[0]["text"] and "Coolray" in out[0]["text"] and "2)" in out[0]["text"]

    out = say(bot, telegram, "А какие из них на замере?")
    assert "Coolray" in out[0]["text"] and "Monjaro" not in out[0]["text"], \
        "the brand from the previous turn must be kept, the new stage filter added"

    say(bot, telegram, "Какие проекты GEELY в работе?")
    out = say(bot, telegram, "Покажи второй")
    assert out[0]["text"].startswith("🛠") and "Coolray" in out[0]["text"], "ordinal picks the 2nd item of the last list shown"

    out = say(bot, telegram, "Покажи пятый")
    assert "Нет" in out[0]["text"] or "Не понял" in out[0]["text"], "out-of-range ordinal must not invent a project"

    assert_owner_only_no_ai(telegram, anthropic)


# ------------------------------------------------------------------ task queries

def test_task_queries_overdue_today_week_responsible(bot, telegram, anthropic):
    tz = ZoneInfo("Asia/Yekaterinburg")
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    this_week_other_day = monday + timedelta(days=(today.weekday() + 1) % 7)  # still Mon..Sun, != today
    create_task(bot, title="Старый долг", due="2020-01-01")
    create_task(bot, title="Сделать сегодня", due=today.isoformat())
    create_task(bot, title="На неделе", due=this_week_other_day.isoformat())
    create_task(bot, title="Задача Фурсы", responsible="Фурса")

    out = say(bot, telegram, "Какие мои задачи просрочены?")
    assert "Старый долг" in out[0]["text"] and "На неделе" not in out[0]["text"]

    out = say(bot, telegram, "Что нужно сделать сегодня?")
    assert "Сделать сегодня" in out[0]["text"]

    out = say(bot, telegram, "Покажи задачи Фурсы.")
    assert "Задача Фурсы" in out[0]["text"] and "Старый долг" not in out[0]["text"]

    out = say(bot, telegram, "Какие задачи на этой неделе?")
    assert "На неделе" in out[0]["text"]

    assert_owner_only_no_ai(telegram, anthropic)


# ------------------------------------------------------------------ 5: task mutation + confirmation

def test_create_task_via_nlu_requires_confirmation(bot, telegram, anthropic):
    out = say(bot, telegram, "Создай задачу проверить крепления защиты BAW.", update_id=9001, message_id=90010)
    assert "Создать задачу" in out[0]["text"] and "проверить крепления защиты BAW" in out[0]["text"]
    assert "не указан" in out[0]["text"]
    assert list_tasks(bot) == [], "nothing is written before the owner confirms"

    out = say(bot, telegram, "✖️ Нет")
    assert list_tasks(bot) == [], "declining must not create anything"

    say(bot, telegram, "Создай задачу проверить крепления защиты BAW.", update_id=9002, message_id=90011)
    out = say(bot, telegram, "✅ Да", update_id=9003, message_id=90012)
    assert "создана" in out[0]["text"]
    [t] = list_tasks(bot)
    assert t["title"] == "проверить крепления защиты BAW." and t["due"] is None
    assert_owner_only_no_ai(telegram, anthropic)


def test_move_task_shows_old_and_new_value_before_applying(bot, telegram, anthropic):
    t = create_task(bot, title="Проверить TIREI крепление", due="2026-11-25")
    out = say(bot, telegram, "Перенеси задачу TIREI на пятницу.")
    assert "Было" in out[0]["text"] and "Будет" in out[0]["text"] and "25.11.2026" in out[0]["text"]
    assert list_tasks(bot)[0]["due"] == "2026-11-25", "nothing changes before confirmation"

    out = say(bot, telegram, "✅ Да")
    assert "срок" in out[0]["text"].lower()
    after = requests.get(bot.url + f"/api/tasks/{t['id']}", timeout=10).json()
    assert after["due"] != "2026-11-25" and len(after["due"]) == 10
    assert_owner_only_no_ai(telegram, anthropic)


def test_complete_task_via_nlu_requires_confirmation(bot, telegram, anthropic):
    t = create_task(bot, title="Защита HAVAL картера")
    out = say(bot, telegram, "Отметь задачу по HAVAL выполненной.")
    assert "Отметить" in out[0]["text"] and "выполненной" in out[0]["text"]
    assert requests.get(bot.url + f"/api/tasks/{t['id']}", timeout=10).json()["done"] is False

    out = say(bot, telegram, "✅ Да")
    assert "выполненной" in out[0]["text"]
    assert requests.get(bot.url + f"/api/tasks/{t['id']}", timeout=10).json()["done"] is True
    assert_owner_only_no_ai(telegram, anthropic)


# ------------------------------------------------------------------ 4: ambiguous matches

def test_ambiguous_task_match_offers_a_choice(bot, telegram, anthropic):
    t1 = create_task(bot, title="Защита картера GEELY")
    t2 = create_task(bot, title="Защита картера HAVAL")
    out = say(bot, telegram, "Отметь задачу защита выполненной.")
    assert "несколько" in out[0]["text"].lower()
    assert "GEELY" in out[0]["text"] and "HAVAL" in out[0]["text"]

    out = say(bot, telegram, "1")
    assert "Отметить" in out[0]["text"] and "GEELY" in out[0]["text"]
    out = say(bot, telegram, "✅ Да")
    assert requests.get(bot.url + f"/api/tasks/{t1['id']}", timeout=10).json()["done"] is True
    assert requests.get(bot.url + f"/api/tasks/{t2['id']}", timeout=10).json()["done"] is False
    assert_owner_only_no_ai(telegram, anthropic)


def test_task_not_found_explains_instead_of_guessing(bot, telegram, anthropic):
    out = say(bot, telegram, "Отметь задачу по несуществующийтекст выполненной.")
    assert "Не нашёл" in out[0]["text"]
    assert_owner_only_no_ai(telegram, anthropic)


# ------------------------------------------------------------------ project changes are refused

def test_project_mutation_through_chat_is_refused(bot, telegram, anthropic):
    before = list_projects(bot)
    out = say(bot, telegram, "Перенеси проект HAVAL на этап оформления")
    assert "не поддерживается" in out[0]["text"] and "просмотр" in out[0]["text"]
    assert list_projects(bot) == before
    assert_owner_only_no_ai(telegram, anthropic)


# ------------------------------------------------------------------ unrecognised text

def test_unrecognised_nlu_leaning_text_explains_available_actions(bot, telegram, anthropic):
    out = say(bot, telegram, "Какие проекты зщт123 в работе?")
    assert "ничего не найдено" in out[0]["text"], "a brand-shaped query with no match must say so, not fall back to 'create a task'"


# ------------------------------------------------------------------ 6: duplicate-update protection

def test_duplicate_update_does_not_duplicate_an_nlu_created_task(bot, telegram, anthropic):
    say(bot, telegram, "Создай задачу разовая проверка.", update_id=5001, message_id=50010)
    say(bot, telegram, "✅ Да", update_id=5002, message_id=50011)
    assert len(list_tasks(bot)) == 1

    # Telegram redelivers the *original* triggering message under a new update_id (same message_id
    # - this is the realistic retry shape; the webhook's own claimUpdate() dedup is per update_id
    # and would not catch this on its own).
    say(bot, telegram, "Создай задачу разовая проверка.", update_id=5003, message_id=50010)
    say(bot, telegram, "✅ Да", update_id=5004, message_id=50012)
    assert len(list_tasks(bot)) == 1, "the same origin message must never create a second task"
    assert_owner_only_no_ai(telegram, anthropic)


# ------------------------------------------------------------------ compatibility: scripted engine wins

def test_nlu_never_hijacks_the_create_task_wizard(bot, telegram, anthropic):
    say(bot, telegram, "/new")
    assert "Название" in last(telegram)["text"]
    # a message that would be a clear NLU project query anywhere else must be accepted as the
    # literal title while this step-by-step dialog is active
    out = say(bot, telegram, "Какие проекты GEELY в работе?")
    assert "Шаг 2/5" in out[0]["text"], "the wizard must advance to the next step, not answer with a project list"
    say(bot, telegram, "Пропустить")
    say(bot, telegram, "Пропустить")
    say(bot, telegram, "Пропустить")
    out = say(bot, telegram, "Пропустить")
    assert "Какие проекты GEELY в работе?" in out[0]["text"], "the free text became the task's title verbatim"
    say(bot, telegram, "✖️ Отмена")
    assert list_tasks(bot) == []
    assert_owner_only_no_ai(telegram, anthropic)


def test_existing_commands_and_buttons_still_work(bot, telegram, anthropic):
    for cmd in ("/start", "/help", "/menu"):
        out = say(bot, telegram, cmd)
        assert "Доска проектов" in out[0]["text"] or "меню" in out[0]["text"].lower()
    out = say(bot, telegram, "/tasks")
    assert "Активных задач нет" in out[0]["text"] or "Активные задачи" in out[0]["text"]
    out = say(bot, telegram, "/projects")
    assert "Инженерные проекты" in out[0]["text"]
    say(bot, telegram, "/new")
    out = say(bot, telegram, "/cancel")
    assert "отменен" in out[0]["text"].lower()
    assert_owner_only_no_ai(telegram, anthropic)


def test_engineering_chat_and_strangers_never_reach_the_nlu_layer(bot, telegram, anthropic):
    out = say(bot, telegram, "Какие проекты GEELY в работе?", chat=ENG_CHAT, user="555")
    assert out == [] and telegram.sent_messages == []
    out = say(bot, telegram, "Какие проекты GEELY в работе?", chat="777", user="777")
    assert out == [] and telegram.sent_messages == []
    assert anthropic.calls == []
