# Projekt_managment backend API

Implemented in `backend/server.py` (SQLite schema: `backend/schema.sql`). All bodies are JSON. Errors look like `{"error": "<code>", "message": "..."}`.

## Dashboard (read and manual edits)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | |
| GET | `/api/projects` | Optional `?external_project_id=` / `?article_number=` |
| GET | `/api/projects/:id` | Includes `attachments` |
| PATCH | `/api/projects/:id` | Manual edit: `{"changes": {...}, "actor": "user", "expected_version": n}`. Recorded with `source_type = manual`. **Protect with the dashboard's user authentication in production.** |
| GET | `/api/projects/:id/history` | Per-field history from both the agent and manual edits |
| POST | `/api/projects/:id/attachments` | `{"kind": "photo", "url": "...", "uploaded_by": "image_agent"}` |

## Agent (requires `Authorization: Bearer $PM_AGENT_API_KEY`)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/agent/schema` | `writable_fields`, allowed stages, priorities and statuses. The agent never sends a field missing from this list |
| GET | `/api/agent/projects` | All projects, including `version` and `field_sources` |
| POST | `/api/agent/projects` | `{"project": {...}, "sync": {"run_id", "source_file"}}` → `201`. **`409 duplicate`** with `existing_ids` if the id, article or make+model+product already exists |
| PATCH | `/api/agent/projects/:id` | `{"changes": {...}, "expected_version": n, "sync": {...}}`. `409 version_conflict` if the project changed since it was read |
| POST / GET | `/api/agent/sync-runs` | Store and list synchronization reports |

Status codes: `400` unsupported fields or bad JSON, `401` bad key, `404`, `409` duplicate or version conflict, `422` invalid value, `503` agent API disabled (no key configured).

Each create or update runs in **one SQLite transaction** that writes the project row and its history rows together, so a project can never be left half-updated.

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
