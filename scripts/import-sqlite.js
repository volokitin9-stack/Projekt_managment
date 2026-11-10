"use strict";

// One-time import of a database created by an earlier, SQLite-based version of
// Штурман (any schema version) into the JSON data file.
//
//   node scripts/import-sqlite.js path/to/app.sqlite            # dry run: counts only
//   node scripts/import-sqlite.js path/to/app.sqlite --write    # import
//
// * Stop the server first (the data directory is used by one process at a time).
// * The target data file must not contain projects yet - nothing is merged or
//   overwritten. The SQLite file is only read, never modified.
// * Requires Node.js 22.13+ (built-in node:sqlite); no extra dependency.

require("dotenv").config();
const fs = require("fs");

const src = process.argv[2];
const write = process.argv.includes("--write");
if (!src || !fs.existsSync(src)) {
  console.error("usage: node scripts/import-sqlite.js <legacy app.sqlite> [--write]");
  process.exit(2);
}
let DatabaseSync;
try {
  ({ DatabaseSync } = require("node:sqlite"));
} catch (e) {
  console.error("this script needs Node.js 22.13 or newer (built-in node:sqlite)");
  process.exit(2);
}
const db = new DatabaseSync(src, { readOnly: true });
const tables = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((r) => r.name));
const all = (t) => (tables.has(t) ? db.prepare(`SELECT * FROM ${t}`).all() : []);
const parse = (v, dflt = null) => {
  if (v === null || v === undefined) return dflt;
  try { return JSON.parse(v); } catch (e) { return v; }
};

const imported = {
  stages: all("stages").map((r) => ({ key: r.key, label: r.label, sort_order: r.sort_order })),
  docs: all("docs").map((r) => ({ collection: r.collection, id: r.id, data: parse(r.data, {}), version: r.version,
    created_at: r.created_at, updated_at: r.updated_at })),
  projects: all("projects").map((r) => ({ id: r.id, external_project_id: r.external_project_id || null,
    data: parse(r.data, {}), version: r.version, needs_review: !!r.needs_review, created_at: r.created_at,
    updated_at: r.updated_at, record_category: r.record_category || "main_project",
    stage_entered_at: r.stage_entered_at || null, field_sources: parse(r.field_sources, {}) || {},
    last_plan_month: r.last_plan_month || null })),
  project_activities: all("project_activities").map((r) => Object.assign({}, r, {
    hours_by_date: parse(r.hours_by_date), details: parse(r.details), field_sources: parse(r.field_sources, {}) || {},
    removed_from_source: !!r.removed_from_source })),
  project_history: all("project_activity").map((r) => ({ id: r.id, project_id: r.project_id, action: r.action,
    source: r.source, actor: r.actor, previous_value: parse(r.previous_value), new_value: parse(r.new_value), at: r.at }))
    .sort((a, b) => String(a.at).localeCompare(String(b.at))),
  project_files: all("project_files").map((r) => Object.assign({}, r, { is_primary: !!r.is_primary })),
  agent_events: all("agent_events").map((r) => Object.assign({}, r, { payload: parse(r.payload, {}) })),
  review_queue: all("review_queue").map((r) => Object.assign({}, r, { payload: parse(r.payload, {}),
    resolved: !!r.resolved })),
  sync_runs: all("sync_runs").map((r) => ({ id: r.id, source_file: r.source_file, file_sha256: r.file_sha256,
    plan_month: r.plan_month, mode: r.mode, status: r.status, started_at: r.started_at, finished_at: r.finished_at,
    counts: parse(r.counts), error: r.error, review_status: r.review_status, _report: parse(r.report) })),
  plan_imports: all("plan_imports").map((r) => Object.assign({}, r, { summary: parse(r.summary) }))
};

console.log(`legacy database ${src}:`);
Object.entries(imported).forEach(([k, v]) => console.log(`  ${k.padEnd(20)} ${v.length}`));
if (!write) {
  console.log("dry run - nothing written. Re-run with --write to import.");
  process.exit(0);
}

const store = require("../server/store");
const existing = store.read((s) => s.projects.length + s.docs.length);
if (existing) {
  console.error(`refusing to import: ${store.FILE} already contains data (${existing} projects/documents)`);
  process.exit(3);
}
store.tx((s) => {
  Object.entries(imported).forEach(([k, rows]) => {
    if (k === "stages") {
      if (rows.length) s.stages = rows;
      return;
    }
    s[k] = rows.map((r) => {
      const copy = Object.assign({}, r);
      delete copy._report;
      return copy;
    });
  });
});
imported.sync_runs.filter((r) => r._report).forEach((r) => store.saveRunReport(r.id, r._report));
console.log(`imported into ${store.FILE}`);
