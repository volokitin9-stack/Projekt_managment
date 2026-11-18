"use strict";

// Job queue for the Railway + Windows-laptop split deployment (see docs/RAILWAY_DEPLOYMENT.md).
//
// Topology: Railway runs this server with no access to the corporate Excel file.
// The Windows laptop runs the *unchanged* plan_agent package next to the real file.
// This module is the handoff between the two: the server enqueues a job (on a
// manual «Обновить» click or at the scheduled time - see server/planSource/monitor.js),
// the laptop's poll loop (plan_agent/windows_agent.py) claims it, runs
// `python -m plan_agent analyze` / `sync` locally exactly as server/routes/planImports.js's
// runAgent() already does for the local/single-machine deployment, and submits the
// resulting --json report back. The agent's own HTTP client inside plan_agent writes
// project data through this server's existing /api/agent/* API (PM_API_URL pointed at
// Railway instead of 127.0.0.1) - no project-data write path is duplicated here.
//
// One job is active (queued or running) at a time: enqueue() returns the existing
// job instead of creating a second one, so the same workbook is never imported twice
// concurrently. A running job holds a time-limited lease (AGENT_JOB_LEASE_MS); if the
// laptop disconnects and the lease expires before heartbeat() or complete()/fail() is
// called, the job becomes "timed_out" and a fresh enqueue() is possible again - the
// workbook can always be retried without manual cleanup.

const { v4: uuidv4 } = require("uuid");
const store = require("./store");

const LEASE_MS = Number(process.env.AGENT_JOB_LEASE_MS || 5 * 60 * 1000); // 5 minutes
const HEARTBEAT_STALE_MS = Number(process.env.AGENT_HEARTBEAT_STALE_MS || 3 * 60 * 1000); // 3 minutes
const MAX_KEPT = 200; // trim old terminal jobs so agent_jobs does not grow forever

function nowIso() {
  return new Date().toISOString();
}

function clone(x) {
  return x ? JSON.parse(JSON.stringify(x)) : x;
}

function activeJob(s) {
  return s.agent_jobs.find((j) => j.status === "queued" || j.status === "running");
}

/** Mark any "running" job whose lease has expired as timed_out. Mutates s in place. Returns true if changed. */
function sweepExpired(s) {
  const now = Date.now();
  let changed = false;
  s.agent_jobs.forEach((j) => {
    if (j.status === "running" && j.lease_expires_at && new Date(j.lease_expires_at).getTime() < now) {
      j.status = "timed_out";
      j.error = "lease expired: the agent did not heartbeat or submit a result before its lease ran out " +
        "(laptop likely lost its connection or the process stopped)";
      j.updated_at = nowIso();
      changed = true;
    }
  });
  return changed;
}

function trim(s) {
  if (s.agent_jobs.length <= MAX_KEPT) return;
  const terminal = ["completed", "failed", "timed_out"];
  const keepFrom = s.agent_jobs.length - MAX_KEPT;
  s.agent_jobs = s.agent_jobs.filter((j, i) => i >= keepFrom || !terminal.includes(j.status));
}

/** Create a job unless one is already queued or running for this source; idempotent by design -
 *  a second click of «Обновить» while a job is in flight just returns the same job. */
function enqueue({ type = "check", trigger = "manual", scheduledDate = null, scheduledFor = null,
  force = false, requestedBy = null, month = null } = {}) {
  return store.tx((s) => {
    sweepExpired(s);
    const existing = activeJob(s);
    if (existing) return { created: false, job: clone(existing) };
    const job = {
      id: uuidv4(), type, trigger, scheduled_date: scheduledDate, scheduled_for: scheduledFor,
      force: !!force, requested_by: requestedBy, month,
      status: "queued", created_at: nowIso(), updated_at: nowIso(),
      claimed_at: null, lease_expires_at: null, heartbeat_at: null,
      attempts: 0, result: null, error: null
    };
    s.agent_jobs.push(job);
    trim(s);
    return { created: true, job: clone(job) };
  });
}

/** Used only by server/planSource/monitor.js to chain a "sync" job directly after the "check" job
 *  whose analyze result triggered it (auto-sync path) or after a human's approval click (deferred-
 *  approval path) - called while the *previous* job is still technically "running", so it must skip
 *  enqueue()'s active-job guard (that guard is for external callers like the «Обновить» button, not
 *  for the server's own pipeline chaining one job into the next under the same held lock). */
function enqueueChained({ type, trigger, requestedBy = null, versionId = null, month = null } = {}) {
  return store.tx((s) => {
    const job = {
      id: uuidv4(), type, trigger, scheduled_date: null, scheduled_for: null,
      force: true, requested_by: requestedBy, version_id: versionId, month,
      status: "queued", created_at: nowIso(), updated_at: nowIso(),
      claimed_at: null, lease_expires_at: null, heartbeat_at: null,
      attempts: 0, result: null, error: null
    };
    s.agent_jobs.push(job);
    trim(s);
    return { created: true, job: clone(job) };
  });
}

/** Agent polls this. Claims the oldest queued job and starts its lease. One at a time: the store's
 *  own tx() is synchronous and single-writer (see store.js), so two concurrent polls cannot both
 *  claim the same job. */
function claimNext() {
  return store.tx((s) => {
    sweepExpired(s);
    const job = s.agent_jobs.find((j) => j.status === "queued");
    if (!job) return null;
    job.status = "running";
    job.claimed_at = nowIso();
    job.heartbeat_at = nowIso();
    job.lease_expires_at = new Date(Date.now() + LEASE_MS).toISOString();
    job.attempts += 1;
    job.updated_at = nowIso();
    return clone(job);
  });
}

function heartbeat(id) {
  return store.tx((s) => {
    const job = s.agent_jobs.find((j) => j.id === id);
    if (!job) return { ok: false, code: 404, error: "job not found" };
    if (job.status !== "running") return { ok: false, code: 409, error: `job is ${job.status}, not running` };
    job.heartbeat_at = nowIso();
    job.lease_expires_at = new Date(Date.now() + LEASE_MS).toISOString();
    job.updated_at = nowIso();
    return { ok: true, job: clone(job) };
  });
}

/** result: free-form payload the caller (server/planSource/monitor.js's applyRemoteReport) attaches
 *  after it has finished applying the agent's report through the existing import/workflow pipeline. */
function complete(id, result) {
  return store.tx((s) => {
    const job = s.agent_jobs.find((j) => j.id === id);
    if (!job) return { ok: false, code: 404, error: "job not found" };
    if (job.status !== "running") return { ok: false, code: 409, error: `job is ${job.status}, cannot complete` };
    job.status = "completed";
    job.result = result || null;
    job.updated_at = nowIso();
    return { ok: true, job: clone(job) };
  });
}

function fail(id, error) {
  return store.tx((s) => {
    const job = s.agent_jobs.find((j) => j.id === id);
    if (!job) return { ok: false, code: 404, error: "job not found" };
    if (job.status !== "running" && job.status !== "queued") return { ok: false, code: 409, error: `job is ${job.status}` };
    job.status = "failed";
    job.error = String(error || "unknown error").slice(0, 4000);
    job.updated_at = nowIso();
    return { ok: true, job: clone(job) };
  });
}

function get(id) {
  return store.read((s) => clone(s.agent_jobs.find((j) => j.id === id)) || null);
}

function recent(limit = 20) {
  return store.read((s) => s.agent_jobs.slice(-limit).reverse().map(clone));
}

/** Transition any "running" job whose lease has expired to "timed_out" right now, instead of
 *  waiting for the next claimNext()/enqueue() to do it as a side effect. monitor.js's
 *  reconcileStuckRemoteLock() calls this so a laptop that silently disappears is reflected in
 *  /api/plan-source/status (and so the global import lock it was holding gets freed) the next
 *  time anyone - even just the dashboard polling for status - looks, not only on the next job. */
function sweep() {
  return store.tx((s) => { sweepExpired(s); return null; });
}

/** Called once at server boot: a restart mid-run must not leave a job "running" forever past its
 *  lease just because no one polled sweepExpired() while the process was down. */
function recoverAtBoot() {
  sweep();
}

// ---- agent presence (independent of any specific job: the laptop polls even when idle) ----

function recordPing(meta) {
  store.tx((s) => {
    s.agent_status = Object.assign({}, s.agent_status, { last_seen_at: nowIso() }, meta || {});
    return null;
  });
}

function presence() {
  return store.read((s) => {
    const st = s.agent_status || {};
    const lastSeen = st.last_seen_at ? new Date(st.last_seen_at).getTime() : null;
    const online = lastSeen !== null && (Date.now() - lastSeen) < HEARTBEAT_STALE_MS;
    return Object.assign({}, st, { online, stale_after_ms: HEARTBEAT_STALE_MS });
  });
}

module.exports = {
  LEASE_MS, HEARTBEAT_STALE_MS,
  enqueue, enqueueChained, claimNext, heartbeat, complete, fail, get, recent, recoverAtBoot, sweep,
  recordPing, presence,
  activeJob: () => store.read((s) => clone(activeJob(s)) || null) // always present in JSON, never just omitted
};
