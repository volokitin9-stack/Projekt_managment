"use strict";
// Unit tests for server/migrate.js's one-time staging migration, run directly (no Express app,
// no server/store.js) so each scenario gets full control of the filesystem state.
// Run by tests/test_staging_migration.py. Usage: node tests/js/migrate_unit.js <scratch dir>

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { runStagingMigrationIfRequested, REQUIRED_COLLECTIONS, OPTIONAL_COLLECTIONS } = require("../../server/migrate");

const [scratch] = process.argv.slice(2);
const results = {};
let n = 0;
const fresh = (name) => path.join(scratch, `${n += 1}-${name}`);

function emptyShturman(extra) {
  return Object.assign({
    meta: { format: "shturman-json", schema_version: 1 },
    stages: [], docs: [], projects: [], project_activities: [], project_history: [], project_files: [],
    agent_events: [], review_queue: [], sync_runs: [], plan_imports: [], plan_source_versions: [],
    plan_source_events: [], plan_source_runs: [], agent_jobs: [], plan_source: {}, agent_status: {}
  }, extra || {});
}

/** <dir>/extracted/data/shturman.json, uploads/ and (nested) data/plan-imports/, matching
 * scripts/snapshot_data.js's own backup layout (data/ and uploads/ as siblings). */
function buildStaging(dir, { json, uploadFile, planImportFile, staleLock } = {}) {
  const dataDir = path.join(dir, "extracted", "data");
  const uploadsDir = path.join(dir, "extracted", "uploads");
  fs.mkdirSync(dataDir, { recursive: true });
  fs.mkdirSync(uploadsDir, { recursive: true });
  const sourceJson = path.join(dataDir, "shturman.json");
  if (typeof json === "string") fs.writeFileSync(sourceJson, json);
  else fs.writeFileSync(sourceJson, JSON.stringify(json || emptyShturman()));
  if (uploadFile) fs.writeFileSync(path.join(uploadsDir, "photo.jpg"), uploadFile);
  if (planImportFile) {
    const pi = path.join(dataDir, "plan-imports");
    fs.mkdirSync(pi, { recursive: true });
    fs.writeFileSync(path.join(pi, "report.xlsx"), planImportFile);
  }
  if (staleLock) fs.writeFileSync(path.join(dataDir, ".shturman.lock"), staleLock);
  return sourceJson;
}

function targetEnv(targetDir, sourceJson, extra) {
  return Object.assign({
    RAILWAY_MIGRATE_FROM_STAGING: "1",
    MIGRATION_STAGING_SOURCE_JSON: sourceJson,
    DATA_DIR: path.join(targetDir, "data"),
    UPLOADS_DIR: path.join(targetDir, "uploads"),
    PLAN_IMPORT_DIR: path.join(targetDir, "data", "plan-imports")
  }, extra || {});
}

// 1. flag unset: zero filesystem access, no matter what else is set.
{
  const target = fresh("flag-unset");
  const sourceJson = buildStaging(fresh("staging-unused"), { json: emptyShturman() });
  const env = targetEnv(target, sourceJson);
  delete env.RAILWAY_MIGRATE_FROM_STAGING;
  const r = runStagingMigrationIfRequested(env);
  assert.strictEqual(r.ran, false);
  assert.strictEqual(r.reason, "flag not set");
  assert.ok(!fs.existsSync(target), "target dir must not even be created when the flag is unset");
  results.flag_unset_zero_touch = true;
}

// 2. success: staged database + uploads + nested plan-imports all land in the right places;
//    original staging files are left exactly as they were.
let successTarget;
{
  successTarget = fresh("success");
  const staging = fresh("staging-success");
  const staged = emptyShturman({ projects: [{ id: "proj-marker-1", name: "Migrated project" }] });
  const sourceJson = buildStaging(staging, { json: staged, uploadFile: "upload-bytes", planImportFile: "xlsx-bytes" });
  const beforeSourceBytes = fs.readFileSync(sourceJson);

  const r = runStagingMigrationIfRequested(targetEnv(successTarget, sourceJson));
  assert.strictEqual(r.ran, true);

  const target = targetEnv(successTarget, sourceJson);
  const written = JSON.parse(fs.readFileSync(path.join(target.DATA_DIR, "shturman.json"), "utf-8"));
  assert.strictEqual(written.projects[0].id, "proj-marker-1");
  assert.strictEqual(fs.readFileSync(path.join(target.UPLOADS_DIR, "photo.jpg"), "utf-8"), "upload-bytes");
  assert.strictEqual(fs.readFileSync(path.join(target.PLAN_IMPORT_DIR, "report.xlsx"), "utf-8"), "xlsx-bytes");

  const marker = path.join(target.DATA_DIR, ".migration-from-staging-complete.json");
  assert.ok(fs.existsSync(marker), "completion marker must be written on success");
  const info = JSON.parse(fs.readFileSync(marker, "utf-8"));
  assert.strictEqual(info.record_counts.projects, 1);

  // requirement 4: the original staged source is preserved, byte for byte.
  assert.ok(fs.readFileSync(sourceJson).equals(beforeSourceBytes), "staging source must be left untouched");
  results.success = true;
}

// 3. idempotent: a second (and third, with the staging content now different) run is a safe no-op.
{
  const staging2 = fresh("staging-rerun");
  const sourceJson2 = buildStaging(staging2, { json: emptyShturman({ projects: [{ id: "should-not-appear" }] }) });
  const env = targetEnv(successTarget, sourceJson2); // same already-migrated target as scenario 2

  const r2 = runStagingMigrationIfRequested(env);
  assert.strictEqual(r2.ran, false);
  assert.strictEqual(r2.reason, "already completed");

  const stillWritten = JSON.parse(fs.readFileSync(path.join(env.DATA_DIR, "shturman.json"), "utf-8"));
  assert.strictEqual(stillWritten.projects[0].id, "proj-marker-1", "a completed migration must never re-run");
  results.idempotent = true;
}

// 4. a stale .shturman.lock sitting in the staged export is never copied into the target.
{
  const target = fresh("stale-lock");
  const staging = fresh("staging-stale-lock");
  const sourceJson = buildStaging(staging, { json: emptyShturman(), staleLock: "999999" });
  const r = runStagingMigrationIfRequested(targetEnv(target, sourceJson));
  assert.strictEqual(r.ran, true);
  const targetDataDir = targetEnv(target, sourceJson).DATA_DIR;
  assert.ok(!fs.existsSync(path.join(targetDataDir, ".shturman.lock")), "stale lock must not be copied");
  results.stale_lock_excluded = true;
}

// 5. a corrupted/invalid source fails loudly and leaves any pre-existing target completely alone.
{
  const target = fresh("bad-source");
  const targetDataDir = path.join(target, "data");
  fs.mkdirSync(targetDataDir, { recursive: true });
  const existing = emptyShturman({ projects: [{ id: "pre-existing-live-data" }] });
  fs.writeFileSync(path.join(targetDataDir, "shturman.json"), JSON.stringify(existing));

  const staging = fresh("staging-bad");
  const sourceJson = buildStaging(staging, { json: "{ not actually json" });
  assert.throws(() => runStagingMigrationIfRequested(targetEnv(target, sourceJson)), /not valid JSON/);

  const unchanged = JSON.parse(fs.readFileSync(path.join(targetDataDir, "shturman.json"), "utf-8"));
  assert.strictEqual(unchanged.projects[0].id, "pre-existing-live-data", "live data must be untouched on failure");
  assert.ok(!fs.existsSync(path.join(targetDataDir, ".migration-from-staging-complete.json")));
  results.corrupted_source_safe_fail = true;

  // same for a well-formed JSON file that is missing required sections.
  const sourceJson2 = buildStaging(fresh("staging-missing-section"), { json: { meta: { format: "shturman-json" } } });
  assert.throws(() => runStagingMigrationIfRequested(targetEnv(target, sourceJson2)), /missing required legacy section/);
  results.missing_sections_safe_fail = true;
}

// 6. refuses to migrate while another live process holds the target's lock.
{
  const target = fresh("locked");
  const targetDataDir = path.join(target, "data");
  fs.mkdirSync(targetDataDir, { recursive: true });
  fs.writeFileSync(path.join(targetDataDir, "shturman.json"), JSON.stringify(emptyShturman()));
  // pid 1 is always alive in a container/VM and (bar the unusual case of this script itself
  // running as pid 1) different from our own pid - good enough to simulate "another process".
  const otherPid = process.pid === 1 ? 2 : 1;
  fs.writeFileSync(path.join(targetDataDir, ".shturman.lock"), String(otherPid));

  const sourceJson = buildStaging(fresh("staging-locked"), { json: emptyShturman() });
  assert.throws(() => runStagingMigrationIfRequested(targetEnv(target, sourceJson)), /in use by another/);
  results.refuses_while_locked = true;
}

// 7. a real legacy export: has every REQUIRED_COLLECTIONS section (the original JSON-storage
//    format) but predates every later-added OPTIONAL_COLLECTIONS section, agent_jobs included -
//    it must migrate successfully, with every missing optional section backfilled to [], and
//    every existing record/field preserved exactly as it was in the legacy file.
{
  const target = fresh("legacy-no-agent-jobs");
  const staging = fresh("staging-legacy");
  const legacy = { meta: { format: "shturman-json", schema_version: 1 } };
  REQUIRED_COLLECTIONS.forEach((c) => { legacy[c] = []; });
  legacy.projects = [{ id: "legacy-proj-1", name: "Pre-Railway project", custom_field: "must survive" }];
  // Deliberately does NOT set any OPTIONAL_COLLECTIONS key (agent_jobs, plan_source_versions,
  // plan_source_events, plan_source_runs) - this is the exact shape of the real staged database.
  OPTIONAL_COLLECTIONS.forEach((c) => assert.ok(!(c in legacy), `test setup bug: ${c} should be absent`));

  const sourceJson = buildStaging(staging, { json: legacy });
  const r = runStagingMigrationIfRequested(targetEnv(target, sourceJson));
  assert.strictEqual(r.ran, true, "a legacy export missing only optional collections must migrate, not be rejected");

  const targetDataDir = targetEnv(target, sourceJson).DATA_DIR;
  const written = JSON.parse(fs.readFileSync(path.join(targetDataDir, "shturman.json"), "utf-8"));
  OPTIONAL_COLLECTIONS.forEach((c) => {
    assert.ok(Array.isArray(written[c]), `${c} must be backfilled to an empty array`);
    assert.strictEqual(written[c].length, 0);
  });
  assert.deepStrictEqual(written.projects, legacy.projects, "existing records/fields must be preserved unchanged");
  assert.strictEqual(r.info.record_counts.agent_jobs, 0);
  results.legacy_missing_optional_collections_backfilled = true;
}

console.log(JSON.stringify(results));
process.exit(0);
