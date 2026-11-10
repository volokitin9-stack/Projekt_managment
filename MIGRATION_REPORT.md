> **Note (later change):** storage has since moved from SQLite to a JSON data file (`server/store.js`, `data/shturman.json`). The SQLite details below describe the original migration and are kept for history. Old SQLite databases can be imported with `npm run import-sqlite`.

# Migration Report — Штурман Artifact → Standalone App

## Source of truth

The live Claude Artifact at `https://claude.ai/artifact/6NQ1HSqssn7YuBY7T1L5Vo`
(version 27 at time of migration), read via the Artifact tool and used
verbatim as the starting point for `public/index.html`. No functionality
was redesigned, removed, or replaced with a static mockup.

## Files created

```
package.json, .env.example, .gitignore
README.md, MIGRATION_REPORT.md
docs/agent-request-example.json
server/index.js
server/db.js
server/docStore.js
server/projectsModel.js
server/storage.js
server/auth.js
server/seed.js
server/routes/docCollection.js
server/routes/projects.js
server/routes/stages.js
server/routes/agent.js
server/routes/reviewQueue.js
```

## Files modified

Only one: `public/index.html` — copied byte-for-byte from the Artifact, then
edited **only** in the places that called Claude Artifact–specific APIs.
Every edit is isolated and listed below; nothing else in the ~2,300-line
file was touched (CSS, markup, D3 tree rendering, modals, the Kanban board
layout, filters, the detail panel — all untouched).

1. **State variables**: removed `db` (ArtifactData namespace), `usingLocalFallback`,
   `localIdCounter` — replaced with a single `serverOnline` boolean.
2. **`addDoc` / `updateDocItem` / `deleteDocItem` / `findItem`**: same
   function signatures, same call sites throughout the rest of the file —
   internals rewritten to call the new `apiFetch()` helper against
   `/api/tasks` or `/api/globals` instead of `db.collection(...)`.
3. **New `apiFetch()` helper**: thin `fetch()` wrapper (JSON in/out, error
   surfacing) — the only new piece of client-side infrastructure added.
4. **Engineering-projects sample data**: the hardcoded `ENG_SAMPLE_PROJECTS`
   array (Phase-1 preview data) became `engProjects`, a variable populated by
   a new `loadEngProjects()` that fetches `/api/projects`. The exact same 14
   sample records are preserved — they're now the server's seed data
   (`server/seed.js`) instead of a client-side literal, so the preview you
   already approved looks identical on first run. Also added `loadStages()`,
   which lets the server's `stages` table override the client-side defaults
   if present (both start out equal, so this changes nothing visually).
5. **Server-wiring block** (bottom of the script): replaced the
   `window.claude.use("db")` / `onSnapshot` block with `loadAll()` (fetch +
   20s polling) — the "reliable refresh/polling mechanism" the migration
   spec explicitly allows in place of WebSockets.
6. **"Проверить Telegram" button**: this called `window.claude.use("mcp")` →
   `fire_trigger` on a Claude Code Remote routine that doesn't exist outside
   Claude — there is no standalone equivalent of "ask Claude to go poll
   Telegram right now," because that whole mechanism (an agentic Claude
   session acting as the bot) is exactly what's being replaced by the
   external agent. Repurposed the same button, in the same spot, relabeled
   "Проверить обновления," to force an immediate re-poll of the server. The
   integrations panel's "Telegram @bot" / "Раз в час" copy was updated to
   "AI-агент / через API" and "Каждые 20 сек" so the UI doesn't claim a
   connection the app no longer makes directly (per spec §11: the dashboard
   must not talk to Telegram itself).

JS syntax was checked (`node --check`) after every edit.

## Technologies used

- **Backend**: Node.js, Express 4, `better-sqlite3`, `multer` 2.x,
  `uuid` 11.x, `dotenv`, `cors`. No framework beyond Express — deliberately,
  to match the original app's own "no build step, no framework" footprint.
- **Database**: SQLite (file-based), chosen because the original app had no
  database to stay compatible with (see README's "Database" section for the
  full reasoning and the migration path to Postgres/MySQL if ever needed).
- **Frontend**: unchanged — vanilla JS, D3.js v7 (CDN), hand-rolled CSS. No
  bundler was introduced.

## Features preserved (verified, not assumed)

- Доска (task tree, quadrants, subtasks, detail panel) — code path untouched,
  now reads/writes through `/api/tasks`.
- Глобально (initiatives, subtasks, Gantt-style bars) — untouched, now reads/
  writes through `/api/globals`.
- Инж. проекты (Kanban preview: 5 stages, cards, KPIs, filters, detail
  panel) — untouched, now reads real data through `/api/projects` instead of
  a hardcoded array.
- All modals (new task, new global initiative), the FAB, zoom/pan controls,
  the clock, overdue styling, responsive breakpoints — all untouched markup/
  CSS/JS, no visual or behavioral change.

## New integration components

- REST API for dashboard reads/writes (`/api/tasks`, `/api/globals`,
  `/api/projects`, `/api/stages`, `/api/review-queue`).
- Authenticated REST API for the external agent (`/api/agent/*`), with:
  - Idempotent project upsert keyed by `external_project_id`.
  - Idempotent image import keyed by (`telegram_chat_id`, `telegram_message_id`).
  - Idempotent event recording keyed by `event_id`.
  - Field validation (required fields on create; `priority` and `status`
    checked against controlled vocabularies).
  - A review queue for agent submissions that can't be confidently matched
    to a project (missing `external_project_id` on create, or an
    explicitly-unsure image upload).
  - Per-project append-only activity log.
- Local-disk file storage behind a small adapter (`server/storage.js`) that
  isolates the one place that would need to change to move to S3/GCS later.
- Configurable workflow stages (a `stages` table, seeded with the same 5
  stages, rather than a value hardcoded into the schema).

## Tests performed

All run against a live local instance (`npm install && npm start`), not
just read from the code:

- `node --check` on the extracted frontend `<script>` after every edit.
- `GET /api/health`, `/api/stages`, `/api/projects` (seeded count = 14,
  shape includes both canonical and frontend-alias fields).
- `GET /api/projects/:id` returns `images`, `attachments`, `comments`,
  `activity`.
- Full `tasks` CRUD cycle: create → patch with correct `version` (succeeds,
  version increments) → patch with the now-stale `version` (**409**, no
  silent overwrite) → delete (**204**) → get (**404**).
- Agent auth: no header → **401**; wrong key → **401**.
- Agent create: missing required fields → **400** with field-level
  `details`; invalid `status` value → **400** naming the valid set.
- Agent create with `external_project_id` → **201**, `matched:"created"`.
- Re-POST of the **same** `external_project_id` with a changed field →
  **200**, `matched:"existing"`, project count unchanged (no duplicate),
  and the change appears in `project_activity`.
- Agent create **without** `external_project_id` → still created (data not
  discarded) but `needs_review:true` and visible in `GET /api/review-queue`.
- Image upload (multipart, real PNG bytes) with Telegram ids → **201**,
  file appears in the project's `images[]`, `is_primary` applied.
- Re-upload of the **same** `telegram_message_id`/`telegram_chat_id` →
  **200**, `duplicate:true`, no second row created.
- Upload of a disallowed MIME type (`text/plain`) → **400**, rejected by
  declared content-type, not by filename.
- Uploaded file is actually retrievable over HTTP (`/uploads/...` → `200
  image/png`).
- Event recording: first `event_id` → **201**, appended to `comments[]`;
  redelivery of the same `event_id` → **200**, `duplicate:true`, comment
  list unchanged (length verified, not just status code).
- `GET /` serves `public/index.html` (`<title>Штурман</title>` confirmed).
- **Persistence across restart**: server process fully killed (`pkill`) and
  relaunched; `/api/projects` and `/api/tasks` returned the same counts and
  content as before the restart, including every record created by the
  tests above — this is the concrete evidence for "data persists after
  application restart," not an assumption.
- `npm audit`: 0 vulnerabilities (multer bumped 1.x→2.x and uuid 9.x→11.x
  after the initial `npm install` flagged both; re-verified clean after).

## Remaining configuration requirements

- **Set `AGENT_API_KEY`** in the deployed environment before the agent can
  be authorized — there is no working default, by design.
- **Point `DATABASE_PATH` and `UPLOADS_DIR` at persistent storage** on
  whatever host this is deployed to (see README "Database" section) — on a
  host with an ephemeral filesystem, data will not survive a redeploy
  unless this is done.
- **Choose a hosting target** — this was prepared to run anywhere with
  Node + a persistent disk, but no specific provider was configured (the
  task spec said not to assume one).
- If `CORS_ORIGINS` is ever needed (dashboard served from a different origin
  than the API), set it; it's blank by default since this app serves both
  from the same origin.

## Explicitly not built (out of scope for this migration)

- **The Telegram agent itself** — out of scope per the task spec (§11);
  only the receiving side (`/api/agent/*`) was prepared.
- **A UI for the review queue** — `GET /api/review-queue` exists and was
  tested, but there's no dashboard screen to browse/resolve it yet. Items
  are safely captured either way.
- **Drag-and-drop between Kanban stages, and a "create project" form for
  the Инж. проекты tab** — these were already flagged as a separate,
  later phase of dashboard work before this migration task started, and
  that scope line was kept as-is; this migration's job was the backend/API,
  not new dashboard UI.
- **Per-user roles/permissions on the API** — the original Artifact had no
  concept of user accounts; this migration preserves that (single shared
  secret for the agent, open reads for the dashboard) rather than inventing
  an authentication system the source application never had. If multi-user
  access control is ever needed, that's new product scope, not something
  this migration silently dropped.
