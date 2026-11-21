# Telegram integration: two channels, one bot, no Claude Artifact dependency

This replaces the Telegram handling that previously ran as scheduled Claude routines against the
Claude Artifact board (`https://claude.ai/artifact/6NQ1HSqssn7YuBY7T1L5Vo`) with real server code
on Railway. The audit below is what that replacement is based on; everything after it is new.

## Audit: how the Claude Artifact actually worked

There is no bot *code* in the Artifact to port. The personal bot (channel 1 below) was entirely
two Claude scheduled triggers on the Claude account that created this repository:

- **`Штурман: опрос Telegram`** — weekdays, hourly 07:00–19:00 Europe/Moscow. Called Telegram's
  `getUpdates` with an offset tracked in the artifact's own database (`config/telegram
  .lastUpdateId`), classified each message (list request / reply-to-notification / new task) by a
  Claude session re-reading the same instructions every time, wrote the result into the artifact's
  `tasks` collection, and replied via `sendMessage`.
- **`Штурман: утренняя сводка`** — weekdays, 07:53 Europe/Moscow. Read `tasks`, filtered
  `done:false`, grouped by `quadrant`, sent a summary.
- The Artifact's own "Проверить Telegram" button called `window.claude.use("mcp") → fire_trigger`
  on the first routine — not a network call the button itself made.

That's all of it. There is no second channel: no construction-chat monitoring, no photo pipeline,
no AI-model call, anywhere in the Claude Artifact or its triggers — channel 2 is new, not ported.

A prior, already-documented migration (`MIGRATION_REPORT.md`, written when this repo was first
created from the Artifact) independently confirms this and explains why: *"The Telegram agent
itself — out of scope per the task spec; only the receiving side (`/api/agent/*`) was prepared."*
That receiving side — `POST /api/agent/telegram/photos`, `/api/agent/images/unassigned`, the review
queue, idempotent event recording — was already built and is reused here (see `server/
projectsModel.js`'s `matchByIdentifiers`/`attachProjectFile`, factored out of `server/routes/
agent.js` so both the original agent route and the new engineering channel share one
implementation).

| Old Artifact function | Railway before this work | This work |
|---|---|---|
| `getUpdates` polling / webhook | none | `POST /api/telegram/webhook` (`server/routes/telegramWebhook.js`) |
| NL command → task, by a Claude session re-reading a prompt | none | `server/telegram/aiClient.js` (Anthropic) + `server/telegram/personalChannel.js` |
| `tasks` collection in the artifact's own DB | `server/docStore.js` "tasks" (same engine the dashboard already uses) | reused directly — **not** a new task store |
| reply-to-notification → subtask | artifact-specific `telegram_messages` collection | not reimplemented (see "Known gaps" below) |
| morning digest | a third Claude trigger | `server/telegram/dailyDigest.js` - same schedule, same content rules, see below |
| construction-chat monitoring, photo→project matching | did not exist | `server/telegram/engineeringChannel.js`, reusing `/api/agent/telegram/photos`'s matching/attach logic |
| stage-change proposal UI | did not exist for Telegram | reuses the dashboard's *existing* review-queue "Подтвердить «стадия»" button (`public/index.html`'s `engReviewTableHtml`) — no new UI needed for this part |
| idempotency (`lastUpdateId`) | n/a | `telegram_updates` collection, keyed by `update_id` (webhook delivery, not polling, so no cursor is needed — see below) |

### Known gaps vs. the old Artifact bot (reported honestly, not silently dropped)

- **Reply-to-notification → subtask.** The old bot let you reply to its own "task created"
  message to append a subtask. This is not reimplemented: `personalChannel.js`'s `update_task`
  intent lets you say *"добавь Берникову шаг — заказать крепёж"* in a fresh message instead, which
  the AI resolves against the existing task by title. If you specifically need reply-threading
  semantics, that is additional, well-scoped work (store the bot's own `message_id` → task id,
  exactly like the old `telegram_messages` collection) — flag it and it can be added.
- **Morning digest.** Reimplemented (критическое требование №2, "Дополнение к ТЗ Telegram-интеграции
  «Штурмана»") in `server/telegram/dailyDigest.js`, following the same self-rearming-timer pattern
  as `server/planSource/scheduler.js`. Schedule and content are copied from the old Claude trigger's
  own prompt, not reinvented: weekdays only, 07:53 Europe/Moscow (`CRON_TZ=Europe/Moscow 53 7 * * 1-5`,
  **no** work-calendar/holiday awareness — the Excel scheduler has that, the old digest never did),
  sent to `TELEGRAM_OWNER_CHAT_ID` only, reading `tasks` (`done:false`, grouped by `quadrant` in the
  fixed order inbox/do/plan/delegate/drop), one line per task with whichever of
  направление/от/ответственный/срок is set, "— просрочено" for an unfinished task whose срок has
  passed. At most one send per local calendar date (`telegram_digest` object in `server/store.js`,
  independent of `plan_source`/`plan_source_runs` — ticking one scheduler never touches the other's
  state or timer, see `tests/test_telegram_digest.py::test_does_not_interfere_with_the_excel_planning_agent_schedule`).
  A restart does not resend today's digest; a run more than 30 minutes late (including a server that
  started after 07:53) is recorded as missed and not replayed, same rule as the Excel scheduler.
  Status is reported under `daily_digest` in `GET /api/telegram/status`. Never creates, updates or
  deletes a task — read-only against `tasks`. Only ever sends to `TELEGRAM_OWNER_CHAT_ID`; it never
  even references `TELEGRAM_ENGINEERING_CHAT_ID`, and the hard guard below would refuse it anyway.
- **Data migration (ТЗ Stage 4).** Not performed by this change. The Artifact's live `tasks`
  collection (34 real records at audit time) and `server/docStore.js`'s `tasks` collection
  (`server/seed.js`'s demo data) are **different data** today. Stage 4 below must copy the real
  records across, deduplicated, before cutover — this is explicitly a separate, later step, not
  silently assumed done.
- **Construction-chat `chat_id`.** Not available at the time of this work. `TELEGRAM_ENGINEERING_
  CHAT_ID` is unset by default — channel 2 stays fully disabled (not even registered as relevant)
  until you set it. Nothing about channel 1 depends on it.

## Architecture

One Telegram bot, one webhook endpoint, two independent, logically separate handlers dispatched
by `chat.id` (`server/telegram/access.js`):

```text
Telegram ──POST (secret-verified)──► /api/telegram/webhook (server/routes/telegramWebhook.js)
                                            │
                              claim update_id (idempotent) — telegram_updates
                                            │
                        resolveChannel(message.chat.id) ──────────────┐
                                            │                          │
                            chat_id == TELEGRAM_OWNER_CHAT_ID   chat_id == TELEGRAM_ENGINEERING_CHAT_ID
                                            │                          │
                         server/telegram/personalChannel.js    server/telegram/engineeringChannel.js
                                            │                          │
                       AI classify (aiClient.js, Anthropic)    AI extract (aiClient.js, Anthropic)
                                            │                          │
                 docStore("tasks") / projectsModel.moveStage    projectsModel.matchByIdentifiers →
                 (explicit command, executed directly)          recordActivity (always) +
                                            │                    review_queue "telegram_stage_
                                            │                    suggestion" (proposal only, never
                                            │                    applied) + attachProjectFile (photos)
                                            ▼                          ▼
                                  server/store.js (shturman.json) - one writer, same lock as everything else
```

Why a webhook, not polling (ТЗ §8.1 allows either): Railway gives this app a public HTTPS URL
already, a webhook reacts immediately rather than up to an hour late like the old scheduled
polling, and it needs no new always-running process — the Windows agent's poll model exists
because the *laptop* has no public endpoint, which doesn't apply here.

### The engineering chat is read-only - enforced at the outbound-call level, not per handler

Критическое требование №1 ("Дополнение к ТЗ Telegram-интеграции «Штурмана»"): the bot must never
send *anything* to `TELEGRAM_ENGINEERING_CHAT_ID` — no reply, confirmation, greeting, notification,
error, digest, or clarification request, from any code path (a main handler, an exception handler,
a background job, a retry).

Today's code already has only one outbound call site (`personalChannel.js`'s `sendMessage`, always
to the resolved *personal* chat id by construction) and `engineeringChannel.js` never calls
`sendMessage`/`sendPhoto`/`sendDocument` at all — but relying on "the code currently happens not to
call it" is not a guarantee. The guard instead lives in `server/telegram/botApi.js`'s shared `call()`
function, the one chokepoint every outbound Telegram API request goes through: any call whose
`chat_id` matches `TELEGRAM_ENGINEERING_CHAT_ID` is refused (`engineering_chat_is_read_only`) before
the HTTP request is even made, regardless of method name — covering `sendMessage`, `sendPhoto`,
`sendDocument`, and any method added later with no per-method enumeration needed. `getFile`/file
download (reading, not sending) is unaffected — the chat is read-only, not unreachable.

Tests: `tests/js/telegram_readonly_unit.js` (direct proof the guard fires for every method, string
and numeric `chat_id` alike, and leaves the owner chat/unconfigured case unaffected) and
`tests/test_telegram.py`'s `test_engineering_channel_never_sends_anything_to_telegram_across_every_scenario`
/ `test_engineering_channel_ai_failure_path_never_sends_anything_either` (the full existing
engineering-channel scenario matrix — relevant message, irrelevant message, photo queued then
attached, unauthorized chat, AI-unavailable fallback — against a real fake Telegram server, with
zero outbound requests recorded).

### Why two *logically* separate handlers, one bot

The task spec requires strict separation of business logic, not strict separation of
infrastructure. `personalChannel.js` and `engineeringChannel.js` share nothing except the two
stateless clients (`aiClient.js`, `botApi.js`) and the matching/storage primitives already used by
the dashboard and the agent API — neither can create a Kanban task from the construction chat
except through one explicit, narrow path (`action.explicit_task_assignment`, high confidence only,
ТЗ §2: "только при явно выраженном поручении"), and neither can change a confirmed project stage
without a human clicking the existing dashboard confirmation button.

### Conflict resolution: Telegram vs. Excel vs. manual edits (ТЗ §10)

No new rule was invented; this channel follows the dashboard's existing ones, which already cover
three competing writers (Excel/agent, manual dashboard edits, and now Telegram):

- **A confirmed stage (`status`) only ever changes through an explicit, recorded transition** —
  this was already true for Excel (`projectsModel.moveStage`, never inferred from planned work)
  and is now true for Telegram identically: the engineering channel only ever *proposes*
  (`review_queue` "telegram_stage_suggestion"), through the same confirmation action Excel's own
  `planned_stage` suggestions use.
- **Manual-edit protection** (`projectsModel.safeUpdate`'s `protectManual`) already stops an Excel
  sync from silently overwriting a field a person edited by hand; the Telegram engineering channel
  never calls `updateProject`/`safeUpdate` at all for project fields — it only appends to history
  (`recordActivity`) and files (`attachProjectFile`), which are append-only and can't conflict.
- **Ambiguous identity** (which project, which stage) is never guessed — both channels go to
  `review_queue` exactly like an agent-import record without `external_project_id` already does.

If a future need arises for Telegram to *write* a project field directly (not just propose a
stage), that should go through `safeUpdate` with `source: "telegram"`, which already participates
in the same manual-edit-protection and field-history rules as Excel and the dashboard — this is a
design note for that future work, not something this change needed.

### Degraded mode: the AI is unavailable (no Anthropic credit, network, bad response)

**Incident and fix.** `/start` stopped working after the Anthropic account ran out of credit -
the webhook log showed `Anthropic API returned HTTP 400: Your credit balance is too low...`. Root
cause: `/start` went through `aiClient.classify()` like every other message, even though it needs
no interpretation at all - there is nothing to extract, nothing ambiguous. With the AI down, it hit
the same generic "AI unavailable" fallback as a real natural-language command.

**What needs the AI and what doesn't (the actual audit, п.2 of this fix's ТЗ).** This channel's
whole design is natural-language-driven - free text in, a structured action out
(`personalChannel.js`'s `TOOL_SCHEMA`). Creating, closing or updating a task by description
genuinely has no deterministic equivalent: there is a title, a responsible person, a due date to
extract from whatever the user actually typed, and that extraction *is* the AI's job. The one
exception is listing: "show my active tasks" needs no extraction, only formatting already-known
data. So the non-AI command set is, and will stay, small and explicit:

- `/start`, `/help` - a short description of the bot and this same list, nothing database-related.
- `/tasks` (alias `/list`) - active tasks, formatted exactly like the AI-driven `list_tasks` intent
  (`listTasks()`, shared by both paths - one implementation, not two).

All three are recognised in `personalChannel.js`'s `handle()` *before* `aiClient.classify()` is
ever called, so they work identically whether the AI is configured, reachable, unreachable, or
out of credit - requirement 1 of this fix. If a deterministic equivalent for another command ever
becomes worth adding, the same pattern applies: only add one when there is truly nothing left to
interpret, never a heuristic guess standing in for the AI.

**Never leaking an AI failure to a Telegram user (requirement 4).** Every path that calls
`aiClient.classify()` catches its error and replies with one fixed, friendly sentence ("Не удалось
обработать сообщение прямо сейчас (ИИ временно недоступен). Сохранил его, обработаю позже.") -
never `e.message` (Anthropic's own wording, an HTTP status, a stack trace), whatever the failure
actually was (`aiClient.js`'s `error.code`: `not_configured` | `network_error` | `api_error` |
`bad_response` - all handled identically, so a missing credit, a network blip and a malformed
response read the same to the end user). The engineering channel never replies at all, so this
doesn't apply there by construction (see below). `personalChannel.js`'s other catch - around
*executing* an already-classified action, nothing to do with the AI - was tightened the same way
(a plain "не получилось выполнить команду", not the underlying error) rather than leaving a second,
narrower leak.

**Never losing a message (requirement 5).** This was already true before this fix and is
unchanged: both channels queue the message as `review_queue` kind `"telegram_pending"`
(`reason: "ИИ недоступен: ..."`) instead of dropping it, and the engineering channel still
downloads and queues any photo on the same message even when the AI call around it failed. What
this fix adds is *visibility*: `GET /api/telegram/status`'s `personal`/`engineering` blocks now
report `ai_unavailable_pending` (how many `telegram_pending` items are open for that channel) and
`ai_unavailable_last_at`, shown on "Интеграции → Telegram" as soon as either count is nonzero -
before this fix, these items existed but were invisible outside the generic engineering review
table. There is still no automatic re-processing once the AI comes back - a person resolves these
from the review queue, same as any other queued record; that is unchanged scope, not part of this
fix.

**The engineering chat stays read-only regardless (requirement 6).** None of the above changes
`engineeringChannel.js`'s behaviour of never calling `sendMessage` - it has no reply path to patch,
and `botApi.js`'s own hard guard (see above) would refuse any accidental attempt either way. Tested
explicitly for the AI-unavailable case (`tests/test_telegram.py::test_insufficient_anthropic_credit_in_the_engineering_chat_queues_and_stays_silent`),
not just assumed from the happy-path tests.

## Environment variables (see `.env.example` for the full block with defaults)

| Variable | Required for | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | both channels | from `@BotFather` |
| `TELEGRAM_WEBHOOK_SECRET` | both channels | the webhook refuses every request (501) without it |
| `TELEGRAM_OWNER_CHAT_ID`, `TELEGRAM_OWNER_USER_ID` | channel 1 | your own numeric Telegram ids; `TELEGRAM_OWNER_CHAT_ID` is also where the daily digest (`dailyDigest.js`) sends - no separate variable |
| `TELEGRAM_ENGINEERING_CHAT_ID` | channel 2 | unset = channel 2 fully disabled |
| `TELEGRAM_ENGINEERING_ALLOWED_USER_IDS` | channel 2 (optional) | comma-separated; unset = whole chat |
| `ANTHROPIC_API_KEY` | both channels | server-only; never reaches the client |
| `TELEGRAM_AI_MODEL` | optional | default `claude-haiku-5-5` |

## Finding chat and user ids

Telegram ids are numbers, not `@usernames`. Easiest way: message `@userinfobot` (or your own bot,
temporarily, before the webhook is set) and read the `id` field it returns; for a group, add the
bot to the group and check `message.chat.id` in the `/api/telegram/log` view (negative number for
a group/supergroup) once a message from that chat has been logged, or via `getUpdates` directly
(`https://api.telegram.org/bot<token>/getUpdates`) before switching to a webhook.

## Setting up the webhook

**From the dashboard (no shell access needed - Railway's own Console has neither `node` nor
`curl`):** open "Интеграции → Telegram" and click **«Подключить webhook»**. This calls
`POST /api/telegram/webhook/connect` (`server/routes/telegramAdmin.js`), which:
1. Calls Telegram's `setWebhook` with a fixed, hardcoded URL
   (`https://projektmanagment-production.up.railway.app/api/telegram/webhook` -
   `TELEGRAM_BOT_TOKEN`/`TELEGRAM_WEBHOOK_SECRET` only, no new env var, nothing from the request
   body), `allowed_updates: ["message"]`, `drop_pending_updates: false`.
2. Verifies the result with `getWebhookInfo` - a plain "ok" from `setWebhook` alone is not treated
   as success.
3. Stores the outcome (`telegram_webhook` in `server/store.js`) and shows it right there: connected
   yes/no, the registered URL, Telegram's own `pending_update_count`/`last_error_message`/
   `last_error_date`, and when it was last checked. Never the bot token or the webhook secret,
   in the UI or in `telegramLog`'s audit trail.

Gated the same way as every other dashboard mutation: session-protected (`dashboardAuth`'s global
middleware - this path is not the one exemption, `/api/telegram/webhook` itself), plus an explicit
CSRF token (`GET /api/whoami`'s `csrf_token`, sent back as `X-CSRF-Token`) since this one action
reaches outside the app. With no `DASHBOARD_USERNAME`/`PASSWORD` configured, the button works
unauthenticated, exactly like the rest of the dashboard in that mode.

**From a shell that has `curl`** (a local machine, not Railway's Console), the equivalent manual
call still works and is unaffected by any of the above:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-railway-domain>/api/telegram/webhook" \
  -d "secret_token=<same value as TELEGRAM_WEBHOOK_SECRET>"
```

Verify either way: `curl "https://api.telegram.org/bot<token>/getWebhookInfo"` should show your URL
with `pending_update_count: 0` and no `last_error_message` - the same thing the dashboard button
already shows you.

## "Интеграции → Telegram" page

`GET /api/telegram/status` (dashboard-session-protected, same as every other dashboard read) backs
the status blocks in the dashboard's "Интеграции" tab - webhook connection state (see above),
personal bot / construction chat, the daily digest - connection state, last command/message,
counts, errors — never the token/secret/API key themselves (only whether each is configured).
`GET /api/telegram/log?channel=personal|engineering&limit=N` backs the processing-log viewer.

## Stage 3 — testing before cutover (ТЗ §11)

Test against **a separate test bot** (a second `@BotFather` bot, pointed at a Railway *preview*
deployment or a locally-tunnelled instance with its own `TELEGRAM_BOT_TOKEN`/`TELEGRAM_WEBHOOK_
SECRET`) — this is the only way to validate real Telegram delivery (not just this repo's own
`tests/test_telegram.py`, which uses a fake Telegram/Anthropic backend) without ever touching the
real `@II_Volokitinbot` your Claude routines still own. Before touching the production bot, confirm
on the test bot:

- [ ] Create a Kanban task through a personal-chat message
- [ ] Edit/close/reopen a task through a follow-up message
- [ ] List active/overdue tasks
- [ ] Send a construction-chat-style message about a known seeded project (e.g. HAVAL Jolion) →
      check `agent_events`/history updated, and a `telegram_stage_suggestion` review item appears
      — **and the project's confirmed stage did not change**
- [ ] Confirm that suggestion via the dashboard's existing button → stage changes
- [ ] Send a message about an unrecognisable project → it lands in "На проверке", not silently lost
- [ ] Attach a photo (with and without caption, as part of a multi-photo album) → ends up on the
      right project, or in "Нераспределённые фото" if genuinely unmatchable
- [ ] Re-deliver the same `update_id` (Telegram does this on any failure) → no duplicate
- [ ] Message from a user/chat not on the allowlist → ignored, nothing created

## Stage 4 — data migration (do this only after Stage 3 passes)

The Claude Artifact board now has an **«Экспорт задач в JSON»** button (added directly to the
board's action row) that downloads its entire `tasks` collection as one JSON file — exact field
values, nothing invented or backfilled; see the file's own `export_meta` block for exactly which
fields this artifact's data model does and doesn't have (no `description`, `priority`, `comments`,
a `stage` distinct from `quadrant`, per-field history, or a link to an engineering-project id — the
artifact never had those, so the export doesn't fabricate them).

Import that file with `scripts/import_artifact_tasks.js`:

```bash
node scripts/import_artifact_tasks.js path/to/shturman_tasks_export.json             # dry run: pre-import analysis only
node scripts/import_artifact_tasks.js path/to/shturman_tasks_export.json --write     # import
node scripts/import_artifact_tasks.js --rollback path/to/import-manifest-*.json      # dry run: what rollback would remove
node scripts/import_artifact_tasks.js --rollback path/to/import-manifest-*.json --write  # rollback
```

Run it against the target `DATA_DIR` (e.g. `railway run`, or directly on the host with `DATA_DIR`
pointed at the mounted volume) **with the server stopped first** — same single-writer rule as every
other one-time script in `scripts/`. It writes directly into the `tasks` doc store (not over HTTP,
so no dashboard-session login is needed), skips any `id` that already exists rather than
overwriting it, and never touches any other collection. This makes it **safe to re-run**: export
again after more tasks accumulate in the Artifact and re-run the same command — only the genuinely
new records get added, nothing already-imported or created directly on the new board is touched or
duplicated. **Do this read-only against the Artifact** (the export button never writes) **and
verify record counts before and after** — do not assume Telegram will backfill missed history once
the new handler is live; it only ever sees messages sent after the webhook is set.

**Pre-import analysis (критическое требование №3).** Every run, dry or `--write`, first prints: how
many records the export file has (by quadrant, done vs. active), how many tasks already exist in
the target, and — the duplicate check — exactly which ids from the export already exist there and
would therefore be skipped, vs. which ids are genuinely new. Nothing is written until you have seen
this and chosen `--write`.

**Rollback.** Every `--write` import that creates at least one record also writes an
`import-manifest-<timestamp>.json` file (next to the export file) listing exactly the ids *that
run* created. `--rollback <manifest>` reads only that file and removes exactly those ids — never a
record imported by an earlier run, and never a record created directly on the board since (proven
in `tests/test_import_artifact_tasks.py::test_write_produces_a_rollback_manifest_and_rollback_removes_exactly_those_ids`,
which creates a task directly on the board between import and rollback and checks it survives).
Like the import itself, rollback defaults to a dry run; `--write` applies it. Re-running a rollback
against the same manifest a second time is a safe no-op (ids already gone are reported, not an
error).

**Production.** Neither this script nor this change has been run against the real Railway volume —
only against synthetic data in this test suite and locally. Nothing in production has been
modified. Running the real import against the real Railway `DATA_DIR` is Stage 4's own step, to be
done only when you give the explicit go-ahead ("не изменять production до отдельного
подтверждения"), with the server stopped and a backup taken first (Stage 5 step 1 below covers the
backup; do that before, not instead of, this step).

## Stage 5 — cutover

1. **Backup first**: `node scripts/snapshot_data.js` (existing script — same one the Railway
   migration guide uses) against the live Railway volume, or a platform-level volume snapshot.
2. Set all `TELEGRAM_*`/`ANTHROPIC_*` variables on the real Railway deployment.
3. **Disable the two Claude scheduled triggers** (`Штурман: опрос Telegram`, `Штурман: утренняя
   сводка`) — do this only now, not earlier; they are what keeps the old behaviour available as a
   fallback through Stage 3.
4. Click **«Подключить webhook»** on "Интеграции → Telegram" (or run the `curl` command above) -
   either way, confirm the status it shows is `connected: true` with no `last_error_message`.
5. Send one message on each channel from the real `@II_Volokitinbot`/chats and confirm in
   `/api/telegram/log` and the dashboard.
6. Watch `/api/telegram/status` and the server log for the first real day of traffic before
   calling this done.

## Rollback

- **Webhook misbehaving**: `curl -X POST "https://api.telegram.org/bot<token>/deleteWebhook"`
  immediately stops Railway receiving anything — Telegram then just queues/drops updates
  silently, which is safe (no data loss on the Railway side, nothing was committed).
- **Re-enable the Claude triggers** (`update_trigger` with `enabled: true`) to go back to the old
  polling behaviour — safe to do even with the webhook still set, since Telegram allows only one
  delivery mechanism at a time and a `getUpdates` call automatically fails once a webhook is
  registered; `deleteWebhook` first, then re-enable the triggers.
- No data already written by the new handler needs to be undone — it only ever appends
  (`project_history`/`agent_events`) or goes through the same review-queue/approval path as every
  other source; nothing is deleted by rollback.
- **Stage 4's task import specifically**: use `scripts/import_artifact_tasks.js --rollback
  <import-manifest.json>` (see Stage 4 above) — this is the one part of this change that does write
  new records outside the append-only/review-queue pattern, so it is the one part with its own
  dedicated, manifest-scoped rollback.

## Checklist before disabling the Claude Artifact (ТЗ Stage 6 / §12.12)

- [ ] All of Stage 3's tests pass on the test bot
- [ ] Stage 4's data migration completed and record counts verified
- [ ] Stage 5 cutover completed and both channels confirmed live for at least one full working day
- [ ] The reply-to-subtask gap above is either accepted or has its own follow-up plan (the
      morning-digest gap is closed — see "Known gaps" above)
- [ ] A fresh backup (Stage 5 step 1) exists from *after* cutover, not just before
- [ ] Only then: stop/delete the two Claude scheduled triggers for good, and decide what (if
      anything) to do with the Claude Artifact itself — it is not deleted by anything in this change

## What was verified, and how

Everything in `tests/test_telegram.py` runs against this app's own real Node server, with a fake
Anthropic server and a fake Telegram Bot API server standing in for the real services (the same
approach already used for the Railway migration's staged-export tests) — not against the real
Telegram or Anthropic APIs, since this sandbox has no bot added to a real construction chat and no
Anthropic key. **No live Railway deployment, real Telegram bot cutover, or real AI call was made or
could be made from here.** Stage 3 above is what proves the real integration before you rely on it.
