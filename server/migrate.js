"use strict";

// One-time migration: copy a staged export of an older Штурман install (data/shturman.json +
// uploads/ + plan-imports/) onto this deployment's persistent storage (DATA_DIR/UPLOADS_DIR/
// PLAN_IMPORT_DIR) before the server starts serving traffic or seed()/store.js ever run.
//
// Only active when RAILWAY_MIGRATE_FROM_STAGING is set truthy - see docs/RAILWAY_DEPLOYMENT.md
// #migration for the exact one-time enable/disable instructions. When unset this module does not
// touch the filesystem at all.
//
// Must be required BEFORE server/store.js (directly, or transitively via server/seed.js) is ever
// required in this process: store.js takes a lock and loads/creates shturman.json as a top-level
// side effect of being required, so this has to run first. For the same reason this file resolves
// DATA_DIR/UPLOADS_DIR/PLAN_IMPORT_DIR itself (duplicating a few lines from store.js/storage.js/
// routes/planImports.js) instead of requiring those modules - same deliberate duplication
// scripts/verify_migration.js already uses, and for the same reason.

const fs = require("fs");
const path = require("path");

// Mirrors server/store.js's COLLECTIONS (kept separate - see header comment above).
const COLLECTIONS = ["stages", "docs", "projects", "project_activities", "project_history", "project_files",
  "agent_events", "review_queue", "sync_runs", "plan_imports", "plan_source_versions", "plan_source_events",
  "plan_source_runs", "agent_jobs"];

// REQUIRED_COLLECTIONS: present since the original JSON-storage format (server/store.js @ 76fd8f8) -
// a staged export missing one of these is not a valid Штурман database and must be rejected.
// Everything else in COLLECTIONS was added later, each time "without a format change: missing =
// empty" (store.js's own convention - see its loadState, which already defaults any missing
// collection to []). A staged export from before one of those additions is still a perfectly
// valid legacy database; it just predates that collection. agent_jobs, for instance, only exists
// since the Railway job-queue work (65d386d) and is absent from every pre-Railway Windows export.
const REQUIRED_COLLECTIONS = ["stages", "docs", "projects", "project_activities", "project_history",
  "project_files", "agent_events", "review_queue", "sync_runs", "plan_imports"];
const OPTIONAL_COLLECTIONS = COLLECTIONS.filter((c) => !REQUIRED_COLLECTIONS.includes(c));

const SKIP_NAMES = new Set([".shturman.lock"]); // never copy a lock file from staging into the live install
function shouldSkip(name) {
  return SKIP_NAMES.has(name) || /\.tmp-/.test(name) || /\.env$/i.test(name) || /secret|credential/i.test(name);
}

function truthy(v) {
  return typeof v === "string" && ["1", "true", "yes"].includes(v.trim().toLowerCase());
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === "EPERM";
  }
}

function copyRecursive(src, dst, { skip = () => false } = {}) {
  if (!fs.existsSync(src)) return;
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (skip(entry.name)) continue;
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) copyRecursive(s, d, { skip });
    else fs.copyFileSync(s, d);
  }
}

function resolveTargetPaths(env = process.env) {
  return {
    dataDir: path.resolve(env.DATA_DIR || "./data"),
    uploadsDir: path.resolve(env.UPLOADS_DIR || "./uploads"),
    planImportDir: path.resolve(env.PLAN_IMPORT_DIR || "./data/plan-imports")
  };
}

function resolveStagingPaths(sourceJson) {
  const stagingDataDir = path.dirname(sourceJson);     // .../extracted/data
  const stagingRoot = path.dirname(stagingDataDir);    // .../extracted
  // scripts/snapshot_data.js's backup layout puts uploads/ as a sibling of data/.
  const uploadsDir = path.join(stagingRoot, "uploads");
  // plan-imports: the common default (PLAN_IMPORT_DIR=./data/plan-imports) nests it inside data/,
  // so it is already swept up by a plain data/ copy; a Railway-style sibling layout is the
  // fallback. Whichever actually exists on disk is used.
  const nested = path.join(stagingDataDir, "plan-imports");
  const sibling = path.join(stagingRoot, "plan-imports");
  const planImportDir = fs.existsSync(nested) ? nested : (fs.existsSync(sibling) ? sibling : null);
  return { stagingDataDir, uploadsDir, planImportDir };
}

function validateStagedJson(sourceJson) {
  let raw;
  try {
    raw = fs.readFileSync(sourceJson, "utf-8");
  } catch (e) {
    throw new Error(`cannot read staged source ${sourceJson}: ${e.message}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    throw new Error(`staged source ${sourceJson} is not valid JSON: ${e.message}`);
  }
  if (!parsed.meta || parsed.meta.format !== "shturman-json") {
    throw new Error(`staged source ${sourceJson} is not a Штурман data file (meta.format mismatch)`);
  }
  const missing = REQUIRED_COLLECTIONS.filter((c) => !Array.isArray(parsed[c]));
  if (missing.length) {
    throw new Error(`staged source ${sourceJson} is missing required legacy section(s): ${missing.join(", ")}`);
  }
  // Collections introduced after this export was taken: initialize to empty, leave everything
  // else in the parsed object (including every existing record and field) exactly as read.
  const backfilled = [];
  OPTIONAL_COLLECTIONS.forEach((c) => {
    if (!Array.isArray(parsed[c])) {
      parsed[c] = [];
      backfilled.push(c);
    }
  });
  if (backfilled.length) {
    console.log(`[migrate] staged source predates these collections - initializing empty: ${backfilled.join(", ")}`); // eslint-disable-line no-console
  }
  return parsed;
}

function checkNoLiveWriter(targetDataDir) {
  const lock = path.join(targetDataDir, ".shturman.lock");
  if (!fs.existsSync(lock)) return;
  const pid = Number(String(fs.readFileSync(lock, "utf-8")).trim());
  if (pid && pid !== process.pid && pidAlive(pid)) {
    throw new Error(`refusing to migrate: ${targetDataDir} is in use by another Штурман process (pid ${pid}) - ` +
      "stop it before migrating");
  }
}

function countRecords(parsed) {
  const counts = {};
  COLLECTIONS.forEach((c) => { counts[c] = (parsed[c] || []).length; });
  return counts;
}

function nowStamp() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

/**
 * Runs the one-time staging migration if (and only if) RAILWAY_MIGRATE_FROM_STAGING is truthy and
 * migration has not already completed. No-op, with zero filesystem access, when the flag is unset.
 * Throws on any failure - the caller (server/index.js) must not proceed to start the server when
 * this throws, so a half-migrated state is never served.
 */
function runStagingMigrationIfRequested(env = process.env) {
  if (!truthy(env.RAILWAY_MIGRATE_FROM_STAGING)) return { ran: false, reason: "flag not set" };

  const target = resolveTargetPaths(env);
  const marker = path.join(target.dataDir, ".migration-from-staging-complete.json");
  if (fs.existsSync(marker)) {
    const info = JSON.parse(fs.readFileSync(marker, "utf-8"));
    console.log(`[migrate] staging migration already completed at ${info.completed_at} - skipping (idempotent).`); // eslint-disable-line no-console
    return { ran: false, reason: "already completed", marker: info };
  }

  const sourceJson = path.resolve(env.MIGRATION_STAGING_SOURCE_JSON || "/data/migration-staging/extracted/data/shturman.json");
  console.log(`[migrate] RAILWAY_MIGRATE_FROM_STAGING is set - migrating ${sourceJson} -> ${target.dataDir}`); // eslint-disable-line no-console

  if (!fs.existsSync(sourceJson)) {
    throw new Error(`staging source not found: ${sourceJson} (set MIGRATION_STAGING_SOURCE_JSON if it is elsewhere)`);
  }
  const parsed = validateStagedJson(sourceJson); // throws on bad JSON/schema - nothing written yet
  const staging = resolveStagingPaths(sourceJson);

  checkNoLiveWriter(target.dataDir); // throws if another instance currently holds the target's lock

  // --- backup current target (if any) before any write -------------------------------------
  let backupDir = null;
  if (fs.existsSync(path.join(target.dataDir, "shturman.json"))) {
    backupDir = path.join(target.dataDir, "migration-backups", nowStamp());
    console.log(`[migrate] backing up current data to ${backupDir}`); // eslint-disable-line no-console
    copyRecursive(target.dataDir, path.join(backupDir, "data"), { skip: (n) => shouldSkip(n) || n === "migration-backups" });
    copyRecursive(target.uploadsDir, path.join(backupDir, "uploads"), { skip: shouldSkip });
    copyRecursive(target.planImportDir, path.join(backupDir, "plan-imports"), { skip: shouldSkip });
  } else {
    console.log("[migrate] no existing shturman.json at the target - nothing to back up (fresh volume)."); // eslint-disable-line no-console
  }

  // --- copy secondary assets first; the canonical JSON is swapped in last and atomically ----
  if (fs.existsSync(staging.uploadsDir)) {
    copyRecursive(staging.uploadsDir, target.uploadsDir, { skip: shouldSkip });
  } else {
    console.warn(`[migrate] no staged uploads/ found at ${staging.uploadsDir} - skipping (nothing to copy).`); // eslint-disable-line no-console
  }
  if (staging.planImportDir) {
    copyRecursive(staging.planImportDir, target.planImportDir, { skip: shouldSkip });
  } else {
    console.warn("[migrate] no staged plan-imports/ found - skipping (nothing to copy)."); // eslint-disable-line no-console
  }
  // Everything in the staged data dir except shturman.json itself (sync-runs/, any nested
  // plan-imports/ already handled above, etc.) - never the lock file, never the main JSON yet.
  copyRecursive(staging.stagingDataDir, target.dataDir, {
    skip: (n) => shouldSkip(n) || n === "shturman.json" || n === "plan-imports" || n === "migration-backups"
  });

  // --- canonical JSON: atomic write, same pattern as server/store.js ------------------------
  fs.mkdirSync(target.dataDir, { recursive: true });
  const finalFile = path.join(target.dataDir, "shturman.json");
  const tmp = `${finalFile}.tmp-migrate-${process.pid}`;
  const text = JSON.stringify(parsed);
  const fd = fs.openSync(tmp, "w");
  try {
    fs.writeSync(fd, text);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, finalFile); // atomic on the same filesystem - this is the point of no return

  // --- completion marker: makes every later run (flag still set or set again) a safe no-op --
  const info = {
    completed_at: new Date().toISOString(),
    source: sourceJson,
    backup: backupDir,
    uploads_copied: fs.existsSync(staging.uploadsDir),
    plan_imports_copied: Boolean(staging.planImportDir),
    record_counts: countRecords(parsed)
  };
  fs.writeFileSync(marker, JSON.stringify(info, null, 2));
  console.log(`[migrate] done. ${JSON.stringify(info.record_counts)}`); // eslint-disable-line no-console
  return { ran: true, info };
}

module.exports = {
  runStagingMigrationIfRequested, resolveTargetPaths, resolveStagingPaths, validateStagedJson,
  COLLECTIONS, REQUIRED_COLLECTIONS, OPTIONAL_COLLECTIONS
};
