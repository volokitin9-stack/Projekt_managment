"use strict";

// One-time import of the personal Kanban board's "tasks" collection, exported from the Claude
// Artifact ("Экспорт задач в JSON" button) as one JSON file, into this app's own "tasks" doc
// store (server/docStore.js) - see docs/TELEGRAM_INTEGRATION.md "Stage 4 - data migration".
// Критическое требование №3 ("Дополнение к ТЗ Telegram-интеграции «Штурмана»"): a pre-import
// analysis before writing anything, explicit duplicate detection, and a rollback path - all below.
//
//   node scripts/import_artifact_tasks.js path/to/shturman_tasks_export.json             # dry run: analysis only
//   node scripts/import_artifact_tasks.js path/to/shturman_tasks_export.json --write      # import
//   node scripts/import_artifact_tasks.js --rollback path/to/import-manifest-*.json       # dry run: what would be removed
//   node scripts/import_artifact_tasks.js --rollback path/to/import-manifest-*.json --write # remove exactly those records
//
// * Stop the server first (the data directory is used by one process at a time - same rule as
//   scripts/import-sqlite.js).
// * Idempotent by id: a record whose id already exists in this target is skipped, never
//   overwritten - safe to re-run (e.g. after exporting again with newly-added tasks) without
//   ever duplicating or clobbering anything already imported or created directly here.
// * Only ever adds new task documents. Never deletes, never touches any other collection -
//   except --rollback, which only ever deletes exactly the ids its own matching manifest recorded.
// * The export file is only read, never modified.
// * Every --write import writes a manifest (import-manifest-<timestamp>.json, next to the export
//   file) recording exactly which ids it created - the only input --rollback accepts, so a
//   rollback can never remove a record this script did not itself create in that run (not a
//   record imported by an earlier run, and never a record created directly on the board since).

require("../server/env");   // .env from the project folder, backslashes kept literally
const fs = require("fs");
const path = require("path");

const { createDocStore } = require("../server/docStore");
const store = createDocStore("tasks");

const args = process.argv.slice(2);
const write = args.includes("--write");
const rollbackIdx = args.indexOf("--rollback");

if (rollbackIdx !== -1) {
  runRollback(args[rollbackIdx + 1]);
} else {
  runImport(args.find((a) => a !== "--write"));
}

function fail(msg) {
  console.error(msg);
  process.exit(2);
}

function runImport(src) {
  if (!src || !fs.existsSync(src)) {
    fail("usage: node scripts/import_artifact_tasks.js <shturman_tasks_export.json> [--write]\n" +
      "       node scripts/import_artifact_tasks.js --rollback <import-manifest.json> [--write]");
  }

  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(src, "utf-8"));
  } catch (e) {
    fail(`${src} is not valid JSON: ${e.message}`);
  }
  const tasks = Array.isArray(payload.tasks) ? payload.tasks : null;
  if (!tasks) fail(`${src} has no "tasks" array - is this a shturman_tasks_export.json file?`);

  // ---- pre-import analysis (always runs, write or not): what exists, what would change ----
  const existingCount = store.list().length;
  const byQuadrant = {};
  let doneCount = 0;
  tasks.forEach((t) => {
    if (!t) return;
    byQuadrant[t.quadrant || "inbox"] = (byQuadrant[t.quadrant || "inbox"] || 0) + 1;
    if (t.done) doneCount += 1;
  });
  console.log(`Source: ${src} (${tasks.length} record(s): ${Object.entries(byQuadrant)
    .map(([q, n]) => `${q}=${n}`).join(", ")}; ${doneCount} done, ${tasks.length - doneCount} active).`);
  console.log(`Target: ${existingCount} existing task(s) already in this board's "tasks" collection before this run.`);

  const toCreate = [];
  const toSkip = [];
  tasks.forEach((t) => {
    if (!t || !t.id) return;
    (store.get(t.id) ? toSkip : toCreate).push(t);
  });
  const noIdCount = tasks.filter((t) => !t || !t.id).length;
  if (noIdCount) console.warn(`${noIdCount} record(s) have no id and will be skipped - this should not happen in a real export file.`);
  console.log(`${toCreate.length} new, ${toSkip.length} duplicate (already present by id) - duplicates are never overwritten.`);
  if (toSkip.length) console.log(`Duplicate ids: ${toSkip.map((t) => t.id).join(", ")}`);

  if (!write) {
    console.log(`Would import ${toCreate.length} new task(s); skipped ${toSkip.length} already-existing id(s).`);
    console.log("Dry run only - nothing was written. Re-run with --write to apply.");
    return;
  }

  const createdIds = [];
  toCreate.forEach((t) => {
    const data = {
      id: t.id,
      title: t.title != null ? t.title : "",
      quadrant: t.quadrant || "inbox",
      done: !!t.done,
      direction: t.direction !== undefined ? t.direction : null,
      responsible: t.responsible !== undefined ? t.responsible : null,
      from: t.from !== undefined ? t.from : null,
      due: t.due !== undefined ? t.due : null,
      subtasks: t.subtasks || [],
      telegramNotified: !!t.telegramNotified,
      calendarSynced: !!t.calendarSynced
    };
    if (t.source !== undefined) data.source = t.source;
    // created_at/updated_at from the export are preserved as the true history dates, not overwritten
    // with "now" - the doc store's own createdAt/updatedAt (set on create()) would otherwise lose them.
    if (t.created_at) data.importedCreatedAt = t.created_at;
    if (t.updated_at) data.importedUpdatedAt = t.updated_at;
    store.create(data);
    createdIds.push(t.id);
  });

  console.log(`Imported ${createdIds.length} new task(s); skipped ${toSkip.length} already-existing id(s).`);

  if (createdIds.length) {
    const manifest = {
      imported_at: new Date().toISOString(), source_file: path.resolve(src),
      data_dir: require("../server/store").DATA_DIR, created_ids: createdIds
    };
    const manifestPath = path.join(path.dirname(path.resolve(src)), `import-manifest-${Date.now()}.json`);
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
    console.log(`Rollback manifest written: ${manifestPath}`);
    console.log(`To undo this exact import: node scripts/import_artifact_tasks.js --rollback ${manifestPath} --write`);
  }
}

function runRollback(manifestPath) {
  if (!manifestPath || !fs.existsSync(manifestPath)) {
    fail("usage: node scripts/import_artifact_tasks.js --rollback <import-manifest.json> [--write]");
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
  const present = ids.filter((id) => store.get(id));
  const alreadyGone = ids.length - present.length;
  if (alreadyGone) console.log(`${alreadyGone} id(s) from the manifest are already gone (removed earlier, or never written) - left alone.`);
  console.log(`${write ? "Removing" : "Would remove"} ${present.length} task(s) still present: ${present.join(", ") || "(none)"}.`);

  if (!write) {
    console.log("Dry run only - nothing was removed. Re-run with --write to apply.");
    return;
  }
  let removed = 0;
  present.forEach((id) => { if (store.delete(id)) removed += 1; });
  console.log(`Removed ${removed} task(s). This never touches any record outside this manifest's own created_ids.`);
}
