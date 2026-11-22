"use strict";

// Admin interface for importing personal tasks from an old board export (server/taskImport.js).
// Mounted at /api/admin/task-import, behind the dashboard session (global middleware in
// server/index.js - not exempted, and no agent key is accepted). Writes additionally require the
// CSRF token. Works on Railway with the server running: no shell, no manual edit of shturman.json.

const express = require("express");
const dashboardAuth = require("../dashboardAuth");
const taskImport = require("../taskImport");

const router = express.Router();
const actorOf = (req) => dashboardAuth.sessionUser(req) || "dashboard_user";
const fileName = (b) => (typeof b.file_name === "string" ? b.file_name.slice(0, 200) : null);

router.get("/", (req, res) => res.json({ imports: taskImport.list() }));

// dry run - never writes
router.post("/analyze", (req, res) => {
  const b = req.body || {};
  const r = taskImport.analyze(b.export, { fileName: fileName(b) });
  if (r.error) return res.status(400).json(r);
  res.json(r);
});

router.post("/apply", dashboardAuth.requireCsrfToken, (req, res) => {
  const b = req.body || {};
  const r = taskImport.apply(b.export, { planHash: b.plan_hash, actor: actorOf(req), fileName: fileName(b) });
  res.status(r.status).json(r.body);
});

router.post("/:id/rollback", dashboardAuth.requireCsrfToken, (req, res) => {
  const b = req.body || {};
  const r = taskImport.rollback(req.params.id, { force: b.force === true, dryRun: b.dry_run === true, actor: actorOf(req) });
  res.status(r.status).json(r.body);
});

module.exports = router;
