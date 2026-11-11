# Automatic plan retrieval (file source)

Штурман checks the department's planning workbook **once per official Russian working day at 09:00 Yekaterinburg time**. The «Обновить» button on the Kanban board runs the same check immediately, on any day. Both run the same pipeline as a manual upload: snapshot, analysis (dry run), then the restricted automatic sync or approval.

```text
09:00 Asia/Yekaterinburg on a working day (config/work_calendar/RU.json)
or «Обновить» on the board / «Проверить обновления» in «Импорт плана»
  └─ adapter.probe()     locate the file; refuse lock files, partial or too-fresh files
  └─ adapter.fetchTo()   stable copy + SHA-256; discarded if the file changed while being read
  └─ same SHA-256 + month already processed?  → "unchanged", stop
  └─ new version: plan_imports entry + dry run (python -m plan_agent analyze)
       ├─ structure/month problem   → status "blocked" + review item, nothing written
       ├─ mapping not approved yet  → "awaiting_approval" (always manual)
       ├─ manual mode               → "awaiting_approval": preview, then «Утвердить синхронизацию»
       └─ automatic mode            → plan_agent sync --auto-safe → "synced"
```

## Schedule

| Rule | Behaviour |
|---|---|
| Time | `PLAN_SCHEDULE_TIME` (09:00) in `PLAN_TIMEZONE` (`Asia/Yekaterinburg`, UTC+5), i.e. 04:00 UTC |
| Days | working days of the production calendar `PLAN_WORK_CALENDAR` (`RU`): Monday–Friday, minus the listed holidays and transferred days off, plus any weekend designated as a working day |
| Once per date | the scheduled run of a date is recorded in `data/shturman.json` (`plan_source_runs`) **before** it starts; a restart never runs the same date again (an interrupted run is marked `interrupted`) |
| Missed runs | not replayed: if the server starts after 09:00, or the timer fires more than `PLAN_SCHEDULE_GRACE_MINUTES` (30) late, the date is recorded as `missed` |
| Failures | retried only at the next scheduled run or with «Обновить» |
| Overlap | scheduled and manual runs share one job lock; the second is recorded as `busy` and does nothing |
| Calendar missing | a year that is not in the calendar file, is `verified: false`, or whose computed number of working days differs from `expected_working_days` stops the automatic run (`calendar_unavailable`, shown on the board). Nothing is guessed |

### Production calendar (`config/work_calendar/RU.json`)

One entry per year, added only after the government resolution for that year is published:

```json
"2026": {
  "verified": true,
  "legal_basis": ["ТК РФ ст. 112", "Постановление Правительства РФ от 24.09.2025 № 1466"],
  "expected_working_days": 247,
  "non_working_dates": [{"date": "2026-01-09", "reason": "Перенос с субботы 3 января"}, ...],
  "working_weekend_dates": []
}
```

**2026** (five-day week, 247 working days). Non-working days on weekdays:

* 1, 2 and 5–8 January (New Year holidays and Christmas);
* 9 January and 31 December (moved from Saturday 3 and Sunday 4 January by Resolution No. 1466);
* 23 February;
* 9 March (8 March falls on a Sunday);
* 1 May;
* 11 May (9 May falls on a Saturday);
* 12 June;
* 4 November.

There are no working Saturdays in 2026.

**2027** is not in the file yet. From 1 January 2027 the automatic run stops with "календарь 2027 не утверждён" until the 2027 resolution is entered: add the year, set `expected_working_days` and `verified: true`, then restart the server. The «Обновить» button keeps working regardless of the calendar.

## Execution history

Every scheduled or manual execution is a record in `plan_source_runs` (last 1000), shown in the status API:

* `trigger`: `schedule` or `manual`; `scheduled_date` / `scheduled_for` for scheduled runs.
* `phases.check`: `ok`, `failed` or `not_run` (file located, stable copy, SHA-256).
* `phases.analysis`: `ok`, `awaiting_approval`, `blocked`, `failed` or `not_run` (dry run).
* `phases.sync`: `ok`, `failed` or `not_run` (data written).
* `outcome`: one of `unchanged`, `synced`, `synced_review_required`, `review_required`, `error`, `busy`, `missed`, `skipped_non_working`, `calendar_unavailable` or `interrupted`.

A run counts as a successful synchronisation only when `phases.sync` is `ok`; an analysis alone never does. An unchanged file is recorded as a successful check (`unchanged`), and no import runs.

## «Обновить» on the board

Located in the toolbar of **Инж. проекты** after the filters. It:

* checks the file immediately, also on weekends and holidays;
* applies only the changes allowed by the automatic rules and sends the rest to «На проверке»;
* refreshes the board.

| Button | Meaning |
|---|---|
| Обновить | ready |
| Обновление... | running (repeated clicks are disabled) |
| Обновлено | changes applied |
| Изменений нет | same SHA-256 as the last processed version |
| Требуется проверка | changes that need a person: mapping approval, ambiguous file, held changes |
| Ошибка обновления | file or processing error (the message says which) |

Next to it: **Последняя проверка**, **Последняя синхронизация данных** and **Следующее обновление**, in Yekaterinburg time. The button never confirms a stage, never approves a new file structure and never creates ambiguous projects. With `PLAN_AUTO_SYNC_ENABLED=false` it only analyses the file and reports «Требуется проверка».

## Configuration

All settings are environment variables of the server (`.env`). No path is built into the code. A relative `PLAN_SOURCE_PATH` is resolved against the application folder.

| Variable | Default | Meaning |
|---|---|---|
| `PLAN_SOURCE_TYPE` | (empty) | `local`. Empty means no checks |
| `PLAN_SOURCE_PATH` | (empty) | the workbook, or a folder holding exactly one planning file. Without an extension, `.xlsx`, `.xlsm`, `.xls` and `.csv` are tried, and exactly one must exist. A `\\server\share\...` path works only when the server runs on Windows |
| `PLAN_SOURCE_PATTERN` | (empty) | folder mode only: file-name pattern |
| `PLAN_AUTO_MONITOR_ENABLED` | `true` | master switch of the scheduled check |
| `PLAN_SCHEDULE_ENABLED` | `true` | daily schedule on/off (the button works either way) |
| `PLAN_SCHEDULE_TIME` | `09:00` | local time of the run |
| `PLAN_TIMEZONE` | `Asia/Yekaterinburg` | IANA time zone of the schedule, the displayed times and `PLAN_TARGET_MONTH=auto` |
| `PLAN_WORK_CALENDAR` | `RU` | `config/work_calendar/<ID>.json` (or a path to a calendar file) |
| `PLAN_SCHEDULE_GRACE_MINUTES` | `30` | a run later than this is recorded as missed, not executed |
| `PLAN_AUTO_SYNC_ENABLED` | `false` (`.env.example`: `true`) | apply permitted changes automatically; the UI switch overrides it |
| `PLAN_REQUIRE_APPROVAL` | `true` | changes outside the automatic rules need approval. `false` is not supported and is shown as a configuration warning |
| `PLAN_TARGET_MONTH` | `auto` | `auto` = current month in `PLAN_TIMEZONE`, or a fixed `YYYY-MM` |
| `PLAN_SOURCE_SETTLE_SECONDS` | `5` | the file must be unchanged this long before it is read |
| `AGENT_API_KEY` | (required) | without it, no file is processed |

`PLAN_CHECK_INTERVAL_MINUTES` and `PLAN_SOURCE_INITIAL_DELAY_SECONDS` from the previous version are no longer used: there is no interval polling.

### The corporate workbook

```text
\\s017\Отделы\Конструкторский\1. Планы и отчеты\AfM СIS+OEM 2026
```

* Run Штурман on a machine **inside the corporate network**, under an account that can read this folder. The development cloud environment cannot reach `s017`.
* **Windows host:** put the path as is into `.env`, unquoted or in single quotes:
  ```text
  PLAN_SOURCE_PATH=\\s017\Отделы\Конструкторский\1. Планы и отчеты\AfM СIS+OEM 2026
  ```
  The extension is found automatically. If both `.xlsx` and `.xlsm` exist, the run stops; then write the full name with its extension.
* **Linux host:** mount the share, for example `mount -t cifs "//s017/Отделы" /mnt/s017 -o credentials=/etc/s017.cred,ro,iocharset=utf8`. Then set `PLAN_SOURCE_PATH=/mnt/s017/Конструкторский/1. Планы и отчеты/AfM СIS+OEM 2026`. A `\\s017\...` path on Linux is rejected with a clear message.
* The name contains a **Cyrillic «С»** in «СIS». Copy the name from Explorer rather than typing it.
* **Verify before relying on the schedule:** start the server, click «Обновить», and check that the status shows the real file name with its extension and a SHA-256 value. `Ошибка обновления: не найдено` or `нет доступа` means the path or the permissions are wrong. The file is only read, never written, locked or moved.

### Network shares and other sources

* **Network share (SMB/NFS):** mount it on the server (for example `/mnt/plans`, or a mapped drive on Windows) and point `PLAN_SOURCE_PATH` at the mount with `PLAN_SOURCE_TYPE=local`. An unreachable share is reported as `unavailable` and retried on the next check.
* **HTTP(S), SharePoint/OneDrive, S3, Google Drive:** not implemented in this version. `PLAN_SOURCE_TYPE=https` and similar values are recognised and reported as "adapter not implemented". A new adapter implements `describe()`, `probe()` and `fetchTo(dest, probe)` (see `server/planSource/adapters/index.js`), maps its failures to the error codes below, and is registered with `registerSource(type, factory)`. Its credentials must come from environment variables.
* Files can also still be delivered by another agent (`docs/EXTERNAL_AGENT.md`) or uploaded manually.

## Modes

### Manual approval (default)

1. A new version is detected and analysed (dry run). Nothing is written.
2. **Инж. проекты → Импорт плана → Автоматическое обновление плана** shows «Есть изменения — ожидает утверждения».
3. «Просмотреть изменения» lists every project that would be created, updated or held for review, with the changed fields, activity changes and conflicts with manual edits.
4. «Утвердить синхронизацию» writes the version (the same behaviour as «Синхронизировать» for a manual upload).

### Automatic

Switch it on with «Включить автосинхронизацию», which asks for confirmation, or with `PLAN_AUTO_SYNC_ENABLED=true` (the recommended setting for the daily schedule). For an existing project, the automatic mode writes **only**:

* the data of activities that already exist for the plan month: planned and actual hours, hours by day, progress, ОК/NOK and so on;
* the project fields listed in `config/ownership_policy.yaml → auto_sync.project_fields` (priority, work status, dates, source references, …).

Everything else is **not written** and goes to **На проверке** (kind "Автосинхронизация: требует утверждения"), with the old and new values:

* new projects;
* added or removed activities, including a new month's first activities;
* identity changes, engineer changes and suggested-stage changes;
* records with ambiguities;
* conflicts with manual edits (the manual value is always kept).

The confirmed Kanban stage and the stage timer are never changed. The first import of a file structure (no approved mapping, or a changed structure) always waits for manual approval, even in automatic mode. Independent projects are processed even when others are held.

## Version tracking

Each processed version is stored in `data/shturman.json` (`plan_source_versions`) with its SHA-256, size, modification time, month, import, counts and status: `analyzing`, `awaiting_approval`, `blocked`, `failed`, `syncing`, `synced`, `sync_failed` or `interrupted`.

* **SHA-256 decides.** A file with a new modification time but the same content is "unchanged". The same content for a different month is a new version.
* A version becomes `synced` only after the agent reported a completed sync; a sync with errors stays `sync_failed` and is retried.
* A restart during processing leaves the version `interrupted`, and it is processed again. Syncs are duplicate-safe, so a repeated run creates nothing twice.
* The snapshot used for the analysis is kept in `PLAN_IMPORT_DIR/files/`, and the reports in `data/sync-runs/` and `PLAN_IMPORT_DIR/state/reports/`.
* Events (checks, new versions, errors) are kept in `plan_source_events` (last 300) and printed to the server log as `[plan-source] ...`.

## Errors and what happens

| Code | Situation | Result |
|---|---|---|
| `not_found` | file or folder missing | status "Ошибка", retried on the next check |
| `unavailable` | share offline, I/O error | "повтор позже", retried |
| `permission_denied` | no read access | error + review item |
| `locked` | `~$file.xlsx` present (open in Excel), or the file is busy | "повтор позже", the file is not read |
| `unstable` | modified less than `PLAN_SOURCE_SETTLE_SECONDS` ago, or changed during the copy (incomplete transfer) | the copy is discarded, retried |
| `empty` | 0 bytes | error, nothing imported |
| `corrupt` | not a valid Excel file | error + review item, nothing imported |
| `ambiguous` | several planning files in the folder | error + review item (keep one file or set `PLAN_SOURCE_PATTERN`) |
| analysis failed | unexpected structure, unreadable workbook | version `failed` + review item |
| blocked | two sheets for the month, no sheet for the month, year mismatch | version `blocked` + review item, nothing written |

A failed check never changes project data. The store writes atomically and keeps `shturman.json.bak`.

## Operational checklist

1. Put `DATA_DIR`, `UPLOADS_DIR` and `PLAN_IMPORT_DIR` on a persistent disk and back up `data/` and `uploads/`.
2. Set `AGENT_API_KEY` and the variables above, then restart the server. The log shows `scheduler_started` with the next run date.
3. Click «Обновить» on the board (or «Проверить обновления» in «Импорт плана»). Check the file name, month and counts.
4. Approve the first version manually. Enable automatic mode only after that, if you want it.

**Automatic retrieval is operational only once a real source has been connected and checked this way on the production server.**
