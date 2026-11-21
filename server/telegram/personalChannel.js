"use strict";

// Channel 1: the personal Telegram bot - dialogue-driven management of the personal Kanban board
// (server/docStore.js "tasks" collection, the same one the dashboard's Доска/Глобально tabs use)
// and, when the user explicitly names one, an engineering project (server/projectsModel.js).
//
// Design rule (ТЗ §6.4/§6.8/§11): the AI classifies intent and extracts fields only - it never
// states a fact the server hasn't verified, and it never gets to execute a change by itself. Every
// reply with real numbers/names is built here from the actual database; every ambiguous match
// (which task? which project? which stage?) asks the user instead of guessing.

const { createDocStore } = require("../docStore");
const store = require("../store");
const projectsModel = require("../projectsModel");
const aiClient = require("./aiClient");
const botApi = require("./botApi");
const telegramLog = require("./log");

const tasksStore = createDocStore("tasks");

const TOOL_SCHEMA = {
  type: "object",
  properties: {
    intent: { type: "string", enum: ["create_task", "list_tasks", "update_task", "close_task", "reopen_task",
      "search", "project_info", "change_project_stage", "clarify", "none"] },
    entity: { type: "string", enum: ["personal_task", "engineering_project"] },
    title: { type: ["string", "null"] },
    direction: { type: ["string", "null"] },
    responsible: { type: ["string", "null"] },
    from: { type: ["string", "null"] },
    due: { type: ["string", "null"], description: "ISO 8601 date or datetime if a deadline was stated, else null" },
    target_query: { type: ["string", "null"], description: "free text identifying which existing task or project is meant" },
    manufacturer: { type: ["string", "null"] },
    vehicle_model: { type: ["string", "null"] },
    product_type: { type: ["string", "null"] },
    article_number: { type: ["string", "null"] },
    stage_hint: { type: ["string", "null"], description: "the stage name the user said, verbatim, for the server to match" },
    filter: { type: ["string", "null"], enum: ["active", "overdue", "done", "all", null] },
    search_query: { type: ["string", "null"] },
    confidence: { type: "string", enum: ["high", "low"] },
    clarification_question: { type: ["string", "null"] }
  },
  required: ["intent", "entity", "confidence"]
};

const SYSTEM = "Ты классифицируешь сообщения личного Telegram-бота системы «Штурман» (управление личными " +
  "задачами и инженерными проектами). Извлекай только то, что явно сказано в сообщении - никогда не " +
  "придумывай сотрудников, сроки, артикулы или факты о проекте. Если неясно, какая именно задача или проект " +
  "имеется в виду, или команда может навредить данным при ошибке - ставь confidence:\"low\" и формулируй " +
  "clarification_question. intent:\"none\" - если это не похоже ни на команду, ни на вопрос.";

function fmtDate(d) {
  return new Date(d).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
}

function overdue(task) {
  return task.due && !task.done && new Date(task.due).getTime() < Date.now();
}

function taskLine(t) {
  const bits = [t.title];
  const meta = [];
  if (t.direction) meta.push(t.direction);
  if (t.responsible) meta.push(t.responsible);
  if (t.due) meta.push((overdue(t) ? "просрочено " : "срок ") + fmtDate(t.due));
  if (meta.length) bits.push(`(${meta.join(", ")})`);
  return bits.join(" ");
}

function findTaskCandidates(query) {
  if (!query) return [];
  const q = query.toLowerCase();
  return tasksStore.list().filter((t) => !t.done && t.title && t.title.toLowerCase().includes(q));
}

function findProjectCandidates({ target_query, manufacturer, vehicle_model, product_type, article_number }) {
  if (manufacturer || vehicle_model || product_type || article_number) {
    const { matches } = projectsModel.matchByIdentifiers({ manufacturer, vehicle_model, product_type, article_number });
    if (matches.length) return matches;
  }
  if (!target_query) return [];
  const q = target_query.toLowerCase();
  return projectsModel.allRows().filter((r) => {
    const d = r.data || {};
    return [d.project_name, d.manufacturer, d.vehicle_model, d.product_type, d.article_number]
      .filter(Boolean).some((v) => String(v).toLowerCase().includes(q));
  });
}

function matchStage(hint) {
  if (!hint) return null;
  const q = hint.toLowerCase();
  const stages = store.read((s) => s.stages);
  const hit = stages.filter((s) => s.key.toLowerCase().includes(q) || s.label.toLowerCase().includes(q));
  return hit.length === 1 ? hit[0].key : null;
}

async function reply(chatId, text) {
  try {
    await botApi.sendMessage(chatId, text);
  } catch (e) { /* best-effort; the outcome is still logged either way */ }
}

// Критическое требование ("не обрабатывать /start и /help через ИИ"): a handful of commands need
// no natural-language interpretation at all - no field to extract, nothing ambiguous - so they are
// recognised and answered here, before aiClient.classify() is ever called, and keep working
// exactly the same whether the AI is configured, reachable, or completely unavailable (no
// Anthropic credit, network down, etc.). Everything else in this channel's design is genuinely
// NL-driven (create/close/update a task by free text) and has no non-AI equivalent - that was the
// actual audit result for "which commands can run without AI" (ТЗ "Доработка Telegram: /start без
// ИИ", п.2): only a command with nothing left to interpret qualifies. /tasks (alias /list) is the
// one task-management command that qualifies, since listing needs no extraction - it reuses the
// exact same formatting as the AI-driven list_tasks intent (listTasks() below).
const HELP_TEXT = "Привет! Я бот «Штурман» — помогаю вести личные задачи и подсказываю по инженерным проектам.\n\n" +
  "Просто напишите, что нужно сделать, например:\n" +
  "«Проверить крепление защиты картера, срок завтра»\n" +
  "«Покажи активные задачи»\n" +
  "«Закрой задачу про крепление»\n\n" +
  "Эти команды работают без ИИ, даже если интеллектуальная обработка временно недоступна:\n" +
  "/tasks — список активных задач\n" +
  "/help — эта справка";

/** The command word of a Telegram "/command" message (lowercase, without "@botname" or arguments),
 *  or null if `text` isn't a command. */
function commandName(text) {
  const m = text.match(/^\/([a-zA-Z_]+)(?:@\S+)?(?:\s|$)/);
  return m ? m[1].toLowerCase() : null;
}

async function listTasks(filter, base, chatId) {
  let list = tasksStore.list();
  if (filter === "active") list = list.filter((t) => !t.done);
  else if (filter === "overdue") list = list.filter(overdue);
  else if (filter === "done") list = list.filter((t) => t.done);
  telegramLog.record(Object.assign({}, base, { outcome: "listed", detail: `${filter}:${list.length}` }));
  if (!list.length) { await reply(chatId, "Таких задач нет."); return; }
  await reply(chatId, list.slice(0, 40).map((t, i) => `${i + 1}. ${taskLine(t)}`).join("\n"));
}

async function handle(message, ctx) {
  const chatId = ctx.chatId;
  const text = (message.text || "").trim();
  const base = { channel: "personal", chat_id: chatId, user_id: ctx.userId, update_id: ctx.updateId, message_text: text };
  if (!text) {
    telegramLog.record(Object.assign({}, base, { outcome: "ignored", detail: "no text" }));
    return;
  }

  const cmd = commandName(text);
  if (cmd === "start" || cmd === "help") {
    telegramLog.record(Object.assign({}, base, { outcome: "replied", detail: `/${cmd}` }));
    await reply(chatId, HELP_TEXT);
    return;
  }
  if (cmd === "tasks" || cmd === "list") {
    await listTasks("active", base, chatId);
    return;
  }

  const activeTasks = tasksStore.list().filter((t) => !t.done);
  let action;
  try {
    action = await aiClient.classify({
      system: SYSTEM,
      prompt: `Сегодня: ${new Date().toISOString().slice(0, 10)}.\nАктивные задачи (для сопоставления, не для копирования): ` +
        JSON.stringify(activeTasks.slice(0, 50).map((t) => ({ title: t.title, direction: t.direction, responsible: t.responsible }))) +
        `\n\nСообщение пользователя: ${JSON.stringify(text)}`,
      toolName: "personal_action", toolSchema: TOOL_SCHEMA
    });
  } catch (e) {
    // Критическое требование: never show the user an Anthropic error or any other internal
    // detail - a plain, friendly "temporarily unavailable" message, whatever the underlying cause
    // (missing credit, network, malformed response - aiClient.classify()'s e.code, logged below
    // for the admin, is never echoed to Telegram). The message itself is never lost: queued in the
    // review queue (telegram_pending) for a person to handle, same as any other unmatched record.
    const { upsert } = require("../reviewQueueModel");
    upsert({ kind: "telegram_pending", payload: base, reason: `ИИ недоступен: ${e.message}`, source: "telegram" });
    telegramLog.record(Object.assign({}, base, { outcome: "queued", detail: e.message }));
    await reply(chatId, "Не удалось обработать сообщение прямо сейчас (ИИ временно недоступен). Сохранил его, обработаю позже.");
    return;
  }

  try {
    await execute(action, base, chatId);
  } catch (e) {
    // Same rule as above: whatever this error actually is (a version conflict, a data error -
    // nothing to do with the AI), the user only ever sees a generic message; the real one is only
    // in telegramLog, for the dashboard.
    telegramLog.record(Object.assign({}, base, { outcome: "error", error: e.message }));
    await reply(chatId, "Не получилось выполнить команду. Попробуйте переформулировать или повторить позже.");
  }
}

async function execute(action, base, chatId) {
  if (action.confidence === "low" || action.intent === "clarify") {
    telegramLog.record(Object.assign({}, base, { outcome: "replied", detail: "clarification requested" }));
    await reply(chatId, action.clarification_question || "Уточните, пожалуйста, что именно нужно сделать.");
    return;
  }

  if (action.intent === "none") {
    telegramLog.record(Object.assign({}, base, { outcome: "ignored" }));
    return;
  }

  if (action.intent === "create_task") {
    if (action.entity === "engineering_project") {
      telegramLog.record(Object.assign({}, base, { outcome: "replied", detail: "project creation not supported via chat" }));
      await reply(chatId, "Создание инженерных проектов через чат не поддерживается — используйте раздел «Инж. проекты».");
      return;
    }
    const created = tasksStore.create({ title: action.title, quadrant: "inbox", direction: action.direction || null,
      responsible: action.responsible || null, from: action.from || null, due: action.due || null, done: false,
      calendarSynced: false, telegramNotified: true, source: "telegram" });
    telegramLog.record(Object.assign({}, base, { outcome: "created", detail: created.id }));
    await reply(chatId, `Готово — создал задачу «${created.title}».`);
    return;
  }

  if (action.intent === "list_tasks") {
    await listTasks(action.filter || "active", base, chatId);
    return;
  }

  if (action.intent === "search") {
    const q = (action.search_query || action.target_query || "").toLowerCase();
    const list = tasksStore.list().filter((t) => t.title && t.title.toLowerCase().includes(q));
    telegramLog.record(Object.assign({}, base, { outcome: "listed", detail: `search:${list.length}` }));
    if (!list.length) { await reply(chatId, "Ничего не нашёл."); return; }
    await reply(chatId, list.slice(0, 20).map((t, i) => `${i + 1}. ${taskLine(t)}`).join("\n"));
    return;
  }

  if (action.intent === "close_task" || action.intent === "reopen_task" || action.intent === "update_task") {
    const candidates = findTaskCandidates(action.target_query);
    if (candidates.length !== 1) {
      telegramLog.record(Object.assign({}, base, { outcome: "replied", detail: `ambiguous: ${candidates.length}` }));
      await reply(chatId, candidates.length
        ? `Нашёл несколько задач: ${candidates.map((t) => t.title).join("; ")}. Уточните, какую.`
        : "Не нашёл такую задачу. Уточните название.");
      return;
    }
    const t = candidates[0];
    if (action.intent === "close_task") {
      tasksStore.update(t.id, { version: t.version, done: true });
      telegramLog.record(Object.assign({}, base, { outcome: "updated", detail: `${t.id}:done` }));
      await reply(chatId, `Готово — закрыл «${t.title}».`);
    } else if (action.intent === "reopen_task") {
      tasksStore.update(t.id, { version: t.version, done: false });
      telegramLog.record(Object.assign({}, base, { outcome: "updated", detail: `${t.id}:reopened` }));
      await reply(chatId, `Готово — снова открыл «${t.title}».`);
    } else {
      const patch = { version: t.version };
      if (action.responsible) patch.responsible = action.responsible;
      if (action.due) patch.due = action.due;
      if (action.direction) patch.direction = action.direction;
      if (Object.keys(patch).length === 1) {
        await reply(chatId, "Не понял, что именно изменить.");
        return;
      }
      tasksStore.update(t.id, patch);
      telegramLog.record(Object.assign({}, base, { outcome: "updated", detail: `${t.id}:${Object.keys(patch).join(",")}` }));
      await reply(chatId, `Готово — обновил «${t.title}».`);
    }
    return;
  }

  if (action.intent === "project_info" || action.intent === "change_project_stage") {
    const candidates = findProjectCandidates(action);
    if (candidates.length !== 1) {
      telegramLog.record(Object.assign({}, base, { outcome: "replied", detail: `ambiguous project: ${candidates.length}` }));
      await reply(chatId, candidates.length
        ? `Нашёл несколько проектов: ${candidates.map((r) => (r.data || {}).project_name || r.external_project_id).join("; ")}. Уточните, какой.`
        : "Не нашёл такой проект. Уточните марку/модель/изделие или артикул.");
      return;
    }
    const row = candidates[0];
    if (action.intent === "project_info") {
      const p = projectsModel.getProject(row.id);
      telegramLog.record(Object.assign({}, base, { outcome: "replied", detail: `project_info:${row.id}` }));
      await reply(chatId, `${p.project_name || ""}\nСтадия: ${p.status || "не задана"}\nПриоритет: ${p.priority || "—"}\n` +
        `Инженер: ${p.assigned_engineer || "—"}`);
      return;
    }
    const stageKey = matchStage(action.stage_hint);
    if (!stageKey) {
      telegramLog.record(Object.assign({}, base, { outcome: "replied", detail: "stage not matched" }));
      await reply(chatId, "Не понял, на какую стадию переносить. Уточните название стадии.");
      return;
    }
    const r = projectsModel.moveStage(row.id, stageKey, { source: "telegram", actor: `tg:${base.user_id}`,
      note: "изменено через личного Telegram-бота" });
    if (r.error) {
      telegramLog.record(Object.assign({}, base, { outcome: "error", error: JSON.stringify(r) }));
      await reply(chatId, "Не получилось изменить стадию.");
      return;
    }
    telegramLog.record(Object.assign({}, base, { outcome: "updated", detail: `${row.id}:stage=${stageKey}` }));
    await reply(chatId, r.unchanged ? "Проект уже на этой стадии." : `Готово — перенёс проект на стадию «${stageKey}».`);
    return;
  }

  telegramLog.record(Object.assign({}, base, { outcome: "ignored", detail: `unhandled intent ${action.intent}` }));
}

module.exports = { handle };
