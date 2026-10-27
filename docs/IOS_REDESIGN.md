# HM Algo 2.0 — iOS redesign notes (phone + tablet)

**Date:** 2026-09-22
**Scope:** Redesign the phone (≤599 px) and tablet (600–1024 px) experience to
feel like a native iOS app, following Apple's HIG. Desktop (≥1025 px) is
deliberately **unchanged**.

## Breakpoints

| Range | Mode | Pattern |
|---|---|---|
| ≤599 px | Phone | nav bar + large title, bottom tab bar, modal sheets |
| 600–1024 px | Tablet | persistent sidebar + split view (UIBackgroundConfiguration-style liquid glass) |
| ≥1025 px | Desktop | unchanged — the multi-column terminal layout already ships |

The previous "phone" breakpoint was 767 px; the new 599 / 1024 split is the
standard iOS / iPadOS pair.

## Phone (≤599 px) — key changes

- **App-bar nav (top):** the rail becomes a 3-row grid — drawer-trigger + brand
  + session row, an account-strip row, then a 34 pt large title (`Trade`,
  `News`, ...) under the nav bar. Honours `env(safe-area-inset-top)` and keeps
  the liquid-glass sheen at full blur.
- **Bottom tab bar (UITabBar):** `.tt-tabs` is re-parented onto `<body>` (the
  rail's `backdrop-filter` would otherwise make it the containing block and
  pin the bar under the nav bar). 49 pt tall + safe-area inset, six
  destinations with SF Symbols-style inline SVGs and labels.
- **Pane selector:** the Trade view's three panes are still switchable via a
  segmented control in the content area (Watchlist / Chart / Ticket); the view
  grid becomes single-column and hides the other two panes.
- **Modal sheets:** the drawer becomes a bottom sheet (20 pt top radius,
  grabber, slide-in 340 ms / `cubic-bezier(0.32, 0.72, 0, 1)`). The auth
  overlay does the same.
- **Redundant chrome removed on phones:** the markets dropdown, the drawer's
  duplicate Views list, and the desktop account/auth chip are hidden — the tab
  bar reaches every view in one tap; the drawer keeps Panels, Markets and
  the account.
- **Generous spacing + Dynamic Type:** all spacing on a 4 pt grid; type scale
  in `rem` so the browser text-size setting scales the UI.
- **44 pt minimum touch targets:** buttons, segmented buttons, selects,
  position tabs and the drawer trigger are all ≥44 pt tall; the layout
  verifier enforces this on a coarse pointer.
- **Floor on body height:** `.tt-panel__body { min-height: 96px }` — without
  it the news-filter toolbar (~132 px) and the chart head could squeeze a
  list down to 24 px, smaller than a 37 px control inside it, and the
  verifier flags it as silently clipped content.

## Tablet (600–1024 px) — key changes

- **Sidebar (left, 236 pt):** the rail becomes a vertical flex column. The
  same `.tt-tab` DOM as the phone tab bar becomes the sidebar nav — one state
  machine, no duplicated destinations. The Markets dropdown stays inline.
  Account metrics, broker-online chip, clock and admin/logout sit at the
  bottom of the sidebar.
- **Split view (right):** every view shows all its panels at once (the phone
  "one pane at a time" rule does not apply). Trade uses a two-column split
  (watchlist + chart on top, ticket full-width below) so the order ticket is
  never letter-boxed.
- **Pane bar hidden:** redundant when every pane is visible.
- **Drawer hidden:** the sidebar already surfaces every destination.
- **Status footer retained:** the terminal status line is still useful on a
  tablet.

## Desktop (≥1025 px) — unchanged

Three-column Trade, full charts, horizontal text tab strip. All desktop rules
live outside every media query in the new stylesheet, so no rule on a small
screen can leak into ≥1025 px. The icons added to `.tt-tab` buttons default to
`display: none` and only appear inside the ≤1024 px blocks.

## Files

- `jarvis/ui/static/css/ios_mobile.css` — new responsive layer (tokens,
  primitives, phone, tablet, accessibility). Loaded last so it wins at equal
  specificity. Organised by breakpoint.
- `jarvis/ui/templates/dashboard.html` — tab buttons now carry
  `<svg class="tt-tab__ico">` + `<span class="tt-tab__label">`. The drawer's
  "Views" section is tagged `tt-drawer__only-views` so phones can hide the
  duplicate.
- `jarvis/ui/static/js/dashboard.js` — `syncTabBarHost()` uses
  `'(max-width: 599px)'`; the tab bar is only re-parented onto `<body>` on a
  phone.
- `jarvis/ui/static/css/theme_terminal.css` — four `@media (max-width: 767px)`
  blocks (rail, pane bar, drawer trigger, single-pane rule) re-scoped to
  `599 px`, with an explanatory comment.

## Accessibility

- `color-scheme: light dark` honours the OS appearance even though the page
  meta pins "dark".
- `text-size-adjust: 100%` lets Dynamic Type scale the rem-based type.
- `prefers-reduced-motion: reduce` drops the sheet slide and view transition.
- `prefers-contrast: more` paints panel borders with `CanvasText`.
- Focus-visible 3 px blue ring on every interactive element.

## Verification (all green)

| Harness | Result |
|---|---|
| `tools/verify_ui_layout.js` | 238 / 238 PASS |
| `tools/verify_dashboard_render.js` | 209 / 209 PASS |
| `tools/verify_dashboard_nav.js` | 31 / 31 PASS |
| `tools/verify_phone_nav.js` | 13 / 13 PASS |
| `tools/verify_copilot_render.js` | 23 / 23 PASS |
| `tools/verify_terminal_render.js` | 55 / 55 PASS |
| full pytest | green (no Python code touched) |

The layout harness proves, per page per viewport, that no page scrolls
sideways, every primary control is ≥44 pt tall, and `.tt-rail` / `.tt-panel`
keep both `backdrop-filter` and a gradient background-image — the same
invariants the verifier enforced before this change.