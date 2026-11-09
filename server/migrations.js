"use strict";

// Versioned, forward-only database migrations.
//
// db.js creates the original (v1) schema with CREATE TABLE IF NOT EXISTS, exactly
// as before. Everything added after that lives here as numbered steps. Each step
// runs once, inside a transaction, and is recorded in `schema_migrations`.
//
// Data-safety rules:
// * steps only ADD tables/columns/indexes - nothing is dropped or rewritten;
// * before the first pending step runs on a database that already contains
//   data, a copy of the database file is written next to it
//   (<file>.pre-migration-v<N>-<timestamp>.bak).

const fs = require("fs");
const path = require("path");

function columnExists(db, table, column) {
  return db.prepare(`PRAGMA table_info(${table})`).all().some((c) => c.name === column);
}

function addColumn(db, table, column, ddl) {
  if (!columnExists(db, table, column)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${ddl}`);
}

const MIGRATIONS = [
  {
    version: 2,
    name: "projects: category, confirmed-stage timestamp, field sources, plan month",
    up(db) {
      addColumn(db, "projects", "record_category", "TEXT NOT NULL DEFAULT 'main_project'");
      // When the CURRENT confirmed stage was entered. NULL = unknown (existing
      // projects keep NULL: their stage-entry date was never recorded).
      addColumn(db, "projects", "stage_entered_at", "TEXT");
      // Last writer per field ("manual" | "agent" | "seed") - protects manual edits.
      addColumn(db, "projects", "field_sources", "TEXT NOT NULL DEFAULT '{}'");
      addColumn(db, "projects", "last_plan_month", "TEXT");
      db.exec("CREATE INDEX IF NOT EXISTS idx_projects_category ON projects(record_category)");
    }
  },
  {
    version: 3,
    name: "project_activities: engineering activities per project and plan month",
    up(db) {
      db.exec(`
        CREATE TABLE IF NOT EXISTS project_activities (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          activity_key TEXT NOT NULL,
          plan_month TEXT,
          sequence INTEGER,
          cycle INTEGER,
          name TEXT,
          activity_type TEXT,
          stage TEXT,
          task_type TEXT,
          project_stage_original TEXT,
          task_group TEXT,
          assignee TEXT,
          assignee_original TEXT,
          planned_hours REAL,
          actual_hours REAL,
          hours_by_date TEXT,
          progress_status TEXT,
          effort_ratio REAL,
          items_ok TEXT,
          items_nok TEXT,
          result_raw TEXT,
          comment TEXT,
          manual_note TEXT,
          details TEXT,
          source_file TEXT,
          source_sheet TEXT,
          source_row INTEGER,
          removed_from_source INTEGER NOT NULL DEFAULT 0,
          field_sources TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (project_id, activity_key)
        );
        CREATE INDEX IF NOT EXISTS idx_activities_project ON project_activities(project_id, plan_month, sequence);
      `);
    }
  },
  {
    version: 4,
    name: "project_files: description, technical comment, event timestamp",
    up(db) {
      addColumn(db, "project_files", "technical_comment", "TEXT");
      addColumn(db, "project_files", "event_at", "TEXT");
      addColumn(db, "project_files", "telegram_ref", "TEXT");
    }
  },
  {
    version: 5,
    name: "sync_runs and plan_imports",
    up(db) {
      db.exec(`
        CREATE TABLE IF NOT EXISTS sync_runs (
          id TEXT PRIMARY KEY,
          source_file TEXT,
          file_sha256 TEXT,
          plan_month TEXT,
          mode TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          counts TEXT,
          report TEXT,
          error TEXT,
          review_status TEXT
        );
        CREATE TABLE IF NOT EXISTS plan_imports (
          id TEXT PRIMARY KEY,
          original_name TEXT,
          sha256 TEXT NOT NULL,
          stored_path TEXT NOT NULL,
          size INTEGER,
          plan_month TEXT,
          status TEXT NOT NULL,
          fingerprint TEXT,
          dry_run_id TEXT,
          sync_run_id TEXT,
          summary TEXT,
          error TEXT,
          uploaded_at TEXT NOT NULL,
          approved_at TEXT,
          approved_by TEXT
        );
      `);
    }
  },
  {
    version: 6,
    name: "review_queue: idempotency key, source, resolution",
    up(db) {
      addColumn(db, "review_queue", "dedupe_key", "TEXT");
      addColumn(db, "review_queue", "source", "TEXT");
      addColumn(db, "review_queue", "updated_at", "TEXT");
      addColumn(db, "review_queue", "resolved_at", "TEXT");
      addColumn(db, "review_queue", "resolution", "TEXT");
      db.exec("CREATE UNIQUE INDEX IF NOT EXISTS idx_review_queue_dedupe ON review_queue(dedupe_key) " +
              "WHERE dedupe_key IS NOT NULL");
    }
  }
];

function backupBeforeMigrating(db, databasePath, firstPending) {
  if (!databasePath || databasePath === ":memory:" || !fs.existsSync(databasePath)) return null;
  const hasData = db.prepare("SELECT COUNT(*) AS n FROM projects").get().n > 0 ||
    db.prepare("SELECT COUNT(*) AS n FROM docs").get().n > 0;
  if (!hasData) return null;
  db.pragma("wal_checkpoint(TRUNCATE)");
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const target = `${databasePath}.pre-migration-v${firstPending}-${stamp}.bak`;
  fs.copyFileSync(databasePath, target);
  return target;
}

function migrate(db, databasePath) {
  db.exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)`);
  if (!db.prepare("SELECT 1 FROM schema_migrations WHERE version = 1").get()) {
    db.prepare("INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, ?, ?)")
      .run("baseline schema (server/db.js)", new Date().toISOString());
  }
  const applied = new Set(db.prepare("SELECT version FROM schema_migrations").all().map((r) => r.version));
  const pending = MIGRATIONS.filter((m) => !applied.has(m.version));
  if (!pending.length) return { applied: [], backup: null };
  const backup = backupBeforeMigrating(db, databasePath, pending[0].version);
  const done = [];
  for (const m of pending) {
    db.transaction(() => {
      m.up(db);
      db.prepare("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)")
        .run(m.version, m.name, new Date().toISOString());
    })();
    done.push(m.version);
  }
  return { applied: done, backup: backup ? path.basename(backup) : null };
}

function currentVersion(db) {
  return db.prepare("SELECT MAX(version) AS v FROM schema_migrations").get().v;
}

module.exports = { migrate, currentVersion, MIGRATIONS };
