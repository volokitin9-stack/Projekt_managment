# Штурман: engineering project management with an integrated planning agent

One application:

* **Штурман dashboard** (`public/`): the existing board with its Доска, Глобально and Инж. проекты views. The Kanban columns are Сканирование → Разработка → Тестовый образец → Замер → Оформление.
* **Штурман backend** (`server/`): Node.js/Express with **one SQLite database**. This is the system of record for tasks, projects, activities, history, photos, the review queue and import runs.
* **Engineering planning agent** (`plan_agent/`): Python. It reads the department's Excel plans, recognizes projects, activities, engineers, priorities and hours, runs a dry run, and after approval writes **only through the Штурман agent API**. It has no database of its own.

```text
Browser ── Штурман UI (public/index.html)
             │ fetch, polling every 20 s
             ▼
        Express (server/) ─── SQLite (DATABASE_PATH) · uploads (UPLOADS_DIR)
             │  /api/plan-imports: stores the file, starts the agent
             ▼
        python -m plan_agent analyze | approve | sync
             │  HTTP, Authorization: Bearer AGENT_API_KEY
             └──────────► /api/agent/* of the same server
```

The original standalone migration notes are in `MIGRATION_REPORT.md`.

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

* The database schema is upgraded by **versioned migrations** (`server/migrations.js`). They are additive only, and a backup copy of a non-empty database is written before upgrading: `<db>.pre-migration-vN-<time>.bak`.
* Demo projects are inserted **only** with `SEED_DEMO_DATA=true` (or `npm run seed:demo`), and only into an empty database. `npm run demo-data` lists them; `npm run demo-data -- --remove` removes only demo projects nobody has touched.
* Agent writes:
  * unknown fields are rejected, not dropped;
  * empty values never clear data;
  * a field edited by a person is kept and reported as a conflict;
  * the agent never sets the confirmed stage;
  * creates are duplicate-safe;
  * updates use version checks.
* Uploaded planning files are stored in `PLAN_IMPORT_DIR` (not publicly served). Do not commit planning files, databases or photos; `.gitignore` excludes them.

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

## Production

This version is meant to be verified locally first. For production:
* run on a host with a **persistent disk** for `DATABASE_PATH`, `UPLOADS_DIR` and `PLAN_IMPORT_DIR`;
* set the variables in the host environment;
* put a TLS reverse proxy in front of the app.

The dashboard has no user accounts: the dashboard routes are unauthenticated by design of the original app, while the agent routes require `AGENT_API_KEY`. Add access control (e.g. at the reverse proxy) before exposing the app to a network.
