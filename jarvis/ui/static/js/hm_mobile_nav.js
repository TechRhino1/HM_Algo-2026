/**
 * HM Algo 2.0 — PHONE BOTTOM TAB BAR
 *
 * The phone layouts used to carry four bands of top chrome (metrics strip,
 * large title, destination tab strip, pane segments) before any content.
 * This file replaces the destination strip with an iOS-style bottom tab bar
 * and moves the overflow behind a "More" sheet, so the top of every page
 * collapses to a single app bar.
 *
 * Scope and behaviour:
 *   - phone only (max-width: 599px); tears itself down when resized up;
 *   - /classic is frozen and never gets the bar;
 *   - on the dashboard (body.tt-app) "More" opens the existing navigation
 *     drawer, which already lists every view, market and tool plus auth;
 *   - on the other pages "More" opens a bottom sheet with the same link set;
 *   - adds `hm-has-tabbar` (bar present) and `hm-pad-tabbar` (page scrolls
 *     the document and needs bottom clearance) to <body>;
 *   - on the dashboard, the account strip collapses to a tappable one-row
 *     summary that expands to the full metric list (see ios_mobile.css).
 */
(function () {
  'use strict';

  if (location.pathname === '/classic') return;

  var PHONE = '(max-width: 599px)';
  var mq = window.matchMedia(PHONE);
  var isDashboard = document.body.classList.contains('tt-app');

  var TABS = [
    { id: 'cockpit',   ico: '⚡', label: 'Cockpit',   href: '/' },
    { id: 'symbols',   ico: '🎯', label: 'Symbols',   href: '/#symbols' },
    { id: 'markets',   ico: '🌐', label: 'Markets',   href: '/stocks' },
    { id: 'positions', ico: '📊', label: 'Positions', href: '/positions' },
    { id: 'more',      ico: '⋯', label: 'More',      more: true }
  ];

  var SHEET_GROUPS = [
    {
      title: 'Desks',
      items: [
        { ico: '🎯', label: 'Symbol Control', href: '/#symbols' },
        { ico: '🧠', label: 'Analyst',        href: '/#analyst' },
        { ico: '📰', label: 'News',           href: '/#news' },
        { ico: '📡', label: 'Market Scanner', href: '/#markets' },
        { ico: '📈', label: 'Analytics',      href: '/#analytics' },
        { ico: '🧪', label: 'Backtest',       href: '/#backtest' }
      ]
    },
    {
      title: 'Global Markets',
      items: [
        { ico: '🇺🇸', label: 'US Stocks Screener', href: '/stocks' },
        { ico: '🇮🇳', label: 'India Stocks (NSE/BSE)', href: '/india' },
        { ico: '📊', label: 'India Options (F&O)', href: '/options' }
      ]
    },
    {
      title: 'Tools',
      items: [
        { ico: '⚡', label: 'Cockpit (Forex/Crypto)', href: '/' },
        { ico: '📟', label: 'Console & Operations', href: '/console' },
        { ico: '📋', label: 'Live Positions Book', href: '/positions' }
      ]
    }
  ];

  var bar = null;
  var sheet = null;

  /* ── Active tab ─────────────────────────────────────────────────────── */

  function currentTab() {
    var p = location.pathname;
    var h = (location.hash || '').slice(1);
    if (p === '/stocks' || p === '/india' || p === '/options') return 'markets';
    if (p === '/positions') return 'positions';
    if (p === '/' || p === '/dashboard' || p === '/forex') {
      return h === 'symbols' ? 'symbols' : 'cockpit';
    }
    return '';
  }

  function updateActive() {
    if (!bar) return;
    var active = currentTab();
    Array.prototype.forEach.call(bar.querySelectorAll('[data-tab]'), function (tab) {
      var on = tab.getAttribute('data-tab') === active;
      if (on) tab.setAttribute('aria-current', 'page');
      else tab.removeAttribute('aria-current');
    });
  }

  /* ── Tab bar ────────────────────────────────────────────────────────── */

  function buildBar() {
    if (bar) return;
    bar = document.createElement('nav');
    bar.className = 'hm-tabbar';
    bar.setAttribute('aria-label', 'Primary');

    TABS.forEach(function (t) {
      var el;
      if (t.more) {
        el = document.createElement('button');
        el.type = 'button';
        el.setAttribute('aria-haspopup', 'dialog');
      } else {
        el = document.createElement('a');
        el.href = t.href;
      }
      el.className = 'hm-tabbar__tab';
      el.setAttribute('data-tab', t.id);

      var ico = document.createElement('span');
      ico.className = 'hm-tabbar__ico';
      ico.setAttribute('aria-hidden', 'true');
      ico.textContent = t.ico;

      var label = document.createElement('span');
      label.className = 'hm-tabbar__label';
      label.textContent = t.label;

      el.appendChild(ico);
      el.appendChild(label);
      if (t.more) {
        el.addEventListener('click', onMore);
      }
      bar.appendChild(el);
    });

    document.body.appendChild(bar);
    document.body.classList.add('hm-has-tabbar');

    /* Document-scrolling pages need clearance; the dashboard shell and the
       console already reserve their own bottom space. */
    if (!isDashboard && !document.querySelector('.cx-main')) {
      document.body.classList.add('hm-pad-tabbar');
    }

    updateActive();
  }

  function destroyBar() {
    if (!bar) return;
    bar.parentNode.removeChild(bar);
    bar = null;
    document.body.classList.remove('hm-has-tabbar');
    document.body.classList.remove('hm-pad-tabbar');
    closeSheet();
  }

  /* ── More sheet (non-dashboard pages) ───────────────────────────────── */

  function buildSheet() {
    if (sheet) return;
    sheet = document.createElement('div');
    sheet.className = 'hm-sheet';
    sheet.hidden = true;

    var scrim = document.createElement('div');
    scrim.className = 'hm-sheet__scrim';
    scrim.addEventListener('click', closeSheet);

    var panel = document.createElement('nav');
    panel.className = 'hm-sheet__panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-label', 'All destinations');

    var grabber = document.createElement('div');
    grabber.className = 'hm-sheet__grabber';
    grabber.setAttribute('aria-hidden', 'true');
    grabber.addEventListener('click', closeSheet);
    panel.appendChild(grabber);

    SHEET_GROUPS.forEach(function (group) {
      var head = document.createElement('p');
      head.className = 'hm-sheet__section';
      head.textContent = group.title;
      panel.appendChild(head);

      group.items.forEach(function (item) {
        var a = document.createElement('a');
        a.className = 'hm-sheet__item';
        a.href = item.href;

        var ico = document.createElement('span');
        ico.className = 'hm-sheet__ico';
        ico.setAttribute('aria-hidden', 'true');
        ico.textContent = item.ico;

        var label = document.createElement('span');
        label.textContent = item.label;

        a.appendChild(ico);
        a.appendChild(label);
        a.addEventListener('click', closeSheet);
        panel.appendChild(a);
      });
    });

    sheet.appendChild(scrim);
    sheet.appendChild(panel);
    document.body.appendChild(sheet);
  }

  function openSheet() {
    buildSheet();
    sheet.hidden = false;
    document.body.classList.add('hm-sheet-open');
    var more = bar && bar.querySelector('[data-tab="more"]');
    if (more) more.setAttribute('aria-expanded', 'true');
  }

  function closeSheet() {
    if (!sheet || sheet.hidden) return;
    sheet.hidden = true;
    document.body.classList.remove('hm-sheet-open');
    var more = bar && bar.querySelector('[data-tab="more"]');
    if (more) more.setAttribute('aria-expanded', 'false');
  }

  function onMore() {
    if (isDashboard) {
      /* The drawer already carries every destination plus the auth mount. */
      var trigger = document.getElementById('nav-drawer-trigger');
      if (trigger) { trigger.click(); return; }
    }
    if (sheet && !sheet.hidden) closeSheet();
    else openSheet();
  }

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') closeSheet();
  });

  /* ── Dashboard: account strip expand/collapse ───────────────────────── */

  function bindAccountStrip() {
    var strip = document.getElementById('account-strip');
    if (!strip || strip.getAttribute('data-hm-bound') === '1') return;
    strip.setAttribute('data-hm-bound', '1');

    strip.addEventListener('click', function (ev) {
      if (!mq.matches) return;
      /* The account identity control is a dropdown - let it open itself. */
      if (ev.target.closest('[data-dropdown-trigger], button, a, input, select')) return;
      var expanded = strip.getAttribute('data-hm-expanded') === 'true';
      strip.setAttribute('data-hm-expanded', String(!expanded));
    });

    /* The strip starts collapsed; reflect it for AT. */
    strip.setAttribute('data-hm-expanded', 'false');
  }

  /* ── Lifecycle ──────────────────────────────────────────────────────── */

  function apply() {
    if (mq.matches) {
      buildBar();
      if (isDashboard) bindAccountStrip();
    } else {
      destroyBar();
    }
  }

  window.addEventListener('hashchange', updateActive);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }

  if (mq.addEventListener) mq.addEventListener('change', apply);
  else if (mq.addListener) mq.addListener(apply);
})();
