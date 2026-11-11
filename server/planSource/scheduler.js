"use strict";

// Daily working-day schedule of the automatic plan check.
//
// * Runs at PLAN_SCHEDULE_TIME (default 09:00) local time in PLAN_TIMEZONE
//   (default Asia/Yekaterinburg), only on working days of the official
//   production calendar PLAN_WORK_CALENDAR (config/work_calendar/RU.json).
// * At most one scheduled execution per local date. The run is recorded in the
//   JSON store (plan_source_runs) BEFORE it starts, so a restart can never run
//   the same date twice.
// * Missed runs are not replayed: if the server starts after the scheduled
//   time, or the timer fires more than PLAN_SCHEDULE_GRACE_MINUTES late, the
//   date is recorded as "missed". A failed run is retried only at the next
//   scheduled run or by the manual «Обновить» button.
// * A year whose calendar is missing or unverified stops the automatic run
//   ("calendar_unavailable") instead of guessing.
// * Scheduled and manual runs share monitor.runRecorded() and the job lock, so
//   they never overlap.

const { v4: uuidv4 } = require("uuid");
const store = require("../store");
const monitor = require("./monitor");
const cal = require("./workCalendar");

const MAX_TIMER_MS = 6 * 60 * 60 * 1000;
const TEST_HOOKS = process.env.PLAN_SCHEDULER_TEST_HOOKS === "1";

const state = { armed: false, handle: null, target: null, fakeNow: null, running: false };

function now() {
  return state.fakeNow ? new Date(state.fakeNow) : new Date();
}

function nowIso() {
  return new Date().toISOString();
}

function active(cfg) {
  return cfg.configured && cfg.monitorEnabled && cfg.scheduleEnabled;
}

function scheduledRun(date) {
  return store.read((s) => {
    const r = s.plan_source_runs.find((x) => x.trigger === "schedule" && x.scheduled_date === date);
    return r ? JSON.parse(JSON.stringify(r)) : null;
  });
}

function setRun(id, fields) {
  store.tx((s) => Object.assign(s.plan_source_runs.find((x) => x.id === id), fields));
}

/** The next scheduled working-day run at or after `from` (or an error). */
function nextRun(cfg, from = now()) {
  if (!cfg.scheduleTime) return { error: `PLAN_SCHEDULE_TIME «${cfg.scheduleTimeRaw}» — ожидается ЧЧ:ММ` };
  if (!cal.validTimezone(cfg.timezone)) return { error: `PLAN_TIMEZONE «${cfg.timezone}» — неизвестный часовой пояс` };
  let date = cal.localParts(from, cfg.timezone).date;
  for (let i = 0; i < 400; i += 1, date = cal.addDays(date, 1)) {
    const at = cal.zonedInstant(date, cfg.scheduleTime, cfg.timezone);
    if (at.getTime() <= from.getTime() - 0) continue;
    let info;
    try {
      info = cal.dayInfo(cfg.workCalendar, date);
    } catch (e) {
      return { error: e.message, blocked_date: date };
    }
    if (info.working && !scheduledRun(date)) return { date, at: at.toISOString() };
  }
  return { error: "в ближайшие 400 дней нет рабочего дня" };
}

/**
 * Evaluate the schedule at time t. Returns what happened:
 * not_due | already_done | missed | non_working | calendar_unavailable | ran | inactive | config_error
 */
async function tick(t = now(), { startup = false } = {}) {
  const cfg = monitor.config();
  if (!active(cfg)) return { action: "inactive" };
  if (!cfg.scheduleTime || !cal.validTimezone(cfg.timezone)) {
    const e = nextRun(cfg, t).error;
    monitor.logEvent("error", "schedule_config", e);
    return { action: "config_error", error: e };
  }
  const date = cal.localParts(t, cfg.timezone).date;
  const target = cal.zonedInstant(date, cfg.scheduleTime, cfg.timezone);
  if (t.getTime() < target.getTime()) return { action: "not_due", date };
  if (scheduledRun(date)) return { action: "already_done", date };
  const base = { trigger: "schedule", scheduled_date: date, scheduled_for: target.toISOString() };

  let info;
  try {
    info = cal.dayInfo(cfg.workCalendar, date);
  } catch (e) {
    monitor.recordRun(Object.assign(base, { outcome: "calendar_unavailable", error: e.message,
      phases: { check: "not_run", analysis: "not_run", sync: "not_run" } }));
    monitor.logEvent("error", "calendar_unavailable", `${date}: ${e.message}`);
    return { action: "calendar_unavailable", date, error: e.message };
  }
  if (!info.working) {
    monitor.recordRun(Object.assign(base, { outcome: "skipped_non_working", reason: info.reason,
      phases: { check: "not_run", analysis: "not_run", sync: "not_run" } }));
    return { action: "non_working", date, reason: info.reason };
  }
  const lateMin = (t.getTime() - target.getTime()) / 60000;
  if (startup || lateMin > cfg.graceMinutes) {
    monitor.recordRun(Object.assign(base, { outcome: "missed",
      reason: startup ? "сервер запущен после времени запуска — пропущенный запуск не повторяется"
        : `запуск опоздал на ${Math.round(lateMin)} мин — не повторяется`,
      phases: { check: "not_run", analysis: "not_run", sync: "not_run" } }));
    monitor.logEvent("warn", "schedule_missed", `плановый запуск ${date} пропущен`);
    return { action: "missed", date };
  }
  // Claim the date first: a restart during the run must not start it again.
  const claim = monitor.recordRun(Object.assign(base, { outcome: "running", started_at: nowIso(),
    phases: { check: "not_run", analysis: "not_run", sync: "not_run" } }));
  state.running = true;
  try {
    const { run } = await monitor.runRecorded({ trigger: "schedule", scheduledDate: date, scheduledFor: base.scheduled_for });
    // merge the detailed record into the claim and drop the duplicate entry
    store.tx((s) => {
      const i = s.plan_source_runs.findIndex((x) => x.id === run.id);
      if (i >= 0) s.plan_source_runs.splice(i, 1);
      const c = s.plan_source_runs.find((x) => x.id === claim.id);
      Object.assign(c, run, { id: claim.id });
    });
    return { action: "ran", date, outcome: run.outcome };
  } catch (e) {
    setRun(claim.id, { outcome: "error", error: e.message, finished_at: nowIso() });
    return { action: "ran", date, outcome: "error", error: e.message };
  } finally {
    state.running = false;
  }
}

// A restart while a scheduled run was in progress: the date stays claimed.
function recoverInterrupted() {
  store.tx((s) => s.plan_source_runs.forEach((r) => {
    if (r.outcome === "running") {
      r.outcome = "interrupted";
      r.error = "выполнение прервано перезапуском сервера; повтор — в следующий плановый запуск или кнопкой «Обновить»";
      r.finished_at = nowIso();
    }
  }));
}

function arm() {
  clearTimeout(state.handle);
  const cfg = monitor.config();
  if (!active(cfg) || !cfg.scheduleTime || !cal.validTimezone(cfg.timezone)) {
    state.target = null;
    return;
  }
  const t = now();
  let date = cal.localParts(t, cfg.timezone).date;
  let target = cal.zonedInstant(date, cfg.scheduleTime, cfg.timezone);
  if (target.getTime() <= t.getTime()) {
    date = cal.addDays(date, 1);
    target = cal.zonedInstant(date, cfg.scheduleTime, cfg.timezone);
  }
  state.target = target.toISOString();
  const delay = Math.min(target.getTime() - t.getTime(), MAX_TIMER_MS);
  state.handle = setTimeout(async () => {
    if (Date.now() >= target.getTime() - 1000) {
      try {
        await tick(new Date(Math.max(Date.now(), target.getTime())));
      } catch (e) {
        console.error("[plan-source] scheduled run crashed:", e); // eslint-disable-line no-console
      }
    }
    arm();
  }, Math.max(1000, delay));
  if (state.handle.unref) state.handle.unref();
}

/** Called once when the server starts listening. */
function start() {
  recoverInterrupted();
  const cfg = monitor.config();
  if (!active(cfg)) return false;
  const startNow = TEST_HOOKS && process.env.PLAN_SCHEDULER_TEST_NOW ? new Date(process.env.PLAN_SCHEDULER_TEST_NOW) : now();
  tick(startNow, { startup: true }).catch((e) => console.error(e)); // eslint-disable-line no-console
  const nr = nextRun(cfg, startNow);
  monitor.logEvent(nr.error ? "error" : "info", "scheduler_started", nr.error
    ? `расписание: ${nr.error}`
    : `ежедневная проверка в рабочие дни в ${cfg.scheduleTime} (${cfg.timezone}), календарь ${cfg.workCalendar}; следующая: ${nr.date}`);
  if (!TEST_HOOKS) {
    state.armed = true;
    arm();
  }
  return true;
}

function status() {
  const cfg = monitor.config();
  const nr = active(cfg) ? nextRun(cfg) : {};
  const last = store.read((s) => {
    const sched = s.plan_source_runs.filter((r) => r.trigger === "schedule");
    const all = s.plan_source_runs;
    const lastSync = all.filter((r) => r.phases && r.phases.sync === "ok").slice(-1)[0] || null;
    return { scheduled: sched.slice(-1)[0] || null, any: all.slice(-1)[0] || null, sync: lastSync,
      history: all.slice(-30).reverse() };
  });
  return {
    monitor_enabled: cfg.monitorEnabled,
    schedule_enabled: cfg.scheduleEnabled,
    schedule_active: active(cfg),
    schedule_time: cfg.scheduleTime,
    timezone: cfg.timezone,
    work_calendar: cfg.workCalendar,
    grace_minutes: cfg.graceMinutes,
    next_run_at: nr.at || null,
    next_run_date: nr.date || null,
    schedule_error: nr.error || null,
    last_scheduled_run: last.scheduled,
    last_run: last.any,
    last_sync_run: last.sync,
    history: last.history
  };
}

// ---- test hooks (PLAN_SCHEDULER_TEST_HOOKS=1 only) ----
function setClock(iso) {
  state.fakeNow = iso ? new Date(iso).toISOString() : null;
}

module.exports = { start, tick, nextRun, status, arm, setClock, TEST_HOOKS, now };
