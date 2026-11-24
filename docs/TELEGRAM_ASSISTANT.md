# Personal Telegram bot: natural-language layer (`server/telegram/nlu/`)

Branch: `claude/telegram-assistant` (off `claude/railway-deployment`, **not merged, not deployed to
Railway**). Adds a rule-based Russian natural-language layer on top of the existing menu/wizard bot
(`server/telegram/personalBot.js`) - no paid LLM API, no change to real Railway data.

## What's new

The bot already understood `/start /help /tasks /projects /new /cancel /menu`, its reply-keyboard
buttons, and a step-by-step task-creation/edit wizard - all of that is **unchanged**. On top of it,
free text that isn't a command and isn't an answer to an active wizard step is now run through a
rule-based interpreter before falling back to the old "create a task from this text?" prompt. It
answers:

- **Project questions** (read-only): brand (with Russian synonyms), stage, responsible engineer,
  "stuck on one stage too long", and follow-up refinements of the same question.
- **Task questions**: overdue, due today, due this week, by responsible person.
- **Task changes** (create / move the due date / mark done): finds the task, shows the old and new
  value, and applies nothing until the owner presses "✅ Да".

Explicit commands and an **active** step-by-step prompt (task creation, editing a field, etc.)
still go first, unconditionally - the natural-language layer is only ever tried once none of those
claimed the message. See "Compatibility" below.

## Module layout

```
server/telegram/dateUtils.js        shared date helpers (today/add-days/weekday/week-range), used
                                     by both personalBot.js's wizard and the NLU layer
server/telegram/nlu/
  normalize.js    text normalization + loose Russian word/stem comparison (no real morphology)
  brands.js       manufacturer synonym dictionary ("Джили" -> GEELY, "Хавал" -> HAVAL, ...)
  stages.js       Kanban stage recognition by Russian word root ("замере"/"замеряем" -> measurement)
  dates.js        a relative/absolute date found *anywhere* in a sentence (TZ: Asia/Yekaterinburg)
  projectsQuery.js   read-only project search (server/projectsModel.js) - brand/stage/responsible/
                      long-in-stage/free text
  tasksQuery.js       read-only task search + candidate-finding for a mutation (server/docStore.js)
  intents.js      classifies one message in isolation into {intent, ...entities} or null
  assistant.js    the only thing personalBot.js calls - merges intents.js's result with short-term
                  context (last question/list), runs the query, decides what needs confirmation
  llmHook.js      inert extension point for a future LLM classifier (see "Future LLM" below)
```

`assistant.js` never writes to the database. A task is only ever created/changed by
`personalBot.js` itself, through the exact same `patchTask()`/`tasks.create()` it already used for
the wizard - after, and only after, the owner presses "✅ Да". Project data is never written by
this feature at all (see "Limitations").

## Recognising a Russian sentence

No ML, no morphological analyser - three small, explicit tools, layered:

1. **Brand synonyms** (`brands.js`): a fixed list (`GEELY`/Джили, `HAVAL`/Хавал, `BAW`/Бав, `CHERY`,
   `LADA`, ... - see the file for the full list). A manufacturer *not* in the list still matches,
   just by literal substring instead of a synonym.
2. **Stage word roots** (`stages.js`): each of the 5 Kanban stages is matched by the Russian root
   that survives every grammatical form ("замере"/"замеряем"/"замер" all contain "замер";
   "разрабатываем"/"разработка" both map to `development` via two roots, since Russian verb and
   noun forms of that word don't actually share one substring).
3. **Loose word/stem comparison** (`normalize.js`): strips one trailing Russian case ending so
   "Калиткина" (genitive, "у Калиткина") matches a stored "Калиткин" - a fixed suffix list, not a
   real declension engine.

`dates.js` finds a date *anywhere* in a sentence ("перенеси задачу TIREI **на пятницу**"), not just
a whole-message answer the way the wizard's own `parseDue()` does - weekday names, сегодня/
завтра/послезавтра, `DD.MM[.YYYY]`, `YYYY-MM-DD`, all resolved against `TELEGRAM_BOT_TIMEZONE`
(default `Asia/Yekaterinburg`), never the server's own local time.

## Context ("what did we just talk about")

A follow-up only merges with the previous project question when the new sentence actually reads
like a follow-up - starts with "а ", or contains "из них"/"них"/"эти"/"этих"/"тоже"
(`assistant.js`'s `looksLikeContinuation`). Otherwise a new brand mention always starts a fresh,
independent filter - "Покажи все проекты GEELY" right after an "HAVAL на замере" question does
*not* inherit the old stage filter, because nothing in it signals a refinement.

```
Какие проекты HAVAL в работе?        -> 2 projects (two different product types)
А какие из них на замере?            -> stage=measurement merged with the remembered brand=HAVAL
Покажи второй                        -> opens item #2 of the *list that was just shown*
```

The remembered state (`dialog.nlu_context`: entity, filters, the shown list's ids) lives 10 minutes
and is stored in the same per-chat `telegram_dialogs` row the wizard already uses - nothing new to
back up or migrate. An ordinal ("первый".."пятый") or a bare number against a stale/absent context
is simply not recognised as a pick (falls through like any other unrecognised text), and a number
past the end of the list is reported as out of range, never silently answered with the wrong item.

## Changing a task (the only mutation path)

Every change - create, move the due date, mark done - goes through the same three steps:

1. **Find** the task (`tasksQuery.findCandidates`, a loose substring match over title/description/
   responsible/direction).
   - **0 matches** -> "не нашёл такую задачу", nothing else happens.
   - **>1 matches** -> a numbered list, waiting for a number or enough of the title to be
     unambiguous - *this* is the confirmation dialog's new state (`nlu_pick`), not yet the change
     itself.
   - **1 match** -> straight to confirmation.
2. **Show old vs. new** and ask "✅ Да"/"✖️ Нет" (`nlu_confirm` state) - for a new task, the
   extracted title and due date (or "не указан", never invented); for a move, the task's current
   due date and the one just parsed; for "mark done", just the task's title.
3. **Apply** only on "✅ Да", via `personalBot.js`'s own `patchTask()`/`tasks.create()` - the same
   version-conflict handling and idempotent-create-by-id the wizard already relies on (see
   "Idempotency" below). "✖️ Нет" or `/cancel` discards the proposal; nothing was ever written.

Engineering projects are **never** changed this way, by design (see "Limitations") - a sentence
that reads as a project change ("перенеси проект HAVAL на этап оформления") is recognised just well
enough to explain that chat can't do that, instead of silently doing nothing or guessing a task
instead.

## Idempotency

A task created through the natural-language layer gets the id `tg-nlu-<chat>-<id of the
*triggering* message>` - deterministic from the message that said "создай задачу...", the same way
the wizard already keys its own created task off the id of its *confirming* message. If Telegram
redelivers that original message under a new `update_id` (a real occasional webhook behaviour, and
the reason `telegram/log.js`'s per-`update_id` dedup alone isn't enough here), the bot proposes the
same task again, but confirming it a second time resolves to the same id and is treated as the
existing duplicate-create case already in `patchTask()`'s caller - no second task. Covered by
`test_duplicate_update_does_not_duplicate_an_nlu_created_task`.

## Compatibility with the scripted engine

`personalBot.js`'s dispatcher tries, in this order, on every incoming message: (1) global commands/
menu buttons, (2) feature commands (`/tasks`, `/projects`, `/new`, task codes `/t_*`/`/p_*`),
(3) **the active step-by-step dialog's own expected input** (title/description/due/priority/
responsible while creating a task; a field value while editing; a status pick; etc.) - every one of
these either answers the message or returns, so **the natural-language layer is only ever reached
once none of them claimed it**. Concretely: while `/new`'s wizard is waiting for a title, typing
"Какие проекты GEELY в работе?" becomes the task's title verbatim, not a project query - covered by
`test_nlu_never_hijacks_the_create_task_wizard`.

## Future LLM (not used today)

`nlu/llmHook.js` is a documented no-op - `classify()` always returns `null`, synchronously, no
network call, no API key read. `assistant.js` calls it exactly once, only after the rule-based
`intents.js` has already failed to recognise the message. Wiring in a real LLM later means: make
`llmHook.classify()` call it, map its answer onto the exact same `{intent, ...}` shape
`intents.js` produces, and gate it behind its own new env var - never reusing
`TELEGRAM_PERSONAL_AI_ENABLED`/`TELEGRAM_ENGINEERING_AI_ENABLED` (the existing paid-AI switches),
so it can be reviewed and enabled independently. Everything downstream of classification - search,
context, the confirmation dialog, the idempotent apply, the owner-only/role checks - needs no
change either way; an LLM here only ever gets to *propose* an intent, exactly like the rule-based
matcher does today.

## Limitations

- **Not a morphological analyser.** Brand synonyms, stage roots, and the case-ending stripper are
  fixed, small lookup tables tuned to the examples in the ТЗ and this codebase's own data (car
  manufacturer names, `server/seed.js`'s demo set). A manufacturer or phrasing outside those lists
  still works through plain substring matching, just without synonym expansion.
- **Engineering projects: view only.** Confirmed in requirement §4 of the originating ТЗ - a
  project's stage/assignee/etc. can only be changed on the dashboard board, never through this
  chat, even with confirmation. A sentence that reads as a project change is recognised and
  explained, never attempted.
- **"Long on one stage" threshold is fixed** at `TELEGRAM_LONG_STAGE_DAYS` (default 14 days),
  env-overridable, not user-adjustable per question ("покажи проекты дольше месяца" is not
  recognised as "30 days" - it's recognised as the one fixed threshold, or not at all).
  "показать проекты которые дольше 30 дней" and other spelled out numeric
  thresholds are not parsed.
- **Context is single-slot.** One remembered question (project *or* task, not both at once) per
  chat, 10 minutes. Asking about tasks and then "а что из них" about an earlier *project* question
  does not reach back past the task question in between.
- **Free-text project search is deliberately narrow.** Only brand/stage/responsible/long-in-stage
  are extracted as *intents*; a sentence that names neither but still mentions "проект" falls back
  to a literal substring search across name/manufacturer/model/product/article (so an unmatched
  made-up word correctly reports "ничего не найдено" instead of showing everything) - but there is
  no dedicated "search by article number" or "search by model" *intent* the way brand/stage ones
  exist. The existing `/projects <text>` command and the step-by-step "🔎 Найти проект" prompt
  remain the precise way to search by article/model.
- **No multi-field change in one sentence.** "Перенеси задачу TIREI на пятницу и назначь на
  Иванова" only acts on the date; a second field in the same sentence is not extracted.
- **Rate limiting / lockout**: unaffected and unrelated - this feature adds no new unauthenticated
  surface (it only runs after `server/telegram/access.js`'s existing owner-chat check).

## Example dialogues

```
> Какие проекты Джили сейчас в работе?
Проекты: GEELY (2):
1. GEELY Monjaro · Сканирование · до —  /p_...
2. GEELY Coolray · Замер · до 15.11.2026  /p_...

> А какие из них на замере?
Проекты: GEELY, замер (1):
1. GEELY Coolray · Замер · до 15.11.2026  /p_...

> Покажи второй
(out of range for a 1-item list - reports "Не понял" rather than guessing)

> Что сейчас на оформлении?
Проекты: оформление (2):
1. CHANGAN CS35 · Оформление · до 10.11.2026  /p_...
2. TOYOTA Camry · Оформление · до 07.11.2026  /p_...

> Какие мои задачи просрочены?
Просроченные задачи (1):
1. Старый долг · просрочено 01.01.2020  /t_...

> Создай задачу проверить крепления защиты BAW.
Создать задачу «проверить крепления защиты BAW.»?
Срок: не указан
[✅ Да] [✖️ Нет]
> ✅ Да
Готово — задача «проверить крепления защиты BAW.» создана на доске «Доска проектов».

> Перенеси задачу TIREI на пятницу.
Перенести срок задачи «Проверить TIREI крепление»?
Было: 25.11.2026
Будет: 27.11.2026
[✅ Да] [✖️ Нет]
> ✅ Да
Готово — срок изменён на 27.11.2026.

> Отметь задачу защита выполненной.
Нашёл несколько подходящих задач:
1. Защита картера GEELY
2. Защита картера HAVAL
Пришлите номер или уточните название.
> 1
Отметить задачу «Защита картера GEELY» выполненной?
[✅ Да] [✖️ Нет]
> ✅ Да
Готово — задача отмечена выполненной.

> Перенеси проект HAVAL на этап оформления
Изменение инженерных проектов через чат не поддерживается — только просмотр. Вносите изменения на доске.
```

## Test results

`npm test` (full suite, this branch): **381 passed, 1 skipped, 1 failed.** The one failure
(`test_pdf_table`) is the pre-existing sandbox `_cffi_backend`/`cryptography` native-binding issue,
confirmed present on an unmodified checkout too - unrelated to this feature. 366 passed on the
parent branch before this change; the +15 are exactly this branch's new tests:

- `tests/js/nlu_unit.js` (run by `test_nlu_unit_scenarios`) - normalization/stemming, brand and
  stage synonym recognition across every ТЗ example sentence, relative/absolute date resolution in
  Asia/Yekaterinburg, intent classification for every given project/task example (including the
  negative case: an ordinary task title with no trigger word is not claimed at all), and the
  long-in-stage search against real `days_in_stage` data.
- `tests/test_telegram_assistant.py` (14 end-to-end tests against the real server + fake Telegram/
  fake Anthropic backends, mirroring `tests/test_telegram_personal_bot.py`'s own fixtures):
  brand synonyms and stage search over the real seeded projects (including a genuinely-empty
  result, never a fabricated one); the two-turn follow-up + ordinal-pick dialogue; task queries
  (overdue/today/week/by person); create/move/complete with confirmation and old-vs-new values
  shown before anything is written; an ambiguous task match offering a numbered choice; "not
  found" explained rather than guessed; a project-change attempt refused; an unmatched
  brand-shaped query reporting "nothing found" instead of silently listing everything; the
  duplicate-update idempotency case; proof the wizard's own title step is never hijacked by the
  NLU layer; the existing commands/buttons still working; and that neither the КО group chat nor a
  stranger ever reaches this code at all. Every one of these also asserts the fake Anthropic
  backend received zero calls and every outbound message stayed inside the owner's own chat.

## Rollout / rollback

Same posture as every other feature branch here: review, then merge into `claude/railway-deployment`
when ready - nothing here needs a new environment variable (`TELEGRAM_LONG_STAGE_DAYS` is optional,
defaults to 14). No data migration, no new collection, no change to `users`/`ko_messages`/anything
the earlier manager-accounts or shturman-ko-analyst work added. Rolling back is reverting the merge
commit - `telegram_dialogs`' new `nlu_context` field is simply unused by older code, same
forward/backward-tolerant convention every other optional field in that store already follows.
