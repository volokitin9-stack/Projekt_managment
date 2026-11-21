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

const router = express.Router();

const actorOf = (req) => dashboardAuth.sessionUser(req) || req.get("x-dashboard-user") || "dashboard_user";

router.get("/proposals", (req, res) => {
  const status = ["open", "resolved", "all"].includes(req.query.status) ? req.query.status : "open";
  res.json({ proposals: proposals.list({ status }), archive: archive.stats() });
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

router.post("/proposals/:id/reject", dashboardAuth.requireCsrfToken, (req, res) => {
  const r = proposals.reject(req.params.id, { actor: actorOf(req), reason: (req.body || {}).reason || null });
  res.status(r.status).json(r.body);
});

module.exports = router;
