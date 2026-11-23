"use strict";

// API for the shturman-ko-analyst agent: /api/agent/ko/* with Authorization: Bearer <KO_ANALYST_API_KEY>.
// Read-only access to the КО chat archive, photo bytes and project reference data, plus exactly one
// write: submitting proposals to the review queue (server/ko/proposals.js). Mounted in
// server/index.js BEFORE the general /api/agent router so it never falls through to AGENT_API_KEY
// routes; every other method/path under this prefix is a 404 here.

const express = require("express");
const { requireKoAnalystAuth } = require("../auth");
const archive = require("../ko/archive");
const proposals = require("../ko/proposals");
const projectsModel = require("../projectsModel");

const router = express.Router();
router.use(requireKoAnalystAuth);

const PROJECT_KEYS = ["id", "external_project_id", "record_category", "project_name", "manufacturer", "vehicle_model",
  "product_type", "article_number", "assigned_engineer", "priority", "status", "planned_stage", "work_status",
  "planned_deadline", "stage_entered_at", "days_in_stage", "next_action", "version", "updated_at"];
const ACTIVITY_KEYS = ["name", "activity_type", "stage", "plan_month", "assignee", "progress_status", "normalized_status",
  "planned_hours", "actual_hours", "comment"];

function pick(o, keys) {
  const out = {};
  keys.forEach((k) => { if (o[k] !== undefined) out[k] = o[k]; });
  return out;
}

router.get("/messages", (req, res) => {
  res.json(archive.listMessages({ after: req.query.after, limit: req.query.limit }));
});

router.get("/files/:id", (req, res) => {
  const f = archive.readFile(req.params.id);
  if (!f) return res.status(404).json({ error: "Not found" });
  res.set({ "Content-Type": f.meta.mime_type, "Content-Length": String(f.buffer.length),
    "Content-Disposition": `attachment; filename="${f.meta.id}${require("path").extname(f.meta.storage_key)}"`,
    "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store" });
  res.end(f.buffer);
});

router.get("/projects", (req, res) => {
  const list = projectsModel.listProjects({ category: req.query.category || null }).map((p) => Object.assign(pick(p, PROJECT_KEYS),
    { activities: (p.activities || []).map((a) => pick(a, ACTIVITY_KEYS)) }));
  res.json({ projects: list, stages: projectsModel.validStageKeys() });
});

router.get("/proposals", (req, res) => {
  const status = ["open", "resolved", "all"].includes(req.query.status) ? req.query.status : "open";
  res.json({ proposals: proposals.list({ status }) });
});

router.post("/proposals", (req, res) => {
  const body = req.body || {};
  const r = proposals.submit(Array.isArray(body) ? body : body.proposals);
  res.status(r.status).json(r.body);
});

// Processing cursor of the daily analysis (see docs/KO_ANALYST.md "Ежедневный запуск").
router.get("/cursor", (req, res) => res.json(archive.getCursor()));
router.put("/cursor", (req, res) => {
  const b = req.body || {};
  const r = archive.setCursor(b.cursor, b.run_id);
  if (r.error) return res.status(r.error).json({ error: r.message });
  res.json(r);
});

router.use((req, res) => res.status(404).json({ error: "Not found" }));

module.exports = router;
