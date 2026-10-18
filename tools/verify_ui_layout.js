/* ===========================================================================
   Layout / responsive verifier for the HM Algo 2.0 UI (tools/verify_ui_layout.js)

   WHY THIS EXISTS
   ---------------
   verify_dashboard_render.js proves the controller issues the right drawing
   calls, and verify_dashboard_nav.js proves real clicks work. Neither can see
   LAYOUT: whether a page scrolls sideways on a phone, whether a control is too
   small to tap, or whether a CSS effect silently failed to apply. Those are the
   defects a user notices first and the ones no other tool here can catch.

   This drives the real Chrome that is installed on the box through
   `puppeteer-core` (managed node workspace) and checks, per page per viewport:

     1. HORIZONTAL OVERFLOW - documentElement.scrollWidth vs clientWidth.
        A page that scrolls sideways on a phone is broken even if every element
        is individually correct, because one oversized child widens the document
        and every sibling inherits the width.
     2. TAP TARGET SIZE - primary nav/action controls must be >= 44px tall on a
        coarse pointer (WCAG 2.5.5). Measured, not assumed from CSS.
     3. GLASS ACTUALLY APPLIED - computed backdrop-filter on the glass surfaces
        must not be `none`. A `@supports` block or a source-order mistake can
        drop the effect with no error anywhere; this is the only way to notice.
     4. CONSOLE / NETWORK ERRORS - with an explicit allowlist, so a new error is
        a failure rather than noise.

   Run:  node tools/verify_ui_layout.js
         node tools/verify_ui_layout.js --shots .scratch/shots   (also write PNGs)
   Exits non-zero if any check fails.

   Requires the server to be up on 127.0.0.1:8501 (HM_dashboard.bat).
   =========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');

/* puppeteer-core is NOT vendored in this repo (there is no package.json). It lives in the
   managed node workspace. Requiring it bare only works when NODE_PATH happens to be set, so
   resolve it explicitly and fail with an actionable message rather than a MODULE_NOT_FOUND
   stack that says nothing about what to install or where. */
const puppeteer = (function () {
  const candidates = [
    'puppeteer-core',
    path.join(process.env.WORKBUDDY_NODE_WORKSPACE || '', 'node_modules', 'puppeteer-core'),
    'C:/Users/Itrai/.workbuddy-ai/binaries/node/workspace/node_modules/puppeteer-core',
  ].filter(Boolean);
  for (const c of candidates) {
    try { return require(c); } catch (e) { /* try the next candidate */ }
  }
  console.error(
    'verify_ui_layout: cannot load puppeteer-core.\n' +
    '  Install it into the managed node workspace, or point WORKBUDDY_NODE_WORKSPACE at it:\n' +
    '    cd "C:/Users/Itrai/.workbuddy-ai/binaries/node/workspace" && npm install puppeteer-core\n' +
    '  Tried:\n' + candidates.map(c => '    ' + c).join('\n'));
  process.exit(2);
})();

const BASE = process.env.JARVIS_BASE || 'http://127.0.0.1:8501';
const CHROME = process.env.JARVIS_CHROME ||
  'C:/Program Files/Google/Chrome/Application/chrome.exe';

const shotsIdx = process.argv.indexOf('--shots');
const SHOTS_DIR = shotsIdx !== -1 ? process.argv[shotsIdx + 1] : null;

const VIEWPORTS = [
  { name: 'phone-360', width: 360, height: 780, mobile: true },
  { name: 'phone-390', width: 390, height: 844, mobile: true },
  { name: 'tablet-768', width: 768, height: 1024, mobile: true },
  /* The common 1366x768 laptop. The navbar's natural single-row width is
     ~1580px, so this viewport is where the account strip used to wrap inside
     the fixed-height rail and spill above the viewport - a break the
     1440-wide desktop run never saw. */
  { name: 'laptop-1366', width: 1366, height: 768, mobile: false },
  { name: 'desktop-1440', width: 1440, height: 900, mobile: false },
];

/* Dashboard tabs are separate layouts, not just separate data - each has its
   own grid-template-columns, so each can overflow independently. */
/* `hud` names the page's own chrome bar. The market/console pages have no
   .tt-app shell, so GLASS_SELECTORS below never matched them and their glass
   was asserted nowhere - the four pages most recently given the treatment were
   the four pages whose treatment was unmeasured. */
/* The `hud` selector must be the bar the page actually renders. The old
   `.stocks-hud` / `.india-hud` / `.opt-hud` / `.cx-topbar` names were the
   pre-Bugatti headers; every market page and the console now renders the same
   `.bugatti-navbar` chrome, so the old selectors matched nothing and those
   checks could only ever fail. */
const PAGES = [
  { name: 'dashboard', url: '/dashboard', views: ['trade', 'news', 'analyst', 'markets', 'analytics', 'backtest'] },
  { name: 'forex', url: '/' },
  { name: 'stocks', url: '/stocks', hud: '.bugatti-navbar' },
  { name: 'india', url: '/india', hud: '.bugatti-navbar' },
  { name: 'options', url: '/options', hud: '.bugatti-navbar' },
  { name: 'console', url: '/console', hud: '.bugatti-navbar' },
  { name: 'positions', url: '/positions', hud: '.bugatti-navbar' },
];

/* Controls WCAG 2.5.5 applies to: the primary way in and out of every view.
   Elements with no box (display:none, or a closed drawer) are skipped by the
   probe below, so listing a control that is only visible in one state is
   correct - it is measured in the state where it is actually reachable. */
const TAP_SELECTORS = [
  '.tt-tab', '.tt-pane-bar__btn', '.mob-tab-btn',
  '.market-nav-item', '.tt-nav-trigger', '.btn-nav-switch',
  '.tt-drawer-trigger', '.tt-drawer__item',
  /* Market / console surfaces. These were previously unmeasured, which is how
     a 28px Logout button and a 26px underlying pill shipped. */
  '.auth-logout-btn', '.auth-login-btn', '.auth-user-pill',
  '.btn-buy-now-cta', '.und-pill', '.cx-iconbtn', '.cx-viewtab',
  '.cx-panelbar__btn', '.mobile-nav-bar button',
];

/* Surfaces that must carry the liquid-glass treatment. */
const GLASS_SELECTORS = ['.tt-rail', '.tt-panel'];

/* Errors that are expected on a UI-only server (no MT5 client attached) or are
   not defects. Anything not listed here fails the run - that is the point. */
const ERROR_ALLOWLIST = [
  /auto-selection.*503/i,
  /status of 503/i,
];

let checks = 0;
let failures = 0;

function ok(label, condition, detail) {
  checks++;
  if (condition) {
    console.log('  PASS  ' + label);
  } else {
    failures++;
    console.log('  FAIL  ' + label + (detail ? '\n          ' + detail : ''));
  }
}

function section(title) {
  console.log('\n' + title);
}

async function main() {
  if (!fs.existsSync(CHROME)) {
    console.error('Chrome not found at ' + CHROME + ' - set JARVIS_CHROME');
    process.exit(2);
  }
  if (SHOTS_DIR) fs.mkdirSync(SHOTS_DIR, { recursive: true });

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--force-color-profile=srgb'],
  });

  for (const vp of VIEWPORTS) {
    section(`viewport ${vp.name} (${vp.width}x${vp.height})`);

    for (const pageDef of PAGES) {
      const page = await browser.newPage();
      await page.setViewport({
        width: vp.width, height: vp.height,
        deviceScaleFactor: vp.mobile ? 2 : 1,
        isMobile: vp.mobile, hasTouch: vp.mobile,
      });

      const errors = [];
      page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
      page.on('pageerror', e => errors.push('PAGEERROR: ' + String(e.message)));
      page.on('requestfailed', r => errors.push('REQFAIL: ' + r.url()));

      let navigated = true;
      try {
        await page.goto(BASE + pageDef.url, { waitUntil: 'networkidle2', timeout: 45000 });
      } catch (e) {
        navigated = false;
        ok(`${pageDef.name} loads`, false, e.message.slice(0, 140));
      }
      if (!navigated) { await page.close(); continue; }
      await new Promise(r => setTimeout(r, 1200));

      const views = pageDef.views || [null];
      for (const view of views) {
        const label = view ? `${pageDef.name}/${view}` : pageDef.name;

        if (view) {
          const sel = `[data-view-btn="${view}"]`;
          if (await page.$(sel)) {
            await page.evaluate(s => document.querySelector(s).click(), sel);
            await new Promise(r => setTimeout(r, 1400));
          }
        }

        const probe = await page.evaluate((tapSel, glassSel, hudSel) => {
          const de = document.documentElement;
          const out = {
            scrollW: de.scrollWidth,
            clientW: de.clientWidth,
            tap: [],
            glass: [],
            hud: null,
          };

          /* The page's own chrome bar. Measured independently of
             GLASS_SELECTORS, which are dashboard-shell selectors and match
             nothing on the market/console pages. */
          if (hudSel) {
            const hud = document.querySelector(hudSel);
            if (hud && hud.getBoundingClientRect().width > 0) {
              const cs = getComputedStyle(hud);
              const bf = cs.backdropFilter || cs.webkitBackdropFilter || 'none';
              const bg = cs.backgroundColor || '';
              const m = /rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)/.exec(bg);
              out.hud = {
                sel: hudSel,
                backdrop: bf,
                bg,
                alpha: m && m[4] !== undefined ? parseFloat(m[4]) : (m ? 1 : 0),
                sheen: /inset/.test(cs.boxShadow || '') && /0\.5px/.test(cs.boxShadow || ''),
              };
            }
          }

          // ---- Chrome bar contains its children -------------------------------
          // The dashboard rail is a fixed-height app-shell grid row. The
          // account strip inside it carries flex-wrap:wrap, so below ~1580px
          // it wrapped to three lines, the rail refused to grow, and the
          // equity row rendered half-clipped ABOVE the viewport top. Nothing
          // measured vertical containment of the bar, so every run passed.
          {
            const bar = document.querySelector(hudSel || '.tt-rail');
            out.barOverflow = [];
            if (bar) {
              const oc = getComputedStyle(bar).overflowY;
              /* A scrolling bar (the tablet sidebar) legitimately holds more
                 content than its box; clipped bars are exactly the defect. */
              if (oc !== 'auto' && oc !== 'scroll') {
                const br = bar.getBoundingClientRect();
                for (const c of bar.children) {
                  if (c.hasAttribute('hidden')) continue;
                  const cr = c.getBoundingClientRect();
                  if (cr.width === 0 && cr.height === 0) continue;
                  if (cr.top < br.top - 1 || cr.bottom > br.bottom + 1) {
                    out.barOverflow.push(
                      (String(c.className).split(' ')[0] || c.tagName.toLowerCase()) +
                      ' [' + Math.round(cr.top) + ',' + Math.round(cr.bottom) + ']' +
                      ' vs bar [' + Math.round(br.top) + ',' + Math.round(br.bottom) + ']');
                  }
                }
              }
            }
          }

          // ---- Panel content containment (dashboard chart panel) ---------------
          // The chart panel is a fixed-height app-shell cell. The chart body
          // carried a fixed 400-460px height, so on 600-800px-tall desktop
          // windows the positions footer was pushed clean out of the panel
          // (clipped away by the panel's overflow:hidden) and the canvas
          // x-axis was sliced off at the panel edge. Nothing measured this.
          out.chartPanel = null;
          {
            const panel = document.querySelector('.tt-panel--chart');
            const pr = panel && panel.getBoundingClientRect();
            if (panel && pr.width > 0 && pr.height > 0) {
              const oc = getComputedStyle(panel).overflowY;
              const over = panel.scrollHeight - panel.clientHeight;
              const problems = [];
              /* A panel that is allowed to scroll (short-window fallback) can
                 hold more than its box; a clipped panel is the defect. */
              if (oc !== 'auto' && oc !== 'scroll' && over > 2) {
                problems.push('panel content overflows ' + over + 'px into overflow:hidden');
              }
              if (over > 2) panel.scrollTop = over;   // reachable via scroll
              const body = panel.querySelector('.tt-chart__body');
              const footer = panel.querySelector('.tt-panel__body--flush');
              const cv = panel.querySelector('.tt-chart canvas');
              if (body && cv) {
                const br = body.getBoundingClientRect();
                const cr = cv.getBoundingClientRect();
                if (cr.bottom > br.bottom + 1) {
                  problems.push('chart canvas x-axis clipped (canvas ' +
                    Math.round(cr.bottom) + ' > body ' + Math.round(br.bottom) + ')');
                }
              }
              if (footer) {
                const fr = footer.getBoundingClientRect();
                if (fr.bottom > pr.bottom + 1 || fr.top < pr.top - 1) {
                  problems.push('positions footer outside panel [' +
                    Math.round(fr.top) + ',' + Math.round(fr.bottom) + '] vs [' +
                    Math.round(pr.top) + ',' + Math.round(pr.bottom) + ']');
                }
              }
              panel.scrollTop = 0;
              out.chartPanel = problems;
            }
          }

          // Tap targets: only on a coarse pointer, and only real controls.
          const coarse = window.matchMedia('(pointer: coarse)').matches;
          if (coarse) {
            tapSel.forEach(s => {
              document.querySelectorAll(s).forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return;   // hidden
                out.tap.push({
                  sel: s,
                  label: (el.textContent || '').trim().slice(0, 18),
                  h: Math.round(r.height),
                });
              });
            });
          }

          glassSel.forEach(s => {
            document.querySelectorAll(s).forEach(el => {
              const cs = getComputedStyle(el);
              const bf = cs.backdropFilter || cs.webkitBackdropFilter || '';
              const bi = cs.backgroundImage || '';
              out.glass.push({
                sel: s,
                backdrop: bf,
                hasSheen: /gradient/i.test(bi),
              });
            });
          });

          // ---- App shell integrity (dashboard only) --------------------------
          // The dashboard is an app shell: the rail and the status bar are
          // pinned and the VIEW scrolls. If the DOCUMENT scrolls instead, the
          // shell height has become indefinite (min-height where height was
          // meant), the 1fr main row has resolved against CONTENT, and every
          // panel that was supposed to fill the screen is cut off at the fold.
          // That is a 1263px-tall view on a 900px screen, and nothing else in
          // this repo could see it.
          const appEl = document.querySelector('.tt-app');
          const mainEl = document.querySelector('.tt-main');
          const viewEl = document.querySelector('.tt-view[data-active="true"]');
          out.shell = appEl ? {
            docScrolls: de.scrollHeight > de.clientHeight + 1,
            docScrollH: de.scrollHeight,
            docClientH: de.clientHeight,
            mainH: mainEl ? Math.round(mainEl.getBoundingClientRect().height) : 0,
            viewH: viewEl ? Math.round(viewEl.getBoundingClientRect().height) : 0,
            viewId: viewEl ? viewEl.id : '(none)',
          } : null;

          // ---- Silent clipping inside a panel --------------------------------
          // A panel can clip its own content (overflow:hidden, or a flex child
          // that shrank below its content) while the document width stays
          // legal, so the overflow check above cannot see it. Three things are
          // NOT defects and are excluded, because a check that fires on them
          // stops being read:
          //   - text-overflow:ellipsis  designed truncation, the reader sees it
          //   - .tt-sr-only             visually-hidden screen-reader text
          //   - form controls           scroll their own value when focused
          //   - #chart / #chart-tv      lightweight-charts' own generated DOM
          //
          // VERTICAL is checked too, and `overflow:auto` counts. A flex child
          // carrying `min-height:0` can be squashed below a fraction of its own
          // content while the document width stays perfectly legal - the
          // trade-history filter toolbar sat at 38px of its 116px, so the
          // filters rendered as an unlabelled sliver of a scrollbar. `auto`
          // conceals that as completely as `hidden` does, which is why
          // restricting this to hidden/clip let it through.
          //
          // The vertical rule is deliberately narrow: a box that cannot show
          // even ONE of the controls it contains. Scrolling a list is normal -
          // #selection-body is 35px over 442px of rows on a phone and that is
          // accepted - but a control taller than its own clipping box can never
          // be shown in full at any scroll offset. Sizing a first child against
          // the box was tried first and flagged every one of those lists, which
          // is how a check stops being read.
          const tallestControl = (el) => {
            let h = 0;
            el.querySelectorAll('input,select,textarea,button,[role="button"]')
              .forEach((c) => {
                if (c.closest('.tt-sr-only')) return;
                const r = c.getBoundingClientRect();
                if (r.height > h) h = r.height;
              });
            return Math.ceil(h);
          };
          out.clip = [];
          if (viewEl) {
            for (const el of viewEl.querySelectorAll('*')) {
              const cs = getComputedStyle(el);
              if (cs.textOverflow === 'ellipsis') continue;
              if (el.closest('.tt-sr-only')) continue;
              if (el.closest('#chart, #chart-tv')) continue;
              // The radar dial masks a rotating sweep beam to a circle - the
              // clip IS the widget, like #chart's generated DOM above.
              if (el.closest('.radar-sweep-screen')) continue;
              if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) continue;
              if (el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1 &&
                  (cs.overflowX === 'hidden' || cs.overflowX === 'clip')) {
                out.clip.push({
                  tag: el.tagName.toLowerCase(),
                  id: el.id || '',
                  kind: 'h',
                  scroll: el.scrollWidth,
                  client: el.clientWidth,
                  need: el.clientWidth,
                });
                continue;
              }
              const oy = cs.overflowY;
              if (el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 1 &&
                  (oy === 'auto' || oy === 'scroll' ||
                   oy === 'hidden' || oy === 'clip')) {
                const need = tallestControl(el);
                if (need > 0 && el.clientHeight < need - 1) {
                  out.clip.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    kind: 'v',
                    scroll: el.scrollHeight,
                    client: el.clientHeight,
                    need,
                  });
                }
              }
            }
          }

          // ---- Silent clipping, page-wide -------------------------------------
          // The block above is scoped to the dashboard's active `.tt-view`. The
          // market and console pages have no `.tt-view`, so their clipped
          // content was never measured — a 219px `.buy-now-comp` inside a 150px
          // box on /stocks passed every run. This is the same rule applied to
          // the whole document, with the same three exclusions (ellipsis,
          // sr-only, lightweight-charts).
          out.clipAll = [];
          for (const el of document.body.querySelectorAll('*')) {
            const cs = getComputedStyle(el);
            if (cs.textOverflow === 'ellipsis') continue;
            // Visually-hidden text is 1x1 BY DESIGN, so its "overflow" is the
            // whole point. Three naming conventions exist in this repo.
            if (el.closest('.tt-sr-only, .sr-only, .cx-visually-hidden')) continue;
            if (el.closest('#chart, #chart-tv')) continue;
            if (el.closest('.radar-sweep-screen')) continue;
            if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) continue;
            // A horizontal clip only counts if the element cannot scroll to the
            // content itself (overflow auto/scroll is a working scroll box).
            if (el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 2 &&
                (cs.overflowX === 'hidden' || cs.overflowX === 'clip')) {
              out.clipAll.push({
                tag: el.tagName.toLowerCase(),
                id: el.id || '',
                cls: String(el.className || '').slice(0, 40),
                kind: 'h',
                scroll: el.scrollWidth,
                client: el.clientWidth,
                need: el.clientWidth,
              });
              continue;
            }
            const oy = cs.overflowY;
            if (el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 2 &&
                (oy === 'hidden' || oy === 'clip')) {
              const need = tallestControl(el);
              if (need > 0 && el.clientHeight < need - 1) {
                out.clipAll.push({
                  tag: el.tagName.toLowerCase(),
                  id: el.id || '',
                  cls: String(el.className || '').slice(0, 40),
                  kind: 'v',
                  scroll: el.scrollHeight,
                  client: el.clientHeight,
                  need,
                });
              }
            }
          }

          return out;
        }, TAP_SELECTORS, GLASS_SELECTORS, pageDef.hud || null);

        // 1. Horizontal overflow.
        const overflow = probe.scrollW - probe.clientW;
        ok(`${label}: no horizontal overflow`, overflow <= 2,
          `scrollWidth ${probe.scrollW} vs clientWidth ${probe.clientW} (+${overflow}px)`);

        // 2. Tap targets.
        const small = probe.tap.filter(t => t.h < 44);
        if (probe.tap.length) {
          ok(`${label}: ${probe.tap.length} controls >= 44px tall`, small.length === 0,
            small.map(t => `${t.sel} "${t.label}" ${t.h}px`).join(', '));
        }

        // 3. Glass applied (only meaningful where the surface exists).
        const badGlass = probe.glass.filter(g => !g.backdrop || g.backdrop === 'none');
        if (probe.glass.length) {
          ok(`${label}: ${probe.glass.length} glass surfaces have backdrop-filter`,
            badGlass.length === 0,
            badGlass.map(g => g.sel).join(', '));
          const noSheen = probe.glass.filter(g => !g.hasSheen);
          ok(`${label}: glass surfaces carry the sheen layer`, noSheen.length === 0,
            noSheen.map(g => g.sel).join(', '));
        }

        // 3b. The page's own chrome bar must be the glass material. The market
        //     and console pages have no app shell, so check 3 never covered them.
        //     Gated to the sheet's own range: ios_pages.css is deliberately inert
        //     above 1024px, so asserting its sheen at desktop-1440 would demand
        //     the desktop change - the opposite of the contract.
        if (pageDef.hud && vp.width <= 1024) {
          const hud = probe.hud;
          if (!hud) {
            ok(`${label}: chrome bar present`, false,
              `${pageDef.hud} not found or has no box`);
          } else {
            ok(`${label}: chrome bar is frosted (${hud.sel})`,
              hud.backdrop && hud.backdrop !== 'none',
              `backdrop-filter = ${hud.backdrop}`);
            ok(`${label}: chrome bar is translucent (${hud.sel})`,
              hud.alpha > 0 && hud.alpha < 1,
              `background ${hud.bg} (alpha ${hud.alpha}) - an opaque bar is not glass`);
            ok(`${label}: chrome bar carries the hairline sheen (${hud.sel})`,
              hud.sheen, 'no inset 0.5px highlight in box-shadow');
          }
        }

        // 3c. The chrome bar must contain its children vertically (probe note
        //     above: the account strip wrapped and spilled above the rail).
        if (probe.barOverflow) {
          ok(`${label}: chrome bar contains its children`,
            probe.barOverflow.length === 0,
            probe.barOverflow.slice(0, 3).join(', '));
        }

        // 3d. The chart panel must hold its own content: chart canvas inside
        //     the chart body, positions footer inside the panel (probe note
        //     above: the fixed chart height pushed the footer out entirely).
        if (probe.chartPanel) {
          ok(`${label}: chart panel holds its content`,
            probe.chartPanel.length === 0,
            probe.chartPanel.slice(0, 3).join(', '));
        }

        // 4. App shell integrity + silent clipping (dashboard only).
        if (probe.shell) {
          ok(`${label}: document does not scroll (app shell)`,
            !probe.shell.docScrolls,
            `documentElement ${probe.shell.docScrollH} > ${probe.shell.docClientH}px — ` +
            'the shell height is indefinite, so 1fr main resolved against content');
          ok(`${label}: view fits inside main`,
            probe.shell.viewH <= probe.shell.mainH + 1,
            `${probe.shell.viewId} ${probe.shell.viewH}px > main ${probe.shell.mainH}px — content past the fold`);

          const clips = probe.clip || [];
          ok(`${label}: no silently clipped content`, clips.length === 0,
            clips.slice(0, 3).map(c => c.kind === 'v'
              ? `${c.tag}${c.id ? '#' + c.id : ''} squashed to ${c.client}px, ` +
                `needs ${c.need}px (content ${c.scroll}px)`
              : `${c.tag}${c.id ? '#' + c.id : ''} ${c.scroll}>${c.client}`).join(', '));
        }

        // 5. Page-wide silent clipping (every page).
        {
          const clips = probe.clipAll || [];
          ok(`${label}: nothing clipped out of its own box`, clips.length === 0,
            clips.slice(0, 3).map(c => c.kind === 'v'
              ? `${c.tag}${c.id ? '#' + c.id : ''}${c.cls ? '.' + c.cls.split(' ')[0] : ''} ` +
                `squashed to ${c.client}px, needs ${c.need}px`
              : `${c.tag}${c.id ? '#' + c.id : ''}${c.cls ? '.' + c.cls.split(' ')[0] : ''} ` +
                `${c.scroll}>${c.client}`).join(', '));
        }

        if (SHOTS_DIR) {
          const f = path.join(SHOTS_DIR, `${vp.name}-${label.replace('/', '-')}.png`);
          await page.screenshot({ path: f });
        }
      }

      // 4. Console / network errors, minus the allowlist.
      const unexpected = [...new Set(errors)].filter(
        e => !ERROR_ALLOWLIST.some(re => re.test(e)));
      ok(`${pageDef.name}: no unexpected console/network errors`,
        unexpected.length === 0, unexpected.slice(0, 4).join(' | '));

      await page.close();
    }
  }

  await browser.close();

  console.log('\n' + '='.repeat(74));
  console.log(`${checks - failures}/${checks} checks passed`);
  if (failures) {
    console.log(`${failures} FAILED`);
    process.exit(1);
  }
  console.log('layout, tap targets and glass application all verified');
}

main().catch(e => { console.error('FAILED TO RUN:', e); process.exit(2); });
