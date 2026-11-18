# Railway + the always-on Windows laptop

Штурман runs centrally on Railway. The corporate Excel workbook stays on the always-on Windows
laptop and is never exposed to the internet: `plan_agent/windows_agent.py` polls Railway over
outbound HTTPS only, runs the *unchanged* `plan_agent` CLI locally, and submits its `--json`
report back. The laptop opens no inbound port and is never a server.

```text
┌─────────────────────────────┐        outbound HTTPS only        ┌──────────────────────────────┐
│ Windows laptop (corporate    │  poll / claim / heartbeat /       │ Railway: Node/Express server  │
│ network, always-on)          │  submit result  ─────────────►   │ (this repo, server/)          │
│                               │                                  │                               │
│ corporate\Planning\plan.xlsx  │  plan_agent's own HTTP client,    │ /api/agent/*  (AGENT_API_KEY) │
│   (SMB share, never public)  │  unchanged, writes project data   │ /api/agent-jobs/*             │
│                               │  ───────────────────────────►    │   (WINDOWS_AGENT_API_KEY)     │
│ plan_agent/windows_agent.py   │                                  │ shturman.json + uploads/       │
│   (this script)               │                                  │   on a Railway volume (/data) │
└───────────────────────────────┘                                  └──────────────────────────────┘
                                                                       ▲
                                                                       │ browser (dashboard session,
                                                                       │ DASHBOARD_USERNAME/PASSWORD)
                                                                  authorized devices
```

Nothing about the existing Kanban board, workflow automation, JSON storage, stage history, review
queue, or the 09:00 Yekaterinburg working-day schedule changes. `PLAN_SOURCE_TYPE=remote_agent` is
a new, additive mode alongside the existing `local` one (`docs/FILE_SOURCE.md`); the local,
single-machine deployment (`docs/WINDOWS.md`) keeps working exactly as before if you don't set it.

**No live Railway deployment exists as a result of this work.** Everything below was written and
tested against a local server standing in for Railway (this sandbox has no Railway account or
API token). Section "What was verified, and how" at the bottom says exactly what that does and
doesn't prove, and what to confirm yourself once this is deployed for real.

## Preserving the «Обновить» button

The button's route (`POST /api/plan-source/update`) and its internal entry point
(`monitor.runRecorded()`) are unchanged. What's new is entirely inside `runRecorded()`: when
`PLAN_SOURCE_TYPE=remote_agent`, it enqueues a job instead of reading a local file
(`monitor.enqueueRemoteCheck()`); otherwise it calls `check()` exactly as before. The 09:00
scheduler (`server/planSource/scheduler.js`) calls `runRecorded()` the same way it always has and
needed no changes at all - it has no idea which mode it's running in.

## API changes

| Endpoint | Change |
|---|---|
| `POST /api/plan-source/update`, the 09:00 scheduler | Unchanged route/trigger. In `remote_agent` mode, enqueues a job and returns `{ui: {state: "busy", job_id}}` instead of reading a file synchronously. |
| `POST /api/plan-source/approve` | Unchanged route. In `remote_agent` mode, queues a "sync" job for the laptop (carrying the mapping fingerprint) instead of running `sync` here; returns `200` with `result.status: "queued"`. |
| `GET /api/plan-source/status` | Adds a `remote_agent` field: `{enabled, agent: {online, last_seen_at}, active_job, recent_jobs}` when in `remote_agent` mode; `null` otherwise. Also now self-heals a lock left by a laptop that went offline (`reconcileStuckRemoteLock()`). |
| `GET /api/agent-jobs/agent/next` (new) | Windows agent polls for its next job. Claims the oldest queued job; always counts as a presence heartbeat. |
| `POST /api/agent-jobs/agent/:id/heartbeat` (new) | Extends a claimed job's lease while a long analyze/sync is running. |
| `POST /api/agent-jobs/agent/:id/result` (new) | Agent submits its `--json` report (`{type: "analyze"\|"sync", ...}`). Applied through the exact same `analyzeImport`/`approveAndSync`/`syncVersion` logic a local run uses. |
| `POST /api/agent-jobs/agent/:id/fail` (new) | Agent reports it could not complete the job this attempt. |
| `POST /api/login`, `/api/logout`, `GET /api/whoami` (new) | Dashboard session login. No-op (501/pass-through) unless `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` are set. |
| `/api/agent/*` (plan_agent's own data-write API) | **Unchanged.** Still `AGENT_API_KEY`. plan_agent's HTTP client just now talks to it over the internet (via `PM_API_URL`) instead of `127.0.0.1`. |

No endpoint a human or plan_agent already used was removed or renamed.

## Security

| Requirement | How it's met |
|---|---|
| All project data requires authentication | `server/dashboardAuth.js`: a signed, httpOnly session cookie gates every `/api/*` route except `/api/agent*` and `/api/health` once `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` are set. **Set these on Railway** - without them the dashboard is open, exactly like the current local install. |
| Separate agent credential | `WINDOWS_AGENT_API_KEY` (job queue) is a distinct secret from `AGENT_API_KEY` (plan_agent's data writes, unchanged). Rotate either independently; a leak of one does not expose the other. |
| Agent endpoint auth, authorization, replay resistance, rate limits | `server/auth.js`'s `requireWindowsAgentAuth`: Bearer `WINDOWS_AGENT_API_KEY` + `X-Agent-Timestamp` (must be within `AGENT_REPLAY_WINDOW_MS`, default 5 min) + `X-Agent-Nonce` (≥16 chars, rejected on reuse) + a fixed-window rate limit (`AGENT_RATE_LIMIT_MAX` per `AGENT_RATE_LIMIT_WINDOW_MS`, default 120/min). |
| No credentials in frontend JS | Dashboard session is a cookie set by `/api/login`, never embedded in any served page. Agent keys are never sent to the browser. |
| HTTPS everywhere | Railway terminates TLS for you; the laptop only ever makes outbound HTTPS calls (`PM_API_URL`/`WINDOWS_AGENT_URL` must be `https://`). |
| Workbook stays inside the corporate network | Railway never reads it. Only `plan_agent/windows_agent.py`, running on the laptop, opens the file - over the local SMB share, never over the internet. |
| Never commit secrets/workbook/employee data | `.env` stays git-ignored (already the case); `windows/windows_agent.env.example` is a template with no real values; `data/`, `uploads/` are already git-ignored. |

## Persistent storage (the Railway volume)

Storage genuinely did not need new code: `DATA_DIR` and `UPLOADS_DIR` are already
env-configurable, writes to `shturman.json` are already atomic (temp file + fsync + rename, with
a `.bak` kept - `server/store.js`), and a lock file already prevents two instances from writing at
once. All that's needed is pointing them at a persistent Railway volume instead of the container's
ephemeral disk, which is wiped on every redeploy.

1. Railway dashboard → your service → **Settings → Volumes → New Volume**.
2. Mount path: `/data`.
3. Set these three variables (see the full table below) so everything durable lives under that
   one volume:
   ```
   DATA_DIR=/data
   UPLOADS_DIR=/data/uploads
   PLAN_IMPORT_DIR=/data/plan-imports
   ```
4. Redeploy. The health check (`GET /api/health`) must pass before Railway marks the deploy
   healthy - already wired up in `railway.json`.

**A redeploy never overwrites existing data**: `server/store.js` only ever creates
`shturman.json` if it does not already exist (`seed()`); an existing file on the volume is loaded,
not replaced. Back it up anyway before any risky change - see `scripts/snapshot_data.js` above the
migration section's own backup step.

## Migration (moving existing data onto Railway)

Because storage is already one JSON file plus plain upload files, migrating is copying two
folders - not a format conversion, not a database import. The scripts below exist to make that
copy safe and verifiable, not because the move itself is complex.

1. **Backup first**, from wherever the data currently lives:
   ```bash
   node scripts/snapshot_data.js <current_DATA_DIR> <current_UPLOADS_DIR> ./migration-backups
   ```
2. **Validate the process with synthetic data first.** `SEED_DEMO_DATA=true` on a scratch
   `DATA_DIR`/`UPLOADS_DIR` gives you real projects, history and a photo to copy, then verify:
   ```bash
   node scripts/snapshot_data.js /tmp/synthetic-data /tmp/synthetic-uploads /tmp/synthetic-backup
   node scripts/verify_migration.js /tmp/synthetic-data /tmp/synthetic-uploads \
     /tmp/synthetic-backup/<timestamp>/data /tmp/synthetic-backup/<timestamp>/uploads
   ```
   (This exact pair of commands was run as part of this work - see "What was verified" below.)
3. **Copy the real data** onto the Railway volume. How depends on where it is now:
   - Already on a machine Railway's CLI can reach: `railway volume` doesn't accept arbitrary file
     uploads directly - use `railway run` to get a shell with the volume mounted, or Railway's
     SFTP/`railway ssh` (see Railway's own docs for the current mechanism - this changes faster
     than this file does), and copy `shturman.json` + `uploads/` + `plan-imports/` across.
   - Simplest path for a first cutover: stop the old server, `tar`/zip `DATA_DIR` and
     `UPLOADS_DIR`, transfer the archive, extract it onto the volume at `/data`.
4. **Verify before cutting over traffic:**
   ```bash
   node scripts/verify_migration.js <old_data> <old_uploads> <volume_data> <volume_uploads>
   ```
   Must print `RESULT: OK` (or only `[info]` lines about in-flight job/agent-presence state, which
   legitimately differ once the new side has started running) before anyone is pointed at the new
   deployment.
5. **Never run two instances against the same data directory** - `store.js`'s lock file (`.shturman.lock`)
   actively refuses a second writer, so this fails loudly rather than corrupting anything, but
   plan accordingly: stop the old process before the new one starts writing the same files.

What's preserved automatically, because it's just fields in the one JSON file being copied
byte-for-byte: project IDs, stage + stage source (manual/agent_explicit/agent_rule/inferred),
manual stage overrides, full `project_history`, effective priority, workload/`project_activities`,
review queue items, plan import/version history, and photo/attachment references (the files
themselves move with `uploads/`). Comments live in `project_history`/`docs` the same way.

## Windows agent: setup and startup

Full detail in `windows/README.md` and `windows/windows_agent.env.example`; summary:

1. Create a dedicated, least-privilege Windows account with just enough rights to read the
   workbook's share.
2. On the laptop: `pip install -r requirements.txt` (ideally into this repo's own `.venv`).
3. Copy `windows/windows_agent.env.example` to a file OUTSIDE the repo, fill in `PM_API_URL`
   (your Railway URL), `PM_AGENT_API_KEY` (same value as Railway's `AGENT_API_KEY`),
   `WINDOWS_AGENT_API_KEY` (a NEW secret, matching Railway's `WINDOWS_AGENT_API_KEY`), and
   `WINDOWS_AGENT_WORKBOOK_PATH` (or `_FOLDER`/`_PATTERN`).
4. As Administrator, from `windows/`:
   ```powershell
   .\install_task_scheduler.ps1 -EnvFile <your env file> -ServiceAccount ".\svc-shturman-agent"
   ```
   (or `install_nssm_service.ps1` if you'd rather have a Windows Service - see `windows/README.md`
   for when to pick which). Either way: starts at boot, runs with no one logged in, restarts on
   crash, no console window, starts immediately without a reboot.
5. Confirm: the log at `<PLAN_AGENT_STATE_DIR>\logs\windows_agent.log` shows it polling, and
   `/api/plan-source/status`'s `remote_agent.agent.online` becomes `true` within
   `WINDOWS_AGENT_POLL_SECONDS` (default 20s) plus a few seconds.

## Verifying the Windows agent (and the whole chain)

1. `remote_agent.agent.online` is `true` on `/api/plan-source/status` - the laptop is polling.
2. Click «Обновить». Status moves to `awaiting_agent` (job queued), then `awaiting_approval` or
   `up_to_date` within roughly `WINDOWS_AGENT_SETTLE_SECONDS` + however long the workbook takes to
   analyze - watch the agent's log for `running \`plan_agent analyze\``.
3. If `awaiting_approval`: click «Утвердить синхронизацию» in «Импорт плана». Status should reach
   `up_to_date` and the projects/history/workload appear on the board, matching the local
   deployment's behavior exactly.
4. Take the laptop offline (stop the service/task, or disconnect it). `remote_agent.agent.online`
   must become `false` after `stale_after_ms` (default 3 min, `AGENT_HEARTBEAT_STALE_MS`) with no
   server-side error or crash. Reconnect it and confirm it picks back up on its own.
5. Kill the agent process mid-job (`taskkill /F`) and restart it. The in-flight job's lease expires
   (`AGENT_JOB_LEASE_MS`, default 5 min) and the lock frees itself (`reconcileStuckRemoteLock`,
   checked on every `/api/plan-source/status` call) - a fresh «Обновить» click works again without
   manual cleanup.

## Environment variables

### Railway (the server)

In addition to the existing table in `docs/CONFIGURATION.md`:

| Variable | Purpose |
|---|---|
| `PLAN_SOURCE_TYPE=remote_agent` | Switches the board's «Обновить» button and the 09:00 scheduler onto the job queue instead of a local file. No `PLAN_SOURCE_PATH` is needed or used in this mode. |
| `WINDOWS_AGENT_API_KEY` | Required to enable `/api/agent-jobs/*` (501 until set). A distinct secret from `AGENT_API_KEY` - generate with `openssl rand -hex 32`. |
| `AGENT_JOB_LEASE_MS` | How long a claimed job may run before being presumed lost (default 300000 = 5 min). Raise it if workbooks are large and `analyze`/`sync` genuinely take longer. |
| `AGENT_HEARTBEAT_STALE_MS` | How long without any agent request before the dashboard shows it offline (default 180000 = 3 min). |
| `AGENT_REPLAY_WINDOW_MS` | Replay-protection window for `X-Agent-Timestamp` (default 300000 = 5 min). Keep the laptop's clock roughly in sync (NTP). |
| `AGENT_RATE_LIMIT_MAX`, `AGENT_RATE_LIMIT_WINDOW_MS` | Job-queue request cap (default 120 requests / 60000 ms). |
| `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD` | **Set both to require login.** Unset = open dashboard, same as the local install today. |
| `DASHBOARD_SESSION_SECRET` | Set this explicitly in any multi-instance/production deployment - without it, sessions are signed with a key derived from the password alone and every session is invalidated on every restart. |
| `DASHBOARD_SESSION_MS` | Session length (default 43200000 = 12h). |
| `FORCE_SECURE_COOKIE` | Set `1` to mark the session cookie `Secure` even if `NODE_ENV` isn't `production` (Railway sets `NODE_ENV=production` by default, which already does this). |
| `DATA_DIR`, `UPLOADS_DIR`, `PLAN_IMPORT_DIR` | Point all three at the Railway volume: `/data`, `/data/uploads`, `/data/plan-imports`. |

### Windows laptop (the agent)

See `windows/windows_agent.env.example` for the full, commented list. The required ones:
`PM_API_URL` (Railway URL), `PM_AGENT_API_KEY` (= Railway's `AGENT_API_KEY`),
`WINDOWS_AGENT_API_KEY` (= Railway's `WINDOWS_AGENT_API_KEY`), and `WINDOWS_AGENT_WORKBOOK_PATH`
(or `WINDOWS_AGENT_WORKBOOK_FOLDER` + `WINDOWS_AGENT_WORKBOOK_PATTERN`).

## Exact Railway UI setup steps

1. **New project** → **Deploy from GitHub repo** → select this repository, branch
   `claude/railway-deployment` (or wherever this is merged to) once you've reviewed it.
2. Railway detects `railway.json`/`nixpacks.toml` automatically - no manual build/start command
   entry needed. Confirm under **Settings → Deploy** that the start command shows `npm start` and
   the healthcheck path shows `/api/health`.
3. **Settings → Networking** → generate a public domain (or attach your own).
4. **Settings → Volumes → New Volume**, mount path `/data`.
5. **Variables** tab: add every variable from the two tables above that applies to Railway (not
   the Windows-agent-only ones). At minimum: `AGENT_API_KEY`, `WINDOWS_AGENT_API_KEY`,
   `PLAN_SOURCE_TYPE=remote_agent`, `DATA_DIR=/data`, `UPLOADS_DIR=/data/uploads`,
   `PLAN_IMPORT_DIR=/data/plan-imports`, `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`,
   `DASHBOARD_SESSION_SECRET`, `PLAN_TARGET_MONTH` (or leave `auto`), `PLAN_AUTO_SYNC_ENABLED`
   (`true`/`false` per your workflow).
6. **Deploy.** Watch the build log; it should show only the Node install (no Python step - see
   `nixpacks.toml`'s comment on why).
7. Once healthy, open the generated domain, log in with `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`,
   and confirm the board loads (empty, until the first sync or migration).
8. Set up the Windows agent (above) pointed at this same domain, then run through "Verifying the
   Windows agent" above before relying on this for real work.
9. If migrating existing data, do it now (previous section) - before anyone starts using the new
   deployment for real, not after.

## Backup checklist

- [ ] Before any migration or risky change: `node scripts/snapshot_data.js <DATA_DIR> <UPLOADS_DIR> <somewhere persistent>`.
- [ ] After any migration: `node scripts/verify_migration.js` reports `RESULT: OK` (or only `[info]` lines) before cutting over traffic.
- [ ] Railway volumes are not automatically backed up outside the platform's own retention - periodically run `snapshot_data.js` against the live volume (e.g. via `railway run`) and store the result somewhere else (not on the same volume).
- [ ] `shturman.json.bak` (written automatically on every save) and any `shturman.json.pre-migration-v*.bak` are already a second line of defense on the volume itself - don't delete them casually.

## What was verified, and how

This was built and tested in a Linux sandbox with **no Railway account, no Railway CLI/API
access, and no Windows machine** - so "verified" below always means "against a local stand-in,"
never against the real services. Do not treat anything here as a live deployment.

**Verified in this sandbox (automated, repeatable):**
- `npm test`: 224 passed, 1 skipped, 1 pre-existing sandbox-only failure unrelated to this work
  (pdfminer/`_cffi_backend`, confirmed present on the commit before this work too).
- `tests/test_remote_agent.py` (16 tests): job lifecycle and state transitions, auth
  (missing/wrong key, missing/stale timestamp, short/reused nonce, rate limiting), the manual
  approve flow, the auto-sync chained-job flow, idempotent unchanged-file acknowledgment,
  concurrent-«Обновить»-click prevention (no duplicate job), lease expiry and recovery, heartbeat
  extending a lease, dashboard session auth (configured and unconfigured), and that the job
  carries the server-resolved month.
- `tests/test_windows_agent.py` (2 tests): the REAL `plan_agent/windows_agent.py`, as a real
  subprocess, against a real (local) Штурман server and a synthetic Excel workbook - the full
  analyze → awaiting_approval → approve → plan_agent approve+sync → real project data written
  through `/api/agent/*` chain, plus a clean shutdown on SIGTERM.
- `scripts/snapshot_data.js` + `scripts/verify_migration.js`: run end-to-end against synthetic
  data, including a deliberately tampered field and a deleted upload file, both caught and
  reported precisely.
- The server starts, binds `PORT`, and `/api/health` responds, with `DATA_DIR`/`UPLOADS_DIR`
  pointed at a throwaway local directory standing in for a Railway volume - proving the
  config/health-check/persistence wiring, not a real Railway environment.
- Node syntax-checked (`node --check`) on every modified/new server file.

**NOT verified here - confirm yourself before relying on this:**
- That Railway's real builder actually produces a healthy deploy from `railway.json`/`nixpacks.toml`,
  that a real Railway volume behaves as documented, and that the generated domain is reachable
  from the browser.
- That `install_task_scheduler.ps1` / `install_nssm_service.ps1` actually register a working
  task/service on a real Windows machine - they were written and reviewed, not executed (no
  Windows machine in this sandbox). `windows/README.md` lists exactly what to check.
- That the dedicated Windows service account can actually reach the real corporate SMB share with
  the permissions you grant it.
- Real-world timing: how long `plan_agent analyze`/`sync` actually take against the real,
  presumably much larger, production workbook, and whether `AGENT_JOB_LEASE_MS` needs raising.
- Anything about the real workbook's actual data, counts, or structure - every test here used
  synthetic fixtures (`tests/fixtures/make_fixtures.py`), per this project's standing rule to
  never use production data in tests.
