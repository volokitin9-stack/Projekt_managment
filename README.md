# Штурман — Project Management Dashboard

This is the Штурман task/project dashboard, migrated out of a Claude Artifact
into a standalone Node.js web application with a persistent database and a
REST API for an external AI agent.

The dashboard itself — look, layout, the task tree, the "Глобально" view, the
"Инж. проекты" Kanban preview — is unchanged from the Artifact version. What
changed is everything underneath it: data now lives in a real SQLite
database behind an Express API instead of the Artifact's built-in storage,
and that API is ready for a separately-developed AI agent to write into.

See `MIGRATION_REPORT.md` for exactly what was preserved, what changed, and
what is implemented-and-tested versus prepared-but-not-yet-wired-up.

## Architecture

```
public/index.html   →  the dashboard (unchanged UI, same CSS/HTML/D3 tree rendering)
                        talks to the API below over fetch(), polling every 20s
                        (replacing the Artifact's real-time ArtifactData listeners)
        ↓ HTTP
server/index.js      →  Express app: static file serving + API routing
server/routes/*.js    →  one file per route group (tasks, globals, projects, agent, stages, review-queue)
server/projectsModel.js → project read/write/validation logic shared by the public and agent routes
server/docStore.js    →  generic JSON-document CRUD (used for tasks + globals)
server/storage.js     →  local-disk file storage adapter (swap this to move to S3/GCS later)
server/auth.js        →  Bearer-token auth for the agent-only routes
server/db.js           →  SQLite schema + connection (better-sqlite3)
        ↓
data/app.sqlite        →  all persistent data (gitignored — created on first run)
uploads/                →  uploaded images/attachments (gitignored — created on first run)
```

No build step: the frontend is plain static HTML/CSS/JS (exactly as it was
in the Artifact), served as-is by Express. There is nothing to bundle or
transpile.

## Installation

```bash
git clone https://github.com/volokitin9-stack/Projekt_managment.git
cd Projekt_managment
npm install
cp .env.example .env
```

Open `.env` and set at least `AGENT_API_KEY` (generate one with
`openssl rand -hex 32`). Agent endpoints return `501` until this is set —
there is no insecure default.

## Local development

```bash
npm run seed   # optional: seeds the 5 workflow stages + 14 sample projects
                # into a fresh database; `npm start` also does this
                # automatically on first boot, so this is only needed if you
                # want to reseed explicitly
npm start       # or: npm run dev   (restarts on file changes)
```

Then open `http://localhost:3000`.

## Production build

There is no separate build artifact to produce — `npm run build` is a no-op
that says so. To run in production:

```bash
npm ci --omit=dev
NODE_ENV=production npm start
```

Put a reverse proxy (nginx, Caddy, your platform's own edge) in front of it
for TLS; the app itself just listens on `PORT` (default `3000`).

## Database

SQLite via `better-sqlite3`, chosen because:

- The original app had no database at all (it used the Artifact's own
  document store, which doesn't exist outside Claude), so there was no
  existing engine to stay compatible with.
- It needs no separate server process, which keeps "clone it and run it"
  true for a one-person/small-team dashboard like this one.
- It's a single file, which makes backup trivial (`cp data/app.sqlite
  backup.sqlite`) and makes "does my data survive a restart" easy to reason
  about and to test (see `MIGRATION_REPORT.md` for the actual restart test
  performed during migration).

**Important for deployment:** `DATABASE_PATH` and `UPLOADS_DIR` must point
at a persistent volume/disk on whatever platform you deploy to. Several
popular Node hosts wipe the local filesystem on every redeploy; if
`data/app.sqlite` lives on that ephemeral filesystem, a redeploy silently
discards all projects, tasks and uploaded images. If your platform can't
give you a persistent disk, you'll need to point `DATABASE_PATH` at a
managed Postgres/MySQL instead and port `server/db.js` + `server/docStore.js`
+ `server/projectsModel.js` to it — the SQL in those three files is plain
enough that this is a mechanical change, not a rewrite.

Schema (see `server/db.js` for the authoritative `CREATE TABLE` statements):

| Table | Purpose |
|---|---|
| `docs` | generic JSON-document store for `tasks` and `globals` (the two collections the original board used) |
| `projects` | engineering projects — one JSON blob per row plus an indexed `external_project_id` |
| `stages` | the 5 workflow stages, **configurable rows, not a hardcoded list** |
| `project_files` | images/attachments, one row per file, linked to a project |
| `project_activity` | append-only change log per project (who/what/when) |
| `agent_events` | raw agent-submitted events, keyed by the agent's own `event_id` for idempotency |
| `review_queue` | agent submissions that couldn't be confidently matched to a project |

## API

All endpoints return JSON. Errors are `{"error": "...", "details": [...]}`
with an appropriate 4xx/5xx status.

### Dashboard-facing (no auth — see "Authentication" below for why)

| Method | Path | |
|---|---|---|
| GET | `/api/tasks` | list |
| POST | `/api/tasks` | create |
| PATCH | `/api/tasks/:id` | update (send back `version` from your last read; mismatched version → `409`) |
| DELETE | `/api/tasks/:id` | delete |
| GET/POST/PATCH/DELETE | `/api/globals/...` | same shape, for the "Глобально" collection |
| GET | `/api/projects` | list engineering projects (supports `?make=&model=&product=&engineer=&priority=&status=&q=`) |
| GET | `/api/projects/:id` | one project, with its `images`, `attachments`, `comments`, `activity` |
| GET | `/api/stages` | the configured workflow stages, in order |
| GET | `/api/review-queue` | items the agent couldn't confidently match (no UI yet — see Migration Report) |
| GET | `/api/health` | `{ok:true}` liveness check |

### Agent-facing (requires `Authorization: Bearer <AGENT_API_KEY>`)

| Method | Path | |
|---|---|---|
| POST | `/api/agent/projects` | create, or **upsert** if `external_project_id` matches an existing project |
| PATCH | `/api/agent/projects/:id` | update by internal id or `external_project_id` |
| POST | `/api/agent/projects/:id/images` | multipart upload; attach an image/attachment to a known project |
| POST | `/api/agent/images/unassigned` | multipart upload for a photo the agent **isn't sure** belongs to any project — goes to `review_queue` instead of guessing |
| POST | `/api/agent/projects/:id/events` | record a generic event (`event_id` makes redelivery a no-op) |

See `docs/agent-request-example.json` for a full worked example of every one
of these calls.

## Authentication

There is exactly one agent-auth scheme: a shared secret
(`Authorization: Bearer <AGENT_API_KEY>`), checked in `server/auth.js`.

The dashboard-facing GET/CRUD routes (`/api/tasks`, `/api/globals`,
`/api/projects`, …) have **no** authentication. This mirrors the original
Artifact, which was a personal, single-user tool shared "anyone with the
link," with no concept of user accounts or roles — that decision was made
explicitly during the Artifact's own development, not revisited here. If
this dashboard will ever be used by multiple people who need different
permissions, that's new work, not something this migration silently skipped
— see `MIGRATION_REPORT.md`.

**Secrets are never written to the frontend.** `public/index.html` and
`public/` as a whole contain no API key of any kind. `AGENT_API_KEY` lives
only in `.env` (gitignored) / your host's environment variable settings.

## Image/attachment upload workflow

Images and other files are **not** stored in the Git repository or inside
the database — they go to disk under `UPLOADS_DIR`, with only metadata
(filename, mime type, size, which project, who/what uploaded it, and —
for Telegram-sourced files — the originating message/chat id) recorded in
the `project_files` table. `server/storage.js` is the one module that
knows about the actual disk path; swapping to S3/GCS/etc. later means
rewriting that one file, not anything that calls into it.

Validation happens server-side (`server/storage.js#validateFile`) against
the **declared MIME type of the uploaded part**, not the filename extension:
allowed types are `image/jpeg`, `image/png`, `image/webp`, `image/gif`, and
`application/pdf` (for technical drawings/documentation), capped at
`MAX_UPLOAD_BYTES` (default 10 MB).

Duplicate-import protection: a (`telegram_chat_id`, `telegram_message_id`)
pair is unique in `project_files`. Submitting the same Telegram message's
photo twice returns the existing file record (`"duplicate": true`) instead
of creating a second row.

## Agent integration

The AI agent is developed separately (see the original task spec); this
repository only prepares the receiving side. From the agent's perspective:

1. It authenticates with `AGENT_API_KEY`.
2. For each project it wants to create or touch, it should carry a stable
   `external_project_id` of its own choosing (e.g. derived from how it
   tracks the project internally) and send that on every call. This is what
   makes `POST /api/agent/projects` an upsert instead of a duplicate-creator,
   and what makes `PATCH .../:id` work by that id as well as by our internal
   one.
3. If the agent creates a project **without** an `external_project_id`, the
   project is still created (data is never silently discarded) but flagged
   `needs_review: true` and added to `/api/review-queue`, because future
   updates for that project can't be reliably matched back to it.
4. If the agent has a photo but isn't confident which project it belongs to,
   `POST /api/agent/images/unassigned` stores it (with whatever identifying
   hints are available) for manual confirmation, rather than guessing.
5. Every project mutation is appended to `project_activity`
   (`GET /api/projects/:id` → `.activity`), so "what did the agent change
   and when" is always inspectable.
6. Every event the agent records carries its own `event_id`; redelivering
   the same `event_id` is a safe no-op.

The dashboard never talks to Telegram (or any other external source)
itself — that's entirely the agent's job, by design (see the original task
spec, section 11).

## What's implemented and tested vs. prepared only

See `MIGRATION_REPORT.md` for the detailed breakdown. Short version:
database, REST API (dashboard + agent), auth, idempotency, image upload,
and the ported dashboard UI are implemented and were exercised end-to-end
during migration. A UI for the review queue, drag-and-drop between
Kanban stages, and the Telegram agent itself are explicitly out of scope
for this migration and are not built.

## Deployment

This is a standard Node.js + SQLite app — it runs anywhere that gives you:

- A Node 18+ runtime.
- A persistent disk (for `data/app.sqlite` and `uploads/` — see "Database"
  above).
- The ability to set environment variables (`.env.example` lists all of
  them).

Any conventional VM/container host (a small VPS, Render, Railway, Fly.io,
a Docker host you already run, etc.) works; there's nothing here that
assumes a specific provider. Point your reverse proxy / platform ingress at
`PORT` (default `3000`), set `AGENT_API_KEY` and the two path variables as
real environment variables (not `.env`, which isn't deployed), and run
`npm ci --omit=dev && npm start`.
