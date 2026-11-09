-- Projekt_managment operational database (SQLite reference schema).
-- The dashboard UI reads these tables through the REST API in server.py.

CREATE TABLE IF NOT EXISTS projects (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    external_project_id    TEXT UNIQUE,
    project_name           TEXT NOT NULL,
    manufacturer           TEXT,
    vehicle_model          TEXT,
    product_type           TEXT,
    article_number         TEXT UNIQUE,
    assigned_engineer      TEXT,
    priority               TEXT CHECK (priority IN ('high','medium','low') OR priority IS NULL),
    current_stage          TEXT,
    project_status         TEXT CHECK (project_status IN ('not_started','in_progress','on_hold','completed','cancelled')
                                       OR project_status IS NULL),
    planned_start_date     TEXT,
    planned_end_date       TEXT,
    actual_completion_date TEXT,
    description            TEXT,
    comments               TEXT,              -- manual, dashboard-owned
    stages                 TEXT,              -- JSON list of activities
    dependencies           TEXT,              -- JSON list
    extra_attributes       TEXT,              -- JSON object: preserved unmapped information
    source_file            TEXT,
    source_row             INTEGER,
    last_sync              TEXT,
    field_sources          TEXT NOT NULL DEFAULT '{}',  -- JSON {field: "agent"|"manual"} last writer per field
    version                INTEGER NOT NULL DEFAULT 1,  -- optimistic concurrency
    created_by             TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    kind        TEXT NOT NULL,                -- photo | document | other
    url         TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,                -- manual | image_agent | ...
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES projects(id),
    event_type     TEXT NOT NULL,             -- project_created | project_updated
    field          TEXT,
    previous_value TEXT,                      -- JSON
    new_value      TEXT,                      -- JSON
    source_type    TEXT NOT NULL,             -- agent | manual
    source         TEXT,                      -- e.g. source file name
    actor          TEXT,
    sync_run_id    TEXT,
    timestamp      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_project ON project_history (project_id, id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source_file TEXT,
    file_sha256 TEXT,
    mode        TEXT NOT NULL,                -- dry_run | sync
    status      TEXT NOT NULL,
    report      TEXT                          -- JSON
);
