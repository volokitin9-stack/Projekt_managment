"use strict";

// Telegram webhook receiver - the single HTTP entry point for both channels (ТЗ §8: a secret-
// verified webhook, idempotent by update_id, restricted to known chat_id/user_id). Mounted
// outside the dashboard-session check in server/index.js, the same way /api/agent/* is: Telegram
// itself can never present that session's cookie, and this route has its own, separate secret.
//
// Security (ТЗ §8.2/8.3/8.5): every request must carry the exact secret Telegram was told to set
// (X-Telegram-Bot-Api-Secret-Token) - if TELEGRAM_WEBHOOK_SECRET is not configured, every request
// is refused (fail closed, same convention as server/auth.js's requireAgentAuth). A chat/user not
// recognised by server/telegram/access.js is logged and otherwise ignored - never creates data.

const express = require("express");
const access = require("../telegram/access");
const telegramLog = require("../telegram/log");
const personalChannel = require("../telegram/personalChannel");
const engineeringChannel = require("../telegram/engineeringChannel");

const router = express.Router();

router.post("/webhook", async (req, res) => {
  const expected = process.env.TELEGRAM_WEBHOOK_SECRET;
  if (!expected) {
    return res.status(501).json({ error: "TELEGRAM_WEBHOOK_SECRET is not configured on the server." });
  }
  if (req.get("x-telegram-bot-api-secret-token") !== expected) {
    return res.status(401).json({ error: "Invalid webhook secret" });
  }
  const update = req.body || {};
  const message = update.message;
  // An update type we don't handle (e.g. channel_post, edited_message) is not a delivery failure -
  // acknowledge it so Telegram doesn't retry, there is just nothing to do with it.
  if (!update.update_id || !message) return res.status(200).json({ ok: true });
  if (!telegramLog.claimUpdate(update.update_id)) return res.status(200).json({ ok: true }); // already processed (Telegram retry)

  const resolved = access.resolveChannel(message);
  const base = { update_id: update.update_id, chat_id: resolved.chatId, user_id: resolved.userId,
    message_text: message.text || message.caption || null };
  if (!resolved.channel) {
    telegramLog.record(Object.assign({ channel: "unknown" }, base, { outcome: "rejected", detail: "unrecognised chat" }));
    return res.status(200).json({ ok: true });
  }
  if (!resolved.allowed) {
    telegramLog.record(Object.assign({ channel: resolved.channel }, base, { outcome: "rejected", detail: "user not allowed" }));
    return res.status(200).json({ ok: true });
  }

  const ctx = { chatId: resolved.chatId, userId: resolved.userId, updateId: update.update_id };
  try {
    if (resolved.channel === "personal") await personalChannel.handle(message, ctx);
    else await engineeringChannel.handle(message, ctx);
  } catch (e) {
    telegramLog.record(Object.assign({ channel: resolved.channel }, base, { outcome: "error", error: e.message }));
  }
  res.status(200).json({ ok: true });
});

module.exports = router;
