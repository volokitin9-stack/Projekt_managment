"use strict";

// Normalised Telegram chat/user id env vars - the ONE place they are read, so routing
// (server/telegram/access.js), the КО read-only outbound guard (server/telegram/botApi.js), КО
// evidence checks (server/ko/proposals.js) and the status page all see the same value.
//
// Why: values pasted into Railway Variables sometimes keep a trailing space/newline or a pair of
// surrounding quotes ("-4994794290 " or "\"-4994794290\""). Compared verbatim, such a value never
// equals String(message.chat.id), so every КО message was logged as channel unknown / rejected -
// and, worse, the read-only guard compared against the same raw value, so a mis-formatted variable
// would also have silently disabled it. Normalisation: trim whitespace, strip ONE pair of matching
// surrounding single or double quotes, trim again.

const SCALAR_VARS = ["TELEGRAM_OWNER_CHAT_ID", "TELEGRAM_OWNER_USER_ID", "TELEGRAM_ENGINEERING_CHAT_ID"];
const LIST_VARS = ["TELEGRAM_ENGINEERING_ALLOWED_USER_IDS"];

function stripQuotes(s) {
  if (s.length >= 2 && (s[0] === '"' || s[0] === "'") && s[s.length - 1] === s[0]) return s.slice(1, -1).trim();
  return s;
}

/** Normalises one id value. Undefined/null -> "". */
function normalizeId(raw) {
  return stripQuotes(String(raw == null ? "" : raw).trim());
}

/** Normalises a comma-separated id list: the whole value may be quoted, and so may each entry. */
function normalizeIdList(raw) {
  return normalizeId(raw).split(",").map(normalizeId).filter(Boolean);
}

function envId(name) {
  return normalizeId(process.env[name]);
}

function envIdList(name) {
  return normalizeIdList(process.env[name]);
}

const ownerChatId = () => envId("TELEGRAM_OWNER_CHAT_ID");
const ownerUserId = () => envId("TELEGRAM_OWNER_USER_ID");
const engineeringChatId = () => envId("TELEGRAM_ENGINEERING_CHAT_ID");
const engineeringAllowedUserIds = () => envIdList("TELEGRAM_ENGINEERING_ALLOWED_USER_IDS");

/** One human-readable warning per variable whose raw value differs from its normalised form (it
 *  still works - this just tells the operator to clean it up). Never includes the value itself. */
function warnings() {
  const out = [];
  SCALAR_VARS.forEach((name) => {
    const raw = process.env[name];
    if (raw == null || raw === "") return;
    if (raw !== normalizeId(raw)) {
      out.push(`${name}: значение содержит пробелы/переводы строки или кавычки по краям - используется очищенное значение; исправьте переменную окружения`);
    }
    if (!normalizeId(raw)) out.push(`${name}: после очистки значение пустое - канал не настроен`);
  });
  LIST_VARS.forEach((name) => {
    const raw = process.env[name];
    if (raw == null || raw === "") return;
    // whitespace around entries was always accepted ("1, 2") - only quotes are worth flagging
    const plain = String(raw).split(",").map((s) => s.trim()).filter(Boolean);
    const norm = normalizeIdList(raw);
    if (plain.length !== norm.length || plain.some((s, i) => s !== norm[i])) {
      out.push(`${name}: значения содержат кавычки - используется очищенный список; исправьте переменную окружения`);
    }
  });
  return out;
}

module.exports = {
  normalizeId, normalizeIdList, envId, envIdList,
  ownerChatId, ownerUserId, engineeringChatId, engineeringAllowedUserIds, warnings
};
