"use strict";

// Agent-facing job-queue endpoints for the Railway + Windows-laptop split deployment.
// Mounted at /api/agent-jobs, protected by requireWindowsAgentAuth (a distinct credential
// from the project-data AGENT_API_KEY - see server/auth.js for why, and the replay/rate-limit
// rules enforced on every request here).
//
// The dashboard never calls this router directly: POST /api/plan-source/update and the 09:00
// scheduler (server/planSource/monitor.js) create jobs; the dashboard reads their state through
// the existing /api/plan-source/status response's `remote_agent` field.

const express = require("express");
const { requireWindowsAgentAuth } = require("../auth");
const agentJobs = require("../agentJobs");
const monitor = require("../planSource/monitor");
const router = express.Router();

router.use(requireWindowsAgentAuth);

// GET /api/agent-jobs/agent/next - poll. Every poll counts as a heartbeat (presence), whether or
// not a job is returned, so the dashboard's agent-online indicator reflects a laptop that is up
// and polling even on a day with nothing to do.
router.get("/agent/next", (req, res) => {
  agentJobs.recordPing({ agent_version: req.get("x-agent-version") || null });
  const job = agentJobs.claimNext();
  res.json({ job });
});

// POST /api/agent-jobs/agent/:id/heartbeat - keep a long-running analyze/sync job's lease alive.
router.post("/agent/:id/heartbeat", (req, res) => {
  agentJobs.recordPing({});
  const r = agentJobs.heartbeat(req.params.id);
  res.status(r.ok ? 200 : (r.code || 409)).json(r);
});

// POST /api/agent-jobs/agent/:id/result
// body: { type: "analyze" | "sync", sha256, size, mtime, name, month,
//         analyzeJson?, syncJson?, launchError?, autoApply?, by? }
//
// type "analyze": the agent ran `python -m plan_agent analyze ... --json` locally and is reporting
//   the result, same shape runAgent() already produces for a local deployment. Unchanged-from-last-
//   time is a normal, successful outcome here, not an error - see monitor.applyRemoteAnalyze().
// type "sync": the agent ran `python -m plan_agent sync ... --json` (auto-safe routine sync, or a
//   human-approved deferred approval - see monitor.enqueueRemoteApproval()).
router.post("/agent/:id/result", async (req, res) => {
  agentJobs.recordPing({});
  const id = req.params.id;
  const job = agentJobs.get(id);
  if (!job) return res.status(404).json({ error: "job not found" });
  if (job.status !== "running") return res.status(409).json({ error: `job is ${job.status}, not running` });
  const body = req.body || {};
  if (body.type !== "analyze" && body.type !== "sync") {
    return res.status(400).json({ error: 'body.type must be "analyze" or "sync"' });
  }
  try {
    let result;
    if (body.type === "analyze") {
      if (!body.sha256 || !body.name || !body.month) {
        return res.status(400).json({ error: "analyze result requires sha256, name and month" });
      }
      result = await monitor.applyRemoteAnalyze({
        sha256: body.sha256, size: body.size || 0, mtime: body.mtime || null, name: body.name, month: body.month,
        analyzeJson: body.analyzeJson || null, launchError: body.launchError || null, autoApply: body.autoApply
      });
    } else {
      if (!job.version_id && !body.versionId) {
        return res.status(400).json({ error: "sync result requires a version_id (set on the job, or body.versionId)" });
      }
      result = await monitor.applyRemoteSync({
        versionId: job.version_id || body.versionId, syncJson: body.syncJson || null,
        by: body.by || job.requested_by || "windows_agent", autoSafe: body.autoApply !== false && job.trigger !== "approve"
      });
    }
    agentJobs.complete(id, { outcome: result.status });
    res.json({ ok: true, result });
  } catch (e) {
    agentJobs.fail(id, e.message || String(e));
    res.status(500).json({ ok: false, error: e.message || String(e) });
  }
});

// POST /api/agent-jobs/agent/:id/fail - the agent could not complete the job (e.g. the workbook is
// open and locked past its retry budget, or the laptop's own plan_agent CLI crashed) and is giving
// up on this attempt rather than let the lease time out silently.
router.post("/agent/:id/fail", (req, res) => {
  agentJobs.recordPing({});
  const r = agentJobs.fail(req.params.id, (req.body && req.body.error) || "agent reported failure");
  res.status(r.ok ? 200 : (r.code || 409)).json(r);
});

module.exports = router;
