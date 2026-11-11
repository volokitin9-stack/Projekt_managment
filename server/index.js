"use strict";

require("dotenv").config();

const path = require("path");
const express = require("express");
const cors = require("cors");

const { seed } = require("./seed");
const storage = require("./storage");
const { buildDocRouter } = require("./routes/docCollection");
const projectsRouter = require("./routes/projects");
const stagesRouter = require("./routes/stages");
const agentRouter = require("./routes/agent");
const reviewQueueRouter = require("./routes/reviewQueue");
const planImportsRouter = require("./routes/planImports");
const planSourceRouter = require("./routes/planSource");
const planScheduler = require("./planSource/scheduler");
const store = require("./store");

seed();

const app = express();
const PORT = process.env.PORT || 3000;

const corsOrigins = (process.env.CORS_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
if (corsOrigins.length) {
  app.use(cors({ origin: corsOrigins }));
}

app.use(express.json({ limit: "2mb" }));

// Dashboard-facing document collections (tasks/globals board data).
app.use("/api/tasks", buildDocRouter("tasks"));
app.use("/api/globals", buildDocRouter("globals"));

// Dashboard-facing read API + config for engineering projects.
app.use("/api/projects", projectsRouter);
app.use("/api/stages", stagesRouter);
app.use("/api/review-queue", reviewQueueRouter);
app.use("/api/plan-imports", planImportsRouter);
app.use("/api/plan-source", planSourceRouter);

// Synchronisation runs recorded by the planning agent (read-only for the dashboard).
app.get("/api/sync-runs", (req, res) => {
  res.json(agentRouter.listSyncRuns());
});
app.get("/api/sync-runs/:id", (req, res) => {
  const run = agentRouter.listSyncRuns().find((r) => r.id === req.params.id);
  if (!run) return res.status(404).json({ error: "Not found" });
  res.json(Object.assign({}, run, { report: store.loadRunReport(run.id) }));
});

// Agent-facing write API — everything under here requires AGENT_API_KEY auth (see server/auth.js).
app.use("/api/agent", agentRouter);

app.get("/api/health", (req, res) => {
  res.json({ ok: true, time: new Date().toISOString(), schema_version: store.schemaVersion(), storage: "json" });
});

// Uploaded files (see server/storage.js — swap this mount if you move to an
// external object store; URLs returned by the API would then be absolute).
app.use("/uploads", express.static(storage.UPLOADS_DIR));

// The migrated dashboard itself.
app.use(express.static(path.join(__dirname, "..", "public")));

app.use((err, req, res, next) => { // eslint-disable-line no-unused-vars
  if (err && err.type === "entity.parse.failed") {
    return res.status(400).json({ error: "Invalid JSON body" });
  }
  if (err && err.message && /File too large/i.test(err.message)) {
    return res.status(413).json({ error: "File exceeds the upload size limit" });
  }
  console.error(err); // eslint-disable-line no-console
  res.status(500).json({ error: "Internal server error" });
});

if (require.main === module) {
  app.listen(PORT, () => {
    console.log(`Штурман dashboard listening on http://localhost:${PORT}`); // eslint-disable-line no-console
    // Daily working-day plan check: active only when PLAN_SOURCE_TYPE and PLAN_SOURCE_PATH are set
    // and PLAN_AUTO_MONITOR_ENABLED / PLAN_SCHEDULE_ENABLED are not false.
    planScheduler.start();
  });
}

module.exports = app;
