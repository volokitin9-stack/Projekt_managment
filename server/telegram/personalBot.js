"use strict";

// Personal Telegram bot of «Доска проектов»: menu + step-by-step dialogs, no AI at all.
//
// Works on the existing task board (server/docStore.js "tasks" - the same collection the
// dashboard's Доска tab uses) and reads engineering projects (server/projectsModel.js) without
// changing them. Dialog state lives in the same data file (store.js object `telegram_dialogs`) -
// no parallel storage.
//
// Buttons are a Telegram *reply keyboard*: pressing one sends its label as an ordinary message, so
// this works with the existing webhook (allowed_updates: ["message"]) - inline buttons would need
// callback_query updates and a webhook change, which is out of scope.
//
// Idempotency: the webhook already ignores a repeated update_id (telegram/log.js#claimUpdate). On
// top of that, a task created by the bot gets the id `tg-<chat>-<message_id>` of the confirming
// message, so even the same message delivered under a new update_id cannot create a second task.

const { createDocStore } = require("../docStore");
const store = require("../store");
const projectsModel = require("../projectsModel");
const botApi = require("./botApi");
const telegramLog = require("./log");

const tasks = createDocStore("tasks");
const BRAND = "Доска проектов";
const TZ = process.env.TELEGRAM_BOT_TIMEZONE || "Asia/Yekaterinburg";
const DIALOG_TTL_MS = 2 * 60 * 60 * 1000;
const MAX_TEXT = 3800;

const B = {
  tasks: "📋 Задачи", create: "➕ Новая задача", projects: "🛠 Проекты", help: "❓ Помощь", menu: "⬅️ Меню",
  cancel: "✖️ Отмена", skip: "Пропустить", clear: "Очистить", back: "⬅️ Назад", me: "Я",
  confirm: "✅ Создать", yes: "✅ Да, создать", no: "✖️ Нет",
  done: "✅ Выполнено", reopen: "↩️ Вернуть в работу", status: "🔁 Статус", edit: "✏️ Изменить",
  search: "🔎 Найти проект"
};
const QUADRANTS = [["inbox", "Распредели"], ["do", "Срочно"], ["plan", "Мои задачи"], ["delegate", "Отдай другому"], ["drop", "Отложить"]];
const QLABEL = Object.fromEntries(QUADRANTS);
const PRIORITIES = [["high", "Высокий"], ["normal", "Обычный"], ["low", "Низкий"]];
const PLABEL = Object.fromEntries(PRIORITIES);
const EDIT_FIELDS = [["title", "Название"], ["description", "Описание"], ["due", "Срок"], ["priority", "Приоритет"], ["responsible", "Ответственный"]];
const STAGE_LABELS = { scanning: "Сканирование", development: "Разработка", prototype: "Тестовый образец",
  measurement: "Замер", documentation: "Оформление" };

const kb = (rows) => ({ reply_markup: { keyboard: rows.map((r) => r.map((text) => ({ text }))), resize_keyboard: true, is_persistent: true } });
const MAIN_KB = kb([[B.tasks, B.create], [B.projects, B.help]]);

const HELP_TEXT = `Привет! Я бот «${BRAND}» — веду ваши задачи и показываю инженерные проекты.\n\n` +
  "Команды:\n" +
  "/tasks — активные задачи\n" +
  "/new — новая задача (пошагово)\n" +
  "/projects — инженерные проекты (только просмотр)\n" +
  "/menu — главное меню\n" +
  "/cancel — отменить текущий ввод\n" +
  "/help — эта справка\n\n" +
  "Нажмите код вида /t_1a2b3c4d в списке, чтобы открыть карточку задачи: там можно отметить выполнение, " +
  "сменить статус или отредактировать поля. Можно просто написать текст — я предложу создать из него задачу.";

// ---------------------------------------------------------------- helpers

function nowIso() {
  return new Date().toISOString();
}

function localToday() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}

function addDays(ymd, n) {
  const d = new Date(`${ymd}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function validYmd(y, m, d) {
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/** "сегодня|завтра|послезавтра|ДД.ММ[.ГГ[ГГ]]|ГГГГ-ММ-ДД" + optional "ЧЧ:ММ" -> "YYYY-MM-DD[THH:MM]" | null.
 *  Same naive local format the dashboard's datetime-local field stores. */
function parseDue(input) {
  const s = String(input || "").trim().toLowerCase().replace(/\s+/g, " ");
  const tm = s.match(/(?:^|\s)(?:в\s)?(\d{1,2})[:.](\d{2})$/);
  let rest = s;
  let time = null;
  if (tm && !/^\d{1,2}\.\d{1,2}$/.test(s)) {
    const h = Number(tm[1]);
    const mi = Number(tm[2]);
    if (h > 23 || mi > 59) return null;
    time = `${String(h).padStart(2, "0")}:${String(mi).padStart(2, "0")}`;
    rest = s.slice(0, tm.index).trim();
  }
  const today = localToday();
  let ymd = null;
  if (rest === "сегодня") ymd = today;
  else if (rest === "завтра") ymd = addDays(today, 1);
  else if (rest === "послезавтра") ymd = addDays(today, 2);
  else {
    let m = rest.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (m) {
      if (validYmd(+m[1], +m[2], +m[3])) ymd = `${m[1]}-${m[2]}-${m[3]}`;
    } else if ((m = rest.match(/^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?$/))) {
      const d = +m[1];
      const mo = +m[2];
      let y = m[3] ? +m[3] : +today.slice(0, 4);
      if (m[3] && m[3].length === 2) y += 2000;
      if (validYmd(y, mo, d)) {
        ymd = `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
        if (!m[3] && ymd < today) ymd = `${y + 1}${ymd.slice(4)}`;   // "05.01" in December -> next year
      }
    }
  }
  if (!ymd) return null;
  return time ? `${ymd}T${time}` : ymd;
}

function fmtDue(due) {
  if (!due) return null;
  const [d, t] = String(due).split("T");
  const [y, m, dd] = d.split("-");
  if (!dd) return String(due);
  return `${dd}.${m}.${y}${t ? " " + t.slice(0, 5) : ""}`;
}

function isOverdue(t) {
  if (!t.due || t.done) return false;
  const due = String(t.due);
  const today = localToday();
  return due.length <= 10 ? due < today : new Date(due).getTime() < Date.now();
}

function codeOf(id) {
  return String(id).toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 8);
}

function resolveByCode(list, code) {
  const c = String(code).toLowerCase();
  return list.filter((x) => String(x.id).toLowerCase().replace(/[^a-z0-9]/g, "").startsWith(c));
}

function chunks(text) {
  const out = [];
  let cur = "";
  String(text).split("\n").forEach((line) => {
    if ((cur + "\n" + line).length > MAX_TEXT && cur) { out.push(cur); cur = line; } else cur = cur ? `${cur}\n${line}` : line;
  });
  if (cur) out.push(cur);
  return out.length ? out : [""];
}

async function send(chatId, text, extra = MAIN_KB) {
  const parts = chunks(text);
  for (let i = 0; i < parts.length; i += 1) {
    try {
      await botApi.sendMessage(chatId, parts[i], i === parts.length - 1 ? extra : {});
    } catch (e) { /* best-effort; the outcome is logged by the caller */ }
  }
}

// ---------------------------------------------------------------- dialog state (store.js)

function getDialog(chatId) {
  const d = store.read((s) => (s.telegram_dialogs || {})[String(chatId)] || null);
  if (!d || Date.now() - Date.parse(d.updated_at || 0) > DIALOG_TTL_MS) return { mode: "idle" };
  return JSON.parse(JSON.stringify(d));
}

function setDialog(chatId, d) {
  store.tx((s) => {
    if (!s.telegram_dialogs || typeof s.telegram_dialogs !== "object") s.telegram_dialogs = {};
    s.telegram_dialogs[String(chatId)] = Object.assign({}, d, { updated_at: nowIso() });
  });
}

function resetDialog(chatId, keep = {}) {
  setDialog(chatId, Object.assign({ mode: "idle" }, keep));
}

// ---------------------------------------------------------------- tasks

function taskLine(t, i) {
  const meta = [];
  if (t.priority === "high") meta.push("❗");
  if (t.due) meta.push((isOverdue(t) ? "просрочено " : "до ") + fmtDue(t.due));
  if (t.responsible) meta.push(t.responsible);
  return `${i + 1}. ${t.title || "(без названия)"}${meta.length ? " · " + meta.join(" · ") : ""}  /t_${codeOf(t.id)}`;
}

function activeTasks() {
  const order = { high: 0, normal: 1, low: 2 };
  return tasks.list().filter((t) => !t.done).sort((a, b) =>
    (isOverdue(b) - isOverdue(a)) ||
    ((order[a.priority] ?? 1) - (order[b.priority] ?? 1)) ||
    String(a.due || "9999").localeCompare(String(b.due || "9999")));
}

function taskCard(t) {
  const subs = Array.isArray(t.subtasks) ? t.subtasks : [];
  const lines = [`📌 ${t.title || "(без названия)"}`,
    `Статус: ${t.done ? "✅ выполнена" : QLABEL[t.quadrant] || t.quadrant || "—"}`];
  if (t.priority) lines.push(`Приоритет: ${PLABEL[t.priority] || t.priority}`);
  if (t.due) lines.push(`Срок: ${fmtDue(t.due)}${isOverdue(t) ? " — просрочено" : ""}`);
  if (t.responsible) lines.push(`Ответственный: ${t.responsible}`);
  if (t.from) lines.push(`От кого: ${t.from}`);
  if (t.direction) lines.push(`Направление: ${t.direction}`);
  if (t.description) lines.push(`Описание: ${t.description}`);
  if (subs.length) lines.push(`Подзадачи: ${subs.filter((x) => x.done).length}/${subs.length} выполнено`);
  lines.push(`Код: /t_${codeOf(t.id)}`);
  return lines.join("\n");
}

function cardKb(t) {
  return kb([[t.done ? B.reopen : B.done, B.status], [B.edit, B.menu]]);
}

async function showTasks(chatId, log) {
  const list = activeTasks();
  log("listed", `tasks:${list.length}`);
  setDialog(chatId, { mode: "list", list: list.slice(0, 40).map((t) => t.id) });
  if (!list.length) return send(chatId, "Активных задач нет. Нажмите «➕ Новая задача», чтобы добавить.");
  const head = `Активные задачи (${list.length})${list.length > 40 ? ", показаны первые 40" : ""}:`;
  return send(chatId, `${head}\n${list.slice(0, 40).map(taskLine).join("\n")}\n\nНажмите код или пришлите номер, чтобы открыть карточку.`);
}

async function openTask(chatId, t, log, prefix = "") {
  setDialog(chatId, { mode: "task", task_id: t.id });
  log("replied", `card:${t.id}`);
  return send(chatId, (prefix ? prefix + "\n\n" : "") + taskCard(t), cardKb(t));
}

/** Apply a patch with the version just read; on a concurrent edit, report it and show the fresh card. */
async function patchTask(chatId, id, patch, okText, log) {
  const t = tasks.get(id);
  if (!t) { resetDialog(chatId); log("replied", `task gone:${id}`); return send(chatId, "Задача не найдена — возможно, её удалили на доске."); }
  const r = tasks.update(id, Object.assign({ version: t.version }, patch));
  if (r.error) {
    log("error", `update ${id}: ${r.error}`);
    const fresh = tasks.get(id);
    return fresh ? openTask(chatId, fresh, log, "Задача только что изменилась на доске — изменение не применено, вот актуальная карточка.")
      : send(chatId, "Задача не найдена.");
  }
  log("updated", `${id}:${Object.keys(patch).join(",")}`);
  return openTask(chatId, r.doc, log, `Готово — ${okText}.`);
}

// ---------------------------------------------------------------- create flow

const CREATE_STEPS = ["title", "description", "due", "priority", "responsible", "confirm"];

function createPrompt(step, draft) {
  if (step === "title") return ["Шаг 1/5. Название задачи?", kb([[B.cancel]])];
  if (step === "description") return ["Шаг 2/5. Описание (или «Пропустить»).", kb([[B.skip], [B.cancel]])];
  if (step === "due") return ["Шаг 3/5. Срок: «сегодня», «завтра», 25.12, 25.12.2026 или 2026-12-25, можно со временем «завтра 10:00». Или «Пропустить».",
    kb([["сегодня", "завтра"], [B.skip], [B.cancel]])];
  if (step === "priority") return ["Шаг 4/5. Приоритет?", kb([PRIORITIES.map((p) => p[1]), [B.skip], [B.cancel]])];
  if (step === "responsible") return ["Шаг 5/5. Ответственный? Напишите имя, «Я» или «Пропустить».", kb([[B.me, B.skip], [B.cancel]])];
  const lines = ["Проверьте задачу:", `Название: ${draft.title}`];
  if (draft.description) lines.push(`Описание: ${draft.description}`);
  lines.push(`Срок: ${draft.due ? fmtDue(draft.due) : "—"}`, `Приоритет: ${draft.priority ? PLABEL[draft.priority] : "—"}`,
    `Ответственный: ${draft.responsible || "—"}`, "Статус: Распредели");
  return [lines.join("\n"), kb([[B.confirm], [B.cancel]])];
}

async function askStep(chatId, d) {
  setDialog(chatId, d);
  const [text, k] = createPrompt(d.step, d.draft);
  return send(chatId, text, k);
}

async function createStep(chatId, d, text, message, log) {
  const draft = d.draft || {};
  const skip = text === B.skip;
  const next = (step) => askStep(chatId, { mode: "create", step, draft });
  switch (d.step) {
    case "title":
      if (!text || text.length > 300) return send(chatId, "Название не может быть пустым или длиннее 300 символов. Попробуйте ещё раз.", kb([[B.cancel]]));
      draft.title = text;
      return next("description");
    case "description":
      if (!skip) {
        if (text.length > 2000) return send(chatId, "Описание длиннее 2000 символов — сократите, пожалуйста.", kb([[B.skip], [B.cancel]]));
        draft.description = text;
      }
      return next("due");
    case "due":
      if (!skip) {
        const due = parseDue(text);
        if (!due) return send(chatId, "Не понял дату. Примеры: «завтра», «25.12», «25.12.2026 10:00», «2026-12-25». Или «Пропустить».",
          kb([["сегодня", "завтра"], [B.skip], [B.cancel]]));
        draft.due = due;
      }
      return next("priority");
    case "priority": {
      if (!skip) {
        const p = PRIORITIES.find((x) => x[1].toLowerCase() === text.toLowerCase());
        if (!p) return send(chatId, "Выберите приоритет кнопкой.", kb([PRIORITIES.map((x) => x[1]), [B.skip], [B.cancel]]));
        draft.priority = p[0];
      }
      return next("responsible");
    }
    case "responsible":
      if (!skip) {
        if (text.length > 200) return send(chatId, "Слишком длинно — до 200 символов.", kb([[B.me, B.skip], [B.cancel]]));
        draft.responsible = text;
      }
      return next("confirm");
    case "confirm": {
      if (text !== B.confirm) return send(chatId, "Нажмите «✅ Создать» или «✖️ Отмена».", kb([[B.confirm], [B.cancel]]));
      const id = `tg-${String(message.chat.id).replace(/[^0-9]/g, "")}-${message.message_id}`;
      let created = tasks.get(id);
      const duplicate = !!created;
      if (!created) {
        created = tasks.create({ id, title: draft.title, description: draft.description || null, quadrant: "inbox",
          direction: null, responsible: draft.responsible || null, from: null, due: draft.due || null,
          priority: draft.priority || null, done: false, calendarSynced: false, telegramNotified: true,
          source: "telegram", createdAt: nowIso() });
      }
      log(duplicate ? "replied" : "created", `${duplicate ? "duplicate " : ""}${created.id}`);
      return openTask(chatId, created, log, duplicate ? "Эта задача уже создана." : `Готово — задача «${created.title}» создана на доске «${BRAND}».`);
    }
    default:
      resetDialog(chatId);
      return send(chatId, "Начнём сначала.", MAIN_KB);
  }
}

// ---------------------------------------------------------------- edit / status

function editPrompt(field) {
  if (field === "title") return ["Новое название?", kb([[B.back]])];
  if (field === "description") return ["Новое описание? («Очистить» — удалить)", kb([[B.clear], [B.back]])];
  if (field === "due") return ["Новый срок? (завтра, 25.12, 2026-12-25 10:00; «Очистить» — убрать срок)", kb([["сегодня", "завтра"], [B.clear], [B.back]])];
  if (field === "priority") return ["Новый приоритет?", kb([PRIORITIES.map((p) => p[1]), [B.clear], [B.back]])];
  return ["Новый ответственный? («Я», имя или «Очистить»)", kb([[B.me, B.clear], [B.back]])];
}

async function editValue(chatId, d, text, log) {
  const field = d.field;
  if (text === B.back) {
    const t = tasks.get(d.task_id);
    return t ? openTask(chatId, t, log) : showTasks(chatId, log);
  }
  let value;
  if (text === B.clear && field !== "title") value = null;
  else if (field === "title") {
    if (!text || text.length > 300) return send(chatId, "Название: от 1 до 300 символов.", kb([[B.back]]));
    value = text;
  } else if (field === "description") {
    if (text.length > 2000) return send(chatId, "До 2000 символов.", kb([[B.clear], [B.back]]));
    value = text;
  } else if (field === "due") {
    value = parseDue(text);
    if (!value) return send(chatId, "Не понял дату. Примеры: «завтра», «25.12», «2026-12-25 10:00».", kb([["сегодня", "завтра"], [B.clear], [B.back]]));
  } else if (field === "priority") {
    const p = PRIORITIES.find((x) => x[1].toLowerCase() === text.toLowerCase());
    if (!p) return send(chatId, "Выберите приоритет кнопкой.", kb([PRIORITIES.map((x) => x[1]), [B.clear], [B.back]]));
    value = p[0];
  } else {
    if (text.length > 200) return send(chatId, "До 200 символов.", kb([[B.me, B.clear], [B.back]]));
    value = text;
  }
  const label = EDIT_FIELDS.find((f) => f[0] === field)[1].toLowerCase();
  return patchTask(chatId, d.task_id, { [field]: value }, `поле «${label}» обновлено`, log);
}

async function taskAction(chatId, d, text, log) {
  const t = tasks.get(d.task_id);
  if (!t) { resetDialog(chatId); return send(chatId, "Задача не найдена — возможно, её удалили на доске."); }
  if (text === B.done) return patchTask(chatId, t.id, { done: true, doneAt: nowIso() }, "задача отмечена выполненной", log);
  if (text === B.reopen) return patchTask(chatId, t.id, { done: false, doneAt: null }, "задача снова в работе", log);
  if (text === B.status) {
    setDialog(chatId, { mode: "status", task_id: t.id });
    return send(chatId, `Текущий статус: ${t.done ? "выполнена" : QLABEL[t.quadrant] || "—"}. Выберите новый:`,
      kb([QUADRANTS.slice(0, 3).map((q) => q[1]), QUADRANTS.slice(3).map((q) => q[1]).concat([B.done]), [B.back]]));
  }
  if (text === B.edit) {
    setDialog(chatId, { mode: "edit", task_id: t.id });
    return send(chatId, "Что изменить?", kb([[EDIT_FIELDS[0][1], EDIT_FIELDS[1][1]], [EDIT_FIELDS[2][1], EDIT_FIELDS[3][1]], [EDIT_FIELDS[4][1], B.back]]));
  }
  return null;
}

// ---------------------------------------------------------------- projects (read-only)

function projectLine(p, i) {
  const meta = [STAGE_LABELS[p.status] || (p.status ? p.status : "этап не подтверждён")];
  if (p.assigned_engineer) meta.push(p.assigned_engineer);
  if (p.planned_deadline) meta.push("до " + fmtDue(p.planned_deadline));
  return `${i + 1}. ${p.project_name} · ${meta.join(" · ")}  /p_${codeOf(p.id)}`;
}

function mainProjects() {
  return projectsModel.listProjects({ category: "main_project", includeRelations: false }).filter((p) => !p.actual_completion_date);
}

function projectCard(p) {
  const lines = [`🛠 ${p.project_name}`, `Этап: ${STAGE_LABELS[p.status] || p.status || "не подтверждён"}`];
  if (p.planned_stage && p.planned_stage !== p.status) lines.push(`По плану: ${STAGE_LABELS[p.planned_stage] || p.planned_stage}`);
  if (p.article_number) lines.push(`Артикул: ${p.article_number}`);
  lines.push(`Конструктор: ${p.assigned_engineer || "—"}`);
  if (p.priority) lines.push(`Приоритет: ${p.priority}`);
  if (p.planned_deadline) lines.push(`Срок: ${fmtDue(p.planned_deadline)}`);
  if (p.next_action) lines.push(`Следующее действие: ${p.next_action}`);
  lines.push("Только просмотр — изменения вносятся на доске.");
  return lines.join("\n");
}

const projectsKb = (counts) => kb([
  ["scanning", "development"].map((k) => `${STAGE_LABELS[k]} (${counts[k] || 0})`),
  ["prototype", "measurement"].map((k) => `${STAGE_LABELS[k]} (${counts[k] || 0})`),
  [`${STAGE_LABELS.documentation} (${counts.documentation || 0})`, `Без этапа (${counts.none || 0})`],
  [B.search, B.menu]]);

async function showProjects(chatId, log) {
  const list = mainProjects();
  const counts = {};
  list.forEach((p) => { const k = STAGE_LABELS[p.status] ? p.status : "none"; counts[k] = (counts[k] || 0) + 1; });
  setDialog(chatId, { mode: "projects" });
  log("listed", `projects:${list.length}`);
  const lines = [`Инженерные проекты в работе: ${list.length}`].concat(
    Object.keys(STAGE_LABELS).map((k) => `${STAGE_LABELS[k]}: ${counts[k] || 0}`), [`Без подтверждённого этапа: ${counts.none || 0}`],
    ["", "Выберите этап или «🔎 Найти проект» (марка, модель, изделие, артикул)."]);
  return send(chatId, lines.join("\n"), projectsKb(counts));
}

async function listProjects(chatId, title, list, log) {
  setDialog(chatId, { mode: "projects", list: list.slice(0, 40).map((p) => p.id) });
  log("listed", `projects:${title}:${list.length}`);
  if (!list.length) return send(chatId, `${title}: ничего не найдено.`, kb([[B.search, B.projects], [B.menu]]));
  return send(chatId, `${title} (${list.length}${list.length > 40 ? ", первые 40" : ""}):\n${list.slice(0, 40).map(projectLine).join("\n")}`,
    kb([[B.search, B.projects], [B.menu]]));
}

function searchProjects(q) {
  const needle = String(q).toLowerCase().replace(/ё/g, "е");
  const words = needle.split(/\s+/).filter(Boolean);
  return mainProjects().filter((p) => {
    const hay = [p.project_name, p.manufacturer, p.vehicle_model, p.product_type, p.article_number, p.external_project_id]
      .filter(Boolean).join(" ").toLowerCase().replace(/ё/g, "е");
    return words.every((w) => hay.includes(w));
  });
}

// ---------------------------------------------------------------- dispatcher

function commandOf(text) {
  const m = text.match(/^\/([a-zA-Z0-9_]+)(?:@\S+)?(?:\s+([\s\S]*))?$/);
  return m ? { name: m[1].toLowerCase(), arg: (m[2] || "").trim() } : null;
}

/** True if this message is something the menu bot handles itself (used by personalChannel.js when
 *  the optional AI mode is on, to decide whether free text may go to the AI). */
function claims(message, chatId) {
  const text = (message.text || "").trim();
  if (!text) return false;
  if (commandOf(text)) return true;
  if (Object.values(B).includes(text)) return true;
  return getDialog(chatId).mode !== "idle";
}

async function handle(message, ctx) {
  const chatId = ctx.chatId;
  const text = (message.text || "").trim();
  const base = { channel: "personal", chat_id: chatId, user_id: ctx.userId, update_id: ctx.updateId, message_text: text };
  const log = (outcome, detail) => telegramLog.record(Object.assign({}, base, { outcome, detail }));
  if (!text) { log("ignored", "no text"); return; }

  const cmd = commandOf(text);
  const d = getDialog(chatId);

  // global commands and menu buttons - always available, they also leave any unfinished dialog
  if (cmd && (cmd.name === "start" || cmd.name === "help" || cmd.name === "menu") || text === B.help || text === B.menu) {
    resetDialog(chatId);
    log("replied", cmd ? `/${cmd.name}` : "menu");
    return send(chatId, cmd && cmd.name === "menu" || text === B.menu ? "Главное меню." : HELP_TEXT, MAIN_KB);
  }
  if (cmd && cmd.name === "cancel" || text === B.cancel) {
    resetDialog(chatId);
    log("replied", "cancel");
    return send(chatId, d.mode === "create" ? "Создание задачи отменено, ничего не сохранено." : "Отменено.", MAIN_KB);
  }
  if (cmd && (cmd.name === "tasks" || cmd.name === "list") || text === B.tasks) return showTasks(chatId, log);
  if (cmd && cmd.name === "projects" || text === B.projects) {
    if (cmd && cmd.arg) return listProjects(chatId, `Поиск «${cmd.arg}»`, searchProjects(cmd.arg), log);
    return showProjects(chatId, log);
  }
  if (cmd && cmd.name === "new" || text === B.create) {
    const draft = {};
    if (cmd && cmd.arg) draft.title = cmd.arg.slice(0, 300);
    log("replied", "create:start");
    return askStep(chatId, { mode: "create", step: draft.title ? "description" : "title", draft });
  }
  if (cmd && /^t_[a-z0-9]+$/.test(cmd.name)) {
    const hits = resolveByCode(tasks.list(), cmd.name.slice(2));
    if (hits.length !== 1) { log("replied", `task code:${hits.length}`); return send(chatId, hits.length ? "Код неоднозначен — откройте задачу из списка /tasks." : "Задача не найдена."); }
    return openTask(chatId, hits[0], log);
  }
  if (cmd && /^p_[a-z0-9]+$/.test(cmd.name)) {
    const hits = resolveByCode(projectsModel.listProjects({ includeRelations: false }), cmd.name.slice(2));
    if (hits.length !== 1) { log("replied", `project code:${hits.length}`); return send(chatId, "Проект не найден."); }
    log("replied", `project:${hits[0].id}`);
    return send(chatId, projectCard(hits[0]), kb([[B.search, B.projects], [B.menu]]));
  }
  if (cmd) { log("replied", `unknown /${cmd.name}`); return send(chatId, "Не знаю такой команды. /help — список команд."); }

  // dialog steps
  if (d.mode === "create") return createStep(chatId, d, text, message, log);
  if (d.mode === "edit_value") return editValue(chatId, d, text, log);
  if (d.mode === "edit") {
    if (text === B.back) { const t = tasks.get(d.task_id); return t ? openTask(chatId, t, log) : showTasks(chatId, log); }
    const f = EDIT_FIELDS.find((x) => x[1] === text);
    if (!f) return send(chatId, "Выберите поле кнопкой.", kb([[EDIT_FIELDS[0][1], EDIT_FIELDS[1][1]], [EDIT_FIELDS[2][1], EDIT_FIELDS[3][1]], [EDIT_FIELDS[4][1], B.back]]));
    setDialog(chatId, { mode: "edit_value", task_id: d.task_id, field: f[0] });
    const [p, k] = editPrompt(f[0]);
    return send(chatId, p, k);
  }
  if (d.mode === "status") {
    if (text === B.back) { const t = tasks.get(d.task_id); return t ? openTask(chatId, t, log) : showTasks(chatId, log); }
    if (text === B.done) return patchTask(chatId, d.task_id, { done: true, doneAt: nowIso() }, "задача отмечена выполненной", log);
    const q = QUADRANTS.find((x) => x[1] === text);
    if (!q) return send(chatId, "Выберите статус кнопкой.");
    return patchTask(chatId, d.task_id, { quadrant: q[0], done: false, doneAt: null }, `статус «${q[1]}»`, log);
  }
  if (d.mode === "task") {
    const r = await taskAction(chatId, d, text, log);
    if (r !== null) return r;
  }
  if (d.mode === "projects") {
    if (text === B.search) { setDialog(chatId, { mode: "project_search" }); return send(chatId, "Что искать? Марка, модель, изделие или артикул.", kb([[B.projects, B.menu]])); }
    const st = text.match(/^(.+) \(\d+\)$/);
    if (st) {
      const key = st[1] === "Без этапа" ? "none" : Object.keys(STAGE_LABELS).find((k) => STAGE_LABELS[k] === st[1]);
      if (key) return listProjects(chatId, st[1], mainProjects().filter((p) => (key === "none" ? !STAGE_LABELS[p.status] : p.status === key)), log);
    }
  }
  if (d.mode === "project_search") return listProjects(chatId, `Поиск «${text}»`, searchProjects(text), log);
  if (d.mode === "list" && /^\d{1,2}$/.test(text)) {
    const id = (d.list || [])[Number(text) - 1];
    const t = id ? tasks.get(id) : null;
    if (t) return openTask(chatId, t, log);
    return send(chatId, "Нет задачи с таким номером в последнем списке.");
  }
  if (d.mode === "offer" && (text === B.yes || text === B.no)) {
    if (text === B.no) { resetDialog(chatId); log("replied", "offer declined"); return send(chatId, "Хорошо, ничего не создаю.", MAIN_KB); }
    log("replied", "create:from text");
    return askStep(chatId, { mode: "create", step: "description", draft: { title: d.title } });
  }

  // Free text that is not a command: never guessed - offer to turn it into a task via the dialog.
  const title = text.slice(0, 300);
  setDialog(chatId, { mode: "offer", title });
  log("replied", "offer create");
  return send(chatId, `Не разобрал команду. Создать задачу «${title}»? Дальше я спрошу описание, срок, приоритет и ответственного.`,
    kb([[B.yes, B.no], [B.menu]]));
}

module.exports = { handle, claims, parseDue, codeOf, HELP_TEXT, BRAND, BUTTONS: B };
