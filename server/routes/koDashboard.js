"use strict";

// Dashboard side of shturman-ko-analyst proposals: /api/ko/*. Protected by the dashboard session
// (global middleware in server/index.js - /api/ko is NOT in its exemption list, and the agent's
// bearer key is not accepted here). State-changing actions additionally require the CSRF token,
// like "Подключить webhook". Applying goes through server/ko/proposals.js#apply, which reuses the
// same model functions and checks as the regular dashboard routes.

const express = require("express");
const dashboardAuth = require("../dashboardAuth");
const archive = require("../ko/archive");
const proposals = require("../ko/proposals");
const koNotifications = require("../koNotificationsModel");

const router = express.Router();

const actorOf = (req) => dashboardAuth.sessionUser(req) || req.get("x-dashboard-user") || "dashboard_user";

router.get("/proposals", (req, res) => {
  const status = ["open", "resolved", "all"].includes(req.query.status) ? req.query.status : "open";
  res.json({ proposals: proposals.list({ status }), archive: archive.stats() });
});

// Notification bell (критическое требование: "центр уведомлений агента КО", администратор-only -
// this whole router is already mounted under requireAdminRole in server/index.js). GET is read-only;
// both POSTs below mark read state only - they never touch the proposal itself (просмотр не
// подтверждает предложение).
router.get("/notifications", (req, res) => {
  const status = req.query.status === "unread" ? "unread" : "all";
  const limit = req.query.limit ? Number(req.query.limit) : undefined;
  res.json(koNotifications.list({ status, limit }));
});

router.post("/notifications/:id/read", dashboardAuth.requireCsrfToken, (req, res) => {
  const ok = koNotifications.markRead(req.params.id);
  if (!ok) return res.status(404).json({ error: "Not found" });
  res.json({ id: req.params.id, read: true });
});

router.post("/notifications/read-all", dashboardAuth.requireCsrfToken, (req, res) => {
  const n = koNotifications.markAllRead();
  res.json({ marked: n });
});

// Photo preview for the review screen (session-gated; not a public URL).
router.get("/files/:id", (req, res) => {
  const f = archive.readFile(req.params.id);
  if (!f) return res.status(404).json({ error: "Not found" });
  res.set({ "Content-Type": f.meta.mime_type, "Content-Length": String(f.buffer.length), "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store", "Content-Security-Policy": "default-src 'none'; img-src 'self'; sandbox" });
  res.end(f.buffer);
});

router.post("/proposals/:id/apply", dashboardAuth.requireCsrfToken, (req, res) => {
  const b = req.body || {};
  const r = proposals.apply(req.params.id, { actor: actorOf(req), version: b.version != null ? Number(b.version) : null });
  res.status(r.status).json(r.body);
});

// Explicit, separate action from "apply" (критическое требование: never an automatic side effect
// of acknowledging project_match/needs_clarification) - a person fills in and confirms the
// identity/stage fields themselves; server/ko/proposals.js#createProjectFromProposal validates,
// de-duplicates and creates record_category "main_project" only here.
router.post("/proposals/:id/create-project", dashboardAuth.requireCsrfToken, (req, res) => {
  const r = proposals.createProjectFromProposal(req.params.id, { actor: actorOf(req), fields: req.body || {} });
  res.status(r.status).json(r.body);
});

router.post("/proposals/:id/reject", dashboardAuth.requireCsrfToken, (req, res) => {
  const r = proposals.reject(req.params.id, { actor: actorOf(req), reason: (req.body || {}).reason || null });
  res.status(r.status).json(r.body);
});

// Manual photo attach: fixes the case automatic matching correctly refuses (ambiguous identity) or
// this phase's own bug left unattached - works on a proposal in any state, see
// server/ko/proposals.js#attachPhotosToProject.
router.post("/proposals/:id/attach-photos", dashboardAuth.requireCsrfToken, (req, res) => {
  const r = proposals.attachPhotosToProject(req.params.id, { actor: actorOf(req), projectId: (req.body || {}).project_id || null });
  res.status(r.status).json(r.body);
});

// Read-only diagnostic (критическое требование: "не выполнять массовую автоматическую привязку
// без предварительного отчёта") - never attaches or creates anything itself.
router.get("/photo-backlog", (req, res) => {
  res.json(proposals.photoBacklogReport());
});

module.exports = router;
