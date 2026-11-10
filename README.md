# Штурман: engineering project management with an integrated planning agent

One application:

* **Штурман dashboard** (`public/`): the existing board with its Доска, Глобально and Инж. проекты views. The Kanban columns are Сканирование → Разработка → Тестовый образец → Замер → Оформление.
* **Штурман backend** (`server/`): Node.js/Express with **one data store**, a JSON file (`data/shturman.json`, see "Data storage"). This is the system of record for tasks, projects, activities, workload, stage history, photo/attachment metadata, the review queue and import runs.
* **Engineering planning agent** (`plan_agent/`): Python. It reads the department's Excel plans, recognizes projects, activities, engineers, priorities and hours, runs a dry run, and after approval writes **only through the Штурман agent API**. It has no database of its own.

```text
Browser ── Штурман UI (public/index.html)
             │ fetch, polling every 20 s
             ▼
        Express (server/) ─── data/shturman.json (DATA_DIR) · uploads/ (UPLOADS_DIR)
             │  /api/plan-imports: stores the file, starts the agent
             ▼
        python -m plan_agent analyze | approve | sync
             │  HTTP, Authorization: Bearer AGENT_API_KEY
             └──────────► /api/agent/* of the same server
```

Everything (application, agent, configuration templates, tests and documentation) lives in this one repository:

```text
Projekt_managment/
├── public/        # Штурман interface (index.html: Доска, Глобально, Инж. проекты)
├── server/        # backend and API (Express) + JSON storage layer (store.js) + planSource/ (file monitor)
├── plan_agent/    # Excel processing agent (Python)
├── config/        # mapping configuration: vocabulary, ownership policy, monthly-plan profile
├── data/          # local operational data (git-ignored; README and empty template committed)
├── uploads/       # local photos and attachments (git-ignored)
├── scripts/       # maintenance: demo data, import of an old SQLite database
├── tests/         # automated tests (synthetic data only)
├── docs/          # documentation
├── package.json   # Node app; requirements.txt for the agent
└── README.md
```

The notes from the original Artifact-to-app migration are kept in `MIGRATION_REPORT.md`. Its SQLite parts are historical: storage is now JSON.

## Quick start (local)

Requirements: Node 18 or newer and Python 3.10 or newer.

Install the Node dependencies:

```bash
npm install
```

Install the Python dependencies of the planning agent:

```bash
python3 -m pip install -r requirements.txt
```

Create the configuration file:

```bash
cp .env.example .env
```

In `.env`, set `AGENT_API_KEY`. Generate a value with `openssl rand -hex 32`. Without it, the agent endpoints and the plan import are disabled. Optionally set `SEED_DEMO_DATA=true` to start an empty development database with 14 demo projects.

Start the application:

```bash
npm start
```

Then open <http://localhost:3000>, go to **Инж. проекты**, and use the board.

To insert the demo projects explicitly (development databases only):

```bash
npm run seed:demo
```

## Importing an engineering plan (manual upload)

1. **Инж. проекты → Импорт плана**: choose the Excel file and the reporting month (e.g. `2026-11`), then click **Анализировать**.
   * Only the worksheet of that month is read (e.g. «Ноябрь»). Summary sheets such as «Сводная Ноябрь» are reported but not imported. If two sheets look like the same month, the run stops for verification.
   * This is a **dry run**: nothing is written. You see the counts (new, updated, unchanged, for review) and the full analysis report.
2. Check the result, then click **Синхронизировать** and confirm. This approves the mapping of exactly this analysis (bound to its structure fingerprint) and writes the data.
3. The board updates:
   * Main projects appear in the **«Этап не подтверждён»** strip with the stage the plan suggests.
   * Additional work and backlog items appear under **Доп. работы** and **Банк задач**.
   * Ambiguous records go to **На проверке**.

The same file can be imported again without creating duplicates: matched projects are updated or left unchanged. Projects missing from a newer file are never deleted. A later month (e.g. December) adds its activities to the same projects, and the November history stays.

The same steps from the command line, against a running server:

```bash
PM_API_URL=http://127.0.0.1:3000 PM_AGENT_API_KEY=<key> PLAN_AGENT_STATE_DIR=./data/plan-imports/state python3 -m plan_agent analyze plan.xlsx --month 2026-11
```

Command-line syncs to a remote dashboard also require `python3 -m plan_agent verify-deployment` to report READY (see `docs/DEPLOYMENT_VERIFICATION.md`).

## Automatic plan updates (file source)

The server can watch a planning file and process new versions itself. Set on the server (see `docs/FILE_SOURCE.md`):

```bash
PLAN_SOURCE_TYPE=local
PLAN_SOURCE_PATH=../plan-inbox          # file or folder; relative to the application folder
PLAN_CHECK_INTERVAL_MINUTES=30
PLAN_AUTO_SYNC_ENABLED=false
PLAN_TARGET_MONTH=auto
PLAN_TIMEZONE=Europe/Moscow
```

* Monitoring starts with the server when type and path are set. There is one monitor per server, and checks never overlap each other or a manual import.
* A version counts as new only when its **SHA-256** differs; size and modification time are reported. Unchanged files are skipped. Locked, still-being-written, empty or corrupt files are never imported.
* **Manual mode (default):** a new version is analysed (dry run) and waits. In **Импорт плана → Автоматическое обновление плана** use «Просмотреть изменения», then «Утвердить синхронизацию».
* **Automatic mode:** only safe changes are written (hours and other data of existing activities, plus the approved fields in `config/ownership_policy.yaml → auto_sync`). New projects, identity, engineer or suggested-stage changes, added or removed activities and conflicts with manual edits go to **На проверке**. The confirmed stage is never changed. The first import of a file structure always needs manual approval.
* Ambiguous month sheets or an unexpected structure stop the run and create a review item. Earlier months are never overwritten.

## Stages: confirmed vs. planned

* A card is placed in a Kanban column only by its **confirmed stage** (`status`). A person confirms it by dragging the card to a column, by using the "Подтвердить «…»" button, or in the project panel. An explicit agent call with `"confirmed": true` can also confirm it.
* A planning file only provides a **suggested stage** (`planned_stage`). Planned work is never treated as proof that a project is in a stage, and an unknown stage never defaults to Сканирование.
* Every confirmed move is recorded in the project history (previous stage, new stage, time, source, person or agent) and sets the stage-entry time. The card shows **«На стадии: N дней»** counted from that verified time, or **«Длительность неизвестна»** if it was never recorded (for example, projects that existed before this version).

## Project card and details

* **Card:** manufacturer, model, product, article number (if any), time in stage, priority (Критично, Высокий, Средний, Низкий or Не указан), and the responsible design engineer or «Не назначен». A photo is shown if one exists.
* **Details panel:**
  * **Информация о проекте**: identity, stage, time in stage, suggested stage, work status, source file, sheet and rows. Editing covers stage, engineer, priority (with the original plan value), article and description, plus comments.
  * **Трудозатраты**: planned, actual and remaining hours, and every activity with its type, assignee, planned and actual hours, progress, ОК/NOK and source row. Breakdowns by employee, by plan month and by day.
  * **История проекта**: creation, stage transitions, field changes, activity changes, agent syncs and files.
  * **Фотографии и файлы**: a gallery with full-size preview (description, technical comment, date, Telegram reference), attachments and upload.

Hours are effort, not completion. Nothing is marked done automatically, and manual edits survive later imports.

## Data protection rules

* See "Data storage" for atomic writes, backups, the single-process lock and versioned format upgrades.
* Demo projects are inserted **only** with `SEED_DEMO_DATA=true` (or `npm run seed:demo`), and only into an empty database. `npm run demo-data` lists them; `npm run demo-data -- --remove` removes only demo projects nobody has touched.
* Agent writes:
  * unknown fields are rejected, not dropped;
  * empty values never clear data;
  * a field edited by a person is kept and reported as a conflict;
  * the agent never sets the confirmed stage;
  * creates are duplicate-safe;
  * updates use version checks.
* Uploaded planning files are stored in `PLAN_IMPORT_DIR` (not publicly served). Do not commit planning files, databases or photos; `.gitignore` excludes them.

## Data storage

For the initial launch all operational data is kept in **one JSON file**, `data/shturman.json`. The application creates it on first start from `data/templates/shturman.empty.json`. Full reports of import runs are kept in `data/sync-runs/`, uploaded planning files in `data/plan-imports/`, and photos and attachments in `uploads/`.

* **Atomic writes:** each change is written to a temporary file, flushed to disk and renamed over the data file, so a crash never leaves a half-written file. The previous version is kept as `shturman.json.bak`.
* **All-or-nothing:** a change that fails validation part-way (for example, a project and its activities) is rolled back completely.
* **One writer:** a lock file stops a second server process from using the same `data/` folder. The planning agent never touches the files; it goes through the server's API.
* **Versioned format:** `meta.schema_version`. Future format upgrades copy the file first (`shturman.json.pre-migration-vN-*.bak`). A file from a newer application version is refused, not overwritten.
* **Backup:** copy `data/` and `uploads/` together. See `data/README.md`.
* **Earlier SQLite installations:** stop the server, then run a dry run first and add `--write` to import.

```bash
npm run import-sqlite -- path/to/app.sqlite
```

```bash
npm run import-sqlite -- path/to/app.sqlite --write
```

The import never merges into a data file that already holds projects, and it only reads the SQLite file (Node.js 22.13 or newer).

Only the folder structure, `data/README.md` and the empty template are in Git. Real data, planning files, photos, credentials (`.env`) and backups are excluded by `.gitignore`.

## Telegram photos (receiving side prepared)

`POST /api/agent/telegram/photos` (multipart) accepts a photo with identifiers and the Telegram chat and message IDs. It attaches the photo only when the project is identified reliably, in this order: external project ID, unique article number, then unique manufacturer + model + product. Otherwise the photo goes to the review queue. Redelivering the same message is a no-op. The Telegram monitoring agent itself is not part of this version.

## Tests

The tests start the real Node server with temporary databases and use synthetic data only.

Install the development dependencies:

```bash
python3 -m pip install -r requirements-dev.txt
```

Run the tests (this runs `python3 -m pytest -q`):

```bash
npm test
```

## Documentation

* `docs/API.md`: dashboard and agent endpoints
* `docs/CONFIGURATION.md`: environment variables, mapping configuration and the monthly-plan profile
* `docs/EXTERNAL_AGENT.md`: connecting an automatic file-delivery agent
* `docs/DEPLOYMENT_VERIFICATION.md`: checks before syncing to a production server
* `docs/FILE_SOURCE.md`: automatic plan retrieval, file-source configuration and troubleshooting

## Production

This version is meant to be verified locally first. For production:
* run on a host with a **persistent disk** for `DATA_DIR`, `UPLOADS_DIR` and `PLAN_IMPORT_DIR` (the defaults are inside `data/` and `uploads/`);
* set the variables in the host environment;
* put a TLS reverse proxy in front of the app.

The dashboard has no user accounts: the dashboard routes are unauthenticated by design of the original app, while the agent routes require `AGENT_API_KEY`. Add access control (e.g. at the reverse proxy) before exposing the app to a network.
