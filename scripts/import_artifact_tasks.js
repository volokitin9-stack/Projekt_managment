"use strict";

// One-time import of the personal Kanban board's "tasks" collection, exported from the Claude
// Artifact ("Экспорт задач в JSON" button) as one JSON file, into this app's own "tasks" doc
// store (server/docStore.js) - see docs/TELEGRAM_INTEGRATION.md "Stage 4 - data migration".
//
//   node scripts/import_artifact_tasks.js path/to/shturman_tasks_export.json            # dry run
//   node scripts/import_artifact_tasks.js path/to/shturman_tasks_export.json --write    # import
//
// * Stop the server first (the data directory is used by one process at a time - same rule as
//   scripts/import-sqlite.js).
// * Idempotent by id: a record whose id already exists in this target is skipped, never
//   overwritten - safe to re-run (e.g. after exporting again with newly-added tasks) without
//   ever duplicating or clobbering anything already imported or created directly here.
// * Only ever adds new task documents. Never deletes, never touches any other collection.
// * The export file is only read, never modified.

require("../server/env");   // .env from the project folder, backslashes kept literally
const fs = require("fs");

const src = process.argv[2];
const write = process.argv.includes("--write");
if (!src || !fs.existsSync(src)) {
  console.error("usage: node scripts/import_artifact_tasks.js <shturman_tasks_export.json> [--write]");
  process.exit(2);
}

let payload;
try {
  payload = JSON.parse(fs.readFileSync(src, "utf-8"));
} catch (e) {
  console.error(`${src} is not valid JSON: ${e.message}`);
  process.exit(2);
}
const tasks = Array.isArray(payload.tasks) ? payload.tasks : null;
if (!tasks) {
  console.error(`${src} has no "tasks" array - is this a shturman_tasks_export.json file?`);
  process.exit(2);
}

const { createDocStore } = require("../server/docStore");
const store = createDocStore("tasks");

let created = 0;
let skipped = 0;
const skippedIds = [];

for (const t of tasks) {
  if (!t || !t.id) {
    console.warn("skipping a record with no id - this should not happen in a real export file");
    continue;
  }
  const existing = store.get(t.id);
  if (existing) {
    skipped += 1;
    skippedIds.push(t.id);
    continue;
  }
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
  if (write) store.create(data);
  created += 1;
}

console.log(`${write ? "Imported" : "Would import"} ${created} new task(s); skipped ${skipped} already-existing id(s).`);
if (skippedIds.length) console.log(`Skipped ids: ${skippedIds.join(", ")}`);
if (!write) console.log("Dry run only - nothing was written. Re-run with --write to apply.");
