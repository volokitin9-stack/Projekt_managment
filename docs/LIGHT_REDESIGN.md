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

## Remaining phases (not yet done)

5. Personal task manager visual pass (mostly inherited from Phase 2/3's token and header changes -
   verify once real task data is on screen; the graph/tree logic itself is untouched).
6. Responsive pass beyond the three required desktop breakpoints (tablet/mobile).
7. Full regression pass + final screenshots under both roles.
