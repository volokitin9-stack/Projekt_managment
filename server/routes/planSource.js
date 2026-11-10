"use strict";

// Status and controls of the automatic plan retrieval («Импорт плана» view).
// Like the other dashboard routes, unauthenticated by design of the original
// app - put access control in front of the server before exposing it.

const express = require("express");
const monitor = require("../planSource/monitor");

const router = express.Router();

router.get("/status", (req, res) => {
  res.json(monitor.status());
});

router.get("/preview", (req, res) => {
  const p = monitor.preview(req.query.version || null);
  if (!p) return res.status(404).json({ error: "Нет обработанной версии файла" });
  res.json(p);
});

// POST /api/plan-source/check {dry_run: true?} - check the source now.
// dry_run: analyse the current file again even if it is unchanged (never writes).
router.post("/check", async (req, res) => {
  const r = await monitor.check({ trigger: "manual", force: !!(req.body && req.body.dry_run) });
  res.status(r.status === "busy" ? 409 : 200).json(Object.assign({ result: r }, { state: monitor.status() }));
});

// POST /api/plan-source/approve {confirm: true, approved_by} - mode A: approve and sync the pending version.
router.post("/approve", async (req, res) => {
  if (!req.body || req.body.confirm !== true) {
    return res.status(400).json({ error: "Требуется явное подтверждение: {\"confirm\": true}" });
  }
  const by = String(req.body.approved_by || "dashboard_user").slice(0, 80);
  const r = await monitor.approvePending(by);
  const code = r.status === "synced" ? 200 : r.status === "busy" ? 409 : r.status === "nothing_pending" ? 409 : 422;
  res.status(code).json(Object.assign({ result: r }, { state: monitor.status() }));
});

// POST /api/plan-source/auto-sync {enabled: bool, confirm: true}
router.post("/auto-sync", (req, res) => {
  if (!req.body || typeof req.body.enabled !== "boolean" || req.body.confirm !== true) {
    return res.status(400).json({ error: "Ожидается {\"enabled\": true|false, \"confirm\": true}" });
  }
  monitor.setAutoSync(req.body.enabled);
  res.json(monitor.status());
});

module.exports = router;
