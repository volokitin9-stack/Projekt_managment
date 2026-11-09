-- Projekt_managment operational database (SQLite reference schema).
-- The dashboard UI reads these tables through the REST API in server.py.
-- Columns added after the first release are also added to existing databases
-- by Store.migrate() (see MIGRATIONS in server.py).

CREATE TABLE IF NOT EXISTS projects (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    external_project_id    TEXT UNIQUE,       -- stable identifier (from the file or derived by the agent)
    record_category        TEXT NOT NULL DEFAULT 'main_project'
                           CHECK (record_category IN ('main_project','additional_work','backlog')),
    project_name           TEXT NOT NULL,
    manufacturer           TEXT,
    vehicle_model          TEXT,
    product_type           TEXT,
    article_number         TEXT UNIQUE,
    assigned_engineer      TEXT,              -- responsible DESIGN engineer (project level)
    priority               TEXT CHECK (priority IN ('high','medium','low') OR priority IS NULL),
    current_stage          TEXT,
    project_status         TEXT CHECK (project_status IN ('not_started','in_progress','on_hold','completed','cancelled')
                                       OR project_status IS NULL),
    planned_start_date     TEXT,
    planned_end_date       TEXT,
    actual_completion_date TEXT,
    description            TEXT,
    comments               TEXT,              -- manual, dashboard-owned
    stages                 TEXT,              -- JSON list (legacy summary; activities live in project_activities)
    dependencies           TEXT,              -- JSON list
    extra_attributes       TEXT,              -- JSON object: preserved unmapped information
    source_file            TEXT,
    source_row             INTEGER,
    last_plan_month        TEXT,              -- latest monthly plan (YYYY-MM) that updated the project fields
    last_sync              TEXT,
    field_sources          TEXT NOT NULL DEFAULT '{}',  -- JSON {field: "agent"|"manual"} last writer per field
    version                INTEGER NOT NULL DEFAULT 1,  -- optimistic concurrency
    created_by             TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

-- One row per engineering activity (task) of a project in a given plan month.
-- Activities of earlier months are kept when later months are synchronised.
CREATE TABLE IF NOT EXISTS project_activities (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id             INTEGER NOT NULL REFERENCES projects(id),
    activity_key           TEXT NOT NULL,     -- stable within the project, e.g. "2026-11#03"
    plan_month             TEXT,              -- YYYY-MM
    sequence               INTEGER,           -- order of the task inside the month
    cycle                  INTEGER,           -- engineering cycle inside the month (1, 2, ...)
    name                   TEXT,
    activity_type          TEXT,              -- scanning | development | test_prototype | measurement |
                                              -- documentation | testing | transfer_extension | engineering_change
    stage                  TEXT,              -- one of the five main stages, NULL for additional types
    task_type              TEXT,              -- original "План на месяц"
    project_stage_original TEXT,              -- original "Стадия проекта"
    task_group             TEXT,              -- original "Группа задач"
    assignee               TEXT,              -- person/team doing this activity
    assignee_original      TEXT,              -- original value when it is not a person (e.g. "Н")
    planned_hours          REAL,
    actual_hours           REAL,
    hours_by_date          TEXT,              -- JSON {"2026-11-03": 3.5}
    progress_status        TEXT CHECK (progress_status IN ('planned','in_progress','done','blocked','cancelled')
                                       OR progress_status IS NULL),
    effort_ratio           REAL,              -- actual / planned hours (effort, NOT completion)
    items_ok               TEXT,              -- original "ОК"
    items_nok              TEXT,              -- original "NOK"
    result_raw             TEXT,              -- original "Факт"
    comment                TEXT,              -- comment from the plan
    manual_note            TEXT,              -- dashboard-owned note
    details                TEXT,              -- JSON: further original values
    source_file            TEXT,
    source_sheet           TEXT,
    source_row             INTEGER,
    removed_from_source    INTEGER NOT NULL DEFAULT 0,  -- task disappeared from the month's file (kept, not deleted)
    field_sources          TEXT NOT NULL DEFAULT '{}',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE (project_id, activity_key)
);
CREATE INDEX IF NOT EXISTS idx_activities_project ON project_activities (project_id, plan_month, sequence);

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
    event_type     TEXT NOT NULL,             -- project_created | project_updated | activity_added |
                                              -- activity_updated | activity_removed_from_source
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
