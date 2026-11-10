# Automatic plan retrieval (file source)

The Штурман server can watch the department's planning file and process every new version with the same pipeline as a manual upload: snapshot, analysis (dry run), then approval or the restricted automatic sync.

```text
timer (PLAN_CHECK_INTERVAL_MINUTES) or «Проверить обновления»
  └─ adapter.probe()     locate the file; refuse lock files, partial or too-fresh files
  └─ adapter.fetchTo()   stable copy + SHA-256; discarded if the file changed while being read
  └─ same SHA-256 + month already processed?  → "unchanged", stop
  └─ new version: plan_imports entry + dry run (python -m plan_agent analyze)
       ├─ structure/month problem   → status "blocked" + review item, nothing written
       ├─ mapping not approved yet  → "awaiting_approval" (always manual)
       ├─ manual mode               → "awaiting_approval": preview, then «Утвердить синхронизацию»
       └─ automatic mode            → plan_agent sync --auto-safe → "synced"
```

## Configuration

All settings are environment variables of the server (`.env`). No path is built into the code. A relative `PLAN_SOURCE_PATH` is resolved against the application folder.

| Variable | Default | Meaning |
|---|---|---|
| `PLAN_SOURCE_TYPE` | (empty) | `local`. Empty means monitoring is off |
| `PLAN_SOURCE_PATH` | (empty) | the planning file, or a folder holding exactly **one** planning file (`.xlsx`, `.xlsm`, `.xls`, `.csv`). Empty means off |
| `PLAN_SOURCE_PATTERN` | (empty) | folder mode only: file-name pattern, e.g. `AfM__ISOEM_*.xlsx` |
| `PLAN_CHECK_INTERVAL_MINUTES` | `30` | time between checks |
| `PLAN_AUTO_SYNC_ENABLED` | `false` | starting value of the mode; the UI switch is stored and overrides it |
| `PLAN_TARGET_MONTH` | `auto` | `auto` = current month in `PLAN_TIMEZONE`; or a fixed `YYYY-MM` |
| `PLAN_TIMEZONE` | `UTC` | IANA time zone, e.g. `Europe/Moscow`. Set it explicitly: on 30 Nov 23:30 Moscow time, `UTC` still says November |
| `PLAN_SOURCE_SETTLE_SECONDS` | `5` | the file must be unchanged this long before it is read |
| `PLAN_SOURCE_INITIAL_DELAY_SECONDS` | `15` | first check after start-up |
| `AGENT_API_KEY` | (required) | without it, no file is processed |

Example: a folder next to the application, shared with the planners:

```bash
PLAN_SOURCE_TYPE=local
PLAN_SOURCE_PATH=../plan-inbox
PLAN_SOURCE_PATTERN=AfM__ISOEM_*.xlsx
PLAN_CHECK_INTERVAL_MINUTES=30
PLAN_AUTO_SYNC_ENABLED=false
PLAN_TARGET_MONTH=auto
PLAN_TIMEZONE=Europe/Moscow
```

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

Switch it on with «Включить автосинхронизацию», which asks for confirmation, or with `PLAN_AUTO_SYNC_ENABLED=true`. For an existing project, the automatic mode writes **only**:

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
2. Set `AGENT_API_KEY` and the variables above, then restart the server. The log shows `monitor_started`.
3. Open «Импорт плана» and click «Проверить обновления». Check the source, month and counts.
4. Approve the first version manually. Enable automatic mode only after that, if you want it.

**Automatic retrieval is operational only once a real source has been connected and checked this way on the production server.**
