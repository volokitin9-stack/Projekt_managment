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

## Remaining phases (not yet done)

3. Engineering board structural touch-ups (detail panel → real tabs; unconfirmed-stage compact
   horizontal cards per §10.2).
4. Messages relabel (icon → mail glyph + "Сообщения" text, same `#notifBellWrap`/API underneath).
5. Personal task manager visual pass (mostly inherited from Phase 2's token swap already - verify
   once real task data is on screen).
6. Responsive pass (laptop/tablet/mobile).
7. Full regression pass + screenshots under both roles.
