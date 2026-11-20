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

const router = express.Router();

function allowedUsers(csv) {
  return String(csv || "").split(",").map((s) => s.trim()).filter(Boolean);
}

router.get("/status", (req, res) => {
  const personal = telegramLog.summary("personal");
  const engineering = telegramLog.summary("engineering");
  const stageProposals = reviewQueue.list({ kind: "telegram_stage_suggestion" }).length;
  const unassignedPhotos = reviewQueue.list({ kind: "telegram_photo" }).length;
  const ambiguousProjects = reviewQueue.list({ kind: "telegram_ambiguous_project" }).length;
  const photosSaved = store.read((s) => s.project_files.filter((f) => f.source === "telegram" && f.kind === "image").length);
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
      processing_errors: personal.errors
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
      processing_errors: engineering.errors
    },
    daily_digest: dailyDigest.status()
  });
});

router.get("/log", (req, res) => {
  const channel = req.query.channel === "personal" || req.query.channel === "engineering" ? req.query.channel : null;
  const limit = Math.min(Number(req.query.limit) || 50, 500);
  res.json({ entries: telegramLog.recent(channel, limit) });
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
