"use strict";

const fs = require("fs");
const path = require("path");
const Database = require("better-sqlite3");

const DATABASE_PATH = process.env.DATABASE_PATH || "./data/app.sqlite";

function ensureParentDir(filePath) {
  const dir = path.dirname(filePath);
  if (dir && dir !== "." && !fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

ensureParentDir(DATABASE_PATH);
const db = new Database(DATABASE_PATH);
db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

// ---- schema ----
// Generic document collections ("tasks", "globals") mirror the shape the original
// Artifact used with its ArtifactData store: an arbitrary JSON blob per row, plus a
// `version` column used the same way ArtifactData's `if_version` was — the caller
// reads a version, sends it back on write, and a mismatch is rejected instead of
// silently overwritten.
db.exec(`
  CREATE TABLE IF NOT EXISTS docs (
    collection TEXT NOT NULL,
    id TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (collection, id)
  );

  CREATE TABLE IF NOT EXISTS stages (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    external_project_id TEXT,
    data TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  );
  CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_external_id
    ON projects(external_project_id) WHERE external_project_id IS NOT NULL;

  CREATE TABLE IF NOT EXISTS project_files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'image',
    storage_key TEXT NOT NULL,
    original_filename TEXT,
    mime_type TEXT,
    size INTEGER,
    is_primary INTEGER NOT NULL DEFAULT 0,
    caption TEXT,
    source TEXT,
    telegram_message_id TEXT,
    telegram_chat_id TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL
  );
  CREATE UNIQUE INDEX IF NOT EXISTS idx_project_files_telegram_dedupe
    ON project_files(telegram_chat_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL AND telegram_chat_id IS NOT NULL;
  CREATE INDEX IF NOT EXISTS idx_project_files_project ON project_files(project_id);

  CREATE TABLE IF NOT EXISTS project_activity (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    source TEXT,
    actor TEXT,
    previous_value TEXT,
    new_value TEXT,
    at TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_project_activity_project ON project_activity(project_id);

  CREATE TABLE IF NOT EXISTS agent_events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT,
    event_type TEXT,
    payload TEXT,
    received_at TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS review_queue (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
  );
`);

// Versioned migrations on top of the baseline schema above (see server/migrations.js).
const migrations = require("./migrations");
const migrationResult = migrations.migrate(db, DATABASE_PATH);
if (migrationResult.applied.length) {
  // eslint-disable-next-line no-console
  console.log(`database migrated to v${migrations.currentVersion(db)} (applied ${migrationResult.applied.join(", ")})` +
    (migrationResult.backup ? `; backup written: ${migrationResult.backup}` : ""));
}

module.exports = db;
module.exports.DATABASE_PATH = DATABASE_PATH;
module.exports.migrationResult = migrationResult;
module.exports.schemaVersion = () => migrations.currentVersion(db);
