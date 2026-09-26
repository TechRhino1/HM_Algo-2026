/* ===========================================================================
   HM Algo 2.0 — Trading Terminal controller (dashboard.js)

   DESIGN NOTES
   ------------
   1. NO HARD-CODED MARKET DATA. Every value rendered here comes from an API
      response. Where a field is absent the UI shows an explicit state
      (loading / empty / stale / error) — never a plausible-looking default.
      A trading screen that invents a price is worse than one that shows none.

   2. EVERY REQUEST IS BOUNDED. fetch() has no default timeout, so an endpoint
      that never responds leaves a panel spinning forever. apiGet() wraps every
      call in an AbortController with a per-endpoint budget, and the caller
      renders a real error state when it trips.

   3. STALENESS IS VISIBLE. Cached values are useful, but a cached value shown
      as live is a trap. Every payload carries its own timestamp where the API
      provides one, and the header shows the age once it exceeds the cadence.

   4. NO innerHTML WITH SERVER DATA. Symbol names, strategies, gate reasons and
      error strings all originate outside this process. Every one is written
      with textContent, or escaped through esc() when it must be interpolated
      into a template string.

   5. POLLING IS TIERED. Telemetry drives the account strip and the watchlist,
      so it polls fast. Candles are heavier. Reliability and job lists change
      slowly. Polling everything at the fastest rate burns CPU for no gain.
   =========================================================================== */
(function () {
  'use strict';

  /* ── Configuration ────────────────────────────────────────────────────── */
  var POLL = {
    telemetry: 3000,
    jobs: 4000,
    chart: 20000,
    analytics: 20000,
    news: 120000
  };

  // Per-request budgets in ms. Chosen from measured behaviour: local endpoints
  // answer in well under a second, but the external-provider routes (stocks,
  // india) hang indefinitely without a ceiling. The provider budget is longer
  // because those routes do a cold-cache sweep on first call and then answer
  // from cache; the panel renders its loading state meanwhile.
  var TIMEOUT = {
    fast: 8000,
    normal: 15000,
    provider: 30000,
    slow: 60000
  };

  var VIEWS = ['trade', 'news', 'analyst', 'markets', 'analytics', 'backtest'];
  var PANES = ['watchlist', 'chart', 'ticket'];

  var state = {
    view: 'trade',
    pane: 'watchlist',
    symbol: null,
    timeframe: 'H1',
    decisions: {},        // symbol -> decision object (from telemetry)
    marketStatuses: {},   // symbol -> session status
    positions: [],
    history: [],
    account: null,
    services: {},
    executionMode: null,
    safeMode: null,
    telemetryAt: null,    // client clock when telemetry last arrived
    serverTimestamp: null,
    reliability: [],
    jobs: [],
    activeJob: null,
    backtestMeta: null,   // /api/backtest/meta — dimensions + defaults from the server
    selection: null,
    radar: [],            // orchestrator's ranked candidates (telemetry)
    radarFilter: 'ALL',   // style filter for the scanner radar
    posTab: 'open',       // active tab in the Open positions panel: open | history | pending
    posPending: [],       // working orders fetched from /api/pending_orders for the panel's PENDING tab
    chart: null,          // {host, chart, candles, volume, lines, tradeLines}
    chartCandles: [],     // bars currently drawn — source for level maths
    chartLevels: null,    // {r1,r2,s1,s2}; null when no swing pivot exists
    chartSymbol: null,    // symbol the drawn series belongs to
    chartTimeframe: null, // timeframe the drawn series belongs to
    chartPainted: false,  // false until the first setData() has run
    showLevels: true,     // support/resistance + trade overlays on the chart
    chartSource: 'native',// 'native' | 'tradingview'
    tvSymbol: null,       // symbol the TradingView widget was built for
    tvInterval: null,     // timeframe the TradingView widget was built for
    tvState: 'idle',      // idle | loading | ready | failed
    news: [],             // events from /api/news, in server order
    newsAt: 0,            // client clock when the calendar arrived
    newsSkew: null,       // ms this machine's clock runs ahead of the server
    newsSource: null,     // live_feed | mixed | synthetic_calendar, from the API
    newsSynthetic: 0,     // how many of those events are the hardcoded plan
    newsImpact: 'ALL',    // impact filter
    newsCurrency: 'ALL',  // currency filter
    newsSelected: null,   // key of the event open in the detail panel
    newsHeroKey: null,    // event the "next release" panel is featuring
    context: 'why',       // which strip is showing beside the ticket
    equities: null,       // /api/stocks/screener payload
    equityRows: [],
    heatmap: null,        // /api/stocks/heatmap payload
    indiaIndices: null,   // /api/india/indices payload
    indiaFii: null,       // /api/india/fii_dii payload
    indiaOptionChain: null,
    lastValues: {}        // for flash-on-change
  };

  var timers = {};

  /* ── DOM helpers ──────────────────────────────────────────────────────── */
  function $(id) { return document.getElementById(id); }

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function setText(el, value) {
    if (!el) return;
    el.textContent = (value === null || value === undefined || value === '') ? '—' : String(value);
  }

  /* Set the explicit state of a container. `data-state` drives the stylesheet. */
  function setState(el, name, message, detail) {
    if (!el) return;
    el.setAttribute('data-state', name);
    if (message === undefined) return;
    el.innerHTML = '';
    var wrap = document.createElement('div');
    wrap.className = 'tt-state tt-state--' + name;
    var title = document.createElement('span');
    title.className = 'tt-state__title';
    title.textContent = message;
    wrap.appendChild(title);
    if (detail) {
      var d = document.createElement('span');
      d.textContent = detail;
      wrap.appendChild(d);
    }
    el.appendChild(wrap);
  }

  /* ── Formatters ───────────────────────────────────────────────────────── */
  function num(value, digits) {
    if (value === null || value === undefined || value === '' || isNaN(value)) return '—';
    var v = Number(value);
    if (!isFinite(v)) return '—';
    return v.toLocaleString('en-US', {
      minimumFractionDigits: digits === undefined ? 2 : digits,
      maximumFractionDigits: digits === undefined ? 2 : digits
    });
  }

  /* Price precision is a property of the instrument, not of the number's
     magnitude. A JPY pair quotes to 3 decimals and gold to 2 whatever the
     value; inferring from size alone renders USDJPY as 151.23400 and disagrees
     with the backend, which resolves precision per symbol. */
  var PRICE_DIGITS = [
    [/^(XAU|GOLD)/, 2],
    [/^(XAG|SILVER)/, 2],
    [/^(BTC|ETH)/, 2],
    [/(NAS100|US30|US500|US100|GER40|UK40|UK100|JP225|HK50|SPX|DAX)/, 1],
    [/JPY$/, 3]
  ];

  function priceDigits(symbol, value) {
    var s = String(symbol || '').toUpperCase();
    for (var i = 0; i < PRICE_DIGITS.length; i++) {
      if (PRICE_DIGITS[i][0].test(s)) return PRICE_DIGITS[i][1];
    }
    var v = Math.abs(Number(value));
    if (!isFinite(v) || v === 0) return 5;
    if (v >= 100) return 2;
    if (v >= 10) return 3;
    return 5;
  }

  /* A price of 0 means "not set" for a stop or target, so it renders as a dash
     rather than as a real level at zero. */
  function formatPrice(value, symbol) {
    if (value === null || value === undefined || value === '') return '—';
    var v = Number(value);
    if (!isFinite(v) || v === 0) return '—';
    return num(v, priceDigits(symbol, v));
  }

  function pct(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return '—';
    return num(Number(value) * (Math.abs(Number(value)) <= 1.5 ? 100 : 1), digits === undefined ? 1 : digits) + '%';
  }

  function signClass(value) {
    var v = Number(value);
    if (!isFinite(v) || v === 0) return 'tt-flat';
    return v > 0 ? 'tt-up' : 'tt-down';
  }

  function clockTime(value) {
    if (!value) return '—';
    var d = value instanceof Date ? value : new Date(value);
    if (isNaN(d.getTime())) return String(value);
    return d.toLocaleTimeString('en-GB', { hour12: false });
  }

  /* Relative age of a timestamp, in seconds. */
  function ageSeconds(ts) {
    if (!ts) return null;
    var d = new Date(String(ts).replace(' ', 'T'));
    if (isNaN(d.getTime())) return null;
    return Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  }

  function agoText(seconds) {
    if (seconds === null) return '—';
    if (seconds < 60) return seconds + 's ago';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm ago';
    return Math.round(seconds / 3600) + 'h ago';
  }

  /* Flash a cell when its value changes, so movement is visible without
     reading every digit. Removed immediately afterwards. */
  function flash(el, value) {
    if (!el) return;
    var key = el.id || '';
    var prev = state.lastValues[key];
    state.lastValues[key] = value;
    if (prev === undefined || prev === null || value === null) return;
    if (Number(prev) === Number(value)) return;
    if (!isFinite(Number(prev)) || !isFinite(Number(value))) return;
    var cls = Number(value) > Number(prev) ? 'tt-flash-up' : 'tt-flash-down';
    el.classList.remove('tt-flash-up', 'tt-flash-down');
    void el.offsetWidth;               // restart the animation
    el.classList.add(cls);
    setTimeout(function () { el.classList.remove(cls); }, 700);
  }

  /* ── Transport ────────────────────────────────────────────────────────── */
  function authHeaders() {
    var h = { 'Content-Type': 'application/json' };
    try {
      var token = (typeof window.getAuthToken === 'function' && window.getAuthToken()) || null;
      if (token) h['Authorization'] = 'Bearer ' + token;
    } catch (e) { /* auth is optional for public reads */ }
    return h;
  }

  /* fetch with a hard ceiling. Returns {ok, status, data, error}. */
  function apiRequest(path, options) {
    options = options || {};
    var budget = options.timeout || TIMEOUT.normal;
    var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = null;

    if (controller) {
      timer = setTimeout(function () { controller.abort(); }, budget);
    }

    return fetch(path, {
      method: options.method || 'GET',
      headers: authHeaders(),
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: controller ? controller.signal : undefined,
      cache: 'no-store'
    }).then(function (resp) {
      if (timer) clearTimeout(timer);
      return resp.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        return { ok: resp.ok, status: resp.status, data: data, raw: text };
      });
    }).catch(function (err) {
      if (timer) clearTimeout(timer);
      var aborted = err && (err.name === 'AbortError' || /abort/i.test(String(err.message || '')));
      var raw = String((err && err.message) || err);
      // `fetch` rejects with the bare string "Failed to fetch" for EVERY
      // transport-level failure - connection refused, DNS failure, reset
      // mid-flight. That is the browser's internal phrasing: it names no cause
      // and suggests no action. The panels render this string verbatim, so a
      // dead backend used to put "Failed to fetch" in front of a trader. The
      // common case here is simply that the server is not running, so say so.
      var msg = aborted
        ? 'Request timed out after ' + Math.round(budget / 1000) + 's'
        : (/failed to fetch|networkerror|load failed|err_connection/i.test(raw)
            ? 'Backend unreachable — is the server running?'
            : raw);
      return {
        ok: false,
        status: 0,
        data: null,
        error: msg
      };
    });
  }

  function apiGet(path, timeout) {
    return apiRequest(path, { timeout: timeout });
  }

  function apiPost(path, body, timeout) {
    return apiRequest(path, { method: 'POST', body: body || {}, timeout: timeout });
  }

  /* ── Action outcomes ──────────────────────────────────────────────────── */
  /* The broker reports a refused order with **HTTP 200** and a status of its
     own, so `res.ok` says nothing about whether the order happened. A refusal
     arrives as `{"status": "FAILED", "reason": …}` (the broker said no) or
     `{"status": "BLOCKED", "reason": …}` (execution is disabled, so nothing was
     ever sent). Testing for a single sentinel therefore misreports the other,
     and the market-order path tested for nothing at all — so a rejected order
     was announced to the user as submitted. Anything in this set means the
     action did not happen; every other status is left alone, so a status this
     list has not heard of can never turn a real success into a false failure. */
  /* `UNKNOWN` means the broker call timed out and we cannot tell whether the
     order filled. It belongs here, with the refusals, for one reason: the
     alternative is rendering an unconfirmed order as a completed trade. Treat
     "we do not know" as "not confirmed", never as success. */
  var ACTION_REFUSED = { FAILED: 1, BLOCKED: 1, REJECTED: 1, ERROR: 1, UNKNOWN: 1 };

  function actionRefused(data) {
    return !!ACTION_REFUSED[String((data || {}).status || '').toUpperCase()];
  }

  /* One message chain for every action. The market-order path read only
     `error`, but the broker sends `reason` — so a refusal fell through to
     "HTTP 200", which reads as a success code attached to a failure. */
  function actionFailureMessage(prefix, res) {
    var data = (res && res.data) || {};
    return prefix + (data.error || data.reason || (res && res.error) ||
                     ('HTTP ' + (res && res.status)));
  }

  /* ── Toasts ───────────────────────────────────────────────────────────── */
  function toast(message, kind) {
    var host = $('toasts');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'tt-toast tt-toast--' + (kind || 'ok');
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.textContent = String(message);
    host.appendChild(el);
    setTimeout(function () {
      el.style.transition = 'opacity 200ms';
      el.style.opacity = '0';
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }, kind === 'error' ? 7000 : 4000);
  }

  /* ── View / pane switching ────────────────────────────────────────────── */
  function setView(view) {
    if (VIEWS.indexOf(view) === -1) view = 'trade';
    state.view = view;
    document.body.setAttribute('data-view', view);

    // Switching views dismisses any open menu. A panel left hanging over the new
    // view reads as part of it.
    closeDropdown(false);

    Array.prototype.forEach.call(document.querySelectorAll('[data-view-btn]'), function (btn) {
      btn.setAttribute('aria-selected', String(btn.getAttribute('data-view-btn') === view));
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-view-panel]'), function (panel) {
      var active = panel.getAttribute('data-view-panel') === view;
      panel.setAttribute('data-active', String(active));
      if (active) panel.removeAttribute('hidden');
      else panel.setAttribute('hidden', '');
    });

    // iOS large title in the phone nav bar mirrors the active view.
    var lt = $('ios-largetitle');
    if (lt) lt.textContent = view.charAt(0).toUpperCase() + view.slice(1);

    if (view === 'analytics') { loadAnalytics(); }
    if (view === 'backtest') { loadBacktestMeta(); loadJobs(); }
    if (view === 'news') { loadNews(); }
    if (view === 'analyst') { renderDevilAdvocate(); renderQualityGate(); renderObjections(); }
    if (view === 'markets') { loadMarkets(); }
    if (view === 'trade') {
      loadChart();
      // The chart container was display:none while another view was active, so
      // its canvas kept whatever width it had when it was hidden. Resize once
      // the layout has flushed rather than measuring a zero-width element.
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(resizeChart);
    }
  }

  function setPane(pane) {
    if (PANES.indexOf(pane) === -1) pane = 'watchlist';
    state.pane = pane;
    document.body.setAttribute('data-pane', pane);
    Array.prototype.forEach.call(document.querySelectorAll('[data-pane-btn]'), function (btn) {
      btn.setAttribute('aria-selected', String(btn.getAttribute('data-pane-btn') === pane));
    });
    if (pane === 'chart') {
      loadChart();
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(resizeChart);
    }
  }

  /* ── Account strip ────────────────────────────────────────────────────── */
  function renderAccount() {
    var acc = state.account;
    var eq = $('acc-equity'), pf = $('acc-profit'),
        mg = $('acc-margin'), rk = $('acc-risk');

    if (!acc) {
      [eq, pf, mg, rk].forEach(function (el) {
        if (el) { el.textContent = '—'; el.setAttribute('data-state', 'empty'); }
      });
      renderAccountDetails(null);
      return;
    }

    setText(eq, num(acc.equity, 2) + ' ' + (acc.currency || ''));
    flash(eq, acc.equity);

    var profit = Number(acc.profit || 0);
    setText(pf, (profit > 0 ? '+' : '') + num(profit, 2));
    if (pf) pf.className = 'tt-metric__value tt-num ' + signClass(profit);
    flash(pf, profit);

    setText(mg, num(acc.free_margin, 2));

    // Margin level is only meaningful with open exposure; 0 means "no positions".
    var ml = Number(acc.margin_level || 0);
    setText(rk, ml > 0 ? num(ml, 1) + '%' : 'flat');
    if (rk) rk.className = 'tt-metric__value ' + (ml > 0 && ml < 200 ? 'tt-down' : 'tt-flat');

    // A snapshot taken while the broker link is down is LAST-KNOWN, not live.
    // The strip used to present it as current: "broker offline" sat next to a
    // confident equity figure with nothing to say the number was stale.
    var brokerUp = (state.services || {}).MT5 === 'CONNECTED';
    [eq, pf, mg, rk].forEach(function (el) {
      if (!el) return;
      el.setAttribute('data-state', brokerUp ? 'ready' : 'stale');
      if (brokerUp) {
        el.removeAttribute('title');
      } else {
        el.setAttribute('title', 'Last known value - the broker link is down.');
      }
    });

    renderAccountDetails(acc);
  }

  /* The identity behind those figures. The login is the broker account number
     and the only field that says WHICH account the strip is showing, so it sits
     on the strip and the rest of the snapshot hangs off it.

     Every value is copied from the telemetry payload as sent. A field the
     broker did not report reads "—" rather than being defaulted to a
     plausible-looking number: an absent leverage and a real 1:1 are different
     facts, and the panel must not conflate them. */
  function renderAccountDetails(acc) {
    var id = $('acc-id');
    var login = acc && acc.login != null ? String(acc.login) : '—';

    setText(id, login);
    if (id) id.setAttribute('data-state', acc && acc.login != null ? 'ready' : 'empty');

    setText($('acc-d-login'), login);
    setText($('acc-d-name'), acc ? (acc.name || '—') : '—');
    setText($('acc-d-server'), acc ? (acc.server || '—') : '—');
    setText($('acc-d-company'), acc ? (acc.company || '—') : '—');
    setText($('acc-d-sync'), acc ? (acc.last_sync_time || '—') : '—');

    if (!acc) {
      ['acc-d-balance', 'acc-d-equity', 'acc-d-free', 'acc-d-level',
       'acc-d-leverage', 'acc-d-trade'].forEach(function (k) {
        var el = $(k);
        if (el) { el.textContent = '—'; el.removeAttribute('data-state'); }
      });
      return;
    }

    var cur = acc.currency || '';
    var money = function (v) {
      return v == null ? '—' : num(v, 2) + (cur ? ' ' + cur : '');
    };

    setText($('acc-d-balance'), money(acc.balance));
    setText($('acc-d-equity'), money(acc.equity));
    setText($('acc-d-free'), money(acc.free_margin));

    var ml = Number(acc.margin_level || 0);
    var level = $('acc-d-level');
    setText(level, ml > 0 ? num(ml, 1) + '%' : 'flat');
    if (level) {
      // Below 200% is the level a margin call becomes likely, so it earns the
      // warning colour. Zero is not a low margin level - it means no exposure.
      if (ml > 0 && ml < 200) level.setAttribute('data-state', 'warn');
      else level.removeAttribute('data-state');
    }

    setText($('acc-d-leverage'), acc.leverage ? '1:' + acc.leverage : '—');

    // trade_allowed is tri-state: true, false, or absent. Absent must not be
    // rendered as "blocked" - that would invent a restriction the broker never
    // stated - so it gets its own dash.
    var trade = $('acc-d-trade');
    if (trade) {
      if (acc.trade_allowed === true) {
        trade.textContent = 'allowed';
        trade.setAttribute('data-state', 'ok');
      } else if (acc.trade_allowed === false) {
        trade.textContent = 'blocked';
        trade.setAttribute('data-state', 'warn');
      } else {
        trade.textContent = '—';
        trade.removeAttribute('data-state');
      }
    }
  }

  /* ── Dropdowns ────────────────────────────────────────────────────────────
     One delegated controller for every [data-dropdown] on the page.

     Deliberately NOT a MutationObserver. An observer whose callback writes the
     attribute it watches re-queues itself on every pass, and because microtasks
     drain before the browser may paint or dispatch input, that blocks the main
     thread permanently with no error raised - which is exactly how the tab ARIA
     sync in hm_ui.js used to freeze the page on the first nav click. A click is
     a discrete event, so there is nothing here to loop over.

     The open panel is tracked in a variable rather than read back out of the
     DOM, so a panel left open by some future renderer cannot wedge the state. */
  var openDropdown = null;

  function setDropdown(trigger, open) {
    var panel = document.getElementById(trigger.getAttribute('aria-controls'));
    if (!panel) return;
    trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) panel.removeAttribute('hidden');
    else panel.setAttribute('hidden', '');
  }

  function closeDropdown(focusTrigger) {
    if (!openDropdown) return;
    var trigger = openDropdown;
    openDropdown = null;
    setDropdown(trigger, false);
    if (focusTrigger && typeof trigger.focus === 'function') trigger.focus();
  }

  function wireDropdowns() {
    document.addEventListener('click', function (ev) {
      var target = ev.target;
      if (!target || typeof target.closest !== 'function') return;

      var trigger = target.closest('[data-dropdown-trigger]');
      if (trigger) {
        ev.preventDefault();
        var willOpen = openDropdown !== trigger;
        closeDropdown(false);
        if (willOpen) { setDropdown(trigger, true); openDropdown = trigger; }
        return;
      }

      // A click inside an open panel is real use - usually a link. Only a click
      // somewhere else dismisses it.
      if (openDropdown && !target.closest('[data-dropdown-panel]')) closeDropdown(false);
    });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && openDropdown) closeDropdown(true);
    });
  }

  /* ── Watchlist ────────────────────────────────────────────────────────── */
  function symbolList() {
    return Object.keys(state.decisions).sort();
  }

  function renderWatchlist() {
    var body = $('watch-body');
    if (!body) return;
    var symbols = symbolList();

    if (!symbols.length) {
      setState(body, 'empty', 'No instruments reporting',
        'Telemetry has not published any decisions yet.');
      setText($('watch-count'), '0');
      return;
    }

    body.removeAttribute('data-state');
    body.innerHTML = '';
    var frag = document.createDocumentFragment();

    symbols.forEach(function (sym) {
      var d = state.decisions[sym] || {};
      var ms = state.marketStatuses[sym] || {};
      var bias = String(d.bias || 'HOLD').toUpperCase();
      var conf = Number(d.model_confidence);
      var rr = Number(d.risk_reward_ratio);

      var tr = document.createElement('tr');
      tr.setAttribute('data-symbol', sym);
      tr.setAttribute('data-clickable', 'true');
      tr.setAttribute('tabindex', '0');
      if (sym === state.symbol) tr.setAttribute('aria-selected', 'true');

      var dirCls = bias === 'BUY' ? 'tt-dir--buy' : (bias === 'SELL' ? 'tt-dir--sell' : 'tt-dir--flat');

      tr.innerHTML =
        '<td><span class="tt-symbol">' + esc(sym) + '</span></td>' +
        '<td><span class="tt-dir ' + dirCls + '">' + esc(bias) + '</span></td>' +
        '<td class="tt-num">' + (isFinite(conf) ? num(conf * 100, 0) + '%' : '—') + '</td>' +
        '<td class="tt-num">' + formatPrice(d.entry_price, sym) + '</td>' +
        '<td class="tt-num">' + (isFinite(rr) ? num(rr, 2) + 'R' : '—') + '</td>' +
        '<td><span class="tt-chip ' + sessionChip(ms.status) + '">' + esc(ms.status || '—') + '</span></td>';

      tr.addEventListener('click', function () { selectSymbol(sym); });
      tr.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectSymbol(sym); }
      });
      frag.appendChild(tr);
    });

    body.appendChild(frag);
    setText($('watch-count'), String(symbols.length));
  }

  function sessionChip(status) {
    var s = String(status || '').toUpperCase();
    if (s === 'OPEN') return 'tt-chip--buy';
    if (s === 'CLOSED') return 'tt-chip--none';
    return 'tt-chip--low';
  }

  function selectSymbol(sym) {
    state.symbol = sym;
    Array.prototype.forEach.call(document.querySelectorAll('#watch-body tr'), function (tr) {
      tr.setAttribute('aria-selected', String(tr.getAttribute('data-symbol') === sym));
    });
    var input = $('ticket-symbol');
    if (input) input.value = sym;
    var title = $('chart-title');
    if (title) title.textContent = sym + ' · ' + state.timeframe;
    renderReasoning();
    prefillTicket();
    loadChart();
    loadSelection();
    updateCopilotFocus();
  }

  /* ── Auto-selection ───────────────────────────────────────────────────── */
  function loadSelection() {
    var host = $('selection-body');
    apiGet('/api/intelligence/auto-selection?refresh=1', TIMEOUT.slow).then(function (res) {
      if (!res.ok || !res.data || res.data.status !== 'OK') {
        state.selection = null;
        setText($('selection-count'), '0');
        setText($('selection-tier'), '—');
        var reason = (res.data && (res.data.error || res.data.status)) ||
                     res.error || ('HTTP ' + res.status);
        // A 503 here means the engine is not attached — an honest, expected state.
        setState(host, res.status === 503 ? 'stale' : 'error',
          res.status === 503 ? 'Selection engine not attached' : 'Selection unavailable',
          String(reason).slice(0, 160));
        return;
      }
      state.selection = res.data;
      renderSelection();
    });
  }

  function renderSelection() {
    var host = $('selection-body');
    var data = state.selection || {};
    var decisions = data.decisions || [];
    var tradeable = decisions.filter(function (d) { return d.is_tradeable; });

    setText($('selection-count'), String(tradeable.length));
    var best = tradeable[0];
    setText($('selection-tier'), best ? (best.confidence_tier || '—') : 'none');
    var tierEl = $('selection-tier');
    if (tierEl) {
      var t = best ? String(best.confidence_tier || '').toLowerCase() : 'none';
      tierEl.className = 'tt-chip tt-chip--' + (['high', 'medium', 'low'].indexOf(t) >= 0 ? t : 'none');
    }

    if (!tradeable.length) {
      setState(host, 'empty', 'No consensus setup',
        decisions.length
          ? decisions.length + ' symbol(s) evaluated, none reached cross-style agreement.'
          : 'No candidates were produced by the scan.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '';
    tradeable.slice(0, 8).forEach(function (d) {
      var card = document.createElement('div');
      card.className = 'tt-pad';
      card.style.borderBottom = '1px solid var(--hm-border-subtle)';
      card.style.cursor = 'pointer';

      var dirCls = String(d.direction).toUpperCase() === 'BUY' ? 'tt-dir--buy' : 'tt-dir--sell';
      var tier = String(d.confidence_tier || '').toLowerCase();
      var tierCls = ['high', 'medium', 'low'].indexOf(tier) >= 0 ? tier : 'none';

      card.innerHTML =
        '<div class="tt-row">' +
          '<span class="tt-symbol">' + esc(d.symbol) + '</span>' +
          '<span class="tt-dir ' + dirCls + '">' + esc(d.direction) + '</span>' +
          '<span class="tt-chip tt-chip--' + tierCls + '">' + esc(d.confidence_tier || '—') + '</span>' +
          '<span class="tt-rail__spacer"></span>' +
          '<span class="tt-num">' + num(d.consensus_score, 1) + '</span>' +
        '</div>' +
        '<div class="tt-row" style="margin-top:4px">' +
          '<span class="tt-hint">' + esc(d.rationale || '') + '</span>' +
        '</div>';

      card.addEventListener('click', function () { selectSymbol(d.symbol); });
      host.appendChild(card);
    });
  }

  /* ── Scanner radar ────────────────────────────────────────────────────── */
  /* Rows come from telemetry's radar_opportunities — the orchestrator's ranked
     candidate list. A candidate the engine considers actionable is marked, so
     the list does not imply that everything in it is tradeable. */
  function renderRadar() {
    var host = $('radar-body');
    if (!host) return;

    var all = state.radar || [];
    var filter = String(state.radarFilter || 'ALL').toUpperCase();
    var rows = all.filter(function (o) {
      if (filter === 'ALL') return true;
      var style = String(o.trade_style || '').toUpperCase();
      if (filter === 'DAY_TRADING') return style === 'DAY_TRADING' || style === 'DAY' || style === 'INTRADAY';
      return style === filter;
    });

    setText($('radar-count'), String(rows.length));

    if (!rows.length) {
      setState(host, 'empty',
        all.length ? 'No setups in this style' : 'No scan published',
        all.length
          ? all.length + ' candidate(s) scanned, none matching the filter.'
          : 'The radar has not published a scan yet.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '';
    var frag = document.createDocumentFragment();

    rows.slice(0, 20).forEach(function (o) {
      var sym = o.symbol || '';
      var actionable = o.is_actionable === true;
      var label = String(o.status_label || o.action || o.decision || '—').toUpperCase();
      var win = Number(o.win_prob !== undefined && o.win_prob !== null ? o.win_prob : o.score);
      var ev = Number(o.ev);

      var card = document.createElement('div');
      card.className = 'tt-radar' + (actionable ? ' tt-radar--live' : '');
      card.setAttribute('data-clickable', 'true');
      card.setAttribute('tabindex', '0');
      card.innerHTML =
        '<div class="tt-radar__top">' +
          '<span class="tt-symbol">' + esc(sym) + '</span>' +
          '<span class="tt-radar__action' + (actionable ? ' is-live' : '') + '">' + esc(label) + '</span>' +
          '<span class="tt-rail__spacer"></span>' +
          '<span class="tt-chip tt-chip--muted">' + esc(o.trade_style || '—') + '</span>' +
        '</div>' +
        '<div class="tt-radar__nums">' +
          '<span>Entry <b>' + formatPrice(o.entry_price, sym) + '</b></span>' +
          '<span>SL <b>' + formatPrice(o.stop_loss, sym) + '</b></span>' +
          '<span>TP <b>' + formatPrice(o.take_profit, sym) + '</b></span>' +
          (o.tp1_price ? '<span title="TP1 Scale-Out (50%)">TP1 <b>' + formatPrice(o.tp1_price, sym) + '</b></span>' : '') +
          (o.tp3_price ? '<span title="TP3 Macro Runner">TP3 <b>' + formatPrice(o.tp3_price, sym) + '</b></span>' : '') +
          '<span>R:R <b>' + num(o.risk_reward_ratio, 2) + '</b></span>' +
          '<span>Win <b>' + (isFinite(win) ? num(win, 0) + '%' : '—') + '</b></span>' +
          '<span>EV <b class="' + signClass(ev) + '">' + (isFinite(ev) && ev > 0 ? '+' : '') + num(ev, 2) + 'R</b></span>' +
        '</div>' +
        '<div class="tt-radar__meta">' +
          esc(o.regime || '—') + ' · ' + esc(o.strategy || '—') +
          (o.confluence_tier ? ' · ' + esc(o.confluence_tier) : '') +
          (o.setup_grade ? ' · ' + esc(o.setup_grade) : '') +
        '</div>';

      card.addEventListener('click', function () { selectSymbol(sym); });
      card.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectSymbol(sym); }
      });
      frag.appendChild(card);
    });

    host.appendChild(frag);
  }

  
  /* Mirror the ticket's controls onto the selected order type. Market keeps the
     BUY/SELL pair; a pending type carries its own direction, so it gets one
     button, and the price field becomes required rather than optional. */
  function syncTicketMode() {
    var sel = $('ticket-type');
    var type = sel ? String(sel.value || 'MARKET').toUpperCase() : 'MARKET';
    var isPending = type !== 'MARKET';
    var marketRow = $('ticket-market-row');
    var pendingRow = $('ticket-pending-row');
    if (marketRow) marketRow.hidden = isPending;
    if (pendingRow) pendingRow.hidden = !isPending;

    var hint = $('ticket-type-hint');
    if (hint) {
      hint.textContent = isPending
        ? 'Fills when price reaches ' + (type.indexOf('STOP') >= 0 ? 'or breaks ' : '')
          + 'the level — a limit waits for a better price, a stop waits for a breakout.'
        : "A market order fills now at the broker's quote.";
    }

    var price = $('ticket-price');
    if (price) {
      price.placeholder = isPending ? 'required' : 'market';
      if (isPending) price.setAttribute('required', 'required');
      else price.removeAttribute('required');
    }

    var place = $('ticket-place');
    if (place) place.textContent = 'Place order';
  }

  function submitPendingOrder() {
    var sym = ($('ticket-symbol').value || '').trim().toUpperCase();
    var type = String($('ticket-type').value || '').toUpperCase();
    var vol = Number($('ticket-volume').value || 0);
    var price = Number($('ticket-price').value);

    if (!sym) { toast('Enter a symbol', 'warn'); return; }
    if (!(vol > 0)) { toast('Enter a volume greater than zero', 'warn'); return; }
    if (!isFinite(price) || price <= 0) { toast('A pending order needs a price', 'warn'); return; }

    var body = { symbol: sym, order_type: type, price: price, volume: vol };
    var slEl = $('ticket-sl');
    var tpEl = $('ticket-tp');
    // Empty means "none" on a new order, so only send a level that was actually typed.
    if (slEl && slEl.value !== '') body.sl = Number(slEl.value);
    if (tpEl && tpEl.value !== '') body.tp = Number(tpEl.value);

    apiPost('/api/action/place_pending_order', body, TIMEOUT.normal).then(function (res) {
      var data = res.data || {};
      if (res.ok && !actionRefused(data)) {
        toast(type.replace('_', ' ').toLowerCase() + ' ' + vol + ' ' + sym + ' @ ' + price + ' placed');
      } else {
        toast(actionFailureMessage('Order rejected: ', res), 'error');
      }
    });
  }

  /* ── Positions panel ────────────────────────────────────────────────── */
  /* Renders the OPEN tab: live positions from state.positions. The History
     and Pending tabs reuse pos-body as their render target and live in
     renderHistoryInPosPanel / renderPendingInPosPanel below; setPosTab
     dispatches between the three and rewrites thead columns per tab. */
  function renderPositions() {
    var body = $('pos-body');
    var positions = state.positions || [];
    setText($('pos-tab-count-open'), String(positions.length));

    if (!positions.length) {
      setState(body, 'empty', 'No open positions', null);
      setText($('pos-total'), '—');
      // The chart's trade overlays belong to this list, so a flat book must
      // clear them rather than leaving stale entry and stop lines on screen.
      refreshChartDecorations();
      return;
    }

    body.removeAttribute('data-state');
    body.innerHTML = '';
    var total = 0;
    var frag = document.createDocumentFragment();

    positions.forEach(function (p) {
      var profit = Number(p.profit || 0);
      total += profit;
      var tr = document.createElement('tr');
      var side = String(p.type || p.side || '').toUpperCase();
      var dirCls = /BUY|LONG/.test(side) ? 'tt-dir--buy' : 'tt-dir--sell';
      var ticket = p.ticket;
      var sl = Number(p.sl);
      var tp = Number(p.tp);
      tr.innerHTML =
        '<td><span class="tt-symbol">' + esc(p.symbol) + '</span></td>' +
        '<td><span class="tt-dir ' + dirCls + '">' + esc(side || '—') + '</span></td>' +
        '<td class="tt-num">' + num(p.volume, 2) + '</td>' +
        '<td class="tt-num">' + formatPrice(p.open_price, p.symbol) + '</td>' +
        '<td class="tt-num">' + formatPrice(p.current_price, p.symbol) + '</td>' +
        '<td class="tt-num">' + (isFinite(sl) && sl > 0 ? formatPrice(sl, p.symbol) : '<span class="tt-muted">—</span>') + '</td>' +
        '<td class="tt-num">' + (isFinite(tp) && tp > 0 ? formatPrice(tp, p.symbol) : '<span class="tt-muted">—</span>') +
          (p.tp1 ? '<span class="tt-muted" style="font-size:10px; display:block;">TP1: ' + formatPrice(p.tp1, p.symbol) + '</span>' : '') +
          (p.milestone_status && p.milestone_status !== 'OPEN' ? '<span class="tt-chip tt-chip--accent" style="font-size:9px; display:inline-block; padding:1px 3px; margin-top:2px;">' + esc(p.milestone_status.replace(/_/g, ' ')) + '</span>' : '') +
        '</td>' +
        '<td class="tt-num tt-pos-pnl ' + signClass(profit) + '">' + (profit > 0 ? '+' : '') + num(profit, 2) + '</td>' +
        '<td class="tt-pos-actions">' +
          (ticket !== undefined && ticket !== null
            ? '<button class="tt-btn tt-btn--sm tt-btn--pos-close" data-pos-close="' + esc(String(ticket)) + '" type="button">Close</button>'
            : '<span class="tt-muted">—</span>') +
        '</td>';
      frag.appendChild(tr);
    });

    body.appendChild(frag);
    Array.prototype.forEach.call(body.querySelectorAll('[data-pos-close]'), function (btn) {
      btn.addEventListener('click', function () {
        closePosition(Number(btn.getAttribute('data-pos-close')));
      });
    });
    var tot = $('pos-total');
    setText(tot, (total > 0 ? '+' : '') + num(total, 2));
    if (tot) tot.className = 'tt-num tt-pos-pnl ' + signClass(total);

    // Overlays follow the position list, not only the candle poll, so a fill or
    // a close redraws immediately instead of at the next chart refresh.
    refreshChartDecorations();
  }

  /* HISTORY tab: closed trades from state.history (loaded by loadHistory). The
     same fields renderHistory (the dedicated history panel) uses, trimmed to
     what fits the positions table: symbol, side, volume, entry, exit, P&L,
     closed time. Open trades carry a (open) tag rather than a fake exit. */
  function renderHistoryInPosPanel() {
    var body = $('pos-body');
    var rows = state.history || [];
    setText($('pos-tab-count-history'), String(rows.length));

    if (!rows.length) {
      if (state.historyLoading) {
        setState(body, 'loading', 'Loading closed trades…', null);
      } else {
        setState(body, 'empty', 'No closed trades', 'The History tab fills up as positions close.');
      }
      return;
    }

    body.removeAttribute('data-state');
    body.innerHTML = rows.map(function (t) {
      var sym = t.symbol || '';
      var side = String(t.action || t.type || t.side || '').toUpperCase();
      var dirCls = /BUY|LONG/.test(side) ? 'tt-dir--buy' : 'tt-dir--sell';
      var pnl = historyPnl(t);
      var entry = Number(t.entry_price);
      var exit = Number(t.exit_price);
      var stamp = t.closed_at || t.exit_time || t.timestamp;
      var closed = !!(t.closed_at || t.exit_time);
      var when = stamp ? String(stamp).replace('T', ' ').replace(/\.\d+.*$/, '').slice(0, 16) : '—';
      return '<tr>' +
        '<td><span class="tt-symbol">' + esc(sym) + '</span></td>' +
        '<td><span class="tt-dir ' + dirCls + '">' + esc(side || '—') + '</span></td>' +
        '<td class="tt-num">' + num(t.volume, 2) + '</td>' +
        '<td class="tt-num">' + (isFinite(entry) && entry > 0 ? formatPrice(entry, sym) : '—') + '</td>' +
        '<td class="tt-num">' + (closed && isFinite(exit) && exit > 0 ? formatPrice(exit, sym) : '<span class="tt-muted">—</span>') + '</td>' +
        '<td class="tt-num tt-pos-pnl ' + (pnl === null ? 'tt-muted' : signClass(pnl)) + '">' +
          (pnl === null ? '—' : (pnl > 0 ? '+' : '') + num(pnl, 2)) + '</td>' +
        '<td class="tt-muted">' + esc(when) + (closed ? '' : ' <span class="tt-muted">(open)</span>') + '</td>' +
        '<td class="tt-pos-actions tt-muted" aria-hidden="true">—</td>' +
        '</tr>';
    }).join('');
  }

  /* PENDING tab: working orders from /api/pending_orders (loaded by
     loadPendingForTab). Each row gets a Cancel button that hits the existing
     /api/action/cancel_pending_order endpoint. */
  function renderPendingInPosPanel() {
    var body = $('pos-body');
    var list = state.posPending || [];
    setText($('pos-tab-count-pending'), String(list.length));

    if (!list.length) {
      setState(body, 'empty', 'No working orders', 'Place a limit or stop from the ticket and it shows up here.');
      return;
    }

    body.removeAttribute('data-state');
    body.innerHTML = list.map(function (o) {
      var sym = o.symbol || '';
      var t = o.type;
      var typeStr = pendingTypeNameLocal(t);
      var dirCls = /BUY/i.test(typeStr) ? 'tt-dir--buy' : (/SELL/i.test(typeStr) ? 'tt-dir--sell' : '');
      var ticket = o.ticket;
      var sl = Number(o.sl);
      var tp = Number(o.tp);
      return '<tr>' +
        '<td><span class="tt-symbol">' + esc(sym) + '</span></td>' +
        '<td><span class="tt-dir ' + dirCls + '">' + esc(typeStr) + '</span></td>' +
        '<td class="tt-num">' + num(o.volume, 2) + '</td>' +
        '<td class="tt-num">' + formatPrice(o.price, sym) + '</td>' +
        '<td class="tt-num">' + (isFinite(sl) && sl > 0 ? formatPrice(sl, sym) : '<span class="tt-muted">—</span>') + '</td>' +
        '<td class="tt-num">' + (isFinite(tp) && tp > 0 ? formatPrice(tp, sym) : '<span class="tt-muted">—</span>') + '</td>' +
        '<td class="tt-muted">' + esc(pendingAge(o)) + '</td>' +
        '<td class="tt-pos-actions">' +
          (ticket !== undefined && ticket !== null
            ? '<button class="tt-btn tt-btn--sm tt-btn--pos-close" data-pos-cancel="' + esc(String(ticket)) + '" type="button">Cancel</button>'
            : '<span class="tt-muted">—</span>') +
        '</td>' +
        '</tr>';
    }).join('');

    Array.prototype.forEach.call(body.querySelectorAll('[data-pos-cancel]'), function (btn) {
      btn.addEventListener('click', function () {
        cancelPendingFromPanel(Number(btn.getAttribute('data-pos-cancel')));
      });
    });
  }

  /* MT5 reports the order type as a numeric enum; the label the trader sees
     is the same lookup the removed list used so the two views agree. */
  function pendingTypeNameLocal(t) {
    var map = { 2: 'BUY LIMIT', 3: 'SELL LIMIT', 4: 'BUY STOP', 5: 'SELL STOP' };
    var n = Number(t);
    if (map[n]) return map[n];
    var s = String(t == null ? '' : t).toUpperCase();
    return s || '—';
  }

  function pendingAge(o) {
    if (!o.time_setup) return '—';
    var ms = Date.now() - Number(o.time_setup) * 1000;
    if (!isFinite(ms) || ms < 0) return '—';
    var m = Math.floor(ms / 60000);
    if (m < 60) return m + 'm';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h ' + (m % 60) + 'm';
    var d = Math.floor(h / 24);
    return d + 'd ' + (h % 24) + 'h';
  }

  /* Cancel a working order from the PENDING tab. The endpoint and confirm
     pattern are shared with the classic terminal. */
  function cancelPendingFromPanel(ticket) {
    if (!ticket) return;
    if (!window.confirm('Cancel working order #' + ticket + '?')) return;
    apiPost('/api/action/cancel_pending_order', { ticket: ticket }, TIMEOUT.normal).then(function (res) {
      var data = res.data || {};
      if (res.ok && !actionRefused(data)) {
        toast('Order #' + ticket + ' cancelled');
        loadPendingForTab();
      } else {
        toast(actionFailureMessage('Cancel failed: ', res), 'error');
      }
    });
  }

  /* Fetch the working orders list. Kept separate from loadHistory so a tab
     switch never blocks on a slow /api/history call. */
  function loadPendingForTab() {
    apiGet('/api/pending_orders', TIMEOUT.normal).then(function (res) {
      if (!res || !res.ok) { state.posPending = []; renderPendingInPosPanel(); return; }
      var list = Array.isArray(res.data) ? res.data : ((res.data && res.data.orders) || []);
      state.posPending = list;
      renderPendingInPosPanel();
    });
  }

  /* Tab dispatcher: rewrites thead columns per tab, shows / hides the total
     and Flatten all (OPEN-only), marks the active tab, and re-renders. */
  function setPosTab(name) {
    state.posTab = name;
    var headCols = {
      open:    '<th>Symbol</th><th>Side</th><th class="tt-num">Vol</th><th class="tt-num">Entry</th>' +
               '<th class="tt-num">Now</th><th class="tt-num">SL</th><th class="tt-num">TP</th>' +
               '<th class="tt-num">P&amp;L</th><th class="tt-pos-actions-col">Actions</th>',
      history: '<th>Symbol</th><th>Side</th><th class="tt-num">Vol</th><th class="tt-num">Entry</th>' +
               '<th class="tt-num">Exit</th><th class="tt-num">P&amp;L</th>' +
               '<th>Closed</th><th class="tt-pos-actions-col">Actions</th>',
      pending: '<th>Symbol</th><th>Type</th><th class="tt-num">Vol</th><th class="tt-num">Price</th>' +
               '<th class="tt-num">SL</th><th class="tt-num">TP</th>' +
               '<th>Age</th><th class="tt-pos-actions-col">Actions</th>'
    };
    var thead = $('pos-thead');
    if (thead) thead.innerHTML = '<tr>' + headCols[name] + '</tr>';

    Array.prototype.forEach.call(document.querySelectorAll('[data-pos-tab]'), function (b) {
      b.setAttribute('aria-selected', b.getAttribute('data-pos-tab') === name ? 'true' : 'false');
    });

    /* OPEN-only controls: the total P&L is a live-book metric and Flatten all
       closes down the live book. Hide on History / Pending so the panel header
       never advertises a control that doesn't apply to the active view. */
    var showOpenOnly = name === 'open';
    var tot = $('pos-total');
    if (tot) tot.parentNode.style.display = showOpenOnly ? '' : 'none';
    var flatten = $('flatten-all');
    if (flatten) flatten.hidden = !showOpenOnly;

    if (name === 'open') renderPositions();
    else if (name === 'history') renderHistoryInPosPanel();
    else if (name === 'pending') renderPendingInPosPanel();
  }

  /* Close one open position by ticket. The server answers HTTP 200 with
     `status` set for a broker refusal, so the HTTP code alone cannot decide the
     outcome. The action endpoint and confirm-then-toast pattern are shared
     with the classic terminal (terminal.js: closePosition). */
  function closePosition(ticket) {
    if (!ticket) return;
    if (!window.confirm('Close position #' + ticket + '?')) return;
    apiPost('/api/action/close_position', { ticket: ticket }, TIMEOUT.normal).then(function (res) {
      var data = res.data || {};
      if (res.ok && !actionRefused(data)) {
        toast('Position #' + ticket + ' closed');
      } else {
        toast(actionFailureMessage('Close failed: ', res), 'error');
      }
    });
  }

  /* ── Reasoning ("why this trade") ─────────────────────────────────────── */
  function renderReasoning() {
    var host = $('reason-body');
    var sym = state.symbol;
    var d = sym ? state.decisions[sym] : null;

    if (!d) {
      setText($('reason-tier'), '—');
      setState(host, 'empty', 'No setup selected',
        'Pick an instrument to see the engine\u2019s reasoning.');
      return;
    }

    var tier = String(d.master_confluence_tier || '').toLowerCase();
    var tierCls = ['high', 'medium', 'low'].indexOf(tier) >= 0 ? tier : 'none';
    var chip = $('reason-tier');
    if (chip) {
      chip.className = 'tt-chip tt-chip--' + tierCls;
      chip.textContent = (d.master_confluence_tier || '—') + ' · ' + (d.master_confluence_score !== undefined ? d.master_confluence_score : '—');
    }

    var rows = [
      ['Decision', String(d.decision || '—') + ' · ' + String(d.bias || '—')],
      ['Strategy', d.strategy || '—'],
      ['Regime', (d.regime && d.regime.primary) || '—'],
      ['Regime conf', d.regime && d.regime.confidence !== undefined ? pct(d.regime.confidence, 0) : '—'],
      ['Entry', d.entry_price !== undefined ? num(d.entry_price, 5) : '—'],
      ['Stop', d.stop_loss !== undefined ? num(d.stop_loss, 5) : '—'],
      ['Target', d.take_profit !== undefined ? num(d.take_profit, 5) : '—'],
      ['R:R', d.risk_reward_ratio !== undefined ? num(d.risk_reward_ratio, 2) + 'R' : '—'],
      ['Risk', d.calculated_risk_percent !== undefined ? num(d.calculated_risk_percent, 2) + '%' : '—'],
      ['Expected value', d.expected_value !== undefined ? num(d.expected_value, 2) : '—'],
      ['Model confidence', d.model_confidence !== undefined ? pct(d.model_confidence, 1) : '—'],
      ['Dissection', String(d.dissection_tier || '—') + ' (' + (d.dissection_score !== undefined ? d.dissection_score : '—') + ')'],
      ['Adversarial penalty', d.adversarial_penalty !== undefined ? num(d.adversarial_penalty, 1) : '—'],
      ['Gate policy', d.gate_policy_decision || '—'],
      ['Authorised', d.execution_authorized ? 'yes' : 'no'],
      ['Sample size', d.pattern_sample_size !== undefined ? d.pattern_sample_size : '—'],
      ['Updated', d.timestamp || '—']
    ];

    host.removeAttribute('data-state');
    host.innerHTML = '';
    var ul = document.createElement('ul');
    ul.className = 'tt-reasons';
    rows.forEach(function (pair) {
      var li = document.createElement('li');
      var k = document.createElement('span');
      k.className = 'tt-reasons__key';
      k.textContent = pair[0];
      var v = document.createElement('span');
      v.className = 'tt-reasons__val';
      v.textContent = pair[1];
      li.appendChild(k);
      li.appendChild(v);
      ul.appendChild(li);
    });
    host.appendChild(ul);

    // Probability split, when the engine published one.
    var probs = d.probabilities;
    if (probs && typeof probs === 'object') {
      var bar = document.createElement('div');
      bar.className = 'tt-pad';
      bar.innerHTML =
        '<div class="tt-row" style="justify-content:space-between">' +
          '<span class="tt-hint">BUY ' + pct(probs.buy, 0) + '</span>' +
          '<span class="tt-hint">NO TRADE ' + pct(probs.no_trade, 0) + '</span>' +
          '<span class="tt-hint">SELL ' + pct(probs.sell, 0) + '</span>' +
        '</div>';
      host.appendChild(bar);
    }

    // The context strip reads the same decision, so it follows this render.
    renderContextAnalyst();
  }

  /* ── Ticket prefill ───────────────────────────────────────────────────── */
  function prefillTicket() {
    var d = state.symbol ? state.decisions[state.symbol] : null;
    var src = $('ticket-source');
    if (!d) {
      if (src) src.textContent = 'manual';
      return;
    }
    if (src) src.textContent = 'from setup';
    var set = function (id, val) {
      var el = $(id);
      if (el && val !== undefined && val !== null && val !== '') el.value = val;
    };
    set('ticket-price', d.entry_price);
    set('ticket-sl', d.stop_loss);
    set('ticket-tp', d.take_profit);

    var hint = $('ticket-hint');
    if (hint) {
      hint.textContent = d.execution_authorized
        ? 'Engine authorised this setup. Review before sending.'
        : 'Engine has NOT authorised this setup (' + (d.gate_policy_decision || 'blocked') + '). Manual entry only.';
      hint.className = d.execution_authorized ? 'tt-hint' : 'tt-hint tt-down';
    }
  }

  /* ── Chart ────────────────────────────────────────────────────────────────
     One lightweight-charts instance per session, rebuilt only when the
     container it was built into goes away.

     Four rules keep it honest and usable:

     1. LIVE TICKS UPDATE, THEY DO NOT RELOAD. setData() resets the viewport, so
        a trader who scrolled back to a level is thrown forward on every poll.
        The first paint uses setData(); later ticks call update() on the forming
        bar, which leaves scroll and zoom exactly where the user put them.

     2. LEVELS ARE DERIVED, NEVER INVENTED. Support and resistance come from
        swing pivots in the candles actually on screen. When the series is too
        short to contain a pivot on a side, that level is simply not drawn and
        the legend says so — a synthetic level reads as a real price and someone
        will trade against it.

     3. OVERLAYS ARE THE TRADE. Entry, stop and target are drawn as price lines
        whose axis labels carry the side, size and price, so the geometry of an
        open position is legible on the chart itself rather than only in the
        table beneath it.

     4. THE CHART IS THE SAME DATA AS THE TABLE. Candle precision is resolved
        per symbol by the same rule the backend uses, so a price read off the
        axis and the same price read off a row agree digit for digit.
     ────────────────────────────────────────────────────────────────────────── */

  var CHART_COLORS = {
    up: '#00f59b',
    down: '#ff3b5c',
    resistance1: '#ff2a5f',
    resistance2: '#f43f5e',
    support1: '#00f59b',
    support2: '#10b981',
    entryBuy: '#00d4ff',
    entrySell: '#c084fc',
    stopAtRisk: '#ff0055',
    stopLocked: '#fbbf24',
    target: '#00ff88',
    tp1: '#38bdf8',
    tp2: '#10b981',
    tp3: '#c084fc',
    navUpper: '#f43f5e',
    navBasis: '#3b82f6',
    navLower: '#10b981',
    navQuarter: 'rgba(148, 163, 184, 0.45)'
  };

  /* Build the chart once. Returns null when the library or the container is
     missing, so every caller can bail rather than throw into a poll loop. */
  function ensureChart() {
    var host = $('chart');
    if (!host) return null;
    if (typeof LightweightCharts === 'undefined') return null;
    if (state.chart && state.chart.host === host && host.contains(state.chart.chart.chartElement())) {
      return state.chart;
    }

    host.innerHTML = '';
    var chart = LightweightCharts.createChart(host, {
      width: host.clientWidth || 600,
      height: host.clientHeight || 320,
      layout: {
        background: { color: 'transparent' },
        textColor: '#8494ab',
        fontSize: 11,
        fontFamily: "'JetBrains Mono', 'Roboto Mono', Consolas, monospace"
      },
      grid: {
        vertLines: { color: 'rgba(148,163,184,0.06)' },
        horzLines: { color: 'rgba(148,163,184,0.06)' }
      },
      rightPriceScale: {
        borderColor: 'rgba(148,163,184,0.15)',
        scaleMargins: { top: 0.08, bottom: 0.24 }
      },
      timeScale: {
        borderColor: 'rgba(148,163,184,0.15)',
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 4
      },
      crosshair: {
        mode: 1,
        vertLine: { color: 'rgba(56,189,248,0.5)', width: 1, style: 3, labelBackgroundColor: '#1a2438' },
        horzLine: { color: 'rgba(56,189,248,0.5)', width: 1, style: 3, labelBackgroundColor: '#1a2438' }
      },
      localization: { priceFormatter: function (p) { return p.toFixed(priceDigits(state.chartSymbol || state.symbol, p)); } }
    });

    // v4 exposes addCandlestickSeries(); v5 replaced it with addSeries(type).
    // Support both so a vendored-library bump does not silently blank the chart.
    var candleOpts = {
      upColor: CHART_COLORS.up, downColor: CHART_COLORS.down,
      borderUpColor: CHART_COLORS.up, borderDownColor: CHART_COLORS.down,
      wickUpColor: CHART_COLORS.up, wickDownColor: CHART_COLORS.down
    };
    var candleSeries = (typeof chart.addCandlestickSeries === 'function')
      ? chart.addCandlestickSeries(candleOpts)
      : chart.addSeries(LightweightCharts.CandlestickSeries, candleOpts);

    // Volume shares the price pane on its own overlay scale pinned to the
    // bottom fifth. lightweight-charts v4 has no pane API, and a second chart
    // instance would need its own time axis kept in sync by hand.
    var volumeSeries = null;
    if (typeof chart.addHistogramSeries === 'function') {
      volumeSeries = chart.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
        lastValueVisible: false,
        priceLineVisible: false
      });
      chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    }

    state.chart = {
      host: host,
      chart: chart,
      candles: candleSeries,
      volume: volumeSeries,
      lines: [],
      tradeLines: []
    };

    // ResizeObserver is what the layout actually drives: the window listener
    // alone misses pane switches and view changes, which resize without a
    // window event and leave the canvas at its old width.
    if (typeof ResizeObserver !== 'undefined') {
      var ro = new ResizeObserver(function () {
        resizeChart();
      });
      ro.observe(host);
    }

    subscribeCrosshair(chart, candleSeries);
    return state.chart;
  }

  function resizeChart() {
    if (!state.chart) return;
    var host = state.chart.host;
    var w = host.clientWidth || 600;
    var h = host.clientHeight || 320;
    state.chart.chart.applyOptions({ width: w, height: h });
  }

  /* Crosshair readout. Beyond OHLC this names the level the bar is testing,
     which is the reason the levels are drawn at all. */
  function subscribeCrosshair(chart, candleSeries) {
    chart.subscribeCrosshairMove(function (param) {
      var tip = $('chart-tooltip');
      if (!tip) return;
      if (!param || !param.time || !param.seriesData || !param.point) {
        tip.hidden = true;
        return;
      }
      var bar = param.seriesData.get(candleSeries);
      if (!bar) { tip.hidden = true; return; }

      var sym = state.chartSymbol || state.symbol || '';
      var digits = priceDigits(sym, bar.close);
      var lv = state.chartLevels;

      var tag = 'Mid-range';
      var tagCls = ' tt-tip__tag--flat';
      if (lv) {
        if (lv.r1 !== null && bar.high >= lv.r1) { tag = 'Testing R1 resistance'; tagCls = ' tt-tip__tag--down'; }
        else if (lv.r2 !== null && bar.high >= lv.r2) { tag = 'Testing R2 resistance'; tagCls = ' tt-tip__tag--down'; }
        else if (lv.s1 !== null && bar.low <= lv.s1) { tag = 'Testing S1 support'; tagCls = ' tt-tip__tag--up'; }
        else if (lv.s2 !== null && bar.low <= lv.s2) { tag = 'Testing S2 support'; tagCls = ' tt-tip__tag--up'; }
      }

      var when = typeof param.time === 'number'
        ? new Date(param.time * 1000).toISOString().replace('T', ' ').slice(0, 16)
        : String(param.time);

      tip.innerHTML =
        '<div class="tt-tip__head"><span>' + esc(sym) + ' · ' + esc(state.timeframe) + '</span><span>' + esc(when) + '</span></div>' +
        '<div class="tt-tip__row"><span>Open</span><b>' + num(bar.open, digits) + '</b></div>' +
        '<div class="tt-tip__row"><span>High</span><b>' + num(bar.high, digits) + '</b></div>' +
        '<div class="tt-tip__row"><span>Low</span><b>' + num(bar.low, digits) + '</b></div>' +
        '<div class="tt-tip__row"><span>Close</span><b class="' + (bar.close >= bar.open ? 'tt-up' : 'tt-down') + '">' + num(bar.close, digits) + '</b></div>' +
        '<div class="tt-tip__tag' + tagCls + '">' + esc(tag) + '</div>';

      tip.hidden = false;
      // Flip the card to the left of the cursor when it would overflow the
      // panel, and clamp vertically so it never leaves the chart area.
      var host = state.chart ? state.chart.host : null;
      var hw = host ? host.clientWidth : 600;
      var x = param.point.x;
      var left = x > hw - 210 ? Math.max(4, x - 206) : x + 14;
      tip.style.left = left + 'px';
      tip.style.top = Math.max(4, param.point.y - 48) + 'px';
    });
  }

  /* Swing-pivot support and resistance.

     A pivot high is a bar whose high exceeds the two bars either side of it; a
     pivot low is the mirror. The two nearest pivots above the last close become
     R1 and R2, the two nearest below become S1 and S2 — the same definition the
     terminal used before the redesign, so a level a trader remembers still
     lands in the same place.

     Returns null rather than a synthesised level when the series is too short
     or has no pivot on either side. */
  function computeLevels(candles) {
    if (!candles || candles.length < 12) return null;

    var highs = [];
    var lows = [];
    for (var i = 2; i < candles.length - 2; i++) {
      var c = candles[i];
      if (c.high > candles[i - 1].high && c.high > candles[i - 2].high &&
          c.high > candles[i + 1].high && c.high > candles[i + 2].high) highs.push(c.high);
      if (c.low < candles[i - 1].low && c.low < candles[i - 2].low &&
          c.low < candles[i + 1].low && c.low < candles[i + 2].low) lows.push(c.low);
    }

    var last = candles[candles.length - 1].close;
    var above = highs.filter(function (h) { return h > last; }).sort(function (a, b) { return a - b; });
    var below = lows.filter(function (l) { return l < last; }).sort(function (a, b) { return b - a; });

    var r1 = above.length > 0 ? above[0] : null;
    var r2 = above.length > 1 ? above[1] : null;
    var s1 = below.length > 0 ? below[0] : null;
    var s2 = below.length > 1 ? below[1] : null;

    // ── Indicator 1: KN - Smart TP SL Signals ─────────────────────────────────
    var calcEMA = function (arr, p) {
      if (!arr || !arr.length) return 0;
      var k = 2 / (p + 1);
      var ema = arr[0].close;
      for (var idx = 1; idx < arr.length; idx++) {
        ema = arr[idx].close * k + ema * (1 - k);
      }
      return ema;
    };

    var calcATR = function (arr, p) {
      if (!arr || arr.length < 2) return (last * 0.002);
      var len = Math.min(p || 14, arr.length - 1);
      var sum = 0;
      for (var idx = arr.length - len; idx < arr.length; idx++) {
        var cur = arr[idx], prev = arr[idx - 1];
        sum += Math.max(cur.high - cur.low, Math.abs(cur.high - prev.close), Math.abs(cur.low - prev.close));
      }
      return sum / len;
    };

    var fastEMA = calcEMA(candles, 5);
    var slowEMA = calcEMA(candles, 13);
    var atr14 = calcATR(candles, 14);
    var knBullish = fastEMA >= slowEMA;
    var knSignal = knBullish ? 'BULLISH' : 'BEARISH';
    var knSL = knBullish ? (last - 1.5 * atr14) : (last + 1.5 * atr14);
    var knRisk = Math.abs(last - knSL);
    var knTP1 = knBullish ? (last + 1.0 * knRisk) : (last - 1.0 * knRisk);
    var knTP2 = knBullish ? (last + 2.0 * knRisk) : (last - 2.0 * knRisk);
    var knTP3 = knBullish ? (last + 3.0 * knRisk) : (last - 3.0 * knRisk);

    // ── Indicator 2: Trend Channel Navigator (AQDC) ───────────────────────────
    var w = 5;
    var sh = null, sl = null;
    for (var k = candles.length - 1 - w; k >= w; k--) {
      var bar = candles[k];
      var isH = true, isL = true;
      for (var j = 1; j <= w; j++) {
        if (candles[k - j].high >= bar.high || candles[k + j].high > bar.high) isH = false;
        if (candles[k - j].low <= bar.low || candles[k + j].low < bar.low) isL = false;
      }
      if (isH && !sh) sh = { index: k, price: bar.high };
      if (isL && !sl) sl = { index: k, price: bar.low };
      if (sh && sl) break;
    }
    if (!sh || !sl) {
      var slc = candles.slice(-Math.min(30, candles.length));
      var mx = -Infinity, mn = Infinity, imx = 0, imn = 0;
      slc.forEach(function (b, i) {
        if (b.high > mx) { mx = b.high; imx = candles.length - slc.length + i; }
        if (b.low < mn) { mn = b.low; imn = candles.length - slc.length + i; }
      });
      sh = { index: imx, price: mx };
      sl = { index: imn, price: mn };
    }

    var i1 = Math.min(sh.index, sl.index);
    var i2 = Math.max(sh.index, sl.index);
    var p1 = i1 === sh.index ? sh.price : sl.price;
    var p2 = i2 === sh.index ? sh.price : sl.price;
    var slope = (p2 - p1) / Math.max(i2 - i1, 1);
    var curIdx = candles.length - 1;
    var curBasis = p1 + slope * (curIdx - i1);

    var upDevs = [], lowDevs = [];
    for (var idx = i1; idx <= curIdx; idx++) {
      var bVal = p1 + slope * (idx - i1);
      if (candles[idx].high > bVal) upDevs.push(candles[idx].high - bVal);
      if (candles[idx].low < bVal) lowDevs.push(bVal - candles[idx].low);
    }
    upDevs.sort(function (a, b) { return a - b; });
    lowDevs.sort(function (a, b) { return a - b; });

    var p90 = function (arr, fallback) {
      if (!arr.length) return fallback;
      var pos = Math.min(arr.length - 1, Math.floor(arr.length * 0.90));
      return arr[pos];
    };

    var chUpper = curBasis + p90(upDevs, atr14 * 1.5);
    var chLower = curBasis - p90(lowDevs, atr14 * 1.5);
    var chSpan = Math.max(chUpper - chLower, 1e-9);
    var posPct = Math.max(0, Math.min(100, ((last - chLower) / chSpan) * 100.0));
    var quarter = posPct <= 25.0 ? 'LOWER_QUARTER' : (posPct >= 75.0 ? 'UPPER_QUARTER' : 'MIDDLE');
    var q25 = chLower + 0.25 * chSpan;
    var q75 = chLower + 0.75 * chSpan;

    // Recency-weighted Micro Regression (N=20, lambda=0.94)
    var nMicro = Math.min(20, candles.length);
    var decay = 0.94;
    var sumW = 0, sumWX = 0, sumWY = 0;
    var mClose = candles.slice(-nMicro);
    for (var m = 0; m < nMicro; m++) {
      var wgt = Math.pow(decay, nMicro - 1 - m);
      sumW += wgt;
      sumWX += wgt * m;
      sumWY += wgt * mClose[m].close;
    }
    var meanX = sumWX / sumW;
    var meanY = sumWY / sumW;
    var covXY = 0, varX = 0, varY = 0;
    for (var m = 0; m < nMicro; m++) {
      var dx = m - meanX;
      var dy = mClose[m].close - meanY;
      var wgt = Math.pow(decay, nMicro - 1 - m);
      covXY += wgt * dx * dy;
      varX += wgt * dx * dx;
      varY += wgt * dy * dy;
    }
    var mSlope = covXY / (varX + 1e-9);
    var mR2 = (varX * varY > 1e-12) ? Math.max(0, Math.min(1, (covXY * covXY) / (varX * varY + 1e-9))) : 0;
    var angle = Math.atan(mSlope / (atr14 + 1e-9)) * (180 / Math.PI);

    return {
      r1: r1,
      r2: r2,
      s1: s1,
      s2: s2,
      // Indicator 1: KN Smart TP SL
      kn_signal: knSignal,
      kn_fast_ema: fastEMA,
      kn_slow_ema: slowEMA,
      kn_atr: atr14,
      kn_sl: knSL,
      kn_tp1: knTP1,
      kn_tp2: knTP2,
      kn_tp3: knTP3,
      // Indicator 2: Trend Channel Navigator (AQDC)
      channel_basis: curBasis,
      channel_upper: chUpper,
      channel_lower: chLower,
      quarter_25: q25,
      quarter_75: q75,
      channel_quarter: quarter,
      channel_pos_pct: posPct,
      micro_r2: mR2,
      micro_slope_angle: angle
    };
  }

  /* Remove one bucket of price lines. removePriceLine throws if the line was
     already detached (which happens when the series is reset), so each call is
     individually guarded rather than aborting the sweep. */
  function clearLines(bucket) {
    if (!state.chart || !state.chart[bucket]) return;
    var series = state.chart.candles;
    state.chart[bucket].forEach(function (line) {
      try { series.removePriceLine(line); } catch (e) { /* already detached */ }
    });
    state.chart[bucket] = [];
  }

  /* The legend chips for the derived levels and institutional indicators. */
  function levelChipsHtml(digits) {
    var lv = state.chartLevels;
    if (!lv) return '';
    var defs = [
      ['r1', 'R1'], ['r2', 'R2'], ['s1', 'S1'], ['s2', 'S2']
    ];
    var chips = [];
    defs.forEach(function (d) {
      var price = lv[d[0]];
      if (price === null || price === undefined) return;
      chips.push('<span class="tt-level tt-level--' + d[1].charAt(0).toLowerCase() + '">' +
                 '<i aria-hidden="true"></i>' + d[1] + ' <b>' + num(price, digits) + '</b></span>');
    });

    // Indicator 1 & 2 legend badges
    if (lv.channel_quarter) {
      var navCol = lv.channel_quarter === 'LOWER_QUARTER' ? '#10b981' : (lv.channel_quarter === 'UPPER_QUARTER' ? '#f43f5e' : '#3b82f6');
      chips.push('<span class="tt-level" style="border-color:' + navCol + ';color:' + navCol + '">' +
                 '<i aria-hidden="true"></i>NAVIGATOR: <b>' + esc(lv.channel_quarter) + '</b> (R² ' + num(lv.micro_r2, 2) + ')</span>');
    }
    if (lv.kn_signal) {
      var knCol = lv.kn_signal === 'BULLISH' ? '#10b981' : '#f43f5e';
      chips.push('<span class="tt-level" style="border-color:' + knCol + ';color:' + knCol + '">' +
                 '<i aria-hidden="true"></i>KN SMART: <b>' + esc(lv.kn_signal) + '</b></span>');
    }

    return chips.join('');
  }

  function drawLevels(sym, digits) {
    if (!state.chart) return;
    clearLines('lines');

    var legend = $('chart-legend');
    var lv = state.chartLevels;

    if (!lv || !state.showLevels) {
      if (legend) legend.innerHTML = '';
      return;
    }

    var defs = [
      ['r1', CHART_COLORS.resistance1, 2.5, 'Solid', 'R1'],
      ['r2', CHART_COLORS.resistance2, 1.5, 'Dashed', 'R2'],
      ['s1', CHART_COLORS.support1, 2.5, 'Solid', 'S1'],
      ['s2', CHART_COLORS.support2, 1.5, 'Dashed', 'S2']
    ];

    defs.forEach(function (d) {
      var price = lv[d[0]];
      if (price === null || price === undefined) return;
      state.chart.lines.push(state.chart.candles.createPriceLine({
        price: price,
        color: d[1],
        lineWidth: d[2],
        lineStyle: d[3] === 'Solid' ? LightweightCharts.LineStyle.Solid : LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: d[4] + ': ' + num(price, digits)
      }));
    });

    // ── Trend Channel Navigator (AQDC) Price Lines ──────────────────────────
    if (lv.channel_upper && lv.channel_basis && lv.channel_lower) {
      state.chart.lines.push(state.chart.candles.createPriceLine({
        price: lv.channel_upper,
        color: CHART_COLORS.navUpper,
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
        title: 'NAVIGATOR UPPER (90%): ' + num(lv.channel_upper, digits)
      }));
      state.chart.lines.push(state.chart.candles.createPriceLine({
        price: lv.channel_basis,
        color: CHART_COLORS.navBasis,
        lineWidth: 1.5,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
        title: 'NAVIGATOR BASIS: ' + num(lv.channel_basis, digits)
      }));
      state.chart.lines.push(state.chart.candles.createPriceLine({
        price: lv.channel_lower,
        color: CHART_COLORS.navLower,
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
        title: 'NAVIGATOR LOWER (90%): ' + num(lv.channel_lower, digits)
      }));
      if (lv.quarter_75) {
        state.chart.lines.push(state.chart.candles.createPriceLine({
          price: lv.quarter_75,
          color: CHART_COLORS.navQuarter,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: false,
          title: 'PULLBACK ZONE (75%): ' + num(lv.quarter_75, digits)
        }));
      }
      if (lv.quarter_25) {
        state.chart.lines.push(state.chart.candles.createPriceLine({
          price: lv.quarter_25,
          color: CHART_COLORS.navQuarter,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: false,
          title: 'PULLBACK ZONE (25%): ' + num(lv.quarter_25, digits)
        }));
      }
    }

    if (legend) {
      var chips = levelChipsHtml(digits);
      legend.innerHTML = chips || '<span class="tt-hint">No swing pivot in range</span>';
    }
  }

  /* Active Entry, stop loss and take profit targets (TP1, TP2, TP3 smart milestones)
     for the active trade or dynamic indicator targets when no position is open. */
  function drawTradeOverlays(sym, digits) {
    var mine = (state.positions || []).filter(function (p) {
      return String(p.symbol || '').toUpperCase() === String(sym || '').toUpperCase();
    });

    // The card is rendered from telemetry, not from the candles, so it is drawn
    // whether or not there is a native series to hang price lines off.
    renderTradeHud(mine);

    if (!state.chart) return;
    clearLines('tradeLines');
    if (!state.showLevels) return;

    var series = state.chart.candles;
    var lv = state.chartLevels;

    if (mine.length) {
      // ── Active Open Position Overlays ───────────────────────────────────────
      mine.forEach(function (pos) {
        var side = String(pos.type || pos.side || '').toUpperCase();
        var isBuy = side === 'BUY' || side === 'LONG';
        var entry = Number(pos.open_price || 0);
        var sl = Number(pos.sl || 0);
        var tp = Number(pos.tp || 0);
        var lots = Number(pos.volume || 0);
        var lotText = isFinite(lots) ? lots.toFixed(2) : '—';

        if (entry > 0) {
          state.chart.tradeLines.push(series.createPriceLine({
            price: entry,
            color: isBuy ? CHART_COLORS.entryBuy : CHART_COLORS.entrySell,
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Solid,
            axisLabelVisible: true,
            title: (isBuy ? 'BUY' : 'SELL') + ' ' + lotText + 'L @ ' + num(entry, digits)
          }));
        }
        if (sl > 0) {
          var locked = isBuy ? sl >= entry : sl <= entry;
          state.chart.tradeLines.push(series.createPriceLine({
            price: sl,
            color: locked ? CHART_COLORS.stopLocked : CHART_COLORS.stopAtRisk,
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: (locked ? 'SL locked' : 'SL') + ': ' + num(sl, digits)
          }));
        }
        if (tp > 0) {
          state.chart.tradeLines.push(series.createPriceLine({
            price: tp,
            color: CHART_COLORS.target,
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'TP: ' + num(tp, digits)
          }));
        }

        // Smart Milestones TP1, TP2, TP3
        var tp1 = Number(pos.tp1 || 0);
        var tp2 = Number(pos.tp2 || 0);
        var tp3 = Number(pos.tp3 || 0);
        var risk = (entry > 0 && sl > 0) ? Math.abs(entry - sl) : 0;

        if (tp1 > 0) {
          state.chart.tradeLines.push(series.createPriceLine({
            price: tp1,
            color: CHART_COLORS.tp1,
            lineWidth: 1.5,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'TP1 (50% Cash): ' + num(tp1, digits)
          }));
        } else if (risk > 0 && tp > 0) {
          var calcTp1 = isBuy ? entry + risk : entry - risk;
          state.chart.tradeLines.push(series.createPriceLine({
            price: calcTp1,
            color: CHART_COLORS.tp1,
            lineWidth: 1.5,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'TP1 (1.0R Cash): ' + num(calcTp1, digits)
          }));
        }

        if (tp2 > 0 && Math.abs(tp2 - tp) > 1e-6) {
          state.chart.tradeLines.push(series.createPriceLine({
            price: tp2,
            color: CHART_COLORS.tp2,
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'TP2: ' + num(tp2, digits)
          }));
        }

        if (tp3 > 0) {
          state.chart.tradeLines.push(series.createPriceLine({
            price: tp3,
            color: CHART_COLORS.tp3,
            lineWidth: 1.5,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'TP3 (Runner): ' + num(tp3, digits)
          }));
        } else if (risk > 0) {
          var calcTp3 = isBuy ? entry + (risk * 3.0) : entry - (risk * 3.0);
          state.chart.tradeLines.push(series.createPriceLine({
            price: calcTp3,
            color: CHART_COLORS.tp3,
            lineWidth: 1.5,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'TP3 (3.0R Runner): ' + num(calcTp3, digits)
          }));
        }
      });
    } else if (lv && lv.kn_sl) {
      // ── KN Smart Signals Dynamic Levels (when no active position) ───────────
      state.chart.tradeLines.push(series.createPriceLine({
        price: lv.kn_sl,
        color: CHART_COLORS.stopAtRisk,
        lineWidth: 1.5,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: 'KN SMART SL: ' + num(lv.kn_sl, digits)
      }));
      state.chart.tradeLines.push(series.createPriceLine({
        price: lv.kn_tp1,
        color: CHART_COLORS.tp1,
        lineWidth: 1.5,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: 'KN SMART TP1 (1.0R): ' + num(lv.kn_tp1, digits)
      }));
      state.chart.tradeLines.push(series.createPriceLine({
        price: lv.kn_tp2,
        color: CHART_COLORS.tp2,
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: 'KN SMART TP2 (2.0R): ' + num(lv.kn_tp2, digits)
      }));
      state.chart.tradeLines.push(series.createPriceLine({
        price: lv.kn_tp3,
        color: CHART_COLORS.tp3,
        lineWidth: 1.5,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: 'KN SMART TP3 (3.0R): ' + num(lv.kn_tp3, digits)
      }));
    }
  }

  /* The floating trade card over the chart. Sourced from the broker's position
     record rather than from the price series, so it remains correct in
     TradingView mode where the candles are drawn by an external widget. */
  function renderTradeHud(mine) {
    var hud = $('chart-hud');
    if (!hud) return;
    if (!state.showLevels || !mine || !mine.length) { hud.hidden = true; return; }

    var p = mine[0];
    var pSide = String(p.type || p.side || '').toUpperCase();
    var pBuy = pSide === 'BUY' || pSide === 'LONG';
    var pnl = Number(p.profit || 0);
    var lots2 = Number(p.volume || 0);

    hud.hidden = false;
    hud.className = 'tt-chart__hud ' + (pBuy ? 'tt-chart__hud--buy' : 'tt-chart__hud--sell');
    hud.innerHTML =
      '<div class="tt-chart__hud-top">' +
        '<span class="tt-chart__hud-side">' + esc(pSide || '—') + '</span>' +
        '<span class="tt-num">' + (isFinite(lots2) ? lots2.toFixed(2) : '—') + 'L</span>' +
        (p.ticket !== undefined && p.ticket !== null ? '<span class="tt-chart__hud-ticket">#' + esc(p.ticket) + '</span>' : '') +
      '</div>' +
      '<div class="tt-chart__hud-grid">' +
        '<span>Entry</span><b class="tt-num">' + formatPrice(p.open_price, p.symbol) + '</b>' +
        '<span>Stop</span><b class="tt-num">' + (Number(p.sl || 0) > 0 ? formatPrice(p.sl, p.symbol) : 'none') + '</b>' +
        '<span>Target</span><b class="tt-num">' + (Number(p.tp || 0) > 0 ? formatPrice(p.tp, p.symbol) : 'none') + '</b>' +
        '<span>P&amp;L</span><b class="tt-num ' + signClass(pnl) + '">' + (pnl > 0 ? '+' : '') + num(pnl, 2) + '</b>' +
      '</div>' +
      (mine.length > 1
        ? '<div class="tt-chart__hud-more">+' + (mine.length - 1) + ' more on ' + esc(p.symbol || '') + '</div>'
        : '');
  }

  function updateChartHeader(sym, candles) {
    var title = $('chart-title');
    if (title) title.textContent = sym + ' · ' + state.timeframe;

    var el = $('chart-live-price');
    var last = candles[candles.length - 1];
    if (!el || !last) return;
    el.textContent = num(last.close, priceDigits(sym, last.close));
    el.className = 'tt-num tt-num--lg tt-chart__price ' + (last.close >= last.open ? 'tt-up' : 'tt-down');
    el.setAttribute('data-state', 'ready');
  }

  /* ── Entry / exit markers ───────────────────────────────────────────── */
  /* Timestamps on this page are UTC. "2026-09-11 08:09:50" carries no zone
     marker and Date.parse reads a zoneless string as *local* time, which would
     shift every marker by the machine's UTC offset. Force UTC unless the string
     already says otherwise. */
  function utcSeconds(value) {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value === 'number') return isFinite(value) ? Math.round(value) : null;
    var s = String(value).trim();
    if (!s) return null;
    var hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(s);
    if (s.indexOf('T') < 0) s = s.replace(' ', 'T');
    if (!hasZone) s += 'Z';
    var ms = Date.parse(s);
    return isFinite(ms) ? Math.round(ms / 1000) : null;
  }

  /* A marker can only sit on a bar the series actually contains, so an event is
     snapped to the last bar at or before it. An event older than the whole
     loaded window returns null rather than being clamped onto the first bar —
     drawing it there would state a time that is not true. */
  function barTimeAt(seconds) {
    var bars = state.chartCandles || [];
    if (!bars.length || !isFinite(seconds)) return null;
    var found = null;
    for (var i = 0; i < bars.length; i++) {
      var t = Number(bars[i].time);
      if (!isFinite(t)) continue;
      if (t <= seconds) found = t;
      else break;
    }
    return found;
  }

  function drawTradeMarkers(sym, digits) {
    if (!state.chart || !state.chart.candles) return;
    var series = state.chart.candles;
    if (typeof series.setMarkers !== 'function') return;

    var symU = String(sym || '').toUpperCase();
    var markers = [];

    if (state.showLevels) {
      (state.positions || []).forEach(function (pos) {
        if (String(pos.symbol || '').toUpperCase() !== symU) return;
        var seconds = utcSeconds(pos.open_time);
        var bar = seconds === null ? null : barTimeAt(seconds);
        if (bar === null) return;
        var side = String(pos.type || pos.side || '').toUpperCase();
        var isBuy = side === 'BUY' || side === 'LONG';
        var lots = Number(pos.volume || 0);
        markers.push({
          time: bar,
          position: isBuy ? 'belowBar' : 'aboveBar',
          color: isBuy ? CHART_COLORS.entryBuy : CHART_COLORS.entrySell,
          shape: isBuy ? 'arrowUp' : 'arrowDown',
          text: (isBuy ? 'BUY ' : 'SELL ') + (isFinite(lots) ? lots.toFixed(2) : '?') + 'L @ '
                + num(pos.open_price, digits)
        });
      });
    }

    markers.sort(function (a, b) { return a.time - b.time; });
    try { series.setMarkers(markers); } catch (e) { /* series replaced mid-draw */ }
  }

  /* Redraw the decorations against the candles already on screen. Called after
     the position list changes so an overlay follows a fill or a close without
     waiting for the next candle poll. */
  function refreshChartDecorations() {
    if (!state.chart || !state.chartCandles || !state.chartCandles.length) return;
    var sym = state.chartSymbol || state.symbol;
    var last = state.chartCandles[state.chartCandles.length - 1];
    var digits = priceDigits(sym, last.close);
    drawLevels(sym, digits);
    drawTradeOverlays(sym, digits);
    drawTradeMarkers(sym, digits);
  }

  function renderChart(sym, candles) {
    // De-duplicate by timestamp and sort ascending: the library rejects a
    // series that is out of order or repeats a time.
    var seen = {};
    var bars = [];
    var vols = [];
    candles.forEach(function (raw) {
      var t = Number(raw.time);
      if (!isFinite(t) || seen[t]) return;
      var o = Number(raw.open), h = Number(raw.high), l = Number(raw.low), cl = Number(raw.close);
      if (!isFinite(o) || !isFinite(h) || !isFinite(l) || !isFinite(cl)) return;
      seen[t] = true;
      bars.push({ time: t, open: o, high: h, low: l, close: cl });
      vols.push({
        time: t,
        value: Number(raw.volume || 0),
        color: cl >= o ? 'rgba(0,245,155,0.30)' : 'rgba(255,59,92,0.30)'
      });
    });
    if (!bars.length) return;

    var order = function (a, b) { return a.time - b.time; };
    bars.sort(order);
    vols.sort(order);

    var digits = priceDigits(sym, bars[bars.length - 1].close);
    var firstPaint = (state.chartSymbol !== sym) ||
                     (state.chartTimeframe !== state.timeframe) ||
                     !state.chartPainted;

    state.chartSymbol = sym;
    state.chartTimeframe = state.timeframe;
    state.chartCandles = bars;
    state.chartLevels = computeLevels(bars);
    updateChartHeader(sym, bars);

    // TradingView mode. The candles are drawn by an external widget we do not
    // control, so no price lines are drawn — but the feed's own support and
    // resistance still appear in the legend and the trade card still renders,
    // because both are derived from data rather than from the native series.
    if (state.chartSource === 'tradingview') {
      var legend = $('chart-legend');
      if (legend) {
        var chips = state.showLevels ? levelChipsHtml(digits) : '';
        legend.innerHTML = state.showLevels
          ? (chips || '<span class="tt-hint">No swing pivot in range</span>')
          : '';
      }
      drawTradeOverlays(sym, digits);
      return;
    }

    var c = ensureChart();
    if (!c) return;

    c.candles.applyOptions({
      priceFormat: { type: 'price', precision: digits, minMove: Math.pow(10, -digits) }
    });

    if (firstPaint) {
      c.candles.setData(bars);
      if (c.volume) c.volume.setData(vols);
      c.chart.timeScale().fitContent();
      state.chartPainted = true;
    } else {
      // Update the forming bar only. setData() here would reset the viewport on
      // every poll and throw the user out of wherever they had scrolled to.
      try {
        c.candles.update(bars[bars.length - 1]);
        if (c.volume && vols.length) c.volume.update(vols[vols.length - 1]);
      } catch (e) {
        c.candles.setData(bars);
        if (c.volume) c.volume.setData(vols);
      }
    }

    drawLevels(sym, digits);
    drawTradeOverlays(sym, digits);
  }

  function loadChart() {
    var sym = state.symbol;
    var overlay = $('chart-overlay');
    if (!sym) {
      if (overlay) setState(overlay, 'empty', 'No instrument selected', null);
      return;
    }

    // The query key is `tf`, not `timeframe`. console.js and terminal.js both
    // send `tf`; this call sent `timeframe`, which the handler never read, so
    // the selector silently returned H1 whatever the user picked.
    apiGet('/api/candles?symbol=' + encodeURIComponent(sym) +
           '&tf=' + encodeURIComponent(state.timeframe), TIMEOUT.normal)
      .then(function (res) {
        var candles = (res.data && res.data.candles) || [];
        if (!res.ok || !candles.length) {
          // In TradingView mode the overlay sits over the external widget, so a
          // candle failure there is reported as a legend note rather than by
          // covering the chart the user is actually reading.
          if (state.chartSource === 'tradingview') {
            var legend = $('chart-legend');
            if (legend) {
              legend.innerHTML = '<span class="tt-hint">' +
                esc(res.ok ? 'No candles for ' + sym : 'Feed unavailable') + '</span>';
            }
            return;
          }
          if (overlay) {
            setState(overlay, res.ok ? 'empty' : 'error',
              res.ok ? 'No candles for ' + sym : 'Chart unavailable',
              res.ok ? 'The feed returned an empty series.' : (res.error || ('HTTP ' + res.status)));
          }
          return;
        }
        if (state.chartSource !== 'tradingview' && typeof LightweightCharts === 'undefined') {
          if (overlay) setState(overlay, 'error', 'Chart library unavailable', 'lightweight-charts did not load.');
          return;
        }
        if (overlay) overlay.innerHTML = '';
        renderChart(sym, candles);
      });
  }

  /* ── TradingView chart source ─────────────────────────────────────────────
     An alternative to the native chart, not a replacement: the native series
     carries the derived support/resistance and the trade overlays, and this is
     the same instrument drawn by TradingView's own widget for a second opinion.

     Three rules:

     1. THE SCRIPT IS LOADED ON DEMAND. tv.js is a third-party bundle; pulling
        it on every page load would cost every user a network round trip for a
        feature most sessions never open. It is fetched the first time this
        source is selected.

     2. FAILURE IS EXPLICIT AND LOCAL. A blocked CDN is common on a locked-down
        machine. The panel says the widget could not load and points at the
        native chart, rather than showing an empty box.

     3. THE REQUESTED TICKER IS ON SCREEN. A broker symbol has to be translated
        to a TradingView one, and that mapping is a best guess for anything not
        in the table below. Printing the resolved ticker means a wrong mapping
        is visible as a wrong ticker instead of silently showing a different
        instrument.
  */

  var TV_SCRIPT_SRC = 'https://s3.tradingview.com/tv.js';
  var tvScriptPromise = null;

  var TV_INTERVALS = {
    M1: '1', M5: '5', M15: '15', M30: '30',
    H1: '60', H4: '240', D1: 'D', W1: 'W'
  };

  /* Broker symbol -> TradingView ticker. Ported from the legacy terminal so the
     mapping a returning user expects is preserved. */
  function tradingViewSymbol(rawSym) {
    var s = String(rawSym || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!s) return 'OANDA:XAUUSD';

    // Metals and energy
    if (s.indexOf('XAU') >= 0 || s.indexOf('GOLD') >= 0) return 'OANDA:XAUUSD';
    if (s.indexOf('XAG') >= 0 || s.indexOf('SILVER') >= 0) return 'OANDA:XAGUSD';
    if (s.indexOf('WTI') >= 0 || s.indexOf('OIL') >= 0 || s.indexOf('CRUDE') >= 0) return 'TVC:USOIL';

    // Crypto
    if (s.indexOf('BTC') === 0 || s.indexOf('BITCOIN') >= 0) return 'BINANCE:BTCUSDT';
    if (s.indexOf('ETH') === 0 || s.indexOf('ETHEREUM') >= 0) return 'BINANCE:ETHUSDT';
    if (s.indexOf('SOL') === 0) return 'BINANCE:SOLUSDT';
    if (s.indexOf('XRP') === 0) return 'BINANCE:XRPUSDT';
    if (s.indexOf('DOGE') === 0) return 'BINANCE:DOGEUSDT';
    if (s.indexOf('BNB') === 0) return 'BINANCE:BNBUSDT';
    if (s.indexOf('ADA') === 0) return 'BINANCE:ADAUSDT';

    // Indices. Order matters: "NAS100" contains no "500", but "US500" does, and
    // the Dow check must not catch "US30" before the Nasdaq check sees "NAS100".
    if (s.indexOf('500') >= 0 || s.indexOf('SPX') >= 0) return 'FOREXCOM:SPX500USD';
    if (s.indexOf('NAS') >= 0 || s.indexOf('NDX') >= 0 || s.indexOf('TEC') >= 0) return 'FOREXCOM:NAS100USD';
    if (s.indexOf('DJI') >= 0 || s.indexOf('DOW') >= 0 || s.indexOf('WALL') >= 0) return 'FOREXCOM:DJI';
    if (s.indexOf('GER') >= 0 || s.indexOf('DAX') >= 0) return 'FOREXCOM:GER40';
    if (s.indexOf('UK100') >= 0 || s.indexOf('FTSE') >= 0) return 'FOREXCOM:UK100';
    if (s.indexOf('30') >= 0) return 'FOREXCOM:DJI';

    // Indian indices
    if (s.indexOf('NIFTY') >= 0 || s.indexOf('NIF') >= 0) return 'NSE:NIFTY';
    if (s.indexOf('BANKNIFTY') >= 0) return 'NSE:BANKNIFTY';
    if (s.indexOf('SENSEX') >= 0) return 'BSE:SENSEX';

    // FX majors and minors
    var pairs = ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'NZDUSD', 'USDCAD', 'USDCHF',
                 'EURJPY', 'GBPJPY', 'EURGBP', 'AUDJPY', 'CADJPY', 'CHFJPY',
                 'EURAUD', 'EURCAD'];
    for (var i = 0; i < pairs.length; i++) {
      if (s.indexOf(pairs[i]) === 0) return 'FX:' + pairs[i];
    }
    return 'FX:' + s.slice(0, 6);
  }

  /* Load tv.js once, with a ceiling. A blocked CDN must not leave the panel
     spinning, so the promise resolves false on error or timeout and the caller
     renders a real failure state. */
  function ensureTvScript() {
    if (typeof window.TradingView !== 'undefined') return Promise.resolve(true);
    if (tvScriptPromise) return tvScriptPromise;

    tvScriptPromise = new Promise(function (resolve) {
      var settled = false;
      var finish = function (ok) {
        if (settled) return;
        settled = true;
        if (!ok) tvScriptPromise = null;   // allow a retry on the next switch
        resolve(ok);
      };

      var s = document.createElement('script');
      s.src = TV_SCRIPT_SRC;
      s.async = true;
      s.onload = function () { finish(typeof window.TradingView !== 'undefined'); };
      s.onerror = function () { finish(false); };
      setTimeout(function () { finish(typeof window.TradingView !== 'undefined'); }, 15000);
      document.head.appendChild(s);
    });
    return tvScriptPromise;
  }

  function buildTvWidget(host, sym, timeframe) {
    var tvSym = tradingViewSymbol(sym);
    var interval = TV_INTERVALS[timeframe] || '60';

    host.innerHTML = '';

    var frame = document.createElement('div');
    frame.id = 'tv-widget-frame';
    frame.className = 'tt-chart__tv-frame';
    host.appendChild(frame);

    var note = document.createElement('div');
    note.className = 'tt-chart__tv-note';
    note.textContent = 'TradingView · ' + tvSym + ' · ' + timeframe + ' · ' + interval;
    host.appendChild(note);

    try {
      /* global TradingView */
      new window.TradingView.widget({
        container_id: 'tv-widget-frame',
        autosize: true,
        symbol: tvSym,
        interval: interval,
        timezone: 'Etc/UTC',
        theme: 'dark',
        style: '1',
        locale: 'en',
        enable_publishing: false,
        hide_side_toolbar: false,
        allow_symbol_change: true,
        details: true,
        hotlist: true,
        calendar: true,
        disabled_features: ['create_volume_indicator_by_default'],
        studies: []
      });
      state.tvState = 'ready';
    } catch (e) {
      state.tvState = 'failed';
      setState(host, 'error', 'TradingView failed to start',
        String((e && e.message) || e));
    }
  }

  function loadTradingView() {
    var host = $('chart-tv');
    if (!host) return;

    var sym = state.symbol;
    if (!sym) {
      setState(host, 'empty', 'No instrument selected', null);
      return;
    }

    // Rebuilding the widget reloads a whole iframe, which flickers. Only do it
    // when the instrument or the timeframe actually changed.
    if (state.tvState === 'ready' &&
        state.tvSymbol === sym &&
        state.tvInterval === state.timeframe) {
      return;
    }

    state.tvSymbol = sym;
    state.tvInterval = state.timeframe;
    state.tvState = 'loading';

    host.innerHTML = '';
    setState(host, 'loading', 'Loading TradingView…',
      'Requesting the external chart widget.');

    ensureTvScript().then(function (ok) {
      if (!ok) {
        state.tvState = 'failed';
        setState(host, 'error', 'TradingView unavailable',
          'The widget script could not be loaded. The native chart is unaffected.');
        return;
      }
      buildTvWidget(host, sym, state.timeframe);
    });
  }

  function setChartSource(source) {
    if (source !== 'native' && source !== 'tradingview') source = 'native';
    state.chartSource = source;

    var nativeBtn = $('chart-src-native');
    var tvBtn = $('chart-src-tv');
    if (nativeBtn) nativeBtn.setAttribute('aria-pressed', String(source === 'native'));
    if (tvBtn) tvBtn.setAttribute('aria-pressed', String(source === 'tradingview'));

    var overlay = $('chart-overlay');
    var tvHost = $('chart-tv');

    // Visibility is driven by a body attribute, matching how views and panes
    // already switch, so the stylesheet owns it rather than inline styles.
    document.body.setAttribute('data-chart-source', source);

    if (source === 'tradingview') {
      if (tvHost) tvHost.removeAttribute('hidden');
      if (overlay) { overlay.innerHTML = ''; overlay.hidden = true; }
      loadTradingView();
      return;
    }

    if (tvHost) tvHost.setAttribute('hidden', '');
    if (overlay) overlay.hidden = false;
    // The native canvas kept whatever size it had while hidden, and the series
    // was never painted if the session started in TradingView mode.
    state.chartPainted = false;
    loadChart();
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(resizeChart);
  }

  /* ── Devil's advocate ────────────────────────────────────────────────────
     This view exists to argue against the trade. Every element on it is the
     engine's own adversarial output for the selected symbol: the bull and bear
     cases, the threat vectors, the levels that would invalidate the idea, the
     penalty the devil analyst applied, and the quality gate that decides
     whether the setup may be executed at all.

     Two rules govern how it is rendered:

     1. AN EMPTY LIST IS NOT A CLEAN BILL OF HEALTH. When the engine reports no
        threat vectors the panel says the engine reported none — it never reads
        as "no risks". The absence of an objection is not the absence of risk,
        and a trader must be able to tell "checked and clear" apart from "not
        checked".

     2. THE PENALTY IS ON THE ENGINE'S OWN SCALE. `adversarial_penalty` runs
        0-50, bounded by DecisionEngine.max_devil_penalty. The gauge is drawn as
        penalty/50 so the fill is meaningful, and the colour thresholds match the
        legacy terminal's: above 25 the objection is severe, above 15 it is
        material, below that it is minor.
  */

  var DEVIL_PENALTY_MAX = 50;

  function devilPenaltyBand(p) {
    if (p > 25) return 'down';
    if (p > 15) return 'warn';
    return 'up';
  }

  function devilVerdict(d) {
    if (!d) return { label: '—', cls: 'none' };
    if (d.execution_authorized) return { label: 'AUTHORISED', cls: 'buy' };
    var gate = String(d.gate_policy_decision || '').toUpperCase();
    var dec = String(d.decision || '').toUpperCase();
    if (gate === 'BLOCK' || dec === 'NO_TRADE') return { label: 'BLOCKED', cls: 'sell' };
    if (dec === 'WAIT') return { label: 'WAIT', cls: 'medium' };
    return { label: dec || '—', cls: 'none' };
  }

  function daMetricCard(label, value, extra) {
    var card = document.createElement('div');
    card.className = 'tt-metric';
    var l = document.createElement('span');
    l.className = 'tt-metric__label';
    l.textContent = label;
    var v = document.createElement('span');
    v.className = 'tt-metric__value';
    v.textContent = value === null || value === undefined ? '—' : String(value);
    card.appendChild(l);
    card.appendChild(v);
    if (extra) card.appendChild(extra);
    return card;
  }

  function renderDaMetrics(d) {
    var host = $('da-metrics');
    if (!host) return;

    if (!d) {
      setState(host, 'empty', 'No setup selected',
        'Pick an instrument to see the case against it.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '';

    // The penalty gets a gauge as well as a number: 12 out of 50 is a very
    // different objection from 42 out of 50, and a bare figure hides that.
    var penalty = Number(d.adversarial_penalty);
    var hasPenalty = isFinite(penalty);
    var band = hasPenalty ? devilPenaltyBand(penalty) : 'none';
    var gauge = document.createElement('span');
    gauge.className = 'tt-gauge tt-gauge--' + band;
    var fill = document.createElement('span');
    fill.className = 'tt-gauge__fill';
    fill.style.width = hasPenalty
      ? Math.max(0, Math.min(100, (penalty / DEVIL_PENALTY_MAX) * 100)).toFixed(1) + '%'
      : '0%';
    gauge.appendChild(fill);

    host.appendChild(daMetricCard('Adversarial penalty',
      hasPenalty ? num(penalty, 1) + ' / ' + DEVIL_PENALTY_MAX : '—', gauge));
    host.appendChild(daMetricCard('Model confidence',
      d.model_confidence !== undefined ? pct(d.model_confidence, 1) : '—'));
    host.appendChild(daMetricCard('Dissection',
      String(d.dissection_tier || '—') + ' · ' + num(d.dissection_score, 1)));
    host.appendChild(daMetricCard('Confluence',
      String(d.master_confluence_tier || '—') + ' · ' + num(d.master_confluence_score, 1)));
    host.appendChild(daMetricCard('Risk per trade',
      d.calculated_risk_percent !== undefined ? num(d.calculated_risk_percent, 2) + '%' : '—'));
    host.appendChild(daMetricCard('Reward:risk',
      d.risk_reward_ratio !== undefined ? num(d.risk_reward_ratio, 2) + 'R' : '—'));
    host.appendChild(daMetricCard('Expected value',
      d.expected_value !== undefined ? num(d.expected_value, 2) : '—'));
    host.appendChild(daMetricCard('Sample size',
      d.pattern_sample_size !== undefined ? String(d.pattern_sample_size) : '—'));
  }

  /* Renders a list of the engine's own statements. An empty list is reported as
     "the engine returned none" rather than as an all-clear. */
  function daList(host, items, emptyTitle, emptyDetail) {
    if (!host) return;
    host.innerHTML = '';
    var list = Array.isArray(items) ? items : [];
    if (!list.length) {
      setState(host, 'empty', emptyTitle, emptyDetail);
      return;
    }
    host.removeAttribute('data-state');
    var ul = document.createElement('ul');
    ul.className = 'tt-da__list';
    list.forEach(function (text) {
      var li = document.createElement('li');
      li.textContent = String(text);
      ul.appendChild(li);
    });
    host.appendChild(ul);
  }

  function renderDevilAdvocate() {
    var d = state.symbol ? state.decisions[state.symbol] : null;

    setText($('da-symbol'), state.symbol || '—');
    var v = devilVerdict(d);
    var chip = $('da-verdict');
    if (chip) {
      chip.className = 'tt-chip tt-chip--' + v.cls;
      chip.textContent = v.label;
    }

    renderDaMetrics(d);

    daList($('da-bull'), d && d.bull_case,
      'No bull case recorded',
      'The engine did not publish supporting evidence for this setup.');
    daList($('da-bear'), d && d.bear_case,
      'No bear case recorded',
      'The engine did not publish evidence against this setup.');
    daList($('da-threats'), d && d.risk_factors,
      'No threat vectors reported',
      'The engine returned an empty list. That is not the same as the setup being safe.');
    daList($('da-invalidation'), d && d.invalidation_levels,
      'No invalidation levels reported',
      'The engine did not state what would prove this idea wrong.');
  }

  /* ── Context strip beside the ticket ────────────────────────────────── */
  /* The analyst and news views keep their own tabs. This strip shows a compact
     read of each next to the ticket; it renders from the same `state` the full
     panels use, so the two can never disagree, and it writes to its own
     containers so no element id is shared with them. */
  function setContext(tab) {
    if (['why', 'analyst', 'news'].indexOf(tab) === -1) tab = 'why';
    state.context = tab;
    Array.prototype.forEach.call(document.querySelectorAll('[data-ctx]'), function (btn) {
      btn.setAttribute('aria-pressed', String(btn.getAttribute('data-ctx') === tab));
    });
    var map = { why: 'reason-body', analyst: 'ctx-analyst', news: 'ctx-news' };
    Object.keys(map).forEach(function (k) {
      var el = $(map[k]);
      if (el) el.hidden = (k !== tab);
    });
    if (tab === 'analyst') renderContextAnalyst();
    if (tab === 'news') renderContextNews();
  }

  function renderContextAnalyst() {
    var host = $('ctx-analyst');
    if (!host || host.hidden) return;
    var d = state.symbol ? state.decisions[state.symbol] : null;
    if (!d) {
      setState(host, 'empty', 'No setup selected', 'Pick an instrument to see the analyst view.');
      return;
    }

    var v = devilVerdict(d);
    var gate = d.quality_gate || null;
    var gatePass = gate ? String(gate.passed === true ? 'pass' : (gate.passed === false ? 'block' : '—')) : '—';

    var threats = [];
    ['risk_factors', 'bear_case', 'threats'].forEach(function (k) {
      (d[k] || []).forEach(function (t) {
        var text = typeof t === 'string' ? t : (t.text || t.reason || t.description || '');
        if (text) threats.push(text);
      });
    });

    host.removeAttribute('data-state');
    host.innerHTML =
      '<div class="tt-row" style="gap:var(--hm-space-2);margin-bottom:var(--hm-space-2)">' +
        '<span class="tt-chip tt-chip--' + v.cls + '">' + esc(v.label) + '</span>' +
        '<span class="tt-chip tt-chip--none">gate ' + esc(gatePass) + '</span>' +
        '<span class="tt-chip tt-chip--none">' +
          esc(d.master_confluence_tier || '—') + ' · ' +
          (d.master_confluence_score !== undefined ? d.master_confluence_score : '—') +
        '</span>' +
      '</div>' +
      '<div class="tt-subhead"><span class="tt-panel__title">Case against</span></div>' +
      (threats.length
        ? '<ul class="tt-reasons">' + threats.slice(0, 6).map(function (t) {
            return '<li><span>' + esc(t) + '</span></li>';
          }).join('') + '</ul>'
        : '<p class="tt-hint">The engine reported no threats. That is not the same as the setup being safe.</p>') +
      '<p class="tt-hint" style="margin-top:var(--hm-space-2)">' +
        'Full bull/bear cases, gate detail and objections are on the Analyst tab.' +
      '</p>';
  }

  function renderContextNews() {
    var host = $('ctx-news');
    if (!host || host.hidden) return;
    var all = state.news || [];
    if (!all.length) {
      setState(host, 'empty', 'No calendar loaded', 'Open the News tab to fetch the macro calendar.');
      return;
    }

    var withTimes = all.map(function (ev) {
      return { ev: ev, rem: newsRemaining(ev) };
    }).filter(function (row) {
      // 'past' and 'unknown' are excluded: a release that already happened is
      // not something the trader can still position for.
      return row.rem !== null && ['live', 'soon', 'upcoming'].indexOf(newsPhase(row.ev, row.rem)) >= 0;
    }).sort(function (a, b) { return a.rem - b.rem; }).slice(0, 6);

    var upcoming = withTimes.map(function (row) { return row.ev; });

    if (!upcoming.length) {
      setState(host, 'empty', 'Nothing scheduled', 'No upcoming releases in the loaded calendar.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '<ul class="tt-reasons">' + withTimes.map(function (row) {
      var ev = row.ev;
      var impact = String(ev.impact || '').toUpperCase();
      var cls = impact === 'HIGH' ? 'tt-chip--sell' : impact === 'MEDIUM' ? 'tt-chip--medium' : 'tt-chip--none';
      return '<li>' +
        '<span class="tt-chip ' + cls + '">' + esc(impact || '—') + '</span> ' +
        '<b>' + esc(ev.currency || '') + '</b> ' + esc(ev.event || '—') +
        ' <span class="tt-muted">· ' + esc(countdownLabel(newsPhase(ev, row.rem), row.rem)) + '</span>' +
        '</li>';
    }).join('') + '</ul>' +
    '<p class="tt-hint" style="margin-top:var(--hm-space-2)">Full calendar and impact analysis are on the News tab.</p>';
  }

  function renderQualityGate() {
    var host = $('gate-body');
    var d = state.symbol ? state.decisions[state.symbol] : null;
    var gate = d && d.quality_gate;
    var chip = $('gate-verdict');

    if (!gate) {
      setText($('gate-count'), '0');
      if (chip) { chip.className = 'tt-chip tt-chip--none'; chip.textContent = '—'; }
      setState(host, 'empty', 'No gate result',
        'Pick an instrument whose setup the engine has evaluated.');
      return;
    }

    var checks = (gate.checks && typeof gate.checks === 'object') ? gate.checks : {};
    var names = Object.keys(checks);
    var failed = names.filter(function (n) { return !checks[n]; });

    setText($('gate-count'), (names.length - failed.length) + ' / ' + names.length);
    if (chip) {
      chip.className = 'tt-chip ' + (gate.passed ? 'tt-chip--buy' : 'tt-chip--sell');
      chip.textContent = gate.passed ? 'PASSED' : 'BLOCKED';
    }

    if (!names.length) {
      setState(host, 'empty', 'No checks reported',
        'The engine returned an empty gate for this setup.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '';

    var grid = document.createElement('div');
    grid.className = 'tt-gate';

    // Failures first. The reason a setup is blocked matters more to a trader
    // than the twenty checks that agreed with it.
    names.slice().sort(function (a, b) {
      return (checks[a] ? 1 : 0) - (checks[b] ? 1 : 0) || String(a).localeCompare(String(b));
    }).forEach(function (name) {
      var ok = !!checks[name];
      var cell = document.createElement('div');
      cell.className = 'tt-gate__cell tt-gate__cell--' + (ok ? 'pass' : 'fail');
      cell.setAttribute('title', name + ': ' + (ok ? 'passed' : 'failed'));

      var mark = document.createElement('span');
      mark.className = 'tt-gate__mark';
      mark.setAttribute('aria-hidden', 'true');
      mark.textContent = ok ? '\u2713' : '\u2715';

      var label = document.createElement('span');
      label.className = 'tt-gate__label';
      label.textContent = name;

      cell.appendChild(mark);
      cell.appendChild(label);
      grid.appendChild(cell);
    });

    host.appendChild(grid);
  }

  function renderObjections() {
    var host = $('da-objections');
    var d = state.symbol ? state.decisions[state.symbol] : null;
    if (!d) {
      setState(host, 'empty', 'No setup selected',
        'Pick an instrument to see the engine\u2019s recorded objections.');
      return;
    }

    var rows = [];
    (Array.isArray(d.waiting_reasons) ? d.waiting_reasons : []).forEach(function (r) {
      rows.push(['Waiting', r]);
    });
    (Array.isArray(d.rejection_reasons) ? d.rejection_reasons : []).forEach(function (r) {
      rows.push(['Rejected', r]);
    });

    if (!rows.length) {
      setState(host, 'empty', 'No objections recorded',
        'The engine recorded neither a waiting nor a rejection reason for this setup.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '';
    var ul = document.createElement('ul');
    ul.className = 'tt-reasons';
    rows.forEach(function (pair) {
      var li = document.createElement('li');
      var k = document.createElement('span');
      k.className = 'tt-reasons__key';
      k.textContent = pair[0];
      var v = document.createElement('span');
      v.className = 'tt-reasons__val';
      v.textContent = String(pair[1]);
      li.appendChild(k);
      li.appendChild(v);
      ul.appendChild(li);
    });
    host.appendChild(ul);
  }

  /* ── News and the economic calendar ──────────────────────────────────────
     The calendar is the one panel whose interesting quantity changes every
     second, which drives three rules:

     1. THE COUNTDOWN IS COMPUTED HERE, NOT READ FROM THE PAYLOAD. The server
        sends `diff_seconds` against its own clock at generation time, plus a
        `status_badge` string like "IN 14h 33m" baked at that same instant. Both
        are stale the moment the response lands, and a badge would sit on
        "IN 0m" indefinitely. Each row is anchored to its own `timestamp_iso`
        and corrected for clock skew against the payload's `timestamp`, so the
        countdown is exact and keeps running between polls.

     2. THE LIVE WINDOW IS THE SERVER'S OWN RULE, not a new one. The backend
        (jarvis/market/news.py) opens a volatility-shock window five minutes
        before a release and closes it fifteen minutes after. The client applies
        the same offsets to its own clock, so a row becomes "live" and then
        "released" while the page is open rather than holding whatever state the
        payload happened to describe once.

     3. NO INVENTED PRINTS. Forecast, previous and actual are frequently absent
        from the feed. An absent print renders as a dash — never as 0.00, which
        is a real and materially different number to a trader.
  */

  var NEWS_LIVE_BEFORE = 300;   // seconds a release goes live ahead of time
  var NEWS_LIVE_AFTER = 900;    // seconds it stays live afterwards

  /* Server's "now", derived from the skew measured when the payload landed.
     Anchoring to the server's clock rather than this machine's keeps the
     countdown correct even when the local clock is wrong. */
  function serverNowMs() {
    return state.newsSkew === null ? Date.now() : Date.now() - state.newsSkew;
  }

  /* Seconds until an event. Positive means still ahead of us. */
  function newsRemaining(ev) {
    if (!ev) return null;
    if (ev.timestamp_iso) {
      var t = Date.parse(ev.timestamp_iso);
      if (!isNaN(t)) return (t - serverNowMs()) / 1000;
    }
    if (typeof ev.diff_seconds === 'number' && isFinite(ev.diff_seconds)) {
      return ev.diff_seconds - ((Date.now() - state.newsAt) / 1000);
    }
    return null;
  }

  /* Mirrors the backend's window exactly: live from -5min to +15min. */
  function newsPhase(ev, rem) {
    if (rem === null) return 'unknown';
    if (rem < -NEWS_LIVE_AFTER) return 'past';
    if (rem <= NEWS_LIVE_BEFORE) return 'live';
    if (rem <= 3600) return 'soon';
    return 'upcoming';
  }

  function phaseLabel(phase) {
    return {
      live: 'LIVE', soon: 'IMMINENT', upcoming: 'SCHEDULED',
      past: 'RELEASED', unknown: '—'
    }[phase] || '—';
  }

  /* Absolute duration. Sign is expressed by the caller, so "ago" and "in"
     read correctly rather than relying on a leading minus. */
  function fmtDuration(sec) {
    var s = Math.round(Math.abs(Number(sec) || 0));
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60), ss = s % 60;
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    if (m > 0) return m + 'm ' + ss + 's';
    return ss + 's';
  }

  function countdownLabel(phase, rem) {
    if (rem === null) return '—';
    if (phase === 'live') return rem >= 0 ? 'T\u2212' + fmtDuration(rem) : 'T+' + fmtDuration(rem);
    if (phase === 'past') return fmtDuration(rem) + ' ago';
    return 'in ' + fmtDuration(rem);
  }

  function impactChip(impact) {
    var k = String(impact || '').toUpperCase();
    var cls = ['HIGH', 'MEDIUM', 'LOW'].indexOf(k) >= 0 ? k.toLowerCase() : 'none';
    return '<span class="tt-chip tt-chip--' + cls + '">' + esc(k || '—') + '</span>';
  }

  /* Stable identity for selection, so a refresh does not drop the open event. */
  function newsKey(ev) {
    if (!ev) return null;
    return String(ev.timestamp_iso || '') + '|' + String(ev.event || '');
  }

  function newsFiltered() {
    var impact = state.newsImpact, cur = state.newsCurrency;
    return (state.news || []).filter(function (e) {
      if (impact !== 'ALL' && String(e.impact || '').toUpperCase() !== impact) return false;
      if (cur !== 'ALL' && String(e.currency || '').toUpperCase() !== cur) return false;
      return true;
    });
  }

  /* The event the "Next release" panel should feature: one inside its live
     window if any, else the soonest still ahead of us. The server orders the
     upcoming block ascending, so the first match is the soonest. Falls back to
     the most recent release so the panel is never blank while loaded. */
  function pickNextEvent() {
    var list = state.news || [];
    if (!list.length) return null;
    var live = null, next = null;
    list.forEach(function (ev) {
      var phase = newsPhase(ev, newsRemaining(ev));
      if (phase === 'live') { if (!live) live = ev; }
      else if ((phase === 'soon' || phase === 'upcoming') && !next) next = ev;
    });
    return live || next || list[0];
  }

  function renderNewsLiveChip() {
    var chip = $('news-live-chip');
    if (!chip) return;
    var live = null;
    (state.news || []).some(function (ev) {
      if (newsPhase(ev, newsRemaining(ev)) === 'live') { live = ev; return true; }
      return false;
    });
    if (live) {
      chip.className = 'tt-chip tt-chip--live';
      chip.textContent = 'LIVE · ' + (live.currency || '') + ' ' + (live.event || '');
    } else {
      chip.className = 'tt-chip tt-chip--none';
      chip.textContent = 'no release live';
    }
  }

  /* Provenance, stated rather than implied. `payload.source` is 'live_feed',
     'mixed' or 'synthetic_calendar'. The engine substitutes a HARDCODED event
     plan when the feed is rate-limited or unreachable, so a calendar that looks
     entirely real may be entirely invented -- and it used to say nothing. */
  function renderNewsSourceChip() {
    var chip = $('news-source-chip');
    if (!chip) return;
    var src = state.newsSource;
    if (!src || src === 'live_feed') { chip.hidden = true; return; }
    chip.hidden = false;
    chip.className = 'tt-chip tt-chip--high';
    chip.textContent = (src === 'mixed')
      ? 'partly synthetic · ' + (state.newsSynthetic || 0)
      : 'synthetic calendar · feed unavailable';
  }

  /* The countdown cell carries its own anchors so the per-second tick can
     recompute without a lookup into a list that a filter may have changed. */
  function newsCdCell(ev, rem, phase, cls) {
    return '<td class="' + cls + '" data-cd="1" data-phase="' + esc(phase) + '"' +
      ' data-iso="' + esc(ev.timestamp_iso || '') + '"' +
      ' data-diff="' + esc(typeof ev.diff_seconds === 'number' ? ev.diff_seconds : '') + '">' +
      '<span class="tt-news__cd-val" data-cd-val="1">' + esc(countdownLabel(phase, rem)) + '</span>' +
      '<span class="tt-news__cd-badge">' + esc(phaseLabel(phase)) + '</span>' +
      '</td>';
  }

  function newsRow(ev) {
    var rem = newsRemaining(ev);
    var phase = newsPhase(ev, rem);
    var key = newsKey(ev);
    var pairs = Array.isArray(ev.affected_pairs) ? ev.affected_pairs : [];

    var tr = document.createElement('tr');
    tr.className = 'tt-news__row';
    tr.setAttribute('data-phase', phase);
    tr.setAttribute('data-key', key);
    if (state.newsSelected === key) tr.setAttribute('data-selected', 'true');
    tr.setAttribute('tabindex', '0');
    tr.setAttribute('role', 'button');
    tr.setAttribute('aria-label', 'Event detail: ' + String(ev.event || 'event'));

    tr.innerHTML =
      '<td class="tt-news__when">' +
        '<span class="tt-num">' + esc(ev.time_ist || ev.time || '—') + '</span>' +
        (ev.time_utc ? '<span class="tt-news__utc">' + esc(ev.time_utc) + '</span>' : '') +
      '</td>' +
      newsCdCell(ev, rem, phase, 'tt-news__cd') +
      '<td>' + impactChip(ev.impact) + '</td>' +
      '<td class="tt-news__cur">' + esc(ev.currency || '—') + '</td>' +
      '<td class="tt-news__event">' +
        '<span class="tt-news__name">' + esc(ev.event || '—') + '</span>' +
        (pairs.length ? '<span class="tt-news__pairs">' + esc(pairs.join(' · ')) + '</span>' : '') +
      '</td>' +
      '<td class="tt-num">' + esc(ev.actual || '—') + '</td>' +
      '<td class="tt-num">' + esc(ev.forecast || '—') + '</td>' +
      '<td class="tt-num">' + esc(ev.previous || '—') + '</td>';

    tr.addEventListener('click', function () { selectNews(ev); });
    tr.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectNews(ev); }
    });
    return tr;
  }

  function renderNews() {
    var host = $('news-body');
    if (!host) return;

    var all = state.news || [];
    var rows = newsFiltered();

    setText($('news-count'), all.length === rows.length
      ? String(rows.length) : rows.length + ' of ' + all.length);
    renderNewsLiveChip();
    renderNewsSourceChip();

    if (!all.length) {
      setState(host, 'empty', 'No events scheduled',
        'The calendar feed returned no recent or upcoming releases.');
      return;
    }
    if (!rows.length) {
      setState(host, 'empty', 'No events match the filter',
        'Clear the impact or currency filter to see all ' + all.length + ' events.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '';

    var table = document.createElement('table');
    table.className = 'tt-table tt-news';
    var thead = document.createElement('thead');
    thead.innerHTML =
      '<tr>' +
        '<th scope="col">When</th>' +
        '<th scope="col">Countdown</th>' +
        '<th scope="col">Impact</th>' +
        '<th scope="col">Cur</th>' +
        '<th scope="col">Event</th>' +
        '<th scope="col" class="tt-num">Actual</th>' +
        '<th scope="col" class="tt-num">Forecast</th>' +
        '<th scope="col" class="tt-num">Previous</th>' +
      '</tr>';
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    rows.forEach(function (ev) { tbody.appendChild(newsRow(ev)); });
    table.appendChild(tbody);
    host.appendChild(table);

    // The context strip reads the same cached calendar.
    renderContextNews();
  }

  function renderNewsHero() {
    var host = $('news-hero');
    if (!host) return;

    var ev = pickNextEvent();
    if (!ev) {
      state.newsHeroKey = null;
      setState(host, 'empty', 'No upcoming release',
        'The calendar feed returned no events.');
      return;
    }

    var rem = newsRemaining(ev);
    var phase = newsPhase(ev, rem);
    state.newsHeroKey = newsKey(ev);

    host.removeAttribute('data-state');
    host.innerHTML = '';

    var wrap = document.createElement('div');
    wrap.className = 'tt-news-hero';
    wrap.setAttribute('data-phase', phase);

    var grid = [
      ['Forecast', ev.forecast], ['Previous', ev.previous], ['Actual', ev.actual],
      ['Bias', ev.direction_bias], ['Shock risk', ev.shock_risk]
    ];
    var cells = grid.map(function (p) {
      return '<div class="tt-news-hero__cell">' +
        '<span class="tt-news-hero__k">' + esc(p[0]) + '</span>' +
        '<span class="tt-news-hero__v">' + esc(p[1] || '—') + '</span>' +
      '</div>';
    }).join('');

    wrap.innerHTML =
      '<div class="tt-news-hero__top">' +
        impactChip(ev.impact) +
        '<span class="tt-news-hero__cur">' + esc(ev.currency || '—') + '</span>' +
        '<span class="tt-hint">' + esc(ev.time_ist || ev.time || '—') + '</span>' +
      '</div>' +
      '<h3 class="tt-news-hero__title">' + esc(ev.event || '—') + '</h3>' +
      '<div class="tt-news-hero__cd" data-cd="1" data-phase="' + esc(phase) + '"' +
        ' data-iso="' + esc(ev.timestamp_iso || '') + '"' +
        ' data-diff="' + esc(typeof ev.diff_seconds === 'number' ? ev.diff_seconds : '') + '">' +
        '<span class="tt-news-hero__cd-val" data-cd-val="1">' + esc(countdownLabel(phase, rem)) + '</span>' +
        '<span class="tt-news-hero__cd-badge">' + esc(phaseLabel(phase)) + '</span>' +
      '</div>' +
      '<div class="tt-news-hero__grid">' + cells + '</div>';

    host.appendChild(wrap);
  }

  function renderNewsDetail(explicit) {
    var host = $('news-detail');
    if (!host) return;

    var ev = explicit || null;
    if (!ev && state.newsSelected) {
      ev = (state.news || []).filter(function (e) {
        return newsKey(e) === state.newsSelected;
      })[0] || null;
    }

    var chip = $('news-detail-impact');
    if (!ev) {
      if (chip) { chip.className = 'tt-chip tt-chip--none'; chip.textContent = '—'; }
      setState(host, 'empty', 'No event selected',
        'Pick an event from the calendar to read its impact analysis.');
      return;
    }

    var k = String(ev.impact || '').toUpperCase();
    if (chip) {
      chip.className = 'tt-chip tt-chip--' +
        (['HIGH', 'MEDIUM', 'LOW'].indexOf(k) >= 0 ? k.toLowerCase() : 'none');
      chip.textContent = k || '—';
    }

    var rem = newsRemaining(ev);
    var phase = newsPhase(ev, rem);
    var pairs = Array.isArray(ev.affected_pairs) ? ev.affected_pairs : [];

    host.removeAttribute('data-state');
    host.innerHTML = '';

    var rows = [
      // The event's own name, first. A detail panel that shows forecast,
      // previous and actual without saying which release they belong to is
      // unreadable the moment the user scrolls the calendar.
      ['Event', ev.event],
      ['Status', phaseLabel(phase) + ' · ' + countdownLabel(phase, rem)],
      ['When (IST)', ev.time_ist || ev.time || '—'],
      ['When (UTC)', ev.time_utc || '—'],
      ['Forecast', ev.forecast], ['Previous', ev.previous], ['Actual', ev.actual],
      ['Deviation', ev.deviation_summary],
      ['Direction bias', ev.direction_bias],
      ['Shock risk', ev.shock_risk],
      ['Category', ev.category],
      ['Affected', pairs.length ? pairs.join(' · ') : null]
    ];

    var dl = document.createElement('dl');
    dl.className = 'tt-detail';
    rows.forEach(function (pair) {
      var dt = document.createElement('dt');
      dt.textContent = pair[0];
      var dd = document.createElement('dd');
      setText(dd, pair[1]);
      dl.appendChild(dt);
      dl.appendChild(dd);
    });
    host.appendChild(dl);

    // Prose blocks. Each is written with textContent, so a headline containing
    // markup cannot inject into the panel.
    [
      ['Impact', ev.impact_analysis],
      ['What it is', ev.description],
      ['Execution', ev.execution_warning],
      ['Window', ev.shock_alert]
    ].forEach(function (pair) {
      if (!pair[1]) return;
      var h = document.createElement('h4');
      h.className = 'tt-da__heading';
      h.textContent = pair[0];
      var p = document.createElement('p');
      p.className = 'tt-news__prose';
      p.textContent = String(pair[1]);
      host.appendChild(h);
      host.appendChild(p);
    });
  }

  function selectNews(ev) {
    state.newsSelected = newsKey(ev);
    var root = $('news-body');
    if (root) {
      Array.prototype.forEach.call(root.querySelectorAll('tr[data-key]'), function (tr) {
        if (tr.getAttribute('data-key') === state.newsSelected) tr.setAttribute('data-selected', 'true');
        else tr.removeAttribute('data-selected');
      });
    }
    renderNewsDetail(ev);
  }

  /* Rebuild the currency filter only when the set actually changed. Rebuilding
     on every poll would reset the user's selection mid-session. */
  function populateNewsCurrencies(events) {
    var sel = $('news-currency');
    if (!sel) return;
    var seen = {};
    events.forEach(function (e) {
      var c = String(e.currency || '').toUpperCase();
      if (c) seen[c] = true;
    });
    var wanted = ['ALL'].concat(Object.keys(seen).sort());
    var existing = Array.prototype.map.call(sel.options || [], function (o) { return o.value; });
    if (existing.join(',') === wanted.join(',')) return;

    sel.innerHTML = '';
    wanted.forEach(function (c) {
      var o = document.createElement('option');
      o.value = c;
      o.textContent = c === 'ALL' ? 'All currencies' : c;
      sel.appendChild(o);
    });
    sel.value = wanted.indexOf(state.newsCurrency) >= 0 ? state.newsCurrency : 'ALL';
    state.newsCurrency = sel.value;
  }

  /* One tick per second. Updates only the countdown cells in place — rebuilding
     the table here would throw away the user's scroll position and focus every
     second. */
  function tickNews() {
    if (state.view !== 'news') return;
    var root = $('view-news');
    if (!root) return;

    Array.prototype.forEach.call(root.querySelectorAll('[data-cd]'), function (cell) {
      var iso = cell.getAttribute('data-iso') || null;
      var rawDiff = cell.getAttribute('data-diff');
      var rem = newsRemaining({
        timestamp_iso: iso,
        diff_seconds: rawDiff ? parseFloat(rawDiff) : null
      });
      var phase = newsPhase({ timestamp_iso: iso }, rem);

      var val = cell.querySelector('[data-cd-val]');
      if (val) val.textContent = countdownLabel(phase, rem);
      cell.setAttribute('data-phase', phase);

      var badge = cell.querySelector('.tt-news__cd-badge');
      if (badge) badge.textContent = phaseLabel(phase);

      // The row and the hero both carry the phase on the element above the
      // cell, which is what the stylesheet keys its colour off.
      if (cell.parentNode && cell.parentNode.getAttribute) {
        cell.parentNode.setAttribute('data-phase', phase);
      }
    });

    renderNewsLiveChip();
    renderNewsSourceChip();

    // Promote the next release once the featured one has finished.
    if (newsKey(pickNextEvent()) !== state.newsHeroKey) renderNewsHero();
  }

  function loadNews() {
    var host = $('news-body');
    if (!host) return;
    if (!state.news.length) setState(host, 'loading', 'Loading calendar…');

    apiGet('/api/news', TIMEOUT.normal).then(function (res) {
      if (!res.ok) {
        // Keep the last good calendar on screen; a refresh failure is not a
        // reason to blank a panel the trader may be reading.
        if (state.news.length) {
          toast('Calendar refresh failed: ' + (res.error || ('HTTP ' + res.status)), 'error');
          return;
        }
        setState(host, 'error', 'Calendar unavailable',
          res.error || ('HTTP ' + res.status));
        return;
      }

      var payload = res.data || {};
      var events = Array.isArray(payload.news) ? payload.news : [];
      state.news = events;
      // Provenance. Older servers do not send these, so a missing value must
      // read as "unknown" and render no chip -- never as "real".
      state.newsSource = payload.source || null;
      state.newsSynthetic = typeof payload.synthetic_count === 'number'
        ? payload.synthetic_count : 0;
      state.newsAt = Date.now();

      // Skew between this machine's clock and the server's, taken from the
      // payload's own generation time.
      var st = payload.timestamp ? Date.parse(payload.timestamp) : NaN;
      state.newsSkew = isNaN(st) ? null : Date.now() - st;

      populateNewsCurrencies(events);
      setText($('news-updated'), payload.timestamp ? 'as of ' + payload.timestamp : '—');

      renderNews();
      renderNewsHero();
      renderNewsDetail();
    });
  }

  /* ── Global and Indian markets ───────────────────────────────────────────
     Five panels, five independent requests, three rules:

     1. EACH PANEL OWNS ITS OWN REQUEST AND ITS OWN FAILURE STATE. These routes
        sit behind external providers and several block for many seconds on a
        cold cache. One slow provider must not blank the other four, so every
        panel is fetched and rendered separately, and a refresh failure keeps the
        last good payload on screen rather than emptying a panel the trader is
        reading.

     2. PROVENANCE IS ON SCREEN, NOT IN A TOOLTIP. Not every figure here comes
        from a live read: the institutional-flow endpoint returns fixed sample
        values, the screener emits placeholder rows for symbols it failed to
        analyse, and the India price series is modelled. Each panel prints the
        source of its own numbers.

     3. PLACEHOLDERS ARE NEVER RENDERED AS ANALYSIS. On a fallback row the grade,
        probability, bias, entry, stop and target are all suppressed and the
        price is marked as a reference. A screener that invents a GRADE B
        breakout for a symbol it could not analyse is worse than one that says
        the symbol was not analysed.
  */

  var SOURCE_META = {
    live: { label: 'live', cls: 'buy', note: 'Read from a live provider.' },
    synthetic_anchored: { label: 'modelled', cls: 'low', note: 'Every bar is generated; only the anchor price came from a live quote.' },
    calibrated_feed: { label: 'modelled', cls: 'low', note: 'Candles generated and anchored to a reference price, not read from a feed.' },
    synthetic: { label: 'modelled', cls: 'low', note: 'Modelled series; no live read was available.' },
    synthetic_fallback: { label: 'modelled', cls: 'low', note: 'Modelled series; no live read was available.' },
    profile_reference: { label: 'reference', cls: 'medium', note: 'Static profile reference price, not a market quote.' },
    sample: { label: 'sample', cls: 'medium', note: 'Fixed sample values. Not live data.' },
    mixed: { label: 'mixed', cls: 'medium', note: 'Rows in this panel came from more than one source.' },
    unknown: { label: 'unknown', cls: 'none', note: 'The response did not report where its numbers came from.' }
  };

  /* Ordered strongest to weakest claim. Used to report the *weakest* source in
     a panel: if one row is a static reference price, the panel as a whole is not
     a live read, and labelling it "live" because most rows were would be exactly
     the mistake the provenance markers exist to prevent.

     `synthetic_anchored` sits below `live` and above `calibrated_feed`: its anchor
     is a current quote, but every bar is still generated. It exists because the
     engines used to report the ANCHOR's provenance as the SERIES' — so a fully
     generated series was published as `live` and rendered with the "Read from a
     live provider" chip. */
  var SOURCE_STRENGTH = ['live', 'synthetic_anchored', 'calibrated_feed', 'synthetic',
                         'synthetic_fallback', 'profile_reference', 'sample',
                         'mixed', 'unknown'];

  function weakestSource(list) {
    var worst = null;
    var worstIdx = -1;
    (list || []).forEach(function (s) {
      var key = String(s || 'unknown').toLowerCase();
      var i = SOURCE_STRENGTH.indexOf(key);
      if (i < 0) i = SOURCE_STRENGTH.length;   // an unrecognised source is the weakest claim of all
      if (i > worstIdx) { worstIdx = i; worst = key; }
    });
    return worst || 'unknown';
  }

  function renderProvChip(el, source) {
    if (!el) return;
    var key = String(source || 'unknown').toLowerCase();
    var meta = SOURCE_META[key] || { label: key, cls: 'none', note: '' };
    el.className = 'tt-chip tt-chip--' + meta.cls;
    el.textContent = meta.label;
    el.setAttribute('title', meta.note || key);
  }

  /* A value already expressed in percent (the screener's change_pct, the India
     indices' change_pct). Deliberately not pct(), which rescales small values on
     the assumption they are fractions — 1.2% would render as 120%. */
  function signedPct(value, digits) {
    if (value === null || value === undefined || value === '' || isNaN(value)) return '—';
    var v = Number(value);
    if (!isFinite(v)) return '—';
    return (v > 0 ? '+' : '') + num(v, digits === undefined ? 2 : digits) + '%';
  }

  function heatClass(changePct) {
    var v = Number(changePct);
    if (!isFinite(v) || v === 0) return '';
    var mag = Math.abs(v);
    var band = mag >= 3 ? 4 : (mag >= 1.5 ? 3 : (mag >= 0.5 ? 2 : 1));
    return (v > 0 ? 'tt-heat-' : 'tt-heat-neg-') + band;
  }

  function loadMarkets() {
    loadEquities();
    loadHeatmap();
    loadIndiaIndices();
    loadIndiaFii();
    loadIndiaOptionChain();
  }

  /* ── Global equities ─────────────────────────────────────────────────── */

  function loadEquities() {
    var host = $('eq-body');
    if (!host) return;
    if (!state.equityRows.length) setState(host, 'loading', 'Loading global equities…');

    apiGet('/api/stocks/screener?sort_by=probability&sort_dir=desc&limit=40', TIMEOUT.provider)
      .then(function (res) {
        if (!res.ok || !res.data) {
          if (state.equityRows.length) {
            toast('Equities refresh failed: ' + (res.error || ('HTTP ' + res.status)), 'error');
            return;
          }
          setState(host, 'error', 'Screener unavailable',
            res.error || ('HTTP ' + res.status));
          renderProvChip($('eq-prov'), 'unknown');
          return;
        }
        var payload = res.data;
        state.equities = payload;
        state.equityRows = Array.isArray(payload.stocks) ? payload.stocks : [];
        renderEquities();
      });
  }

  function renderEquities() {
    var host = $('eq-body');
    if (!host) return;
    var payload = state.equities || {};
    var rows = state.equityRows;

    setText($('eq-count'), String(rows.length) +
      (payload.total_universe ? ' / ' + payload.total_universe : ''));

    var fallbacks = Number(payload.fallback_count || 0);
    renderProvChip($('eq-prov'), fallbacks > 0 ? 'mixed' : 'calibrated_feed');
    var prov = $('eq-prov');
    if (prov && fallbacks > 0) {
      prov.textContent = fallbacks + ' placeholder';
      prov.setAttribute('title',
        fallbacks + ' of ' + payload.total_universe +
        ' symbols failed analysis. Those rows show no setup fields.');
    }

    if (!rows.length) {
      setState(host, 'empty', 'No equities returned',
        'The screener answered with an empty universe.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '';

    var table = document.createElement('table');
    table.className = 'tt-table tt-eq';
    var thead = document.createElement('thead');
    thead.innerHTML =
      '<tr>' +
        '<th scope="col">Symbol</th>' +
        '<th scope="col" class="tt-num">Price</th>' +
        '<th scope="col" class="tt-num">Chg</th>' +
        '<th scope="col" class="tt-num">Breakout</th>' +
        '<th scope="col">Grade</th>' +
        '<th scope="col">Bias</th>' +
        '<th scope="col" class="tt-num">Entry</th>' +
        '<th scope="col" class="tt-num">R:R</th>' +
      '</tr>';
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    rows.forEach(function (s) { tbody.appendChild(equityRow(s)); });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  function equityRow(s) {
    var fallback = String(s.analysis_source || '') === 'fallback';
    var digits = priceDigits(s.symbol, s.price);
    var chg = Number(s.change_pct);
    var grade = String(s.grade_badge || '').toUpperCase();
    var gradeCls = grade.indexOf('A') >= 0 ? 'tt-chip--buy'
                 : (grade.indexOf('B') >= 0 ? 'tt-chip--medium' : 'tt-chip--none');

    var tr = document.createElement('tr');
    tr.className = 'tt-eq__row' + (fallback ? ' tt-eq__row--fallback' : '');
    tr.setAttribute('data-source', fallback ? 'fallback' : 'computed');

    var dash = '<span class="tt-muted">—</span>';

    tr.innerHTML =
      '<td class="tt-eq__sym">' +
        '<span class="tt-eq__name">' + esc(s.symbol || '—') + '</span>' +
        '<span class="tt-eq__sector">' + esc(s.sector || '—') + '</span>' +
      '</td>' +
      '<td class="tt-num">' + (fallback
        ? '<span class="tt-eq__ref" title="Static profile reference, not a quote">' +
          esc(num(s.price, digits)) + ' ref</span>'
        : esc(num(s.price, digits))) + '</td>' +
      '<td class="tt-num ' + (fallback ? '' : signClass(chg)) + '">' +
        (fallback ? dash : esc(signedPct(chg))) + '</td>' +
      '<td class="tt-num">' +
        (fallback ? dash : esc(num(s.breakout_probability, 0) + '%')) + '</td>' +
      '<td>' + (fallback
        ? '<span class="tt-chip tt-chip--none">no analysis</span>'
        : '<span class="tt-chip ' + gradeCls + '">' + esc(grade || '—') + '</span>') + '</td>' +
      '<td>' + (fallback ? dash : esc(s.trend_bias || '—')) + '</td>' +
      '<td class="tt-num">' + (fallback ? dash : esc(num(s.entry_zone, digits))) + '</td>' +
      '<td class="tt-num">' +
        (fallback ? dash : esc(num(s.risk_reward, 2) + 'R')) + '</td>';

    return tr;
  }

  /* ── Sector rotation ─────────────────────────────────────────────────── */

  function loadHeatmap() {
    var host = $('eq-heatmap');
    if (!host) return;
    if (!state.heatmap) setState(host, 'loading', 'Loading sector rotation…');

    apiGet('/api/stocks/heatmap', TIMEOUT.provider).then(function (res) {
      if (!res.ok || !res.data) {
        if (state.heatmap) { toast('Sector rotation refresh failed', 'error'); return; }
        setState(host, 'error', 'Sector data unavailable', res.error || ('HTTP ' + res.status));
        return;
      }
      state.heatmap = res.data;
      renderHeatmap();
    });
  }

  function renderHeatmap() {
    var host = $('eq-heatmap');
    if (!host) return;
    var sectors = (state.heatmap && state.heatmap.sectors) || [];

    setText($('eq-heat-note'), sectors.length
      ? sectors.length + ' sectors · sorted by average change'
      : '—');

    if (!sectors.length) {
      setState(host, 'empty', 'No sector data', 'The heatmap endpoint returned no sectors.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '';
    var grid = document.createElement('div');
    grid.className = 'tt-heatgrid';

    sectors.forEach(function (s) {
      // The US and India heatmaps disagree on the count field name; accept both
      // rather than silently rendering a blank.
      var count = Number(s.count || s.stock_count || 0);
      var tile = document.createElement('div');
      tile.className = 'tt-heattile ' + heatClass(s.avg_change_pct);
      tile.setAttribute('title', String(s.sector || '') + ' · ' +
        (s.rotation_status || '') + (s.avg_probability !== undefined
          ? ' · avg breakout ' + s.avg_probability + '%' : ''));

      var name = document.createElement('span');
      name.className = 'tt-heattile__name';
      name.textContent = String(s.sector || '—');

      var chg = document.createElement('span');
      chg.className = 'tt-heattile__chg tt-num';
      chg.textContent = signedPct(s.avg_change_pct);

      var meta = document.createElement('span');
      meta.className = 'tt-heattile__meta';
      meta.textContent = count + ' names' +
        (s.top_leader_symbol ? ' · ' + s.top_leader_symbol : '');

      tile.appendChild(name);
      tile.appendChild(chg);
      tile.appendChild(meta);
      grid.appendChild(tile);
    });

    host.appendChild(grid);
  }

  /* ── India: benchmark indices ────────────────────────────────────────── */

  function loadIndiaIndices() {
    var host = $('in-indices');
    if (!host) return;
    if (!state.indiaIndices) setState(host, 'loading', 'Loading Indian indices…');

    apiGet('/api/india/indices', TIMEOUT.provider).then(function (res) {
      if (!res.ok || !res.data) {
        if (state.indiaIndices) { toast('India indices refresh failed', 'error'); return; }
        setState(host, 'error', 'Indices unavailable', res.error || ('HTTP ' + res.status));
        renderProvChip($('in-index-prov'), 'unknown');
        return;
      }
      state.indiaIndices = Array.isArray(res.data.indices) ? res.data.indices : [];
      renderIndiaIndices();
    });
  }

  function renderIndiaIndices() {
    var host = $('in-indices');
    if (!host) return;
    var rows = state.indiaIndices || [];

    if (!rows.length) {
      setState(host, 'empty', 'No indices returned', 'The India endpoint returned an empty list.');
      renderProvChip($('in-index-prov'), 'unknown');
      return;
    }

    // One chip for the panel, reporting the weakest source in it: if any index
    // is only a reference price, the panel is not wholly live.
    renderProvChip($('in-index-prov'),
      weakestSource(rows.map(function (r) { return r.data_source; })));

    host.removeAttribute('data-state');
    host.innerHTML = '';

    var table = document.createElement('table');
    table.className = 'tt-table tt-india';
    var thead = document.createElement('thead');
    thead.innerHTML =
      '<tr>' +
        '<th scope="col">Index</th>' +
        '<th scope="col" class="tt-num">Level</th>' +
        '<th scope="col" class="tt-num">Chg</th>' +
        '<th scope="col">CPR</th>' +
        '<th scope="col" class="tt-num">VWAP</th>' +
        '<th scope="col">Bias</th>' +
      '</tr>';
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    rows.forEach(function (r) {
      var chg = Number(r.change_pct);
      var tr = document.createElement('tr');
      // The API field is `cpr_classification`. The legacy india.js read
      // `cpr_width` and tested it against 'NARROW_CPR', so that badge never
      // rendered correctly.
      var cpr = String(r.cpr_classification || '').toUpperCase();
      var cprCls = cpr.indexOf('NARROW') >= 0 ? 'tt-chip--low'
                 : (cpr.indexOf('WIDE') >= 0 ? 'tt-chip--high' : 'tt-chip--none');
      tr.innerHTML =
        '<td>' + esc(r.symbol || '—') +
          '<span class="tt-eq__sector">' + esc(r.name || '') + '</span></td>' +
        '<td class="tt-num">' + esc(num(r.price, 2)) + '</td>' +
        '<td class="tt-num ' + signClass(chg) + '">' + esc(signedPct(chg)) + '</td>' +
        '<td><span class="tt-chip ' + cprCls + '">' + esc(r.cpr_label || cpr || '—') + '</span></td>' +
        '<td class="tt-num">' + esc(num(r.vwap, 2)) + '</td>' +
        '<td>' + esc(r.bias || '—') + '</td>';
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  /* ── India: institutional flows ──────────────────────────────────────── */

  function loadIndiaFii() {
    var host = $('in-fii');
    if (!host) return;
    if (!state.indiaFii) setState(host, 'loading', 'Loading institutional flows…');

    apiGet('/api/india/fii_dii', TIMEOUT.normal).then(function (res) {
      if (!res.ok || !res.data) {
        if (state.indiaFii) { toast('Institutional flows refresh failed', 'error'); return; }
        setState(host, 'error', 'Flow data unavailable', res.error || ('HTTP ' + res.status));
        renderProvChip($('in-fii-prov'), 'unknown');
        return;
      }
      state.indiaFii = res.data;
      renderIndiaFii();
    });
  }

  function renderIndiaFii() {
    var host = $('in-fii');
    if (!host) return;
    var d = state.indiaFii;
    if (!d) return;

    renderProvChip($('in-fii-prov'), d.data_source);

    host.removeAttribute('data-state');
    host.innerHTML = '';

    // The note the backend publishes alongside the sample flag. Shown in full
    // rather than hidden behind a tooltip: these are the numbers a trader would
    // otherwise read as today's flows.
    if (d.data_source_note) {
      var note = document.createElement('p');
      note.className = 'tt-prov-note';
      note.textContent = String(d.data_source_note);
      host.appendChild(note);
    }

    var metrics = document.createElement('div');
    metrics.className = 'tt-metrics';
    [
      ['Session', d.date],
      ['FII cash (₹ Cr)', num(d.fii_cash_net_cr, 2)],
      ['DII cash (₹ Cr)', num(d.dii_cash_net_cr, 2)],
      ['Net institutional (₹ Cr)', num(d.total_net_institutional_cr, 2)],
      ['FII index futures long', d.fii_index_futures_long_pct !== undefined
        ? num(d.fii_index_futures_long_pct, 1) + '%' : '—'],
      ['FII index options PCR', num(d.fii_index_options_pcr, 2)],
      ['FII stance', d.fii_sentiment],
      ['DII stance', d.dii_sentiment],
      ['Institutional bias', d.institutional_bias]
    ].forEach(function (pair) {
      var card = document.createElement('div');
      card.className = 'tt-metric';
      var l = document.createElement('span');
      l.className = 'tt-metric__label';
      l.textContent = pair[0];
      var v = document.createElement('span');
      v.className = 'tt-metric__value';
      setText(v, pair[1]);
      card.appendChild(l);
      card.appendChild(v);
      metrics.appendChild(card);
    });
    host.appendChild(metrics);
  }

  /* ── India: option chain ─────────────────────────────────────────────── */

  function loadIndiaOptionChain() {
    var host = $('in-optionchain');
    if (!host) return;
    var sym = ($('in-oc-symbol') && $('in-oc-symbol').value) || 'NIFTY';
    if (!state.indiaOptionChain) setState(host, 'loading', 'Loading ' + sym + ' option chain…');

    apiGet('/api/india/option_chain?symbol=' + encodeURIComponent(sym), TIMEOUT.provider)
      .then(function (res) {
        if (!res.ok || !res.data) {
          if (state.indiaOptionChain) { toast('Option chain refresh failed', 'error'); return; }
          setState(host, 'error', 'Option chain unavailable', res.error || ('HTTP ' + res.status));
          renderProvChip($('in-oc-prov'), 'unknown');
          return;
        }
        state.indiaOptionChain = res.data;
        renderIndiaOptionChain();
      });
  }

  function renderIndiaOptionChain() {
    var host = $('in-optionchain');
    if (!host) return;
    var d = state.indiaOptionChain;
    if (!d) return;

    renderProvChip($('in-oc-prov'), d.data_source);

    var chain = Array.isArray(d.chain) ? d.chain : [];
    if (!chain.length) {
      setState(host, 'empty', 'No strikes returned',
        'The chain endpoint answered without a strike ladder.');
      return;
    }

    host.removeAttribute('data-state');
    host.innerHTML = '';

    if (String(d.data_source || '') !== 'live') {
      var note = document.createElement('p');
      note.className = 'tt-prov-note';
      note.textContent = 'Open interest, implied volatility and the greeks below are modelled, ' +
        'not read from the NSE chain. Levels are indicative only.';
      host.appendChild(note);
    }

    var pcr = d.pcr || {};
    var straddle = d.atm_straddle || {};

    var metrics = document.createElement('div');
    metrics.className = 'tt-metrics';
    [
      ['Spot', num(d.spot_price, 2)],
      ['ATM strike', num(d.atm_strike, 0)],
      ['Expiry', d.expiry],
      ['Lot size', d.lot_size],
      ['Max pain', num(d.max_pain_strike, 0)],
      ['PCR (OI)', num(pcr.pcr_oi, 2)],
      ['PCR (volume)', num(pcr.pcr_volume, 2)],
      ['PCR stance', pcr.sentiment || pcr.bias_badge],
      ['Straddle premium', num(straddle.combined_premium, 2)],
      ['Expected move', straddle.expected_move_pct !== undefined
        ? num(straddle.expected_move_pct, 2) + '%' : '—'],
      ['Breakeven range', straddle.lower_breakeven !== undefined
        ? num(straddle.lower_breakeven, 0) + ' – ' + num(straddle.upper_breakeven, 0) : '—'],
      // The engine randomises this value (options_engine.py), so it is labelled
      // rather than presented as a measured reading.
      ['IV rank (modelled)', num(d.iv_rank, 1)]
    ].forEach(function (pair) {
      var card = document.createElement('div');
      card.className = 'tt-metric';
      var l = document.createElement('span');
      l.className = 'tt-metric__label';
      l.textContent = pair[0];
      var v = document.createElement('span');
      v.className = 'tt-metric__value';
      setText(v, pair[1]);
      card.appendChild(l);
      card.appendChild(v);
      metrics.appendChild(card);
    });
    host.appendChild(metrics);

    // Only the strikes around the money are shown. A 25-strike ladder is 50
    // contracts of noise; the ATM window is where the decision is.
    var atmIdx = -1;
    chain.forEach(function (r, i) { if (r.is_atm) atmIdx = i; });
    if (atmIdx < 0) atmIdx = Math.floor(chain.length / 2);
    var lo = Math.max(0, atmIdx - 6);
    var hi = Math.min(chain.length, atmIdx + 7);

    var table = document.createElement('table');
    table.className = 'tt-table tt-oc';
    var thead = document.createElement('thead');
    thead.innerHTML =
      '<tr>' +
        '<th scope="col" class="tt-num">CE OI</th>' +
        '<th scope="col" class="tt-num">CE LTP</th>' +
        '<th scope="col" class="tt-num">CE IV</th>' +
        '<th scope="col">Strike</th>' +
        '<th scope="col" class="tt-num">PE IV</th>' +
        '<th scope="col" class="tt-num">PE LTP</th>' +
        '<th scope="col" class="tt-num">PE OI</th>' +
      '</tr>';
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    chain.slice(lo, hi).forEach(function (r) {
      var ce = r.call || {}, pe = r.put || {};
      var tr = document.createElement('tr');
      if (r.is_atm) tr.className = 'tt-oc__row--atm';
      if (String(r.strike) === String(d.max_pain_strike)) tr.className += ' tt-oc__row--pain';
      tr.innerHTML =
        '<td class="tt-num">' + esc(num(ce.oi, 0)) + '</td>' +
        '<td class="tt-num">' + esc(num(ce.ltp, 2)) + '</td>' +
        '<td class="tt-num">' + esc(num(ce.iv, 1)) + '</td>' +
        '<td class="tt-oc__strike tt-num">' + esc(num(r.strike, 0)) +
          (r.is_atm ? '<span class="tt-oc__tag">ATM</span>' : '') +
          (String(r.strike) === String(d.max_pain_strike) ? '<span class="tt-oc__tag">MAX PAIN</span>' : '') +
        '</td>' +
        '<td class="tt-num">' + esc(num(pe.iv, 1)) + '</td>' +
        '<td class="tt-num">' + esc(num(pe.ltp, 2)) + '</td>' +
        '<td class="tt-num">' + esc(num(pe.oi, 0)) + '</td>';
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  /* ── Status bar ───────────────────────────────────────────────────────── */
  var FEED_LABEL = {
    STREAMING: 'live',
    STALE: 'stale',
    CLOSED: 'market closed',
    SYNTHETIC: 'synthetic',
    OFFLINE: 'offline'
  };

  function renderStatus() {
    var services = state.services || {};
    var feed = services.DATA_FEED || '—';
    var mt5 = services.MT5 || '—';

    var feedStatus = String(feed).toUpperCase();
    // Real bars are the proof that the broker is reachable. "broker online" used
    // to be `mt5 === 'CONNECTED'` alone - the EXECUTION account flag - so any
    // session that reads prices without sending orders (paper mode, or a server
    // started without a broker client) announced "broker offline" while live
    // bars were streaming in. Reading prices and placing orders are separate
    // capabilities, so the chip now reports the link that prices come over, and
    // the feed label beside it still says plainly when bars are synthetic.
    var realBars = feedStatus === 'STREAMING' || feedStatus === 'STALE' || feedStatus === 'CLOSED';
    var online = realBars || mt5 === 'CONNECTED';

    setText($('status-feed'), FEED_LABEL[feedStatus] || String(feed).toLowerCase());

    // The age of the last SUCCESSFUL TELEMETRY POLL - not the age of the last
    // market tick. The label used to read "Last tick", which claimed otherwise
    // and still showed "0s ago" with the feed down, because the HTTP endpoint
    // kept answering. A number that cannot go stale is not a freshness check.
    var age = state.telemetryAt ? Math.round((Date.now() - state.telemetryAt) / 1000) : null;
    setText($('status-tick'), age === null ? '—' : agoText(age));
    setText($('status-symbols'), String(symbolList().length));
    setText($('status-positions'), String((state.positions || []).length));

    var conn = $('conn-chip');
    if (conn) {
      conn.className = 'tt-chip ' + (online ? 'tt-chip--buy' : 'tt-chip--none');
      conn.textContent = online ? 'broker online' : 'broker offline';
    }

    setText($('exec-mode'), (state.executionMode || '—') + (state.safeMode ? ' · SAFE' : ''));

    // Session state, derived from the symbol the user is looking at.
    var ms = state.symbol ? (state.marketStatuses[state.symbol] || {}) : {};
    var sessionEl = $('session-state');
    if (sessionEl) {
      var st = String(ms.status || '').toUpperCase();
      sessionEl.setAttribute('data-state', st === 'OPEN' ? 'open' : (st === 'CLOSED' ? 'closed' : 'pre'));
      var sessionText = ms.status
        ? ms.status + (ms.countdown_formatted ? ' · ' + ms.countdown_formatted : '')
        : '—';
      setText($('session-label'), sessionText);
      // The phone app bar caps the chip's width and ellipsises the label, which
      // would otherwise swallow the countdown. Keep the untruncated string as a
      // tooltip so the value is still reachable rather than merely lost.
      sessionEl.setAttribute('title', 'Market session: ' + sessionText);
    }

    var ver = $('status-version');
    if (ver && window.HMUI && window.HMUI.version) setText(ver, 'v' + window.HMUI.version);
  }

  /* ── Telemetry ────────────────────────────────────────────────────────── */
  function loadTelemetry() {
    var started = Date.now();
    return apiGet('/api/telemetry_state', TIMEOUT.fast).then(function (res) {
      var latency = $('status-latency');
      if (latency) setText(latency, (Date.now() - started) + ' ms');

      if (!res.ok || !res.data) {
        setText($('status-feed'), 'unreachable');
        if (latency) latency.className = 'tt-status__item';
        return;
      }

      var d = res.data;
      state.account = d.account || null;
      state.decisions = d.latest_decisions || {};
      state.marketStatuses = d.market_statuses || {};
      state.positions = d.positions || [];
      state.services = d.services || {};
      state.radar = d.radar_opportunities || [];
      state.executionMode = d.execution_mode;
      state.safeMode = d.safe_mode;
      state.telemetryAt = Date.now();
      state.serverTimestamp = d.timestamp;

      if (!state.symbol) {
        var syms = symbolList();
        if (syms.length) state.symbol = syms.indexOf('XAUUSD') >= 0 ? 'XAUUSD' : syms[0];
      }

      renderAccount();
      renderWatchlist();
      /* The positions panel is now tabbed (OPEN / HISTORY / PENDING). Each
         telemetry refresh must re-render the active tab rather than the OPEN
         body unconditionally — otherwise a trader on PENDING sees their list
         overwritten by "No open positions" every few seconds. The OPEN count
         badge is kept fresh even when another tab is active so the segmented
         control's tally matches the live book. */
      setText($('pos-tab-count-open'), String((state.positions || []).length));
      if (state.posTab === 'open') renderPositions();
      else if (state.posTab === 'history') renderHistoryInPosPanel();
      else if (state.posTab === 'pending') renderPendingInPosPanel();
      renderReasoning();
      renderRadar();
      renderStatus();

      // The analyst view is bound to the same decisions telemetry just
      // refreshed, so keep it current while it is on screen.
      if (state.view === 'analyst') {
        renderDevilAdvocate();
        renderQualityGate();
        renderObjections();
      }
    });
  }

  /* ── Analytics ────────────────────────────────────────────────────────── */
  function loadAnalytics() {
    loadReliability();
    loadRegimePolicy();
    renderAnalyticsMetrics();
    loadHistory();
    renderRisk();
  }

  function renderAnalyticsMetrics() {
    var host = $('analytics-metrics');
    var acc = state.account;
    if (!host) return;
    if (!acc) {
      setState(host, 'empty', 'No account data', 'Telemetry has not published an account snapshot.');
      return;
    }
    var ml = Number(acc.margin_level || 0);
    var tiles = [
      ['Balance', num(acc.balance, 2), acc.currency || ''],
      ['Equity', num(acc.equity, 2), acc.currency || ''],
      ['Open P&L', (Number(acc.profit) > 0 ? '+' : '') + num(acc.profit, 2), signClass(acc.profit)],
      ['Free margin', num(acc.free_margin, 2), ''],
      ['Margin used', num(acc.margin, 2), ''],
      ['Margin level', ml > 0 ? num(ml, 1) + '%' : 'flat', ''],
      ['Leverage', '1:' + num(acc.leverage, 0), ''],
      ['Trade allowed', acc.trade_allowed ? 'yes' : 'no', acc.trade_allowed ? 'tt-up' : 'tt-down']
    ];
    host.removeAttribute('data-state');
    host.innerHTML = tiles.map(function (t) {
      return '<div class="tt-metric">' +
        '<span class="tt-metric__label">' + esc(t[0]) + '</span>' +
        '<span class="tt-metric__value ' + esc(t[2]) + '">' + esc(t[1]) + '</span>' +
        '</div>';
    }).join('');
  }

  function loadReliability() {
    var host = $('reliability-body');
    apiGet('/api/intelligence/reliability', TIMEOUT.normal).then(function (res) {
      if (!res.ok || !res.data || !res.data.styles) {
        setState(host, 'error', 'Reliability unavailable', res.error || ('HTTP ' + res.status));
        return;
      }
      state.reliability = res.data.styles || [];
      var src = $('reliability-source');
      if (src && res.data.model) setText(src, res.data.model.source_path || 'neutral weights');

      host.removeAttribute('data-state');
      host.innerHTML = '<table class="tt-table"><thead><tr>' +
        '<th scope="col">Mode</th><th scope="col" class="tt-num">Weight</th>' +
        '<th scope="col" class="tt-num">Trades</th><th scope="col" class="tt-num">Exp (R)</th>' +
        '<th scope="col" class="tt-num">PF</th><th scope="col">Trust</th>' +
        '</tr></thead><tbody>' +
        state.reliability.map(function (s) {
          var w = Number(s.weight || 0);
          var barPct = Math.max(0, Math.min(100, w * 100));
          var cls = w >= 0.5 ? 'tt-up' : 'tt-down';
          return '<tr>' +
            '<td><span class="tt-symbol">' + esc(s.style) + '</span></td>' +
            '<td class="tt-num ' + cls + '">' + num(w, 4) + '</td>' +
            '<td class="tt-num">' + (s.trades !== undefined ? s.trades : '—') + '</td>' +
            '<td class="tt-num ' + signClass(s.expectancy_r) + '">' + num(s.expectancy_r, 4) + '</td>' +
            '<td class="tt-num">' + num(s.profit_factor, 3) + '</td>' +
            '<td><div class="tt-gauge"><div class="tt-gauge__bar">' +
              '<div class="tt-gauge__fill" style="width:' + barPct + '%;background:' +
              (w >= 0.5 ? 'var(--hm-bull)' : 'var(--hm-bear)') + '"></div>' +
            '</div></div></td>' +
            '</tr>';
        }).join('') +
        '</tbody></table>';
    });
  }

  /* ── Trade history ──────────────────────────────────────────────────── */
  /* Closed trades come from /api/history, which merges the SQLite journal with
     the broker's own deal history. `state.history` used to be written by
     nothing at all, so this table could only ever render its empty state. */
  function loadHistory() {
    var days = ($('hist-filter-days') || {}).value || '60';
    var body = $('hist-body');
    if (body) body.setAttribute('data-state', 'loading');
    apiGet('/api/history?limit=1000&days=' + encodeURIComponent(days), TIMEOUT.slow).then(function (res) {
      var rows = Array.isArray(res.data) ? res.data : ((res.data && (res.data.trades || res.data.history)) || []);
      state.history = rows;
      renderHistory();
      // The History tab on the positions panel reads the same list. Refresh it
      // here so a trader who flipped to History before /api/history resolved
      // sees the rows appear the moment they land, instead of a stale empty
      // state that then jumps to the table.
      renderHistoryInPosPanel();
      // Closed-trade exit markers live on the price chart, so the chart has to
      // be repainted once history arrives — not only when the tab is open.
      refreshChartDecorations();
    });
  }

  function historyPnl(t) {
    var v = t.realized_pnl;
    if (v === null || v === undefined || v === '') v = t.profit;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function historyFilters() {
    return {
      symbol: String(($('hist-filter-symbol') || {}).value || '').trim().toUpperCase(),
      side: String(($('hist-filter-side') || {}).value || 'ALL').toUpperCase(),
      source: String(($('hist-filter-source') || {}).value || 'ALL').toUpperCase(),
      outcome: String(($('hist-filter-outcome') || {}).value || 'ALL').toUpperCase()
    };
  }

  function filterHistory(rows) {
    var f = historyFilters();
    return (rows || []).filter(function (t) {
      if (f.symbol && String(t.symbol || '').toUpperCase().indexOf(f.symbol) < 0) return false;
      if (f.side !== 'ALL' && String(t.action || t.type || t.side || '').toUpperCase() !== f.side) return false;
      if (f.source !== 'ALL') {
        var exec = String(t.executor || '').toUpperCase();
        var isManual = /MANUAL|SL EXIT|TP EXIT|BROKER/.test(exec) || String(t.regime || '').toUpperCase() === 'MANUAL_EXECUTION';
        if (f.source === 'MANUAL' && !isManual) return false;
        if (f.source === 'AI' && isManual) return false;
      }
      if (f.outcome !== 'ALL') {
        var pnl = historyPnl(t);
        if (f.outcome === 'OPEN') { if (pnl !== null) return false; }
        else if (pnl === null) return false;
        else if (f.outcome === 'WIN' && pnl <= 0) return false;
        else if (f.outcome === 'LOSS' && pnl >= 0) return false;
        else if (f.outcome === 'FLAT' && pnl !== 0) return false;
      }
      return true;
    });
  }

  function renderPnlBreakdown(rows) {
    var hostSym = $('pnl-by-symbol');
    var hostSide = $('pnl-by-side');
    var hostExec = $('pnl-by-executor');
    if (!hostSym || !hostSide || !hostExec) return;

    if (!rows || !rows.length) {
      hostSym.innerHTML = '<span class="tt-muted">No closed trade data</span>';
      hostSide.innerHTML = '<span class="tt-muted">No closed trade data</span>';
      hostExec.innerHTML = '<span class="tt-muted">No closed trade data</span>';
      return;
    }

    var bySym = {}, bySide = {}, byExec = {};

    rows.forEach(function (t) {
      var sym = t.symbol || 'OTHER';
      var side = String(t.action || t.type || t.side || '').toUpperCase();
      if (!/BUY|SELL/.test(side)) side = 'OTHER';
      var exec = String(t.executor || 'OTHER');
      if (exec.indexOf('BOT') !== -1) exec = 'BOT (AI)';
      else if (exec.indexOf('MANUAL') !== -1) exec = 'MANUAL';
      else if (exec.indexOf('SL') !== -1 || exec.indexOf('TP') !== -1) exec = 'STOP / TP';

      var pnl = historyPnl(t);
      var validPnl = pnl !== null;
      var profit = validPnl ? pnl : 0;
      var isWin = profit > 0 ? 1 : 0;
      var isLoss = profit < 0 ? 1 : 0;

      // Symbol
      if (!bySym[sym]) bySym[sym] = { pnl: 0, count: 0, wins: 0, losses: 0, vol: 0 };
      bySym[sym].count++;
      bySym[sym].vol += Number(t.volume || 0);
      if (validPnl) {
        bySym[sym].pnl += profit;
        bySym[sym].wins += isWin;
        bySym[sym].losses += isLoss;
      }

      // Side
      if (!bySide[side]) bySide[side] = { pnl: 0, count: 0, wins: 0, losses: 0 };
      bySide[side].count++;
      if (validPnl) {
        bySide[side].pnl += profit;
        bySide[side].wins += isWin;
        bySide[side].losses += isLoss;
      }

      // Executor
      if (!byExec[exec]) byExec[exec] = { pnl: 0, count: 0, wins: 0, losses: 0 };
      byExec[exec].count++;
      if (validPnl) {
        byExec[exec].pnl += profit;
        byExec[exec].wins += isWin;
        byExec[exec].losses += isLoss;
      }
    });

    // Render Symbol Breakdown Table
    var symKeys = Object.keys(bySym).sort(function (a, b) { return bySym[b].pnl - bySym[a].pnl; });
    hostSym.innerHTML = '<table class="tt-table" style="font-size:0.75rem;">' +
      '<thead><tr><th>Symbol</th><th class="tt-num">Trades</th><th class="tt-num">Win%</th><th class="tt-num">Net P&amp;L</th></tr></thead><tbody>' +
      symKeys.map(function (s) {
        var d = bySym[s];
        var counted = d.wins + d.losses;
        var wr = counted ? Math.round((d.wins / counted) * 100) : 0;
        return '<tr><td><b>' + esc(s) + '</b></td>' +
          '<td class="tt-num">' + d.count + '</td>' +
          '<td class="tt-num">' + wr + '%</td>' +
          '<td class="tt-num ' + signClass(d.pnl) + '">' + (d.pnl > 0 ? '+' : '') + num(d.pnl, 2) + '</td></tr>';
      }).join('') + '</tbody></table>';

    // Render Side Breakdown Table
    var sideKeys = Object.keys(bySide);
    hostSide.innerHTML = '<table class="tt-table" style="font-size:0.75rem;">' +
      '<thead><tr><th>Side</th><th class="tt-num">Trades</th><th class="tt-num">Win%</th><th class="tt-num">Net P&amp;L</th></tr></thead><tbody>' +
      sideKeys.map(function (s) {
        var d = bySide[s];
        var counted = d.wins + d.losses;
        var wr = counted ? Math.round((d.wins / counted) * 100) : 0;
        var cls = s === 'BUY' ? 'tt-dir--buy' : (s === 'SELL' ? 'tt-dir--sell' : '');
        return '<tr><td><span class="tt-dir ' + cls + '">' + esc(s) + '</span></td>' +
          '<td class="tt-num">' + d.count + '</td>' +
          '<td class="tt-num">' + wr + '%</td>' +
          '<td class="tt-num ' + signClass(d.pnl) + '">' + (d.pnl > 0 ? '+' : '') + num(d.pnl, 2) + '</td></tr>';
      }).join('') + '</tbody></table>';

    // Render Executor Breakdown Table
    var execKeys = Object.keys(byExec);
    hostExec.innerHTML = '<table class="tt-table" style="font-size:0.75rem;">' +
      '<thead><tr><th>Executor</th><th class="tt-num">Trades</th><th class="tt-num">Win%</th><th class="tt-num">Net P&amp;L</th></tr></thead><tbody>' +
      execKeys.map(function (e) {
        var d = byExec[e];
        var counted = d.wins + d.losses;
        var wr = counted ? Math.round((d.wins / counted) * 100) : 0;
        return '<tr><td>' + esc(e) + '</td>' +
          '<td class="tt-num">' + d.count + '</td>' +
          '<td class="tt-num">' + wr + '%</td>' +
          '<td class="tt-num ' + signClass(d.pnl) + '">' + (d.pnl > 0 ? '+' : '') + num(d.pnl, 2) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  function showAiExplanationModal(ticket) {
    var modal = $('ai-explanation-modal');
    var content = $('ai-modal-content');
    var title = $('ai-modal-title');
    if (!modal || !content) return;

    var trades = state.history || [];
    var trade = null;
    for (var i = 0; i < trades.length; i++) {
      if (String(trades[i].ticket || trades[i].id) === String(ticket)) {
        trade = trades[i];
        break;
      }
    }

    if (!trade) {
      var dec = state.symbol ? state.decisions[state.symbol] : null;
      if (dec) trade = dec;
    }

    if (!trade) {
      content.innerHTML = '<p class="tt-muted">No details found for ticket #' + esc(ticket) + '</p>';
      modal.hidden = false;
      modal.style.display = 'flex';
      return;
    }

    var sym = trade.symbol || '';
    var side = String(trade.action || trade.type || trade.bias || 'BUY').toUpperCase();
    if (title) setText(title, 'AI Intel: ' + sym + ' ' + side + ' #' + (trade.ticket || ticket));

    var feat = {};
    if (trade.features_json) {
      try { feat = typeof trade.features_json === 'string' ? JSON.parse(trade.features_json) : trade.features_json; } catch(e){}
    }

    var aiScore = trade.ai_score || feat.ai_score || (trade.model_confidence ? Math.round(trade.model_confidence * 100) : '—');
    var strat = trade.strategy || feat.strategy || 'Adaptive Dissection';
    var ev = trade.expected_value !== undefined ? num(trade.expected_value, 2) : (feat.expected_value ? num(feat.expected_value, 2) : '—');
    var rr = trade.risk_reward_ratio !== undefined ? num(trade.risk_reward_ratio, 2) : (feat.rr_ratio ? num(feat.rr_ratio, 2) : '—');
    var regime = trade.regime || '—';

    var threats = [];
    if (trade.threats_json) {
      try { threats = typeof trade.threats_json === 'string' ? JSON.parse(trade.threats_json) : trade.threats_json; } catch(e){}
    } else if (trade.risk_factors) {
      threats = trade.risk_factors;
    }

    content.innerHTML =
      '<div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(130px, 1fr));gap:8px;margin-bottom:14px;">' +
        '<div class="tt-metric"><span class="tt-metric__label">AI Score</span><span class="tt-metric__value" style="color:#f59e0b;font-weight:700;">' + esc(aiScore) + '</span></div>' +
        '<div class="tt-metric"><span class="tt-metric__label">Strategy</span><span class="tt-metric__value">' + esc(strat) + '</span></div>' +
        '<div class="tt-metric"><span class="tt-metric__label">Regime</span><span class="tt-metric__value">' + esc(regime) + '</span></div>' +
        '<div class="tt-metric"><span class="tt-metric__label">Expected Value</span><span class="tt-metric__value">' + esc(ev) + '</span></div>' +
        '<div class="tt-metric"><span class="tt-metric__label">R:R Ratio</span><span class="tt-metric__value">' + esc(rr) + '</span></div>' +
      '</div>' +
      '<div style="margin-top:12px;border-top:1px solid var(--hm-border);padding-top:10px;">' +
        '<div style="font-weight:600;font-size:0.8rem;text-transform:uppercase;color:#3b82f6;margin-bottom:6px;">🎯 3-Tier Smart TP/SL Milestones</div>' +
        '<div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));gap:8px;font-size:0.85rem;">' +
          '<div style="background:rgba(59,130,246,0.08);padding:6px 8px;border-radius:4px;border:1px solid rgba(59,130,246,0.25);">' +
            '<div style="font-size:0.75rem;color:#93c5fd;font-weight:600;">TP1 (Scale-Out)</div>' +
            '<div style="font-size:0.95rem;font-weight:700;">' + (trade.tp1_price || trade.tp1 ? formatPrice(trade.tp1_price || trade.tp1, sym) : '—') + '</div>' +
            '<div style="font-size:0.7rem;color:var(--hm-text-muted);">50% Close &rarr; BE Lock</div>' +
          '</div>' +
          '<div style="background:rgba(16,185,129,0.08);padding:6px 8px;border-radius:4px;border:1px solid rgba(16,185,129,0.25);">' +
            '<div style="font-size:0.75rem;color:#6ee7b7;font-weight:600;">TP2 (Structure)</div>' +
            '<div style="font-size:0.95rem;font-weight:700;">' + (trade.tp2_price || trade.tp2 || trade.take_profit ? formatPrice(trade.tp2_price || trade.tp2 || trade.take_profit, sym) : '—') + '</div>' +
            '<div style="font-size:0.7rem;color:var(--hm-text-muted);">Primary Target (+2R)</div>' +
          '</div>' +
          '<div style="background:rgba(168,85,247,0.08);padding:6px 8px;border-radius:4px;border:1px solid rgba(168,85,247,0.25);">' +
            '<div style="font-size:0.75rem;color:#d8b4fe;font-weight:600;">TP3 (Runner)</div>' +
            '<div style="font-size:0.95rem;font-weight:700;">' + (trade.tp3_price || trade.tp3 ? formatPrice(trade.tp3_price || trade.tp3, sym) : '—') + '</div>' +
            '<div style="font-size:0.7rem;color:var(--hm-text-muted);">ATR Ratchet Trailing</div>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div style="margin-top:12px;border-top:1px solid var(--hm-border);padding-top:10px;">' +
        '<div style="font-weight:600;font-size:0.8rem;text-transform:uppercase;color:var(--hm-text-muted);margin-bottom:6px;">Thesis &amp; Confluence Evidence</div>' +
        '<ul style="margin:0 0 10px 18px;padding:0;font-size:0.85rem;line-height:1.4;">' +
          '<li>Setup generated by <b>' + esc(strat) + '</b> under <b>' + esc(regime) + '</b> regime.</li>' +
          '<li>Multi-agent consensus AI Score: <b>' + esc(aiScore) + ' / 100</b>.</li>' +
          (trade.session_name ? '<li>Session context: <b>' + esc(trade.session_name) + '</b>.</li>' : '') +
        '</ul>' +
      '</div>' +
      (threats && threats.length ?
        '<div style="margin-top:12px;border-top:1px solid var(--hm-border);padding-top:10px;">' +
          '<div style="font-weight:600;font-size:0.8rem;text-transform:uppercase;color:#ef4444;margin-bottom:6px;">Risk &amp; Invalidation Vectors</div>' +
          '<ul style="margin:0 0 0 18px;padding:0;font-size:0.85rem;color:#fca5a5;line-height:1.4;">' +
            threats.map(function(th){ return '<li>' + esc(th) + '</li>'; }).join('') +
          '</ul>' +
        '</div>' : '');

    modal.hidden = false;
    modal.style.display = 'flex';
  }

  function closeAiExplanationModal() {
    var modal = $('ai-explanation-modal');
    if (modal) {
      modal.hidden = true;
      modal.style.display = 'none';
    }
  }

  function renderHistory() {
    var body = $('hist-body');
    if (!body) return;
    var all = state.history || [];
    var rows = filterHistory(all);
    setText($('hist-count'), String(rows.length));

    var summary = $('hist-summary');
    if (summary) {
      var net = 0, wins = 0, losses = 0, counted = 0;
      rows.forEach(function (t) {
        var p = historyPnl(t);
        if (p === null) return;
        net += p; counted++;
        if (p > 0) wins++; else if (p < 0) losses++;
      });
      var text = rows.length === all.length
        ? rows.length + ' trades'
        : rows.length + ' of ' + all.length + ' trades';
      if (counted) {
        text += ' · net ' + (net > 0 ? '+' : '') + num(net, 2)
          + ' · ' + wins + 'W / ' + losses + 'L'
          + ' · win rate ' + num(counted ? (wins / counted) * 100 : 0, 0) + '%';
      } else {
        text += ' · no realised P&L recorded';
      }
      setText(summary, text);
    }

    renderPnlBreakdown(rows);

    if (!rows.length) {
      setState(body, 'empty', all.length ? 'No trades match these filters' : 'No closed trades',
        all.length ? 'Widen the filters above.' : 'Nothing has been closed in this window yet.');
      return;
    }

    body.removeAttribute('data-state');
    body.innerHTML = rows.map(function (t) {
      var sym = t.symbol || '';
      var side = String(t.action || t.type || t.side || '').toUpperCase();
      var dirCls = /BUY|LONG/.test(side) ? 'tt-dir--buy' : 'tt-dir--sell';
      var pnl = historyPnl(t);
      var exec = String(t.executor || '—');
      var manual = /MANUAL|SL EXIT|TP EXIT|BROKER/.test(exec.toUpperCase());
      var closedAt = t.closed_at || null;
      var stamp = closedAt || t.timestamp;
      var when = stamp ? String(stamp).replace('T', ' ').replace(/\.\d+.*$/, '').slice(0, 19) : '—';
      var whenCell = closedAt
        ? esc(when)
        : (stamp ? esc(when) + ' <span class="tt-muted">(open)</span>' : '—');
      var ticketId = esc(t.ticket || t.id || '—');
      return '<tr>' +
        '<td class="tt-muted">' + ticketId + '</td>' +
        '<td><span class="tt-symbol">' + esc(sym) + '</span></td>' +
        '<td><span class="tt-dir ' + dirCls + '">' + esc(side || '—') + '</span></td>' +
        '<td class="' + (manual ? 'tt-muted' : '') + '">' + esc(exec) + '</td>' +
        '<td class="tt-num">' + num(t.volume, 2) + '</td>' +
        '<td class="tt-num">' + formatPrice(t.entry_price, sym) + '</td>' +
        '<td class="tt-num tt-down">' + (Number(t.sl) > 0 ? formatPrice(t.sl, sym) : '—') + '</td>' +
        '<td class="tt-num tt-up">' + (Number(t.tp) > 0 ? formatPrice(t.tp, sym) : '—') + '</td>' +
        '<td class="tt-num ' + (pnl === null ? 'tt-muted' : signClass(pnl)) + '">' +
          (pnl === null ? '—' : (pnl > 0 ? '+' : '') + num(pnl, 2)) + '</td>' +
        '<td class="tt-muted" title="' + (closedAt ? 'Closed' : 'Opened; not yet closed') + '">' +
          whenCell + '</td>' +
        '<td style="text-align:center;"><button type="button" class="tt-btn tt-btn--ghost tt-btn--xs" data-ai-ticket="' + ticketId + '" style="font-size:0.75rem;padding:2px 8px;border:1px solid var(--hm-border);border-radius:4px;cursor:pointer;">🧠 AI Intel</button></td>' +
        '</tr>';
    }).join('');
  }

  function renderRisk() {
    var host = $('risk-metrics');
    var guardHost = $('risk-guard-metrics');
    var exp = $('exposure-body');
    var positions = state.positions || [];
    var acc = state.account;

    if (!host) return;
    if (!acc) {
      setState(host, 'empty', 'No account data', null);
      setState(exp, 'empty', 'No exposure data', null);
      return;
    }

    var gross = 0, net = 0;
    var bySymbol = {};
    positions.forEach(function (p) {
      var v = Number(p.volume || 0);
      gross += Math.abs(v);
      var side = String(p.type || p.side || '').toUpperCase();
      net += /BUY|LONG/.test(side) ? v : -v;
      bySymbol[p.symbol] = (bySymbol[p.symbol] || 0) + v;
    });

    host.removeAttribute('data-state');
    host.innerHTML = [
      ['Open positions', String(positions.length)],
      ['Gross volume', num(gross, 2)],
      ['Net volume', (net > 0 ? '+' : '') + num(net, 2)],
      ['Margin used', num(acc.margin, 2)]
    ].map(function (t) {
      return '<div class="tt-metric">' +
        '<span class="tt-metric__label">' + esc(t[0]) + '</span>' +
        '<span class="tt-metric__value">' + esc(t[1]) + '</span></div>';
    }).join('');

    // Phase B / Phase D: Fetch & Render Real-Time Risk Guard Diagnostics
    if (guardHost) {
      apiGet('/api/risk_status', TIMEOUT.quick).then(function (res) {
        if (!res.ok || !res.data) return;
        var r = res.data;
        var cbActive = r.circuit_breaker_active;
        var btnStop = $('btn-emergency-stop');
        var btnResume = $('btn-resume-trading');
        if (btnStop && btnResume) {
          if (cbActive) {
            btnStop.style.display = 'none';
            btnResume.style.display = 'inline-block';
          } else {
            btnStop.style.display = 'inline-block';
            btnResume.style.display = 'none';
          }
        }
        guardHost.innerHTML = [
          ['Daily loss', num(r.daily_loss_pct, 2) + '% / ' + num(r.max_daily_loss_pct, 1) + '% max'],
          ['Total DD', num(r.total_dd_pct, 2) + '% / ' + num(r.max_drawdown_pct, 1) + '% max'],
          ['DD multiplier', num(r.drawdown_multiplier, 2) + 'x'],
          ['Circuit breaker', cbActive ? '🚨 TRIPPED (' + (r.circuit_breaker_cooldown_sec || 0) + 's)' : '✅ ARMED']
        ].map(function (t) {
          return '<div class="tt-metric">' +
            '<span class="tt-metric__label">' + esc(t[0]) + '</span>' +
            '<span class="tt-metric__value" style="font-weight:600;">' + esc(t[1]) + '</span></div>';
        }).join('');
      });
    }

    var syms = Object.keys(bySymbol);
    if (!syms.length) {
      setState(exp, 'empty', 'No open exposure', null);
      return;
    }
    exp.removeAttribute('data-state');
    exp.innerHTML = '<table class="tt-table"><thead><tr>' +
      '<th scope="col">Symbol</th><th scope="col" class="tt-num">Volume</th>' +
      '</tr></thead><tbody>' +
      syms.sort().map(function (s) {
        return '<tr><td><span class="tt-symbol">' + esc(s) + '</span></td>' +
               '<td class="tt-num">' + num(bySymbol[s], 2) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  /* ── Regime policy ────────────────────────────────────────────────────── */
  // Which geometry to run in which market condition, and which conditions the
  // optimiser has switched off. Read from the report the optimiser writes; the
  // dashboard never computes or assumes a policy of its own.
  function loadRegimePolicy() {
    var body = $('regime-policy-body');
    var host = $('regime-policy-metrics');
    var src = $('regime-policy-source');

    apiGet('/api/backtest/regime-policy', TIMEOUT.normal).then(function (res) {
      // 503 is not a failure here: it is the honest "the optimiser has not been
      // run" answer. Showing it as an error would train the operator to ignore
      // it, and showing an empty policy would imply the engine has nothing to
      // trade — a different and much more alarming claim.
      if (res.status === 503) {
        var reason = (res.data && res.data.error) || 'no regime policy report';
        setState(body, 'empty', 'No regime policy yet', reason);
        if (host) setState(host, 'empty', 'Not optimised', reason);
        if (src) src.textContent = 'never run';
        return;
      }
      if (!res.ok || !res.data || res.data.status !== 'OK') {
        setState(body, 'error', 'Regime policy unavailable',
          res.error || ('HTTP ' + res.status));
        if (host) setState(host, 'error', 'Unavailable', null);
        if (src) src.textContent = 'unavailable';
        return;
      }
      if (src) {
        src.textContent = (res.data.source_report || 'report') +
          (res.data.age_seconds != null ? ' · ' + agoText(res.data.age_seconds) : '');
      }
      renderRegimePolicy(res.data);
    });
  }

  function renderRegimePolicy(data) {
    var body = $('regime-policy-body');
    var host = $('regime-policy-metrics');
    if (!body) return;

    var policy = data.policy || {};
    var modes = Object.keys(policy).sort();
    var rows = [];
    var totalRegimes = 0, totalEnabled = 0, totalOwn = 0;

    modes.forEach(function (style) {
      var m = policy[style];
      if (!m || m.error) return;
      var regimes = m.regimes || {};
      Object.keys(regimes).sort().forEach(function (name) {
        var r = regimes[name] || {};
        totalRegimes += 1;
        if (r.enabled) totalEnabled += 1;
        if (r.uses_own_geometry) totalOwn += 1;

        var g = r.geometry || {};
        var geomText = g.tp_r != null ? ('tp ' + num(g.tp_r, 2)) : '—';
        // The quantile is a selectivity statement, so show it as one: "top 1%"
        // is what an operator needs, not "0.99".
        var q = Number(r.min_score_quantile);
        var select = (isFinite(q) && q > 0)
          ? 'top ' + num((1 - q) * 100, 0) + '%'
          : 'all setups';
        var base = r.baseline_expectancy_r;
        var expText = (base == null)
          ? '—'
          : (Number(base) > 0 ? '+' : '') + num(base, 4) + 'R';

        rows.push('<tr>' +
          '<td><span class="tt-chip tt-chip--muted">' + esc(style) + '</span></td>' +
          '<td><span class="tt-symbol">' + esc(name) + '</span></td>' +
          '<td><span class="tt-dir tt-dir--' + (r.enabled ? 'buy' : 'sell') + '">' +
            (r.enabled ? 'yes' : 'no') + '</span></td>' +
          '<td class="tt-num">' + esc(geomText) + ' ' +
            (r.uses_own_geometry
              ? '<span class="tt-chip tt-chip--high">own</span>'
              : '<span class="tt-chip tt-chip--muted">pooled</span>') + '</td>' +
          '<td class="tt-num">' + esc(select) + '</td>' +
          '<td class="tt-num">' +
            (r.baseline_trades == null ? '—' : num(r.baseline_trades, 0)) + '</td>' +
          '<td class="tt-num ' + signClass(base) + '">' + esc(expText) + '</td>' +
          '<td class="tt-hint">' + esc(r.basis || r.reason || '') + '</td>' +
          '</tr>');
      });
    });

    if (!rows.length) {
      setState(body, 'empty', 'Policy table is empty',
        'The report contains no regime rows.');
      if (host) setState(host, 'empty', 'No rows', null);
      return;
    }

    body.removeAttribute('data-state');
    body.innerHTML = rows.join('');

    if (host) {
      host.removeAttribute('data-state');
      host.innerHTML = [
        ['Modes', String(modes.length)],
        ['Conditions', String(totalRegimes)],
        ['Tradeable', String(totalEnabled)],
        ['Own geometry', String(totalOwn)],
        ['Objective', String(data.objective || '—')]
      ].map(function (t) {
        return '<div class="tt-metric">' +
          '<span class="tt-metric__label">' + esc(t[0]) + '</span>' +
          '<span class="tt-metric__value">' + esc(t[1]) + '</span></div>';
      }).join('');
    }
  }

  /* ── Backtest ─────────────────────────────────────────────────────────── */
  /* The value each axis collapses to when its box is unticked. Kept identical to
     the console's own defaults so both front ends submit the same grid. */
  var COLLAPSED_SPACE = {
    tp_r: [2.5],
    be_trigger_r: [null],
    fast_cash_r: [null],
    trail_atr: [null],
    min_score_quantiles: [0.0, 0.97]
  };

  /* Used only if /api/backtest/meta has not arrived yet. Mirrors
     jarvis.backtesting.optimizer.GeometrySpace — the full product is 1920. */
  var FALLBACK_SPACE = {
    tp_r: [1.0, 1.5, 2.0, 2.5, 3.0],
    be_trigger_r: [null, 0.5, 1.0, 1.5],
    fast_cash_r: [null, 0.75, 1.0, 1.5],
    trail_atr: [null, 1.0, 1.5, 2.0],
    min_score_quantiles: [0.0, 0.5, 0.75, 0.9, 0.97, 0.99]
  };

  function dimensions() {
    var m = state.backtestMeta;
    return (m && m.default_space && Object.keys(m.default_space).length)
      ? m.default_space : FALLBACK_SPACE;
  }

  /* Tick a box to search that axis; leave it unticked to pin it to one value.
     The old code sent only the *names* of the ticked axes as `grid_dimensions`,
     a key the server never read, so every run silently searched all 1920. */
  function buildSpace() {
    var dims = dimensions();
    var space = {};
    var total = 1;
    Object.keys(dims).forEach(function (dim) {
      var values = dims[dim];
      if (!Array.isArray(values) || !values.length) return;
      var box = document.querySelector('[data-grid-dim="' + dim + '"]');
      var used = (box && !box.checked) ? (COLLAPSED_SPACE[dim] || [values[0]]) : values;
      space[dim] = used.slice();
      total *= used.length;
    });
    return { space: space, count: total };
  }

  function updateGridHint() {
    var hint = $('bt-grid-hint');
    if (!hint) return;
    var built = buildSpace();
    var names = Object.keys(built.space).filter(function (d) {
      var box = document.querySelector('[data-grid-dim="' + d + '"]');
      return box && box.checked;
    });
    setText(hint, built.count.toLocaleString() + ' geometries per mode'
      + (names.length ? ' — searching ' + names.length + ' of ' + Object.keys(built.space).length + ' axes'
                      : ' — all axes pinned'));
  }

  function loadBacktestMeta() {
    apiGet('/api/backtest/meta', TIMEOUT.normal).then(function (res) {
      if (!res.ok || !res.data) return;
      var meta = res.data;
      state.backtestMeta = meta;

      var eng = $('bt-engine');
      if (eng) {
        eng.textContent = meta.orchestrator_attached ? 'engine attached' : 'engine detached';
        eng.className = 'tt-chip ' + (meta.orchestrator_attached ? 'tt-chip--medium' : 'tt-chip--muted');
      }

      // Objectives and modes come from the server, not from this file.
      var objSel = $('bt-objective');
      if (objSel && objSel.options.length === 0 && meta.objectives) {
        meta.objectives.forEach(function (o) {
          var opt = document.createElement('option');
          opt.value = o; opt.textContent = o;
          objSel.appendChild(opt);
        });
      }

      var modeSel = $('bt-modes');
      if (modeSel && modeSel.options.length === 0 && meta.styles) {
        meta.styles.forEach(function (s) {
          var opt = document.createElement('option');
          opt.value = s; opt.textContent = s; opt.selected = true;
          modeSel.appendChild(opt);
        });
      }

      var grid = $('bt-grid');
      if (grid && !grid.children.length && meta.default_space) {
        Object.keys(meta.default_space).forEach(function (dim) {
          var values = meta.default_space[dim];
          var count = Array.isArray(values) ? values.length : 0;
          var label = document.createElement('label');
          label.className = 'tt-check';
          label.style.minWidth = '132px';
          label.innerHTML =
            '<input type="checkbox" data-grid-dim="' + esc(dim) + '" checked>' +
            '<span>' + esc(dim) + '</span>' +
            '<span class="tt-muted">(' + count + ')</span>';
          grid.appendChild(label);
        });
        grid.addEventListener('change', function (ev) {
          if (ev.target && ev.target.hasAttribute('data-grid-dim')) updateGridHint();
        });
        updateGridHint();
      }

      var hint = $('bt-universe-hint');
      if (hint && meta.defaults) {
        setText(hint, 'Defaults: min trades ' + (meta.defaults.min_trades !== undefined ? meta.defaults.min_trades : '—') +
          ', max DD ' + (meta.defaults.max_dd_r !== undefined ? meta.defaults.max_dd_r : '—') + 'R');
      }
    });
  }

  function collectSpec() {
    var modes = Array.prototype.filter.call($('bt-modes').options, function (o) { return o.selected; })
      .map(function (o) { return o.value; });
    var rawSymbols = ($('bt-symbols').value || '').split(/[\s,]+/).filter(Boolean)
      .map(function (s) { return s.toUpperCase(); });

    return {
      label: 'dashboard',
      objective: $('bt-objective').value,
      modes: modes,
      symbols: rawSymbols,
      min_trades: Number($('bt-mintrades').value || 30),
      max_dd_r: Number($('bt-maxdd').value || 40),
      passes: Number($('bt-passes').value || 3),
      max_evaluations: Number($('bt-evals').value || 400),
      walk_forward_split: Number($('bt-split').value || 70) / 100,
      space: buildSpace().space
    };
  }

  function runBacktest(ev) {
    if (ev) ev.preventDefault();
    var btn = $('bt-run');
    if (btn) btn.setAttribute('aria-disabled', 'true');
    setText($('bt-status'), 'queued');
    var log = $('bt-log');
    if (log) log.textContent = 'Submitting… (' + buildSpace().count.toLocaleString() + ' geometries per mode)';

    apiPost('/api/backtest/run', collectSpec(), TIMEOUT.slow).then(function (res) {
      if (btn) btn.removeAttribute('aria-disabled');
      if (!res.ok || !res.data) {
        setText($('bt-status'), 'failed');
        if (log) log.textContent = 'Submit failed: ' + (res.error || ('HTTP ' + res.status));
        toast('Backtest could not be queued', 'error');
        return;
      }
      var jobId = res.data.job_id || (res.data.job && res.data.job.job_id);
      if (!jobId) {
        setText($('bt-status'), 'failed');
        if (log) log.textContent = 'Server returned no job id.';
        return;
      }
      state.activeJob = jobId;
      var cancel = $('bt-cancel');
      if (cancel) cancel.removeAttribute('aria-disabled');
      if (log) log.textContent = 'Job ' + jobId + ' queued.';
      toast('Backtest queued: ' + jobId);
      pollJob(jobId);
    });
  }

  function pollJob(jobId) {
    if (timers.job) clearTimeout(timers.job);
    apiGet('/api/backtest/jobs/' + encodeURIComponent(jobId), TIMEOUT.fast).then(function (res) {
      var job = (res.data && (res.data.job || res.data)) || null;
      if (!res.ok || !job) {
        setText($('bt-status'), 'unknown');
        return;
      }
      var status = String(job.status || '').toUpperCase();
      setText($('bt-status'), status.toLowerCase());
      var chip = $('bt-status');
      if (chip) {
        chip.className = 'tt-chip ' +
          (status === 'DONE' ? 'tt-chip--buy' :
           status === 'FAILED' ? 'tt-chip--sell' :
           status === 'RUNNING' ? 'tt-chip--medium' : 'tt-chip--none');
      }

      var progress = Number(job.progress || 0);
      var fill = $('bt-progress-fill');
      if (fill) fill.style.width = Math.max(0, Math.min(100, progress)) + '%';
      var bar = $('bt-progress');
      if (bar) bar.setAttribute('aria-valuenow', String(Math.round(progress)));

      var log = $('bt-log');
      if (log) {
        var lines = job.progress_lines || job.log || [];
        if (typeof lines === 'string') lines = [lines];
        log.textContent = lines.slice(-14).join('\n');
      }

      if (status === 'DONE') {
        loadJobResult(jobId);
        loadJobs();
        var cancel = $('bt-cancel');
        if (cancel) cancel.setAttribute('aria-disabled', 'true');
        return;
      }
      if (status === 'FAILED' || status === 'CANCELLED') {
        if (log) log.textContent = (job.error || status) + '\n' + (log.textContent || '');
        loadJobs();
        var cancel2 = $('bt-cancel');
        if (cancel2) cancel2.setAttribute('aria-disabled', 'true');
        return;
      }
      timers.job = setTimeout(function () { pollJob(jobId); }, POLL.jobs);
    });
  }

  function loadJobResult(jobId) {
    apiGet('/api/backtest/jobs/' + encodeURIComponent(jobId) + '/result', TIMEOUT.slow).then(function (res) {
      if (!res.ok || !res.data) return;
      renderBacktestResult(res.data);
    });
  }

  /* The optimiser's report is per *trading style*, not per symbol — one row per
     mode carrying the pooled in-sample / out-of-sample split, plus a separate
     `series` list describing what data each symbol-mode contributed. Older code
     looked for `per_symbol` / `symbols`, which this report never produces, so a
     perfectly good run always rendered as "no results". */
  function unwrapReport(payload) {
    if (!payload || typeof payload !== 'object') return {};
    var candidates = [
      payload.report, payload.result,
      payload.job && payload.job.result,
      payload.job && payload.job.report,
      payload.job, payload
    ];
    for (var i = 0; i < candidates.length; i++) {
      var c = candidates[i];
      if (c && typeof c === 'object' && (c.modes || c.series || c.per_symbol)) return c;
    }
    return payload || {};
  }

  function geometryLabel(g) {
    if (!g || typeof g !== 'object') return '—';
    var bits = ['tp ' + (g.tp_r !== null && g.tp_r !== undefined ? num(g.tp_r, 2) : '—')];
    bits.push('be ' + (g.be_trigger_r === null || g.be_trigger_r === undefined ? 'off' : num(g.be_trigger_r, 2)));
    bits.push('pc ' + (g.fast_cash_r === null || g.fast_cash_r === undefined ? 'off' : num(g.fast_cash_r, 2)));
    bits.push('trail ' + (g.trail_atr === null || g.trail_atr === undefined ? 'off' : num(g.trail_atr, 2)));
    return bits.join(' · ');
  }

  function renderBacktestResult(payload) {
    var host = $('bt-results');
    if (!host) return;
    var report = unwrapReport(payload);

    var modes = report.modes || null;
    var series = report.series || null;
    var legacy = report.per_symbol || (Array.isArray(report.symbols) ? report.symbols : null);

    var meta = $('bt-result-meta');
    if (meta) {
      var spec = report.spec || {};
      var bits = [];
      if (spec.objective) bits.push('objective ' + spec.objective);
      if (modes) bits.push(modes.length + (modes.length === 1 ? ' mode' : ' modes'));
      if (series) bits.push(series.length + ' series');
      if (report.elapsed_seconds !== undefined) bits.push(num(report.elapsed_seconds, 1) + 's');
      if (report.evaluations !== undefined) bits.push(report.evaluations + ' evals');
      if (report.cache_hit_rate !== undefined) bits.push('cache ' + num(report.cache_hit_rate * 100, 0) + '%');
      meta.textContent = bits.join(' · ') || '—';
    }

    if (!modes && !series && !legacy) {
      setState(host, 'empty', 'No results in this job',
        'The job finished but produced no report body. Check the progress log.');
      return;
    }

    var html = '';

    if (modes && modes.length) {
      html += '<div class="tt-subhead"><span class="tt-panel__title">Per style</span></div>';
      html += '<table class="tt-table"><thead><tr>' +
        '<th scope="col">Style</th><th scope="col">TF</th>' +
        '<th scope="col" class="tt-num">Series</th>' +
        '<th scope="col" class="tt-num">IS trades</th><th scope="col" class="tt-num">IS exp (R)</th>' +
        '<th scope="col" class="tt-num">OOS trades</th><th scope="col" class="tt-num">OOS exp (R)</th>' +
        '<th scope="col" class="tt-num">PF</th><th scope="col" class="tt-num">DD (R)</th>' +
        '<th scope="col">Best geometry</th><th scope="col">Verdict</th>' +
        '</tr></thead><tbody>' +
        modes.map(function (m) {
          var is_ = m.in_sample || {};
          var oos = m.out_of_sample || {};
          var wf = m.walk_forward || {};
          var verdict, vCls;
          if (m.feasible && wf.generalises) { verdict = 'generalisable'; vCls = 'tt-up'; }
          else if (m.feasible) { verdict = 'in-sample only'; vCls = 'tt-muted'; }
          else { verdict = 'no feasible geometry'; vCls = 'tt-down'; }
          var isExp = is_.expectancy_r, oosExp = oos.expectancy_r;
          return '<tr>' +
            '<td><span class="tt-symbol">' + esc(m.style || '—') + '</span></td>' +
            '<td class="tt-muted">' + esc(m.primary_timeframe || '—') + '</td>' +
            '<td class="tt-num">' + num(m.series_count, 0) + '</td>' +
            '<td class="tt-num">' + num(is_.trades, 0) + '</td>' +
            '<td class="tt-num ' + signClass(isExp) + '">' + (isExp !== undefined ? num(isExp, 4) : '—') + '</td>' +
            '<td class="tt-num">' + num(oos.trades, 0) + '</td>' +
            '<td class="tt-num ' + signClass(oosExp) + '">' + (oosExp !== undefined ? num(oosExp, 4) : '—') + '</td>' +
            '<td class="tt-num">' + num((m.full_window || {}).profit_factor, 3) + '</td>' +
            '<td class="tt-num">' + num((m.full_window || {}).max_dd_r, 2) + '</td>' +
            '<td class="tt-truncate tt-muted" style="max-width:210px" title="' +
              esc(m.best_geometry_key || '') + '">' + esc(geometryLabel(m.best_geometry)) + '</td>' +
            '<td class="' + vCls + '">' + esc(verdict) + '</td>' +
            '</tr>';
        }).join('') + '</tbody></table>';
    }

    if (series && series.length) {
      html += '<div class="tt-subhead"><span class="tt-panel__title">Data coverage</span></div>';
      html += '<table class="tt-table"><thead><tr>' +
        '<th scope="col">Symbol</th><th scope="col">Style</th><th scope="col">TF</th>' +
        '<th scope="col" class="tt-num">Bars</th><th scope="col" class="tt-num">Candidates</th>' +
        '</tr></thead><tbody>' +
        series.map(function (s) {
          return '<tr>' +
            '<td><span class="tt-symbol">' + esc(s.symbol) + '</span></td>' +
            '<td class="tt-muted">' + esc(s.style || '—') + '</td>' +
            '<td class="tt-muted">' + esc(s.timeframe || '—') + '</td>' +
            '<td class="tt-num">' + num(s.bars, 0) + '</td>' +
            '<td class="tt-num">' + num(s.candidates, 0) + '</td>' +
            '</tr>';
        }).join('') + '</tbody></table>';
    }

    if (legacy && legacy.length && !modes) {
      html += '<table class="tt-table"><thead><tr>' +
        '<th scope="col">Symbol</th><th scope="col">Style</th>' +
        '<th scope="col" class="tt-num">Trades</th><th scope="col" class="tt-num">Exp (R)</th>' +
        '<th scope="col" class="tt-num">Total R</th><th scope="col" class="tt-num">PF</th>' +
        '<th scope="col" class="tt-num">DD (R)</th><th scope="col">Validated</th>' +
        '</tr></thead><tbody>' +
        legacy.map(function (row) {
          var exp = Number(row.expectancy_r || 0);
          var validated = row.generalises === true ? 'yes' : (row.generalises === false ? 'no' : '—');
          var vCls = row.generalises === true ? 'tt-up' : (row.generalises === false ? 'tt-down' : 'tt-muted');
          return '<tr>' +
            '<td><span class="tt-symbol">' + esc(row.symbol) + '</span></td>' +
            '<td class="tt-muted">' + esc(row.style || '—') + '</td>' +
            '<td class="tt-num">' + (row.trades !== undefined ? row.trades : '—') + '</td>' +
            '<td class="tt-num ' + signClass(exp) + '">' + num(exp, 4) + '</td>' +
            '<td class="tt-num ' + signClass(row.total_r) + '">' + num(row.total_r, 2) + '</td>' +
            '<td class="tt-num">' + num(row.profit_factor, 3) + '</td>' +
            '<td class="tt-num">' + num(row.max_dd_r, 2) + '</td>' +
            '<td class="' + vCls + '">' + esc(validated) + '</td>' +
            '</tr>';
        }).join('') + '</tbody></table>';
    }

    host.removeAttribute('data-state');
    host.innerHTML = html;
  }

  function loadJobs() {
    var body = $('bt-history');
    apiGet('/api/backtest/jobs', TIMEOUT.normal).then(function (res) {
      var jobs = (res.data && (res.data.jobs || res.data)) || [];
      if (!Array.isArray(jobs)) jobs = [];
      state.jobs = jobs;
      if (!jobs.length) {
        setState(body, 'empty', 'No jobs yet', null);
        return;
      }
      body.removeAttribute('data-state');
      body.innerHTML = jobs.slice(0, 12).map(function (j) {
        var status = String(j.status || '').toUpperCase();
        var cls = status === 'DONE' ? 'tt-chip--buy' :
                  status === 'FAILED' ? 'tt-chip--sell' :
                  status === 'RUNNING' ? 'tt-chip--medium' : 'tt-chip--none';
        return '<tr data-clickable="true" data-job="' + esc(j.job_id || j.id) + '">' +
          '<td class="tt-muted tt-truncate" style="max-width:150px">' + esc(j.label || j.job_id || j.id) + '</td>' +
          '<td><span class="tt-chip ' + cls + '">' + esc(status.toLowerCase()) + '</span></td>' +
          // `progress` is the log list, not a percentage — showing it as a
          // percentage rendered "—%" on every row.
          '<td class="tt-num">' + (status === 'DONE' ? 'done' : num(j.progress_lines, 0) + ' steps') + '</td>' +
          '<td class="tt-muted">' + esc(clockTime(j.started_utc || j.created_utc)) + '</td>' +
          '</tr>';
      }).join('');

      Array.prototype.forEach.call(body.querySelectorAll('tr[data-job]'), function (tr) {
        tr.addEventListener('click', function () {
          var id = tr.getAttribute('data-job');
          state.activeJob = id;
          pollJob(id);
          loadJobResult(id);
          setView('backtest');
        });
      });
    });
  }

  function cancelJob() {
    if (!state.activeJob) return;
    apiPost('/api/backtest/cancel', { job_id: state.activeJob }, TIMEOUT.normal).then(function (res) {
      var data = res.data || {};
      /* A job that has already finished is answered **200** with
         `{"status": "NOOP", "cancelled": false}` — nothing was cancelled. Reading
         only `res.ok` toasted "Cancel requested" for a job that was still
         running and still holding memory, which is the state the user is trying
         to get out of. */
      if (res.ok && data.cancelled) toast('Cancel requested');
      else if (res.ok) toast('That job is no longer running', 'warn');
      else toast(actionFailureMessage('Cancel failed: ', res), 'error');
    });
  }

  /* ── Manual trade ─────────────────────────────────────────────────────── */
  function submitTrade(side) {
    var sym = ($('ticket-symbol').value || '').trim().toUpperCase();
    var vol = Number($('ticket-volume').value || 0);
    if (!sym) { toast('Enter a symbol', 'warn'); return; }
    if (!(vol > 0)) { toast('Enter a volume greater than zero', 'warn'); return; }

    var body = { symbol: sym, side: side, volume: vol };
    var price = Number($('ticket-price').value);
    if (isFinite(price) && price > 0) body.price = price;
    var sl = Number($('ticket-sl').value);
    if (isFinite(sl) && sl > 0) body.sl = sl;
    var tp = Number($('ticket-tp').value);
    if (isFinite(tp) && tp > 0) body.tp = tp;

    apiPost('/api/action/manual_trade', body, TIMEOUT.normal).then(function (res) {
      if (res.ok && !actionRefused(res.data)) toast(side + ' ' + vol + ' ' + sym + ' submitted');
      else toast(actionFailureMessage('Order rejected: ', res), 'error');
    });
  }

  /* ── Copilot ─────────────────────────────────────────────────────────── */
  /* The copilot answers in a trimmed-down markdown: **bold**, "- " bullets and
     newlines. It is escaped *before* those are applied, so a symbol name or a
     broker comment containing markup can never inject HTML. */
  /* The inline pass, kept separate so a bullet's body gets exactly the same
     treatment as any other line. Nearly every bullet the copilot writes puts
     **bold** on its label ("- **Current Bias**: …", "- Balance **1,234.56**"),
     so applying this only to non-bullet lines left the asterisks on screen for
     most of a typical answer. */
  function copilotInline(line) {
    return line
      .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
      // Single-asterisk italics only where the text was not already consumed
      // by the bold pass above.
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>');
  }

  function copilotHtml(text) {
    var safe = esc(String(text === null || text === undefined ? '' : text));
    var lines = safe.split('\n');
    var out = [];
    var inList = false;
    lines.forEach(function (line) {
      if (/^\s*-\s+/.test(line)) {
        if (!inList) { out.push('<ul>'); inList = true; }
        out.push('<li>' + copilotInline(line.replace(/^\s*-\s+/, '')) + '</li>');
        return;
      }
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(copilotInline(line));
    });
    if (inList) out.push('</ul>');
    return out.join('<br>').replace(/<br>(<ul>|<\/ul>)/g, '$1');
  }

  function copilotSay(cls, html) {
    var log = $('copilot-log');
    if (!log) return null;
    var div = document.createElement('div');
    div.className = 'tt-copilot__msg tt-copilot__msg--' + cls;
    div.innerHTML = html;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function toggleCopilot(open) {
    var panel = $('copilot-panel');
    var fab = $('copilot-fab');
    if (!panel) return;
    var show = open === undefined ? panel.hidden : !!open;
    panel.hidden = !show;
    if (fab) fab.setAttribute('aria-expanded', String(show));
    if (show) {
      var input = $('copilot-input');
      if (input) input.focus();
    }
  }

  function askCopilot(query) {
    query = String(query || '').trim();
    if (!query) return;
    copilotSay('user', copilotHtml(query));
    var input = $('copilot-input');
    if (input) input.value = '';

    var pending = copilotSay('pending', 'Thinking…');
    apiPost('/api/copilot/ask', {
      query: query,
      // What the trader is looking at, so a bare "why?" is answerable.
      context: { symbol: state.symbol || null, view: state.view || 'trade' }
    }, TIMEOUT.normal).then(function (res) {
      if (pending) pending.parentNode.removeChild(pending);
      if (!res.ok) {
        copilotSay('error', '<b>Unavailable</b> (HTTP ' + res.status + ') — '
          + esc(String(((res.data || {}).error) || res.error || 'request refused')));
        return;
      }
      var text = (res.data || {}).response;
      copilotSay('bot', copilotHtml(text || 'No response.'));
    });
  }

  function updateCopilotFocus() {
    var chip = $('copilot-focus');
    if (chip) chip.textContent = state.symbol ? state.symbol : 'no symbol';
  }

  /* ── Scheduler ────────────────────────────────────────────────────────── */
  function schedule() {
    function tick(fn, base) {
      return function loop() {
        // Back off 4x while the tab is hidden — a background tab does not need
        // to keep a trading screen at full refresh rate.
        var delay = document.hidden ? base * 4 : base;
        Promise.resolve().then(fn).then(function () {
          setTimeout(loop, delay);
        });
      };
    }
    tick(loadTelemetry, POLL.telemetry)();
    tick(function () { if (state.view === 'trade') loadChart(); }, POLL.chart)();
    tick(function () { if (state.view === 'analytics') loadReliability(); }, POLL.analytics)();
    // The calendar is slow-moving; it only needs refetching every couple of
    // minutes, and the context strip re-renders from whatever is cached.
    tick(function () { loadNews(); }, POLL.news)();
  }

  /* ── Boot ─────────────────────────────────────────────────────────────── */
  function bind() {
    wireDropdowns();

    /* The phone navigation drawer reuses the dropdown controller above for
       open/close, Escape and click-outside. That controller deliberately
       IGNORES clicks inside a panel, because inside a normal dropdown a click is
       usually a link you are about to follow anyway. A navigation drawer wants
       the opposite: choosing a destination should dismiss it. This is the only
       behaviour the drawer adds, and it is scoped to the drawer element so no
       existing dropdown changes.

       The view/pane buttons themselves need no wiring here - they carry the
       same `data-view-btn` / `data-pane-btn` attributes as the inline controls,
       so the listeners added below and `setView`/`setPane`'s aria-selected sync
       already cover them. */
    var drawer = $('nav-drawer');
    if (drawer) {
      drawer.addEventListener('click', function (ev) {
        var t = ev.target;
        if (!t || typeof t.closest !== 'function') return;
        if (t.closest('[data-view-btn], [data-pane-btn], a')) closeDropdown(false);
      });
    }
    var drawerClose = $('nav-drawer-close');
    if (drawerClose) {
      drawerClose.addEventListener('click', function () { closeDropdown(true); });
    }

    Array.prototype.forEach.call(document.querySelectorAll('[data-view-btn]'), function (btn) {
      btn.addEventListener('click', function () { setView(btn.getAttribute('data-view-btn')); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-pane-btn]'), function (btn) {
      btn.addEventListener('click', function () { setPane(btn.getAttribute('data-pane-btn')); });
    });

    var refresh = $('watch-refresh');
    if (refresh) refresh.addEventListener('click', function () { loadTelemetry(); loadSelection(); });

    /* Open positions panel — tab bar (OPEN / HISTORY / PENDING). Each tab
       rewrites thead columns and renders into the same pos-body, with OPEN-only
       controls (total P&L, Flatten all) hidden on the other two tabs. */
    Array.prototype.forEach.call(document.querySelectorAll('[data-pos-tab]'), function (btn) {
      btn.addEventListener('click', function () {
        var name = btn.getAttribute('data-pos-tab');
        setPosTab(name);
        /* Refresh the working orders on every switch to PENDING. The list is
           also warmed once on boot so the badge is right before the first
           switch, but orders come and go, so the tab re-reads on arrival.
           (An `!state.posPending` guard here would never fire: the array is
           initialised to [], which is truthy.) */
        if (name === 'pending') loadPendingForTab();
      });
    });

    var flattenBtn = $('flatten-all');
    if (flattenBtn) {
      flattenBtn.addEventListener('click', function () {
        var open = state.positions || [];
        if (!open.length) { toast('Nothing to flatten', 'warn'); return; }
        if (!window.confirm('Close all ' + open.length + ' open positions?')) return;
        /* Fan out a close per position. Each one already goes through the same
           /api/action/close_position path the per-row Close button uses, so a
           broker refusal on any one position is reported on its own toast and
           the others still proceed. */
        /* Only positions carrying a ticket can be closed, so the summary is
           counted against the attempted set — otherwise a ticket-less row
           would leave the total one short and the toast would never fire. */
        var targets = open.filter(function (p) {
          return p.ticket !== undefined && p.ticket !== null;
        });
        if (!targets.length) { toast('Nothing to flatten', 'warn'); return; }
        var done = 0, failed = 0;
        targets.forEach(function (p) {
          apiPost('/api/action/close_position', { ticket: p.ticket }, TIMEOUT.normal).then(function (res) {
            var data = res.data || {};
            if (res.ok && !actionRefused(data)) done++; else failed++;
            if (done + failed === targets.length) {
              if (failed) toast(failed + ' of ' + targets.length + ' failed to close', 'error');
              else toast('All positions flattened', 'success');
              loadTelemetry();
            }
          });
        });
      });
    }

    var radarFilter = $('radar-filter');
    if (radarFilter) {
      radarFilter.value = state.radarFilter;
      radarFilter.addEventListener('change', function () {
        state.radarFilter = radarFilter.value;
        renderRadar();
      });
    }

    /* Phone quick-action bar (.tt-qbar). Each button delegates to an existing
       control — Refresh and Flatten click their own buttons so all existing
       confirmation, telemetry refresh and broker-refusal handling stay in one
       place. New Order switches the pane via `data-pane`, which already wires
       through `setPane`. Nothing here duplicates business logic. */
    Array.prototype.forEach.call(document.querySelectorAll('.tt-qbar__btn[data-action]'), function (btn) {
      btn.addEventListener('click', function () {
        var action = btn.getAttribute('data-action');
        if (action === 'refresh-watch') {
          var w = $('watch-refresh');
          if (w) w.click();
        } else if (action === 'flatten-all') {
          var f = $('flatten-all');
          if (f) f.click();
        } else if (action === 'quick-trade') {
          var pane = btn.getAttribute('data-pane');
          if (pane && typeof setPane === 'function') setPane(pane);
        }
      });
    });

    // ── Chart source ──────────────────────────────────────────────────────
    var srcNative = $('chart-src-native');
    if (srcNative) srcNative.addEventListener('click', function () { setChartSource('native'); });
    var srcTv = $('chart-src-tv');
    if (srcTv) srcTv.addEventListener('click', function () { setChartSource('tradingview'); });

    // ── News calendar ─────────────────────────────────────────────────────
    var newsRefresh = $('news-refresh');
    if (newsRefresh) newsRefresh.addEventListener('click', loadNews);

    var newsImpact = $('news-impact');
    if (newsImpact) {
      newsImpact.value = state.newsImpact;
      newsImpact.addEventListener('change', function () {
        state.newsImpact = newsImpact.value;
        renderNews();
      });
    }

    var newsCurrency = $('news-currency');
    if (newsCurrency) {
      newsCurrency.addEventListener('change', function () {
        state.newsCurrency = newsCurrency.value;
        renderNews();
      });
    }

    // ── Markets ───────────────────────────────────────────────────────────
    var eqRefresh = $('eq-refresh');
    if (eqRefresh) eqRefresh.addEventListener('click', function () {
      loadEquities();
      loadHeatmap();
    });

    var inRefresh = $('in-refresh');
    if (inRefresh) inRefresh.addEventListener('click', function () {
      // Force a real refetch rather than showing the previous payload: the
      // button exists precisely because the user thinks the panel is stale.
      state.indiaIndices = null;
      state.indiaFii = null;
      state.indiaOptionChain = null;
      loadIndiaIndices();
      loadIndiaFii();
      loadIndiaOptionChain();
    });

    var ocSymbol = $('in-oc-symbol');
    if (ocSymbol) {
      ocSymbol.addEventListener('change', function () {
        state.indiaOptionChain = null;
        loadIndiaOptionChain();
      });
    }

    var tf = $('chart-timeframe');
    if (tf) {
      tf.value = state.timeframe;
      tf.addEventListener('change', function () { state.timeframe = tf.value; loadChart(); });
    }

    // Levels toggle. Hides support/resistance and trade overlays together, and
    // re-draws from the candles already on screen — no refetch needed.
    var levelsBtn = $('chart-levels');
    if (levelsBtn) {
      levelsBtn.setAttribute('aria-pressed', String(state.showLevels));
      levelsBtn.addEventListener('click', function () {
        state.showLevels = !state.showLevels;
        levelsBtn.setAttribute('aria-pressed', String(state.showLevels));
        levelsBtn.textContent = state.showLevels ? 'Levels on' : 'Levels off';
        levelsBtn.classList.toggle('is-off', !state.showLevels);
        refreshChartDecorations();
      });
    }

    var buy = $('ticket-buy');
    if (buy) buy.addEventListener('click', function () { submitTrade('BUY'); });
    var sell = $('ticket-sell');
    if (sell) sell.addEventListener('click', function () { submitTrade('SELL'); });

    var orderType = $('ticket-type');
    if (orderType) {
      orderType.addEventListener('change', syncTicketMode);
    }
    var place = $('ticket-place');
    if (place) place.addEventListener('click', submitPendingOrder);
    syncTicketMode();

    // History filters re-render locally; only a window change refetches.
    ['hist-filter-symbol', 'hist-filter-side', 'hist-filter-source', 'hist-filter-outcome']
      .forEach(function (id) {
        var el = $(id);
        if (!el) return;
        el.addEventListener('input', renderHistory);
        el.addEventListener('change', renderHistory);
      });
    var histDays = $('hist-filter-days');
    if (histDays) histDays.addEventListener('change', loadHistory);
    var histRefresh = $('hist-refresh');
    if (histRefresh) histRefresh.addEventListener('click', loadHistory);

    Array.prototype.forEach.call(document.querySelectorAll('[data-ctx]'), function (btn) {
      btn.addEventListener('click', function () { setContext(btn.getAttribute('data-ctx')); });
    });

    var copFab = $('copilot-fab');
    if (copFab) copFab.addEventListener('click', function () { toggleCopilot(true); });
    var copClose = $('copilot-close');
    if (copClose) copClose.addEventListener('click', function () { toggleCopilot(false); });
    var copClear = $('copilot-clear');
    if (copClear) copClear.addEventListener('click', function () {
      var log = $('copilot-log');
      if (log) log.innerHTML = '';
    });
    var copForm = $('copilot-form');
    if (copForm) {
      copForm.addEventListener('submit', function (ev) {
        ev.preventDefault();
        askCopilot($('copilot-input') ? $('copilot-input').value : '');
      });
    }
    updateCopilotFocus();

    var form = $('bt-form');
    if (form) form.addEventListener('submit', runBacktest);
    var cancel = $('bt-cancel');
    if (cancel) cancel.addEventListener('click', cancelJob);

    var symInput = $('ticket-symbol');
    if (symInput) {
      symInput.addEventListener('change', function () {
        var v = symInput.value.trim().toUpperCase();
        if (v) selectSymbol(v);
      });
    }

    // Keyboard: 1-6 switch views, [ ] cycle panes on phone widths.
    document.addEventListener('keydown', function (ev) {
      var panel = $('copilot-panel');
      if (ev.key === 'Escape' && panel && !panel.hidden) { toggleCopilot(false); return; }
      if (ev.target && /INPUT|TEXTAREA|SELECT/.test(ev.target.tagName)) return;
      if (ev.key === '1') setView('trade');
      if (ev.key === '2') setView('news');
      if (ev.key === '3') setView('analyst');
      if (ev.key === '4') setView('markets');
      if (ev.key === '5') setView('analytics');
      if (ev.key === '6') setView('backtest');
    });

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { loadTelemetry(); if (state.view === 'trade') loadChart(); }
    });
  }

  /* The phone bottom tab bar is meant to be viewport-fixed (iOS convention),
     but `.tt-rail` carries backdrop-filter, which makes it the containing block
     for any position:fixed descendant. A fixed child of the rail therefore
     resolves against the rail (top of the screen) instead of the viewport, so
     the bar parks under the nav bar rather than at the bottom. To get a true
     viewport-pinned bar we lift `.tt-tabs` out of the rail onto <body> on phone
     widths (body has no containing-block-creating property), and return it to
     the rail on tablet and desktop. On tablet the same element is the sidebar's
     nav list, so it must stay inside the rail — 600-1024px is the one case
     where the tabs are visible AND must not be reparented. */
  function syncTabBarHost() {
    var tabs = document.querySelector('.tt-tabs');
    var rail = document.querySelector('.tt-rail');
    var app = document.querySelector('.tt-app');
    if (!tabs || !rail || !app) return;
    var phone = window.matchMedia('(max-width: 599px)').matches;
    if (phone) {
      if (tabs.parentElement !== app) app.appendChild(tabs);
    } else if (tabs.parentElement !== rail) {
      var drop = rail.querySelector('.tt-dropdown');
      if (drop) rail.insertBefore(tabs, drop);
      else rail.appendChild(tabs);
    }
  }

  /* ── Mobile card layout for data tables ────────────────────────────────
     A 10-column trade journal is unreadable on a phone: the columns cannot
     fit, the row scrolls sideways, and a bare value like "0.8" means nothing
     without its column header. On narrow screens CSS restyles each <tr> as a
     CARD instead — one record per block, primary field first.

     CSS cannot read the <th> text, so the header has to travel with the cell.
     This copies each column's <th> label onto its <td> as `data-label`, which
     the card CSS renders with `content: attr(data-label)`. Purely
     presentational: no data is read, written, or reordered, and no business
     logic is touched. `data-primary` marks the first cell so the card can
     promote it to a header.

     Rows are rendered (and re-rendered) by many functions, so rather than
     instrument every renderer we observe each table and re-stamp whenever its
     rows change. Observing `childList` only means the setAttribute calls below
     cannot retrigger the observer. */
  function labelTable(table) {
    if (!table) return;
    var headRow = table.querySelector('thead tr');
    if (!headRow) return;
    var heads = headRow.querySelectorAll('th');
    var labels = [];
    for (var i = 0; i < heads.length; i++) {
      labels.push(String(heads[i].textContent || '').trim());
    }
    if (!labels.length) return;

    var rows = table.querySelectorAll('tbody tr');
    for (var r = 0; r < rows.length; r++) {
      var cells = rows[r].querySelectorAll('td');
      for (var c = 0; c < cells.length; c++) {
        var td = cells[c];
        var lbl = labels[c];
        if (lbl && td.getAttribute('data-label') !== lbl) {
          td.setAttribute('data-label', lbl);
        }
        if (c === 0 && !td.hasAttribute('data-primary')) {
          td.setAttribute('data-primary', 'true');
        }
      }
    }
  }

  function enhanceTables() {
    var tables = document.querySelectorAll('table');
    Array.prototype.forEach.call(tables, function (t) {
      labelTable(t);
      if (t.getAttribute('data-hm-cards')) return;
      t.setAttribute('data-hm-cards', 'true');
      if (typeof MutationObserver === 'function') {
        var mo = new MutationObserver(function () { labelTable(t); });
        mo.observe(t, { childList: true, subtree: true });
      }
    });
  }

  function boot() {
    bind();
    // Reparent the tab bar for the current width and keep it correct across
    // breakpoint changes (orientation flip, desktop↔phone resize).
    syncTabBarHost();
    if (window.matchMedia) {
      var mq = window.matchMedia('(max-width: 599px)');
      var onMq = function () { syncTabBarHost(); };
      if (mq.addEventListener) mq.addEventListener('change', onMq);
      else if (mq.addListener) mq.addListener(onMq);
    }
    // Stamp column labels onto every table cell so the mobile card layout can
    // show what each value means. Tables rendered later are covered by the
    // per-table MutationObserver set up here.
    enhanceTables();

    var clock = $('clock');
    var tickClock = function () {
      if (clock) clock.textContent = clockTime(new Date());
    };
    tickClock();
    setInterval(tickClock, 1000);

    // Countdowns are recomputed locally every second, so this timer never
    // touches the network.
    setInterval(tickNews, 1000);

    loadTelemetry().then(function () {
      loadSelection();
      loadChart();
    });
    // Closed-trade history feeds the chart's exit markers, so it is loaded for
    // the trade view too, not only when the analytics tab is opened.
    loadHistory();

    // The positions panel is tabbed (OPEN / HISTORY / PENDING). Select OPEN on
    // boot so the thead, the active pill and the OPEN-only controls are in a
    // known state, and warm the pending list so the first switch to PENDING
    // shows a count immediately instead of an empty state that then fills in.
    setPosTab('open');
    loadPendingForTab();

    copilotSay('bot', 'I can read your open book, the closed-trade journal and the '
      + "engine's own decision record. Try <b>“what positions do I have open?”</b> "
      + 'or <b>“how is my ' + esc(state.symbol || 'symbol') + ' doing?”</b>');
    schedule();

    // Phase D: Wire up AI Explanation Modal & Emergency Controls
    document.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-ai-ticket]');
      if (btn) {
        var ticket = btn.getAttribute('data-ai-ticket');
        if (ticket) showAiExplanationModal(ticket);
        return;
      }
      if (ev.target.id === 'ai-modal-close' || ev.target.id === 'ai-explanation-modal') {
        closeAiExplanationModal();
        return;
      }
      if (ev.target.id === 'btn-emergency-stop') {
        if (confirm('🚨 ACTIVATE EMERGENCY STOP?\nThis will trip the circuit breaker and halt all automated trading immediately.')) {
          apiPost('/api/action/emergency_stop', { reason: 'Operator Emergency Stop Button', close_positions: false }).then(function (res) {
            alert('Emergency stop activated! Circuit breaker is TRIPPED.');
            renderRisk();
          });
        }
        return;
      }
      if (ev.target.id === 'btn-resume-trading') {
        if (confirm('Resume automated trading? This will reset the circuit breaker.')) {
          apiPost('/api/action/resume_trading', {}).then(function (res) {
            alert('Trading resumed. Circuit breaker is RESET.');
            renderRisk();
          });
        }
        return;
      }
    });

    if (window.HMUI && typeof window.HMUI.announce === 'function') {
      window.HMUI.announce('Trading terminal ready');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
