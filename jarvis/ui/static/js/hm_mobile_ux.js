/**
 * HM Algo 2.0 — Mobile UX layer ("Aurora", v3)
 *
 * Phone-only (<= 599px) interactive enhancements, shared by every template.
 * The stylesheet (hm_ios_v3.css) provides the material; this file provides
 * the behaviour. Nothing here changes business logic — each feature delegates
 * to the page's existing controls.
 *
 *   1. Haptics         — subtle vibration on key taps (no-op where unsupported)
 *   2. Pull-to-refresh — iOS-style ring on any list scroller; clicks the
 *                        page's own Refresh control
 *   3. Swipe actions   — swipe-left on a position card reveals Close; the
 *                        action delegates to the row's own close button
 *   4. Ticket sheet    — data-pane="ticket" presents the watchlist panel as
 *                        an iOS modal sheet (scrim / Escape / tap-out)
 *   5. Account ticks   — value flash + P&L sign colouring on the hero strip
 *   6. Tools drawer    — right-hand panel holding what the v4 phone layout
 *                        demotes: account vitals, scanner/context and rare
 *                        actions (cockpit), demoted views + screen guide
 *                        (market pages). Entries delegate to the page's own
 *                        controls; no business logic lives here.
 *   7. List back-bar   — slim return chip while a demoted radar/context
 *                        pane is the active side tab
 *
 * Safety rails: inert on /classic, inert above 599px, never throws on pages
 * that lack a given control, and respects prefers-reduced-motion (features
 * still work — only the animation is dropped, which the stylesheet owns).
 */
(function () {
  'use strict';

  if (location.pathname === '/classic') return;
  if (!window.matchMedia) return;

  var PHONE = window.matchMedia('(max-width: 599px)');
  var $ = function (s, r) { return (r || document).querySelector(s); };

  function isPhone() { return PHONE.matches; }

  /* ── 1. Haptics ─────────────────────────────────────────────────────── */

  function vib(ms) {
    if (!isPhone()) return;
    try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) { /* noop */ }
  }
  window.HMHaptic = { tap: function () { vib(6); }, light: function () { vib(9); }, success: function () { vib([8, 40, 18]); } };

  /* One delegated press cue for the chrome. Captured (not bubbled) so the
     navigation itself cannot cancel it; passive so scrolling stays free. */
  document.addEventListener('pointerdown', function (ev) {
    if (!isPhone() || !ev.target || !ev.target.closest) return;
    var el = ev.target.closest(
      '.hm-tabbar__tab, .tt-pane-bar__btn, .tt-qbar__btn, .hm-swipe__act, .tt-drawer__item, ' +
      '.hm-sheet__item, .ticket-drawer-close, .hm-tools-trigger, .hm-tools__item, ' +
      '.hm-tools__close, .hm-listbar__back');
    if (el) vib(6);
  }, { passive: true, capture: true });

  /* ── 2. Pull-to-refresh ─────────────────────────────────────────────── */

  var ptr = null;         /* the floating ring element */
  var ptrBusy = false;

  function ptrEl() {
    if (ptr) return ptr;
    ptr = document.createElement('div');
    ptr.className = 'hm-ptr';
    ptr.setAttribute('aria-hidden', 'true');
    var ring = document.createElement('div');
    ring.className = 'hm-ptr__ring';
    ptr.appendChild(ring);
    document.body.appendChild(ptr);
    return ptr;
  }

  /* Some views forbid the gesture entirely. */
  function ptrBlocked(target) {
    if (!target || !target.closest) return true;
    if (document.body.classList.contains('hm-sheet-open')) return true;
    if (document.body.classList.contains('hm-tools-open')) return true;
    if (target.closest('#chart, #chart-tv, .tt-chart, .tt-panel--chart, .tt-panel--ticket')) return true;
    if (target.closest('.hm-sheet, .tt-drawer, #subpanel-ticket, .tt-order-drawer-left, .mobile-nav-bar, .hm-tools, .hm-tools-scrim')) return true;
    return false;
  }

  /* The nearest scrollable box the gesture belongs to; the document is the
     fallback for document-scrolling pages (stocks/india/options/console). */
  function ptrScroller(target) {
    var el = target;
    while (el && el !== document.body && el !== document.documentElement) {
      var cs = window.getComputedStyle(el);
      var oy = cs.overflowY;
      if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay') &&
          el.scrollHeight > el.clientHeight + 4) {
        return el;
      }
      el = el.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

  /* The page's own refresh control. Dashboard panes name theirs; the market
     pages name theirs; the fallback is the first visible refresh button. */
  function ptrRefreshButton(scroller) {
    /* The dashboard watchlist body scrolls a table whose refresh control is
       #watch-refresh; the pane body wraps #watch-body. */
    if (scroller && scroller !== document.scrollingElement &&
        scroller.querySelector && scroller.querySelector('#watch-body')) {
      var w = document.getElementById('watch-refresh');
      if (w) return w;
    }
    var named = ['#watch-refresh', '#news-refresh', '#btn-refresh-positions',
                 '#eq-refresh', '#in-refresh', '#hist-refresh', '#missed-refresh'];
    for (var i = 0; i < named.length; i++) {
      var el = $(named[i]);
      if (el && el.offsetParent && !el.disabled) return el;
    }
    var generic = document.querySelectorAll('.tt-btn[id$="-refresh"], .cx-btn[id*="-refresh"], .btn-refresh');
    for (var j = 0; j < generic.length; j++) {
      var g = generic[j];
      if (g.offsetParent && !g.disabled) return g;
    }
    return null;
  }

  var S = null;   /* live gesture state */

  function ptrStart(x, y, target) {
    if (!isPhone() || ptrBusy || S) return;
    if (ptrBlocked(target)) return;
    var scroller = ptrScroller(target);
    if (scroller !== document.scrollingElement && scroller.scrollTop > 0) return;
    if (scroller === document.scrollingElement) {
      var de = document.scrollingElement;
      if ((de.scrollTop || window.scrollY || 0) > 0) return;
    }
    S = { x: x, y: y, scroller: scroller, pull: 0, active: false };
  }

  function ptrMove(ev) {
    if (!S || ev.touches.length !== 1) return;
    var t = ev.touches[0];
    var dx = t.clientX - S.x;
    var dy = t.clientY - S.y;

    if (!S.active) {
      if (dy < 12 && dy > -12) return;                       /* undecided yet */
      if (Math.abs(dx) > Math.abs(dy)) { S = null; return; } /* horizontal: pane swipe / chart pan */
      var atTop = (S.scroller === document.scrollingElement)
        ? ((document.scrollingElement.scrollTop || window.scrollY || 0) <= 0)
        : (S.scroller.scrollTop <= 0);
      if (!atTop || dy < 0) { S = null; return; }
      S.active = true;
    }

    S.pull = Math.min(120, Math.max(0, dy * 0.5));

    if (ev.cancelable) ev.preventDefault();

    var el = ptrEl();
    var r = (S.scroller === document.scrollingElement)
      ? { top: 10 }
      : S.scroller.getBoundingClientRect();
    el.style.top = (r.top + 12) + 'px';
    el.style.opacity = '';
    el.classList.add('hm-ptr--on');
    el.style.transform = 'translateY(' + Math.min(24, S.pull * 0.3) + 'px) scale(' +
      (0.7 + Math.min(0.3, S.pull / 140)) + ')';
    el.classList.toggle('hm-ptr--spin', S.pull >= 64);
  }

  function ptrEnd() {
    if (!S) return;
    var fire = S.pull >= 64 && S.active;
    var scroller = S.scroller;
    var el = ptrEl();
    S = null;
    if (!fire) {
      el.classList.remove('hm-ptr--on', 'hm-ptr--spin');
      el.style.transform = '';
      return;
    }
    vib(10);
    ptrBusy = true;
    el.classList.add('hm-ptr--spin');
    var btn = ptrRefreshButton(scroller) || ptrRefreshButton(null);
    try { if (btn) btn.click(); } catch (e) { /* noop */ }
    window.setTimeout(function () {
      el.classList.remove('hm-ptr--on', 'hm-ptr--spin');
      el.style.transform = '';
      ptrBusy = false;
    }, 950);
  }

  document.addEventListener('touchstart', function (ev) {
    if (!isPhone()) return;
    ptrStart(ev.touches[0].clientX, ev.touches[0].clientY, ev.target);
  }, { passive: true, capture: true });

  document.addEventListener('touchmove', function (ev) {
    if (!S) return;
    ptrMove(ev);
  }, { passive: false, capture: true });

  document.addEventListener('touchend', function () { ptrEnd(); }, { passive: true, capture: true });
  document.addEventListener('touchcancel', function () { if (S) { S = null; if (ptr) ptr.classList.remove('hm-ptr--on', 'hm-ptr--spin'); } }, { passive: true, capture: true });

  /* ── 3. Swipe actions on position cards ─────────────────────────────── */

  var SWIPE_W = 78;      /* open offset */
  var openRow = null;

  function closeRow(tr) {
    if (!tr) return;
    tr.classList.remove('hm-swipe--open', 'hm-swiping');
    Array.prototype.forEach.call(tr.querySelectorAll(':scope > td'), function (td) {
      td.style.transform = '';
    });
    if (openRow === tr) openRow = null;
  }

  function paintRow(tr, dx) {
    Array.prototype.forEach.call(tr.querySelectorAll(':scope > td'), function (td) {
      td.style.transform = 'translateX(' + dx + 'px)';
    });
  }

  function wireSwipeRow(tr) {
    if (tr.getAttribute('data-hm-swiped') === '1') return;
    var action = tr.querySelector('[data-pos-close], [data-action-close]');
    if (!action) return;
    tr.setAttribute('data-hm-swiped', '1');
    tr.classList.add('hm-swipeable');

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'hm-swipe__act';
    btn.setAttribute('aria-label', 'Close position');
    btn.innerHTML =
      '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg><span>Close</span>';
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      ev.preventDefault();
      vib(12);
      closeRow(tr);
      try { action.click(); } catch (e) { /* the row button owns the flow */ }
    });
    tr.appendChild(btn);

    var sx = 0, sy = 0, dx = 0, tracking = false, decided = false, horiz = false;

    tr.addEventListener('touchstart', function (ev) {
      if (!isPhone() || ev.touches.length !== 1 || ptrBlocked(ev.target)) return;
      sx = ev.touches[0].clientX;
      sy = ev.touches[0].clientY;
      tracking = true; decided = false; horiz = false; dx = 0;
    }, { passive: true });

    tr.addEventListener('touchmove', function (ev) {
      if (!tracking || ev.touches.length !== 1) return;
      var t = ev.touches[0];
      var mx = t.clientX - sx;
      var my = t.clientY - sy;
      if (!decided) {
        if (Math.abs(mx) < 8 && Math.abs(my) < 8) return;
        decided = true;
        horiz = Math.abs(mx) > Math.abs(my) * 1.3;
        if (horiz) {
          tr.classList.add('hm-swiping');
          if (openRow && openRow !== tr) closeRow(openRow);
        }
      }
      if (!horiz) return;
      if (ev.cancelable) ev.preventDefault();
      var base = tr.classList.contains('hm-swipe--open') ? -SWIPE_W : 0;
      dx = Math.max(-SWIPE_W - 26, Math.min(0, mx + base));
      tr.classList.add('hm-swipe--drag');
      paintRow(tr, dx);
    }, { passive: false });

    function finish() {
      if (!tracking) return;
      tr.classList.remove('hm-swiping');
      if (horiz) {
        if (dx <= -SWIPE_W * 0.45) {
          tr.classList.add('hm-swipe--open');
          paintRow(tr, -SWIPE_W);
          if (openRow && openRow !== tr) closeRow(openRow);
          openRow = tr;
          vib(8);
        } else {
          closeRow(tr);
        }
      }
      tr.classList.remove('hm-swipe--drag');
      tracking = false;
    }
    tr.addEventListener('touchend', finish, { passive: true });
    tr.addEventListener('touchcancel', finish, { passive: true });

    /* While a row is open, the first tap anywhere on it just closes it. */
    tr.addEventListener('click', function (ev) {
      if (tr.classList.contains('hm-swipe--open') && !ev.target.closest('.hm-swipe__act')) {
        ev.stopPropagation();
        ev.preventDefault();
        closeRow(tr);
      }
    }, true);
  }

  function wireSwipeAll() {
    if (!isPhone()) return;
    var rows = document.querySelectorAll('table tbody > tr');
    Array.prototype.forEach.call(rows, wireSwipeRow);
  }

  /* Rows re-render on every poll, so re-wire as they appear (debounced). */
  var wireTimer = null;
  function scheduleWire() {
    if (wireTimer) return;
    wireTimer = window.setTimeout(function () { wireTimer = null; wireSwipeAll(); }, 260);
  }

  /* ── 4. Ticket bottom sheet (dashboard) ─────────────────────────────── */

  function initTicketSheet() {
    var app = $('.tt-app');
    if (!app) return;

    var scrim = document.createElement('div');
    scrim.className = 'hm-ticket-scrim';
    scrim.setAttribute('aria-hidden', 'true');
    scrim.addEventListener('click', function () { closeTicketSheet(true); });

    var lastPane = 'watchlist';

    function isOpen() { return app.getAttribute('data-hm-ticket') === 'open'; }

    function openTicketSheet() {
      if (isOpen() || !isPhone()) return;
      document.body.appendChild(scrim);
      /* Force a frame so the scrim transition runs from opacity 0. */
      window.requestAnimationFrame(function () { scrim.classList.add('hm-ticket-scrim--on'); });
      app.setAttribute('data-hm-ticket', 'open');
      vib(9);
    }

    function closeTicketSheet(restore) {
      if (!isOpen()) return;
      app.removeAttribute('data-hm-ticket');
      scrim.classList.remove('hm-ticket-scrim--on');
      window.setTimeout(function () {
        if (scrim.parentNode) scrim.parentNode.removeChild(scrim);
      }, 320);
      if (restore) {
        var btn = document.querySelector('.tt-pane-bar__btn[data-pane-btn="' + lastPane + '"]') ||
                  document.querySelector('.tt-pane-bar__btn[data-pane-btn="watchlist"]');
        if (btn) btn.click();
      }
    }

    /* The pane state machine stays in dashboard.js; this only observes it. */
    new MutationObserver(function () {
      var pane = app.getAttribute('data-pane');
      if (pane === 'ticket') openTicketSheet();
      else {
        if (lastPane !== pane) lastPane = pane || 'watchlist';
        closeTicketSheet(false);
      }
    }).observe(app, { attributes: true, attributeFilter: ['data-pane'] });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closeTicketSheet(true);
    });

    /* Leaving the phone layout must not strand the sheet. */
    if (PHONE.addEventListener) PHONE.addEventListener('change', function () { if (!isPhone()) closeTicketSheet(false); });

    /* Tapping a side-pane tab inside the sheet (everything but Context) asks
       for that content full-screen: exit the sheet first. */
    document.addEventListener('click', function (ev) {
      if (!isOpen() || !ev.target || !ev.target.closest) return;
      var tab = ev.target.closest('.tt-side-pane__tabs [data-side-tab], [data-side-btn]');
      if (tab && tab.getAttribute('data-side-btn') !== 'context') closeTicketSheet(false);
    }, true);
  }

  /* ── 4b. Order ticket drawer: swipe-down to dismiss ─────────────────── */

  /* The qbar "New order" opens the Speed Cockpit drawer; v3 presents it as
     a bottom sheet on phones. Dragging its head down follows the finger and
     a decisive flick closes it through the drawer's own close button. */
  function initTicketDrawerSheet() {
    var drawer = document.getElementById('subpanel-ticket');
    var head = drawer && drawer.querySelector('.ticket-drawer-head');
    if (!drawer || !head) return;

    var sy = 0, dy = 0, tracking = false;

    head.addEventListener('touchstart', function (ev) {
      if (!isPhone() || ev.touches.length !== 1 || !drawer.classList.contains('is-open')) return;
      sy = ev.touches[0].clientY;
      dy = 0;
      tracking = true;
      drawer.style.setProperty('transition', 'none', 'important');
    }, { passive: true });

    head.addEventListener('touchmove', function (ev) {
      if (!tracking || ev.touches.length !== 1) return;
      dy = Math.max(0, ev.touches[0].clientY - sy);
      if (dy > 6 && ev.cancelable) ev.preventDefault();
      drawer.style.setProperty('transform', 'translate3d(0,' + dy + 'px,0)', 'important');
    }, { passive: false });

    function endDrag() {
      if (!tracking) return;
      tracking = false;
      drawer.style.removeProperty('transition');
      if (dy <= 8) {
        /* A tap (e.g. on the close button): no inline styling left behind. */
        drawer.style.removeProperty('transform');
        return;
      }
      if (dy > 72) {
        vib(8);
        drawer.style.setProperty('transform', 'translate3d(0, 110%, 0)', 'important');
        window.setTimeout(function () {
          var btn = document.getElementById('btn-close-ticket');
          if (btn) { try { btn.click(); } catch (e) { /* noop */ } }
          window.setTimeout(function () { drawer.style.removeProperty('transform'); }, 420);
        }, 240);
      } else {
        /* Dropping the inline offset lets the sheet transition back home. */
        drawer.style.removeProperty('transform');
      }
    }
    head.addEventListener('touchend', endDrag, { passive: true });
    head.addEventListener('touchcancel', endDrag, { passive: true });
  }

  /* ── 5. Account hero ticks ──────────────────────────────────────────── */

  function initAccountTicks() {
    var pairs = [
      { id: 'acc-equity', sign: false },
      { id: 'acc-profit', sign: true }
    ];

    pairs.forEach(function (p) {
      var el = document.getElementById(p.id);
      if (!el) return;
      var last = parseFloat(String(el.textContent).replace(/[^0-9eE+.\-]/g, ''));

      function apply() {
        var v = parseFloat(String(el.textContent).replace(/[^0-9eE+.\-]/g, ''));
        if (p.sign && isFinite(v)) {
          el.setAttribute('data-hm-sign', v > 0 ? 'pos' : (v < 0 ? 'neg' : ''));
        }
        if (isFinite(v) && isFinite(last) && v !== last) {
          el.classList.remove('hm-tick--up', 'hm-tick--down');
          void el.offsetWidth;                     /* restart the animation */
          el.classList.add(v > last ? 'hm-tick--up' : 'hm-tick--down');
          window.setTimeout(function () {
            el.classList.remove('hm-tick--up', 'hm-tick--down');
          }, 900);
        }
        if (isFinite(v)) last = v;
      }

      new MutationObserver(apply).observe(el, {
        childList: true, characterData: true, subtree: true
      });
      apply();
    });
  }

  /* ── 6. Tools drawer (v4) ───────────────────────────────────────────── */

  /* A right-hand panel hosting everything the v4 phone layout demotes from
     the main screens: account vitals, the scanner, the reasoning context and
     rare actions (cockpit), plus demoted views and the screen guide (market
     pages). Every menu entry delegates through the original control's
     .click(), so no business logic lives here. */

  var tScrim = null;
  var tTools = null;
  var tOpen = false;
  var tTriggersReady = false;
  var tListbarReady = false;

  var TICONS = {
    tools: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><circle cx="9.5" cy="7" r="2.4"/><path d="M4 12h16"/><circle cx="14.5" cy="12" r="2.4"/><path d="M4 17h16"/><circle cx="7.5" cy="17" r="2.4"/></svg>',
    radar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.2"/><circle cx="12" cy="12" r="3.4"/><path d="M12 12l6.2-6.2"/></svg>',
    context: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.2a6.4 6.4 0 0 1 3.8 11.5c-.6.5-.9 1.1-1 1.8l-.1 1.3h-5.4l-.1-1.3c-.1-.7-.4-1.3-1-1.8A6.4 6.4 0 0 1 12 3.2z"/><path d="M9.6 20.4h4.8"/></svg>',
    flatten: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M7 12h10"/><path d="M9 17l-2-4.6"/><path d="M15 17l2-4.6"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12a8 8 0 0 1 14-5.3L20 5"/><path d="M20 5v5h-5"/><path d="M20 12a8 8 0 0 1-14 5.3L4 19"/><path d="M4 19v-5h5"/></svg>',
    grid: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="4" width="6.5" height="6.5" rx="1.6"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.6"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.6"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.6"/></svg>',
    chev: '<svg class="hm-tools__chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>',
    close: '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>',
    back: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 6-6 6 6 6"/></svg>'
  };

  function makeEl(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function isCockpit() { return !!$('.tt-qbar') && !!$('#side-pane-watch'); }
  function marketBar() { return $('.mobile-nav-bar'); }

  function buildTools() {
    if (tTools) return;
    tScrim = makeEl('div', 'hm-tools-scrim');
    tScrim.setAttribute('aria-hidden', 'true');
    tScrim.addEventListener('click', function () { closeTools(); });

    tTools = makeEl('aside', 'hm-tools');
    tTools.setAttribute('role', 'dialog');
    tTools.setAttribute('aria-modal', 'true');
    tTools.setAttribute('aria-label', 'Tools');
    tTools.innerHTML =
      '<div class="hm-tools__head">' +
        '<div>' +
          '<div class="hm-tools__title">Tools</div>' +
          '<div class="hm-tools__page" id="hm-tools-page"></div>' +
        '</div>' +
        '<button type="button" class="hm-tools__close" aria-label="Close tools">' + TICONS.close + '</button>' +
      '</div>' +
      '<div class="hm-tools__body" id="hm-tools-body"></div>';
    tTools.querySelector('.hm-tools__close').addEventListener('click', function () { closeTools(); });

    /* Swipe-left on the head returns the panel home. */
    var head = tTools.querySelector('.hm-tools__head');
    var hx = 0, hy = 0, hlive = false;
    head.addEventListener('touchstart', function (ev) {
      if (ev.touches.length !== 1) return;
      hx = ev.touches[0].clientX;
      hy = ev.touches[0].clientY;
      hlive = true;
    }, { passive: true });
    head.addEventListener('touchend', function (ev) {
      if (!hlive) return;
      hlive = false;
      var t = ev.changedTouches[0];
      var dx = t.clientX - hx;
      var dy = Math.abs(t.clientY - hy);
      if (dx <= -46 && Math.abs(dx) > dy * 1.3) closeTools();
    }, { passive: true });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closeTools();
    });

    document.body.appendChild(tScrim);
    document.body.appendChild(tTools);
  }

  function toolsSection(body, label) {
    body.appendChild(makeEl('div', 'hm-tools__section', label));
  }

  function toolsRow(card, label, valueNode) {
    var r = makeEl('div', 'hm-tools__row');
    r.appendChild(makeEl('span', 'hm-tools__label', label));
    r.appendChild(valueNode);
    card.appendChild(r);
  }

  /* Live mirror of a page value: copies the text (and any P&L sign flag) and
     re-syncs whenever the source element's text changes on a poll. */
  function toolsMirror(srcId, chip) {
    var span = makeEl('span', 'hm-tools__value' + (chip ? ' hm-tools__chip' : ''));
    function sync() {
      var s = document.getElementById(srcId);
      if (!s) { span.textContent = '—'; return; }
      span.textContent = String(s.textContent || '').replace(/\s+/g, ' ').trim() || '—';
      var sign = s.getAttribute('data-hm-sign');
      if (sign) span.setAttribute('data-hm-sign', sign);
      else span.removeAttribute('data-hm-sign');
    }
    sync();
    var src = document.getElementById(srcId);
    if (src) new MutationObserver(sync).observe(src, { childList: true, characterData: true, subtree: true });
    return span;
  }

  function toolsItem(list, icon, label, sub, fn, active) {
    var b = makeEl('button', 'hm-tools__item' + (active ? ' hm-tools__item--active' : ''),
      '<span class="hm-tools__ico">' + icon + '</span>' +
      '<span class="hm-tools__item-text">' +
        '<span class="hm-tools__item-label">' + label + '</span>' +
        (sub ? '<span class="hm-tools__sub">' + sub + '</span>' : '') +
      '</span>' + TICONS.chev);
    b.type = 'button';
    b.addEventListener('click', function () { vib(8); fn(); });
    list.appendChild(b);
    return b;
  }

  function toolsDelegate(id) {
    var el = document.getElementById(id);
    if (el) { try { el.click(); } catch (e) { /* noop */ } }
  }

  function populateCockpit(body) {
    var page = document.getElementById('hm-tools-page');
    if (page) page.textContent = 'Cockpit';

    toolsSection(body, 'Account');
    var vitals = makeEl('div', 'hm-tools__card');
    toolsRow(vitals, 'Equity', toolsMirror('acc-equity'));
    toolsRow(vitals, 'Open P&L', toolsMirror('acc-profit'));
    toolsRow(vitals, 'Free margin', toolsMirror('acc-margin'));
    toolsRow(vitals, 'Risk', toolsMirror('acc-risk'));
    body.appendChild(vitals);

    var desk = makeEl('div', 'hm-tools__card');
    toolsRow(desk, 'Account', toolsMirror('acc-id'));
    toolsRow(desk, 'Broker', toolsMirror('conn-chip', true));
    toolsRow(desk, 'Mode', toolsMirror('exec-mode', true));
    toolsRow(desk, 'Clock', toolsMirror('clock'));
    body.appendChild(desk);

    var side = document.body.getAttribute('data-side') || 'watch';
    toolsSection(body, 'Views');
    toolsItem(body, TICONS.radar, 'Scanner radar', 'Ranked breakout candidates', function () {
      closeTools();
      toolsDelegate('side-btn-radar');
    }, side === 'radar');
    toolsItem(body, TICONS.context, 'Context', 'Engine reasoning · Analyst · News', function () {
      closeTools();
      toolsDelegate('side-btn-context');
    }, side === 'context');

    toolsSection(body, 'Actions');
    toolsItem(body, TICONS.flatten, 'Flatten all', 'Close every open position', function () {
      closeTools();
      toolsDelegate('qbar-flatten');
    });
    toolsItem(body, TICONS.refresh, 'Refresh watchlist', 'Re-pull quotes and scans', function () {
      closeTools();
      toolsDelegate('qbar-refresh');
    });
  }

  function populateMarket(body) {
    var badge = $('.bugatti-brand__badge') || $('.porsche-brand-sub');
    var page = document.getElementById('hm-tools-page');
    if (page) page.textContent = (badge && badge.textContent.replace(/\s+/g, ' ').trim()) || 'Markets';

    var bar = marketBar();
    if (bar) {
      var tabs = bar.querySelectorAll('.mob-tab-btn:not(.hm-tools-trigger)');
      var shown = false;
      Array.prototype.forEach.call(tabs, function (b, i) {
        if (i === 0) return;             /* the primary view keeps its pill */
        if (!shown) { toolsSection(body, 'Views'); shown = true; }
        var label = (b.textContent || '').replace(/\s+/g, ' ').trim() || 'View';
        toolsItem(body, TICONS.grid, label, null, function () {
          closeTools();
          try { b.click(); } catch (e) { /* noop */ }
        }, !!(b.classList.contains('active') || b.getAttribute('aria-selected') === 'true'));
      });
    }

    var hero = $('.buy-now-header-bar');
    if (hero) {
      var title = hero.querySelector('.buy-now-title');
      var desc = hero.querySelector('.buy-now-title ~ div');
      var headline = '';
      if (title) {
        var clone = title.cloneNode(true);
        Array.prototype.forEach.call(clone.querySelectorAll('.badge, .buy-now-pulse-icon'), function (n) {
          n.parentNode.removeChild(n);
        });
        headline = clone.textContent.replace(/\s+/g, ' ').trim();
      }
      var detail = desc ? desc.textContent.replace(/\s+/g, ' ').trim() : '';
      if (headline || detail) {
        toolsSection(body, 'Screen guide');
        var g = makeEl('div', 'hm-tools__guide');
        if (headline) g.appendChild(makeEl('div', 'hm-tools__guide-title', headline));
        if (detail) g.appendChild(makeEl('div', 'hm-tools__guide-desc', detail));
        body.appendChild(g);
      }
    }
  }

  function openTools() {
    if (!isPhone() || tOpen) return;
    buildTools();
    var body = document.getElementById('hm-tools-body');
    if (!body) return;
    body.innerHTML = '';
    if (isCockpit()) populateCockpit(body);
    else if (marketBar()) populateMarket(body);
    else return;
    tOpen = true;
    document.body.classList.add('hm-tools-open');
    Array.prototype.forEach.call(document.querySelectorAll('.hm-tools-trigger'), function (b) {
      b.setAttribute('aria-expanded', 'true');
    });
    /* Force a frame so the panel slides from its off-screen offset. */
    window.requestAnimationFrame(function () {
      tScrim.classList.add('hm-tools-scrim--on');
      tTools.classList.add('hm-tools--open');
    });
    vib(9);
  }

  function closeTools() {
    if (!tOpen || !tTools) return;
    tOpen = false;
    document.body.classList.remove('hm-tools-open');
    tTools.classList.remove('hm-tools--open');
    tScrim.classList.remove('hm-tools-scrim--on');
    Array.prototype.forEach.call(document.querySelectorAll('.hm-tools-trigger'), function (b) {
      b.setAttribute('aria-expanded', 'false');
    });
  }

  function initToolsTriggers() {
    if (!isPhone() || tTriggersReady) return;
    tTriggersReady = true;

    var bar = marketBar();
    if (bar && !isCockpit()) {
      /* Demote everything past the primary view; the buttons stay clickable
         from the drawer (CSS only hides them). */
      var tabs = bar.querySelectorAll('.mob-tab-btn:not(.hm-tools-trigger)');
      Array.prototype.forEach.call(tabs, function (b, i) {
        if (i > 0) b.classList.add('hm-demoted');
      });
      if (!bar.querySelector('.hm-tools-trigger')) {
        var tb = makeEl('button', 'mob-tab-btn hm-tools-trigger', TICONS.tools + '<span>Tools</span>');
        tb.type = 'button';
        tb.setAttribute('aria-label', 'Open tools');
        tb.setAttribute('aria-expanded', 'false');
        tb.addEventListener('click', function () { openTools(); });
        bar.appendChild(tb);
      }
      return;
    }

    var paneBar = $('.tt-pane-bar');
    if (paneBar && !paneBar.querySelector('.hm-tools-trigger')) {
      var pb = makeEl('button', 'hm-tools-trigger', TICONS.tools);
      pb.type = 'button';
      pb.setAttribute('aria-label', 'Open tools');
      pb.setAttribute('aria-expanded', 'false');
      pb.addEventListener('click', function () { openTools(); });
      paneBar.appendChild(pb);
    }
  }

  /* Right-edge swipe opens the drawer on any phone layout. */
  (function () {
    var edgeX = 0, startY = 0, edgeLive = false;
    document.addEventListener('touchstart', function (ev) {
      edgeLive = false;
      if (!isPhone() || tOpen || ev.touches.length !== 1) return;
      if (document.body.getAttribute('data-hm-ticket') === 'open') return;
      var dr = document.getElementById('subpanel-ticket');
      if (dr && dr.classList.contains('is-open')) return;
      var t = ev.touches[0];
      if (t.clientX < window.innerWidth - 26) return;
      edgeX = t.clientX;
      startY = t.clientY;
      edgeLive = true;
    }, { passive: true, capture: true });
    document.addEventListener('touchend', function (ev) {
      if (!edgeLive) return;
      edgeLive = false;
      var t = ev.changedTouches[0];
      var dx = edgeX - t.clientX;
      var dy = Math.abs(t.clientY - startY);
      if (dx >= 52 && dx > dy * 1.4) openTools();
    }, { passive: true, capture: true });
  })();

  /* ── 7. Cockpit list back-bar (v4) ──────────────────────────────────── */

  /* Radar/Context left the segment for the Tools drawer, so their panes get
     a slim return chip instead. dashboard.js mirrors the active side tab on
     <body data-side>; this only reacts to that flag. */
  function initListBar() {
    if (!isPhone() || tListbarReady) return;
    var panel = $('.tt-panel--watchlist');
    if (!panel) return;
    tListbarReady = true;

    function update() {
      var side = document.body.getAttribute('data-side') || 'watch';
      var bar = panel.querySelector('.hm-listbar');
      var wanted = (side === 'radar' || side === 'context');
      if (!wanted) { if (bar) bar.parentNode.removeChild(bar); return; }
      if (!bar) {
        bar = makeEl('div', 'hm-listbar',
          '<button type="button" class="hm-listbar__back">' + TICONS.back + '<span>Watchlist</span></button>' +
          '<span class="hm-listbar__title"></span>');
        bar.querySelector('.hm-listbar__back').addEventListener('click', function () {
          vib(6);
          toolsDelegate('side-btn-watch');
        });
        panel.insertBefore(bar, panel.firstChild);
      }
      bar.querySelector('.hm-listbar__title').textContent = side === 'radar' ? 'Scanner radar' : 'Context';
    }

    new MutationObserver(update).observe(document.body, { attributes: true, attributeFilter: ['data-side'] });
    update();
  }

  /* ── Lifecycle ──────────────────────────────────────────────────────── */

  function boot() {
    try { initTicketSheet(); } catch (e) { /* never block the page */ }
    try { initTicketDrawerSheet(); } catch (e) { /* optional layer */ }
    try { initAccountTicks(); } catch (e) { /* optional helper */ }
    try { initToolsTriggers(); } catch (e) { /* optional layer */ }
    try { initListBar(); } catch (e) { /* optional layer */ }
    if (!isPhone()) return;
    wireSwipeAll();
    new MutationObserver(scheduleWire).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  if (PHONE.addEventListener) {
    PHONE.addEventListener('change', function () {
      if (isPhone()) { wireSwipeAll(); initToolsTriggers(); initListBar(); }
      else {
        closeTools();
        if (openRow) closeRow(openRow);
        if (ptr) { ptr.classList.remove('hm-ptr--on', 'hm-ptr--spin'); }
      }
    });
  }
})();
