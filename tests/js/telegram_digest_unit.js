"use strict";
// Pure buildDigestText() checks - critical requirement #2 ("Дополнение к ТЗ Telegram-интеграции
// «Штурмана»"): grouping order, per-line fields, overdue marker, empty-state text, exactly as the
// old Claude Artifact's own morning-digest trigger and this app's own earlier manually-sent digest
// produced them. Run by tests/test_telegram_digest.py.
const assert = require("assert");

process.env.TELEGRAM_BOT_TOKEN = "unused-in-this-unit-test";
const { buildDigestText, QUADRANT_ORDER } = require("../../server/telegram/dailyDigest");

const at = new Date("2026-11-20T04:53:00Z"); // 07:53 Europe/Moscow, a Friday

const results = {};

// header text + the exact Russian weekday/month/year wording, no trailing "г."
{
  const text = buildDigestText([], at);
  assert.strictEqual(text, "Доброе утро! Активные задачи — пятница, 20 ноября 2026\n\nНа сегодня активных задач нет.");
  results.header_and_empty_state = true;
}

// done:true tasks are excluded entirely, even from an otherwise-active quadrant
{
  const text = buildDigestText([{ id: "d1", title: "Завершена", quadrant: "inbox", done: true }], at);
  assert.ok(!text.includes("Завершена"));
  assert.ok(text.includes("На сегодня активных задач нет."));
  results.done_tasks_excluded = true;
}

// grouping order is fixed (inbox, do, plan, delegate, drop) regardless of input order; empty
// quadrants are skipped entirely (no "Планировать:" header with nothing under it)
{
  assert.deepStrictEqual(QUADRANT_ORDER, ["inbox", "do", "plan", "delegate", "drop"]);
  const tasks = [
    { id: "a", title: "Делегированная", quadrant: "delegate", done: false },
    { id: "b", title: "Входящая", quadrant: "inbox", done: false },
    { id: "c", title: "Сделать", quadrant: "do", done: false }
  ];
  const text = buildDigestText(tasks, at);
  const iInbox = text.indexOf("Входящие:");
  const iDo = text.indexOf("Делать сейчас:");
  const iDeleg = text.indexOf("Делегировать:");
  assert.ok(iInbox > -1 && iDo > iInbox && iDeleg > iDo);
  assert.ok(!text.includes("Планировать:") && !text.includes("Отложить/убрать:"));
  results.fixed_order_and_skipped_empty_groups = true;
}

// per-line fields: only whichever of направление/от/ответственный/срок are actually set show up,
// in that order, and a bare title with nothing set renders as just the title
{
  const tasks = [
    { id: "p1", title: "Без доп. полей", quadrant: "inbox", done: false },
    { id: "p2", title: "С направлением и от кого", quadrant: "inbox", done: false, direction: "E-com", from: "Я" }
  ];
  const text = buildDigestText(tasks, at);
  assert.ok(text.includes("1. Без доп. полей\n"), text);
  assert.ok(text.includes("2. С направлением и от кого (направление: E-com, от: Я)"), text);
  results.per_line_fields = true;
}

// overdue marker: only for an unfinished task whose due date is in the past relative to `at`
{
  const tasks = [
    { id: "o1", title: "Просроченная", quadrant: "plan", done: false, due: "2026-11-01" },
    { id: "o2", title: "Будущая", quadrant: "plan", done: false, due: "2026-12-01" },
    { id: "o3", title: "Завершена с прошлым сроком", quadrant: "plan", done: true, due: "2026-01-01" }
  ];
  const text = buildDigestText(tasks, at);
  assert.ok(text.includes("Просроченная (срок: 2026-11-01 — просрочено)"), text);
  assert.ok(text.includes("Будущая (срок: 2026-12-01)") && !text.includes("Будущая (срок: 2026-12-01 — просрочено)"), text);
  assert.ok(!text.includes("Завершена с прошлым сроком"), "done tasks are excluded, overdue or not");
  results.overdue_marker = true;
}

console.log(JSON.stringify(results));
process.exit(0);
