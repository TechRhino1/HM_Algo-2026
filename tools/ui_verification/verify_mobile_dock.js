/**
 * Mobile dock regression test.
 *
 * Reproduces the real defect deterministically: the market pages render their
 * bottom dock server-side, so the buttons are tappable before the page
 * controller (which defines the onclick handler) has loaded. An early tap threw
 * "switchMobile<Page>View is not defined" and the dock did nothing.
 *
 * Method: intercept the controller script and HOLD it, so the dock is on screen
 * with no handler defined - the exact state a slow network produces. Tap the
 * dock, then assert the tab actually selected and nothing threw. Finally
 * release the controller and assert the tap was honoured rather than dropped
 * (the bootstrap replays it on registration).
 *
 * A page whose dock is inert during load fails here even though a warm-cache
 * run of a plain smoke test would pass.
 *
 * Usage: node tools/verify_mobile_dock.js
 */
const puppeteer = require('puppeteer-core');

const BASE = 'http://127.0.0.1:8501';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PHONE = { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true };

/* page -> { controller script to hold, dock button to tap, global it calls } */
const PAGES = [
  { name: 'stocks', url: '/stocks', controller: /\/static\/js\/stocks\.js/, tap: 'mob-btn-screener', global: 'switchMobileStocksView' },
  { name: 'india', url: '/india', controller: /\/static\/js\/india\.js/, tap: 'mob-btn-screener', global: 'switchMobileIndiaView' },
  { name: 'options', url: '/options', controller: /\/static\/js\/india_options\.js/, tap: 'mob-btn-spreads', global: 'switchMobileOptionsView' },
];

let checks = 0;
let failures = 0;
function ok(label, cond, detail) {
  checks++;
  if (!cond) failures++;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${!cond && detail ? '  -- ' + detail : ''}`);
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });

  for (const p of PAGES) {
    console.log(`\n=== ${p.name} (${p.url}) — controller held ===`);
    const page = await browser.newPage();
    await page.setViewport(PHONE);

    const errs = [];
    page.on('pageerror', e => errs.push(String(e.message).slice(0, 140)));

    /* Hold the controller until we say so. */
    let release;
    const gate = new Promise(r => { release = r; });
    let held = false;
    await page.setRequestInterception(true);
    page.on('request', async req => {
      if (p.controller.test(req.url())) {
        held = true;
        await gate;
        req.continue().catch(() => {});
        return;
      }
      req.continue().catch(() => {});
    });

    /* Do not await the navigation: 'load' cannot fire while the controller is
       held. The dock is above the controller in the markup, so it is parsed and
       painted before the parser blocks on the held script. */
    const nav = page.goto(BASE + p.url, { waitUntil: 'load', timeout: 60000 })
      .catch(() => {});
    await page.waitForSelector('#' + p.tap, { timeout: 15000 });
    ok(`${p.name}: dock rendered while the controller is still loading`, held);

    /* Tap a non-default tab with no handler defined yet. */
    const tapped = await page.evaluate((tapId, globalName) => {
      const btn = document.getElementById(tapId);
      const others = [...document.querySelectorAll('.mob-tab-btn')].filter(b => b !== btn);
      const definedBefore = typeof window[globalName] === 'function';
      btn.click();
      return {
        definedBefore,
        active: btn.classList.contains('active'),
        aria: btn.getAttribute('aria-selected'),
        othersActive: others.filter(b => b.classList.contains('active')).length,
      };
    }, p.tap, p.global);

    ok(`${p.name}: the dock entry point exists before the controller loads`,
      tapped.definedBefore, `${p.global} undefined at tap time`);
    ok(`${p.name}: early tap selects the tab (was a silent no-op)`,
      tapped.active && tapped.aria === 'true',
      `active=${tapped.active} aria-selected=${tapped.aria}`);
    ok(`${p.name}: early tap deselects its siblings`, tapped.othersActive === 0,
      `${tapped.othersActive} other tab(s) still active`);
    ok(`${p.name}: early tap throws nothing`, errs.length === 0, errs.join(' | '));

    /* Release the controller: the bootstrap must replay the tap. */
    release();
    await nav;
    await new Promise(r => setTimeout(r, 2500));
    const after = await page.evaluate((tapId, globalName) => {
      const btn = document.getElementById(tapId);
      return {
        registered: typeof window.registerMobileView === 'function',
        stillActive: btn.classList.contains('active'),
        implWired: typeof window[globalName] === 'function',
      };
    }, p.tap, p.global);

    ok(`${p.name}: controller registers with the bootstrap`, after.registered);
    ok(`${p.name}: the tap survives the controller loading`, after.stillActive,
      'the view requested before load was dropped');
    ok(`${p.name}: no errors after the controller loads`, errs.length === 0, errs.join(' | '));

    await page.close();
  }

  await browser.close();
  console.log('\n' + '='.repeat(74));
  console.log(failures === 0
    ? `${checks}/${checks} checks passed\nthe mobile dock works before its controller has loaded`
    : `${checks - failures}/${checks} checks passed\n${failures} FAILED`);
  process.exit(failures === 0 ? 0 : 1);
})().catch(e => { console.error(e); process.exit(1); });
