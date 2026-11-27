# История предложений агента КО + явное создание проекта

Branch: `claude/ko-proposal-lifecycle`, from `claude/railway-deployment` (already includes the full
light-redesign branch, merged as PR #10). Fixes a reported incident (TAISHAN, HGH) where accepting
a КО-agent proposal made it vanish from "Предложения агента КО" with no visible result and no way
to review what actually happened.

## 1. Диагностика (код, без доступа к боевой базе)

This session has no access to the live Railway database, its API, or its logs - every finding below
comes from reading `server/ko/proposals.js`/`server/ko/rules.js`, not from the production incident
data itself. The live TAISHAN/HGH records were never inspected or recreated here.

Root cause, confirmed in code:

1. **`GET /api/ko/proposals` only ever asked for `status=open`** (`server/routes/koDashboard.js`,
   `public/index.html`'s `loadEngProjects()`) - the instant a proposal resolved (applied, rejected,
   or acknowledged), the next poll's `status=open` fetch dropped it from the frontend's `engKo`
   array entirely. The data was never deleted (`server/reviewQueueModel.js`'s `review_queue` always
   kept it, with a `decision` object recording who resolved it, when, and with what result) - only
   the UI ever discarded it, which is exactly why history was "unavailable" while the record itself
   was intact the whole time.
2. **`apply()`'s real behaviour per `kind` was never surfaced to the person who clicked.** Of the 9
   proposal kinds (`server/ko/rules.js`'s `KINDS`), **none creates a new project** - every kind
   either changes one field on an *already-resolved* project, or (`project_match`/
   `needs_clarification`) is a pure acknowledgement with `result = { acknowledged: true }` and zero
   side effect, by design (`server/ko/proposals.js`, the final `else` branch of `apply()`). A
   proposal about a vehicle not yet in the system (TAISHAN/HGH's most likely shape, given no
   existing project can match a brand-new manufacturer/model) resolves to no project at all
   (`projectsModel.matchByIdentifiers` finds nothing), gets classified by the agent as
   `needs_clarification`, and "Принять к сведению" (the frontend already labelled this correctly,
   `koProjectLabel`/the old `applyLabel` logic) closes it having created or changed nothing. This
   was always the intended behaviour for this kind - the actual defect was that **there was no other
   action available** for a person who, having been shown the proposal, decides a new project should
   in fact be created from it.

Both are confirmed, reproducible defects in the UI/workflow, not in `apply()`'s per-kind logic
itself (which already did the right thing for every kind it supports) - see
`tests/test_ko_integration.py::test_needs_clarification_apply_still_creates_nothing_the_taishan_symptom`,
which reproduces the exact reported symptom end to end against a real server and confirms
`apply()` was never the bug.

## 2. История предложений (`public/index.html`)

No new storage - `GET /api/ko/proposals` already supported `?status=all` server-side
(`server/routes/koDashboard.js`/`server/ko/proposals.js#list`); the frontend was simply never
asking for it. `loadEngProjects()` now fetches `status=all` and keeps every record (`engKo`); four
tabs (Ожидают решения / Принятые / Отклонённые / Все, `engKoFilter` + `engKoTabsHtml()`) filter the
same in-memory list client-side, each with a live count. Every row already carries, straight from
the existing `review_queue` record (`server/reviewQueueModel.js#toApi`): kind, project/vehicle,
summary + rationale + evidence (unchanged), `created_at`, and once resolved - `resolved_at`,
`resolution`, and `payload.decision` (`by`, `result`). `koResultInfo()` turns that `result` into a
plain-language outcome ("Проект создан: ...", "Проект обновлён: этап → ...", "Принято к
сведению - проект не создан и не изменён", ...), including a section-aware note when the touched
project isn't `record_category: main_project` or is already marked completed (so it won't be on the
Kanban even though the change was real), plus an "Открыть" button that jumps straight to the project
card or the personal task manager. Survival across a restart needed no new code - `review_queue` is
the same on-disk collection every other feature already persists through; verified anyway in
`test_ko_proposal_history_survives_a_server_restart`.

## 3. Разделение действий

Already correct at the `apply()` level before this change (project_match/needs_clarification -
acknowledge only; every other kind - a real operation through the same model functions/checks as
the dashboard's own manual edit). What changed is that this distinction is now visible: the apply
button itself was already labelled "Принять к сведению" vs "Применить изменение" for the two
groups (unchanged); the new part is that **after** the click, `koAction()` reads the response body's
`result` (always returned by `apply()`, previously discarded) and shows the real outcome via
`koResultInfo()` instead of just refreshing the list.

## 4. Создание проекта (новое, явное действие)

`server/ko/proposals.js#createProjectFromProposal` + `POST /api/ko/proposals/:id/create-project`
(`server/routes/koDashboard.js`, admin-session + CSRF, same as every other `/api/ko` mutation) -
entirely separate from `apply()`, never triggered by accepting `needs_clarification`/`project_match`
automatically:

- Validated, fixed field set (`manufacturer`, `vehicle_model`, `product_type`, `status`,
  `article_number`, `assigned_engineer`, `priority`, `description` - never the full set an agent
  plan-sync can write); `status` (confirmed stage) is mandatory, enforced before
  `projectsModel.validateProjectInput` even runs.
- Duplicate check via the exact same identity match the dashboard/agent project-creation paths
  already use (`projectsModel.matchByIdentifiers`) - refuses with `409` and the existing project's
  id(s) rather than creating a second record.
- Always `record_category: "main_project"` - there is no other path into this action, so "main_project
  только после подтверждения администратора" is this action itself.
- Fully synchronous end to end (no `await` between the `item.resolved` check and `close()`), the same
  safety property `apply()`/`reject()` already rely on - a second click after the first succeeds
  always hits the `item.resolved` guard and `409`s before touching `projectsModel.createProject`
  again.
- Records the link back to the proposal and its Telegram evidence the same way `apply()` already
  does for every other kind (`recordActivity(..., "ko_proposal_applied", ..., { proposal_id, kind,
  links })`), plus one manual comment quoting the proposal's summary/evidence on the new project.
- Frontend: `public/index.html`'s `#koCreateModalOverlay` (same `.modal-overlay`/`.modal` pattern as
  the existing user-creation dialog), pre-filled from the proposal's own `project_ref`/`change`,
  always requiring the person's explicit confirm (`window.confirm` + the form's own submit) before
  the request is sent. On success: closes the modal, shows the outcome via the same
  `koResultInfo()`, and opens the new project's card - which, because it is always `main_project`,
  is always on the Kanban.
- **Also works on an already-accepted proposal** (критическое требование: "для ранее принятого
  предложения можно вручную создать новый проект, если он ещё не существует") - specifically one
  resolved as `acknowledged` (`project_match`/`needs_clarification`, accepted at the time with zero
  side effect) that nothing has created a project from since. `createProjectFromProposal` checks
  this *before* its `resolved_project_id`/validation/duplicate logic, which stays identical either
  way; any other already-resolved state (a real change applied, or rejected) is still refused -
  creating a project is not a way to undo or redo a different kind of decision.
  `recordLaterProjectCreation()` writes the outcome to its own `payload.project_created_later` field
  - **never** to `resolved`/`resolved_at`/`resolution`/`payload.decision`, which stay byte-for-byte
  what they were at the original acknowledgement (critical requirement: "создание проекта не
  изменяет задним числом историю первоначального решения"). The frontend shows both: the original
  "Принято к сведению" line, unchanged, plus a second "Позже: ..." line with its own timestamp/actor
  and its own "Открыть". Idempotent the same way as the first-creation path - whichever write
  happens (`close()` or `recordLaterProjectCreation()`) is the last synchronous step before
  returning, so a double click, a repeated request, or one issued after a server restart all find
  `project_created_later` already set and are refused with the existing project's id, never a second
  project.

## 5. TAISHAN и HGH

Not recreated, not looked up against any live system (no access here). The new history tabs +
`koResultInfo()` are exactly the tool an administrator with real Railway access needs to answer this
themselves: open «Предложения агента КО» → «Все», find the TAISHAN/HGH rows (they will show
`resolution: acknowledged` if this diagnosis is right), and their `decision.result` will say plainly
whether anything was created. If a project was genuinely never created for either, the «Создать
проект» button is now available directly on that same already-resolved row (see §4 above) - one
explicit action, from the real proposal, with the real evidence still attached, run once by the
administrator with actual access - not recreated or guessed from this session.

## 6. Совместимость

- **Вариант 4**: new tab bar reuses the exact `.eng-detail-tabs`/`.eng-detail-tab` classes from the
  Phase 5 redesign; the new modal reuses the existing `.modal-overlay`/`.modal` pattern (same as the
  user-creation dialog); one new badge variant (`.eng-card-source.rejected`) added next to the three
  that already existed, using the same `--crit-soft`/`--crit-ink` tokens as everywhere else.
- **Existing API/agent КО**: `submit()`/`apply()`/`reject()` untouched; the one new route is strictly
  additive.
- **Права**: `/api/ko/*` stays entirely under `requireAdminRole` (`server/index.js`, unchanged) - the
  new route inherits this automatically by living on the same router; `ko_viewer` still has no
  "Предложения агента КО" subtab, no bell, and the API 403s it directly (verified, not assumed).
- **История проектов / Telegram**: no change to `server/projectsModel.js`'s history rows,
  `server/telegram/*`, or the КО archive - `createProjectFromProposal` only calls the same
  `createProject`/`recordActivity` functions every other project-creating path already uses.
- **Уведомления**: `openKoNotification()` now forces the history filter to "Все" before flashing the
  target row, since a notified proposal may already be resolved by the time it's clicked and would
  otherwise sit under a different tab than whichever one happened to be open.
- **Данные Railway**: this branch has never connected to or altered any live Railway database.

## 7. Тесты

`tests/test_ko_integration.py` (+11 new since this file started, 37 total in the file, all passing):

- `test_needs_clarification_apply_still_creates_nothing_the_taishan_symptom` - reproduces the
  reported symptom exactly; proves `apply()`'s acknowledge path was always correct.
- `test_create_project_from_proposal_requires_fields_dedupes_and_is_not_double_clickable` - auth/CSRF
  gates, field validation, successful creation (record_category/status/history/comment all
  checked), a second click on the resolved proposal `409`s without creating a second project, and a
  second *different* proposal for the identical identity is refused as a duplicate.
- `test_create_project_action_is_admin_only_like_the_rest_of_the_ko_router` - `ko_viewer` gets `403`,
  no project created.
- `test_ko_proposal_history_survives_a_server_restart` - applied/rejected/pending all intact after
  `srv.restart()`, including `decision.result`/`reason`, and the still-open one can still become a
  project afterwards.

**Pre-publication final checklist** (one test per item, so a future regression names exactly which
guarantee broke):

1. `test_check1_acknowledged_needs_clarification_and_project_match_show_under_applied` - both kinds,
   once accepted, come back `resolved: true, resolution: "acknowledged"` from `status=all`, which is
   exactly the frontend's "Принятые" predicate (`resolved && resolution !== "rejected"`).
2. `test_check2_create_project_from_a_previously_accepted_proposal` - the new late-creation path,
   confirmed with `late: true` in the response.
3. `test_check3_late_creation_never_rewrites_the_original_decision` - byte-for-byte comparison of
   `resolved_at`/`resolution`/`payload.decision` before and after; only `payload.project_created_later`
   is new.
4. `test_check4_late_creation_is_protected_against_double_click_repeat_and_restart` - a second
   immediate request, a third request after `srv.restart()`, and a direct count of TAISHAN-identity
   projects (always exactly one).
5. `test_check5_make_model_product_category_and_stage_are_all_required` - each of
   manufacturer/vehicle_model/product_type/status individually omitted is refused with `400`; a
   client attempt to override `record_category` to `"backlog"` is silently ignored - the created
   project is still `main_project` (category is a fixed, always-enforced value, never a
   client-settable field at all, so "required" holds unconditionally by construction).
6. `test_check6_created_project_lands_in_the_correct_kanban_section` - confirms the exact predicate
   `public/index.html`'s own Kanban list uses (`record_category === "main_project" && !is_completed`)
   on the freshly created project via the real `/api/projects` the UI itself calls.

Existing suites re-run clean: `tests/test_ko_integration.py` (37 passed), plus
`tests/test_ko_notifications.py`, `tests/test_kanban_scroll.py`, `tests/test_history_journal.py`,
`tests/test_user_accounts.py`, `tests/test_shturman_app.py` (61 passed together) - covering the
messages centre, Kanban scroll, role gating and restart-persistence of *other* features, to confirm
none of them regressed.

**Real-browser verification** (admin and `ko_viewer`, headless Chromium), two passes:
- First pass (unresolved proposals): all four history tabs with live counts; applying a
  `stage_change` proposal shows "Проект обновлён: этап → Разработка: ..." and the row moves to
  «Принятые» with actor/timestamp; rejecting shows "Предложение отклонено" and the row moves to
  «Отклонённые»; "Открыть" on a resolved row opens the correct project card; the create-project
  modal pre-fills from the proposal, creates `TAISHAN X` as `main_project`/`scanning`, opens its
  card, and the card is immediately visible on the real Kanban's «Сканирование» column; a second
  attempt at the identical identity is refused with the existing project named in the error;
  `ko_viewer` has no "Предложения агента КО" subtab, no bell, and `GET /api/ko/proposals` 403s
  directly for that role.
- Second pass (the new late-creation path): a TAISHAN `needs_clarification` proposal applied via
  the API first (simulating "already accepted earlier"), confirmed visible under «Принятые» with the
  "Создать проект" button still offered on that resolved row; opening the messages bell panel while
  this screen is open works exactly as before; using "Создать проект" on the resolved row creates
  the project, opens its card, and the row afterwards shows **both** the original "Принято к
  сведению - проект не создан и не изменён" line (unchanged) **and** a new "Позже: Проект создан:
  ..." line with its own timestamp/actor, with the create-project button now gone (already used).
- Zero browser console errors in any session, either pass. Screenshots delivered in chat both times.

**Full `npm test`**: 416 passed, 1 skipped, 2 failed - the same two pre-existing, unrelated failures
tracked since the light-redesign work (`tests/test_parsing.py::test_pdf_table` - sandbox
`cryptography`/`cffi` issue; `tests/test_telegram_digest.py::test_digest_includes_recent_engineering_changes_with_a_link`
- a real-calendar edge case in that test's own date helper).
