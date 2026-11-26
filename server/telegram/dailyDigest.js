"use strict";

// Daily personal task digest, sent every weekday morning.
//
// History of this schedule (see docs/MORNING_DIGEST_TIME_FIX.md for the full investigation): the
// original version of this module copied its time verbatim from an old Claude Code scheduled task
// ("Штурман: утренняя сводка", cron `CRON_TZ=Europe/Moscow 53 7 * * 1-5`) that pre-dates this
// server and used to send the digest directly from a Claude session (reading the old Claude
// Artifact, not this app, and calling the Telegram Bot API itself) - that scheduled task, and two
// other leftover ones from the same era ("Утренний обзор канбан-доски", "Штурман: опрос
// Telegram"), are now disabled but still exist; they are not this module and must not be
// re-enabled, or the digest (and task creation from Telegram) would run twice from two unrelated
// places. This module is the one, current, server-side implementation - confirmed scheduled
// correctly below, independent of both the disabled legacy triggers above and of the unrelated
// 16:00 Asia/Yekaterinburg КО-chat analysis (a separate Claude scheduled task, untouched by this
// file).
//   - schedule: 09:00 Asia/Yekaterinburg, Monday-Friday only (fixed per this fix's requirement),
//     with NO work-calendar/holiday awareness (unlike server/planSource/scheduler.js's
//     PLAN_WORK_CALENDAR check for the Excel agent - this digest never had that, so it still
//     doesn't). Uses the exact same TZ-correct conversion (planSource/workCalendar.js's
//     zonedInstant/localParts) the Excel scheduler already uses at its own 09:00
//     Asia/Yekaterinburg - that function was audited as part of this fix and is not itself the
//     source of any offset bug; see the investigation doc.
//   - content: active (done:false) tasks from the "tasks" doc store, grouped by quadrant in the
//     fixed order inbox/do/plan/delegate/drop, one numbered line per task showing whichever of
//     направление/от кого/ответственный/срок is actually set, "— просрочено" appended when an
//     unfinished task's срок is in the past.
//   - destination: TELEGRAM_OWNER_CHAT_ID only. This module never reads or references
//     TELEGRAM_ENGINEERING_CHAT_ID, and botApi.call()'s own guard (server/telegram/botApi.js) would
//     refuse it anyway if it somehow did.
//   - idempotency: at most one send per local (Asia/Yekaterinburg) calendar date - recorded in the
//     "telegram_digest" object (server/store.js OBJECTS), independent of plan_source/plan_source_runs
//     so this never touches the Excel agent's own scheduling or import-lock state (see
//     docs/TELEGRAM_INTEGRATION.md "Interaction with the Excel scheduler"). A restart does not
//     resend today's digest; a run that fires more than GRACE_MINUTES late is marked "missed" and
//     not replayed, the same rule planSource/scheduler.js uses for the Excel check.
//   - never creates, updates or deletes a task or project - it only reads the "tasks" collection
//     (and, for the КО projects section added alongside this fix, only reads project_history -
//     see server/telegram/engineeringDigest.js).

const { createDocStore } = require("../docStore");
const store = require("../store");
const cal = require("../planSource/workCalendar");
const botApi = require("./botApi");
const telegramLog = require("./log");
const engineeringDigest = require("./engineeringDigest");

const TIMEZONE = "Asia/Yekaterinburg";
const SCHEDULE_TIME = "09:00";
const GRACE_MINUTES = 30;
const MAX_TIMER_MS = 6 * 60 * 60 * 1000;
const TEST_HOOKS = process.env.TELEGRAM_DIGEST_TEST_HOOKS === "1";

const QUADRANT_ORDER = ["inbox", "do", "plan", "delegate", "drop"];
const QUADRANT_LABELS = {
  inbox: "Входящие", do: "Делать сейчас", plan: "Планировать", delegate: "Делегировать", drop: "Отложить/убрать"
};

const state = { handle: null, fakeNow: null, target: null };

function now() {
  return state.fakeNow ? new Date(state.fakeNow) : new Date();
}

function isWeekday(dateStr) {
  const [y, m, d] = dateStr.split("-").map(Number);
  const wd = new Date(Date.UTC(y, m - 1, d)).getUTCDay(); // date-only, no tz skew: 0=Sun..6=Sat
  return wd >= 1 && wd <= 5;
}

function ruDateLabel(d, timeZone) {
  // Intl's own 'long' weekday/month in Russian ("пятница, 20 ноября"), plus the bare year appended
  // separately - matches this app's own earlier manually-sent digest text exactly ("...2026", no
  // trailing "г."), which the trailing "г." from year:'numeric' in one formatter call would add.
  const dayPart = new Intl.DateTimeFormat("ru-RU", { weekday: "long", day: "numeric", month: "long", timeZone }).format(d);
  const year = new Intl.DateTimeFormat("ru-RU", { year: "numeric", timeZone }).format(d);
  return `${dayPart} ${year}`;
}

function taskLine(t, at) {
  const meta = [];
  if (t.direction) meta.push(`направление: ${t.direction}`);
  if (t.from) meta.push(`от: ${t.from}`);
  if (t.responsible) meta.push(`ответственный: ${t.responsible}`);
  if (t.due) {
    const overdue = !t.done && new Date(t.due).getTime() < at.getTime();
    meta.push(`срок: ${t.due}${overdue ? " — просрочено" : ""}`);
  }
  const title = t.title || "";
  return meta.length ? `${title} (${meta.join(", ")})` : title;
}

/** Pure, independently testable: the digest text for a given task list and instant, plus the
 *  already-built КО-changes section text (server/telegram/engineeringDigest.js - kept separate so
 *  this function needs no store access of its own and stays a pure formatter, same as before this
 *  section existed). `engineeringSection` is optional so every pre-existing caller/test of this
 *  function (personal tasks only) keeps working unchanged. */
function buildDigestText(tasks, at, engineeringSection) {
  const header = `Доброе утро! Активные задачи — ${ruDateLabel(at, TIMEZONE)}`;
  const active = tasks.filter((t) => !t.done);
  const taskBlock = active.length ? [header].concat((() => {
    const byQuadrant = {};
    active.forEach((t) => { (byQuadrant[t.quadrant] || (byQuadrant[t.quadrant] = [])).push(t); });
    return QUADRANT_ORDER.filter((q) => byQuadrant[q] && byQuadrant[q].length).map((q) =>
      `${QUADRANT_LABELS[q]}:\n${byQuadrant[q].map((t, i) => `${i + 1}. ${taskLine(t, at)}`).join("\n")}`);
  })()).join("\n\n") : `${header}\n\nНа сегодня активных задач нет.`;
  return engineeringSection ? `${taskBlock}\n\n${engineeringSection}` : taskBlock;
}

function digestState() {
  return store.read((s) => Object.assign({}, s.telegram_digest));
}

function setDigestState(fields) {
  store.tx((s) => { s.telegram_digest = Object.assign({}, s.telegram_digest, fields); });
}

function configuredToRun() {
  return botApi.configured() && !!process.env.TELEGRAM_OWNER_CHAT_ID;
}

/** The daily tick: at most one send per local date, weekdays only, missed runs not replayed. */
async function tick(t = now(), { startup = false } = {}) {
  if (!configuredToRun()) return { action: "inactive" };
  const dateKey = cal.localParts(t, TIMEZONE).date;
  if (digestState().last_sent_date === dateKey) return { action: "already_done", date: dateKey };
  if (!isWeekday(dateKey)) return { action: "non_working", date: dateKey };
  const target = cal.zonedInstant(dateKey, SCHEDULE_TIME, TIMEZONE);
  if (t.getTime() < target.getTime()) return { action: "not_due", date: dateKey };
  const lateMin = (t.getTime() - target.getTime()) / 60000;
  if (startup || lateMin > GRACE_MINUTES) {
    setDigestState({ last_missed_date: dateKey });
    telegramLog.record({ channel: "personal", outcome: "ignored",
      detail: `ежедневная сводка за ${dateKey} пропущена (${startup ? "сервер запущен позже времени отправки" : `опоздание ${Math.round(lateMin)} мин`}) — повтор не выполняется` });
    return { action: "missed", date: dateKey };
  }
  const tasks = createDocStore("tasks").list();
  const engineeringSection = engineeringDigest.buildEngineeringChangesSection(t);
  const text = buildDigestText(tasks, t, engineeringSection);
  try {
    await botApi.sendMessage(process.env.TELEGRAM_OWNER_CHAT_ID, text);
    setDigestState({ last_sent_date: dateKey, last_sent_at: new Date().toISOString(),
      last_active_count: tasks.filter((x) => !x.done).length });
    telegramLog.record({ channel: "personal", chat_id: process.env.TELEGRAM_OWNER_CHAT_ID, outcome: "replied",
      detail: `ежедневная сводка за ${dateKey} отправлена` });
    return { action: "sent", date: dateKey };
  } catch (e) {
    telegramLog.record({ channel: "personal", outcome: "error", error: e.message,
      detail: `ежедневная сводка за ${dateKey} не отправлена` });
    return { action: "error", date: dateKey, error: e.message };
  }
}

function arm() {
  clearTimeout(state.handle);
  if (!configuredToRun()) { state.target = null; return; }
  const t = now();
  let date = cal.localParts(t, TIMEZONE).date;
  let target = cal.zonedInstant(date, SCHEDULE_TIME, TIMEZONE);
  if (target.getTime() <= t.getTime()) {
    date = cal.addDays(date, 1);
    target = cal.zonedInstant(date, SCHEDULE_TIME, TIMEZONE);
  }
  state.target = target.toISOString();
  const delay = Math.min(target.getTime() - t.getTime(), MAX_TIMER_MS);
  state.handle = setTimeout(async () => {
    try {
      await tick(new Date(Math.max(Date.now(), target.getTime())));
    } catch (e) {
      console.error("[telegram-digest] scheduled send crashed:", e); // eslint-disable-line no-console
    }
    arm();
  }, Math.max(1000, delay));
  if (state.handle.unref) state.handle.unref();
}

/** Called once when the server starts listening, alongside planScheduler.start(). */
function start() {
  if (!configuredToRun()) return false;
  // Under TEST_HOOKS, setClock() can only run after the server is already listening (it's an HTTP
  // call) - too late for this startup tick. TELEGRAM_DIGEST_TEST_NOW, set before the process
  // starts, lets a test control this one instant too, same as planSource/scheduler.js's
  // PLAN_SCHEDULER_TEST_NOW.
  const startNow = TEST_HOOKS && process.env.TELEGRAM_DIGEST_TEST_NOW ? new Date(process.env.TELEGRAM_DIGEST_TEST_NOW) : now();
  tick(startNow, { startup: true }).catch((e) => console.error(e)); // eslint-disable-line no-console
  if (!TEST_HOOKS) arm();
  return true;
}

function status() {
  const st = digestState();
  return {
    active: configuredToRun(), schedule_time: SCHEDULE_TIME, timezone: TIMEZONE,
    last_sent_date: st.last_sent_date || null, last_sent_at: st.last_sent_at || null,
    last_active_count: st.last_active_count != null ? st.last_active_count : null,
    last_missed_date: st.last_missed_date || null, target: state.target
  };
}

// ---- test hooks (TELEGRAM_DIGEST_TEST_HOOKS=1 only) ----
function setClock(iso) {
  state.fakeNow = iso ? new Date(iso).toISOString() : null;
}

module.exports = { start, tick, status, arm, setClock, TEST_HOOKS, now, buildDigestText, QUADRANT_ORDER, QUADRANT_LABELS };
