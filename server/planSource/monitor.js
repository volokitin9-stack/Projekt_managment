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

const REPO_ROOT = path.join(__dirname, "..", "..");
const MAX_EVENTS = 300;
const FINAL = ["synced", "awaiting_approval", "blocked", "failed"];   // same bytes are not processed again
const DEFAULT_TZ = "UTC";

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
  const minutes = Number(env.PLAN_CHECK_INTERVAL_MINUTES || 30);
  return {
    type,
    path: p,
    pattern: (env.PLAN_SOURCE_PATTERN || "").trim() || null,
    configured: !!(type && p),
    intervalMinutes: Number.isFinite(minutes) && minutes > 0 ? minutes : 30,
    settleSeconds: env.PLAN_SOURCE_SETTLE_SECONDS === undefined ? 5 : Number(env.PLAN_SOURCE_SETTLE_SECONDS),
    autoSyncDefault: bool(env.PLAN_AUTO_SYNC_ENABLED, false),
    targetMonth: (env.PLAN_TARGET_MONTH || "auto").trim() || "auto",
    timezone: (env.PLAN_TIMEZONE || DEFAULT_TZ).trim(),
    initialDelaySeconds: env.PLAN_SOURCE_INITIAL_DELAY_SECONDS === undefined ? 15 : Number(env.PLAN_SOURCE_INITIAL_DELAY_SECONDS)
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
(function recoverInterrupted() {
  store.tx((s) => {
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
async function check({ trigger = "manual", force = false } = {}) {
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
      settleSeconds: cfg.settleSeconds });
    const probe = await source.probe();
    fs.mkdirSync(imports.FILES_DIR, { recursive: true });
    tmp = path.join(imports.FILES_DIR, `.incoming-${uuidv4()}`);
    const snap = await source.fetchTo(tmp, probe);

    let version = findVersion(snap.sha256, month);
    const auto = autoSyncEnabled(cfg);
    const fileInfo = { name: snap.name, size: snap.size, mtime: snap.mtime, sha256: snap.sha256 };
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
      const outcome = classifyDryRun(r.json);
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
    return await syncVersion(version.id, { autoSafe: true, by: "plan_source_auto", startedAt, trigger });
  } catch (e) {
    if (tmp) { try { fs.unlinkSync(tmp); } catch (x) { /* ignore */ } }
    const se = e instanceof SourceError ? e : new SourceError("internal", `${e.name}: ${e.message}`);
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
async function syncVersion(versionId, { autoSafe, by, startedAt, trigger }) {
  const version = store.read((s) => JSON.parse(JSON.stringify(s.plan_source_versions.find((x) => x.id === versionId))));
  const imp = imports.getImport(version.import_id);
  updateVersion(version.id, { status: "syncing", phase: autoSafe ? "auto_sync" : "sync" });
  setState({ running: { started_at: startedAt || nowIso(), trigger, phase: "sync" } });
  const r = await imports.approveAndSync(imp, by, { autoSafe });
  const counts = r.json ? r.json.counts : null;
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

/** Manual approval of the pending version (mode A). */
async function approvePending(by) {
  const st = getState();
  const vid = st.current_version_id;
  const version = vid ? store.read((s) => s.plan_source_versions.find((x) => x.id === vid)) : null;
  if (!version || !["awaiting_approval", "sync_failed", "interrupted"].includes(version.status) || !version.import_id) {
    return { status: "nothing_pending" };
  }
  const imp = imports.getImport(version.import_id);
  if (!imp || !["analyzed", "interrupted", "sync_failed"].includes(imp.status)) return { status: "nothing_pending" };
  if (!imports.tryLock("plan_source:approve")) return { status: "busy", owner: imports.jobs.owner };
  try {
    return await syncVersion(version.id, { autoSafe: false, by, trigger: "approve" });
  } finally {
    imports.unlock();
  }
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
    monitoring: timer.running,
    interval_minutes: cfg.intervalMinutes,
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
    next_check_at: timer.nextAt,
    versions,
    events
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

// --------------------------------------------------------------------- timer
const timer = { running: false, handle: null, nextAt: null };

function schedule(ms) {
  clearTimeout(timer.handle);
  timer.nextAt = new Date(Date.now() + ms).toISOString();
  timer.handle = setTimeout(async () => {
    timer.nextAt = null;
    try {
      await check({ trigger: "schedule" });
    } catch (e) {
      console.error("[plan-source] check crashed:", e); // eslint-disable-line no-console
    }
    if (timer.running) schedule(config().intervalMinutes * 60 * 1000);
  }, ms);
  if (timer.handle.unref) timer.handle.unref();
}

/** Start periodic checks (once per process). Returns false when no source is configured. */
function start() {
  const cfg = config();
  if (timer.running) return true;
  if (!cfg.configured) return false;
  timer.running = true;
  logEvent("info", "monitor_started", `мониторинг источника ${cfg.type}:${cfg.path} каждые ${cfg.intervalMinutes} мин` +
    `, автосинхронизация ${autoSyncEnabled(cfg) ? "включена" : "выключена"}`);
  schedule(Math.max(0, cfg.initialDelaySeconds) * 1000);
  return true;
}

function stop() {
  timer.running = false;
  clearTimeout(timer.handle);
  timer.nextAt = null;
}

module.exports = { config, targetMonth, check, approvePending, status, preview, setAutoSync, start, stop, classifyDryRun };
