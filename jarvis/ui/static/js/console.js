/* ===========================================================================
   HM Algo 2.0 Console — controller
   ===========================================================================

   DESIGN NOTES
   ------------
   1. NO innerHTML WITH SERVER DATA. Every value that originates from the API is
      written with textContent, or through esc() when it has to be interpolated
      into a template string. Symbol names, strategies, gate reasons and error
      strings all flow from the broker and the engine; treating any of them as
      trusted markup would be an XSS hole in a page that can place trades.

   2. TIERED POLLING. Not everything deserves the same cadence. Telemetry drives
      the account strip and positions, so it polls fast; the chart is heavier;
      regime and analytics change slowly. Polling everything at the fastest rate
      would burn CPU for no visible benefit and, on a phone, battery.

   3. EVERY ADVERTISED SHORTCUT IS BOUND. The legacy terminal listed a dozen
      shortcuts in its help text and bound almost none of them. The palette is
      generated from the same table the key handler reads, so the two cannot
      drift apart.

   4. THE VIEW/PANEL STATE LIVES ON <body>. CSS keys off body[data-view] and
      body[data-panel], so what the user sees and what the buttons claim can
      never disagree.
   =========================================================================== */
(function () {
  'use strict';

  /* ── constants ────────────────────────────────────────────────────────── */
  var POLL = { telemetry: 2000, chart: 6000, regime: 30000, analytics: 15000, jobs: 2000 };
  var FALLBACK_SYMBOLS = ['XAUUSD', 'BTCUSD', 'ETHUSD', 'SOLUSD', 'ETHBTC', 'EURUSD', 'GBPUSD', 'USDJPY', 'WTI', 'NAS100'];
  var VIEWS = ['trade', 'backtest', 'analytics'];
  var PANELS = ['watchlist', 'chart', 'ticket'];
  var TIER_CLASS = { HIGH: 'cx-chip--bull', MEDIUM: 'cx-chip', LOW: 'cx-chip--warn', NONE: 'cx-chip--muted' };

  var $ = function (id) { return document.getElementById(id); };

  /* ── state ────────────────────────────────────────────────────────────── */
  var state = {
    symbols: FALLBACK_SYMBOLS.slice(),
    selected: 'XAUUSD',
    timeframe: 'H1',
    view: 'trade',
    panel: 'chart',
    chart: null,
    series: null,
    lastBarTime: 0,
    telemetry: null,
    selection: null,
    jobs: [],
    activeJob: null,
    reliability: null,
    history: [],
    gPressed: false,
    timers: {}
  };

  /* ── utils ────────────────────────────────────────────────────────────── */
  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function num(v, dp) {
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return n.toLocaleString(undefined, {
      minimumFractionDigits: dp == null ? 2 : dp,
      maximumFractionDigits: dp == null ? 2 : dp
    });
  }

  function money(v, currency) {
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return (currency ? currency + ' ' : '') + num(n, 2);
  }

  function signed(v, dp) {
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return (n > 0 ? '+' : '') + num(n, dp == null ? 2 : dp);
  }

  function pnlClass(v) {
    var n = Number(v);
    if (!isFinite(n) || n === 0) return 'cx-flat';
    return n > 0 ? 'cx-pos' : 'cx-neg';
  }

  function toast(msg, type) {
    if (window.HMUI && typeof window.HMUI.toast === 'function') {
      window.HMUI.toast(msg, type ? { type: type } : undefined);
    } else if (window.console) {
      window.console.log('[console]', msg);
    }
  }

  function setText(id, value) {
    var el = $(id);
    if (el) el.textContent = value == null ? '—' : String(value);
  }

  /* ── api ──────────────────────────────────────────────────────────────── */
  function authHeaders() {
    var h = { 'Accept': 'application/json' };
    try {
      var t = window.getAuthToken ? window.getAuthToken() : '';
      if (t) h['Authorization'] = 'Bearer ' + t;
    } catch (e) { /* token is optional for public endpoints */ }
    return h;
  }

  /* Normalise a transport failure BEFORE it reaches a caller.
     `fetch` rejects with the bare string "Failed to fetch" for every
     transport-level failure - connection refused, DNS failure, reset
     mid-flight. It is the browser's internal phrasing and it names no cause.
     Ten catch sites in this file render `err.message` straight to the
     operator, so normalising in the two wrappers below fixes the wording
     everywhere without editing a single call site. */
  function describeTransport(err) {
    var raw = String((err && err.message) || err || '');
    if (err && (err.name === 'AbortError' || /abort/i.test(raw))) {
      return 'Request timed out';
    }
    if (/failed to fetch|networkerror|load failed|err_connection/i.test(raw)) {
      return 'Backend unreachable — is the server running?';
    }
    return raw || 'Request failed';
  }

  function getJSON(url) {
    return fetch(url, { headers: authHeaders(), credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .catch(function (err) {
        // An HTTP status already reads correctly; re-wrapping it is a no-op
        // because describeTransport passes unrecognised messages through.
        throw new Error(describeTransport(err));
      });
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      credentials: 'same-origin',
      body: JSON.stringify(payload || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
        return body;
      });
    }).catch(function (err) {
      throw new Error(describeTransport(err));
    });
  }

  /* ── view / panel ─────────────────────────────────────────────────────── */
  function setView(name) {
    if (VIEWS.indexOf(name) === -1) return;
    state.view = name;
    document.body.setAttribute('data-view', name);

    VIEWS.forEach(function (v) {
      var panel = document.querySelector('[data-view-panel="' + v + '"]');
      if (panel) panel.hidden = (v !== name);
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-view-btn]'), function (btn) {
      var on = btn.getAttribute('data-view-btn') === name;
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
      btn.tabIndex = on ? 0 : -1;
    });

    if (name === 'trade' && state.chart) setTimeout(function () { state.chart.timeScale().fitContent(); }, 60);
    if (name === 'backtest') loadJobs();
    if (name === 'analytics') loadAnalytics();
  }

  function setPanel(name) {
    if (PANELS.indexOf(name) === -1) return;
    state.panel = name;
    document.body.setAttribute('data-panel', name);
    Array.prototype.forEach.call(document.querySelectorAll('[data-panel-btn]'), function (btn) {
      var on = btn.getAttribute('data-panel-btn') === name;
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (name === 'chart' && state.chart) setTimeout(function () { state.chart.timeScale().fitContent(); }, 60);
  }

  function cyclePanel() {
    var i = PANELS.indexOf(state.panel);
    setPanel(PANELS[(i + 1) % PANELS.length]);
  }

  /* ── chart ────────────────────────────────────────────────────────────── */
  function initChart() {
    var host = $('cx-chart');
    if (!host) return;
    if (typeof window.LightweightCharts === 'undefined') {
      var fb = $('cx-chart-fallback');
      if (fb) { fb.hidden = false; fb.textContent = 'Chart library unavailable — prices still update in the watchlist.'; }
      return;
    }

    state.chart = window.LightweightCharts.createChart(host, {
      layout: { background: { color: 'transparent' }, textColor: '#b6c2d4', fontSize: 11 },
      grid: {
        vertLines: { color: 'rgba(148,163,184,0.08)' },
        horzLines: { color: 'rgba(148,163,184,0.08)' }
      },
      rightPriceScale: { borderColor: 'rgba(148,163,184,0.22)' },
      timeScale: { borderColor: 'rgba(148,163,184,0.22)', timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      handleScale: { axisPressedMouseMove: true }
    });

    // v4 API: addCandlestickSeries. v5 renamed this to addSeries — the vendored
    // build is v4, so the v4 call is the correct one here.
    state.series = state.chart.addCandlestickSeries({
      upColor: '#00f59b', downColor: '#ff3b5c',
      borderUpColor: '#00f59b', borderDownColor: '#ff3b5c',
      wickUpColor: '#00f59b', wickDownColor: '#ff3b5c'
    });

    var ro = ('ResizeObserver' in window) ? new ResizeObserver(function () {
      if (state.chart) state.chart.applyOptions({ width: host.clientWidth, height: host.clientHeight });
    }) : null;
    if (ro) ro.observe(host);
    state.chart.applyOptions({ width: host.clientWidth, height: host.clientHeight });
  }

  function loadChart() {
    if (!state.series) return;
    var sym = state.selected;
    var tf = state.timeframe;
    getJSON('/api/candles?symbol=' + encodeURIComponent(sym) + '&tf=' + encodeURIComponent(tf))
      .then(function (data) {
        var raw = (data && data.candles) || [];
        if (!raw.length) {
          var fb = $('cx-chart-fallback');
          if (fb) { fb.hidden = false; fb.textContent = 'No candles returned for ' + sym + ' ' + tf + '.'; }
          return;
        }
        var fb = $('cx-chart-fallback');
        if (fb) fb.hidden = true;

        // Lightweight-charts requires strictly ascending, UNIQUE times. The
        // broker feed can repeat a bar while it is still forming, which throws.
        var out = [];
        var seen = Object.create(null);
        for (var i = 0; i < raw.length; i++) {
          var c = raw[i];
          var t = Math.floor(Number(c.time));
          if (!isFinite(t) || seen[t]) continue;
          seen[t] = 1;
          out.push({
            time: t,
            open: Number(c.open), high: Number(c.high),
            low: Number(c.low), close: Number(c.close)
          });
        }
        out.sort(function (a, b) { return a.time - b.time; });
        if (!out.length) return;
        state.series.setData(out);
        state.lastBarTime = out[out.length - 1].time;

        var last = out[out.length - 1];
        var first = out[0];
        var chg = first.open ? ((last.close - first.open) / first.open) * 100 : 0;
        setText('cx-chart-symbol', sym);
        setText('cx-chart-meta', tf + ' · ' + num(last.close) + ' · ' + signed(chg, 2) + '%');
        state.chart.timeScale().fitContent();
      })
      .catch(function (err) {
        var fb = $('cx-chart-fallback');
        if (fb) { fb.hidden = false; fb.textContent = 'Chart unavailable (' + err.message + ').'; }
      });
  }

  /* ── telemetry ────────────────────────────────────────────────────────── */
  function loadTelemetry() {
    return getJSON('/api/telemetry_state?symbol=' + encodeURIComponent(state.selected))
      .then(function (snap) {
        state.telemetry = snap;
        markConnection(true);
        renderAccount(snap);
        renderWatchlist(snap);
        renderPositions(snap);
        renderModeChips(snap);
      })
      .catch(function () { markConnection(false); });
  }

  function markConnection(ok) {
    var dot = $('cx-conn');
    if (!dot) return;
    dot.className = 'cx-dot ' + (ok ? 'is-live' : 'is-down');
    dot.setAttribute('aria-label', ok ? 'Connected' : 'Connection lost');
  }

  function renderAccount(snap) {
    var acc = (snap && snap.account) || {};
    var cur = acc.currency || '';
    setText('cx-acc-equity', acc.equity != null ? money(acc.equity, cur) : '—');
    setText('cx-acc-margin', acc.free_margin != null ? money(acc.free_margin, cur) : '—');

    var profitEl = $('cx-acc-profit');
    if (profitEl) {
      var p = Number(acc.profit);
      profitEl.textContent = isFinite(p) ? signed(p) + (cur ? ' ' + cur : '') : '—';
      profitEl.className = 'cx-mono ' + pnlClass(p);
    }

    // Risk at work = open position P&L-at-risk proxy: margin in use relative to
    // equity. A blunt measure, but an honest one, and it is labelled as such.
    var riskEl = $('cx-acc-risk');
    if (riskEl) {
      var eq = Number(acc.equity), mg = Number(acc.margin);
      riskEl.textContent = (isFinite(eq) && eq > 0 && isFinite(mg)) ? num((mg / eq) * 100, 1) + '%' : '—';
    }
    var upd = $('cx-an-updated');
    if (upd) upd.textContent = snap && snap.timestamp ? 'updated ' + snap.timestamp : '';
  }

  function renderModeChips(snap) {
    setText('cx-exec-mode', (snap && snap.execution_mode) || '—');
    var styleEl = $('cx-trade-style');
    if (styleEl) {
      styleEl.textContent = (snap && snap.trade_style) || '—';
      styleEl.className = 'cx-chip cx-chip--muted';
    }
  }

  function renderWatchlist(snap) {
    var body = $('cx-watchlist-body');
    if (!body) return;

    var radar = (snap && snap.radar_opportunities) || [];
    var statuses = (snap && snap.market_statuses) || {};
    var bySymbol = Object.create(null);
    radar.forEach(function (r) { if (r && r.symbol) bySymbol[r.symbol] = r; });

    var list = state.symbols.slice();
    Object.keys(bySymbol).forEach(function (s) { if (list.indexOf(s) === -1) list.push(s); });

    setText('cx-watchlist-count', list.length + ' symbols');
    body.textContent = '';

    // An empty list must still replace the template's "Loading…" row. Leaving
    // the placeholder in place made an empty watchlist indistinguishable from
    // a request that never came back - the panel read as broken rather than
    // empty, which is exactly the "no data" report this guards against.
    if (!list.length) {
      var emptyRow = document.createElement('tr');
      var emptyCell = document.createElement('td');
      emptyCell.colSpan = 4;
      emptyCell.className = 'cx-empty';
      emptyCell.textContent = 'No symbols to show.';
      emptyRow.appendChild(emptyCell);
      body.appendChild(emptyRow);
      return;
    }

    list.slice(0, 40).forEach(function (sym) {
      var r = bySymbol[sym];
      var st = statuses[sym] || {};
      var tr = document.createElement('tr');
      if (sym === state.selected) tr.className = 'is-selected';
      tr.setAttribute('data-symbol', sym);

      var tdSym = document.createElement('td');
      tdSym.textContent = sym;

      var tdBid = document.createElement('td');
      tdBid.className = 'cx-num';
      tdBid.textContent = r && r.current_price != null ? num(r.current_price) : '—';

      var tdChg = document.createElement('td');
      tdChg.className = 'cx-num';
      var chg = r ? Number(r.ev) : NaN;
      tdChg.textContent = isFinite(chg) ? signed(chg, 2) : '—';

      var tdStatus = document.createElement('td');
      var chip = document.createElement('span');
      var label = (r && (r.action || r.status_label)) || (st.is_open === false ? 'CLOSED' : 'NO SETUP');
      chip.className = 'cx-chip ' + (/READY/.test(label) ? 'cx-chip--bull'
        : /INVALID|NO TRADE/.test(label) ? 'cx-chip--warn' : 'cx-chip--muted');
      chip.textContent = label;
      tdStatus.appendChild(chip);

      tr.appendChild(tdSym); tr.appendChild(tdBid); tr.appendChild(tdChg); tr.appendChild(tdStatus);
      body.appendChild(tr);
    });
  }

  function renderPositions(snap) {
    var body = $('cx-pos-body');
    if (!body) return;
    var positions = (snap && snap.positions) || [];
    setText('cx-pos-count', positions.length);
    body.textContent = '';

    if (!positions.length) {
      var tr = document.createElement('tr');
      var td = document.createElement('td');
      td.colSpan = 7; td.className = 'cx-empty'; td.textContent = 'No open positions.';
      tr.appendChild(td); body.appendChild(tr);
      return;
    }

    positions.forEach(function (p) {
      var tr = document.createElement('tr');
      function cell(text, cls) {
        var td = document.createElement('td');
        if (cls) td.className = cls;
        td.textContent = text;
        tr.appendChild(td);
        return td;
      }
      cell(p.symbol || '—');
      var side = String(p.type || '').toUpperCase();
      cell(side === 'BUY' ? 'Buy' : side === 'SELL' ? 'Sell' : (side || '—'));
      cell(num(p.volume, 2), 'cx-num');
      cell(num(p.open_price), 'cx-num');
      cell(num(p.current_price), 'cx-num');
      cell(signed(p.profit), 'cx-num ' + pnlClass(p.profit));

      var tdAct = document.createElement('td');
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cx-btn cx-btn--sm cx-btn--danger';
      btn.textContent = 'Close';
      btn.setAttribute('data-close-ticket', String(p.ticket));
      tdAct.appendChild(btn);
      tr.appendChild(tdAct);

      body.appendChild(tr);
    });
  }

  /* ── auto-selection ───────────────────────────────────────────────────── */
  function loadSelection(force) {
    var body = $('cx-selection-body');
    if (body && force) body.textContent = '';
    var url = '/api/intelligence/auto-selection' + (force ? '?refresh=1' : '');
    return getJSON(url)
      .then(function (data) {
        state.selection = data;
        renderSelection(data);
      })
      .catch(function (err) {
        if (!body) return;
        body.textContent = '';
        var p = document.createElement('p');
        p.className = 'cx-empty';
        p.textContent = 'Auto-selection unavailable — ' + err.message + '. Sign in if prompted.';
        body.appendChild(p);
      });
  }

  function renderSelection(data) {
    var body = $('cx-selection-body');
    if (!body) return;

    var decisions = (data && data.decisions) || [];
    var tradeable = (data && data.tradeable) || [];
    setText('cx-selection-count', decisions.length);
    body.textContent = '';

    if (data && data.status && data.status !== 'OK') {
      var p = document.createElement('p');
      p.className = 'cx-empty';
      p.textContent = data.error || 'Auto-selection is unavailable.';
      body.appendChild(p);
      return;
    }
    if (!decisions.length) {
      var p2 = document.createElement('p');
      p2.className = 'cx-empty';
      p2.textContent = 'No symbol produced a directional setup on this scan.';
      body.appendChild(p2);
      return;
    }

    var bestSymbol = tradeable.length ? tradeable[0].symbol : null;

    decisions.forEach(function (d) {
      var card = document.createElement('div');
      card.className = 'cx-sel' + (d.symbol === bestSymbol ? ' is-best' : '');
      card.setAttribute('role', 'button');
      card.tabIndex = 0;
      card.setAttribute('data-select-symbol', d.symbol);

      var top = document.createElement('div');
      top.className = 'cx-sel__top';

      var sym = document.createElement('span');
      sym.className = 'cx-sel__sym';
      sym.textContent = d.symbol;

      var dir = document.createElement('span');
      dir.className = 'cx-chip ' + (d.direction === 'BUY' ? 'cx-chip--bull'
        : d.direction === 'SELL' ? 'cx-chip--bear' : 'cx-chip--muted');
      dir.textContent = d.direction || 'NONE';

      var tier = document.createElement('span');
      tier.className = TIER_CLASS[d.confidence_tier] || 'cx-chip--muted';
      tier.textContent = d.confidence_tier || 'NONE';

      var score = document.createElement('span');
      score.className = 'cx-sel__score';
      score.textContent = num(d.consensus_score, 1) + ' / 100';

      top.appendChild(sym); top.appendChild(dir); top.appendChild(tier); top.appendChild(score);
      card.appendChild(top);

      var bar = document.createElement('div');
      bar.className = 'cx-sel__bar';
      var fill = document.createElement('span');
      var pct = Math.max(0, Math.min(100, Number(d.consensus_score) || 0));
      fill.style.width = pct + '%';
      fill.style.background = d.direction === 'SELL' ? 'var(--hm-bear)' : 'var(--hm-accent)';
      bar.appendChild(fill);
      card.appendChild(bar);

      var modes = document.createElement('div');
      modes.className = 'cx-sel__modes';
      (d.votes || []).forEach(function (v) {
        var chip = document.createElement('span');
        var agree = (d.supporting_styles || []).indexOf(v.trade_style) !== -1;
        chip.className = 'cx-chip ' + (v.direction === 'NONE' ? 'cx-chip--muted'
          : agree ? 'cx-chip--bull' : 'cx-chip--bear');
        chip.textContent = v.trade_style.replace('DAY_TRADING', 'DAY') + ' ' + v.direction +
          ' · ' + num(v.utility_score, 2);
        chip.title = 'trust ' + num(v.reliability, 2) + ' · grade ' + (v.setup_grade || '');
        modes.appendChild(chip);
      });
      card.appendChild(modes);

      var why = document.createElement('p');
      why.className = 'cx-sel__why';
      why.textContent = d.rationale || '';
      card.appendChild(why);

      body.appendChild(card);
    });
  }

  /* ── reasoning ────────────────────────────────────────────────────────── */
  function loadReasoning() {
    var body = $('cx-why-body');
    if (!body) return;
    var sym = state.selected;
    var snap = state.telemetry;
    var dec = (snap && snap.latest_decisions && snap.latest_decisions[sym]) || null;

    body.textContent = '';
    if (!dec) {
      var p = document.createElement('p');
      p.className = 'cx-empty';
      p.textContent = 'No decision recorded yet for ' + sym + '.';
      body.appendChild(p);
      return;
    }

    var wrap = document.createElement('div');
    wrap.className = 'cx-reason';

    function row(key, value, cls) {
      var r = document.createElement('div');
      r.className = 'cx-reason__row';
      var k = document.createElement('span');
      k.className = 'cx-reason__key';
      k.textContent = key;
      var v = document.createElement('span');
      v.className = 'cx-reason__val ' + (cls || '');
      v.textContent = value == null ? '—' : String(value);
      r.appendChild(k); r.appendChild(v);
      wrap.appendChild(r);
    }

    row('Decision', dec.decision);
    row('Bias', dec.bias);
    row('Strategy', dec.strategy);
    row('Confidence', dec.model_confidence != null ? num(dec.model_confidence * 100, 1) + '%' : '—');
    row('Expected value', dec.expected_value != null ? num(dec.expected_value, 2) + 'R' : '—');
    row('Risk / reward', dec.risk_reward_ratio != null ? num(dec.risk_reward_ratio, 2) : '—');
    row('Entry', num(dec.entry_price));
    row('Stop', num(dec.stop_loss));
    row('Target', num(dec.take_profit));
    row('Adversarial penalty', dec.adversarial_penalty != null ? num(dec.adversarial_penalty, 1) : '—');

    function block(title, items) {
      if (!items || !items.length) return;
      var b = document.createElement('div');
      b.className = 'cx-reason__block';
      var h = document.createElement('h3');
      h.textContent = title;
      var ul = document.createElement('ul');
      ul.className = 'cx-reason__list';
      items.slice(0, 8).forEach(function (it) {
        var li = document.createElement('li');
        li.textContent = String(it);
        ul.appendChild(li);
      });
      b.appendChild(h); b.appendChild(ul);
      wrap.appendChild(b);
    }

    var gate = dec.quality_gate || {};
    block('Failing gates', gate.failing_reasons);
    block('Waiting on', dec.waiting_reasons);
    block('Rejected because', dec.rejection_reasons);
    block('Risk factors', dec.risk_factors);
    block('Bull case', dec.bull_case);
    block('Bear case', dec.bear_case);

    body.appendChild(wrap);
  }

  /* ── analytics ────────────────────────────────────────────────────────── */
  function loadAnalytics() {
    loadReliability();
    loadHistory();
  }

  function loadReliability() {
    var host = $('cx-an-reliability');
    return getJSON('/api/intelligence/reliability')
      .then(function (data) {
        state.reliability = data;
        if (!host) return;
        host.textContent = '';
        var table = document.createElement('table');
        table.className = 'cx-table';
        table.innerHTML =
          '<caption class="cx-visually-hidden">Measured reliability per trading mode</caption>' +
          '<thead><tr><th scope="col">Mode</th><th scope="col" class="cx-num">Weight</th>' +
          '<th scope="col" class="cx-num">Trades</th><th scope="col" class="cx-num">Expectancy</th>' +
          '<th scope="col" class="cx-num">Profit factor</th></tr></thead>';
        var tb = document.createElement('tbody');
        (data.styles || []).forEach(function (s) {
          var tr = document.createElement('tr');
          function cell(text, cls) {
            var td = document.createElement('td');
            if (cls) td.className = cls;
            td.textContent = text;
            tr.appendChild(td);
          }
          cell(s.style);
          cell(num(s.weight, 2), 'cx-num');
          cell(String(s.trades), 'cx-num');
          cell(signed(s.expectancy_r, 4), 'cx-num ' + pnlClass(s.expectancy_r));
          cell(num(s.profit_factor, 2), 'cx-num ' + pnlClass(Number(s.profit_factor) - 1));
          tb.appendChild(tr);
        });
        table.appendChild(tb);
        host.appendChild(table);
      })
      .catch(function (err) {
        if (!host) return;
        host.textContent = '';
        var p = document.createElement('p');
        p.className = 'cx-empty';
        p.textContent = 'Reliability unavailable (' + err.message + ').';
        host.appendChild(p);
      });
  }

  function loadHistory() {
    var body = $('cx-an-hist-body');
    return getJSON('/api/history?limit=100')
      .then(function (data) {
        // The route returns a bare array. Reading `data.trades` off it made
        // every load render empty, so accept both shapes.
        var rows = Array.isArray(data) ? data : ((data && (data.trades || data.history)) || []);
        state.history = rows;
        setText('cx-an-hist-count', rows.length + ' trades');
        if (!body) return;
        body.textContent = '';
        if (!rows.length) {
          var tr = document.createElement('tr');
          var td = document.createElement('td');
          td.colSpan = 8; td.className = 'cx-empty'; td.textContent = 'No history yet.';
          tr.appendChild(td); body.appendChild(tr);
          return;
        }
        rows.slice(0, 100).forEach(function (t) {
          var tr = document.createElement('tr');
          function cell(text, cls) {
            var td = document.createElement('td');
            if (cls) td.className = cls;
            td.textContent = text;
            tr.appendChild(td);
          }
          cell(t.symbol || '—');
          cell(t.type || t.side || '—');
          cell(num(t.lots != null ? t.lots : t.volume, 2), 'cx-num');
          cell(signed(t.pnl), 'cx-num ' + pnlClass(t.pnl));
          cell(t.regime || '—');
          cell(t.strategy || '—');
          cell(t.model_confidence != null ? num(t.model_confidence * 100, 0) + '%' : '—', 'cx-num');
          cell(t.close_time || t.open_time || '—');
          body.appendChild(tr);
        });
      })
      .catch(function () {
        if (!body) return;
        body.textContent = '';
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 8; td.className = 'cx-empty'; td.textContent = 'History unavailable.';
        tr.appendChild(td); body.appendChild(tr);
      });
  }

  /* ── backtest ─────────────────────────────────────────────────────────── */
  function selectedModes() {
    var sel = $('cx-bt-modes');
    if (!sel) return ['SWING', 'DAY_TRADING', 'SCALP'];
    var out = [];
    Array.prototype.forEach.call(sel.options, function (o) { if (o.selected) out.push(o.value); });
    return out.length ? out : ['SWING', 'DAY_TRADING', 'SCALP'];
  }

  function numberOr(id, fallback) {
    var el = $(id);
    var v = el ? Number(el.value) : NaN;
    return isFinite(v) ? v : fallback;
  }

  function buildSpec() {
    var symRaw = ($('cx-bt-symbols') || {}).value || '';
    var symbols = symRaw.split(',').map(function (s) { return s.trim().toUpperCase(); })
      .filter(function (s) { return s.length; });

    // The full grid is 5 x 4 x 4 x 4 x 6 = 1920 geometries per mode, far more
    // than an interactive budget allows. Unchecked dimensions collapse to a
    // single value so the run stays inside its time box.
    var space = {};
    if (!$('cx-bt-g-tp') || !$('cx-bt-g-tp').checked) space.tp_r = [2.5];
    if (!$('cx-bt-g-be') || !$('cx-bt-g-be').checked) space.be_trigger_r = [null];
    if (!$('cx-bt-g-pc') || !$('cx-bt-g-pc').checked) space.fast_cash_r = [null];
    if (!$('cx-bt-g-tr') || !$('cx-bt-g-tr').checked) space.trail_atr = [null];
    if (!$('cx-bt-g-q') || !$('cx-bt-g-q').checked) space.min_score_quantiles = [0.0, 0.97];

    return {
      symbols: symbols,
      modes: selectedModes(),
      objective: ($('cx-bt-objective') || {}).value || 'expectancy_r',
      min_trades: numberOr('cx-bt-mintrades', 30),
      max_dd_r: numberOr('cx-bt-maxdd', 40),
      walk_forward_split: numberOr('cx-bt-split', 0.7),
      passes: numberOr('cx-bt-passes', 2),
      max_evaluations: numberOr('cx-bt-evals', 60),
      label: 'console',
      space: space
    };
  }

  function submitBacktest(ev) {
    if (ev) ev.preventDefault();
    var btn = $('cx-bt-run');
    if (btn) btn.disabled = true;
    setJobStatus('submitting');
    setLog('Submitting backtest…');

    postJSON('/api/backtest/run', { spec: buildSpec() })
      .then(function (res) {
        state.activeJob = res.job_id;
        toast('Backtest queued: ' + res.job_id, 'success');
        setJobStatus('queued');
        pollJob();
        loadJobs();
      })
      .catch(function (err) {
        toast('Could not start backtest: ' + err.message, 'error');
        setJobStatus('error');
        setLog('Failed to submit: ' + err.message);
      })
      .finally(function () { if (btn) btn.disabled = false; });
  }

  function setJobStatus(text) {
    var el = $('cx-bt-status');
    if (el) el.textContent = text;
    var bar = $('cx-bt-progress-bar');
    var prog = $('cx-bt-progress');
    if (bar && prog) {
      var running = /queued|running|submitting/i.test(text);
      bar.classList.toggle('is-indeterminate', running);
      if (/done/i.test(text)) {
        bar.classList.remove('is-indeterminate');
        bar.style.width = '100%';
        prog.setAttribute('aria-valuenow', '100');
      } else if (!running) {
        bar.classList.remove('is-indeterminate');
        bar.style.width = '0';
        prog.setAttribute('aria-valuenow', '0');
      }
    }
  }

  function setLog(text) {
    var el = $('cx-bt-log');
    if (el) el.textContent = text;
  }

  function pollJob() {
    if (!state.activeJob) return;
    getJSON('/api/backtest/jobs/' + encodeURIComponent(state.activeJob) + '/result')
      .then(function (res) {
        var job = res.job || {};
        var lines = job.progress || [];
        setLog(lines.length ? lines.join('\n') : 'Running…');
        var st = String(job.status || '').toLowerCase();
        setJobStatus(st || 'running');

        if (st === 'done' || st === 'failed' || st === 'cancelled') {
          if (st === 'done') {
            toast('Backtest complete', 'success');
            renderBacktestResult(job.result);
          } else if (st === 'failed') {
            toast('Backtest failed: ' + (job.error || 'unknown'), 'error');
          }
          state.activeJob = null;
          loadJobs();
          return;
        }
        state.timers.jobs = setTimeout(pollJob, POLL.jobs);
      })
      .catch(function () { state.timers.jobs = setTimeout(pollJob, POLL.jobs * 2); });
  }

  function loadJobs() {
    return getJSON('/api/backtest/jobs')
      .then(function (res) {
        state.jobs = (res && res.jobs) || [];
        renderJobs();
      })
      .catch(function () { /* the panel simply stays as it was */ });
  }

  function renderJobs() {
    var host = $('cx-bt-history');
    if (!host) return;
    host.textContent = '';
    if (!state.jobs.length) {
      var p = document.createElement('p');
      p.className = 'cx-empty';
      p.textContent = 'No runs yet.';
      host.appendChild(p);
      return;
    }
    state.jobs.forEach(function (j) {
      var row = document.createElement('div');
      row.className = 'cx-sel';

      var top = document.createElement('div');
      top.className = 'cx-sel__top';
      var label = document.createElement('span');
      label.className = 'cx-sel__sym';
      label.textContent = j.label || j.id;
      var st = document.createElement('span');
      st.className = 'cx-chip ' + (j.status === 'DONE' ? 'cx-chip--bull'
        : j.status === 'FAILED' ? 'cx-chip--bear' : 'cx-chip--muted');
      st.textContent = j.status;
      var when = document.createElement('span');
      when.className = 'cx-sel__score';
      when.textContent = (j.created_utc || '').replace('T', ' ').slice(0, 19);
      top.appendChild(label); top.appendChild(st); top.appendChild(when);
      row.appendChild(top);

      var meta = document.createElement('p');
      meta.className = 'cx-sel__why';
      var spec = j.spec || {};
      meta.textContent = (spec.modes || []).join(', ') + ' · maximise ' + (spec.objective || '—') +
        ' · min trades ' + (spec.min_trades != null ? spec.min_trades : '—');
      row.appendChild(meta);

      if (j.status === 'DONE') {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cx-btn cx-btn--sm';
        btn.textContent = 'View results';
        btn.setAttribute('data-job-result', j.id);
        row.appendChild(btn);
      } else if (j.status === 'QUEUED' || j.status === 'RUNNING') {
        var cbtn = document.createElement('button');
        cbtn.type = 'button';
        cbtn.className = 'cx-btn cx-btn--sm cx-btn--danger';
        cbtn.textContent = 'Cancel';
        cbtn.setAttribute('data-job-cancel', j.id);
        row.appendChild(cbtn);
      }
      host.appendChild(row);
    });
  }

  function renderBacktestResult(result) {
    var host = $('cx-bt-results');
    if (!host) return;
    host.textContent = '';
    if (!result) {
      var p = document.createElement('p');
      p.className = 'cx-empty';
      p.textContent = 'No result payload.';
      host.appendChild(p);
      return;
    }

    setText('cx-bt-res-meta', (result.elapsed_seconds != null ? result.elapsed_seconds + 's · ' : '') +
      (result.evaluations != null ? result.evaluations + ' evaluations' : ''));

    (result.modes || []).forEach(function (m) {
      if (m.error) return;
      var card = document.createElement('div');
      card.className = 'cx-sel';

      var top = document.createElement('div');
      top.className = 'cx-sel__top';
      var sym = document.createElement('span');
      sym.className = 'cx-sel__sym';
      sym.textContent = m.style;
      var feas = document.createElement('span');
      feas.className = 'cx-chip ' + (m.feasible ? 'cx-chip--bull' : 'cx-chip--warn');
      feas.textContent = m.feasible ? 'feasible' : 'no feasible geometry';
      var wf = (m.walk_forward || {});
      var gen = document.createElement('span');
      gen.className = 'cx-chip ' + (wf.generalises ? 'cx-chip--bull' : 'cx-chip--muted');
      gen.textContent = wf.generalises ? 'generalises' : 'not confirmed';
      top.appendChild(sym); top.appendChild(feas); top.appendChild(gen);
      card.appendChild(top);

      var detail = document.createElement('p');
      detail.className = 'cx-sel__why';
      detail.textContent = 'best: ' + (m.best_geometry_key || '—') +
        ' · selectivity q=' + num(m.best_min_score_quantile, 2);
      card.appendChild(detail);

      var metrics = document.createElement('p');
      metrics.className = 'cx-sel__why';
      var is = m.in_sample || {}, oos = m.out_of_sample || {};
      metrics.textContent =
        'in-sample ' + signed(is.expectancy_r, 4) + 'R / ' + (is.trades || 0) + ' trades' +
        '  →  held-out ' + signed(oos.expectancy_r, 4) + 'R / ' + (oos.trades || 0) + ' trades' +
        '  · retention ' + (wf.edge_retention == null ? 'n/a' : num(wf.edge_retention, 2)) +
        '  · symbols positive ' + (m.symbols_positive || 0) + '/' + (m.symbols_total || 0);
      card.appendChild(metrics);

      if (wf.note) {
        var note = document.createElement('p');
        note.className = 'cx-sel__why';
        note.textContent = wf.note;
        card.appendChild(note);
      }

      if (m.per_symbol && m.per_symbol.length) {
        var table = document.createElement('table');
        table.className = 'cx-table';
        table.innerHTML = '<thead><tr><th scope="col">Symbol</th><th scope="col" class="cx-num">Trades</th>' +
          '<th scope="col" class="cx-num">Total R</th><th scope="col" class="cx-num">Expectancy</th>' +
          '<th scope="col" class="cx-num">Win%</th><th scope="col" class="cx-num">PF</th></tr></thead>';
        var tb = document.createElement('tbody');
        m.per_symbol.slice(0, 20).forEach(function (r) {
          var tr = document.createElement('tr');
          function cell(text, cls) {
            var td = document.createElement('td');
            if (cls) td.className = cls;
            td.textContent = text;
            tr.appendChild(td);
          }
          cell(r.symbol);
          cell(String(r.trades), 'cx-num');
          cell(signed(r.total_r, 3), 'cx-num ' + pnlClass(r.total_r));
          cell(signed(r.expectancy_r, 4), 'cx-num ' + pnlClass(r.expectancy_r));
          cell(num((r.win_rate || 0) * 100, 1), 'cx-num');
          cell(num(r.profit_factor, 2), 'cx-num');
          tb.appendChild(tr);
        });
        table.appendChild(tb);
        card.appendChild(table);
      }

      host.appendChild(card);
    });

    if (!host.children.length) {
      var p2 = document.createElement('p');
      p2.className = 'cx-empty';
      p2.textContent = 'No modes were evaluated.';
      host.appendChild(p2);
    }
  }

  function loadJobResult(jobId) {
    setLog('Loading result for ' + jobId + '…');
    getJSON('/api/backtest/jobs/' + encodeURIComponent(jobId) + '/result')
      .then(function (res) { renderBacktestResult((res.job || {}).result); })
      .catch(function (err) { toast('Could not load result: ' + err.message, 'error'); });
  }

  /* ── actions ──────────────────────────────────────────────────────────── */
  function selectSymbol(sym) {
    if (!sym) return;
    state.selected = sym;
    var symInput = $('cx-ticket-sym');
    if (symInput) symInput.value = sym;
    setText('cx-ticket-symbol', sym);
    loadChart();
    loadReasoning();
    loadTelemetry();
  }

  function closeAllPositions() {
    var n = ((state.telemetry || {}).positions || []).length;
    if (!n) { toast('No open positions.', 'info'); return; }
    if (!window.confirm('Close all ' + n + ' open position(s)? This cannot be undone.')) return;
    postJSON('/api/action/close_all_positions', {})
      .then(function () { toast('Close-all requested.', 'success'); loadTelemetry(); })
      .catch(function (err) { toast('Close-all failed: ' + err.message, 'error'); });
  }

  function closePosition(ticket) {
    postJSON('/api/action/close_position', { ticket: Number(ticket) })
      .then(function () { toast('Close requested for #' + ticket, 'success'); loadTelemetry(); })
      .catch(function (err) { toast('Close failed: ' + err.message, 'error'); });
  }

  function submitTicket(ev) {
    ev.preventDefault();
    var btn = ev.submitter || null;
    var side = btn && btn.getAttribute('data-side') ? btn.getAttribute('data-side') : 'BUY';
    var payload = {
      symbol: ($('cx-ticket-sym') || {}).value || state.selected,
      side: side,
      lots: Number(($('cx-ticket-lots') || {}).value || 0.01),
      order_type: ($('cx-ticket-type') || {}).value || 'MARKET',
      sl: Number(($('cx-ticket-sl') || {}).value) || 0,
      tp: Number(($('cx-ticket-tp') || {}).value) || 0
    };
    postJSON('/api/action/manual_trade', payload)
      .then(function (res) {
        toast(side + ' ' + payload.symbol + ' submitted (' + (res.status || 'ok') + ')', 'success');
        loadTelemetry();
      })
      .catch(function (err) { toast('Order rejected: ' + err.message, 'error'); });
  }

  /* ── command palette ──────────────────────────────────────────────────── */
  // The single source of truth for shortcuts: the palette renders from this
  // list and the key handler dispatches into it, so nothing can be advertised
  // without being bound.
  var COMMANDS = [
    { id: 'view-trade', label: 'Go to Trade view', keys: 'G T', run: function () { setView('trade'); } },
    { id: 'view-backtest', label: 'Go to Backtest view', keys: 'G B', run: function () { setView('backtest'); } },
    { id: 'view-analytics', label: 'Go to Analytics view', keys: 'G A', run: function () { setView('analytics'); } },
    { id: 'sel-refresh', label: 'Refresh auto-selection', keys: 'S', run: function () { loadSelection(true); toast('Re-scanning all modes…', 'info'); } },
    { id: 'reason', label: 'Re-read engine reasoning', keys: 'C', run: function () { loadReasoning(); toast('Reasoning refreshed.', 'info'); } },
    { id: 'panel', label: 'Cycle mobile panel', keys: 'P', run: function () { cyclePanel(); } },
    { id: 'close-all', label: 'Close all positions', keys: 'X', run: function () { closeAllPositions(); } },
    { id: 'chart-tf', label: 'Cycle chart timeframe', keys: 'F', run: function () { cycleTimeframe(); } },
    { id: 'classic', label: 'Open the classic terminal', keys: '', run: function () { window.location.href = '/classic'; } },
    { id: 'stocks', label: 'Switch to the US Stocks desk', keys: '', run: function () { window.location.href = '/stocks'; } },
    { id: 'india', label: 'Switch to the India desk', keys: '', run: function () { window.location.href = '/india'; } },
    { id: 'options', label: 'Switch to the Options desk', keys: '', run: function () { window.location.href = '/options'; } }
  ];

  var paletteIndex = 0;
  var paletteFiltered = COMMANDS.slice();

  function openPalette() {
    var el = $('cx-palette');
    if (!el) return;
    el.hidden = false;
    var input = $('cx-palette-input');
    if (input) { input.value = ''; input.focus(); }
    paletteFiltered = COMMANDS.slice();
    paletteIndex = 0;
    renderPalette();
  }

  function closePalette() {
    var el = $('cx-palette');
    if (el) el.hidden = true;
  }

  function renderPalette() {
    var list = $('cx-palette-list');
    if (!list) return;
    list.textContent = '';
    paletteFiltered.forEach(function (c, i) {
      var li = document.createElement('li');
      li.className = 'cx-palette__item';
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', i === paletteIndex ? 'true' : 'false');
      li.setAttribute('data-cmd', c.id);

      var span = document.createElement('span');
      span.textContent = c.label;
      li.appendChild(span);

      if (c.keys) {
        var k = document.createElement('span');
        k.className = 'cx-palette__key';
        k.textContent = c.keys;
        li.appendChild(k);
      }
      list.appendChild(li);
    });
    if (!paletteFiltered.length) {
      var li2 = document.createElement('li');
      li2.className = 'cx-palette__item';
      li2.textContent = 'No matching command.';
      list.appendChild(li2);
    }
  }

  function filterPalette(q) {
    var needle = String(q || '').toLowerCase();
    paletteFiltered = COMMANDS.filter(function (c) {
      return !needle || c.label.toLowerCase().indexOf(needle) !== -1;
    });
    paletteIndex = 0;
    renderPalette();
  }

  function runPaletteSelection() {
    var c = paletteFiltered[paletteIndex];
    closePalette();
    if (c) c.run();
  }

  function cycleTimeframe() {
    var sel = $('cx-chart-tf');
    if (!sel) return;
    var i = sel.selectedIndex;
    sel.selectedIndex = (i + 1) % sel.options.length;
    state.timeframe = sel.value;
    loadChart();
    toast('Chart timeframe: ' + state.timeframe, 'info');
  }

  /* ── desk sheet ───────────────────────────────────────────────────────── */
  function openDesks() {
    var sheet = $('cx-desks-sheet');
    if (!sheet) return;
    sheet.hidden = false;
    var btn = $('cx-desks-toggle');
    if (btn) btn.setAttribute('aria-expanded', 'true');
    var close = $('cx-desks-close');
    if (close) close.focus();
  }

  function closeDesks() {
    var sheet = $('cx-desks-sheet');
    if (!sheet) return;
    sheet.hidden = true;
    var btn = $('cx-desks-toggle');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  /* ── events ───────────────────────────────────────────────────────────── */
  function bind() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-view-btn]'), function (btn) {
      btn.addEventListener('click', function () { setView(btn.getAttribute('data-view-btn')); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-panel-btn]'), function (btn) {
      btn.addEventListener('click', function () { setPanel(btn.getAttribute('data-panel-btn')); });
    });

    var wl = $('cx-watchlist-body');
    if (wl) wl.addEventListener('click', function (ev) {
      var tr = ev.target.closest ? ev.target.closest('tr[data-symbol]') : null;
      if (tr) selectSymbol(tr.getAttribute('data-symbol'));
    });

    var sel = $('cx-selection-body');
    if (sel) {
      sel.addEventListener('click', function (ev) {
        var card = ev.target.closest ? ev.target.closest('[data-select-symbol]') : null;
        if (card) { selectSymbol(card.getAttribute('data-select-symbol')); setPanel('chart'); }
      });
      sel.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        var card = ev.target.closest ? ev.target.closest('[data-select-symbol]') : null;
        if (card) { ev.preventDefault(); selectSymbol(card.getAttribute('data-select-symbol')); setPanel('chart'); }
      });
    }

    var pos = $('cx-pos-body');
    if (pos) pos.addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('[data-close-ticket]') : null;
      if (btn) closePosition(btn.getAttribute('data-close-ticket'));
    });

    var hist = $('cx-bt-history');
    if (hist) hist.addEventListener('click', function (ev) {
      var r = ev.target.closest ? ev.target.closest('[data-job-result]') : null;
      if (r) { loadJobResult(r.getAttribute('data-job-result')); return; }
      var c = ev.target.closest ? ev.target.closest('[data-job-cancel]') : null;
      if (c) {
        postJSON('/api/backtest/cancel', { id: c.getAttribute('data-job-cancel') })
          .then(function () { toast('Cancellation requested.', 'info'); loadJobs(); })
          .catch(function (err) { toast('Cancel failed: ' + err.message, 'error'); });
      }
    });

    var form = $('cx-ticket-form');
    if (form) form.addEventListener('submit', submitTicket);

    var btForm = $('cx-bt-form');
    if (btForm) btForm.addEventListener('submit', submitBacktest);

    var tfSel = $('cx-chart-tf');
    if (tfSel) tfSel.addEventListener('change', function () { state.timeframe = tfSel.value; loadChart(); });

    var symInput = $('cx-ticket-sym');
    if (symInput) symInput.addEventListener('change', function () { selectSymbol(symInput.value.toUpperCase().trim()); });

    var bind1 = function (id, fn) { var el = $(id); if (el) el.addEventListener('click', fn); };
    bind1('cx-selection-refresh', function () { loadSelection(true); toast('Re-scanning all modes…', 'info'); });
    bind1('cx-why-refresh', loadReasoning);
    bind1('cx-close-all', closeAllPositions);
    bind1('cx-palette-btn', openPalette);
    bind1('cx-bt-hist-refresh', loadJobs);
    bind1('cx-an-rel-refresh', loadReliability);
    bind1('cx-desks-toggle', openDesks);
    bind1('cx-desks-close', closeDesks);

    var desksSheet = $('cx-desks-sheet');
    if (desksSheet) desksSheet.addEventListener('click', function (ev) {
      if (ev.target === desksSheet) closeDesks();
    });

    var pal = $('cx-palette');
    if (pal) pal.addEventListener('click', function (ev) {
      if (ev.target === pal) { closePalette(); return; }
      var item = ev.target.closest ? ev.target.closest('[data-cmd]') : null;
      if (item) {
        var cmd = COMMANDS.filter(function (c) { return c.id === item.getAttribute('data-cmd'); })[0];
        closePalette();
        if (cmd) cmd.run();
      }
    });

    var palInput = $('cx-palette-input');
    if (palInput) palInput.addEventListener('input', function () { filterPalette(palInput.value); });

    document.addEventListener('keydown', onKeydown);
  }

  function onKeydown(ev) {
    var tag = (ev.target && ev.target.tagName) || '';
    var typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (ev.target && ev.target.isContentEditable);

    // Palette navigation first — it must work while the input has focus.
    var paletteOpen = $('cx-palette') && !$('cx-palette').hidden;
    if (paletteOpen) {
      if (ev.key === 'Escape') { ev.preventDefault(); closePalette(); return; }
      if (ev.key === 'ArrowDown') { ev.preventDefault(); paletteIndex = Math.min(paletteIndex + 1, paletteFiltered.length - 1); renderPalette(); return; }
      if (ev.key === 'ArrowUp') { ev.preventDefault(); paletteIndex = Math.max(paletteIndex - 1, 0); renderPalette(); return; }
      if (ev.key === 'Enter') { ev.preventDefault(); runPaletteSelection(); return; }
    }

    if ((ev.ctrlKey || ev.metaKey) && String(ev.key).toLowerCase() === 'k') {
      ev.preventDefault();
      paletteOpen ? closePalette() : openPalette();
      return;
    }

    if (ev.key === 'Escape') {
      if ($('cx-desks-sheet') && !$('cx-desks-sheet').hidden) { closeDesks(); return; }
    }

    if (typing) return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

    var k = String(ev.key).toLowerCase();

    // Two-key "g" prefix: G then T/B/A.
    if (state.gPressed) {
      state.gPressed = false;
      if (k === 't') { ev.preventDefault(); setView('trade'); return; }
      if (k === 'b') { ev.preventDefault(); setView('backtest'); return; }
      if (k === 'a') { ev.preventDefault(); setView('analytics'); return; }
      return;
    }

    if (k === 'g') { state.gPressed = true; setTimeout(function () { state.gPressed = false; }, 1200); return; }
    if (k === 's') { ev.preventDefault(); loadSelection(true); toast('Re-scanning all modes…', 'info'); return; }
    if (k === 'c') { ev.preventDefault(); loadReasoning(); return; }
    if (k === 'p') { ev.preventDefault(); cyclePanel(); return; }
    if (k === 'x') { ev.preventDefault(); closeAllPositions(); return; }
    if (k === 'f') { ev.preventDefault(); cycleTimeframe(); return; }
    if (ev.key === '?') { ev.preventDefault(); openPalette(); return; }
  }

  /* ── polling ──────────────────────────────────────────────────────────── */
  function schedule(name, fn, ms) {
    function tick() {
      Promise.resolve()
        .then(fn)
        .catch(function () { /* keep the loop alive through transient errors */ })
        .then(function () {
          state.timers[name] = setTimeout(tick, document.hidden ? ms * 4 : ms);
        });
    }
    state.timers[name] = setTimeout(tick, ms);
  }

  /* ── first-paint watchdog ─────────────────────────────────────────────── */
  /* Nothing in this file sets a request timeout, so a poll that never settles
     leaves the template's placeholder on screen indefinitely and silently -
     the panel simply says "Loading…" forever. A measured run showed the server
     answering every request in 10-291ms while the page still sat on that
     placeholder, so the fault is not always server-side and cannot be
     diagnosed from the panel alone.

     The watchdog makes the state honest rather than guessing at the cause: if
     the first telemetry poll has not landed by the grace period, the page says
     so, marks the connection down, and self-heals on the next successful poll
     (every render clears the body first). */
  var FIRST_PAINT_GRACE = 12000;

  function watchdogFirstPaint() {
    setTimeout(function () {
      if (state.telemetry) return;              // the poll landed - nothing to say

      markConnection(false);

      var wl = $('cx-watchlist-body');
      if (wl && /Loading/.test(wl.textContent)) {
        wl.textContent = '';
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 4;
        td.className = 'cx-empty';
        td.textContent = 'No telemetry after ' + (FIRST_PAINT_GRACE / 1000) +
          's — the request has not completed. Check the server, then reload.';
        tr.appendChild(td);
        wl.appendChild(tr);
      }

      var sel = $('cx-selection-body');
      if (sel && /Loading/.test(sel.textContent)) {
        sel.textContent = '';
        var p = document.createElement('p');
        p.className = 'cx-empty';
        p.textContent = 'The consensus scan has not returned yet.';
        sel.appendChild(p);
      }
    }, FIRST_PAINT_GRACE);
  }

  /* ── boot ─────────────────────────────────────────────────────────────── */
  function boot() {
    initChart();
    bind();
    setView('trade');
    setPanel('chart');

    // Seed the watchlist from whatever the radar already knows, so the first
    // paint is not an empty table while the first poll is in flight.
    //
    // Unguarded on purpose. This used to be `if (syms.length) { ... render }`,
    // so an empty radar - which is the normal state when no orchestrator is
    // attached - skipped the render entirely and the template's "Loading…" row
    // stayed on screen for as long as the page was open. The panel was not
    // broken and the data was not missing; it was never asked to repaint.
    loadTelemetry().then(function () {
      var radar = ((state.telemetry || {}).radar_opportunities) || [];
      var syms = [];
      radar.forEach(function (r) { if (r && r.symbol && syms.indexOf(r.symbol) === -1) syms.push(r.symbol); });
      if (syms.length) state.symbols = syms;
      renderWatchlist(state.telemetry);
    });

    watchdogFirstPaint();

    loadChart();
    loadSelection(false);
    loadReasoning();

    schedule('telemetry', loadTelemetry, POLL.telemetry);
    schedule('chart', loadChart, POLL.chart);
    schedule('selection', function () { return loadSelection(false); }, POLL.regime);
    schedule('analytics', function () {
      if (state.view === 'analytics') return loadAnalytics();
      return null;
    }, POLL.analytics);

    if (window.HM_AUTH && typeof window.HM_AUTH.verifyToken === 'function') {
      try { window.HM_AUTH.verifyToken(); } catch (e) { /* not fatal */ }
    }

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { loadTelemetry(); loadChart(); }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
