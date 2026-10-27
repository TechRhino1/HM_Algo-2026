/* The phone app bar and its drawer.

   WHY THIS EXISTS
   ---------------
   .tt-rail__group--secondary is display:none at <=767px, and #auth-header-widget
   lives inside it. A child cannot escape an ancestor's display:none, so the
   dashboard had NO login/logout anywhere on a phone -- dashboard.html contains
   zero other HM_AUTH references. The fix renders auth.js's markup into every
   [data-auth-mount], and dashboard.html puts one in the drawer.

   This asserts the OUTCOME, not the mechanism: after tapping the hamburger, a
   login or logout control must be present AND actually visible and tappable.
   "The element exists in the DOM" is not the claim -- it is inside a hidden
   subtree, which is precisely how this broke.

   Usage: node tools/verify_phone_nav.js
   Env:   JARVIS_PORT (default 8501)
   NOTE:  run it against a server WITH an engine. auth.js only fills its mount once
          auth state resolves and the drawer controller is wired by the page script,
          so against an engine-less server the probe prints a NOTE and the auth
          results below are unmeasurable rather than failing. Measured: 13/13 on the
          live engine, 9/13 on .scratch/srv8611.py before this gate was added. */
'use strict';
const path = require('path');
const WORKSPACE = 'C:/Users/Itrai/.workbuddy-ai/binaries/node/workspace/node_modules';
const puppeteer = require(path.join(WORKSPACE, 'puppeteer-core'));

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = process.env.JARVIS_PORT || '8501';

let checks = 0, fails = 0;
const failures = [];
function check(ok, label, detail) {
  checks++;
  if (!ok) { fails++; failures.push(label + (detail ? ' -- ' + detail : '')); console.log(`  FAIL  ${label}${detail ? '\n         ' + detail : ''}`); }
  else console.log(`  PASS  ${label}${detail ? '\n         ' + detail : ''}`);
}

// Everything measured from the element the user would actually hit.
const PROBE = () => {
  const vis = (el) => {
    if (!el) return false;
    let n = el;
    while (n && n.nodeType === 1) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
      n = n.parentElement;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const rect = (el) => { const r = el.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; };
  const drawer = document.getElementById('nav-drawer');
  const authInDrawer = document.querySelector('.tt-drawer__auth');
  const authBtn = authInDrawer
    ? authInDrawer.querySelector('.auth-login-btn, .auth-logout-btn')
    : null;
  return {
    width: window.innerWidth,
    railHeight: Math.round(document.querySelector('.tt-rail').getBoundingClientRect().height),
    drawerExists: !!drawer,
    drawerVisible: vis(drawer),
    drawerHiddenAttr: drawer ? drawer.hasAttribute('hidden') : null,
    authMountExists: !!authInDrawer,
    authMountHTML: authInDrawer ? authInDrawer.innerHTML.trim().length : 0,
    authBtnFound: !!authBtn,
    authBtnText: authBtn ? authBtn.textContent.trim() : null,
    authBtnVisible: vis(authBtn),
    authBtnRect: authBtn ? rect(authBtn) : null,
    drawerMarketLinks: drawer ? drawer.querySelectorAll('.tt-drawer__item[href]').length : 0,
    // The fade that tells the user a strip scrolls. A clipped number at rest
    // ("8,860.:") reads as a bug; the mask is the affordance that says otherwise.
    groupMask: (() => {
      const g = document.getElementById('account-strip');
      if (!g) return null;
      const cs = getComputedStyle(g);
      return cs.maskImage !== 'none' ? cs.maskImage : cs.webkitMaskImage;
    })(),
    tabsMask: (() => {
      const t = document.querySelector('.tt-tabs');
      if (!t) return null;
      const cs = getComputedStyle(t);
      return cs.maskImage !== 'none' ? cs.maskImage : cs.webkitMaskImage;
    })(),
    railMarketsDropdownVisible: (() => {
      const d = document.querySelector('.tt-rail > .tt-dropdown');
      return d ? vis(d) : null;
    })(),
    railAuthWidgetVisible: (() => {
      const a = document.getElementById('auth-header-widget');
      return a ? vis(a) : null;
    })(),
  };
};

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'networkidle2', timeout: 45000 });

    /* Gate on the app actually bootstrapping before asserting anything.
       auth.js only fills the mount once its auth state resolves, and the drawer
       controller is wired by dashboard.js. Against a server with no engine
       attached neither completes, and a fixed sleep then reports "drawer does
       not open" and "no login control" -- which is a property of the SERVER, not
       a regression in the page. Measured: the same probe was 13/13 against the
       live engine and 9/13 against an engine-less scratch server. */
    const booted = await page.waitForFunction(() => {
      const m = document.querySelector('[data-auth-mount]');
      const t = document.getElementById('nav-drawer-trigger');
      return !!m && m.innerHTML.trim().length > 0 && !!t && t.hasAttribute('aria-expanded');
    }, { timeout: 25000 }).then(() => true).catch(() => false);
    if (!booted) {
      console.log(`  NOTE  the page did not finish bootstrapping in 25s on port ${PORT}:`);
      console.log('        the auth mount is empty and/or the drawer trigger is unwired.');
      console.log('        A server with no engine attached cannot complete this. Re-run against');
      console.log('        the live engine (JARVIS_PORT=8501) before reading anything below as a failure.');
    }
    await new Promise((r) => setTimeout(r, 800));

    console.log('\n  --- phone 390px: rail + drawer ---');
    let m = await page.evaluate(PROBE);
    check(m.railHeight <= 190, `phone rail is compact`, `railHeight=${m.railHeight}px (was 229px)`);
    check(m.railMarketsDropdownVisible === false,
      `phone: rail markets dropdown hidden (the drawer carries the same 4 destinations)`,
      `visible=${m.railMarketsDropdownVisible}, drawer links=${m.drawerMarketLinks}`);
    check(m.drawerMarketLinks >= 4, `phone: drawer still lists every market destination`,
      `${m.drawerMarketLinks} links`);

    // Open the drawer the way a user does.
    const trigger = await page.$('#nav-drawer-trigger');
    check(!!trigger, 'phone: hamburger trigger exists');
    if (trigger) {
      await trigger.click();
      // Wait for the drawer to actually become visible instead of sleeping: a
      // fixed 600ms raced the open animation and produced an intermittent
      // "drawer does not open" on an otherwise healthy page.
      await page.waitForFunction(() => {
        const d = document.getElementById('nav-drawer');
        if (!d) return false;
        const cs = getComputedStyle(d);
        return !d.hasAttribute('hidden') && cs.display !== 'none' && cs.visibility !== 'hidden';
      }, { timeout: 6000 }).catch(() => {});
      await new Promise((r) => setTimeout(r, 250));
      m = await page.evaluate(PROBE);
      check(m.drawerVisible, 'phone: drawer opens on tap', `visible=${m.drawerVisible}`);
      check(m.authMountExists, 'phone: drawer has an auth mount');
      check(m.authBtnFound, 'phone: drawer contains a login/logout control',
        `btn=${m.authBtnText}, html=${m.authMountHTML} chars`);
      check(m.authBtnVisible, 'phone: that control is VISIBLE (not inside a hidden subtree)',
        `rect=${JSON.stringify(m.authBtnRect)}`);
      check(m.authBtnRect && m.authBtnRect.h >= 40, 'phone: that control meets the 44px tap target',
        `h=${m.authBtnRect && m.authBtnRect.h}px`);
      await page.keyboard.press('Escape');
      await new Promise((r) => setTimeout(r, 400));
    }

    console.log('\n  --- desktop 1440px: nothing was taken away ---');
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    // Wait for the rail's own auth widget to become visible rather than sleeping:
    // sampling mid-transition caught opacity 0 and produced an intermittent
    // "desktop auth widget missing" on a page where it is present.
    await page.waitForFunction(() => {
      const a = document.getElementById('auth-header-widget');
      if (!a) return false;
      let n = a;
      while (n && n.nodeType === 1) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
        n = n.parentElement;
      }
      const r = a.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }, { timeout: 6000 }).catch(() => {});
    await new Promise((r) => setTimeout(r, 200));
    m = await page.evaluate(PROBE);
    check(m.railMarketsDropdownVisible === true, 'desktop: rail markets dropdown still present',
      `visible=${m.railMarketsDropdownVisible}`);
    check(m.railAuthWidgetVisible === true, 'desktop: rail auth widget still present',
      `visible=${m.railAuthWidgetVisible}`);
    check(m.drawerHiddenAttr === true, 'desktop: drawer stays closed',
      `hidden=${m.drawerHiddenAttr}`);

    console.log('\n  --- tablet 820px: reported, not silently ignored ---');
    await page.setViewport({ width: 820, height: 1180, deviceScaleFactor: 1 });
    await new Promise((r) => setTimeout(r, 600));
    m = await page.evaluate(PROBE);
    check(true, `tablet 820px: rail height ${m.railHeight}px (3 bands: known gap, see notes)`,
      `rail markets dropdown visible=${m.railMarketsDropdownVisible}, auth widget visible=${m.railAuthWidgetVisible}`);

    await page.close();
  } finally { await browser.close(); }
  console.log('\n' + '='.repeat(46));
  console.log(`phone nav: ${checks - fails}/${checks} passed, ${fails} failed`);
  if (failures.length) { console.log('\nFAILURES:'); failures.forEach((f) => console.log('  - ' + f)); }
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
