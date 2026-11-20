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
| morning digest | a third Claude trigger | not reimplemented (see "Known gaps" below) |
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
- **Morning digest.** Not reimplemented as a Railway cron in this change (the existing Excel 09:00
  scheduler pattern in `server/planSource/scheduler.js` is the template to follow). Until it is,
  keep the Claude morning-digest trigger running — it reads the Claude Artifact's own `tasks`
  collection, which Stage 4 below retires, so it would need to be either ported to read `/api/tasks`
  or disabled once you stop maintaining data in the Artifact.
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

## Environment variables (see `.env.example` for the full block with defaults)

| Variable | Required for | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | both channels | from `@BotFather` |
| `TELEGRAM_WEBHOOK_SECRET` | both channels | the webhook refuses every request (501) without it |
| `TELEGRAM_OWNER_CHAT_ID`, `TELEGRAM_OWNER_USER_ID` | channel 1 | your own numeric Telegram ids |
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

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-railway-domain>/api/telegram/webhook" \
  -d "secret_token=<same value as TELEGRAM_WEBHOOK_SECRET>"
```

Verify: `curl "https://api.telegram.org/bot<token>/getWebhookInfo"` should show your URL with
`pending_update_count: 0` and no `last_error_message`.

## "Интеграции → Telegram" page

`GET /api/telegram/status` (dashboard-session-protected, same as every other dashboard read) backs
the two status blocks (personal bot / construction chat) in the dashboard's "Интеграции" tab:
connection state, last command/message, counts, errors — never the token/secret/API key themselves
(only whether each is configured). `GET /api/telegram/log?channel=personal|engineering&limit=N`
backs the processing-log viewer.

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

Export the Claude Artifact's real `tasks` collection (`ArtifactData list`) and import each record
into `/api/tasks` (`POST` with the same `id` if you want to preserve Telegram reply-linking — the
doc store accepts a caller-supplied id), skipping anything whose `id` already exists. **Do this
once, read-only against the Artifact, and verify counts before and after** — do not assume
Telegram will backfill missed history once the new handler is live; it only ever sees messages
sent after the webhook is set.

## Stage 5 — cutover

1. **Backup first**: `node scripts/snapshot_data.js` (existing script — same one the Railway
   migration guide uses) against the live Railway volume, or a platform-level volume snapshot.
2. Set all `TELEGRAM_*`/`ANTHROPIC_*` variables on the real Railway deployment.
3. **Disable the two Claude scheduled triggers** (`Штурман: опрос Telegram`, `Штурман: утренняя
   сводка`) — do this only now, not earlier; they are what keeps the old behaviour available as a
   fallback through Stage 3.
4. Point `setWebhook` at the production domain with the real secret (same command as above).
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

## Checklist before disabling the Claude Artifact (ТЗ Stage 6 / §12.12)

- [ ] All of Stage 3's tests pass on the test bot
- [ ] Stage 4's data migration completed and record counts verified
- [ ] Stage 5 cutover completed and both channels confirmed live for at least one full working day
- [ ] The morning-digest and reply-to-subtask gaps above are either accepted or have their own
      follow-up plan
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
