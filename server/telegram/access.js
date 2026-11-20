"use strict";

// Which chat an incoming Telegram update belongs to, and who may act in it (ТЗ §3: "Предусмотреть
// отдельные настройки chat_id, разрешённых пользователей и прав доступа", §8.3/8.5: restrict to
// allowed chat_id/user_id, never let an outsider create data).
//
// TELEGRAM_OWNER_CHAT_ID / TELEGRAM_OWNER_USER_ID: the personal channel - one person, one chat.
// TELEGRAM_ENGINEERING_CHAT_ID: the construction-team group chat (unset until the bot is actually
// added there - see docs/TELEGRAM_INTEGRATION.md). TELEGRAM_ENGINEERING_ALLOWED_USER_IDS: optional
// comma-separated allowlist of user ids inside that chat; if unset, every member of the (already
// chat_id-restricted) group may post engineering updates - matching how the original chat itself
// already controls membership.

function ids(csv) {
  return String(csv || "").split(",").map((s) => s.trim()).filter(Boolean);
}

/** Resolves which channel (if any) an update belongs to, and whether its sender is allowed to act
 *  in it. Never both "personal" and "engineering" - a chat is one or the other. */
function resolveChannel(message) {
  const chatId = String((message.chat || {}).id || "");
  const userId = String((message.from || {}).id || "");
  const ownerChatId = String(process.env.TELEGRAM_OWNER_CHAT_ID || "");
  const ownerUserId = String(process.env.TELEGRAM_OWNER_USER_ID || "");
  const engineeringChatId = String(process.env.TELEGRAM_ENGINEERING_CHAT_ID || "");

  if (ownerChatId && chatId === ownerChatId) {
    return { channel: "personal", allowed: !ownerUserId || userId === ownerUserId, chatId, userId };
  }
  if (engineeringChatId && chatId === engineeringChatId) {
    const allowedList = ids(process.env.TELEGRAM_ENGINEERING_ALLOWED_USER_IDS);
    return { channel: "engineering", allowed: !allowedList.length || allowedList.includes(userId), chatId, userId };
  }
  return { channel: null, allowed: false, chatId, userId };
}

module.exports = { resolveChannel };
