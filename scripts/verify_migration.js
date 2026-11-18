#!/usr/bin/env node
"use strict";

// Verifies a Штурман data migration (e.g. local -> Railway volume, or any DATA_DIR/UPLOADS_DIR
// move) by comparing the OLD and NEW copies record-for-record and file-for-file. Read-only: it
// never writes to either side.
//
// Because storage is a single JSON file in a documented, stable shape (server/store.js) and
// uploads are plain files on disk, "migration" is really just copying DATA_DIR and UPLOADS_DIR to
// wherever the new deployment reads them from (see docs/RAILWAY_DEPLOYMENT.md#migration) - no
// format conversion happens. This script is the "did the copy actually preserve everything"
// check the spec asks for: project IDs, stages, manual overrides, stage history, priorities,
// workloads, review items, image references and comments are all just fields inside `projects`,
// `project_history`, `project_activities`, `review_queue` etc. below - a byte-for-byte record
// match across every one of them is exactly "nothing was lost or changed by the move".
//
// Usage:
//   node scripts/verify_migration.js <old_data_dir> <old_uploads_dir> <new_data_dir> <new_uploads_dir>
//
// Exit code 0: identical (safe to cut over). Non-zero: differences are listed - do not cut over
// until this is clean, or until every difference is understood and expected (e.g. new_data_dir
// has a few extra sync_runs because it kept running briefly during the cutover window).

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

// Mirrors server/store.js's schema lists. Kept separate (not required from server/store.js)
// because that module has require-time side effects (creates DATA_DIR, takes a lock file) tied
// to process.env.DATA_DIR - this script must stay a pure, side-effect-free read of two arbitrary
// directories, neither of which is necessarily "the" DATA_DIR of the process running it.
const COLLECTIONS = ["stages", "docs", "projects", "project_activities", "project_history", "project_files",
  "agent_events", "review_queue", "sync_runs", "plan_imports", "plan_source_versions", "plan_source_events",
  "plan_source_runs", "agent_jobs"];
const OBJECTS = ["plan_source", "agent_status"];
// Operational/runtime state that legitimately differs after a cutover (in-flight job state,
// presence pings) - reported, but does not fail the check on its own.
const INFORMATIONAL_ONLY = new Set(["agent_jobs", "agent_status", "plan_source"]);

function loadStore(dataDir) {
  const file = path.join(dataDir, "shturman.json");
  if (!fs.existsSync(file)) throw new Error(`shturman.json not found in ${dataDir}`);
  return JSON.parse(fs.readFileSync(file, "utf-8"));
}

function byId(arr) {
  const m = new Map();
  for (const rec of arr || []) m.set(rec.id, rec);
  return m;
}

function diffCollection(name, oldArr, newArr) {
  const problems = [];
  const oldMap = byId(oldArr);
  const newMap = byId(newArr);
  const missing = [...oldMap.keys()].filter((id) => !newMap.has(id));
  const added = [...newMap.keys()].filter((id) => !oldMap.has(id));
  if (missing.length) problems.push(`${name}: ${missing.length} record(s) missing in new (e.g. ${missing.slice(0, 5).join(", ")})`);
  if (added.length && !INFORMATIONAL_ONLY.has(name)) {
    problems.push(`${name}: ${added.length} unexpected extra record(s) in new (e.g. ${added.slice(0, 5).join(", ")})`);
  }
  let changed = 0;
  const changedExamples = [];
  for (const [id, oldRec] of oldMap) {
    const newRec = newMap.get(id);
    if (!newRec) continue;
    if (JSON.stringify(oldRec) !== JSON.stringify(newRec)) {
      changed++;
      if (changedExamples.length < 5) changedExamples.push(id);
    }
  }
  if (changed) problems.push(`${name}: ${changed} record(s) differ (e.g. ${changedExamples.join(", ")})`);
  return { problems, informational: INFORMATIONAL_ONLY.has(name) };
}

function sha256(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function walkFiles(dir) {
  const out = [];
  if (!fs.existsSync(dir)) return out;
  (function rec(d) {
    for (const entry of fs.readdirSync(d, { withFileTypes: true })) {
      const full = path.join(d, entry.name);
      if (entry.isDirectory()) rec(full);
      else out.push(path.relative(dir, full));
    }
  })(dir);
  return out.sort();
}

function diffUploads(oldDir, newDir) {
  const problems = [];
  const oldFiles = walkFiles(oldDir);
  const newFiles = new Set(walkFiles(newDir));
  const missing = oldFiles.filter((f) => !newFiles.has(f));
  if (missing.length) {
    problems.push(`uploads: ${missing.length} file(s) missing in new (e.g. ${missing.slice(0, 5).join(", ")})`);
  }
  let mismatched = 0;
  const examples = [];
  for (const f of oldFiles) {
    if (!newFiles.has(f)) continue;
    const a = path.join(oldDir, f);
    const b = path.join(newDir, f);
    if (fs.statSync(a).size !== fs.statSync(b).size || sha256(a) !== sha256(b)) {
      mismatched++;
      if (examples.length < 5) examples.push(f);
    }
  }
  if (mismatched) problems.push(`uploads: ${mismatched} file(s) differ in content (e.g. ${examples.join(", ")})`);
  return problems;
}

function main() {
  const [oldData, oldUploads, newData, newUploads] = process.argv.slice(2);
  if (!oldData || !oldUploads || !newData || !newUploads) {
    console.error("usage: node scripts/verify_migration.js <old_data_dir> <old_uploads_dir> <new_data_dir> <new_uploads_dir>");
    process.exit(2);
  }
  const oldStore = loadStore(oldData);
  const newStore = loadStore(newData);

  let hardProblems = 0;
  const report = [];

  for (const name of COLLECTIONS) {
    const { problems, informational } = diffCollection(name, oldStore[name], newStore[name]);
    for (const p of problems) {
      report.push((informational ? "[info] " : "[FAIL] ") + p);
      if (!informational) hardProblems++;
    }
  }
  for (const name of OBJECTS) {
    const a = JSON.stringify(oldStore[name] || {});
    const b = JSON.stringify(newStore[name] || {});
    if (a !== b) {
      const informational = INFORMATIONAL_ONLY.has(name);
      report.push((informational ? "[info] " : "[FAIL] ") + `${name}: object differs`);
      if (!informational) hardProblems++;
    }
  }
  const uploadProblems = diffUploads(oldUploads, newUploads);
  for (const p of uploadProblems) {
    report.push("[FAIL] " + p);
    hardProblems++;
  }

  console.log(report.length ? report.join("\n") : "No differences found.");
  console.log("");
  console.log(hardProblems === 0
    ? "RESULT: OK - safe to cut over."
    : `RESULT: ${hardProblems} problem(s) found - do NOT cut over until these are understood and resolved.`);
  process.exit(hardProblems === 0 ? 0 : 1);
}

main();
