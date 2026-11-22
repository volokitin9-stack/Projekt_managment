"use strict";

// Local, one-time import of personal tasks from an old board export (shturman_tasks_export_*.json)
// into the "tasks" collection. Same logic as the dashboard's admin import (server/taskImport.js) -
// use the dashboard ("Интеграции → Импорт задач") on Railway, where the server keeps the data
// directory locked; use this script only locally, WITH THE SERVER STOPPED.
//
//   node scripts/import_artifact_tasks.js <export.json>                          # dry run: analysis only
//   node scripts/import_artifact_tasks.js <export.json> --write                  # analysis + import
//   node scripts/import_artifact_tasks.js --rollback <import-manifest.json>      # dry run of the rollback
//   node scripts/import_artifact_tasks.js --rollback <import-manifest.json> --write [--force]
//
// The export is only read. An import is one atomic write; it never overwrites or changes an existing
// task. Every --write import writes import-manifest-<timestamp>.json next to the export (and the
// same record is kept in the data file), and --rollback removes only the tasks that import created
// and that were not edited since (--force removes edited ones too).

require("../server/env");   // .env from the project folder, backslashes kept literally
const fs = require("fs");
const path = require("path");

const taskImport = require("../server/taskImport");
const { createDocStore } = require("../server/docStore");

const args = process.argv.slice(2);
const write = args.includes("--write");
const force = args.includes("--force");
const rollbackIdx = args.indexOf("--rollback");

function fail(msg) {
  console.error(msg);
  process.exit(2);
}

function printReport(r, src) {
  const byQ = Object.entries(r.quadrant_mapping).map(([q, m]) => `${q}=${m.count}`).join(", ");
  console.log(`Source: ${src} (${r.source.tasks} record(s): ${byQ}; ${r.source.done} done, ${r.source.active} active; ` +
    `${r.source.subtasks} subtask(s), ${r.source.subtasks_done} done).`);
  console.log(`Target: ${r.target.existing_tasks} existing task(s) already in this board's "tasks" collection before this run.`);
  console.log(`${r.new.length} new, ${r.already_imported.length} duplicate (already present by id) - duplicates are never ` +
    `overwritten. New subtasks: ${r.new_subtasks}.`);
  if (r.already_imported.length) console.log(`Duplicate ids: ${r.already_imported.map((x) => x.id).join(", ")}`);
  console.log("Category mapping:");
  Object.entries(r.quadrant_mapping).forEach(([q, m]) => console.log(`  ${q} -> ${m.target} «${m.target_label}»: ${m.count} ` +
    `(active ${m.active}, done ${m.done})${m.lossless ? "" : " - UNKNOWN category"}`));
  console.log(`Conflicts: ${r.conflicts.length ? "" : "none"}`);
  r.conflicts.forEach((c) => console.log(`  ${c.type}: ${c.id || "#" + c.index}${c.existing_title ? ` (existing: ${c.existing_title})` : ""}`));
  if (r.title_matches.length) console.log(`Same title as an existing task (imported anyway, check manually): ${r.title_matches.map((m) => m.source_id).join(", ")}`);
  r.warnings.forEach((w) => console.log(`  warning ${w.type}: ${w.id}${w.value !== undefined ? ` = ${JSON.stringify(w.value)}` : ""}`));
  console.log("Fields not carried over losslessly:");
  r.lossy_fields.forEach((f) => console.log(`  ${f.field}${f.count != null ? ` (${f.count})` : ""}: ${f.how}`));
}

function runImport(src) {
  if (!src || !fs.existsSync(src)) {
    fail("usage: node scripts/import_artifact_tasks.js <shturman_tasks_export.json> [--write]\n" +
      "       node scripts/import_artifact_tasks.js --rollback <import-manifest.json> [--write] [--force]");
  }
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(src, "utf-8"));
  } catch (e) {
    fail(`${src} is not valid JSON: ${e.message}`);
  }
  const fileName = path.basename(src);
  const r = taskImport.analyze(payload, { fileName });
  if (r.error) fail(`${src}: ${r.error} (no "tasks" array?)`);
  printReport(r, src);
  if (!write) {
    console.log(`Would import ${r.new.length} new task(s); skipped ${r.already_imported.length} already-existing id(s).`);
    console.log("Dry run only - nothing was written. Re-run with --write to apply.");
    return;
  }
  const res = taskImport.apply(payload, { planHash: r.plan_hash, actor: "cli", fileName });
  if (res.status >= 400) fail(`Import refused: ${res.body.error}`);
  const imported = res.body.imported || 0;
  console.log(`Imported ${imported} new task(s); skipped ${r.already_imported.length} already-existing id(s).`);
  if (imported) {
    const rec = taskImport.list().find((x) => x.id === res.body.import_id);
    const manifest = { import_id: rec.id, imported_at: rec.imported_at, source_file: path.resolve(src),
      source_sha256: rec.source_sha256, data_dir: require("../server/store").DATA_DIR, created_ids: rec.created.map((c) => c.id) };
    const manifestPath = path.join(path.dirname(path.resolve(src)), `import-manifest-${Date.now()}.json`);
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
    console.log(`Rollback manifest written: ${manifestPath}`);
    console.log(`To undo this exact import: node scripts/import_artifact_tasks.js --rollback ${manifestPath} --write`);
  }
}

function runRollback(manifestPath) {
  if (!manifestPath || !fs.existsSync(manifestPath)) {
    fail("usage: node scripts/import_artifact_tasks.js --rollback <import-manifest.json> [--write] [--force]");
  }
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
  } catch (e) {
    fail(`${manifestPath} is not valid JSON: ${e.message}`);
  }
  const ids = Array.isArray(manifest.created_ids) ? manifest.created_ids : null;
  if (!ids) fail(`${manifestPath} has no "created_ids" array - is this an import-manifest file written by this script?`);
  console.log(`Manifest: import from ${manifest.source_file || "(unknown)"} at ${manifest.imported_at || "(unknown)"}, ${ids.length} record(s).`);

  let r;
  if (manifest.import_id && taskImport.list().some((x) => x.id === manifest.import_id)) {
    r = taskImport.rollback(manifest.import_id, { dryRun: !write, force, actor: "cli" }).body;
  } else {
    // manifest of the previous version of this script (no import record): exactly its ids, nothing else
    const store = createDocStore("tasks");
    const present = ids.filter((id) => store.get(id));
    r = { would_remove: present, already_gone: ids.filter((id) => !present.includes(id)), kept_modified: [] };
    if (write) { present.forEach((id) => store.delete(id)); r.removed = present.length; }
  }
  if (r.already_gone.length) console.log(`${r.already_gone.length} id(s) from the manifest are already gone (removed earlier, or never written) - left alone.`);
  if (r.kept_modified.length) console.log(`Kept ${r.kept_modified.length} task(s) edited after the import (use --force to remove): ${r.kept_modified.map((k) => k.id).join(", ")}`);
  if (!write) {
    console.log(`Would remove ${r.would_remove.length} task(s) still present: ${r.would_remove.join(", ") || "(none)"}.`);
    console.log("Dry run only - nothing was removed. Re-run with --write to apply.");
    return;
  }
  console.log(`Removed ${r.removed} task(s). This never touches any record outside this import's own tasks.`);
}

if (rollbackIdx !== -1) runRollback(args[rollbackIdx + 1]);
else runImport(args.find((a) => !a.startsWith("--")));
