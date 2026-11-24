# Individual manager accounts (role `ko_viewer`) — "Разграничение доступа к «Доске проектов»"

Branch: `claude/manager-accounts` (off `claude/railway-deployment`, **not merged, not deployed to
Railway** — this whole feature is new and awaiting your review before either happens).

Replaces the idea of one shared manager login with real per-person accounts for managers who need
to **view** "Конструкторский отдел" (engineering projects) without being able to change anything.
The existing administrator login (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`) is unchanged in every
respect — same env vars, same credential check, same session format for it — and remains the one
and only administrator identity.

## Why the administrator stays outside the new `users` collection

The ТЗ asks for two things that are easiest to satisfy by construction rather than by a runtime
check:

- *"Не допускать создания второго администратора через менеджерский интерфейс"* — the admin UI's
  create-user form has no field that can produce anything but role `ko_viewer`
  (`server/usersModel.js`'s `create()` hardcodes it); there is no way to reach "admin" through it,
  by construction, not by validation.
- *"Сохранить совместимость с текущей административной авторизацией и действующим паролем
  администратора"* — the administrator's credential, hashing (none — it's an env var compared
  with `crypto.timingSafeEqual`, as before), and session shape are completely untouched.

Keeping the administrator as the env-var identity it already was, rather than migrating it into a
`users` row, satisfies both directly instead of needing special-case code to enforce them.

*"Не допускать блокировки последнего активного администратора"* is, as a result, true by
construction too: no `users` row ever has role `admin`, so there is nothing to disable. The guard
(`usersModel.setActive()` refuses to deactivate the last active `admin`-role row) is still in the
code, defensively, in case this is ever extended to store admin accounts as rows too — covered by
`tests/js/users_last_admin_unit.js`, which has to force a row's role to `admin` by hand to even
exercise it, since no real code path produces one.

## Data model

Two new collections in `server/store.js` (`users`, `users_audit`) — same JSON file, same atomic
writes, same persistence guarantees as `projects`/`tasks`/everything else already has. No new
migration step and no new env var.

`users` row: `{id, full_name, username, password_hash, role, active, token_version, created_at,
updated_at, last_login_at, created_by, disabled_at, disabled_by}`. `role` is always `"ko_viewer"`
today. `password_hash` and `token_version` are never sent to any client — `usersModel.toApi()` is
the only shape the admin API ever returns.

`users_audit` row: `{id, at, type, actor, target_user_id, target_username, detail}` — `type` one
of `login_success | login_failed | user_created | user_updated | password_reset | user_disabled |
user_activated`. `GET /api/users/audit` (admin-only) reads it back; nothing prunes it below the
most recent 5000 entries.

## Password hashing

**scrypt**, via Node's own built-in `crypto` module (`server/passwords.js`) — not Argon2id. The ТЗ
allows either; this avoids adding the first native-binding npm dependency this codebase has ever
had, which would have complicated the Railway/Railpack build (see `claude/railway-deployment`'s
own recent history fixing exactly that build). Parameters: N=2^15, r=8, p=1, 64-byte key — OWASP's
current guidance for an interactive login, costing ~70ms per hash/verify on this machine. The
stored hash is self-describing (`scrypt1$<log2N>$<r>$<p>$<saltHex>$<hashHex>`), so the cost factor
can be raised later for new hashes without invalidating existing ones.

## Session revocation — the "мгновенная блокировка" mechanism

There is no server-side session table to purge. A manager's session cookie embeds `{uid,
tokenVersion}` (alongside the usual signed, httpOnly, `SameSite=Lax` cookie this app already had).
`token_version` starts at 1 and is incremented by `disable`, `activate`, and `reset-password`.
`server/dashboardAuth.js`'s `requireDashboardSession` — the one middleware every `/api/*` route
already goes through — re-checks `{uid, tokenVersion}` against the **live** user record on every
single request (`usersModel.isSessionLive()`). A mismatch (or the user no longer existing, or no
longer active) rejects with 401 immediately, regardless of the cookie's own un-expired signature.

This is what makes disabling, reactivating, or resetting a password take effect **on the very next
request**, from every device that session is open on — not on that cookie's natural expiry, and
with nothing to clean up. Covered directly in `tests/test_user_accounts.py` (disable mid-session,
reactivate, reset).

The administrator's own session is unaffected — it carries no `uid`, so this check never applies
to it; its only validity check is still the signature and expiry, exactly as before.

## CSRF

`GET /api/whoami`'s `csrf_token` (bound to the session cookie via HMAC, `server/
dashboardAuth.js`'s `csrfToken()`) must be echoed back as `X-CSRF-Token` on every mutating
`/api/users/*` call (`requireCsrfToken`, same mechanism already used for `POST /api/telegram/
webhook/connect`) — on top of `requireAdminRole`, which runs first at the router level. The
dashboard's own `apiFetch()` attaches this automatically once `loadWhoAmI()` has run at boot; a
direct `curl`/script call needs to fetch it from `/api/whoami` itself first.

## Rate limiting

`server/usersModel.js`'s `loginAllowed`/`recordLoginFailure`/`recordLoginSuccess` — an in-memory,
per-process map keyed by the attempted username, 5 failures within one lockout window locks that
username out for 15 minutes (`POST /api/login` then returns 429). Shared by **both** identities:
the administrator's own login attempts go through the same limiter (keyed by `DASHBOARD_USERNAME`),
checked *before* the credential comparison even runs, so a locked-out username can't be walked
past by guessing the right password mid-lockout. In-memory and per-process is a deliberate choice,
not an oversight — see `server/usersModel.js`'s own comment: Railway runs this app as a single
instance, so a redeploy resetting the counter is an accepted, minor trade-off, not a real gap.

## What a `ko_viewer` manager can and can't do

| | Admin | `ko_viewer` |
|---|---|---|
| View "Конструкторский отдел" board/cards (`GET /api/projects`, `/api/stages`) | ✅ | ✅ |
| Search/filter/sort (client-side, over the same GET) | ✅ | ✅ |
| View a project photo/document (`GET /api/projects/:id/files/:fileId/view`, inline) | ✅ | ✅ |
| Download the raw original file (`/uploads/...`) | ✅ | ❌ (403) |
| Edit/create/delete a project, confirm a stage, comment, upload a file, drag a card | ✅ | ❌ (403) |
| Personal Kanban (`/api/tasks`, `/api/globals`) | ✅ | ❌ (403) |
| Review queue, plan import/source control, Telegram settings, Excel re-check | ✅ | ❌ (403) |
| "Пользователи" (create/edit/reset/disable/activate managers) | ✅ | ❌ (403) |
| Sync-run history (`/api/sync-runs`) | ✅ | ❌ (403) |

Enforced server-side on every request (`server/index.js`'s router mounting +
`server/routes/projects.js`'s own read-only-for-non-admin middleware) — a direct URL or curl call
is refused exactly like a UI click would be; the dashboard additionally hides every control a
manager can't use, as a courtesy, not as the security boundary.

**Photos**: per the ТЗ's own note, there is no way to stop a browser's screenshot or save-as short
of not rendering the image at all — `GET .../view` only removes the direct, guessable original-file
link and any download-oriented response header (`Content-Disposition: inline`, never
`attachment`). This is a reduction of exposure, not a cryptographic guarantee, and is described
that way deliberately rather than oversold.

## Rollout (once you decide to merge/deploy this)

1. **Review this branch** (`claude/manager-accounts`) — it is not deployed and has not touched
   `claude/railway-deployment`.
2. Merge it into `claude/railway-deployment` (or wherever you deploy from) **only when you're
   ready** — nothing here needs a new environment variable; `DASHBOARD_USERNAME`/`PASSWORD`/
   `DASHBOARD_SESSION_SECRET` must already be set for any of this to be reachable at all (same
   precondition the dashboard login already has).
3. Deploy. The `users`/`users_audit` collections are created empty automatically — no migration
   step, nothing to backfill.
4. Log in as the administrator, open **"Пользователи"**, click **"Создать пользователя"** for each
   manager: ФИО + логин, leave the password field empty to have the server generate one. **Write
   the generated password down immediately** — it is shown exactly once and cannot be retrieved
   again (resetting it generates a new one, same one-time display).
5. Hand each manager their login + initial password through whatever channel you already use for
   that (this script never emails or messages it anywhere on its own).
6. If a manager leaves or should lose access: **"Отключить"** in the Users table — this is
   immediate, not "on next login", and does not delete their history/audit trail (the ТЗ asks for
   deactivation, not deletion, specifically to keep that history).

No existing data — projects, personal tasks, Telegram state, the Excel plan-source configuration,
the import-manifest rollback tooling — is touched by any of this; it is additive, gated behind the
same `DASHBOARD_USERNAME`/`PASSWORD` on/off switch the dashboard login already had.

## Rollback

Nothing here can corrupt existing data (it only ever adds to `users`/`users_audit`, and only
*reads* everything else - `projects`/`tasks`/etc. are untouched by this change's own code). If you
need to back this branch out after merging/deploying it: revert the merge commit (or redeploy the
previous commit) - the `users`/`users_audit` collections are simply unused by the older code and
can stay on disk harmlessly (same "unused collections are forward/backward tolerant" rule
`server/store.js`'s own `COLLECTIONS` list already relies on elsewhere). There is no in-place
schema change to undo.

## Test results

`tests/test_user_accounts.py` (22 tests) — creation and independence of several managers, login/
logout, no cross-session access (including a tampered cookie), instant lockout on disable,
reactivation requiring a fresh login, password reset invalidating both the old password and old
sessions, login rate limiting (including against the admin identity), every forbidden
read/write/download for `ko_viewer`, that search/filter/view still work for `ko_viewer`, persistence
across a real server restart, administrator access fully preserved, and that the Telegram webhook
route and the Windows-agent/plan_agent routes keep their own independent auth untouched.

Plus `tests/js/users_last_admin_unit.js` (the defensive last-admin guard, run by one of the above).

Full existing suite (`npm test`, every other test file — Telegram, the Excel plan-source agent,
task/task-import tooling, the 14-project demo-seed dataset, deployment verification, the Railway
build/lock fixes from earlier work): **313 passed, 1 skipped, 1 failed** on this branch. The one
failure (`test_pdf_table`) is a pre-existing, sandbox-only `_cffi_backend` issue, confirmed present
on an unmodified checkout too — unrelated to this change. (`test_simultaneous_manual_and_scheduled_
runs`, a known timing-sensitive race test, has also been observed to fail intermittently on an
unmodified checkout in earlier runs this session; it did not fail in this particular run.)

## Known limitations / explicitly not built

- **Export (Excel/CSV/JSON/PDF).** The ТЗ lists this among what `ko_viewer` must not have access
  to, but no such export feature exists anywhere in this codebase today (server or client) - there
  is nothing to restrict yet. If one is added later, gate it the same way `/uploads` is gated here
  (check `req.authRole !== "ko_viewer"`).
- **No automatic session list/"log out everywhere" UI.** Disabling a user already achieves "log
  out everywhere" instantly (see "Session revocation" above); there is no separate UI need for it.
- **Rate limiting is per-process, in-memory** (see "Rate limiting" above) - a deliberate scope
  decision for Railway's single-instance deployment, not a gap to fix later unless the deployment
  model changes.
