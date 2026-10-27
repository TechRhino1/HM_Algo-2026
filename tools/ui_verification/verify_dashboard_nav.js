/* ===========================================================================
   Live interaction check for the HM Algo 2.0 dashboard (tools/verify_dashboard_nav.js)

   WHY THIS EXISTS
   ---------------
   Every other check in this repo is blind to this class of bug. A syntax check
   passes, the render harness passes, and curl returns 200 with a perfectly good
   HTML body - yet clicking a single nav tab froze the renderer forever.

   The cause was a MutationObserver in hm_ui.js that watched
   `class`/`aria-selected` on the tab group and wrote `aria-selected` on those
   same tabs. setAttribute queues a mutation record even when the value is
   unchanged, so each pass queued the next and the microtask queue never
   drained: no exception, no console error, no recovery. Nothing that inspects
   source text or fetches a URL can see that. Only a real browser can.

   WHAT IT CHECKS
   --------------
   1. an idle page stays responsive - the control that rules out a poll loop;
   2. every view tab clicks through, the main thread keeps answering, and the
      view actually switched;
   3. the ARIA observer still mirrors a .active class toggle, so the freeze
      cannot be "fixed" by deleting the feature that caused it;
   4. the account id is readable on the strip and its panel is populated;
   5. the Indian and global market links are offered and reachable on screen.

   Run: node tools/verify_dashboard_nav.js
   Needs the server up:  python -m jarvis.ui.server   (127.0.0.1:8501)
   Exits 0 when all checks pass, 1 on a failed check, 2 when it cannot run.
   =========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const BASE = process.env.DASH_URL || 'http://127.0.0.1:8501/dashboard';
const VIEWS = ['trade', 'news', 'analyst', 'markets', 'analytics', 'backtest'];

let failures = 0;
let checks = 0;

function ok(label, condition, detail) {
  checks++;
  if (condition) {
    console.log('  PASS  ' + label);
  } else {
    failures++;
    console.log('  FAIL  ' + label + (detail ? '  -> ' + detail : ''));
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* puppeteer's evaluate() ignores its own `timeout` option, so a blocked
   renderer would hang this driver forever instead of failing. Every await that
   touches the page goes through a race with a timer. */
function deadline(p, ms, label) {
  let timer;
  return Promise.race([
    Promise.resolve(p).then(
      (v) => ({ ok: true, value: v }),
      (e) => ({ ok: false, error: `${e.name}: ${e.message}` })
    ),
    new Promise((r) => { timer = setTimeout(() => r({ ok: false, error: `DEADLINE ${ms}ms (${label})` }), ms); }),
  ]).finally(() => clearTimeout(timer));
}

/** Round-trip ms, or null when the main thread did not answer in time. */
async function probe(page, ms) {
  const t0 = Date.now();
  const r = await deadline(page.evaluate(() => 1), ms, 'probe');
  return r.ok ? Date.now() - t0 : null;
}

function loadPuppeteer() {
  /* puppeteer-core is not vendored in this repo (there is no package.json). On
     this box it lives in the managed node workspace, so try that before giving
     up - otherwise every run needs PUPPETEER_ROOT exported by hand, and a tool
     that only works when you remember an env var is a tool that stops being
     run. Keep this list in step with tools/verify_ui_layout.js. */
  const candidates = ['puppeteer-core'];
  const managed = process.env.PUPPETEER_ROOT;
  if (managed) candidates.push(path.join(managed, 'puppeteer-core'));
  if (process.env.WORKBUDDY_NODE_WORKSPACE) {
    candidates.push(path.join(process.env.WORKBUDDY_NODE_WORKSPACE,
      'node_modules', 'puppeteer-core'));
  }
  candidates.push('C:/Users/Itrai/.workbuddy-ai/binaries/node/workspace/node_modules/puppeteer-core');
  for (const c of candidates) {
    try { return require(c); } catch (e) { /* try the next one */ }
  }
  console.error(
    '\nCannot load puppeteer-core.\n' +
    '  Install it, or point PUPPETEER_ROOT at the node_modules that holds it:\n' +
    '    PUPPETEER_ROOT=/path/to/node_modules node tools/verify_dashboard_nav.js\n' +
    '  Tried:\n' + candidates.map(c => '    ' + c).join('\n') + '\n'
  );
  process.exit(2);
}

function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c; } catch (e) { /* keep looking */ }
  }
  console.error('\nNo Chrome or Edge binary found. Set CHROME_PATH to one.\n');
  process.exit(2);
}

async function serverIsUp() {
  const url = BASE.replace(/\/dashboard$/, '/');
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
    return res.status < 500;
  } catch (e) {
    return false;
  }
}

(async () => {
  const puppeteer = loadPuppeteer();
  const chrome = findChrome();

  if (!(await serverIsUp())) {
    console.error(
      `\nNo server answering at ${BASE}\n` +
      '  Start it first:  python -m jarvis.ui.server\n'
    );
    process.exit(2);
  }

  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--window-size=1600,900'],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 900 });

  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(`${e.name}: ${e.message}`));
  page.on('dialog', (d) => { pageErrors.push(`DIALOG ${d.type()}: ${d.message()}`); d.dismiss().catch(() => {}); });

  const nav = await deadline(page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 }), 35000, 'goto');
  if (!nav.ok) {
    console.error('\nCould not load ' + BASE + ': ' + nav.error + '\n');
    await browser.close();
    process.exit(2);
  }
  await sleep(4000);

  /* ---- 1. An idle page must stay responsive -----------------------------
     This is the control. If an idle page blocks, the freeze is a poll loop and
     the click is a red herring - a different bug with a different fix. */
  console.log('\nidle page (no interaction)');
  const idle = [];
  for (let i = 0; i < 5; i++) {
    await sleep(1000);
    idle.push(await probe(page, 3000));
  }
  ok('main thread answers while idle', idle.every((v) => v !== null),
     'round trips: ' + idle.map((v) => (v === null ? 'BLOCKED' : v + 'ms')).join(' '));

  /* ---- 2. Every view must be clickable and must actually switch ---------- */
  console.log('\nview navigation');
  for (const view of VIEWS) {
    const before = await probe(page, 3000);
    const click = await deadline(page.click(`[data-view-btn="${view}"]`), 12000, 'click');
    await sleep(1200);
    const after = await probe(page, 3000);

    const state = await deadline(
      page.evaluate(() => {
        const v = document.body.getAttribute('data-view');
        const sec = document.getElementById('view-' + v);
        return {
          view: v,
          activePanels: document.querySelectorAll('[data-view-panel][data-active="true"]').length,
          nodes: sec ? sec.querySelectorAll('*').length : -1,
        };
      }),
      5000,
      'state'
    );

    if (before === null || after === null) {
      ok(`${view}: page stays responsive`, false,
         `before=${before === null ? 'BLOCKED' : before + 'ms'} after=${after === null ? 'BLOCKED' : after + 'ms'}`);
      console.log('  main thread blocked - stopping, later views would all report the same');
      break;
    }

    ok(`${view}: page stays responsive`, true);
    ok(`${view}: click switches the view`,
       click.ok && state.ok && state.value.view === view && state.value.activePanels === 1 && state.value.nodes > 0,
       click.ok ? (state.ok ? JSON.stringify(state.value) : state.error) : click.error);
  }

  /* ---- 3. The ARIA observer must still do its job -----------------------
     Guards the fix itself: the freeze is gone if the observer is deleted, but
     so is the accessibility behaviour. Toggle only the `.active` class, which
     the page scripts own, and the observer is the only thing that can mirror it
     into aria-selected. */
  console.log('\nARIA sync is still live');
  const aria = await deadline(
    page.evaluate(async () => {
      const group = document.querySelector('[data-hm-tabs="done"]');
      if (!group) return { skipped: 'no enhanced tab group on this page' };
      const tabs = Array.prototype.slice.call(group.querySelectorAll('button, [role="tab"]'));
      if (tabs.length < 2) return { skipped: 'group has fewer than 2 tabs' };

      tabs.forEach((t) => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
      await new Promise((r) => setTimeout(r, 50));

      const target = tabs[1];
      target.classList.add('active');          // the only thing a page script does
      await new Promise((r) => setTimeout(r, 150));

      return {
        mirrored: target.getAttribute('aria-selected'),
        tabindex: target.getAttribute('tabindex'),
        othersFalse: tabs.filter((t) => t !== target)
                          .every((t) => t.getAttribute('aria-selected') === 'false'),
      };
    }),
    5000,
    'aria'
  );

  if (!aria.ok) {
    ok('aria sync reachable', false, aria.error);
  } else if (aria.value.skipped) {
    console.log('  SKIP  ' + aria.value.skipped);
  } else {
    ok('a class toggle is mirrored into aria-selected', aria.value.mirrored === 'true',
       'aria-selected=' + aria.value.mirrored);
    ok('the active tab is the only one in the tab order', aria.value.tabindex === '0',
       'tabindex=' + aria.value.tabindex);
    ok('inactive tabs are marked not-selected', aria.value.othersFalse === true);
  }

  /* ---- 4. Account identity: on the strip, full details one click away -----
     The strip shows equity / P&L / margin / risk but never said WHICH account
     those belong to. The login now sits on the strip with the rest of the
     snapshot behind it, so this checks both halves: the id is readable without
     interacting, and the panel it opens is populated rather than placeholder. */
  console.log('\naccount identity');
  const acct = await deadline(
    page.evaluate(() => {
      const t = document.getElementById('acc-id-trigger');
      const panel = document.getElementById('acc-details');
      if (!t || !panel) return { missing: 'trigger or panel absent from the template' };
      return {
        triggerText: (t.textContent || '').trim(),
        expanded: t.getAttribute('aria-expanded'),
        panelHidden: panel.hasAttribute('hidden'),
      };
    }),
    5000,
    'account'
  );

  if (!acct.ok || acct.value.missing) {
    ok('the account id is reachable', false, acct.ok ? acct.value.missing : acct.error);
  } else {
    ok('the account id is shown on the strip', /^\d+$/.test(acct.value.triggerText),
       'trigger reads ' + JSON.stringify(acct.value.triggerText));
    ok('the details panel starts closed',
       acct.value.panelHidden === true && acct.value.expanded === 'false');

    await deadline(page.click('#acc-id-trigger'), 8000, 'click-account');
    await sleep(300);

    const opened = await deadline(
      page.evaluate(() => {
        const panel = document.getElementById('acc-details');
        const rows = {};
        Array.prototype.forEach.call(panel.querySelectorAll('dt'), (dt) => {
          const dd = dt.nextElementSibling;
          if (dd) rows[dt.textContent.trim()] = (dd.textContent || '').trim();
        });
        return {
          hidden: panel.hasAttribute('hidden'),
          visible: panel.getBoundingClientRect().height > 0,
          expanded: document.getElementById('acc-id-trigger').getAttribute('aria-expanded'),
          rows: rows,
        };
      }),
      5000,
      'account-open'
    );

    ok('clicking the id opens the panel',
       opened.ok && opened.value.hidden === false && opened.value.visible === true &&
       opened.value.expanded === 'true',
       opened.ok ? JSON.stringify(opened.value) : opened.error);

    if (opened.ok) {
      const EXPECTED = ['Login', 'Name', 'Server', 'Company', 'Balance', 'Equity',
                        'Free margin', 'Margin level', 'Leverage', 'Trading', 'Last sync'];
      const rows = opened.value.rows;
      const missingRows = EXPECTED.filter((k) => !(k in rows));
      ok('the panel lists every account field', missingRows.length === 0,
         'missing: ' + missingRows.join(', '));

      ok('the panel login matches the strip', rows.Login === acct.value.triggerText,
         'panel=' + JSON.stringify(rows.Login) + ' strip=' + JSON.stringify(acct.value.triggerText));

      // A panel of dashes would pass every structural check above while telling
      // the trader nothing, so the values themselves are asserted.
      const blank = EXPECTED.filter((k) => rows[k] === '—');
      ok('every field carries a value, not a placeholder', blank.length === 0,
         'still dashes: ' + blank.join(', ') + ' (is the broker reporting?)');
    }

    await deadline(page.mouse.click(5, 700), 8000, 'click-away');
    await sleep(300);
    const closed = await deadline(
      page.evaluate(() => document.getElementById('acc-details').hasAttribute('hidden')),
      5000, 'account-close'
    );
    ok('clicking away closes the panel', closed.ok && closed.value === true,
       closed.ok ? String(closed.value) : closed.error);

    await deadline(page.click('#acc-id-trigger'), 8000, 'click-account-again');
    await sleep(200);
    await page.keyboard.press('Escape');
    await sleep(300);
    const escaped = await deadline(
      page.evaluate(() => document.getElementById('acc-details').hasAttribute('hidden')),
      5000, 'account-escape'
    );
    ok('Escape closes the panel', escaped.ok && escaped.value === true,
       escaped.ok ? String(escaped.value) : escaped.error);
  }

  /* ---- 5. Market links: the Indian and global pages must be reachable -----
     The dashboard was the only page without a route to the market sections; the
     other four carry a .nav-links-wrapper row. The rail has no space for a row,
     so the same four destinations live in a dropdown here. Both the hrefs and
     the fact that the panel is not clipped off-screen are checked. */
  console.log('\nmarket links');
  const markets = await deadline(
    page.evaluate(() => {
      const t = document.getElementById('markets-trigger');
      const panel = document.getElementById('markets-menu');
      if (!t || !panel) return { missing: 'trigger or panel absent from the template' };
      return {
        hidden: panel.hasAttribute('hidden'),
        expanded: t.getAttribute('aria-expanded'),
        hrefs: Array.prototype.map.call(panel.querySelectorAll('a[href]'),
                                       (a) => a.getAttribute('href')),
      };
    }),
    5000,
    'markets'
  );

  if (!markets.ok || markets.value.missing) {
    ok('the market links are reachable', false, markets.ok ? markets.value.missing : markets.error);
  } else {
    const WANTED = ['/', '/stocks', '/india', '/options'];
    const absent = WANTED.filter((h) => markets.value.hrefs.indexOf(h) === -1);
    ok('the market links start closed', markets.value.hidden === true);
    ok('the Indian market link is offered', markets.value.hrefs.indexOf('/india') !== -1,
       'hrefs: ' + markets.value.hrefs.join(' '));
    ok('the global (US) market link is offered', markets.value.hrefs.indexOf('/stocks') !== -1,
       'hrefs: ' + markets.value.hrefs.join(' '));
    ok('all four destinations are offered, matching the other pages', absent.length === 0,
       'absent: ' + absent.join(', '));

    await deadline(page.click('#markets-trigger'), 8000, 'click-markets');
    await sleep(300);

    const box = await deadline(
      page.evaluate(() => {
        const panel = document.getElementById('markets-menu');
        const r = panel.getBoundingClientRect();
        return {
          hidden: panel.hasAttribute('hidden'),
          expanded: document.getElementById('markets-trigger').getAttribute('aria-expanded'),
          top: Math.round(r.top), left: Math.round(r.left),
          right: Math.round(r.right), bottom: Math.round(r.bottom),
          vw: window.innerWidth, vh: window.innerHeight,
        };
      }),
      5000,
      'markets-open'
    );

    ok('clicking Markets opens the links',
       box.ok && box.value.hidden === false && box.value.expanded === 'true',
       box.ok ? JSON.stringify(box.value) : box.error);

    // The rail is sticky with a stacking context; a panel that opens behind it or
    // past the viewport edge is present but unreachable.
    ok('the open panel sits fully on screen',
       box.ok && box.value.left >= 0 && box.value.right <= box.value.vw &&
       box.value.top >= 0 && box.value.bottom <= box.value.vh,
       box.ok ? JSON.stringify(box.value) : box.error);

    await page.keyboard.press('Escape');
    await sleep(200);
  }

  ok('no uncaught page errors', pageErrors.length === 0, pageErrors.join(' | '));

  await browser.close();

  console.log(`\n${checks - failures}/${checks} checks passed`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((err) => {
  console.error('driver failed:', err);
  process.exit(2);
});
