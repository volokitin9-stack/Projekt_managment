# Штурман API

All bodies are JSON unless noted. Errors look like `{"error": "...", "details": [...]}`. Agent endpoints also return a machine-readable `code` (`unsupported_fields`, `invalid_value`, `duplicate`, `version_conflict`).

## Dashboard endpoints

There is no user authentication, as in the original app. Protect them at the network or proxy level.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | `{ok, time, schema_version}` |
| GET | `/api/stages` | configured Kanban stages, in order |
| GET | `/api/projects` | all records with activities, workload, history and files. Filters: `category`, `make`, `model`, `product`, `engineer`, `priority`, `status`, `q` |
| GET | `/api/projects/:id` | one record |
| GET | `/api/projects/:id/history` | chronological change log |
| PATCH | `/api/projects/:id` | manual edit `{changes, version}`. Editable: `project_name`, `article_number`, `assigned_engineer`, `priority`, `description`, `planned_deadline`, `work_status`. Stale `version` → `409` |
| POST | `/api/projects/:id/stage` | confirm or move the stage `{stage, version?, note?}`. Use `stage: null` to un-confirm. Recorded as `stage_transition`, and the stage timer restarts |
| POST | `/api/projects/:id/comments` | `{text}` |
| POST | `/api/projects/:id/files` | multipart `file`, `caption`, `technical_comment`. Accepts jpeg, png, webp, gif or pdf |
| PATCH | `/api/projects/:id/activities/:aid` | manual `progress_status`, `manual_note` or `assignee`. Never overwritten by later imports |
| GET | `/api/review-queue` | open items (`?all=1`, `?kind=plan_record\|image\|project`) |
| POST | `/api/review-queue/:id/resolve` | mark handled |
| GET | `/api/sync-runs` | dry runs and syncs recorded by the agent |
| GET/POST | `/api/plan-imports` | POST multipart `file` and `month` (YYYY-MM) runs the **dry run only** |
| GET | `/api/plan-imports/:id` | result, analysis report and sync report |
| POST | `/api/plan-imports/:id/approve` | `{confirm: true, approved_by}` approves this analysis's mapping and synchronizes |
| GET/POST/PATCH/DELETE | `/api/tasks`, `/api/globals` | the original Доска and Глобально collections (unchanged) |

## Automatic plan retrieval (`/api/plan-source`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/plan-source/status` | source, current month, last check / change / sync, status, counts, last error, current version, recent versions and events |
| GET | `/api/plan-source/preview[?version=ID]` | project-level changes of a version's dry run (new / update / review, changed fields, activity changes, conflicts) |
| POST | `/api/plan-source/check` | check the source now. `{"dry_run": true}` analyses the current file again even when unchanged (never writes) |
| POST | `/api/plan-source/approve` | `{"confirm": true, "approved_by": "..."}`: manual mode, approve and sync the pending version |
| POST | `/api/plan-source/auto-sync` | `{"enabled": true|false, "confirm": true}`: switch automatic mode (stored; overrides `PLAN_AUTO_SYNC_ENABLED`) |

`check` returns `{result, state}`; `result.status` is one of `not_configured`, `busy` (HTTP 409, another run is active), `unchanged`, `awaiting_approval`, `blocked`, `dry_run`, `synced`, `failed`, `sync_failed`, `waiting` (file locked/being written; retried) or `error` (with `code`).

## Agent endpoints

These require `Authorization: Bearer <AGENT_API_KEY>`.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/agent/schema` | writable fields, activity fields, categories, stages, priorities and supported features |
| GET | `/api/agent/projects` | `{projects}` with versions, field sources and activities |
| POST | `/api/agent/projects` | **envelope** `{project, activities?, activity_scope?, sync?, create_only?}` → `201`, or `409 duplicate` with `existing_ids`. The **legacy flat body** (original contract) still upserts by `external_project_id` |
| PATCH | `/api/agent/projects/:id` | **envelope** `{changes, expected_version, activities?, activity_scope?, sync?}`. `:id` is the internal or external ID. Legacy flat body still accepted. Response includes `conflicts` (manual values kept) and `activity_summary` |
| POST | `/api/agent/projects/:id/stage-transitions` | `{stage, confirmed: true, at?, note?}`. Explicit, confirmed stage change. Without `confirmed: true` → `400` |
| POST | `/api/agent/projects/:id/images` | multipart. `telegram_chat_id` + `telegram_message_id` make it idempotent. Also `caption`, `technical_comment`, `event_at`, `telegram_ref`, `is_primary` |
| POST | `/api/agent/telegram/photos` | multipart with identifiers. Attached only on a reliable match, otherwise queued for review (`202`) |
| POST | `/api/agent/images/unassigned` | photo the agent could not match, queued for review |
| POST | `/api/agent/projects/:id/events` | idempotent by `event_id` |
| POST | `/api/agent/review-queue` | `{dedupe_key, kind, payload, reason, source}`. Idempotent upsert |
| POST/GET | `/api/agent/sync-runs` | record or list import runs |

### Project fields written by the planning agent

These are the fields listed in `GET /api/agent/schema` → `writable_fields`:

| Field | Meaning |
|---|---|
| `external_project_id` | stable identifier (`PRJ-…`, `AW-…` or `BL-…`, derived from the verified identity fields) |
| `record_category` | `main_project`, `additional_work` or `backlog` |
| `manufacturer`, `vehicle_model`, `product_type`, `article_number`, `project_name` | identity and display |
| `assigned_engineer` | responsible design engineer, or empty |
| `priority`, `priority_original` | `critical`, `high`, `normal`, `low` or `null` (unspecified); `priority_original` is the plan's original value, e.g. `0` |
| `planned_stage` | stage suggested by the plan. **Not** the confirmed stage |
| `work_status` | effort-based status (`in_progress` when hours were logged), never completion |
| `planned_deadline`, `planned_start_date`, `actual_completion_date` | only if the file states them |
| `source_file`, `source_sheet`, `source_row`, `source_rows`, `last_plan_month` | traceability |
| `extra_attributes` | original values that have no dedicated field |

`status` (the confirmed stage) is not agent-writable through these endpoints. It changes only through a stage transition.

### Activities

`activities` are upserted **per plan month** (`activity_scope.plan_month`), in the same transaction as the project write.
* Activities of other months are kept.
* An activity missing from its month's new file is flagged `removed_from_source` and never deleted.
* Fields last edited manually are kept, and the response reports them in `activity_summary.kept_manual`.

The activity fields are `activity_key`, `plan_month`, `sequence`, `cycle`, `name`, `activity_type`, `stage`, `task_type`, `project_stage_original`, `task_group`, `assignee`, `assignee_original`, `planned_hours`, `actual_hours`, `hours_by_date`, `progress_status`, `effort_ratio`, `items_ok`, `items_nok`, `result_raw`, `comment`, `details`, `source_file`, `source_sheet` and `source_row`.

## History event types

| Event | Meaning |
|---|---|
| `created` | record created |
| `stage_transition` | stage changed (previous → new) |
| `stage_entered_at` | verified earlier entry time of the current stage |
| `updated:<field>` | a field changed |
| `activity_added`, `activity_updated:<key>.<field>`, `activity_removed_from_source` | activity changes |
| `agent_sync` | run ID, source file and changed fields of an agent sync |
| `file_uploaded` | photo or attachment added |
| `event:<type>` | agent event |

Each event carries `source` (`manual`, `agent`, `seed` or `telegram`), `actor` and `at`.
