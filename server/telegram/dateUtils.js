"use strict";

// Date helpers shared by the personal Telegram bot's scripted wizard (server/telegram/
// personalBot.js) and its natural-language layer (server/telegram/nlu/). All relative dates
// ("сегодня", "завтра", "в пятницу", "на этой неделе") resolve in one timezone
// (TELEGRAM_BOT_TIMEZONE, default Asia/Yekaterinburg) - never the server's own local time.

const TZ = process.env.TELEGRAM_BOT_TIMEZONE || "Asia/Yekaterinburg";

function localToday(tz = TZ) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
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

/** 0 = Monday ... 6 = Sunday (ISO-ish, matches how Russian calendars are usually read), for the
 *  local (TZ) calendar date `ymd`. */
function weekdayIndex(ymd) {
  const js = new Date(`${ymd}T00:00:00Z`).getUTCDay(); // 0 = Sunday
  return (js + 6) % 7;
}

const WEEKDAY_NAMES = [
  ["понедельник", "пн"], ["вторник", "вт"], ["среда", "ср"], ["четверг", "чт"],
  ["пятница", "пт"], ["суббота", "сб"], ["воскресенье", "вс"]
];

/** "понедельник|вторник|...", any case form ("пятницу", "среду") matched by prefix of the root
 *  (first 4-5 letters are stable across Russian case endings for every weekday name here) -> 0-6,
 *  or null. */
function matchWeekday(word) {
  const w = String(word || "").toLowerCase();
  for (let i = 0; i < WEEKDAY_NAMES.length; i += 1) {
    const [full, short] = WEEKDAY_NAMES[i];
    const root = full.slice(0, full.length - 2); // stable across every case ending used here
    if (w === short || w.startsWith(root)) return i;
  }
  return null;
}

/** Next occurrence of weekday `targetIdx` (0=Mon..6=Sun) strictly after `fromYmd`, or `fromYmd`
 *  itself if it already falls on that weekday and `includeToday` is true. */
function nextWeekday(fromYmd, targetIdx, includeToday = false) {
  const cur = weekdayIndex(fromYmd);
  let diff = (targetIdx - cur + 7) % 7;
  if (diff === 0 && !includeToday) diff = 7;
  return addDays(fromYmd, diff);
}

/** Monday..Sunday of the local calendar week containing `ymd` (or today). */
function weekRange(ymd) {
  const start = addDays(ymd, -weekdayIndex(ymd));
  return { start, end: addDays(start, 6) };
}

/** True if `due` ("YYYY-MM-DD" or "YYYY-MM-DDTHH:MM") is in the past relative to "now" in `tz`,
 *  and the item isn't already marked done. Same rule personalBot.js's task cards already use. */
function isOverdueDue(due, done, tz = TZ) {
  if (!due || done) return false;
  const s = String(due);
  const today = localToday(tz);
  return s.length <= 10 ? s < today : new Date(s).getTime() < Date.now();
}

module.exports = {
  TZ, localToday, addDays, validYmd, weekdayIndex, matchWeekday, nextWeekday, weekRange, WEEKDAY_NAMES, isOverdueDue
};
