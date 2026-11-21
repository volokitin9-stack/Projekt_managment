"use strict";

// Status + log for the "Интеграции → Telegram" dashboard page (ТЗ §9). Session-protected like the
// rest of the dashboard-facing API (dashboardAuth's default middleware covers this path, unlike
// /api/telegram/webhook) - never exposes TELEGRAM_BOT_TOKEN/TELEGRAM_WEBHOOK_SECRET/
// ANTHROPIC_API_KEY, only whether each is configured.

const express = require("express");
const store = require("../store");
const telegramLog = require("../telegram/log");
const aiClient = require("../telegram/aiClient");
const botApi = require("../telegram/botApi");
const reviewQueue = require("../reviewQueueModel");
const dailyDigest = require("../telegram/dailyDigest");
const dashboardAuth = require("../dashboardAuth");

const router = express.Router();

// Критическое требование: setWebhook's target is a fixed, reviewed value - not a new env var and
// not anything a request body can influence (ТЗ §3-4, "Доработка Telegram webhook"). Changing the
// deployed domain means changing this constant, the same deliberate step as changing any other
// hardcoded URL in this codebase.
const WEBHOOK_URL = "https://projektmanagment-production.up.railway.app/api/telegram/webhook";

function allowedUsers(csv) {
  return String(csv || "").split(",").map((s) => s.trim()).filter(Boolean);
}

function webhookState() {
  return store.read((s) => Object.assign({}, s.telegram_webhook));
}

function setWebhookState(fields) {
  store.tx((s) => { s.telegram_webhook = Object.assign({}, s.telegram_webhook, fields); });
}

/** The webhook status shown on "Интеграции → Telegram" - never includes a secret, only the last
 *  outcome of a registration attempt (ok/error, Telegram's own getWebhookInfo fields, when it was
 *  last checked). */
function webhookStatus() {
  const st = webhookState();
  return {
    url: WEBHOOK_URL,
    connected: st.ok === true && st.telegram_url === WEBHOOK_URL,
    last_checked_at: st.last_checked_at || null,
    last_ok: st.ok != null ? st.ok : null,
    last_error: st.error || null,
    telegram_pending_update_count: st.telegram_pending_update_count != null ? st.telegram_pending_update_count : null,
    telegram_last_error_message: st.telegram_last_error_message || null,
    telegram_last_error_date: st.telegram_last_error_date || null
  };
}

router.get("/status", (req, res) => {
  const personal = telegramLog.summary("personal");
  const engineering = telegramLog.summary("engineering");
  const stageProposals = reviewQueue.list({ kind: "telegram_stage_suggestion" }).length;
  const unassignedPhotos = reviewQueue.list({ kind: "telegram_photo" }).length;
  const ambiguousProjects = reviewQueue.list({ kind: "telegram_ambiguous_project" }).length;
  const photosSaved = store.read((s) => s.project_files.filter((f) => f.source === "telegram" && f.kind === "image").length);
  // Критическое требование: messages that couldn't be processed because the AI was unavailable
  // (e.g. no Anthropic credit) are never dropped - personalChannel.js/engineeringChannel.js queue
  // them as review_queue kind "telegram_pending" instead. Surfaced here per channel so this isn't
  // invisible outside the generic review-queue table.
  const aiPending = reviewQueue.list({ kind: "telegram_pending" });
  const aiPendingFor = (channel) => aiPending.filter((r) => (r.payload || {}).channel === channel);
  const personalAiPending = aiPendingFor("personal");
  const engineeringAiPending = aiPendingFor("engineering");
  res.json({
    bot_configured: botApi.configured(),
    ai_configured: aiClient.configured(),
    webhook_secret_configured: !!process.env.TELEGRAM_WEBHOOK_SECRET,
    personal: {
      connected: !!(process.env.TELEGRAM_OWNER_CHAT_ID && botApi.configured()),
      allowed_users: allowedUsers(process.env.TELEGRAM_OWNER_USER_ID),
      last_command_at: personal.last_message_at,
      last_action: personal.last ? `${personal.last.outcome}${personal.last.detail ? ": " + personal.last.detail : ""}` : null,
      tasks_created: personal.by_outcome.created || 0,
      processing_errors: personal.errors,
      ai_unavailable_pending: personalAiPending.length,
      ai_unavailable_last_at: personalAiPending.length ? personalAiPending[personalAiPending.length - 1].updated_at : null
    },
    engineering: {
      connected: !!(process.env.TELEGRAM_ENGINEERING_CHAT_ID && botApi.configured()),
      chat_id: process.env.TELEGRAM_ENGINEERING_CHAT_ID || null,
      last_message_at: engineering.last_message_at,
      messages_processed: engineering.processed,
      events_recorded: engineering.by_outcome.updated || 0,
      photos_saved: photosSaved,
      stage_proposals_pending: stageProposals,
      unassigned_photos_pending: unassignedPhotos,
      ambiguous_projects_pending: ambiguousProjects,
      processing_errors: engineering.errors,
      ai_unavailable_pending: engineeringAiPending.length,
      ai_unavailable_last_at: engineeringAiPending.length ? engineeringAiPending[engineeringAiPending.length - 1].updated_at : null
    },
    daily_digest: dailyDigest.status(),
    webhook: webhookStatus()
  });
});

router.get("/log", (req, res) => {
  const channel = req.query.channel === "personal" || req.query.channel === "engineering" ? req.query.channel : null;
  const limit = Math.min(Number(req.query.limit) || 50, 500);
  res.json({ entries: telegramLog.recent(channel, limit) });
});

// "Подключить webhook" button on "Интеграции → Telegram" - registers WEBHOOK_URL with Telegram's
// setWebhook, then verifies it via getWebhookInfo. Session-gated like every other route in this
// file (the global middleware in server/index.js - this path isn't the exempted
// "/api/telegram/webhook" literal), plus an explicit CSRF check since this one reaches outside the
// app (defense in depth alongside the session cookie's own SameSite=Lax). Only ever uses
// TELEGRAM_BOT_TOKEN/TELEGRAM_WEBHOOK_SECRET - no new env var, no request-supplied URL or secret.
// Never logs or returns either value; Telegram's own getWebhookInfo response never includes one.
router.post("/webhook/connect", dashboardAuth.requireCsrfToken, async (req, res) => {
  const last_checked_at = new Date().toISOString();
  if (!botApi.configured()) {
    setWebhookState({ ok: false, error: "TELEGRAM_BOT_TOKEN is not configured", last_checked_at });
    return res.status(501).json(webhookStatus());
  }
  const secret = process.env.TELEGRAM_WEBHOOK_SECRET;
  if (!secret) {
    setWebhookState({ ok: false, error: "TELEGRAM_WEBHOOK_SECRET is not configured", last_checked_at });
    return res.status(501).json(webhookStatus());
  }
  try {
    await botApi.setWebhook(WEBHOOK_URL, secret, { allowedUpdates: ["message"], dropPendingUpdates: false });
  } catch (e) {
    setWebhookState({ ok: false, error: e.message, last_checked_at });
    telegramLog.record({ channel: "personal", outcome: "error", error: e.message, detail: "setWebhook failed" });
    return res.status(502).json(webhookStatus());
  }
  let info;
  try {
    info = await botApi.getWebhookInfo();
  } catch (e) {
    setWebhookState({ ok: false, error: `setWebhook succeeded but getWebhookInfo failed: ${e.message}`, last_checked_at });
    telegramLog.record({ channel: "personal", outcome: "error", error: e.message, detail: "getWebhookInfo failed after setWebhook" });
    return res.status(502).json(webhookStatus());
  }
  setWebhookState({
    ok: true, error: null, last_checked_at,
    telegram_url: info.url || null,
    telegram_pending_update_count: info.pending_update_count != null ? info.pending_update_count : null,
    telegram_last_error_message: info.last_error_message || null,
    telegram_last_error_date: info.last_error_date || null
  });
  telegramLog.record({ channel: "personal", outcome: "updated", detail: `webhook registered: ${WEBHOOK_URL}` });
  res.json(webhookStatus());
});

// Test hooks: only with TELEGRAM_DIGEST_TEST_HOOKS=1 (automated tests); absent in normal operation.
if (dailyDigest.TEST_HOOKS) {
  router.post("/digest/_test/clock", (req, res) => {
    dailyDigest.setClock(req.body && req.body.now);
    res.json({ now: dailyDigest.now().toISOString() });
  });
  router.post("/digest/_test/tick", async (req, res) => {
    const t = req.body && req.body.now ? new Date(req.body.now) : dailyDigest.now();
    res.json(await dailyDigest.tick(t));
  });
}

module.exports = router;
