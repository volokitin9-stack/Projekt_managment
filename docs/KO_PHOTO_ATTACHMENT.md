# КО-агент: фотографии предложения не попадали в карточку проекта

Branch: `claude/ko-photo-attachment`, from `claude/railway-deployment` (already includes the КО
proposal lifecycle work, merged as PR #11). Fixes a reported incident: a proposal about Lada
Azimut, защита картера, арт. 6029 showed two Telegram photos in "Предложения агента КО"; after a
project was created from that proposal, the project card's «Фото» tab was empty.

No access to the live Railway database was used or needed to diagnose or fix this - the bug is
fully explained by reading the code, and every test below runs against a disposable local server.

## 1. Диагностика

**Где хранятся оригиналы.** `server/ko/archive.js`: every photo the КО Telegram chat ever sent is
downloaded once and kept in `KO_FILES_DIR` (`<DATA_DIR>/ko-files` by default - the same persistent
Railway volume as everything else, never an ephemeral path), indexed by the `ko_files` store
collection (`id`, `file_unique_id`, `chat_id`, `message_id`, `storage_key`, `mime_type`, `status`).
**There is no separate thumbnail** - the proposal panel's small `<img style="width:64px...">` is the
same stored file scaled down by the browser, served by `GET /api/ko/files/:id`
(`server/routes/koDashboard.js`) straight from `archive.readFile()`.

**Identifiers.** Telegram's own `file_id` is ephemeral (download-only, never stored after use) and
`file_unique_id` is the stable Telegram identity used for archive-side dedup. The proposal's own
`payload.photos[].file_id` field is **not** a Telegram id at all - confusingly named the same, it is
the internal `ko_files.id` (confirmed by `server/ko/proposals.js`'s `checkPhotos()`:
`archive.getFileMeta(ph.file_id)` looks it up by `.id`). Once attached to a project, the file gets a
second, separate `project_files.id` (its own attachment id), carrying `telegram_message_id`/
`telegram_chat_id`/`telegram_ref` pointing back to the archived message.

**Root cause.** `server/ko/proposals.js`'s `apply()` already attached photos correctly for the
`photo_link` kind, on an *already-resolved, existing* project (`projectsModel.attachProjectFile`).
But `createProjectFromProposal()` - the action that creates a brand-new project from a proposal like
the Lada Azimut one (a vehicle with no existing project, so the agent could only submit
`needs_clarification`) - **never looked at `payload.photos` at all**. It created the project and a
text comment citing the evidence, and simply never called `attachProjectFile`. The Telegram
originals were never lost or deleted - they stayed exactly where `server/ko/archive.js` always
stores them; the project's own `project_files` row (what the «Фото» tab actually reads) was just
never created. This is why `photo_link` "didn't work" for a newly created project: `photo_link`
itself was never involved - it's a different proposal kind, gated on an already-resolved project
(`rules.NEEDS_PROJECT`), and was never the path Lada Azimut's `needs_clarification` proposal took.

Linkage: a proposal's photos are tied to `file_id` (→ `ko_files`, hence `chat_id`/`message_id`) and,
once resolved, to whichever project the proposal itself resolves to
(`resolved_project_id`, or the result of `createProjectFromProposal`/
`recordLaterProjectCreation` - see `docs/KO_PROPOSAL_LIFECYCLE.md`). `proposal_id` itself was never
stored on the attached file's own row (consistent with the pre-existing `photo_link` behaviour,
which doesn't either - traceability lives on the project's own comment/history entry instead, which
*does* carry `ko_proposal_id`).

## 2. Создание проекта из предложения

`server/ko/proposals.js`'s new `attachProposalPhotos(projectId, photos, opts)` is the fix, shared by
project creation and the new manual action below - the identical validate/store/dedupe path a
manual upload or `photo_link` already use (`storage.validateFile` + `projectsModel.attachProjectFile`,
which already dedupes per-project by `(telegram_chat_id, telegram_message_id)`, so re-running this
for the same message never creates a second copy). Unlike `photo_link` (left untouched - критическое
требование "сохранить работающую логику предложения photo_link"), this never aborts partway: a
missing or rejected original is collected into `unavailable` and the rest of the photos still get
attached - критическое требование "при отсутствии оригинала явно указывать, что он недоступен", not
fail project creation over one bad file. `createProjectFromProposal()` now calls this right after
creating the project and records the outcome in its own response (`result.photos.attached`/
`result.photos.unavailable`) - visible in the dashboard's result column immediately, never silently
dropped. Never deletes, re-downloads, or otherwise modifies the archived Telegram original.

## 3. Привязка фотографий к существующим проектам

Automatic matching is unchanged and still as strict as before: a proposal only has a project to
attach to when its own `project_ref` resolved unambiguously at submit time
(`projectsModel.matchByIdentifiers` - exact external_project_id/article_number, or an exact
manufacturer+model+product triple). **Nothing new auto-attaches on an ambiguous match** - a proposal
whose reference could fit more than one project (or none at all) simply has no project to attach to,
and `photoStatus()` reports it as `pending_no_project`, same as before this phase.

For that case, a new **explicit, manual** action closes the gap - критическое требование
"предусмотреть ручную привязку фотографии из предложения к конкретному проекту":
`POST /api/ko/proposals/:id/attach-photos` (`server/routes/koDashboard.js`, admin session + CSRF)
takes a `project_id` the person picks themselves (`public/index.html`'s new «Привязать фото к
проекту» button + a small search-and-pick modal, same `.modal-overlay` pattern as the rest of the
dashboard) - no auto-matching involved at all, and it works on a proposal in *any* state (open,
acknowledged, applied, rejected - the photos are real evidence independent of whatever decision
track the proposal's text/stage/etc are on). The existing `photo_link` kind's own apply()-time
behaviour is untouched.

Rather than inventing a second, parallel "unassigned photo" queue, an ambiguous or not-yet-linked
proposal's photos stay visible exactly where the person already has full context for them - the
proposal's own row in «Предложения агента КО» (summary, rationale, evidence, confidence) - with the
same manual action available there. The older, separate `telegram_photo` review-queue mechanism
(`server/telegram/engineeringChannel.js`, a different photo pipeline entirely, for the AI-channel
direct-analysis path) is untouched and not reused here; it was already missing even a "pick a
project" action of its own, so bridging into it would have added a second gap-filled pathway instead
of a single, well-contextualized one.

## 4. Старые фотографии (Lada Azimut арт. 6029, TAISHAN, HGH)

`GET /api/ko/photo-backlog` (admin-only, **read-only** - критическое требование "не выполнять
массовую автоматическую привязку без предварительного отчёта") scans every resolved `ko_proposal`
with photos and reports, per proposal: already fully attached, ready to attach now (project known,
originals available, just never run - this phase's own bug), blocked by a missing original, or no
project linked yet at all. It changes nothing by itself. An administrator reviews the report, then
fixes each one individually with `apply()`/`createProjectFromProposal()` (if still open) or
`attachPhotosToProject()` (if already resolved) - never a bulk auto-apply from the report.

This session has no access to the live Railway database, so the real TAISHAN/HGH/Lada-Azimut-6029
records were never inspected, and nothing was recreated or auto-attached for them here - every test
below uses disposable local test data with the same names, never real records. An administrator
with actual Railway access runs the same report there to get the real picture.

## 5. Хранение и безопасность

- Storage is unchanged: `KO_FILES_DIR` for originals, `UPLOADS_DIR` for attached project files -
  both resolve from `DATA_DIR`/`UPLOADS_DIR`, the same persistent Railway volume every other
  collection already uses. Nothing in this phase writes anywhere ephemeral.
- Telegram source links (`telegram_chat_id`, `telegram_message_id`, `telegram_ref`) are preserved on
  every attached file exactly as `photo_link` already does it - unchanged.
- Role access: `/api/ko/*` (including the two new routes) stays entirely under
  `requireAdminRole` (`server/index.js`, unchanged) - `ko_viewer` gets 403 from both the manual
  attach action and the backlog report, verified directly, not assumed.
- Duplicate protection: `projectsModel.attachProjectFile`'s existing per-project
  `(telegram_chat_id, telegram_message_id)` dedupe is the single source of truth re-used everywhere
  photos are attached in this phase - re-processing the same message or re-running the manual action
  never creates a second copy.

## 6. Интерфейс

- The project card's «Фото» tab already renders every real `project_files` row
  (`engPhotosSection()`, untouched) - no frontend change was needed there at all once the backend
  actually creates those rows; verified with a real browser (screenshot below).
- «Предложения агента КО»: each proposal with photos now shows a live, server-computed status line -
  **привязано** / **ожидает привязки** / **ожидает (нет проекта)** / **оригинал недоступен**
  (`server/ko/proposals.js`'s `photoStatus()`, computed fresh on every read - never cached, never
  guessed from the thumbnail or from stored metadata alone: it actually calls `archive.readFile()`
  to confirm the bytes are still really on disk, not just that `ko_files.status` once said "stored").
  A «Привязать фото к проекту» button appears whenever at least one photo isn't attached yet.

## 7. Тесты

`tests/test_ko_integration.py` (+13 new, 46 total in the file, all passing):

- `test_photo_multi_photo_proposal_and_project_creation_attaches_them` - the exact reported
  incident, two photos, reproduced and confirmed fixed end to end (including the project's own
  `images` list and the proposal's live `photo_status`).
- `test_photo_manual_attach_to_an_existing_project_and_no_duplicate_on_repeat` - the new manual
  action, and that repeating it is a safe no-op.
- `test_photo_lada_azimut_6029_two_photos_manually_attached_without_recreating_the_project` -
  the final-check wording literally: an existing Lada Azimut/арт. 6029 project (created from its
  own proposal, never a duplicate) gets both of the incident's photos via the manual action - one
  already attached at creation time, the second added later onto the same project id - and a
  pre-existing, unrelated manual upload on that project survives untouched alongside them
  (`final["images"]` has all three; project count before/after stays at one Lada Azimut).
- `test_photo_attach_requires_an_explicit_project_choice` - no `project_id` (or an explicit `null`)
  is refused with `400` before any matching is attempted; a real id succeeds.
- `test_photo_status_and_backlog_survive_a_server_restart`.
- `test_photo_missing_original_is_reported_not_fatal` - the original Telegram bytes are deleted
  from disk directly (simulating real data loss, since a proposal can never be *submitted*
  referencing a photo that failed to archive in the first place - `checkPhotos` already refuses
  that at submit time); project creation still succeeds, the photo is reported `unavailable`, no
  placeholder/fake image is ever created.
- `test_photo_ambiguous_match_is_never_auto_attached_backlog_flags_it` - two same-make/model demo
  projects, a proposal that can't tell them apart never auto-attaches to either; the manual action
  still works once a person picks one.
- `test_photo_role_access_matches_the_rest_of_the_ko_router` - `ko_viewer` gets 403 from both new
  routes.
- `test_photo_backlog_report_names_lada_azimut_taishan_and_hgh_without_touching_anything` - the
  report alone creates/changes nothing (checked by project count before/after); fixing the Lada
  Azimut case via its own existing proposal (not a new one) attaches its photo and the report then
  shows it `fully_attached`.

Existing tests re-verified unaffected: `test_apply_assignee_deadline_comment_task_and_photo`
(`photo_link`'s own, pre-existing behaviour, untouched) still passes; the project-creation test from
the previous phase was updated for the new `result.photos` field (always present, empty when there
are no photos) rather than asserting a now-incomplete exact dict.

**Real-browser verification**: a Lada Azimut/защита картера/арт. 6029 proposal with one real
photo (downloaded through a local fake Telegram server, not any live chat) - before creating the
project, the row shows "Фото: 1 ожидает (нет проекта)"; creating the project attaches it
immediately, opens the new card, and its «Фото» tab shows the real thumbnail; back in «Предложения
агента КО», the same row now shows "Фото: 1 привязано" with a working "Открыть" link. Zero browser
console errors. Screenshots delivered in chat.

**Full `npm test`**: 425 passed, 1 skipped, 2 failed - the same two pre-existing, unrelated failures
tracked since the light-redesign work (`tests/test_parsing.py::test_pdf_table`;
`tests/test_telegram_digest.py::test_digest_includes_recent_engineering_changes_with_a_link`),
re-confirmed again before publication together with `tests/test_ko_notifications.py`,
`tests/test_kanban_scroll.py`, `tests/test_history_journal.py`, `tests/test_user_accounts.py`,
`tests/test_shturman_app.py` (61 passed together).

## Pre-publication final checks (this round)

1. `origin/claude/railway-deployment` re-fetched - unchanged since this branch was created
   (`bd57ea6`, already includes PR #11). A test-merge of it into this branch reports
   "Already up to date" - no conflicts, nothing foreign in the diff (still exactly the 5 files
   listed above).
2. Automatic attach on new-project creation - `test_photo_multi_photo_proposal_and_project_creation_attaches_them`.
3. Manually linking an *existing* Lada Azimut/арт. 6029 project to both of the incident's photos,
   without recreating it - `test_photo_lada_azimut_6029_two_photos_manually_attached_without_recreating_the_project`.
4. Protection against re-attaching + existing attachments preserved - covered by the same test
   (a pre-existing manual upload survives) and by `test_photo_manual_attach_to_an_existing_project_and_no_duplicate_on_repeat`.
5. Manual attach is admin-only (`test_photo_role_access_matches_the_rest_of_the_ko_router`) and
   requires an explicit `project_id` (`test_photo_attach_requires_an_explicit_project_choice`).
6. `photo-backlog` makes no automatic changes - `test_photo_backlog_report_names_lada_azimut_taishan_and_hgh_without_touching_anything`
   (project count checked before/after calling the report).
7. Full profile re-run, see numbers above.
