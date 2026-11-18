"use strict";

// Automatic retrieval of the engineering plan from a configured file source.
//
//   check -> probe source -> stable snapshot copy + SHA-256 -> unchanged? stop
//         -> new version: dry run (the same pipeline as a manual upload)
//         -> manual mode (default): wait for approval in «Импорт плана»
//         -> automatic mode: apply only safe changes (plan_agent sync --auto-safe),
//            hold everything else for review
//
// * One monitor per server process; the store's lock file keeps one process per
//   data directory. A check never overlaps another check or a manual import
//   (shared job lock of routes/planImports).
// * A version is marked "synced" only after the agent reported a completed
//   sync. A restart during a check leaves the version "interrupted"; the next
//   check processes it again (syncs are duplicate-safe).
// * State, versions and events are kept in the JSON store
//   (plan_source, plan_source_versions, plan_source_events).

const fs = require("fs");
const path = require("path");
const { v4: uuidv4 } = require("uuid");
const store = require("../store");
const reviewQueue = require("../reviewQueueModel");
const imports = require("../routes/planImports");
const { createSource, SourceError } = require("./adapters");
const agentJobs = require("../agentJobs");

const REPO_ROOT = path.join(__dirname, "..", "..");
const MAX_EVENTS = 300;
const FINAL = ["synced", "awaiting_approval", "blocked", "failed"];   // same bytes are not processed again
const DEFAULT_TZ = "Asia/Yekaterinburg";
const MAX_RUNS = 1000;

function nowIso() {
  return new Date().toISOString();
}

function bool(v, dflt) {
  if (v === undefined || v === null || v === "") return dflt;
  return ["1", "true", "yes", "on"].includes(String(v).toLowerCase());
}

/** Configuration from the environment (read on every call: tests and restarts see changes). */
function config(env = process.env) {
  const type = (env.PLAN_SOURCE_TYPE || "").trim();
  const p = (env.PLAN_SOURCE_PATH || "").trim();
  const time = (env.PLAN_SCHEDULE_TIME || "09:00").trim();
  return {
    type,
    path: p,
    pattern: (env.PLAN_SOURCE_PATTERN || "").trim() || null,
    // remote_agent needs no local PLAN_SOURCE_PATH - Railway has no access to the corporate file by
    // design; the Windows agent is the only thing that ever reads it (docs/RAILWAY_DEPLOYMENT.md).
    configured: !!(type && (p || type === "remote_agent")),
    monitorEnabled: bool(env.PLAN_AUTO_MONITOR_ENABLED, true),
    scheduleEnabled: bool(env.PLAN_SCHEDULE_ENABLED, true),
    scheduleTime: /^([01]\d|2[0-3]):[0-5]\d$/.test(time) ? time : null,
    scheduleTimeRaw: time,
    graceMinutes: env.PLAN_SCHEDULE_GRACE_MINUTES === undefined ? 30 : Number(env.PLAN_SCHEDULE_GRACE_MINUTES),
    workCalendar: (env.PLAN_WORK_CALENDAR || "RU").trim(),
    settleSeconds: env.PLAN_SOURCE_SETTLE_SECONDS === undefined ? 5 : Number(env.PLAN_SOURCE_SETTLE_SECONDS),
    readAttempts: env.PLAN_SOURCE_READ_ATTEMPTS === undefined ? 3 : Number(env.PLAN_SOURCE_READ_ATTEMPTS),
    retrySeconds: env.PLAN_SOURCE_RETRY_SECONDS === undefined ? 5 : Number(env.PLAN_SOURCE_RETRY_SECONDS),
    autoSyncDefault: bool(env.PLAN_AUTO_SYNC_ENABLED, false),
    // Changes outside the automatic policy always need approval. "false" is not
    // supported (it would bypass review) and is reported as a configuration warning.
    requireApproval: true,
    requireApprovalWarning: bool(env.PLAN_REQUIRE_APPROVAL, true) ? null
      : "PLAN_REQUIRE_APPROVAL=false не поддерживается: изменения вне правил автосинхронизации всегда требуют утверждения",
    targetMonth: (env.PLAN_TARGET_MONTH || "auto").trim() || "auto",
    timezone: (env.PLAN_TIMEZONE || DEFAULT_TZ).trim()
  };
}

/** The reporting month: explicit PLAN_TARGET_MONTH, or the current month in PLAN_TIMEZONE. */
function targetMonth(cfg, now = new Date()) {
  if (cfg.targetMonth && cfg.targetMonth !== "auto") {
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(cfg.targetMonth)) {
      throw new SourceError("not_configured", `PLAN_TARGET_MONTH «${cfg.targetMonth}» — ожидается ГГГГ-ММ или auto`);
    }
    return cfg.targetMonth;
  }
  let parts;
  try {
    parts = new Intl.DateTimeFormat("en-CA", { timeZone: cfg.timezone, year: "numeric", month: "2-digit" }).formatToParts(now);
  } catch (e) {
    throw new SourceError("not_configured", `PLAN_TIMEZONE «${cfg.timezone}» — неизвестный часовой пояс`);
  }
  const y = parts.find((x) => x.type === "year").value;
  const m = parts.find((x) => x.type === "month").value;
  return `${y}-${m}`;
}

// ---------------------------------------------------------------- state helpers
function getState() {
  return store.read((s) => JSON.parse(JSON.stringify(s.plan_source || {})));
}
function setState(fields) {
  store.tx((s) => { s.plan_source = Object.assign({}, s.plan_source || {}, fields); });
}
function logEvent(level, code, message, extra) {
  const ev = Object.assign({ at: nowIso(), level, code, message }, extra || {});
  store.tx((s) => {
    s.plan_source_events.push(ev);
    if (s.plan_source_events.length > MAX_EVENTS) s.plan_source_events.splice(0, s.plan_source_events.length - MAX_EVENTS);
  });
  const line = `[plan-source] ${level.toUpperCase()} ${code}: ${message}`;
  if (level === "error") console.error(line); else console.log(line); // eslint-disable-line no-console
  return ev;
}
function findVersion(sha, month) {
  return store.read((s) => {
    const v = s.plan_source_versions.find((x) => x.sha256 === sha && x.plan_month === month);
    return v ? JSON.parse(JSON.stringify(v)) : null;
  });
}
function updateVersion(id, fields) {
  store.tx((s) => Object.assign(s.plan_source_versions.find((x) => x.id === id), fields, { updated_at: nowIso() }));
  return store.read((s) => JSON.parse(JSON.stringify(s.plan_source_versions.find((x) => x.id === id))));
}
function autoSyncEnabled(cfg) {
  const st = getState();
  return typeof st.auto_sync_override === "boolean" ? st.auto_sync_override : cfg.autoSyncDefault;
}

function reviewItem(key, kind, reason, payload) {
  try {
    reviewQueue.upsert({ kind, payload, reason, source: "plan_source", dedupeKey: key });
  } catch (e) {
    logEvent("error", "review_queue", `не удалось добавить запись на проверку: ${e.message}`);
  }
}

/** Outcome of a dry run -> version status. */
function classifyDryRun(json) {
  const st = (json && json.status) || "";
  if (!json || st === "Failed") return "failed";
  if (/^Verification required/.test(st)) return "blocked";
  if (/^Approval required/.test(st)) return "awaiting_approval";
  if (/dashboard unavailable/.test(st)) return "retry";
  return "ready";   // "Dry run" with an approved, matching mapping
}

// ------------------------------------------------------------------ recovery
const PYTHON_LAUNCH_FAILURE = /Python was not found|Не удалось запустить Python|Python для агента планирования не найден/i;

(function recoverInterrupted() {
  store.tx((s) => {
    // Versions that "failed" only because the agent's Python could not start (earlier versions
    // recorded them as file failures) are processed again once the interpreter is fixed.
    s.plan_source_versions.forEach((v) => {
      if (["failed", "sync_failed"].includes(v.status) && PYTHON_LAUNCH_FAILURE.test(String(v.error || ""))) {
        v.status = "interrupted";
        v.updated_at = nowIso();
      }
    });
    s.plan_source_versions.forEach((v) => {
      if (["fetching", "analyzing", "syncing"].includes(v.status)) {
        v.status = "interrupted";
        v.error = `обработка прервана перезапуском сервера (этап: ${v.phase || v.status}) — будет повторена`;
        v.updated_at = nowIso();
      }
    });
    if (s.plan_source && s.plan_source.running) {
      s.plan_source.running = null;
      s.plan_source.status = "interrupted";
    }
  });
})();

// --------------------------------------------------------------------- check
/**
 * One check of the source. Options:
 *   trigger: "schedule" | "manual" | "startup"
 *   force:   analyse again even if this version was already processed (dry run button)
 *   approve: in manual mode, approve and sync the pending version (full scope, like
 *            «Синхронизировать» in the import view)
 */
async function check(opts = {}) {
  const ph = { check: "not_run", analysis: "not_run", sync: "not_run" };
  const r = await checkInner(opts, ph);
  r.phases = ph;
  return r;
}

async function checkInner({ trigger = "manual", force = false } = {}, ph) {
  const cfg = config();
  const startedAt = nowIso();
  if (!cfg.configured) {
    setState({ status: "not_configured", last_check_at: startedAt });
    return { status: "not_configured" };
  }
  if (!process.env.AGENT_API_KEY) {
    setState({ status: "error", last_check_at: startedAt, last_error: "AGENT_API_KEY не задан — обработка плана недоступна" });
    return { status: "error", error: "AGENT_API_KEY missing" };
  }
  if (!imports.tryLock(`plan_source:${trigger}`)) {
    logEvent("info", "skipped_busy", `проверка пропущена: идёт другая обработка (${imports.jobs.owner})`);
    return { status: "busy", owner: imports.jobs.owner };
  }
  let tmp = null;
  try {
    setState({ running: { started_at: startedAt, trigger, phase: "probe" }, last_check_at: startedAt });
    const month = targetMonth(cfg);
    setState({ current_month: month });
    const source = createSource(cfg.type, { path: cfg.path, baseDir: REPO_ROOT, pattern: cfg.pattern,
      settleSeconds: cfg.settleSeconds, readAttempts: cfg.readAttempts, retrySeconds: cfg.retrySeconds });
    const probe = await source.probe();
    fs.mkdirSync(imports.FILES_DIR, { recursive: true });
    tmp = path.join(imports.FILES_DIR, `.incoming-${uuidv4()}`);
    const snap = await source.fetchTo(tmp, probe);
    ph.check = "ok";
    ph.snapshot = { attempts: snap.attempts, retried: snap.retried, open_in_excel: !!snap.open_in_excel };
    if (snap.retried) {
      logEvent("info", "snapshot_retried", `${snap.name}: снимок получен с попытки ${snap.attempts} (${snap.retried})`);
    }
    setState({ last_successful_check_at: nowIso(), last_check_message: "Проверка файла выполнена" });

    let version = findVersion(snap.sha256, month);
    const auto = autoSyncEnabled(cfg);
    // open_in_excel is informational only; change detection uses the SHA-256 of the saved content.
    const fileInfo = { name: snap.name, size: snap.size, mtime: snap.mtime, sha256: snap.sha256,
      open_in_excel: !!snap.open_in_excel };
    if (version && FINAL.includes(version.status) && !force &&
        !(version.status === "awaiting_approval" && auto && version.dry_run_ready)) {
      fs.unlinkSync(tmp);
      tmp = null;
      setState({ running: null, status: version.status === "synced" ? "up_to_date" : version.status,
        last_check_result: "unchanged", last_file: fileInfo, last_error: null, current_version_id: version.id });
      return { status: "unchanged", version };
    }

    // ---- new (or retried) version: register an import and run the dry run
    const isNew = !version;
    if (isNew) {
      const id = uuidv4();
      store.tx((s) => s.plan_source_versions.push({ id, sha256: snap.sha256, size: snap.size, mtime: snap.mtime,
        name: snap.name, plan_month: month, source: source.describe(), detected_at: nowIso(), updated_at: nowIso(),
        status: "analyzing", phase: "analyze", import_id: null, sync_import_id: null, synced_at: null,
        dry_run_ready: false, counts: null, error: null }));
      version = findVersion(snap.sha256, month);
      const prev = getState().last_file;
      logEvent("info", "new_version", `новая версия файла ${snap.name} (SHA-256 ${snap.sha256.slice(0, 12)}…, ` +
        `${snap.size} байт, изменён ${snap.mtime})` + (prev ? `; предыдущая ${String(prev.sha256).slice(0, 12)}…` : ""),
        { version_id: version.id });
      setState({ last_change_at: nowIso() });
    }
    setState({ last_file: fileInfo });
    const prevStatus = version.status;
    let importId = version.import_id;
    const needAnalysis = isNew || force || !importId || !["analyzed", "sync_failed", "interrupted", "synced"]
      .includes((imports.getImport(importId) || {}).status);
    if (needAnalysis) {
      const stored = imports.newStoredPath(uuidv4());
      fs.renameSync(tmp, stored);
      tmp = null;
      importId = imports.createImport({ storedPath: stored, name: snap.name, sha256: snap.sha256, size: snap.size,
        month, origin: "plan_source", sourceMeta: Object.assign(source.describe(), { mtime: snap.mtime, trigger }) });
      updateVersion(version.id, { status: "analyzing", phase: "analyze", import_id: importId });
      setState({ running: { started_at: startedAt, trigger, phase: "analyze" } });
      const r = await imports.analyzeImport(importId);
      if (r.launchError) {
        // The interpreter could not run - not a problem of the file: keep the version retryable.
        ph.analysis = "failed";
        updateVersion(version.id, { status: "interrupted", phase: null, error: r.launchError });
        logEvent("error", "python_unavailable", r.launchError, { version_id: version.id });
        setState({ running: null, status: "error", last_error: r.launchError, last_check_result: "error" });
        return { status: "error", code: "python_unavailable", error: r.launchError };
      }
      const outcome = classifyDryRun(r.json);
      ph.analysis = outcome === "ready" ? "ok" : outcome;
      const counts = r.json ? r.json.counts : null;
      setState({ last_counts: counts });
      if (outcome === "failed") {
        const err = (r.row && r.row.error) || "анализ не выполнен";
        updateVersion(version.id, { status: "failed", phase: null, error: err, counts });
        reviewItem(`plan_source:${version.sha256}:${month}`, "plan_source_issue",
          `Файл ${snap.name} не обработан: ${err}`, { version_id: version.id, import_id: importId, file: fileInfo, plan_month: month });
        logEvent("error", "analysis_failed", `${snap.name}: ${err}`, { version_id: version.id });
        setState({ running: null, status: "failed", last_error: err, last_check_result: "failed" });
        return { status: "failed", error: err };
      }
      if (outcome === "retry") {
        updateVersion(version.id, { status: "interrupted", phase: null, error: "панель недоступна во время анализа", counts });
        setState({ running: null, status: "error", last_error: "панель недоступна во время анализа", last_check_result: "retry" });
        return { status: "retry" };
      }
      if (outcome === "blocked" || outcome === "awaiting_approval") {
        const notes = ((r.json && r.json.notes) || []).filter((n) => /VERIFICATION|STRUCTURE|INITIAL/.test(n));
        if (r.json && r.json.fatal_error) notes.unshift(r.json.fatal_error);
        const reason = outcome === "blocked"
          ? `Требуется проверка файла ${snap.name} (${month}): ${notes.join("; ") || r.json.status}`
          : `Файл ${snap.name} (${month}) ожидает утверждения сопоставления: ${notes.join("; ") || r.json.status}`;
        updateVersion(version.id, { status: outcome, phase: null, counts, error: null, dry_run_ready: false });
        reviewItem(`plan_source:${version.sha256}:${month}`, "plan_source_issue", reason,
          { version_id: version.id, import_id: importId, file: fileInfo, plan_month: month, notes });
        logEvent(outcome === "blocked" ? "warn" : "info", outcome, reason, { version_id: version.id });
        setState({ running: null, status: outcome, last_error: outcome === "blocked" ? reason : null,
          last_check_result: "changed", current_version_id: version.id, pending_import_id: importId });
        return { status: outcome, version: findVersion(version.sha256, month) };
      }
      updateVersion(version.id, { counts, dry_run_ready: true });
      if (force) {
        // «Пробный анализ»: analysis only, never a write.
        const keep = prevStatus === "synced" ? "synced" : "awaiting_approval";
        updateVersion(version.id, { status: keep, phase: null, error: null });
        if (tmp) { fs.unlinkSync(tmp); tmp = null; }
        setState({ running: null, status: keep === "synced" ? "up_to_date" : "awaiting_approval", last_error: null,
          last_check_result: "dry_run", current_version_id: version.id, pending_import_id: keep === "synced" ? null : importId });
        logEvent("info", "dry_run", `${snap.name} (${month}): пробный анализ выполнен`, { version_id: version.id });
        return { status: "dry_run", version: findVersion(version.sha256, month) };
      }
    }

    if (!auto) {
      updateVersion(version.id, { status: "awaiting_approval", phase: null, error: null });
      logEvent("info", "awaiting_approval", `${snap.name} (${month}): пробный анализ готов, ожидает утверждения`,
        { version_id: version.id });
      setState({ running: null, status: "awaiting_approval", last_error: null, last_check_result: "changed",
        current_version_id: version.id, pending_import_id: importId });
      if (tmp) { fs.unlinkSync(tmp); tmp = null; }
      return { status: "awaiting_approval", version: findVersion(version.sha256, month) };
    }
    if (tmp) { fs.unlinkSync(tmp); tmp = null; }
    return await syncVersion(version.id, { autoSafe: true, by: "plan_source_auto", startedAt, trigger }, ph);
  } catch (e) {
    if (tmp) { try { fs.unlinkSync(tmp); } catch (x) { /* ignore */ } }
    const se = e instanceof SourceError ? e : new SourceError("internal", `${e.name}: ${e.message}`);
    if (ph.check !== "ok") ph.check = "failed";
    const level = se.transient ? "warn" : "error";
    logEvent(level, se.code, se.message);
    if (!se.transient && se.code !== "not_found" && se.code !== "not_configured") {
      reviewItem(`plan_source:error:${se.code}:${se.message.slice(0, 120)}`, "plan_source_issue",
        `Источник плана: ${se.message}`, { code: se.code, details: se.details });
    }
    setState({ running: null, status: se.transient ? "waiting" : "error", last_error: se.message,
      last_error_code: se.code, last_check_result: "error" });
    return { status: se.transient ? "waiting" : "error", code: se.code, error: se.message };
  } finally {
    imports.unlock();
  }
}

/** Sync a version. Caller holds the job lock. */
async function syncVersion(versionId, { autoSafe, by, startedAt, trigger, precomputed = null }, ph) {
  const version = store.read((s) => JSON.parse(JSON.stringify(s.plan_source_versions.find((x) => x.id === versionId))));
  const imp = imports.getImport(version.import_id);
  updateVersion(version.id, { status: "syncing", phase: autoSafe ? "auto_sync" : "sync" });
  setState({ running: { started_at: startedAt || nowIso(), trigger, phase: "sync" } });
  const r = await imports.approveAndSync(imp, by, { autoSafe, precomputed });
  const counts = r.json ? r.json.counts : null;
  if (ph) ph.sync = r.complete ? "ok" : "failed";
  if (r.complete) {
    const v = updateVersion(version.id, { status: "synced", phase: null, synced_at: nowIso(), sync_import_id: imp.id,
      sync_run_id: r.json.run_id, counts, sync_scope: autoSafe ? "automatic" : "full", error: null,
      held_for_review: r.json.held_for_review || 0 });
    logEvent("info", "synced", `${version.name} (${version.plan_month}) синхронизирован` +
      (autoSafe ? ` автоматически; на проверку отложено: ${r.json.held_for_review || 0}` : ""), { version_id: version.id });
    setState({ running: null, status: "up_to_date", last_sync_at: nowIso(), last_counts: counts, last_error: null,
      last_check_result: "synced", current_version_id: version.id, pending_import_id: null });
    return { status: "synced", version: v };
  }
  const err = r.error || (r.row && r.row.error) || "синхронизация не выполнена";
  updateVersion(version.id, { status: "sync_failed", phase: null, error: err, counts });
  logEvent("error", "sync_failed", `${version.name}: ${err}`, { version_id: version.id });
  setState({ running: null, status: "error", last_error: err, last_counts: counts, last_check_result: "sync_failed" });
  return { status: "sync_failed", error: err };
}

/** Manual approval of the pending version (mode A). A version detected through the Windows agent
 *  (source.type === "remote_agent") has no file on this server to re-run `sync` against locally -
 *  approval instead queues a new agent job so the laptop performs the approve+sync itself; see
 *  enqueueRemoteApproval() and the POST /api/agent-jobs/agent/:id/result handler. */
async function approvePending(by) {
  const st = getState();
  const vid = st.current_version_id;
  const version = vid ? store.read((s) => s.plan_source_versions.find((x) => x.id === vid)) : null;
  if (!version || !["awaiting_approval", "sync_failed", "interrupted"].includes(version.status) || !version.import_id) {
    return { status: "nothing_pending" };
  }
  const imp = imports.getImport(version.import_id);
  if (!imp || !["analyzed", "interrupted", "sync_failed"].includes(imp.status)) return { status: "nothing_pending" };
  if (version.source && version.source.type === "remote_agent") {
    return enqueueRemoteApproval(version, by);
  }
  if (!imports.tryLock("plan_source:approve")) return { status: "busy", owner: imports.jobs.owner };
  try {
    return await syncVersion(version.id, { autoSafe: false, by, trigger: "approve" });
  } finally {
    imports.unlock();
  }
}

// ============================================================================================
// Railway + Windows-agent topology: no local file access, the Windows laptop runs the pipeline.
// See server/agentJobs.js for the job lifecycle and docs/RAILWAY_DEPLOYMENT.md for the full flow.
// Both functions below reuse the exact same post-processing checkInner() already has (classifyDryRun,
// review items, version/import bookkeeping, syncVersion) - only the *source* of the --json report
// differs: a locally-run plan_agent CLI there, an agent-submitted report here.
// ============================================================================================

function isRemoteMode(cfg = config()) {
  return cfg.type === "remote_agent";
}

/** POST /api/plan-source/update and the 09:00 scheduler, when PLAN_SOURCE_TYPE=remote_agent:
 *  enqueue a job instead of reading a local file. Holds the same global import/check lock a local
 *  check would hold, released only once applyRemoteAnalyze()/applyRemoteSync() finishes - so a
 *  manual upload or a second remote check still cannot overlap this one. */
function enqueueRemoteCheck({ trigger, scheduledDate = null, scheduledFor = null, force = false } = {}) {
  if (!imports.tryLock(`plan_source:${trigger}`)) {
    logEvent("info", "skipped_busy", `проверка пропущена: идёт другая обработка (${imports.jobs.owner})`);
    return { status: "busy", owner: imports.jobs.owner };
  }
  const cfg = config();
  let month;
  try {
    month = targetMonth(cfg);
  } catch (e) {
    imports.unlock();
    setState({ status: "error", last_error: e.message });
    return { status: "not_configured", error: e.message };
  }
  const { job } = agentJobs.enqueue({ type: "check", trigger, scheduledDate, scheduledFor, force });
  setState({ running: { started_at: nowIso(), trigger, phase: "await_agent", job_id: job.id }, status: "awaiting_agent",
    current_month: month });
  logEvent("info", "job_queued", `задача ${job.id} поставлена в очередь для агента ноутбука (${trigger})`, { job_id: job.id });
  // Intentionally does not call imports.unlock() here: the lock is released by applyRemoteAnalyze()/
  // applyRemoteSync() (success or failure) or by the job-timeout sweep reconciling a stuck "busy" state
  // on the next status()/check() call - see reconcileStuckRemoteLock() below.
  return { status: "queued", job };
}

/** If the job that is holding the global lock timed out (lease expired, laptop went offline) and
 *  nothing ever called applyRemoteAnalyze/applyRemoteSync to release it, free the lock so the next
 *  attempt is not blocked forever. Cheap to call from status(). */
function reconcileStuckRemoteLock() {
  if (!imports.jobs.busy || !String(imports.jobs.owner || "").startsWith("plan_source:")) return;
  agentJobs.sweep(); // a job's lease can have expired since it was last claimed/heartbeated/enqueued
  const st = getState();
  const jobId = st.running && st.running.job_id;
  if (!jobId) return;
  const job = agentJobs.get(jobId);
  if (job && (job.status === "timed_out" || job.status === "failed")) {
    setState({ running: null, status: "error", last_error: job.error || "задача агента прервана" });
    imports.unlock();
  }
}

/** The agent submitted its `analyze` --json report for a job. Mirrors checkInner()'s post-probe
 *  logic (lines ~216-323 above) exactly, with the agent-reported file metadata standing in for a
 *  local source.probe()/fetchTo(). */
async function applyRemoteAnalyze({ sha256, size, mtime, name, month, analyzeJson, launchError, autoApply }) {
  const cfg = config();
  const fileInfo = { name, size, mtime, sha256, open_in_excel: false };
  const auto = autoSyncEnabled(cfg) && autoApply !== false;
  let version = findVersion(sha256, month);
  if (version && FINAL.includes(version.status)) {
    setState({ running: null, status: version.status === "synced" ? "up_to_date" : version.status,
      last_check_result: "unchanged", last_file: fileInfo, last_error: null, current_version_id: version.id,
      last_successful_check_at: nowIso() });
    imports.unlock();
    return { status: "unchanged", version };
  }
  const isNew = !version;
  if (isNew) {
    const id = uuidv4();
    store.tx((s) => s.plan_source_versions.push({ id, sha256, size, mtime, name, plan_month: month,
      source: { type: "remote_agent", location: "windows-laptop" }, detected_at: nowIso(), updated_at: nowIso(),
      status: "analyzing", phase: "analyze", import_id: null, sync_import_id: null, synced_at: null,
      dry_run_ready: false, counts: null, error: null }));
    version = findVersion(sha256, month);
    setState({ last_change_at: nowIso() });
  }
  setState({ last_file: fileInfo, last_successful_check_at: nowIso() });
  if (launchError) {
    updateVersion(version.id, { status: "interrupted", phase: null, error: launchError });
    logEvent("error", "agent_launch_error", launchError, { version_id: version.id });
    setState({ running: null, status: "error", last_error: launchError, last_check_result: "error" });
    imports.unlock();
    return { status: "error", code: "agent_launch_error", error: launchError };
  }
  const importId = imports.createImport({ storedPath: null, name, sha256, size, month, origin: "plan_source_remote",
    sourceMeta: { type: "remote_agent" } });
  updateVersion(version.id, { status: "analyzing", phase: "analyze", import_id: importId });
  const r = await imports.analyzeImport(importId, { json: analyzeJson });
  const outcome = classifyDryRun(r.json);
  const counts = r.json ? r.json.counts : null;
  setState({ last_counts: counts });
  if (outcome === "failed") {
    const err = (r.row && r.row.error) || "анализ не выполнен";
    updateVersion(version.id, { status: "failed", phase: null, error: err, counts });
    reviewItem(`plan_source:${sha256}:${month}`, "plan_source_issue", `Файл ${name} не обработан: ${err}`,
      { version_id: version.id, import_id: importId, file: fileInfo, plan_month: month });
    setState({ running: null, status: "failed", last_error: err, last_check_result: "failed" });
    imports.unlock();
    return { status: "failed", error: err };
  }
  if (outcome === "retry") {
    updateVersion(version.id, { status: "interrupted", phase: null, error: "отчёт агента не распознан", counts });
    setState({ running: null, status: "error", last_error: "отчёт агента не распознан", last_check_result: "retry" });
    imports.unlock();
    return { status: "retry" };
  }
  if (outcome === "blocked" || outcome === "awaiting_approval") {
    const notes = ((r.json && r.json.notes) || []).filter((n) => /VERIFICATION|STRUCTURE|INITIAL/.test(n));
    if (r.json && r.json.fatal_error) notes.unshift(r.json.fatal_error);
    const reason = outcome === "blocked"
      ? `Требуется проверка файла ${name} (${month}): ${notes.join("; ") || r.json.status}`
      : `Файл ${name} (${month}) ожидает утверждения сопоставления: ${notes.join("; ") || r.json.status}`;
    updateVersion(version.id, { status: outcome, phase: null, counts, error: null, dry_run_ready: false });
    reviewItem(`plan_source:${sha256}:${month}`, "plan_source_issue", reason,
      { version_id: version.id, import_id: importId, file: fileInfo, plan_month: month, notes });
    setState({ running: null, status: outcome, last_error: outcome === "blocked" ? reason : null,
      last_check_result: "changed", current_version_id: version.id, pending_import_id: importId });
    imports.unlock();
    return { status: outcome, version: findVersion(sha256, month) };
  }
  updateVersion(version.id, { counts, dry_run_ready: true });
  if (!auto) {
    updateVersion(version.id, { status: "awaiting_approval", phase: null, error: null });
    setState({ running: null, status: "awaiting_approval", last_error: null, last_check_result: "changed",
      current_version_id: version.id, pending_import_id: importId });
    imports.unlock();
    return { status: "awaiting_approval", version: findVersion(sha256, month) };
  }
  // Auto-sync is on: the analyze job is done (it completes normally, below); chain a second,
  // separate "sync" job for the agent to pick up next, carrying the version id. The global lock
  // started by enqueueRemoteCheck() is deliberately NOT released here - it stays held across both
  // jobs and is only released by applyRemoteSync() once the chain finishes, so nothing else (a
  // manual upload, a second check) can interleave between the two.
  updateVersion(version.id, { status: "analyzed", phase: "await_sync", error: null });
  const { job: syncJob } = agentJobs.enqueueChained({ type: "sync", trigger: "schedule", versionId: version.id });
  setState({ running: { started_at: nowIso(), trigger: "schedule", phase: "await_agent_sync", job_id: syncJob.id } });
  logEvent("info", "job_queued", `задача синхронизации ${syncJob.id} поставлена в очередь (авто) после анализа ${version.name}`,
    { job_id: syncJob.id, version_id: version.id });
  return { status: "awaiting_agent_sync", version: findVersion(sha256, month), chained_job: syncJob };
}

/** The agent submitted its `sync` --json report (auto-safe routine sync, or a human-approved
 *  deferred approval - see enqueueRemoteApproval()). Mirrors syncVersion()'s own logic. */
async function applyRemoteSync({ versionId, syncJson, by, autoSafe }) {
  const result = await syncVersion(versionId, { autoSafe: !!autoSafe, by: by || "windows_agent",
    trigger: "remote_agent", precomputed: { json: syncJson } }, {});
  imports.unlock();
  return result;
}

/** A human clicked "approve" in «Импорт плана» for a remote-agent-sourced version: the laptop must
 *  run the approve+sync itself (it still has - or can re-read - the real file; Railway never does). */
function enqueueRemoteApproval(version, by) {
  if (!imports.tryLock("plan_source:approve")) return { status: "busy", owner: imports.jobs.owner };
  const { job } = agentJobs.enqueueChained({ type: "sync", trigger: "approve", requestedBy: by, versionId: version.id });
  setState({ running: { started_at: nowIso(), trigger: "approve", phase: "await_agent_sync", job_id: job.id } });
  logEvent("info", "job_queued", `задача утверждения ${job.id} поставлена в очередь для агента ноутбука`, { job_id: job.id, version_id: version.id });
  return { status: "queued", job };
}

/** Versions synced through «Импорт плана» directly count as synced. */
function reconcile() {
  store.tx((s) => {
    s.plan_source_versions.forEach((v) => {
      if (v.status === "awaiting_approval" && v.import_id) {
        const imp = s.plan_imports.find((i) => i.id === v.import_id);
        if (imp && imp.status === "synced") {
          Object.assign(v, { status: "synced", synced_at: imp.approved_at, sync_import_id: imp.id,
            sync_run_id: imp.sync_run_id, sync_scope: "full", updated_at: nowIso() });
          if (s.plan_source.current_version_id === v.id) {
            Object.assign(s.plan_source, { status: "up_to_date", last_sync_at: imp.approved_at, pending_import_id: null });
          }
        }
      }
    });
  });
}

function status() {
  reconcile();
  if (isRemoteMode()) reconcileStuckRemoteLock();
  const cfg = config();
  const st = getState();
  const versions = store.read((s) => s.plan_source_versions.slice(-10).reverse().map((v) => JSON.parse(JSON.stringify(v))));
  const events = store.read((s) => s.plan_source_events.slice(-20).reverse());
  let month = null;
  let monthError = null;
  try { month = targetMonth(cfg); } catch (e) { monthError = e.message; }
  return {
    configured: cfg.configured,
    source: cfg.configured ? { type: cfg.type, location: cfg.path, pattern: cfg.pattern } : null,
    require_approval: true,
    config_warnings: [cfg.requireApprovalWarning].filter(Boolean),
    auto_sync_enabled: autoSyncEnabled(cfg),
    auto_sync_default: cfg.autoSyncDefault,
    timezone: cfg.timezone,
    target_month_setting: cfg.targetMonth,
    current_month: month,
    month_error: monthError,
    status: cfg.configured ? (st.status || "idle") : "not_configured",
    running: st.running || null,
    busy: imports.jobs.busy ? imports.jobs.owner : null,
    last_check_at: st.last_check_at || null,
    last_check_result: st.last_check_result || null,
    last_change_at: st.last_change_at || null,
    last_sync_at: st.last_sync_at || null,
    last_error: st.last_error || null,
    last_counts: st.last_counts || null,
    last_file: st.last_file || null,
    current_version: versions.find((v) => v.id === st.current_version_id) || null,
    pending_import_id: st.pending_import_id || null,
    last_successful_check_at: st.last_successful_check_at || null,
    last_recalc_at: st.last_recalc_at || null,
    versions,
    events,
    remote_agent: cfg.type === "remote_agent" ? {
      enabled: true,
      agent: agentJobs.presence(),
      active_job: agentJobs.activeJob(),
      recent_jobs: agentJobs.recent(10)
    } : null
  };
}

/** Compact project-level change list of the current (or a given) version's dry run. */
function preview(versionId) {
  const st = getState();
  const vid = versionId || st.current_version_id;
  const v = vid ? store.read((s) => s.plan_source_versions.find((x) => x.id === vid)) : null;
  if (!v || !v.import_id) return null;
  const imp = imports.getImport(v.import_id);
  const rep = imp ? imports.readReport(imp.summary && imp.summary.dry_run).report : null;
  if (!rep) return { version: v, projects: [] };
  const projects = (rep.projects || []).filter((p) => p.change_type !== "unchanged").map((p) => ({
    project: p.project, category: p.category, change_type: p.change_type, dashboard_id: p.dashboard_id,
    source: p.source, reason: p.reason,
    changes: (p.changes || []).map((c) => ({ field: c.field, old: c.old, new: c.new })),
    activity_changes: p.activity_changes || [],
    conflicts: (p.conflicts || []).map((c) => c.field)
  }));
  return { version: v, import_id: imp.id, counts: rep.counts, status: rep.status, projects };
}

function setAutoSync(enabled) {
  setState({ auto_sync_override: !!enabled });
  logEvent("info", "auto_sync", enabled ? "автоматическая синхронизация включена" : "автоматическая синхронизация выключена");
}

// ------------------------------------------------------------ execution history
/**
 * Record one execution (scheduled or manual) with its phases kept apart:
 * check (file located, stable copy, SHA-256) / analysis (dry run) / sync (data written).
 * outcome summarises it; "synced" only when a sync completed.
 */
function outcomeOf(r) {
  const v = r.version || {};
  switch (r.status) {
    case "unchanged":
      return v.status === "synced" ? "unchanged" : "review_required";
    case "synced":
      return v.held_for_review ? "synced_review_required" : "synced";
    case "awaiting_approval":
    case "dry_run":
    case "blocked":
      return "review_required";
    case "busy":
      return "busy";
    case "not_configured":
      return "not_configured";
    case "queued":
    case "awaiting_agent_sync":
      return "queued";
    default:
      return "error";
  }
}

function recordRun(fields) {
  const run = Object.assign({ id: uuidv4(), at: nowIso() }, fields);
  store.tx((s) => {
    s.plan_source_runs.push(run);
    if (s.plan_source_runs.length > MAX_RUNS) s.plan_source_runs.splice(0, s.plan_source_runs.length - MAX_RUNS);
  });
  return run;
}

/**
 * Recalculate stage / priority / next-action decisions from the stored data - no file is read,
 * nothing is re-imported. Time-dependent rules and manual marks take effect without a new Excel version.
 */
async function recalculate({ trigger = "manual", scheduledDate = null } = {}) {
  const startedAt = nowIso();
  const base = { trigger: "recalc", recalc_trigger: trigger, scheduled_date: scheduledDate, started_at: startedAt,
    phases: { check: "not_run", analysis: "not_run", sync: "not_run", recalc: "not_run" } };
  if (!process.env.AGENT_API_KEY) return recordRun(Object.assign(base, { outcome: "error", error: "AGENT_API_KEY не задан" }));
  if (!imports.tryLock(`recalc:${trigger}`)) {
    return recordRun(Object.assign(base, { outcome: "busy", finished_at: nowIso() }));
  }
  try {
    const r = await imports.runRecalc();
    const j = r.json || {};
    base.phases.recalc = r.ok ? "ok" : "failed";
    const run = recordRun(Object.assign(base, { outcome: r.ok ? "recalculated" : "error", finished_at: nowIso(),
      error: r.ok ? null : r.error, counts: r.ok ? { checked: j.projects_checked, workflow_updated: j.workflow_updated,
        activities_updated: j.activities_updated, conflicts: j.conflicts } : null }));
    if (r.ok) setState({ last_recalc_at: nowIso() });
    else logEvent("error", "recalc_failed", r.error || "пересчёт не выполнен");
    return run;
  } finally {
    imports.unlock();
  }
}

/** Run a check and record it. trigger: "schedule" | "manual". In remote_agent mode this enqueues a
 *  job and returns immediately ("queued") instead of reading a file and running Python locally -
 *  completion happens later, asynchronously, when the Windows agent submits a result (see
 *  applyRemoteAnalyze/applyRemoteSync and POST /api/agent-jobs/agent/:id/result). */
async function runRecorded({ trigger, scheduledDate = null, scheduledFor = null }) {
  const startedAt = nowIso();
  const r = isRemoteMode()
    ? enqueueRemoteCheck({ trigger, scheduledDate, scheduledFor })
    : await check({ trigger });
  const v = r.version || {};
  const run = recordRun({ trigger, scheduled_date: scheduledDate, scheduled_for: scheduledFor, started_at: startedAt,
    finished_at: nowIso(), outcome: outcomeOf(r), result: r.status, phases: r.phases || null,
    version_id: v.id || null, sha256: v.sha256 || null, counts: v.counts || null, held_for_review: v.held_for_review || 0,
    error: r.error || null, error_code: r.code || null, job_id: (r.job && r.job.id) || null });
  return { run, result: r };
}

module.exports = { config, targetMonth, check, runRecorded, recordRun, outcomeOf, approvePending, status, preview, setAutoSync,
  recalculate,
  classifyDryRun, logEvent,
  isRemoteMode, applyRemoteAnalyze, applyRemoteSync };
