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

// Agent-facing write API — everything under here requires AGENT_API_KEY auth (see server/auth.js).
app.use("/api/agent", agentRouter);

app.get("/api/health", (req, res) => {
  res.json({ ok: true, time: new Date().toISOString() });
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
    return res.status(413).json({ error: `File exceeds the ${storage.MAX_UPLOAD_BYTES} byte limit` });
  }
  console.error(err); // eslint-disable-line no-console
  res.status(500).json({ error: "Internal server error" });
});

app.listen(PORT, () => {
  console.log(`Штурман dashboard listening on http://localhost:${PORT}`); // eslint-disable-line no-console
});
