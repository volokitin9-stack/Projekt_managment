# Configuration

## Environment variables

Secrets live only in environment variables, or in a secret manager that sets them. They are never written to reports, logs or mapping files. See `.env.example`.

| Variable | Used by | Purpose |
|---|---|---|
| `PORT` | app | HTTP port (default 3000) |
| `DATA_DIR` | app | folder of the JSON data file `shturman.json`, its backups and run reports (default `./data`). Put it on a **persistent** disk |
| `UPLOADS_DIR` | app | photos and attachments. Must be persistent |
| `AGENT_API_KEY` | app | shared secret for `/api/agent/*`. The plan import is disabled until it is set |
| `SEED_DEMO_DATA` | app | `true` inserts the 14 demo projects into an EMPTY database (development only) |
| `PLAN_IMPORT_DIR` | app | uploaded planning files and the agent state (reports, approved mapping, review queue). Not served over HTTP |
| `PLAN_AGENT_PYTHON` | app | Python interpreter for `python -m plan_agent` (default `python3`) |
| `MAX_UPLOAD_BYTES`, `MAX_PLAN_UPLOAD_BYTES` | app | upload limits |
| `PM_API_URL`, `PM_AGENT_API_KEY` | agent CLI | the Штурман server and key. Set automatically for in-app imports |
| `PLAN_AGENT_STATE_DIR` | agent CLI | agent state directory (in-app: `PLAN_IMPORT_DIR/state`) |
| `PLAN_TARGET_MONTH` | app, agent CLI | `YYYY-MM` target month for monthly workbooks, or `auto` (current month in `PLAN_TIMEZONE`) |
| `PLAN_SOURCE_TYPE`, `PLAN_SOURCE_PATH`, `PLAN_SOURCE_PATTERN` | app | automatic plan retrieval source (see `docs/FILE_SOURCE.md`). Monitoring is off while the type or path is empty |
| `PLAN_CHECK_INTERVAL_MINUTES` | app | minutes between checks (default 30) |
| `PLAN_AUTO_SYNC_ENABLED` | app | `false` (default): manual approval; `true`: automatic mode with safe changes only. Switchable in the UI |
| `PLAN_TIMEZONE` | app | time zone that decides the current month (default `UTC`; set it explicitly) |
| `PLAN_SOURCE_SETTLE_SECONDS`, `PLAN_SOURCE_INITIAL_DELAY_SECONDS` | app | file must be unchanged this long before it is read (default 5); first check after start (default 15) |
| `PM_DASHBOARD_URL` | `verify-deployment` | dashboard page URL, if it differs from the API |
| `PLAN_AGENT_REQUIRE_VERIFIED_DEPLOYMENT` | agent CLI | `1` (default): a command-line sync runs only after `verify-deployment` reported READY. In-app imports set `0`, because the agent talks to its own server |

## `config/vocabulary.yaml`: how the agent reads files

This file holds **hints**, not a fixed layout. Schema detection scores both header text and cell contents against it, and the result is always shown in the proposed mapping before it is used.

* `column_roles`: header synonyms for each field (identifier, manufacturer, model, combined vehicle, product, article, engineer, priority, status, stage, activity, dates, dependencies, comments, and so on). The longest matching phrase wins.
* `stage_subheaders`: words under a stage header in multi-row headers, such as *План / Факт / Статус*.
* `workflow_stages`: the five dashboard stages, in order, with synonyms.
* `additional_stages`: recognized stages outside the five. They are kept as `extra:<code>`.
* `statuses`, `project_status_map`: activity status words and how they map to the dashboard project status.
* `priorities`, `numeric_priority`: priority words, plus the assumed reading of numeric priorities. Numeric priorities are always flagged for confirmation.
* `known_manufacturers`: used only to split a combined "Vehicle" column. An unknown first word is not guessed.

When the department uses a new term, add it here. No code change is needed.

## `config/ownership_policy.yaml`: who controls which field

| Owner | Agent behaviour |
|---|---|
| `planning_file` | Writes the field when the file has a non-empty, different value |
| `dashboard` | Fills the field only while it is empty; otherwise reports a conflict and keeps the dashboard value |
| anything else (`image_agent`, …) | Never touches the field |

* `on_manual_edit`: `report_and_keep` (default) or `overwrite_and_report`. This applies when the backend's `field_sources` shows that a planning-owned field was last changed **manually**.
* `auto_sync.project_fields`: the only project fields the **automatic** mode may change on existing projects. Other changes are withheld and sent to review (see `docs/FILE_SOURCE.md`).
* `identity_fields`: if one of these differs (after normalization) on an already-matched project, the project goes to review instead of being changed.

These rules apply regardless of configuration: an empty value never clears data, projects are never deleted, and attachments, comments and history are never sent.

## Workbook profiles: `config/profiles/`

Some planning files belong to a known family, and an explicit profile reads them instead of the generic heuristics. `monthly_work_plan.yaml` covers the department's monthly work plan: one worksheet per month, one row per task.

* **Target month:** `target_month: auto` uses the current month. Override it per run with `--month YYYY-MM` (CLI), `?month=YYYY-MM` (HTTP intake) or `PLAN_TARGET_MONTH`. **Only** the worksheet of that month is imported.
* **Sheet selection:** a sheet is a month plan when its name contains a month name (Russian or English, all grammatical forms are listed in `month_names`) **and** it has the `required_headers`.
  * A sheet matching `derived_sheet_patterns` (e.g. "Сводная Ноябрь") is reported as a derived summary.
  * **Two plan sheets for the same month** stop the run with *Verification required* until `sheet_override: {"2026-11": "<sheet>"}` is set.
  * If the file name names a different year than the target month, the run also stops for verification.
* **Columns** are found by header text, never by letter.
* **Sections and categories:** `section_markers` split the sheet into sections, and `section_categories` turns each section into a dashboard category: main section → *main_project*, ДОП РАБОТЫ → *additional_work*, БАНК ЗАДАЧ → *backlog*. Task rows end at `end_of_tasks_prefixes`. `cycle_closing_task_groups` defines engineering-cycle boundaries.
* **Accounting:** every non-empty row of the sheet is put into a ledger: a record, a header, a section marker, an empty placeholder, totals, the resource table or the dropdown lists. The dry run proves that no row is unaccounted for or counted twice, and compares actual hours with the sheet's own «Итого факт в день» totals.
* **Identity:** `id_prefixes` set the stable identifiers (`PRJ-`, `AW-`, `BL-` + a hash of the identity fields). Kits and individual products are separate projects, and ambiguous records stay in the review queue.
* **Workflow:** `activity_rules` is an ordered, first-match rule list over (*План на месяц*, *Стадия проекта*, *Группа задач*) that assigns each task an activity `type`. The five main stages define the project's current stage; `testing`, `transfer_extension` and `engineering_change` are additional types.
* **Engineers:** only assignees of `design_activity_types` become the project's responsible engineer, so scanning and fitment people stay on their activities. `unknown_engineer_values` (e.g. «Н») are kept as original values and sent to review.
* **Progress and priority:** `status_rules` give *planned* / *in progress* from hours only, and never *done*. `priority_map` translates priorities; `undefined_priority_values` (e.g. 0) are kept as original values with an empty dashboard priority.
* **Re-validation:** `strict_value_kinds` lists value kinds where a value never seen at approval sends the project to review.

The first dry run copies these rules, together with every observed value combination, into `state/mapping.proposed.yaml`. Approving it makes the approved copy the permanent configuration for later files. A new column structure (fingerprint) triggers a new validation.

## Mapping lifecycle (initial analysis mode)

1. **analyze** writes `state/mapping.proposed.yaml` and `state/reports/<run>_analysis.md`. The proposal contains:
   * for every sheet: relevance and reasons, header rows, layout (`wide` = one row per project; `long` = activity rows), section-row hierarchy;
   * for every column: `role`, `confidence`, `evidence` and `needs_confirmation`;
   * `value_mappings`: every distinct stage, status and priority value found, with its interpretation (`null` = not interpretable).
2. **Edit** the proposal if something is wrong. Valid roles are listed in `plan_agent/mapping_config.py` (`VALID_ROLES`). Stage columns use `stage:<code>:<kind>`, where kind is one of `planned_start`, `planned_end`, `actual`, `status`, `engineer`, `comment`, `dependency`, `date`, `value`. Example: change a column from `stage:measurement:date` to `stage:measurement:actual`.
3. **approve** validates the roles and writes `state/mapping.approved.yaml` with the structure fingerprint.
4. Later files with the **same fingerprint** use the approved roles and value mappings and synchronize automatically. The fingerprint is computed from header texts, roles, layout and hierarchy, not from the file name, sheet names or row count.
   * Different fingerprint (columns added, renamed or re-interpreted): dry run, new proposal, new approval.
   * New stage or status values: only the affected projects go to review. Add the values to `value_mappings` and re-approve.

## Outputs

* `state/reports/<run>.txt` / `.json`: the sync report (text for people, JSON for machines)
* `state/reports/<run>_analysis.md`: the initial or structure-change analysis
* `state/sync_history.jsonl`: one line per run with its counts and changed fields
* `state/inbox/<sha256>/`: the received file plus delivery metadata (channel, original name, sender, time)
* The backend stores per-field history (`project_history`) and the full report (`sync_runs`).
