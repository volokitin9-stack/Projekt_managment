"use strict";

// JSON file storage - the single operational data store of Штурман.
//
// Everything (tasks/globals, projects, activities, history, files metadata,
// review queue, import runs) lives in ONE file: <DATA_DIR>/shturman.json.
// Reports of individual import runs are kept as separate files under
// <DATA_DIR>/sync-runs/ so the main file stays small.
//
// Guarantees:
// * Atomic writes: the new content is written to a temporary file, flushed
//   (fsync) and renamed over the old file. A crash never leaves a half-written
//   file. The previous version is kept as shturman.json.bak.
// * All-or-nothing changes: tx(fn) runs fn against the in-memory state; if fn
//   throws, the state is restored and nothing is written.
// * Single writer: a lock file (<DATA_DIR>/.shturman.lock) prevents two server
//   processes from using the same data directory. The planning agent never
//   touches these files - it talks to the server's HTTP API.
// * Versioned format: meta.schema_version; future format changes are applied
//   by MIGRATIONS with a backup copy written first. A file from a NEWER version
//   is refused instead of being overwritten.

const fs = require("fs");
const path = require("path");

const DATA_DIR = path.resolve(process.env.DATA_DIR || "./data");
const FILE = path.join(DATA_DIR, "shturman.json");
const LOCK = path.join(DATA_DIR, ".shturman.lock");
const TEMPLATE = path.join(DATA_DIR, "templates", "shturman.empty.json");
const SCHEMA_VERSION = 1;
const COLLECTIONS = ["stages", "docs", "projects", "project_activities", "project_history", "project_files",
  "agent_events", "review_queue", "sync_runs", "plan_imports"];

const DEFAULT_STAGES = [
  { key: "scanning", label: "Сканирование", sort_order: 1 },
  { key: "development", label: "Разработка", sort_order: 2 },
  { key: "prototype", label: "Тестовый образец", sort_order: 3 },
  { key: "measurement", label: "Замер", sort_order: 4 },
  { key: "documentation", label: "Оформление", sort_order: 5 }
];

// Future format changes: { version: 2, name: "...", up(state) { ... } }.
const MIGRATIONS = [];

function nowIso() {
  return new Date().toISOString();
}

function emptyState() {
  let base = null;
  if (fs.existsSync(TEMPLATE)) {
    base = JSON.parse(fs.readFileSync(TEMPLATE, "utf-8"));
  }
  const ts = nowIso();
  const state = { meta: { format: "shturman-json", schema_version: SCHEMA_VERSION, created_at: ts, updated_at: ts } };
  COLLECTIONS.forEach((c) => { state[c] = base && Array.isArray(base[c]) ? base[c] : []; });
  if (!state.stages.length) state.stages = DEFAULT_STAGES.map((s) => Object.assign({}, s));
  return state;
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === "EPERM";
  }
}

function acquireLock() {
  if (fs.existsSync(LOCK)) {
    const pid = Number(String(fs.readFileSync(LOCK, "utf-8")).trim());
    if (pid && pid !== process.pid && pidAlive(pid)) {
      throw new Error(`data directory ${DATA_DIR} is in use by another Штурман process (pid ${pid})`);
    }
  }
  fs.writeFileSync(LOCK, String(process.pid));
  const release = () => {
    try {
      if (fs.existsSync(LOCK) && String(fs.readFileSync(LOCK, "utf-8")).trim() === String(process.pid)) fs.unlinkSync(LOCK);
    } catch (e) { /* ignore */ }
  };
  process.on("exit", release);
  ["SIGINT", "SIGTERM"].forEach((sig) => process.on(sig, () => { release(); process.exit(0); }));
}

function writeAtomic(file, text) {
  const tmp = `${file}.tmp-${process.pid}`;
  const fd = fs.openSync(tmp, "w");
  try {
    fs.writeSync(fd, text);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  if (fs.existsSync(file)) fs.copyFileSync(file, `${file}.bak`);
  fs.renameSync(tmp, file);
}

function load() {
  let source = FILE;
  if (!fs.existsSync(FILE) && fs.existsSync(`${FILE}.bak`)) source = `${FILE}.bak`;   // crash between copy and rename
  if (!fs.existsSync(source)) return { state: emptyState(), created: true };
  const state = JSON.parse(fs.readFileSync(source, "utf-8"));
  if (!state.meta || state.meta.format !== "shturman-json") throw new Error(`${source} is not a Штурман data file`);
  if (state.meta.schema_version > SCHEMA_VERSION) {
    throw new Error(`${source} has data format v${state.meta.schema_version}, this application supports up to ` +
      `v${SCHEMA_VERSION} - update the application instead of overwriting newer data`);
  }
  COLLECTIONS.forEach((c) => { if (!Array.isArray(state[c])) state[c] = []; });
  if (!state.stages.length) state.stages = DEFAULT_STAGES.map((s) => Object.assign({}, s));
  return { state, created: false };
}

fs.mkdirSync(DATA_DIR, { recursive: true });
acquireLock();
const loaded = load();
let state = loaded.state;
const migrationResult = { applied: [], backup: null };

(function migrate() {
  const pending = MIGRATIONS.filter((m) => m.version > state.meta.schema_version).sort((a, b) => a.version - b.version);
  if (!pending.length) return;
  if (fs.existsSync(FILE)) {
    const backup = `${FILE}.pre-migration-v${pending[0].version}-${nowIso().replace(/[:.]/g, "-")}.bak`;
    fs.copyFileSync(FILE, backup);
    migrationResult.backup = path.basename(backup);
  }
  pending.forEach((m) => {
    m.up(state);
    state.meta.schema_version = m.version;
    migrationResult.applied.push(m.version);
  });
})();

function persist() {
  state.meta.updated_at = nowIso();
  writeAtomic(FILE, JSON.stringify(state));
}

if (loaded.created || migrationResult.applied.length) persist();

/** Read-only access. Do not mutate the returned objects outside tx(). */
function read(fn) {
  return fn(state);
}

/** Apply fn to the state atomically: persisted on success, rolled back if fn throws. */
function tx(fn) {
  const snapshot = JSON.stringify(state);
  try {
    const result = fn(state);
    persist();
    return result;
  } catch (e) {
    state = JSON.parse(snapshot);
    throw e;
  }
}

// ---- import-run reports (kept out of the main file) ----
const RUNS_DIR = path.join(DATA_DIR, "sync-runs");
function saveRunReport(id, report) {
  fs.mkdirSync(RUNS_DIR, { recursive: true });
  writeAtomic(path.join(RUNS_DIR, `${String(id).replace(/[^\w.-]/g, "_")}.json`), JSON.stringify(report));
}
function loadRunReport(id) {
  const f = path.join(RUNS_DIR, `${String(id).replace(/[^\w.-]/g, "_")}.json`);
  return fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, "utf-8")) : null;
}

module.exports = {
  DATA_DIR,
  FILE,
  SCHEMA_VERSION,
  DEFAULT_STAGES,
  read,
  tx,
  saveRunReport,
  loadRunReport,
  schemaVersion: () => state.meta.schema_version,
  migrationResult
};
