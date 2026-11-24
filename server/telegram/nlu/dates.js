"use strict";

// Extracts a single relative/absolute date mentioned anywhere inside a free-text sentence (as
// opposed to server/telegram/personalBot.js's parseDue(), which parses one whole wizard answer
// that is nothing BUT a date). Used by assistant.js for "перенеси задачу TIREI на пятницу" /
// "что нужно сделать сегодня" / "какие задачи на этой неделе". Resolves in TELEGRAM_BOT_TIMEZONE
// (dateUtils.TZ) - never the server's own local time, never UTC.

const dateUtils = require("./../dateUtils");
const { normalizeText, tokenize } = require("./normalize");

/** "YYYY-MM-DD" or "DD.MM[.YY[YY]]" found anywhere in `text`, resolved against the local today
 *  (a day/month with no year that has already passed this year rolls over to next year, same
 *  rule parseDue() uses) -> "YYYY-MM-DD" | null. */
function extractAbsoluteDate(text) {
  const n = normalizeText(text);
  let m = n.match(/(\d{4})-(\d{2})-(\d{2})/);
  if (m && dateUtils.validYmd(+m[1], +m[2], +m[3])) return `${m[1]}-${m[2]}-${m[3]}`;
  m = n.match(/(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?/);
  if (m) {
    const d = +m[1];
    const mo = +m[2];
    const today = dateUtils.localToday();
    let y = m[3] ? +m[3] : +today.slice(0, 4);
    if (m[3] && m[3].length === 2) y += 2000;
    if (dateUtils.validYmd(y, mo, d)) {
      let ymd = `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
      if (!m[3] && ymd < today) ymd = `${y + 1}${ymd.slice(4)}`;
      return ymd;
    }
  }
  return null;
}

/** "сегодня"/"завтра"/"послезавтра" or a weekday name ("пятницу", "в пятницу") anywhere in
 *  `text` -> "YYYY-MM-DD" | null. A bare weekday means "the next one" (today itself only counts
 *  if the sentence also says "сегодня"). */
function extractRelativeDate(text) {
  const today = dateUtils.localToday();
  const n = normalizeText(text);
  if (/(?:^|\s)послезавтра(?:$|\s)/.test(n)) return dateUtils.addDays(today, 2);
  if (/(?:^|\s)завтра(?:$|\s)/.test(n)) return dateUtils.addDays(today, 1);
  if (/(?:^|\s)сегодня(?:$|\s)/.test(n)) return today;
  for (const word of tokenize(text)) {
    const idx = dateUtils.matchWeekday(word);
    if (idx !== null) return dateUtils.nextWeekday(today, idx, false);
  }
  return null;
}

/** First date mentioned anywhere in `text` (absolute wins over relative if the sentence somehow
 *  has both), or null if none was recognised - callers must never invent a date themselves when
 *  this returns null. */
function extractDate(text) {
  return extractAbsoluteDate(text) || extractRelativeDate(text);
}

module.exports = { extractAbsoluteDate, extractRelativeDate, extractDate };
