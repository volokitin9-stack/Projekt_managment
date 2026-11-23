"use strict";

require("./env");   // .env from the project folder, backslashes kept literally

// Must run before ./seed (or anything else that touches ./store) is required: store.js takes the
// data-dir lock and loads/creates shturman.json as a side effect of being required, so a staged
// database has to be swapped in first or that require would load/lock the pre-migration file.
try {
  require("./migrate").runStagingMigrationIfRequested();
} catch (e) {
  console.error(`[migrate] staging migration failed - server will not start: ${e.message}`);
  process.exit(1);
}

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
const agentJobsRouter = require("./routes/agentJobs");
const telegramWebhookRouter = require("./routes/telegramWebhook");
const telegramAdminRouter = require("./routes/telegramAdmin");
const usersRouter = require("./routes/users");
const planScheduler = require("./planSource/scheduler");
const telegramDailyDigest = require("./telegram/dailyDigest");
const store = require("./store");
const agentJobs = require("./agentJobs");
const dashboardAuth = require("./dashboardAuth");

seed();
// maintenance of review items created by an earlier build (resolved, never deleted)
require("./workflowModel").resolveStageItemsOfNonMain(store);
// a restart must not leave a Windows-agent job "running" forever past its lease (Railway deployment only)
agentJobs.recoverAtBoot();

const app = express();
const PORT = process.env.PORT || 3000;

const corsOrigins = (process.env.CORS_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
if (corsOrigins.length) {
  app.use(cors({ origin: corsOrigins }));
}

app.use(express.json({ limit: "2mb" }));

// Dashboard session auth (Railway: set DASHBOARD_USERNAME + DASHBOARD_PASSWORD; see
// docs/RAILWAY_DEPLOYMENT.md). A no-op for the existing local/single-machine install when unset -
// server/dashboardAuth.js. Agent routes below have their own, separate auth and are not covered by
// this - a human operator's session and the Windows agent's credential are deliberately different.
app.post("/api/login", express.json(), dashboardAuth.login);
app.post("/api/logout", dashboardAuth.logout);
app.get("/api/whoami", dashboardAuth.whoAmI);
app.use((req, res, next) => {
  // /api/telegram/webhook has its own secret (X-Telegram-Bot-Api-Secret-Token, see
  // server/routes/telegramWebhook.js) - Telegram can never present this session's cookie. Every
  // other /api/telegram/* path (the "Интеграции → Telegram" status/log reads) stays session-gated.
  if (req.path.startsWith("/api/agent") || req.path === "/api/health" || req.path === "/login.html" ||
      req.path === "/api/telegram/webhook") return next();
  dashboardAuth.requireDashboardSession(req, res, next);
});

// Dashboard-facing document collections (tasks/globals board data) - the administrator's own
// personal Kanban, not "Конструкторский отдел". Критическое требование (роль ko_viewer):
// "Доступ к личным задачам администратора" is forbidden, so these are administrator-only.
app.use("/api/tasks", dashboardAuth.requireAdminRole, buildDocRouter("tasks"));
app.use("/api/globals", dashboardAuth.requireAdminRole, buildDocRouter("globals"));

// Dashboard-facing read API + config for engineering projects - "Конструкторский отдел" itself.
// GET (viewing the board/cards/stages) is allowed for both the administrator and a ko_viewer
// manager; every mutation on /api/projects is administrator-only, enforced inside
// server/routes/projects.js itself (adminOnlyForMutations) rather than here, since this router
// mixes both. /api/stages has no mutations to gate.
app.use("/api/projects", projectsRouter);
app.use("/api/stages", stagesRouter);
// Everything below is an administrative workflow (review queue, plan import/source control,
// Telegram settings, user management) - критическое требование: "Доступ к интеграциям,
// Telegram-архиву, настройкам, импорту и управлению пользователями" and "к закрытым
// административным API" are both forbidden for ko_viewer, so these routers are
// administrator-only in full, with no per-route carve-out.
app.use("/api/review-queue", dashboardAuth.requireAdminRole, reviewQueueRouter);
app.use("/api/plan-imports", dashboardAuth.requireAdminRole, planImportsRouter);
app.use("/api/plan-source", dashboardAuth.requireAdminRole, planSourceRouter);
// Windows-agent job queue (Railway deployment only) - its own auth (WINDOWS_AGENT_API_KEY +
// replay/rate-limit guard), not the dashboard session above. See server/routes/agentJobs.js.
app.use("/api/agent-jobs", agentJobsRouter);

// Telegram integration (both channels - see server/telegram/). /webhook is public with its own
// secret (exempted above); /status, /log and /webhook/connect are administrator-only.
app.use("/api/telegram", telegramWebhookRouter);
app.use("/api/telegram", dashboardAuth.requireAdminRole, telegramAdminRouter);

// "Пользователи" - administrator-only (server/routes/users.js enforces CSRF per mutation itself).
app.use("/api/users", dashboardAuth.requireAdminRole, usersRouter);

// Synchronisation runs recorded by the planning agent (read-only for the dashboard, but still an
// administrative/operational view - administrator-only, same as plan-source/plan-imports above).
app.get("/api/sync-runs", dashboardAuth.requireAdminRole, (req, res) => {
  res.json(agentRouter.listSyncRuns());
});
app.get("/api/sync-runs/:id", dashboardAuth.requireAdminRole, (req, res) => {
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
// external object store; URLs returned by the API would then be absolute). Критическое
// требование (роль ko_viewer): "Скачивание оригинальных фотографий и документов" forbidden - a
// manager views photos/files through GET /api/projects/:id/files/:fileId/view instead (inline,
// server/routes/projects.js), which this mount deliberately does not replace for anyone else (the
// administrator keeps using these URLs directly, unchanged).
app.use("/uploads", (req, res, next) => {
  if (req.authRole === "ko_viewer") return res.status(403).json({ error: "Forbidden" });
  next();
}, express.static(storage.UPLOADS_DIR));

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
    // Daily personal-task digest to TELEGRAM_OWNER_CHAT_ID, independent of the above (separate
    // schedule, separate idempotency state - see server/telegram/dailyDigest.js).
    telegramDailyDigest.start();
    const py = require("./pythonResolver").getPython();
    (py.warnings || []).forEach((w) => console.warn(`ВНИМАНИЕ: ${w}`)); // eslint-disable-line no-console
    if (py.ok) console.log(`Python для агента планирования: ${py.executable} (${py.source})`); // eslint-disable-line no-console
    else console.error(`ВНИМАНИЕ: ${py.error}. Проверено: ${py.attempted.join("; ")}`); // eslint-disable-line no-console
  });
}

module.exports = app;
