"use strict";
// Pure unit checks of server/telegram/nlu/* (normalization, brand/stage synonyms, relative dates,
// intent classification, long-in-stage search) that don't need a running server. Run by
// tests/test_telegram_assistant.py. Usage: node tests/js/nlu_unit.js <scratch dir>
const path = require("path");
process.env.DATA_DIR = path.join(process.argv[2], "data");
process.env.TELEGRAM_BOT_TIMEZONE = "Asia/Yekaterinburg";

const assert = require("assert");
const failures = [];
function check(name, fn) {
  try { fn(); } catch (e) { failures.push(`${name}: ${e.message}`); }
}

const { looseContains, looseWordMatch } = require("../../server/telegram/nlu/normalize");
const { resolveBrand, extractBrand } = require("../../server/telegram/nlu/brands");
const { extractStage } = require("../../server/telegram/nlu/stages");
const dates = require("../../server/telegram/nlu/dates");
const dateUtils = require("../../server/telegram/dateUtils");
const intents = require("../../server/telegram/nlu/intents");

// ---------------------------------------------------------------- normalize.js
check("surname genitive matches nominative", () => {
  assert.ok(looseWordMatch("Калиткина", "Калиткин"));
  assert.ok(looseWordMatch("фурсы", "Фурса"));
  assert.ok(!looseWordMatch("иванов", "петров"));
});
check("looseContains is order-independent and substring-tolerant", () => {
  assert.ok(looseContains("Защита картера BAW", "картера защита"));
  assert.ok(looseContains("Артикул 333.0544.1", "0544"));
  assert.ok(!looseContains("Защита картера", "радиатор"));
});

// ---------------------------------------------------------------- brands.js
check("brand synonyms resolve to the canonical manufacturer", () => {
  assert.strictEqual(resolveBrand("Джили"), "GEELY");
  assert.strictEqual(resolveBrand("geely"), "GEELY");
  assert.strictEqual(resolveBrand("Хавал"), "HAVAL");
  assert.strictEqual(resolveBrand("бав"), "BAW");
  assert.strictEqual(resolveBrand("нет такой марки"), null);
});
check("extractBrand finds a brand inside a full sentence, punctuation and case included", () => {
  assert.strictEqual(extractBrand("Покажи все проекты GEELY.").canonical, "GEELY");
  assert.strictEqual(extractBrand("Какие проекты Джили сейчас в работе?").canonical, "GEELY");
  assert.strictEqual(extractBrand("Что сейчас разрабатываем для BAW?").canonical, "BAW");
  assert.strictEqual(extractBrand("тут марки нет вообще"), null);
});

// ---------------------------------------------------------------- stages.js
check("extractStage recognises every grammatical form via its root", () => {
  assert.strictEqual(extractStage("Какие проекты HAVAL на замере?"), "measurement");
  assert.strictEqual(extractStage("Что сейчас разрабатываем для BAW?"), "development");
  assert.strictEqual(extractStage("Что сейчас на оформлении?"), "documentation");
  assert.strictEqual(extractStage("сканируем новый картер"), "scanning");
  assert.strictEqual(extractStage("тестовый образец готов"), "prototype");
  assert.strictEqual(extractStage("просто текст без этапа"), null);
});

// ---------------------------------------------------------------- dates.js / dateUtils.js
check("relative dates resolve in the bot's timezone", () => {
  const today = dateUtils.localToday();
  assert.strictEqual(dates.extractRelativeDate("сегодня"), today);
  assert.strictEqual(dates.extractRelativeDate("завтра"), dateUtils.addDays(today, 1));
  assert.strictEqual(dates.extractRelativeDate("послезавтра"), dateUtils.addDays(today, 2));
  const fri = dates.extractRelativeDate("перенеси задачу TIREI на пятницу");
  assert.ok(fri && dateUtils.weekdayIndex(fri) === 4, "пятница must resolve to a Friday");
  assert.ok(fri >= today, "a bare weekday never resolves into the past");
});
check("absolute dates parse DD.MM[.YY[YY]] and roll over the year when already past", () => {
  assert.strictEqual(dates.extractAbsoluteDate("2026-12-25"), "2026-12-25");
  assert.strictEqual(dates.extractAbsoluteDate("перенеси на 25.12.2026"), "2026-12-25");
});
check("week range is Monday..Sunday", () => {
  const { start, end } = dateUtils.weekRange("2026-11-25"); // a Wednesday
  assert.strictEqual(dateUtils.weekdayIndex(start), 0);
  assert.strictEqual(dateUtils.weekdayIndex(end), 6);
  assert.ok(start <= "2026-11-25" && "2026-11-25" <= end);
});

// ---------------------------------------------------------------- intents.js
check("project query examples classify as list_projects with the right entities", () => {
  assert.deepStrictEqual(intents.classify("Какие проекты Джили сейчас в работе?"),
    { intent: "list_projects", brand: "GEELY", stage: null, longInStage: false, responsible: null, freeText: null });
  assert.deepStrictEqual(intents.classify("Какие проекты HAVAL на замере?"),
    { intent: "list_projects", brand: "HAVAL", stage: "measurement", longInStage: false, responsible: null, freeText: null });
  assert.strictEqual(intents.classify("Что сейчас на оформлении?").stage, "documentation");
  assert.strictEqual(intents.classify("Покажи проекты, которые долго находятся на стадии.").longInStage, true);
  assert.strictEqual(intents.classify("Какие проекты у Калиткина?").responsible, "калиткина");
});
check("task query examples classify as list_tasks with the right filter", () => {
  assert.deepStrictEqual(intents.classify("Какие мои задачи просрочены?"), { intent: "list_tasks", filter: "overdue" });
  assert.strictEqual(intents.classify("Что нужно сделать сегодня?").filter, "today");
  assert.strictEqual(intents.classify("Какие задачи на этой неделе?").filter, "week");
  const r = intents.classify("Покажи задачи Фурсы.");
  assert.strictEqual(r.filter, "responsible");
  assert.strictEqual(r.responsible, "Фурсы");
});
check("task mutation examples classify with title/target/due extracted, nothing invented", () => {
  const create = intents.classify("Создай задачу проверить крепления защиты BAW.");
  assert.strictEqual(create.intent, "create_task");
  assert.strictEqual(create.title, "проверить крепления защиты BAW.");
  assert.strictEqual(create.due, null, "no date was said, so none may be invented");
  const move = intents.classify("Перенеси задачу TIREI на пятницу.");
  assert.strictEqual(move.intent, "move_task");
  assert.strictEqual(move.targetQuery, "TIREI");
  assert.ok(move.due);
  const done = intents.classify("Отметь задачу по HAVAL выполненной.");
  assert.strictEqual(done.intent, "complete_task");
  assert.strictEqual(done.targetQuery, "HAVAL");
});
check("an ordinary task title (no trigger word) is not claimed by the NLU layer at all", () => {
  assert.strictEqual(intents.classify("заказать крепёж для стенда"), null);
  assert.strictEqual(intents.classify("Подготовить КД на защиту картера"), null);
});
check("project mutation through free text is recognised only to be refused, never attempted", () => {
  assert.deepStrictEqual(intents.classify("Перенеси проект HAVAL на этап оформления"), { intent: "project_mutation_blocked" });
});

// ---------------------------------------------------------- projectsQuery.js "long in stage"
check("projects stuck in their stage past the threshold are found, never guessed", () => {
  const store = require("../../server/store");
  const projectsQuery = require("../../server/telegram/nlu/projectsQuery");
  const longAgo = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000).toISOString();
  const recently = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString();
  store.tx((s) => {
    s.projects.push({ id: "stuck-1", external_project_id: null, version: 1, needs_review: false,
      created_at: longAgo, updated_at: longAgo, record_category: "main_project", stage_entered_at: longAgo,
      field_sources: {}, last_plan_month: null,
      data: { manufacturer: "TESTCO", vehicle_model: "X1", product_type: "Защита", status: "scanning", project_name: "TESTCO X1" } });
    s.projects.push({ id: "fresh-1", external_project_id: null, version: 1, needs_review: false,
      created_at: recently, updated_at: recently, record_category: "main_project", stage_entered_at: recently,
      field_sources: {}, last_plan_month: null,
      data: { manufacturer: "TESTCO", vehicle_model: "X2", product_type: "Защита", status: "scanning", project_name: "TESTCO X2" } });
  });
  const stuck = projectsQuery.longInStage();
  assert.ok(stuck.some((p) => p.id === "stuck-1"), "the long-stuck project must be found");
  assert.ok(!stuck.some((p) => p.id === "fresh-1"), "a recently-entered stage must not be reported as stuck");
});

if (failures.length) {
  console.error("FAILURES:\n" + failures.join("\n"));
  process.exit(1);
}
console.log("ok");
