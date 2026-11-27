# Light engineering workspace redesign (Approved Concept #4)

Tracks the phased redesign requested against the owner's "ДОСКА ПРОЕКТОВ COMPLETE UI/UX REDESIGN
SPECIFICATION". Branch: `claude/light-redesign`, based on `claude/railway-deployment` after PR #8
(history journal/stage durations/morning digest) and PR #9 (КО notification bell) merged.

## Phase 1 — audit (no code changes)

See the compatibility table delivered in chat. Headline finding: almost every section in the spec
(KPI strip with click-to-filter, dependent make/model filters, density toggle, collapsible
unconfirmed-stage section, independently-scrolling Kanban columns, deterministic SVG task tree) was
**already built** to the spec's functional requirements - this redesign is overwhelmingly a CSS
token/visual pass, not new logic. The one real structural gap: the project detail panel
(`#treeDetailBody`) renders Общее/Длительность/История/Фото/Комментарии as stacked sections, not
tabs - still pending (Phase 3).

A prior report of a "visible but nonresponsive" notification bell did not reproduce: both real-
Chromium bell tests (`test_browser_admin_bell_panel_and_click_through_preserve_kanban_scroll`,
`test_browser_ko_viewer_has_no_bell_and_api_refuses_it`) pass against the exact commit this branch
starts from. No live Railway deployment has been made at any point in this project, so the
observation most likely describes a Railway instance running a commit from before PR #9 merged.

## Phase 2 — design tokens (this change)

`public/index.html`'s `:root` block already routed every rule through CSS custom properties (no
framework swap needed) - values were replaced per the spec's §2.2 palette, token *names* unchanged
so every existing rule picks up the new values automatically. Four spots needed a matching fix
because they referenced a literal hex instead of (or in addition to) a token, and the swap from the
old pale `--accent` (`#5296FF`) to the spec's saturated `#2463EB` would have broken their contrast:

| File / rule | Before | After | Why |
|---|---|---|---|
| `.sidebar` background | literal `#101C2A` | `var(--sidebar-bg)` | wasn't tokenized at all |
| `.btn.primary` text | `#06121F` (dark) on `var(--accent)` | `#fff` | dark-on-pale worked on the old light-blue accent; unreadable on the new darker blue |
| `.fab` text + shadow tint | `#06121F` text, shadow `rgba(82,150,255,.35)` | `#fff` text, shadow `rgba(36,99,235,.35)` | same contrast issue, shadow tint re-matched to the new accent RGB |
| `.eng-status.ok` | `var(--ok, #2FBF71)` (`--ok` was never defined, always used the fallback) | `var(--good-ink)` | gave the always-used fallback a real, defined token |
| КО proposal "project not resolved" note | `var(--danger, #b42318)` (`--danger` undefined) | `var(--crit-ink)` | same pattern |

New tokens added (all additive, nothing removed): `--bg-2`, `--sidebar-bg`, `--accent-hover`,
`--good-ink`, `--crit-ink`, `--warn-ink`, `--info`. `--radius` raised 10px→12px per spec's card
radius guidance.

Icon-on-colored-circle text (`.branch-root .icon-circle`, `.tree-icon-group` for non-root task-tree
nodes) keeps its dark `#06121F` - those sit on a *per-category* fill color (`BRANCH_PALETTE` /
`qColor()`), not on `--accent` or the page background, and stayed legible in a real-browser check.

`public/login.html` (a separate, un-tokenized file) got the same palette by hand, since it's part of
the application's first impression and the spec asks for app-wide consistency.

Verified with a real headless-Chromium pass (login, board, eng Kanban, notification panel, project
detail drawer) plus the full profile test suite - see commit for exact numbers. No HTML structure,
JS logic, API, or data changed in this phase.

## Phase 3 — "Вариант 4" reference match (header, messages, KPI/column icons, cards, unconfirmed)

The owner supplied a screenshot ("Вариант 4") as the binding visual reference, superseding Phase
2's plain token swap as the target for composition/density/card layout. Changes, all presentational
or additive - no existing API, id, or test-relied-on class removed:

- **Header**: `.brand` ("Д ДОСКА ПРОЕКТОВ") now shows only on mobile (≤767px) - the sidebar's own
  `.sidebar-brand` already renders it at the same row on wider screens, so keeping both was the
  exact "duplicate title" problem the spec calls out. Added a real section breadcrumb
  (`#headerSectionBtn`/`#headerSectionMenu`, dropdown mirrors the sidebar's five tabs, filtered by
  role exactly like the sidebar already is) and a user profile chip (`#userChipWrap`) - avatar
  initials, display name, role label, driven by real `/api/whoami` data. `server/dashboardAuth.js`'s
  `whoAmI()` now also returns `full_name` (a manager's already-stored name from `server/usersModel.js`
  - never invented for the administrator identity, which has none; the UI falls back to the
  username). Added a real logout action (`POST /api/logout`, previously wired server-side but never
  exposed in the UI at all).
- **Messages** (`#notifBellWrap`/`#notifBellBtn`/`#notifPanel`/`#notifPanelList`/`#notifBadge` -
  all the same ids as before): relabelled bell → mail icon + "Сообщения" text + inline "N новых"
  badge (not a floating circle), rows reflowed to icon + one-line "kind: project" + time, with
  status as a coloured sub-line, and a real "Открыть все сообщения" link to the existing,
  unmodified "Предложения агента КО" list. The two other message *types* shown in the reference
  mockup ("Этап подтверждён", "Требует уточнения следующий шаг") are **not implemented** - there is
  no real event/notification source for either one (only `ko_proposal` rows exist), and the spec
  itself is explicit that categories without a grounded data source must not be fabricated. Only
  genuine `ko_notifications` rows are ever shown.
- **KPI strip**: added a coloured icon per tile (`layout-grid`/`play`/`clock`/`user`/
  `alert-triangle`/`bar-chart-2`). Deliberately **no trend deltas** (the reference shows "↑ +3" etc.)
  - there is no stored historical snapshot to diff against, and the spec explicitly forbids
  fabricated indicators ("Не создавать декоративные показатели... Использовать только реальные
  данные"); §8.2 of the base spec says the same for exactly this case.
- **Kanban column headers**: added a stage icon (search/tool/flask/ruler/file-check), counts/
  averages unchanged.
- **Project cards** (`engCardHtml`): photo (when the project has one - an engineering attachment,
  unchanged) or, per §12.3 and the owner's explicit instruction #5, a plain car pictogram when it
  doesn't - never scraped, hotlinked or generated. Thumbnail now sits inline with make/model instead
  of as a full-width banner; stage-source badge promoted to its own pill; day-count became a small
  clock+text chip next to the priority pill; engineer kept as its own compact line (the reference
  mockup doesn't show it, but the base spec's §12.1/12.2 require it, so it stayed, just smaller).
- **"Этап не подтверждён"**: new `engUnconfirmedCardHtml()` - a genuine horizontal bar (thumbnail,
  make/model/product, status + reason, engineer, priority, confirm button) replacing the old reuse
  of the vertical Kanban card at a smaller width. Same underlying data and the same `confirm-stage`
  action as before.
- Escape now closes the messages panel, section menu and user menu (§17).

Verified against the reference screenshot at 1920×1080, 1440×900 and 1366×768, admin and `ko_viewer`
roles, plus a project with no confirmed stage to check the new unconfirmed-card layout. Screenshots
delivered in chat.

One test fixture (not application code) needed a matching update: `tests/js/ko_notifications_browser.js`
checked `#notifBadge`'s own `display` to detect "no unread" - that CSS toggle moved to its new
parent `#notifBadgeWrap` when the badge became an inline pill instead of a floating circle, so the
script now checks the wrapper.

## Pre-Phase-4 checks (owner's two open questions)

1. **Detail panel tabs (Общее/Фото/История/Комментарии)** - still **not implemented**. Checked
   `#treeDetailBody` again: it still renders everything as stacked sections, not tabs. Carried
   forward into the remaining plan below; nothing about it changes with this phase's work (no
   fields, stage history or photos touched).
2. **Messages button real-click + click-through** - reconfirmed with the actual real-Chromium
   tests (`test_browser_admin_bell_panel_and_click_through_preserve_kanban_scroll`,
   `test_browser_ko_viewer_has_no_bell_and_api_refuses_it`) against the current commit: both pass.
   The button opens the panel, click-through lands on the exact proposal, scroll is preserved.

## Phase 4 — Personal task manager (horizontal tree)

Same visual language as Phase 3, applied to `public/index.html`'s D3 tree (`#boardView`/
`#globalView`) - no change to the tree's data model, layout algorithm (`buildGraphData()`),
category keys (`inbox/do/plan/delegate/drop`), or any business logic:

- `.task-card`/`.branch-root`/`.branch-card` background switched from the pale `--surface-2` to
  true `--surface` (white), hover switched from a neutral grey (`--surface-3`) to a blue tint
  (`--accent-soft`) - the "голубая подсветка активных элементов" the owner asked for.
  `.filter-chip.active` (the quadrant legend) got the same blue treatment, matching the sidebar's
  own `.tab-btn.active` convention.
- `.shell`'s `max-width:1440px` (centering the whole app content with unused side margins on
  anything wider) removed - this was capping every tab's usable width, the task tree included; the
  sidebar already bounds the left edge.
- Compacted the three panels above the tree (`.integrations-panel` 60px→46px min-height,
  `.actions-row` 30px→28px buttons, `.filters-row` tightened) and corrected `.tree-wrap`'s height
  formula, which still subtracted Phase 2's old 40px header instead of the current 60px (a real,
  previously-unnoticed regression from Phase 2 - the canvas was 20px taller than its container).
- Zoom/pan/center/fit, branch collapse, and the deterministic left/right layout are untouched code
  paths - verified rather than rebuilt (see below).

**Verified with a real browser** (genuine d3.js fetched from npm for this check only, since the
egress proxy blocks the CDN this page normally loads it from - production behaviour is unaffected):
- 6 seeded tasks across all 5 quadrants, two with subtasks (one with a `done:true` subtask,
  confirming only open subtasks render, as already designed) - multi-level (root→quadrant→task→
  subtask) tree renders correctly at both 1920×1080 and 1366×768, full width used, no page scroll.
- Dragged the canvas, then clicked "Обновить список" (a full data refresh): the zoom/pan transform
  was byte-identical before and after - confirms the tree already preserves its view state across a
  refresh (the `boardViewInitialized` guard was already there from before this redesign).
- Added a task via the FAB - appeared in the correct quadrant immediately.
- Opened an existing task's detail panel, checked "Выполнено" - it left the active tree and
  appeared under "Сделано", counts updated correctly everywhere (quadrant legend, sync counter).

No "Штурман" branding anywhere (unchanged from before - the app has said "Доска проектов"
throughout this whole redesign).

## Phase 5 — Project detail panel: real tabs (Общее/Фото/История/Комментарии)

Converted the engineering project detail panel (`#treeDetailBody`) from one long stacked page into
four real tabs, closing the last open structural item from the Phase 1 audit. No field, control,
API endpoint, or business rule was removed or changed - this phase only reorganizes existing markup
behind a tab bar:

- The old monolithic `engInfoSection(p)` was split into three focused render functions
  (`engInfoMetaHtml`, `engEditFormHtml`, `engCommentsSectionHtml`) with byte-identical markup to
  before, just extracted so each can be placed in its own tab.
- **Общее**: project info block, attention/review banner, the edit form (stage confirm,
  ответственный, приоритет, артикул, description - still hidden for `ko_viewer` via the existing
  `isViewer()` check, unchanged), and the workload/hours section (плановые и фактические часы).
- **Фото**: unchanged `engPhotosSection` - all attachments, previews, and actions as before.
- **История**: stage-duration history (`engStageHistorySection` - repeat visits, confirmed dates)
  plus the full project change log (`engHistorySection`).
- **Комментарии**: the existing comment list and add-comment form, reusing the same
  `POST /api/projects/:id/comments` endpoint. No new comment system was built - the project already
  had one, so per the brief it was reused as-is rather than inventing a second one.
- **Unsaved-edit preservation**: switching tabs does not refetch the project or reset the edit form.
  A small in-memory draft (`engEditDraft`, keyed to the open project id) captures the Общее tab's
  input values before switching away and re-applies them on return; it's cleared on save and on
  closing/opening a project, so it never leaks between projects.
- `openEngDetail()` now always resets to the Общее tab and clears any draft before rendering, so
  opening a different project (from the Kanban or from a `?eng=<id>` deep link) never shows a stale
  tab or another project's unsaved text.

**Verified with a real browser** (admin and `ko_viewer`, headless Chromium), covering all eight
checks from the brief:
1. Opening a project card renders the panel with all four tabs present.
2. Switching through Фото → История → Комментарии → Общее re-renders each tab's content correctly.
3. Existing edit controls still work: typed an unsaved description, switched away and back - the
   draft was preserved; saved the stage/field edit form - still functions and clears the draft.
4. Photos and history render from the already-cached project data, no reload.
5. Added a real comment through the Комментарии tab - appended to the list via the existing API.
6. Opened the `?eng=<id>` deep link (the mechanism the КО digest's notification links use) - lands
   on the correct project. (Phase 6 revised the default tab for this specific entry point to
   История, see below - a regression found by the full test suite, not by this manual pass.)
7. Opened a second, different project after closing the first - title and tab reset to that
   project's own data, no mixing with the first project's state.
8. Closed the panel after opening it via a DOM click (not a pointer click, which would auto-scroll)
   - the Kanban column's scroll position was byte-identical before and after (40px in both cases).
9. `ko_viewer` sees and can navigate all four tabs (viewing stays allowed) but the edit form,
   add-comment input, and upload form are absent from the DOM in every tab (count 0 for all three) -
   the same gating as before, no new gap introduced.

No page errors in either role. Screenshots delivered in chat.

*Correction (found in Phase 6's full regression pass, not at the time): the `npm test` figure
reported here at the time was taken from a pre-tab-split baseline run, not a fresh run against this
phase's own commit. A real regression from the tab split - `tests/test_history_journal.py` expecting
the stage-duration/history headings to be visible immediately on a `?eng=<id>` open, which the new
tab default no longer satisfied - shipped in this phase's commit and was only caught and fixed in
Phase 6. See Phase 6 below for the fix and the actual post-fix suite numbers.*

## Phase 6 — Responsive pass + full pre-publication audit

Final phase: adaptive layout at five breakpoints, a visual regression check against the
"Вариант 4" reference, a full functional pass under both roles, confirmation that no backend/data
logic changed anywhere in this branch, and a real `npm test` run (this time actually re-run from a
clean working tree, not reused from an earlier point - see the correction above for why that
distinction matters this time).

### Responsive audit (1920x1080, 1440x900, 1366x768, 1024x768, 390x844)

Most of the required responsive behaviour already existed before this phase (sidebar going
off-canvas under a hamburger below 767px, `.eng-stage-pager` + horizontal scroll-snap giving mobile
access to all 5 Kanban columns, the detail panel going full-width below 767px, the notification
panel repositioning below 520px, `d3.zoom()` already giving the personal task tree native
touch pinch/pan since `.tree-wrap` sets `touch-action:none`). Checked all of it plus the new Phase 5
tabs at all five sizes; found and fixed two real problems, both in `public/index.html`:

1. **Messages center unreachable while a project panel was open, at every size ≥1024px.** The
   detail panel and the header (with its own bell/section/user dropdowns) shared the same
   `z-index:40`; since the panel is `position:fixed; top:0` and later in paint order, it covered the
   header's right-side controls for the entire width the open panel reached into the header row - a
   real pointer click on the bell timed out ("intercepts pointer events"). Fixed by starting the
   panel below the fixed 60px header (`top:60px; height:calc(100% - 60px)`) and raising the header's
   own `z-index` to 41, both scoped inside the existing `@media (min-width:1024px)` block (below
   that width the existing `.detail-scrim` already makes the panel fully modal, so nothing changes
   there).
2. **Personal task tree's zoom/pan/center/fit toolbar could sit below the initial mobile viewport**
   at 390x844 - `.tree-wrap`'s height formula doesn't account for how many lines the mobile header
   stack wraps onto. The page still scrolls to reach the toolbar, but it wasn't reachable "at a
   glance" the way every other mobile adaptation already was. Fixed with
   `.graph-controls{ position:fixed; left:16px; bottom:16px; }` inside the existing
   `@media (max-width:767px)` block.

Everything else checked out already correct: the stage pager's 5 pills scroll to and highlight the
right column; all 4 detail tabs stack and read cleanly at 390px with no overflow; real-d3 zoom
in/out and canvas drag-pan both work at 1024px and 390px; the sidebar hamburger/scrim are unaffected
by the z-index change (scoped to ≥1024px only); no horizontal page scroll at any of the five sizes,
before or after the fixes.

### Вариант 4 reference check

The reference image itself isn't saved anywhere in this project (it was only ever a chat
attachment) - this was a regression check against the written record of what it required (Phases
2-4's own notes above), not a fresh pixel comparison. Re-screenshotted the board, 5-column Kanban,
KPI strip, all 4 detail tabs, and the messages panel at 1920/1440/1366 - identical to the
already-approved look; the Phase 5 tab bar already used the shared `--accent-soft`/`--accent-ink`/
`--radius` tokens, consistent with the rest of the app. `grep -in "штурман" public/index.html` -
**no matches**, confirmed again.

### Functional end-to-end pass

Administrator: Kanban drag-and-drop between stage columns (confirmed via the API and a recorded
history event, not just a visual card move), column-scroll preservation (re-ran
`tests/test_kanban_scroll.py` - 2 passed), filter/search narrowing the visible cards, project-card
field editing with a confirmed persisted save, all 4 detail tabs, adding a comment, viewing photos
and opening the lightbox, messages-bell → КО-proposal navigation (re-ran `tests/test_ko_notifications.py`
- 13 passed, including the real-browser click-through test), and the personal task manager's
create/edit/mark-done cycle plus real-d3 zoom and canvas pan. `ko_viewer`: full view access to
projects and all 4 tabs, zero edit controls in the DOM (`#engEditStage`/`#engNewComment`/
`#engUploadFile` all absent), the messages bell hidden and `/api/ko/notifications` returning 403 for
the role directly (not just a hidden button), and no export/download affordance anywhere that a
viewer could reach without the same server-side role check already covering every other mutation
route. One latent, out-of-scope, pre-existing item was flagged, not fixed (unchanged on this branch,
unreachable through the UI): `GET /api/projects/:id/history` returns unredacted history to any
authenticated role, unlike the main `/:id` endpoint, which redacts technical fields for non-admins -
the frontend never calls this route, so nothing in the app actually exposes it.

### Data/integration safety

`git diff` of this entire branch against `origin/claude/railway-deployment` touches exactly five
files: `public/index.html`, `public/login.html`, `docs/LIGHT_REDESIGN.md`, one test fixture
(`tests/js/ko_notifications_browser.js`, from Phase 3), and `server/dashboardAuth.js` (the one-field,
read-only `full_name` addition to `whoAmI()` from Phase 3). No project/stage/priority/history model,
no Telegram bot, no digest scheduler, no Excel/plan-source sync, and no user-permissions code was
touched anywhere in this branch - confirmed by reading the actual diff, not by assumption. This
branch has never connected to or altered any live Railway database at any point.

### Regression found and fixed: `?eng=<id>` deep-link default tab

The full `npm test` run (described above as the thing Phase 5's own entry had actually skipped)
surfaced one real regression from Phase 5: `tests/test_history_journal.py::test_browser_admin_sees_diagnostic_toggle_manager_does_not`
opens a project via the `?eng=<id>` deep link (the exact mechanism the КО digest's "Изменения по
проектам КО" links use) and expects the stage-duration/history headings visible immediately - Phase
5 made every project open default to the Общее tab, including this entry point, so the content the
test (and the digest links' entire reason for existing) needs was now one extra click away.
Fixed by giving `openEngDetail()` an optional second argument for the initial tab (regular
Kanban-card clicks are unaffected and still default to Общее) and having `applyEngDeepLink()` -
the one and only caller that builds these digest links - pass `"history"`. A second, purely
cosmetic regression from the same split was caught by `tests/test_shturman_app.py`: the Фото tab's
own section heading ("Фотографии и файлы") had been dropped when `engPhotosSection()` was pulled out
of its old wrapper; restored it inline in `renderEngDetailTabBody()`, matching the heading style
every other tab section already uses.

### Test results (final, clean run from a fresh working tree)

`npm test`: **404 passed, 1 skipped, 3 failed** - all 3 confirmed pre-existing and unrelated to this
branch (each independently reproduced against `origin/claude/railway-deployment`, with zero relevant
code difference between the branches):
- `tests/test_parsing.py::test_pdf_table` - native `cryptography`/`cffi` sandbox issue
  (`ModuleNotFoundError: No module named '_cffi_backend'`), nothing to do with this app's code.
- `tests/test_schedule.py::test_simultaneous_manual_and_scheduled_runs` - a timing-dependent
  concurrency test (two threads racing a 0.3s sleep); flaky on both branches (failed 1 of 3 runs on
  `origin/claude/railway-deployment` directly, in an unmodified checkout).
- `tests/test_telegram_digest.py::test_digest_includes_recent_engineering_changes_with_a_link` - a
  real-calendar Friday/weekend-gap edge case in the test's own `next_digest_tick_date()` helper,
  triggered because the sandbox's real date is Friday 2026-11-27; reproduces identically on the base
  branch.

## Remaining phases

None. All 6 phases of this redesign are complete. One pre-existing, out-of-scope item remains noted
above (the unredacted `/api/projects/:id/history` route) - not part of this redesign's brief, not
reachable through the UI, and not changed by this branch; flagged for awareness, not fixed here.
