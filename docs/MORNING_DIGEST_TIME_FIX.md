# Morning Telegram digest: wrong send time, fixed to 09:00 Asia/Yekaterinburg

Branch: `claude/history-stages-digest` (off `claude/railway-deployment`, **not merged, not
deployed to Railway**).

## The report

The digest was arriving at roughly **15:53 Asia/Yekaterinburg**, not the intended morning time.

## What actually sends this digest

`server/telegram/dailyDigest.js` is the one, current, server-side implementation - it runs inside
the Railway Node process, re-arms itself after every send (same pattern as
`server/planSource/scheduler.js`), and is covered by `tests/test_telegram_digest.py`. Before this
fix, it was hardcoded to **07:53 Europe/Moscow**, copied verbatim from an older source rather than
set to a deliberately chosen Yekaterinburg time - see "Root cause" below.

Checking *all* existing schedules (ТЗ item 2: "проверить все существующие расписания, чтобы
исключить дубли") turned up three more mechanisms, none of them in this server's code at all -
Claude Code scheduled tasks on the account that owns this repository (read via
`list_triggers`/`get_trigger`, not grep - these live outside the git history entirely):

| Scheduled task (Claude Code trigger) | Cron | State | What it actually did |
|---|---|---|---|
| `Штурман: утренняя сводка` | `CRON_TZ=Europe/Moscow 53 7 * * 1-5` | **disabled**, last fired 2026-11-20 | A Claude session read the **old Claude Artifact's** `tasks` collection directly (not this server at all) and called the Telegram Bot API itself with a hardcoded token/chat_id embedded in the trigger's own prompt text. |
| `Утренний обзор канбан-доски` | `CRON_TZ=Europe/Moscow 50 7 * * *` | disabled, last fired 2026-11-09 | Same era, same old Artifact, a different summary format. |
| `Штурман: опрос Telegram` | `CRON_TZ=Europe/Moscow 0 7-19 * * 1-5` | disabled, last fired 2026-11-20 | Hourly poll of the bot's `getUpdates`, also against the old Artifact - this is what used to create tasks from incoming Telegram messages before the real webhook (`server/routes/telegramWebhook.js`) existed. |

(For comparison, the one schedule that **is** correct and must stay untouched: `Доска проектов:
ежедневный анализ КО`, `CRON_TZ=Asia/Yekaterinburg 0 16 * * *`, enabled, last fired on time - the
16:00 shturman-ko-analyst run. Nothing in this fix changes it.)

All three legacy triggers are already disabled, so none of them is currently firing - but they are
real, live, re-enablable configuration pointed at the **old Artifact and a bot token that may still
be the live one**, which is exactly the kind of duplicate the ТЗ asks to rule out. **Recommendation
(not yet acted on - deleting a scheduled task is irreversible, so this needs your separate
go-ahead): delete these three**, not just leave them disabled - re-enabling any of them by mistake
would resend the digest a second time from a source this server doesn't control, and `Штурман:
опрос Telegram` would additionally start writing tasks into the old Artifact's database again,
which this app hasn't used as its data store in a long time.

## Root cause of the wrong time

Two compounding problems, both now fixed in `server/telegram/dailyDigest.js`:

1. **Wrong time zone.** The schedule was `Europe/Moscow`, not `Asia/Yekaterinburg`. Moscow is
   UTC+3, Yekaterinburg is UTC+5 - a fixed 2-hour gap (neither zone observes daylight saving).
   `server/planSource/workCalendar.js`'s `zonedInstant()` (the shared, iterative TZ-to-UTC
   conversion both this digest and the Excel scheduler use) was audited as part of this fix and
   confirmed correct by direct calculation: resolving "07:53 Europe/Moscow" through it correctly
   yields 04:53 UTC = **09:53 Yekaterinburg**, not 15:53 - so the conversion function itself was
   never the bug. The *configured* zone/time was simply wrong for this project from the start -
   the original implementation copied the old Claude trigger's cron verbatim (Moscow, because
   whoever tested the old trigger was working from Moscow) instead of the Yekaterinburg time the
   business actually needed, exactly the drift this ТЗ item is now closing.
2. **A second, larger offset on top of that**, consistent with "double offset" (ТЗ's own
   suspicion, item 2: "отсутствие двойного применения смещения"): 15:53 Yekaterinburg is 8 hours
   after 07:53 - and 8 hours is exactly Moscow's +3 and Yekaterinburg's +5 offsets added
   *together* rather than one being subtracted from the other, the classic shape of this bug class
   (converting local→UTC by *adding* the zone offset instead of subtracting it, then *also*
   adding the display zone's offset on top). This matches a 07:53 instant that was never actually
   converted away from being treated as UTC before a +5h Yekaterinburg display offset was applied,
   plus Moscow's own +3h being applied again somewhere in the old, non-server delivery path (most
   plausibly the disabled `Штурман: утренняя сводка` trigger above, which is a Claude session
   following written instructions rather than code running `zonedInstant()` - it had no shared,
   tested conversion function to rely on at all). This cannot be fully re-run against the actual
   historical delivery now that the legacy trigger is disabled and this sandbox has no access to
   Railway's own logs; it is reported as the best-supported explanation from the evidence above,
   not as something re-reproduced byte-for-byte.

**The fix**: `TIMEZONE`/`SCHEDULE_TIME` in `server/telegram/dailyDigest.js` are now `Asia/
Yekaterinburg`/`09:00` - the exact zone and time this ТЗ item asks for, converted through the same
already-audited `zonedInstant()` the correct, unaffected Excel scheduler already uses at its own
09:00 Asia/Yekaterinburg. `tests/test_telegram_digest.py` (10 tests) now ticks the digest at this
new time throughout, including a specific check that it does **not** bleed into the Excel
scheduler's own run even when both now nominally fire at the same 09:00 Yekaterinburg moment
(`test_does_not_interfere_with_the_excel_planning_agent_schedule`) - independence from the 16:00
КО-chat analysis holds trivially, since that is a different process entirely (a Claude scheduled
task, not this server) that this file never references or touches.

## КО-changes section (ТЗ item 4)

Added alongside this fix, in a new, separately-testable module, `server/telegram/
engineeringDigest.js` - see its own header comment for the detection rules. In short: it reads
*only* `project_history` for the 24 hours ending when the digest is built (never a project's
current state - a change is reported because something was recorded happening, not because of
what a project merely looks like now), groups by project, and reports: stage change, new project,
work closed/completed, responsible changed, deadline changed, a significant plan/comment change
(a new comment, the description, or the work status), and a confirmed shturman-ko-analyst proposal
(detected from `server/ko/proposals.js#apply()`'s own confirmation note, which always starts "По
предложению агента КО" - the one reliable signal a proposal was actually applied by a person,
not merely suggested). A project with no history row in the window is simply not mentioned - no
changes at all across every project is reported explicitly ("изменений нет"), never silently
omitted. An unprocessed Telegram message or an un-applied proposal never reaches
`project_history`, so it can never be counted here - by construction, not a special case.

Each line links to the project's card via `DASHBOARD_PUBLIC_URL` (new, optional env var; `?eng=
<project_id>`, handled by a one-time deep link in `public/index.html` that opens that card for
whichever role is logged in - admin or `ko_viewer` manager, since reading a КО project card is
already open to both). Left unset, the digest still names every changed project, just without a
link - never a guessed or fabricated URL.

This section rides on the digest's existing once-per-day idempotency (`telegram_digest.
last_sent_date`) - it is part of the same single message, so "do not resend the same summary
twice" already covers it with no new state.

## What was verified, and how

- `zonedInstant()`'s correctness: traced by hand against both `Europe/Moscow` and `Asia/
  Yekaterinburg`, and already exercised by the Excel scheduler's own long-standing test suite
  (`tests/test_schedule.py`), which this fix does not touch.
- The disabled legacy triggers: read directly via `list_triggers`/`get_trigger` (not grepped from
  this repository, since they live entirely outside it) - their cron expressions, `enabled` state
  and `last_fired_at` are exactly what's quoted above.
- The new schedule and the КО-changes section: `tests/test_telegram_digest.py` (10 tests, schedule/
  idempotency/weekend/grace-window/Excel-independence, all re-pointed at 09:00 Asia/Yekaterinburg)
  and `tests/test_engineering_digest.py` (the new section's own detection rules, against synthetic
  `project_history` data only).
- **Not verified, and cannot be from this sandbox**: the actual live Railway deployment's real
  send time, since (per `docs/RAILWAY_DEPLOYMENT.md`) no live Railway deployment exists in this
  environment. Confirm the first real 09:00 Yekaterinburg send once this is deployed.
