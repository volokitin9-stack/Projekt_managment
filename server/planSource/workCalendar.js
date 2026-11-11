"use strict";

// Official production calendar (five-day week) and time-zone date helpers.
//
// A calendar file (config/work_calendar/<ID>.json) lists, per year, the
// non-working dates (holidays and government-approved transfers) and any
// weekend dates designated as working days. A date is a working day when it is
// Monday-Friday and not listed as non-working, or when it is listed as a
// working weekend date.
//
// Nothing is guessed: a year that is missing, marked verified=false, or whose
// computed number of working days differs from expected_working_days makes
// every lookup for that year throw a CalendarError. Future years must be added
// only after the government resolution for that year has been published.

const fs = require("fs");
const path = require("path");

const CONFIG_DIR = path.join(__dirname, "..", "..", "config", "work_calendar");

class CalendarError extends Error {
  constructor(message) {
    super(message);
    this.name = "CalendarError";
    this.code = "calendar_unavailable";
  }
}

const cache = new Map();

function calendarFile(id) {
  const v = String(id || "").trim();
  if (!v) throw new CalendarError("PLAN_WORK_CALENDAR не задан");
  if (/^[A-Za-z]{2,10}$/.test(v)) return path.join(CONFIG_DIR, `${v.toUpperCase()}.json`);
  return path.resolve(path.join(__dirname, "..", ".."), v);   // explicit path (e.g. a test calendar)
}

function weekday(dateStr) {
  const [y, m, d] = dateStr.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay();   // 0 = Sunday
}

function load(id) {
  const file = calendarFile(id);
  let st;
  try {
    st = fs.statSync(file);
  } catch (e) {
    throw new CalendarError(`файл производственного календаря не найден: ${path.basename(file)}`);
  }
  const hit = cache.get(file);
  if (hit && hit.mtime === st.mtimeMs) return hit.cal;
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(file, "utf-8"));
  } catch (e) {
    throw new CalendarError(`файл календаря ${path.basename(file)} повреждён: ${e.message}`);
  }
  const cal = { id: raw.calendar || id, years: {} };
  Object.entries(raw.years || {}).forEach(([year, y]) => {
    const off = new Set((y.non_working_dates || []).map((x) => (typeof x === "string" ? x : x.date)));
    const on = new Set((y.working_weekend_dates || []).map((x) => (typeof x === "string" ? x : x.date)));
    const entry = { year, verified: y.verified === true, off, on, problem: null, working_days: null };
    const bad = [...off, ...on].filter((d) => !/^\d{4}-\d{2}-\d{2}$/.test(d) || !d.startsWith(`${year}-`));
    if (bad.length) entry.problem = `даты не относятся к ${year} году: ${bad.join(", ")}`;
    [...on].forEach((d) => {
      if (![0, 6].includes(weekday(d))) entry.problem = `${d} указан как рабочий выходной, но это будний день`;
    });
    if (!entry.problem) {
      let n = 0;
      for (let t = Date.UTC(Number(year), 0, 1); new Date(t).getUTCFullYear() === Number(year); t += 86400000) {
        const ds = new Date(t).toISOString().slice(0, 10);
        if (isWorkingIn(entry, ds)) n += 1;
      }
      entry.working_days = n;
      if (y.expected_working_days != null && y.expected_working_days !== n) {
        entry.problem = `рассчитано ${n} рабочих дней, в календаре указано ${y.expected_working_days} — данные не согласованы`;
      }
    }
    cal.years[year] = entry;
  });
  cache.set(file, { mtime: st.mtimeMs, cal });
  return cal;
}

function isWorkingIn(entry, dateStr) {
  if (entry.on.has(dateStr)) return true;
  if (entry.off.has(dateStr)) return false;
  return ![0, 6].includes(weekday(dateStr));
}

/** {working, reason} for a local date YYYY-MM-DD. Throws CalendarError when the year is not usable. */
function dayInfo(id, dateStr) {
  const cal = load(id);
  const year = dateStr.slice(0, 4);
  const entry = cal.years[year];
  if (!entry) throw new CalendarError(`производственный календарь ${cal.id} на ${year} год не утверждён в конфигурации — автоматический запуск остановлен`);
  if (!entry.verified) throw new CalendarError(`календарь ${cal.id} на ${year} год не подтверждён (verified=false) — автоматический запуск остановлен`);
  if (entry.problem) throw new CalendarError(`календарь ${cal.id} на ${year} год: ${entry.problem}`);
  if (entry.on.has(dateStr)) return { working: true, reason: "рабочий выходной (перенос)" };
  if (entry.off.has(dateStr)) return { working: false, reason: "нерабочий праздничный день / перенос" };
  const wd = weekday(dateStr);
  if (wd === 0 || wd === 6) return { working: false, reason: wd === 6 ? "суббота" : "воскресенье" };
  return { working: true, reason: "рабочий день" };
}

// ---------------------------------------------------------------- time zones
function localParts(date, tz) {
  const f = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  const p = {};
  f.formatToParts(date).forEach((x) => { p[x.type] = x.value; });
  return { date: `${p.year}-${p.month}-${p.day}`, time: `${p.hour}:${p.minute}`,
    minutes: Number(p.hour) * 60 + Number(p.minute) };
}

/** The UTC instant of local date + HH:MM in tz. */
function zonedInstant(dateStr, hhmm, tz) {
  const [y, m, d] = dateStr.split("-").map(Number);
  const [hh, mm] = hhmm.split(":").map(Number);
  const guess = Date.UTC(y, m - 1, d, hh, mm);
  let t = guess;
  for (let i = 0; i < 3; i += 1) {        // converge on the zone offset (handles DST zones too)
    const lp = localParts(new Date(t), tz);
    const [ly, lm, ld] = lp.date.split("-").map(Number);
    const shown = Date.UTC(ly, lm - 1, ld, Math.floor(lp.minutes / 60), lp.minutes % 60);
    t += guess - shown;
  }
  return new Date(t);
}

function addDays(dateStr, n) {
  const [y, m, d] = dateStr.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

function validTimezone(tz) {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    return true;
  } catch (e) {
    return false;
  }
}

module.exports = { CalendarError, dayInfo, load, localParts, zonedInstant, addDays, validTimezone, CONFIG_DIR };
