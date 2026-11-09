# Projekt_managment backend API

Implemented in `backend/server.py` (SQLite schema: `backend/schema.sql`). All bodies are JSON. Errors look like `{"error": "<code>", "message": "..."}`.

## Dashboard (read and manual edits)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | |
| GET | `/api/projects` | Optional `?external_project_id=`, `?article_number=`, `?category=main_project\|additional_work\|backlog`, `?include=activities` |
| GET | `/api/projects/:id` | Includes `attachments` |
| PATCH | `/api/projects/:id` | Manual edit: `{"changes": {...}, "actor": "user", "expected_version": n}`. Recorded with `source_type = manual`. **Protect with the dashboard's user authentication in production.** |
| GET | `/api/projects/:id/history` | Per-field history from both the agent and manual edits, including activity events |
| GET | `/api/projects/:id/activities` | All activities of all months |
| PATCH | `/api/projects/:id/activities/:aid` | Manual activity edit: `progress_status`, `manual_note`, `assignee`. The agent never overwrites these afterwards |
| POST | `/api/projects/:id/attachments` | `{"kind": "photo", "url": "...", "uploaded_by": "image_agent"}` |

## Agent (requires `Authorization: Bearer $PM_AGENT_API_KEY`)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/agent/schema` | `writable_fields`, allowed stages, priorities and statuses. The agent never sends a field missing from this list |
| GET | `/api/agent/projects` | All projects, including `version` and `field_sources` |
| POST | `/api/agent/projects` | `{"project": {...}, "activities": [...], "activity_scope": {"plan_month": "2026-11"}, "sync": {"run_id", "source_file"}}` → `201`. **`409 duplicate`** with `existing_ids` if the id, the article, or make+model+product *within the same category* already exists |
| PATCH | `/api/agent/projects/:id` | `{"changes": {...}, "activities": [...], "activity_scope": {...}, "expected_version": n, "sync": {...}}`. `409 version_conflict` if the project changed since it was read |
| POST / GET | `/api/agent/sync-runs` | Store and list synchronization reports |

Status codes: `400` unsupported fields or bad JSON, `401` bad key, `404`, `409` duplicate or version conflict, `422` invalid value, `503` agent API disabled (no key configured).

Each create or update runs in **one SQLite transaction** that writes the project row, its activities and its history rows together, so a project can never be left half-updated.

### Activities (`project_activities`)

Activities are **month-scoped**. A request with `activity_scope.plan_month = 2026-12` inserts or updates only December activities (matched by `activity_key`) and leaves every other month untouched. A December activity that is missing from a later December file is flagged `removed_from_source` and is never deleted. A field last edited manually is kept, and the response reports it under `activity_summary.kept_manual`.

| Field | Meaning |
|---|---|
| `activity_key` | stable key inside the project, e.g. `2026-11#c1#кд#1` (month, cycle, task, occurrence) |
| `plan_month`, `sequence`, `cycle` | month, order inside the month, engineering cycle number |
| `activity_type` / `stage` | one of the 5 main stages (`stage` set), or `testing` / `transfer_extension` / `engineering_change` (`stage` empty) |
| `task_type`, `project_stage_original`, `task_group` | original values from the plan |
| `assignee`, `assignee_original` | person or team doing the activity; the original value when it is not a verified person (e.g. «Н») |
| `planned_hours`, `actual_hours`, `hours_by_date`, `effort_ratio` | effort: hours, hours per date, actual/planned |
| `progress_status` | `planned` / `in_progress`, set from evidence only; `done` / `blocked` / `cancelled` are set manually only |
| `items_ok`, `items_nok`, `result_raw` | original «ОК», «NOK», «Факт» values (meaning not interpreted) |
| `source_file`, `source_sheet`, `source_row` | original Excel reference |

### Categories

`record_category` is `main_project` (engineering projects), `additional_work` (unplanned assignments) or `backlog` (future or pending work). Duplicate detection never crosses categories. `last_plan_month` is the latest monthly plan that set the project fields; an older month only updates its own activities.

## Project fields

| Field | Type | Owner (default policy) |
|---|---|---|
| `external_project_id` | text, unique | planning file |
| `project_name` | text, required | planning file |
| `manufacturer`, `vehicle_model`, `product_type` | text | planning file (identity) |
| `article_number` | text, unique | planning file (identity) |
| `assigned_engineer` | text | planning file |
| `priority` | `high` / `medium` / `low` | planning file |
| `current_stage` | `scanning` / `development` / `test_prototype` / `measurement` / `documentation` / `extra:<code>` | planning file |
| `project_status` | `not_started` / `in_progress` / `on_hold` / `completed` / `cancelled` | planning file |
| `planned_start_date`, `planned_end_date`, `actual_completion_date` | `YYYY-MM-DD` | planning file |
| `description` | text | dashboard |
| `comments` | text | dashboard (manual only, not writable by the agent) |
| `stages` | JSON list of activities `{name, stage, stage_mapped, status, status_raw, planned_start, planned_end, actual_end, engineer, depends_on, comment}` | planning file |
| `dependencies` | JSON list `{raw, resolved}` | planning file |
| `extra_attributes` | JSON object: comments from the file, unmapped columns, original values, `derived_fields` | planning file (merged, never replaced) |
| `source_file`, `source_row` | traceability | planning file |
| `field_sources` | `{field: "agent" \| "manual"}`, last writer of each field | backend |
| `version` | optimistic-concurrency counter | backend |
| `last_sync`, `created_*`, `updated_at` | | backend |

## History record

```json
{"project_id": 1, "event_type": "project_updated", "field": "current_stage",
 "previous_value": "test_prototype", "new_value": "measurement",
 "source_type": "agent", "source": "engineering_plan_v2.xlsx",
 "actor": "plan_agent", "sync_run_id": "20261109T050457Z-da0fae", "timestamp": "2026-11-09T05:04:57+00:00"}
```

`source_type` tells agent updates (`agent`) apart from manual dashboard updates (`manual`).
