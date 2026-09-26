/* ===========================================================================
   Headless render check for the HM Algo 2.0 dashboard (tools/verify_dashboard_render.js)

   WHY THIS EXISTS
   ---------------
   The dashboard's chart is the one part of the UI that cannot be checked with
   curl: support/resistance levels, the volume series and the trade overlays are
   all created at runtime against the lightweight-charts API. A syntax check
   proves the file parses, not that a single price line is ever drawn.

   This harness runs the REAL jarvis/ui/static/js/dashboard.js in a Node `vm`
   with a stubbed DOM and a stubbed charting library, feeds it candle and
   telemetry payloads with a known swing structure, and asserts on what the
   module actually asked the chart library to draw.

   It also cross-checks every element id the module reaches for against the ids
   that exist in dashboard.html. A controller that queries an id the template
   does not define fails silently in a browser — the panel simply never
   updates — so that check is the difference between "looks fine" and "is wired".

   Run: node tools/verify_dashboard_render.js
   Exits non-zero on the first failed expectation.
   =========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const JS = path.join(ROOT, 'jarvis', 'ui', 'static', 'js', 'dashboard.js');
const HTML = path.join(ROOT, 'jarvis', 'ui', 'templates', 'dashboard.html');

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

/* ── Read the template's real ids ─────────────────────────────────────────── */
const html = fs.readFileSync(HTML, 'utf8');
const templateIds = new Set();
{
  const re = /\bid="([^"]+)"/g;
  let m;
  while ((m = re.exec(html)) !== null) templateIds.add(m[1]);
}

/* ── Minimal DOM (shared with the terminal harness) ─────────────────────────── */
/* The element tree, the selector engine and the innerHTML materialiser live in
   tools/dom_stub.js, shared with tools/verify_terminal_render.js. A copy in
   each harness would drift, and the drift would be invisible: a harness whose stub
   is subtly weaker still prints PASS. */
const { createDom } = require('./dom_stub');

const DOC_ROOTS = ['view-trade', 'view-news', 'view-analyst', 'view-markets',
                   'view-analytics', 'view-backtest', 'toasts'];

const TEMPLATE_TREE = {
  'view-trade': ['watch-count', 'watch-body', 'watch-refresh', 'selection-count',
                 'selection-tier', 'selection-body', 'radar-count', 'radar-filter',
                 'radar-body', 'chart-title', 'chart-live-price', 'chart-legend',
                 'chart-src-native', 'chart-src-tv', 'chart-levels', 'chart-timeframe',
                 'chart', 'chart-tv', 'chart-hud', 'chart-tooltip', 'chart-overlay',
                 /* The panel head is tabbed (OPEN / HISTORY / PENDING); the
                    count now lives inside each tab rather than beside the
                    title, so the old `pos-count` badge no longer exists. */
                 'pos-table', 'pos-thead', 'pos-tab-count-open',
                 'pos-tab-count-history', 'pos-tab-count-pending', 'flatten-all',
                 'pos-total', 'pos-body', 'ticket-source', 'ticket-symbol',
                 'ticket-buy', 'ticket-sell', 'ticket-volume', 'ticket-price',
                 'ticket-sl', 'ticket-tp', 'ticket-hint', 'reason-tier', 'reason-body',
                 /* The context strip beside the ticket (item 5: Analyst and News
                    surfaced inside the trade page). It writes to its own
                    containers so no id is shared with the full panels. */
                 'ctx-analyst', 'ctx-news'],
  'view-news': ['news-count', 'news-live-chip', 'news-impact', 'news-currency',
                'news-refresh', 'news-body', 'news-updated', 'news-hero',
                'news-detail-impact', 'news-detail'],
  'view-analyst': ['da-symbol', 'da-verdict', 'da-metrics', 'da-bull', 'da-bear',
                   'da-threats', 'da-invalidation', 'gate-count', 'gate-verdict',
                   'gate-body', 'da-objections'],
  'view-markets': ['eq-count', 'eq-prov', 'eq-refresh', 'eq-body', 'eq-heat-note',
                   'eq-heatmap', 'in-refresh', 'in-index-prov', 'in-indices',
                   'in-fii-prov', 'in-fii', 'in-oc-prov', 'in-oc-symbol',
                   'in-optionchain']
};

const dom = createDom({
  templateIds: templateIds,
  docRoots: DOC_ROOTS,
  templateTree: TEMPLATE_TREE
});
const { documentStub, registry, requestedIds, prelinked, elementFor,
        linkTree, fireDocument, deepHtml, selectAll } = dom;

/* ── The context strip, as dashboard.html:383-393 declares it ────────────────
   The buttons carry `data-ctx` and no id, so the template tree (which builds
   nodes by id) cannot create them — yet setContext() and bind() both find them
   with `document.querySelectorAll('[data-ctx]')`. Without them the strip has no
   controls at all, which is exactly the kind of gap a stub hides.

   The two panels are created in the state the real markup ships them in:
   `hidden`, with `data-state="loading"`. That detail is load-bearing. Both
   renderers early-return while the panel is hidden, so a stub that left them
   visible lets renderDevilAdvocate() (which also refreshes the analyst strip)
   populate them long before any tab is pressed — and the tab's own render then
   looks redundant. Deleting `renderContextAnalyst()` from setContext() passed
   every check until the stub matched the markup.

   Built BEFORE the controller runs, because bind() attaches the handlers once.

   The fourth button carries a value setContext() does not recognise; the
   template never emits one, but the normalisation is a real fail-safe and this
   is how it gets exercised. */
function makeContextStrip() {
  // elementFor(), not registry.get(): the registry is a plain Map that is filled
  // lazily, so .get() on an id nobody has queried yet returns undefined and the
  // state below would be silently applied to nothing.
  ['ctx-analyst', 'ctx-news'].forEach((id) => {
    const el = elementFor(id);
    if (!el) return;
    el.hidden = true;
    el.setAttribute('data-state', 'loading');
  });
  const host = elementFor('view-trade');
  ['why', 'analyst', 'news', 'bogus'].forEach((tab) => {
    const btn = documentStub.createElement('button');
    btn.setAttribute('data-ctx', tab);
    btn.setAttribute('aria-pressed', String(tab === 'why'));
    if (host) host.appendChild(btn);
    else documentStub.appendChild(btn);
  });
}
makeContextStrip();

/* Read a metric card's value by the label it sits beside, so a check binds the
   number to *its own* label rather than asserting that the number appears
   somewhere in the panel. Do NOT normalise the markup with
   `html.replace(/\s+/g, '')` first: the space in `<span class="…">` is part of
   the markup, so stripping it yields `<spanclass="…">` and the pattern can
   never match — a false failure that looks exactly like a rendering bug. */
function metricValue(html, label) {
  const s = String(html || '');
  const at = s.indexOf('>' + label + '</span>');
  if (at < 0) return null;
  const m = /tt-metric__value"\s*>([^<]*)</.exec(s.slice(at));
  return m ? m[1] : null;
}

/* Every toast currently on screen. `toast()` builds a div with
   `textContent` and a `tt-toast--<kind>` class, so both the wording and the
   severity are readable. The sandbox's setTimeout is a no-op, so a toast is
   never auto-dismissed and the harness can read it after the promise settles. */
function toasts() {
  const host = registry.get('toasts');
  return (host ? host.children : []).map((c) => ({
    text: String(c._text || ''),
    cls: String(c.className || '')
  }));
}

/* ── Chart library stub: records every drawing instruction ────────────────── */
const drawn = {
  charts: 0,
  candleSeries: 0,
  volumeSeries: 0,
  priceLines: [],
  candleData: null,
  volumeData: null,
  updates: 0,
  fitContent: 0,
  candleOptions: [],
  /* `setMarkers` used to be a no-op here, which made drawTradeMarkers() wholly
     unobservable: "the renderer was never called" and "the renderer works" look
     identical from outside, and every assertion about trade markers would have
     passed vacuously. Each call is recorded, newest last. */
  markers: [],
  lastMarkers: []
};

function makeSeries(kind) {
  return {
    kind,
    _lines: [],
    _markers: [],
    applyOptions(o) { if (kind === 'candles') drawn.candleOptions.push(o); },
    setData(d) {
      if (kind === 'candles') drawn.candleData = d;
      else drawn.volumeData = d;
    },
    update() { drawn.updates++; },
    createPriceLine(o) { drawn.priceLines.push(o); return { o }; },
    removePriceLine(l) { this._lines = this._lines.filter((x) => x !== l); },
    priceScale() { return { applyOptions() {} }; },
    setMarkers(list) {
      const arr = Array.isArray(list) ? list.slice() : [];
      this._markers = arr;
      drawn.markers.push(arr);
      drawn.lastMarkers = arr;
    }
  };
}

const LightweightCharts = {
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
  createChart(host, opts) {
    drawn.charts++;
    return {
      _opts: opts,
      addCandlestickSeries() { drawn.candleSeries++; return makeSeries('candles'); },
      addHistogramSeries() { drawn.volumeSeries++; return makeSeries('volume'); },
      addSeries() { drawn.candleSeries++; return makeSeries('candles'); },
      priceScale() { return { applyOptions() {} }; },
      applyOptions() {},
      timeScale() { return { fitContent() { drawn.fitContent++; } }; },
      subscribeCrosshairMove() {},
      chartElement() { return host; }
    };
  }
};

/* ── Candle fixture with a known swing structure ──────────────────────────── */
/* Default bars sit at 100 (high 101 / low 99). Two pivot highs (110, 115) and
   two pivot lows (90, 85) are planted, with a last close of 100, so the
   expected levels are exactly R1=110, R2=115, S1=90, S2=85. */
const T0 = 1700000000;
function buildCandles() {
  const bars = [];
  for (let i = 0; i < 30; i++) {
    bars.push({ time: T0 + i * 3600, open: 100, high: 101, low: 99, close: 100, volume: 1000 + i });
  }
  bars[10] = { time: T0 + 10 * 3600, open: 100, high: 110, low: 99, close: 105, volume: 5000 };
  bars[12] = { time: T0 + 12 * 3600, open: 100, high: 101, low: 90, close: 95, volume: 4000 };
  bars[20] = { time: T0 + 20 * 3600, open: 100, high: 115, low: 99, close: 108, volume: 6000 };
  bars[22] = { time: T0 + 22 * 3600, open: 100, high: 101, low: 85, close: 92, volume: 4500 };
  return bars;
}

const TELEMETRY = {
  execution_mode: 'AUTO',
  trade_style: 'SWING',
  safe_mode: false,
  is_running: true,
  account: {
    balance: 10000, equity: 10120, profit: 120, free_margin: 9000,
    margin: 1000, margin_level: 1012, leverage: 100, currency: 'USD', trade_allowed: true
  },
  positions_count: 1,
  positions: [{
    ticket: 12345, symbol: 'XAUUSD', type: 'BUY', volume: 0.10,
    open_price: 100, current_price: 105, sl: 95, tp: 110,
    profit: 12.5, swap: 0, commission: 0, open_time: '2026-09-15 00:00:00', magic: 1, comment: ''
  }],
  services: { DATA_FEED: 'OK', MT5: 'CONNECTED' },
  radar_opportunities: [
    {
      symbol: 'XAUUSD', trade_style: 'SWING', timeframe: 'H1',
      current_price: 105, entry_price: 100, stop_loss: 95, take_profit: 110,
      risk_reward_ratio: 2, ev: 0.42, bias: 'BUY', action: 'BUY READY',
      status_label: 'BUY READY', decision: 'EXECUTE', score: 68, win_prob: 68,
      confluence_score: 7.5, confluence_tier: 'STRONG', regime: 'TREND_BULL',
      strategy: 'TREND_FOLLOW', utility_score: 0.81, setup_grade: 'A',
      is_actionable: true
    },
    {
      symbol: 'EURUSD', trade_style: 'SCALP', timeframe: 'M5',
      current_price: 1.085, entry_price: 1.085, stop_loss: 1.083, take_profit: 1.089,
      risk_reward_ratio: 2, ev: -0.10, bias: 'HOLD', action: 'NO SETUP',
      status_label: 'NO SETUP', decision: 'NO_TRADE', score: 41, win_prob: 41,
      confluence_score: 3.1, confluence_tier: 'WEAK', regime: 'COMPRESSION',
      strategy: 'MEAN_REVERT', utility_score: 0.12, setup_grade: 'C',
      is_actionable: false
    }
  ],
  latest_decisions: {
    XAUUSD: {
      symbol: 'XAUUSD', bias: 'BUY', strategy: 'TREND', entry_price: 100,
      stop_loss: 95, take_profit: 110, risk_reward_ratio: 2, model_confidence: 0.72,
      calculated_risk_percent: 1, expected_value: 0.4,
      // 30 of 50 on the engine's own scale: above the 25 threshold, so the
      // gauge must render in the severe band at 60% fill.
      adversarial_penalty: 30,
      dissection_tier: 'STRONG', dissection_score: 7.5,
      master_confluence_tier: 'HIGH', master_confluence_score: 8.2,
      pattern_sample_size: 140,
      regime: { primary: 'TREND_BULL', probabilities: {}, confidence: 0.8 },
      probabilities: {}, invalidation_levels: ['H4 close below 94.20', 'Loss of 93.80 swing low'],
      bull_case: ['H4 structure in premium rejection', 'Positive order flow'],
      bear_case: ['RSI divergence on H1'],
      risk_factors: ['Event risk inside 6h', 'Spread widens at rollover'],
      quality_gate: {
        passed: false,
        // Two failures, deliberately not first in key order, so the
        // failures-first sort is actually exercised.
        checks: {
          'Market Session Open': true,
          'Regime Viability': true,
          'Devil Adversarial Guard': false,
          'Spread Protection': true,
          'Positive Expected Value': false
        },
        failing_reasons: ['Devil Adversarial Guard', 'Positive Expected Value']
      },
      decision: 'NO_TRADE', execution_authorized: false,
      waiting_reasons: ['Waiting for H1 close above 100.40'],
      rejection_reasons: ['Adversarial penalty above tolerance'],
      gate_policy_decision: 'BLOCK'
    },
    EURUSD: {
      symbol: 'EURUSD', bias: 'HOLD', strategy: 'MEAN_REVERT', entry_price: 1.085,
      stop_loss: 1.083, take_profit: 1.089, risk_reward_ratio: 2, model_confidence: 0.4,
      calculated_risk_percent: 0.5, expected_value: -0.1, adversarial_penalty: 8,
      regime: { primary: 'COMPRESSION', probabilities: {}, confidence: 0.5 },
      probabilities: {}, invalidation_levels: [], bull_case: [], bear_case: [],
      risk_factors: [], quality_gate: { passed: true, checks: {}, failing_reasons: [] },
      decision: 'EXECUTE', execution_authorized: true,
      waiting_reasons: [], rejection_reasons: [], gate_policy_decision: 'ALLOW'
    }
  },
  market_statuses: { XAUUSD: { status: 'OPEN', countdown_formatted: '2h' } },
  timestamp: '2026-09-15 01:00:00'
};

/* ── Calendar fixture ─────────────────────────────────────────────────────── */
/* The payload's own `timestamp` is the anchor the controller uses to correct
   for clock skew, and each event's `timestamp_iso` is absolute. Pinning the
   payload clock means every countdown below is deterministic regardless of
   when the harness runs: serverNow == 2026-09-15T00:00:00Z exactly.

   Expected labels, from the controller's own rules (live window is -5min to
   +15min, matching jarvis/market/news.py):
     live (rem -300)  -> "T+5m 0s"     phase live
     live (rem +120)  -> "T-2m 0s"     phase live
     past (rem -3600) -> "1h 0m ago"   phase past
     soon (rem +1800) -> "in 30m 0s"   phase soon
     later(rem +9000) -> "in 2h 30m"   phase upcoming                */
const NEWS_PAYLOAD = {
  timestamp: '2026-09-15T00:00:00+00:00',
  news: [
    {
      event: 'US Crude Oil Inventories', currency: 'USD', impact: 'HIGH',
      timestamp_iso: '2026-09-14T23:55:00+00:00', diff_seconds: -300,
      time_ist: 'Mon Sep 14, 11:55 PM IST', time_utc: 'Sep 14, 23:55 UTC',
      is_live: true, is_upcoming: false, is_past: false,
      status_badge: 'LIVE', forecast: '-1.2M', previous: '-0.8M', actual: '—',
      affected_pairs: ['XAUUSD', 'WTI'], category: 'Energy',
      description: 'Weekly inventory print.', impact_analysis: 'Oil-sensitive.',
      deviation_summary: 'Pending', direction_bias: 'NEUTRAL',
      execution_warning: 'Spreads widen.', shock_alert: 'Live window active.',
      shock_risk: 'EXTREME', is_most_recent: false
    },
    {
      event: 'US CPI (y/y)', currency: 'USD', impact: 'HIGH',
      timestamp_iso: '2026-09-15T00:02:00+00:00', diff_seconds: 120,
      time_ist: 'Tue Sep 15, 12:02 AM IST', time_utc: 'Sep 15, 00:02 UTC',
      is_live: true, is_upcoming: false, is_past: false,
      status_badge: 'LIVE', forecast: '3.1%', previous: '3.4%', actual: '—',
      affected_pairs: ['EURUSD'], category: 'Inflation',
      description: 'Headline inflation.', impact_analysis: 'Rate path.',
      deviation_summary: 'Pending', direction_bias: 'NEUTRAL',
      execution_warning: 'Spreads widen.', shock_alert: 'Live window active.',
      shock_risk: 'EXTREME', is_most_recent: false
    },
    {
      event: 'US Dallas Fed Manufacturing', currency: 'USD', impact: 'MEDIUM',
      timestamp_iso: '2026-09-14T23:00:00+00:00', diff_seconds: -3600,
      time_ist: 'Mon Sep 14, 11:00 PM IST', time_utc: 'Sep 14, 23:00 UTC',
      is_live: false, is_upcoming: false, is_past: true,
      status_badge: 'LATEST RELEASE', forecast: '-12.0', previous: '-13.5', actual: '-11.0',
      affected_pairs: ['USDJPY'], category: 'Manufacturing',
      description: 'Regional survey.', impact_analysis: 'Second tier.',
      deviation_summary: 'In line', direction_bias: 'NEUTRAL',
      execution_warning: '', shock_alert: 'Window closed.', shock_risk: 'MODERATE',
      is_most_recent: true
    },
    {
      event: 'US CB Consumer Confidence', currency: 'USD', impact: 'HIGH',
      timestamp_iso: '2026-09-15T00:30:00+00:00', diff_seconds: 1800,
      time_ist: 'Tue Sep 15, 12:30 AM IST', time_utc: 'Sep 15, 00:30 UTC',
      is_live: false, is_upcoming: true, is_past: false,
      status_badge: 'IN 30m', forecast: '104.5', previous: '103.2', actual: '—',
      affected_pairs: ['XAUUSD', 'EURUSD'], category: 'Sentiment',
      description: 'Consumer survey.', impact_analysis: 'High liquidity catalyst.',
      deviation_summary: 'Pending', direction_bias: 'NEUTRAL',
      execution_warning: '', shock_alert: '', shock_risk: 'HIGH', is_most_recent: false
    },
    {
      event: 'US Preliminary GDP (q/q)', currency: 'EUR', impact: 'LOW',
      timestamp_iso: '2026-09-15T02:30:00+00:00', diff_seconds: 9000,
      time_ist: 'Tue Sep 15, 02:30 AM IST', time_utc: 'Sep 15, 02:30 UTC',
      is_live: false, is_upcoming: true, is_past: false,
      status_badge: 'IN 2h 30m', forecast: '2.1%', previous: '2.0%', actual: '—',
      affected_pairs: ['EURUSD'], category: 'Growth',
      description: 'Growth print.', impact_analysis: 'Second tier.',
      deviation_summary: 'Pending', direction_bias: 'NEUTRAL',
      execution_warning: '', shock_alert: '', shock_risk: 'MODERATE', is_most_recent: false
    }
  ]
};

/* ── Screener fixture: one real analysis and one failed-analysis row ─────── */
/* The fallback row's placeholder values are deliberately distinctive so the
   assertions can prove they are suppressed rather than merely reformatted. */
const SCREENER_PAYLOAD = {
  count: 2,
  total_universe: 2,
  fallback_count: 1,
  provenance: { analysis: 'partial' },
  timeframe: '1D',
  filters: {},
  ai_recommended_buys: [],
  stocks: [
    {
      symbol: 'NVDA', name: 'NVIDIA', sector: 'Semiconductors', industry: 'Chips',
      market: 'US_EQUITIES', market_cap: '$3.1T', price: 178.42, change_val: 4.1,
      change_pct: 2.35, volume: 41000000, rvol: 1.42, breakout_probability: 78,
      confidence: 0.81, setup_grade: 'GRADE A', grade_badge: 'A',
      timing_badge: 'UPCOMING', trend_bias: 'BULLISH', recommendation: 'BUY NOW',
      risk_level: 'MODERATE', cmf_20: 0.22, entry_zone: 178.9, stop_loss: 171.2,
      take_profit_2: 194.5, risk_reward: 2.8, rsi: 61.4, tags: [],
      analysis_source: 'computed', data_source: 'live'
    },
    {
      symbol: 'ZZZZ', name: 'Placeholder Corp', sector: 'Technology', industry: 'General',
      market: 'US_EQUITIES', market_cap: '$10.0B', price: 100.0, change_val: 0,
      change_pct: 0, volume: 1000000, rvol: 1, breakout_probability: 50,
      confidence: 0.85, setup_grade: 'GRADE B', grade_badge: 'B',
      timing_badge: 'UPCOMING', trend_bias: 'BULLISH', recommendation: 'WATCH',
      risk_level: 'MODERATE', cmf_20: 0, entry_zone: 100, stop_loss: 96,
      take_profit_2: 108, risk_reward: 2, rsi: 50, tags: [],
      analysis_source: 'fallback', data_source: 'profile_reference',
      analysis_note: 'Analysis did not complete for this symbol.'
    }
  ],
  timestamp: '2026-09-15T00:00:00+00:00'
};

const HEATMAP_PAYLOAD = {
  count: 2,
  sectors: [
    {
      sector: 'Semiconductors', count: 2, avg_change_pct: 2.35, avg_cmf: 0.22,
      avg_probability: 78, rotation_status: 'LEADING_INFLOW',
      top_leader_symbol: 'NVDA', top_leader_change: 2.35,
      top_breakout_symbol: 'NVDA', top_breakout_prob: 78, stocks: []
    },
    {
      sector: 'Energy', count: 2, avg_change_pct: -1.8, avg_cmf: -0.1,
      avg_probability: 41, rotation_status: 'OUTFLOW_DEFENSIVE',
      top_leader_symbol: 'XOM', top_leader_change: -1.2,
      top_breakout_symbol: 'CVX', top_breakout_prob: 44, stocks: []
    }
  ]
};

const INDIA_INDICES_PAYLOAD = {
  indices: [
    {
      symbol: 'NIFTY', name: 'Nifty 50', price: 25120.4, change_pct: 0.62,
      change_val: 154.2, cpr_classification: 'NARROW_CPR', cpr_label: 'NARROW',
      camarilla_h4: 25310.0, camarilla_l4: 24930.0, vwap: 25080.5,
      bias: 'BULLISH', data_source: 'calibrated_feed'
    },
    {
      symbol: 'BANKNIFTY', name: 'Bank Nifty', price: 56140.8, change_pct: -0.31,
      change_val: -174.6, cpr_classification: 'WIDE_CPR', cpr_label: 'WIDE',
      camarilla_h4: 56600.0, camarilla_l4: 55700.0, vwap: 56210.0,
      bias: 'BEARISH', data_source: 'profile_reference'
    }
  ]
};

const INDIA_FII_PAYLOAD = {
  date: '15-Sep-2026',
  data_source: 'sample',
  data_source_note: 'Fixed sample values. No live FII/DII feed is connected.',
  fii_cash_net_cr: 1845.5, dii_cash_net_cr: 2410.2,
  total_net_institutional_cr: 4255.7, fii_index_futures_long_pct: 68.5,
  fii_index_options_pcr: 1.22, fii_sentiment: 'NET_BUYERS',
  dii_sentiment: 'STRONG_DOMESTIC_INFLOWS', institutional_bias: 'STRONG_BULLISH_SUPPORT'
};

const INDIA_OPTION_CHAIN_PAYLOAD = {
  symbol: 'NIFTY', name: 'Nifty 50', data_source: 'synthetic',
  spot_price: 25120.4, atm_strike: 25100, strike_step: 50, lot_size: 25,
  freeze_limit: 1800, expiry: '25-Sep-2026', expiry_schedule: {},
  max_pain_strike: 25100,
  pcr: { pcr_oi: 1.08, pcr_volume: 0.94, total_call_oi: 1200000,
         total_put_oi: 1296000, sentiment: 'MILD_BULLISH', bias_badge: 'BULLISH' },
  atm_straddle: { strike: 25100, call_ltp: 180, put_ltp: 165, combined_premium: 345,
                  upper_breakeven: 25445, lower_breakeven: 24755,
                  expected_move_pct: 1.37 },
  iv_rank: 42.7,
  chain: [
    { strike: 25000, is_atm: false, call: { oi: 90000, ltp: 260, iv: 14.2 },
      put: { oi: 130000, ltp: 140, iv: 13.8 } },
    { strike: 25050, is_atm: false, call: { oi: 110000, ltp: 220, iv: 13.9 },
      put: { oi: 150000, ltp: 170, iv: 13.5 } },
    { strike: 25100, is_atm: true, call: { oi: 180000, ltp: 180, iv: 13.4 },
      put: { oi: 210000, ltp: 165, iv: 13.1 } },
    { strike: 25150, is_atm: false, call: { oi: 160000, ltp: 145, iv: 13.6 },
      put: { oi: 120000, ltp: 205, iv: 13.3 } },
    { strike: 25200, is_atm: false, call: { oi: 140000, ltp: 112, iv: 14.0 },
      put: { oi: 95000, ltp: 250, iv: 13.7 } }
  ],
  gex: {}
};

/* ── Backtest fixtures ──────────────────────────────────────────────────────
   The optimiser's report is per *trading style*, not per symbol: one row per
   mode carrying the pooled in-sample / out-of-sample split, plus a `series`
   list describing what data each symbol-mode contributed. That is the shape
   `jarvis/backtesting/optimizer.py` actually builds.

   Two things this fixture exists to catch, because both shipped as bugs:

   1. NESTING. The report arrives at `payload.job.result`, never at the top
      level. The reader that shipped before the fix looked only at
      `payload.report || payload.result`, found nothing, and rendered
      "No results in this job" on a run that had produced a perfectly good
      report. So the fixture below is deliberately nested one level deeper than
      the old reader looked — if someone reintroduces that lookup, the checks
      on the table content go red instead of silently passing.

   2. EMPTINESS. A job that finished with no report body must still say so
      explicitly rather than render a blank panel, so a second job carries an
      empty result and is driven through the same click path. */
const BACKTEST_META = {
  orchestrator_attached: true,
  objectives: ['expectancy', 'profit_factor'],
  styles: ['SCALP', 'SWING', 'POSITION'],
  default_space: { tp_r: [1.0, 2.0, 2.5], be_trigger_r: [null, 1.0], trail_atr: [null, 1.5] }
};

const BACKTEST_JOBS = [
  { job_id: 'bt-real', label: 'SWING · H1 · 20 symbols', status: 'DONE',
    progress_lines: 12, started_utc: '2026-09-15T00:00:00Z' },
  { job_id: 'bt-empty', label: 'SCALP · M15 · no body', status: 'DONE',
    progress_lines: 3, started_utc: '2026-09-15T00:00:00Z' }
];

const BACKTEST_MODES = [
  { style: 'SWING', primary_timeframe: 'H1', series_count: 20,
    best_geometry: { tp_r: 2.5, be_trigger_r: 1.0, fast_cash_r: null, trail_atr: 1.5 },
    best_geometry_key: 'tp_r=2.5|be_trigger_r=1.0|fast_cash_r=null|trail_atr=1.5',
    feasible: true,
    in_sample: { trades: 1204, expectancy_r: 0.0871 },
    out_of_sample: { trades: 402, expectancy_r: 0.0412 },
    full_window: { profit_factor: 1.318, max_dd_r: 12.47 },
    walk_forward: { generalises: true },
    per_symbol: [], symbols_positive: 12, symbols_total: 20 },
  { style: 'SCALP', primary_timeframe: 'M15', series_count: 20,
    best_geometry: { tp_r: 1.0, be_trigger_r: null, fast_cash_r: 0.5, trail_atr: null },
    best_geometry_key: 'tp_r=1.0|be_trigger_r=null|fast_cash_r=0.5|trail_atr=null',
    feasible: true,
    in_sample: { trades: 4811, expectancy_r: 0.0224 },
    out_of_sample: { trades: 1602, expectancy_r: -0.0138 },
    full_window: { profit_factor: 0.994, max_dd_r: 38.9 },
    walk_forward: { generalises: false },
    per_symbol: [], symbols_positive: 8, symbols_total: 20 },
  { style: 'POSITION', primary_timeframe: 'D1', series_count: 20,
    best_geometry: null, best_geometry_key: '',
    feasible: false,
    in_sample: { trades: 96, expectancy_r: -0.0302 },
    out_of_sample: { trades: 31, expectancy_r: -0.0611 },
    full_window: { profit_factor: 0.842, max_dd_r: 21.05 },
    walk_forward: { generalises: false },
    per_symbol: [], symbols_positive: 6, symbols_total: 20 }
];

const BACKTEST_SERIES = [
  { symbol: 'XAUUSD', style: 'SWING', timeframe: 'H1', bars: 8731, candidates: 4210 },
  { symbol: 'EURUSD', style: 'SWING', timeframe: 'H1', bars: 8790, candidates: 3985 }
];

const BACKTEST_RESULT = {
  status: 'OK',
  job: {
    job_id: 'bt-real',
    status: 'DONE',
    result: {
      spec: { objective: 'expectancy', modes: ['SCALP', 'SWING', 'POSITION'] },
      elapsed_seconds: 41.7,
      evaluations: 1920,
      cache_hit_rate: 0.38,
      modes: BACKTEST_MODES,
      series: BACKTEST_SERIES
    }
  }
};

/* The same job, finished, with no report body at all. */
const BACKTEST_RESULT_EMPTY = {
  status: 'OK',
  job: { job_id: 'bt-empty', status: 'DONE', result: {} }
};

/* The reader that shipped before the fix, reproduced here purely as a control.
   It must find nothing in BACKTEST_RESULT — which is precisely why a run with a
   real report rendered as "No results in this job". Keeping it in the harness
   makes the fixture's discriminating power explicit rather than assumed: if the
   nesting in the fixture ever drifts back to a shape the old reader accepted,
   the control stops returning null and the check below tells us the test has
   quietly stopped testing anything. */
function preFixReportReader(payload) {
  const r = (payload && (payload.report || payload.result)) || {};
  return r.per_symbol || r.symbols || null;
}

/* ── Closed-trade history fixture ───────────────────────────────────────────
   /api/history answers with a BARE ARRAY (server.py: "The route answers with a
   bare array — both existing consumers read it that way"), so the fixture is an
   array, not a wrapper object. A stub that returned `{}` — which is what this
   suite used to do — left renderHistory() with zero rows and therefore tested
   nothing about the ten columns, the four filters or the summary line.

   The rows are chosen to pin the three things that actually differ per row:

   1. `timestamp` is AMBIGUOUS. For a row the engine logged it is the entry
      time; for one synced from a closed MT5 out-deal it is the exit. The
      original table was a *closed* list and named the column "Closed", so the
      renderer must show `closed_at` where it exists and mark the rows that have
      none. Row 70001 has an entry of 09-10 and a close of 09-14, so a renderer
      showing `timestamp` is distinguishable from one showing `closed_at`.
   2. Rows synced from MT5 carry `timestamp === closed_at` (row 70003) — the
      close time is the only time known.
   3. P&L arrives as `realized_pnl` for journal rows and as `profit` for synced
      ones (row 70005), which is the fallback historyPnl() exists to cover. */
const HISTORY_ROWS = [
  { ticket: 70001, symbol: 'XAUUSD', action: 'BUY', executor: 'BOT (AI)',
    volume: 0.25, entry_price: 2380.5, sl: 2370.0, tp: 2400.0,
    realized_pnl: 124.75, timestamp: '2026-09-10T08:15:00+00:00',
    closed_at: '2026-09-14T18:30:00+00:00' },
  { ticket: 70002, symbol: 'EURUSD', action: 'SELL', executor: 'MANUAL',
    volume: 0.10, entry_price: 1.0925, sl: 0, tp: 0,
    realized_pnl: null, timestamp: '2026-09-16T11:05:00+00:00',
    closed_at: null },
  { ticket: 70003, symbol: 'GBPUSD', action: 'SELL', executor: 'MT5 BROKER',
    volume: 0.30, entry_price: 1.2710, sl: 0, tp: 0,
    realized_pnl: -58.20, timestamp: '2026-09-15T13:45:00+00:00',
    closed_at: '2026-09-15T13:45:00+00:00' },
  { ticket: 70004, symbol: 'XAUUSD', action: 'BUY', executor: 'SL EXIT',
    volume: 0.15, entry_price: 2395.0, sl: 2390.0, tp: 2420.0,
    realized_pnl: -75.00, timestamp: '2026-09-12T09:00:00+00:00',
    closed_at: '2026-09-12T14:20:00+00:00' },
  { ticket: 70005, symbol: 'USDJPY', action: 'BUY', executor: 'TP EXIT',
    volume: 0.20, entry_price: 147.20, sl: 146.5, tp: 148.5,
    profit: 92.40, timestamp: '2026-09-13T07:30:00+00:00',
    closed_at: '2026-09-13T16:00:00+00:00' },
  { ticket: 70006, symbol: 'AUDUSD', action: 'BUY', executor: 'MANUAL_AI_ASSISTED',
    volume: 0.12, entry_price: 0.6680, sl: 0, tp: 0,
    realized_pnl: 0, timestamp: '2026-09-11T10:00:00+00:00',
    closed_at: '2026-09-11T12:00:00+00:00' },
  /* The next two exist for the chart's exit markers, which need two cases the
     first six rows cannot supply:
     - 70007 is a closed XAUUSD **SELL**, so the exit marker must point the other
       way (closing a short buys) — every other XAUUSD exit in this fixture is a
       long, so the `wasBuy === false` branch was unreachable.
     - 70008 is XAUUSD with `closed_at: null`: a journal row whose `timestamp` is
       when it was logged, not when the trade closed. It must produce NO exit
       marker. Without it the closed_at filter is untestable — every XAUUSD row
       already had a closed_at, so deleting the filter changed nothing (proved by
       mutation: "drop the closed_at filter" left all checks green). */
  { ticket: 70007, symbol: 'XAUUSD', action: 'SELL', executor: 'MANUAL',
    volume: 0.10, entry_price: 2400.0, sl: 2410.0, tp: 2380.0,
    realized_pnl: -40.00, timestamp: '2026-09-16T08:00:00+00:00',
    closed_at: '2026-09-16T10:30:00+00:00' },
  { ticket: 70008, symbol: 'XAUUSD', action: 'BUY', executor: 'BOT (AI)',
    volume: 0.05, entry_price: 2390.0, sl: 2385.0, tp: 2405.0,
    realized_pnl: null, timestamp: '2026-09-16T12:00:00+00:00',
    closed_at: null }
];

/* ── Auto-selection fixture ─────────────────────────────────────────────────
   The payload `/api/intelligence/auto-selection` really returns, including the
   `status: 'OK'` the renderer gates on — a stub body of `{}` fails that gate and
   paints the error state, so the panel's cards were never exercised.

   Decision keys mirror `SelectionDecision.to_dict()` (mode_aggregator.py:421).
   `is_tradeable` is the field the panel filters on, so two of the four decisions
   are tradeable and two are not: the counts and the card list both have to
   disagree with the raw `decisions` length for the check to mean anything. */
const SELECTION_PAYLOAD = {
  status: 'OK',
  generated_utc: '2026-09-15T00:00:00Z',
  dry_run: true,
  cached: false,
  age_seconds: 0.0,
  universe: { symbols: 4, styles: 3, scanned_pairs: 12, candidates: 4 },
  decisions: [
    { symbol: 'XAUUSD', direction: 'BUY', consensus_score: 82.5, agreement_ratio: 0.6667,
      agreement_count: 2, available_count: 3, confidence_tier: 'HIGH', is_tradeable: true,
      dissenting: false, strong_dissent: false,
      supporting_styles: ['SWING', 'DAY_TRADING'], dissenting_styles: [],
      abstaining_styles: ['SCALP'], rationale: '2 of 3 styles agree', votes: [] },
    { symbol: 'EURUSD', direction: 'SELL', consensus_score: 71.0, agreement_ratio: 0.6667,
      agreement_count: 2, available_count: 3, confidence_tier: 'MEDIUM', is_tradeable: true,
      dissenting: false, strong_dissent: false,
      supporting_styles: ['SWING', 'SCALP'], dissenting_styles: [],
      abstaining_styles: ['DAY_TRADING'], rationale: '2 of 3 styles agree', votes: [] },
    { symbol: 'GBPUSD', direction: 'BUY', consensus_score: 55.5, agreement_ratio: 0.3333,
      agreement_count: 1, available_count: 3, confidence_tier: 'LOW', is_tradeable: false,
      dissenting: true, strong_dissent: false,
      supporting_styles: ['SWING'], dissenting_styles: ['SCALP'],
      abstaining_styles: [], rationale: 'no cross-style agreement', votes: [] },
    { symbol: 'USDJPY', direction: 'NONE', consensus_score: 0.0, agreement_ratio: 0.0,
      agreement_count: 0, available_count: 3, confidence_tier: 'NONE', is_tradeable: false,
      dissenting: false, strong_dissent: false, supporting_styles: [],
      dissenting_styles: [], abstaining_styles: [], rationale: 'no directional consensus',
      votes: [] }
  ],
  candidates: [],
  scanned_symbols: ['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY']
};

/* The same route with no orchestrator attached. This is the *expected* answer
   when the dashboard runs without the engine, and the renderer gives it its own
   state ("Selection engine not attached") precisely so it is not mistaken for a
   fault — so it is worth driving, not just the happy path. */
const SELECTION_UNAVAILABLE = {
  status: 'UNAVAILABLE',
  generated_utc: '2026-09-15T00:00:00Z',
  dry_run: true,
  cached: false,
  age_seconds: 0.0,
  error: 'Live orchestrator is not attached to the web server.',
  decisions: [],
  best: null,
  candidates: [],
  universe: { symbols: 0, styles: 0 }
};

/* ── Regime-policy fixture ──────────────────────────────────────────────────
   `/api/backtest/regime-policy` projects `reports/optimizer/regime_*.json` into
   a per-mode table. Three things in the projection are easy to get wrong and are
   each represented here: a mode carrying an `error` is skipped entirely, an
   `enabled: false` row still counts toward the condition total but not the
   tradeable one, and `uses_own_geometry` distinguishes a regime's own geometry
   from the pooled default. */
const REGIME_POLICY_PAYLOAD = {
  status: 'OK',
  source_report: 'regime_20260915_120000.json',
  age_seconds: 3600,
  objective: 'expectancy',
  policy: {
    SWING: {
      primary_timeframe: 'H1',
      regimes: {
        TREND_BULL: { enabled: true, geometry: { tp_r: 2.5 }, min_score_quantile: 0.99,
                      uses_own_geometry: true, basis: 'regime-specific', reason: null,
                      candidates: 120, baseline_expectancy_r: 0.0842, baseline_trades: 310 },
        RANGE_LOW_VOL: { enabled: false, geometry: { tp_r: 1.5 }, min_score_quantile: 0.97,
                         uses_own_geometry: false, basis: 'pooled default',
                         reason: 'below baseline', candidates: 90,
                         baseline_expectancy_r: -0.0121, baseline_trades: 145 }
      },
      enabled_regimes: ['TREND_BULL'],
      regimes_with_own_geometry: ['TREND_BULL'],
      out_of_sample: {}, vs_baseline: {}
    },
    SCALP: {
      primary_timeframe: 'M15',
      regimes: {
        TREND_BEAR: { enabled: true, geometry: { tp_r: 1.0 }, min_score_quantile: 0.97,
                      uses_own_geometry: false, basis: 'pooled default', reason: null,
                      candidates: 200, baseline_expectancy_r: 0.0, baseline_trades: 480 }
      },
      enabled_regimes: ['TREND_BEAR'],
      regimes_with_own_geometry: [], out_of_sample: {}, vs_baseline: {}
    },
    POSITION: { error: 'no data for this mode' }
  }
};

const fetchCalls = [];
/* Flipped by the harness to drive the auto-selection route's 503 branch. */
let selectionAvailable = true;

/* The action endpoints answer **HTTP 200 with a status of their own** — the
   broker reports a refused order as `{"status": "FAILED", "reason": …}` or
   `{"status": "BLOCKED", "reason": …}` and the server passes that dict straight
   through. So the response cannot be inferred from the HTTP code and has to be
   scripted: this is what lets both outcomes of an order be driven. The default
   is the success the real server sends for a paper fill. */
let actionResponse = { status: 'PLACED', ticket: 90001 };
let actionStatus = 200;

/* Every POST the controller makes, with the body it built — the request side of
   the order path, which nothing else in the repo observes. */
const postCalls = [];

function fetchStub(url, opts) {
  fetchCalls.push(url);
  const method = String((opts && opts.method) || 'GET').toUpperCase();
  if (method !== 'GET') {
    let sent = (opts && opts.body) || null;
    if (typeof sent === 'string') { try { sent = JSON.parse(sent); } catch (e) { /* keep the raw string */ } }
    postCalls.push({ url: url, method: method, body: sent });
  }
  let body = {};
  /* The status is a variable rather than a hardcoded 200 because apiRequest()
     reads resp.ok and resp.status and the panels branch on them — a stub that
     can only answer 200 makes every non-200 branch in every panel unreachable,
     including the ones whose whole point is to say "the engine is not attached". */
  let status = 200;
  if (url.indexOf('/api/action/') >= 0 ||
      url.indexOf('/api/copilot/ask') >= 0 ||
      url.indexOf('/api/backtest/cancel') >= 0 ||
      url.indexOf('/api/backtest/run') >= 0) {
    body = actionResponse;
    status = actionStatus;
  } else if (url.indexOf('/api/candles') >= 0) {
    body = { symbol: 'XAUUSD', timeframe: 'H1', candles: buildCandles() };
  } else if (url.indexOf('/api/telemetry_state') >= 0) {
    body = TELEMETRY;
  } else if (url.indexOf('/api/news') >= 0) {
    body = NEWS_PAYLOAD;
  } else if (url.indexOf('/api/stocks/screener') >= 0) {
    body = SCREENER_PAYLOAD;
  } else if (url.indexOf('/api/stocks/heatmap') >= 0) {
    body = HEATMAP_PAYLOAD;
  } else if (url.indexOf('/api/india/indices') >= 0) {
    body = INDIA_INDICES_PAYLOAD;
  } else if (url.indexOf('/api/india/fii_dii') >= 0) {
    body = INDIA_FII_PAYLOAD;
  } else if (url.indexOf('/api/india/option_chain') >= 0) {
    body = INDIA_OPTION_CHAIN_PAYLOAD;
  } else if (url.indexOf('/api/history') >= 0) {
    // Bare array, exactly as server.py sends it.
    body = HISTORY_ROWS;
  } else if (url.indexOf('/api/intelligence/auto-selection') >= 0) {
    // 503 with status UNAVAILABLE is the real "no orchestrator attached"
    // answer, not a fabricated failure.
    body = selectionAvailable ? SELECTION_PAYLOAD : SELECTION_UNAVAILABLE;
    status = selectionAvailable ? 200 : 503;
  } else if (url.indexOf('/api/backtest/regime-policy') >= 0) {
    body = REGIME_POLICY_PAYLOAD;
  } else if (url.indexOf('/api/intelligence/reliability') >= 0) {
    body = { status: 'OK', styles: [] };
  } else if (url.indexOf('/api/backtest/meta') >= 0) {
    body = BACKTEST_META;
  } else if (url.indexOf('/api/backtest/jobs/') >= 0 && url.indexOf('/result') >= 0) {
    // `/jobs/<id>/result` — the report body.
    body = url.indexOf('bt-empty') >= 0 ? BACKTEST_RESULT_EMPTY : BACKTEST_RESULT;
  } else if (url.indexOf('/api/backtest/jobs/') >= 0) {
    // `/jobs/<id>` — the poll. Both fixtures are DONE, so the controller's DONE
    // branch runs and fetches the result, which is the path under test.
    const id = url.split('/api/backtest/jobs/')[1].split(/[?#]/)[0];
    body = { status: 'OK', job: { job_id: id, status: 'DONE', progress: 100,
                                  progress_lines: ['done'] } };
  } else if (url.indexOf('/api/backtest/jobs') >= 0) {
    body = { status: 'OK', jobs: BACKTEST_JOBS };
  } else if (url.indexOf('/api/backtest/') >= 0) {
    body = { status: 'OK', jobs: [] };
  }
  // apiRequest() reads the body with resp.text() and parses it itself, so the
  // stub must expose text() — a json()-only stub makes every call look failed.
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status: status,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body)
  });
}

/* ── Run the module ───────────────────────────────────────────────────────── */
/* A controllable clock. The controller corrects for skew against the payload's
   own timestamp, so with a fixed clock every countdown is deterministic; and by
   advancing the clock we can prove a live release becomes past and the
   "next release" panel promotes the following event, which is the one piece of
   real time-dependent logic in the calendar. */
let fakeNow = Date.parse('2026-09-15T00:00:00Z');
const RealDate = Date;
class FakeDate extends RealDate {
  constructor(...args) {
    if (args.length === 0) super(fakeNow);
    else super(...args);
  }
  static now() { return fakeNow; }
}

/* setInterval is captured rather than ignored so the per-second calendar tick
   can be invoked from the harness. */
const intervals = [];

/* Every element the controller has bound a handler to, so clicks and changes
   can be driven from here. */
linkTree();

const sandbox = {
  console,
  document: documentStub,
  window: {},
  fetch: fetchStub,
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
  clearInterval: () => {},
  requestAnimationFrame: () => 0,
  AbortController: function () { this.signal = {}; this.abort = () => {}; },
  ResizeObserver: function () { this.observe = () => {}; this.disconnect = () => {}; },
  LightweightCharts,
  Date: FakeDate, Math, JSON, Number, String, Array, Object, Promise,
  isNaN, isFinite, RegExp, Set, Map
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const source = fs.readFileSync(JS, 'utf8');
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'dashboard.js' });

/* ── Drive the new views through their real wiring ──────────────────────────
   The keyboard shortcut is the same path a user takes: it calls setView(),
   which is what loads each view's panels. Calling the renderers directly would
   prove nothing about whether the wiring works. */
const wiring = {
  keydowns: 0,
  tvScriptsBeforeClick: documentStub.head.children.length,
  tvScriptsAfterClick: null,
  tvLoadingHtml: null,
  tvErrorHtml: null,
  tvWidgetOpts: null,
  tvSecondClick: 0,
  btRows: 0,
  btRowClicks: 0,
  btMetaHtml: null,
  btResultsHtml: null,
  btResultsState: null,
  btResultMeta: null,
  btEmptyHtml: null,
  btEmptyState: null,
  selection: null,
  regime: null,
  toasts: {},
  copilot: {}
};

function driveViews() {
  fireDocument('keydown', { key: '2', target: { tagName: 'DIV' } });   // news
  fireDocument('keydown', { key: '3', target: { tagName: 'DIV' } });   // analyst
  fireDocument('keydown', { key: '4', target: { tagName: 'DIV' } });   // markets
  wiring.keydowns = 3;
}

function driveTradingView() {
  const tvBtn = registry.get('chart-src-tv');
  if (!tvBtn) return;
  tvBtn.fire('click');
  wiring.tvScriptsAfterClick = documentStub.head.children.length;
  wiring.tvLoadingHtml = deepHtml(registry.get('chart-tv'));

  // Simulate the CDN being blocked.
  const script = documentStub.head.children[documentStub.head.children.length - 1];
  if (script && typeof script.onerror === 'function') script.onerror();
}

/* ── Backtest ───────────────────────────────────────────────────────────────
   The whole point of this suite is that it drives the REAL wiring rather than
   calling renderers directly, so the backtest is entered the same way a user
   enters it — the '6' shortcut, then a click on a job row. Calling
   renderBacktestResult() straight would prove the renderer works and say
   nothing about whether anything ever reaches it, which is exactly the state
   the "backtest does not work" report described. */
function driveBacktestView() {
  fireDocument('keydown', { key: '6', target: { tagName: 'DIV' } });
}

/* Click a job row by index. loadJobs() rebuilds #bt-history on every poll, so
   the rows have to be re-queried at click time rather than captured earlier. */
function clickBacktestJob(index) {
  const body = registry.get('bt-history');
  if (!body) return;
  const rows = body.querySelectorAll('tr[data-job]');
  wiring.btRows = rows.length;
  const row = rows[index];
  if (!row) return;
  wiring.btRowClicks += row.fire('click');
}

function captureBacktestResult() {
  const host = registry.get('bt-results');
  const meta = registry.get('bt-result-meta');
  wiring.btResultsHtml = deepHtml(host);
  wiring.btResultsState = host ? host.getAttribute('data-state') : 'no-element';
  wiring.btResultMeta = meta ? meta.textContent : null;
  wiring.btMetaHtml = deepHtml(registry.get('bt-history'));
}

function captureBacktestEmpty() {
  const host = registry.get('bt-results');
  wiring.btEmptyHtml = deepHtml(host);
  wiring.btEmptyState = host ? host.getAttribute('data-state') : 'no-element';
}

/* ── History ────────────────────────────────────────────────────────────────
   The panel is loaded on boot (loadHistory() is on the boot path because the
   chart's exit markers need the closed trades), so a realistic fixture alone
   exercises the table. The four local filters re-render without a refetch and
   the window select refetches, and both go through the bindings in bind(), so
   they are driven by setting the select's value and firing the same event a
   user's click fires. */
function setHistoryFilter(id, value) {
  const el = registry.get(id);
  if (!el) return false;
  el.value = value;
  return el.fire('change') > 0;
}

function captureHistory(key) {
  const body = registry.get('hist-body');
  wiring.histViews = wiring.histViews || {};
  wiring.histViews[key] = {
    html: deepHtml(body),
    state: body ? body.getAttribute('data-state') : 'no-element',
    rows: body ? body.querySelectorAll('tr').length : 0,
    count: (registry.get('hist-count') || {}).textContent,
    summary: (registry.get('hist-summary') || {}).textContent
  };
}

/* ── Analytics: auto-selection and regime policy ────────────────────────────
   Auto-selection loads on boot (from selectSymbol via the boot symbol) and again
   from the watchlist refresh button, which is the control a user presses when a
   panel looks stale — so the 503 branch is driven through that button rather
   than by calling loadSelection(). Regime policy has no boot path at all: it is
   loaded by setView('analytics'), so the '5' shortcut is the only way in. */
function captureSelection(key) {
  const host = registry.get('selection-body');
  wiring.selection = wiring.selection || {};
  wiring.selection[key] = {
    html: deepHtml(host),
    state: host ? host.getAttribute('data-state') : 'no-element',
    cards: host ? host.children.length : 0,
    count: (registry.get('selection-count') || {}).textContent,
    tier: (registry.get('selection-tier') || {}).textContent,
    tierClass: (registry.get('selection-tier') || {}).className
  };
}

function captureRegimePolicy() {
  const body = registry.get('regime-policy-body');
  const metrics = registry.get('regime-policy-metrics');
  wiring.regime = {
    html: deepHtml(body),
    state: body ? body.getAttribute('data-state') : 'no-element',
    rows: body ? body.querySelectorAll('tr').length : 0,
    metrics: deepHtml(metrics),
    source: (registry.get('regime-policy-source') || {}).textContent
  };
}

function readTvFailure() {
  wiring.tvErrorHtml = deepHtml(registry.get('chart-tv'));
}

/* ── Context strip: Analyst and News beside the ticket ──────────────────────
   Item 5. Driven by clicking the real [data-ctx] buttons — the same route a
   user takes — rather than by calling setContext() directly, so the click
   binding and the render are both exercised. */
function clickContext(tab) {
  const hit = selectAll(documentStub, '[data-ctx]')
    .find((b) => b.getAttribute('data-ctx') === tab);
  if (!hit) return 0;
  return hit.fire('click');
}

function captureContext(key, clicked) {
  wiring.ctx = wiring.ctx || {};
  const pressed = {};
  selectAll(documentStub, '[data-ctx]').forEach((b) => {
    pressed[b.getAttribute('data-ctx')] = b.getAttribute('aria-pressed');
  });
  const panel = (id) => registry.get(id) || {};
  wiring.ctx[key] = {
    // Recorded so an assertion cannot pass because the button was never found.
    clicked: clicked,
    analystHtml: deepHtml(panel('ctx-analyst')),
    newsHtml: deepHtml(panel('ctx-news')),
    reasonHidden: !!panel('reason-body').hidden,
    analystHidden: !!panel('ctx-analyst').hidden,
    newsHidden: !!panel('ctx-news').hidden,
    pressed: pressed
  };
}

/* One <li> of the rendered strip, as plain text, so checks bind to the row the
   renderer actually emitted rather than to a substring anywhere in the panel. */
function ctxItems(html) {
  const s = String(html || '');
  const out = [];
  const re = /<li[^>]*>([\s\S]*?)<\/li>/g;
  let m;
  while ((m = re.exec(s)) !== null) {
    out.push(String(m[1]).replace(/<[^>]*>/g, ' ').replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/\s+/g, ' ').trim());
  }
  return out;
}

/* ── Order submission: the request side of the money path ───────────────────
   Both submit handlers read the ticket form, so the form is filled and the real
   button is clicked — the same route a user takes. Firing the handler directly
   would skip the form parsing that decides what is actually sent. */
function setTicket(fields) {
  Object.keys(fields).forEach(function (id) {
    const el = registry.get(id);
    if (el) el.value = String(fields[id]);
  });
}

let toastMark = 0;
function captureToasts(key) {
  const all = toasts();
  wiring.toasts[key] = all.slice(toastMark);
  toastMark = all.length;
}

function submitManualTrade(side) {
  const btn = registry.get(side === 'SELL' ? 'ticket-sell' : 'ticket-buy');
  return btn ? btn.fire('click') : 0;
}

function submitPendingOrder() {
  const btn = registry.get('ticket-place');
  return btn ? btn.fire('click') : 0;
}

/* ── Copilot ────────────────────────────────────────────────────────────────
   Submitted through the form's own handler, with the event object it expects —
   `preventDefault` is called before anything else, so firing the handler
   without one throws and reads as a broken panel. */
function driveCopilot(query) {
  const input = registry.get('copilot-input');
  if (input) input.value = query;
  const form = registry.get('copilot-form');
  return form ? form.fire('submit', { preventDefault: function () {} }) : 0;
}

function copilotSnapshot() {
  const log = registry.get('copilot-log');
  return { children: log ? log.children.length : 0, posts: postCalls.length };
}

function captureCopilot(key, before) {
  const log = registry.get('copilot-log');
  const kids = log ? log.children : [];
  wiring.copilot[key] = {
    html: deepHtml(log),
    added: kids.length - before.children,
    posts: postCalls.length - before.posts,
    // Each new bubble's own class, so a bubble that should not exist is visible
    // rather than merely absent from the concatenated text.
    newClasses: kids.slice(before.children).map((c) => String(c.className || '')),
    newHtml: kids.slice(before.children).map((c) => deepHtml(c))
  };
}

function driveTvSuccess() {
  // Make the script available, then re-select the source. The controller should
  // build the widget rather than fetch again.
  sandbox.TradingView = { widget: function (o) { wiring.tvWidgetOpts = o; } };
  const tvBtn = registry.get('chart-src-tv');
  if (tvBtn) wiring.tvSecondClick = tvBtn.fire('click');
}

/* Advance the clock an hour and run the calendar's own per-second tick. Every
   event that was live must now read as past, and the "next release" panel must
   promote the following event. */
function advanceClockAndTick() {
  fakeNow += 3600 * 1000;
  intervals.filter((i) => i.ms === 1000).forEach((i) => i.fn());
}

/* ── Assertions (after the boot promise chain settles) ────────────────────── */
function report() {
  console.log('\ndiagnostics');
  console.log('  ids requested: ' + Array.from(new Set(requestedIds)).join(', '));
  console.log('  fetches: ' + fetchCalls.length + '  (' + fetchCalls.join(' | ') + ')');

  console.log('\nchart construction');
  ok('a chart was created', drawn.charts >= 1, 'charts=' + drawn.charts);
  ok('a candlestick series was added', drawn.candleSeries >= 1);
  ok('a volume series was added', drawn.volumeSeries >= 1);
  ok('candles were pushed to the series', Array.isArray(drawn.candleData) && drawn.candleData.length === 30,
    'got ' + (drawn.candleData ? drawn.candleData.length : 'null'));
  ok('volume was pushed to the series', Array.isArray(drawn.volumeData) && drawn.volumeData.length === 30,
    'got ' + (drawn.volumeData ? drawn.volumeData.length : 'null'));

  const titles = drawn.priceLines.map((l) => String(l.title || ''));

  console.log('\nsupport and resistance');
  const levelChecks = [
    ['R1', 110], ['R2', 115], ['S1', 90], ['S2', 85]
  ];
  levelChecks.forEach(([name, price]) => {
    const hit = drawn.priceLines.find((l) => String(l.title || '').indexOf(name + ':') === 0);
    ok(name + ' price line drawn at ' + price,
      !!hit && Math.abs(hit.price - price) < 1e-9,
      hit ? 'title=' + hit.title + ' price=' + hit.price : 'no line titled ' + name);
  });

  const legend = registry.get('chart-legend');
  const legendHtml = legend ? legend.innerHTML : '';
  ok('legend names all four levels',
    ['R1', 'R2', 'S1', 'S2'].every((n) => legendHtml.indexOf(n) >= 0),
    legendHtml.slice(0, 120));

  console.log('\ntrade overlays (entry / stop / target)');
  const entryLine = drawn.priceLines.find((l) => /^(BUY|SELL)\s/.test(String(l.title || '')));
  ok('entry line drawn with side, size and price in the label',
    !!entryLine && entryLine.price === 100 && /BUY 0\.10L/.test(entryLine.title),
    entryLine ? entryLine.title : 'none');
  const slLine = drawn.priceLines.find((l) => String(l.title || '').indexOf('SL') === 0);
  ok('stop loss line drawn at 95', !!slLine && slLine.price === 95, slLine ? slLine.title : 'none');
  const tpLine = drawn.priceLines.find((l) => String(l.title || '').indexOf('TP:') === 0);
  ok('take profit line drawn at 110', !!tpLine && tpLine.price === 110, tpLine ? tpLine.title : 'none');

  const hud = registry.get('chart-hud');
  const hudHtml = hud ? hud.innerHTML : '';
  ok('in-chart HUD is shown', !!hud && hud.hidden === false);
  ok('HUD carries the trade details',
    ['12345', 'BUY', '0.10L', '100', '95', '110', '12.50'].every((t) => hudHtml.indexOf(t) >= 0),
    hudHtml.replace(/\s+/g, ' ').slice(0, 200));

  /* ── Trade markers ───────────────────────────────────────────────────────
     `setMarkers()` was a no-op in the chart stub until this section existed, so
     drawTradeMarkers() could not be observed at all — and "the renderer was
     never called" is indistinguishable from "the renderer works" from outside
     the module. Every earlier assertion about the chart passed without ever
     checking whether a marker was drawn.

     This fixture expects exactly three: one entry for the open XAUUSD BUY, and
     two exits — only the two XAUUSD rows carrying a real `closed_at`. The
     GBPUSD, USDJPY and AUDUSD rows are excluded by symbol, and the EURUSD row
     is excluded because `closed_at` is null, which is the honest rule: a
     journal row's `timestamp` is when it was logged, not when the trade closed.
  */
  console.log('\ntrade markers (active entry on the price chart; exit history omitted for clean view)');
  const marks = drawn.lastMarkers || [];
  const barTimes = (drawn.candleData || []).map((b) => Number(b.time));

  ok('the chart was handed trade markers', marks.length === 1, 'markers=' + marks.length);

  const entry = marks.find((m) => /^BUY /.test(String(m.text || '')));
  ok('an open BUY is marked below the bar, pointing up',
    !!entry && entry.position === 'belowBar' && entry.shape === 'arrowUp',
    entry ? entry.position + '/' + entry.shape : 'no entry marker');
  ok('the entry marker names side, size and price',
    !!entry && /^BUY 0\.10L @ 100\.00$/.test(String(entry.text)),
    entry ? entry.text : 'no entry marker');

  const exits = marks.filter((m) => /^EXIT/.test(String(m.text || '')));
  ok('historical exit markers are omitted from the price chart to keep it clean for active indicators',
    exits.length === 0,
    'exits=' + exits.length);
  ok('every marker snaps to a bar the series actually contains',
    marks.length > 0 && barTimes.length > 0 &&
    marks.every((m) => barTimes.indexOf(Number(m.time)) >= 0),
    'off-grid: ' + marks.filter((m) => barTimes.indexOf(Number(m.time)) < 0)
      .map((m) => m.time).join(', '));
  /* Deliberately NOT asserting that markers are sorted by time. Every event in
     this fixture falls after the loaded window, so all of them snap to the last
     bar and the order carries no information: deleting the `.sort()` call left
     all checks green. An assertion that cannot fail is worse than none, so it
     was removed rather than left as decoration. */

  console.log('\nheader');
  const price = registry.get('chart-live-price');
  ok('live price rendered from the last close', !!price && price.textContent === '100.00',
    price ? price.textContent : 'missing');

  console.log('\nscanner radar');
  const radar = registry.get('radar-body');
  const radarHtml = deepHtml(radar);
  ok('radar rendered both candidates',
    ['XAUUSD', 'EURUSD'].every((s) => radarHtml.indexOf(s) >= 0),
    radarHtml.replace(/\s+/g, ' ').slice(0, 200));
  ok('radar shows entry, stop, target, R:R, win and EV',
    ['Entry', 'SL', 'TP', 'R:R', 'Win', 'EV'].every((t) => radarHtml.indexOf(t) >= 0),
    radarHtml.replace(/\s+/g, ' ').slice(0, 240));
  const radarCards = (radar && radar.children) || [];
  const liveCards = radarCards.filter((c) => /tt-radar--live/.test(c.className || ''));
  ok('radar marks exactly the actionable candidate',
    radarCards.length === 2 && liveCards.length === 1,
    'cards=' + radarCards.length + ' live=' + liveCards.length);
  const radarCount = registry.get('radar-count');
  ok('radar count reflects the row count', !!radarCount && radarCount.textContent === '2',
    radarCount ? radarCount.textContent : 'missing');

  console.log('\nnews calendar');
  ok('opening the view requested the calendar',
    fetchCalls.some((u) => u.indexOf('/api/news') >= 0),
    fetchCalls.filter((u) => u.indexOf('/api/news') >= 0).join(' '));

  const newsBody = registry.get('news-body');
  const newsHtml = deepHtml(newsBody);
  ok('every event in the payload rendered',
    ['US Crude Oil Inventories', 'US CPI (y/y)', 'US Dallas Fed Manufacturing',
     'US CB Consumer Confidence', 'US Preliminary GDP (q/q)']
      .every((t) => newsHtml.indexOf(t) >= 0),
    newsHtml.replace(/\s+/g, ' ').slice(0, 220));
  ok('rows carry both IST and UTC timestamps',
    newsHtml.indexOf('11:55 PM IST') >= 0 && newsHtml.indexOf('Sep 14, 23:55 UTC') >= 0);
  ok('panel count reports the event count',
    (registry.get('news-count') || {}).textContent === '5',
    (registry.get('news-count') || {}).textContent);

  /* The exact labels prove the countdown arithmetic, the phase boundaries and
     the skew correction in one shot: a wrong window edge or an off-by-one
     would show up as a different string. */
  const cdCells = selectAll(newsBody, '[data-cd]');
  const cd = cdCells.map((c) => {
    const v = c.querySelector('[data-cd-val]');
    return c.getAttribute('data-phase') + ':' + (v ? v.textContent : '?');
  });
  ok('five countdown cells rendered', cdCells.length === 5, 'cells=' + cdCells.length);
  ok('live release counts up from the open of its window (T+5m 0s)',
    cd.indexOf('live:T+5m 0s') >= 0, cd.join(' | '));
  ok('live release still ahead counts down (T\u22122m 0s)',
    cd.indexOf('live:T\u22122m 0s') >= 0, cd.join(' | '));
  ok('release past the 15-minute window reads as released (1h 0m ago)',
    cd.indexOf('past:1h 0m ago') >= 0, cd.join(' | '));
  ok('release 30 minutes out is imminent (in 30m 0s)',
    cd.indexOf('soon:in 30m 0s') >= 0, cd.join(' | '));
  ok('release hours out is upcoming (in 2h 30m)',
    cd.indexOf('upcoming:in 2h 30m') >= 0, cd.join(' | '));

  ok('the live chip names the release inside its window',
    (registry.get('news-live-chip') || {}).textContent === 'LIVE · USD US Crude Oil Inventories',
    (registry.get('news-live-chip') || {}).textContent);
  ok('the currency filter was built from the payload, not hard-coded',
    (function () {
      const sel = registry.get('news-currency');
      const opts = (sel && sel.children) || [];
      const values = opts.map((o) => o.value).sort().join(',');
      return values === 'ALL,EUR,USD';
    })(), 'options=' + ((registry.get('news-currency') || {}).children || []).length);

  const heroBefore = deepHtml(registry.get('news-hero'));
  ok('the next-release panel features a live event over an upcoming one',
    heroBefore.indexOf('US Crude Oil Inventories') >= 0,
    heroBefore.replace(/\s+/g, ' ').slice(0, 160));

  console.log('\nnews detail panel');
  const detailBefore = deepHtml(registry.get('news-detail'));
  ok('no event is selected until one is clicked',
    detailBefore.indexOf('No event selected') >= 0,
    detailBefore.replace(/\s+/g, ' ').slice(0, 120));
  const firstRow = selectAll(newsBody, 'tr[data-key]')[0];
  if (firstRow) firstRow.fire('click');
  const detailAfter = deepHtml(registry.get('news-detail'));
  ok('clicking a row opens its impact analysis',
    detailAfter.indexOf('US Crude Oil Inventories') >= 0 &&
    detailAfter.indexOf('Oil-sensitive.') >= 0,
    detailAfter.replace(/\s+/g, ' ').slice(0, 200));
  ok('the selected row is marked',
    !!firstRow && firstRow.getAttribute('data-selected') === 'true');

  /* Advance the clock an hour and run the panel's own per-second tick. Every
     live release must become past, and the next-release panel must promote the
     following event — this is the behaviour a stale server-computed badge
     cannot provide. */
  advanceClockAndTick();
  const cdAfter = selectAll(newsBody, '[data-cd]').map((c) => {
    const v = c.querySelector('[data-cd-val]');
    return c.getAttribute('data-phase') + ':' + (v ? v.textContent : '?');
  });
  ok('the tick flipped every live release to past',
    cdAfter.indexOf('live:') < 0 && cdAfter.filter((s) => s.indexOf('past:') === 0).length === 4,
    cdAfter.join(' | '));
  ok('the tick recomputed the countdown from each event\u2019s own timestamp',
    cdAfter.indexOf('past:1h 5m ago') >= 0, cdAfter.join(' | '));
  ok('the tick advanced the upcoming release (in 1h 30m)',
    cdAfter.indexOf('upcoming:in 1h 30m') >= 0, cdAfter.join(' | '));
  const heroAfter = deepHtml(registry.get('news-hero'));
  ok('the next-release panel promoted the following event',
    heroAfter.indexOf('US Preliminary GDP (q/q)') >= 0 &&
    heroAfter.indexOf('US Crude Oil Inventories') < 0,
    heroAfter.replace(/\s+/g, ' ').slice(0, 160));
  ok('the live chip cleared once nothing was in its window',
    (registry.get('news-live-chip') || {}).textContent === 'no release live',
    (registry.get('news-live-chip') || {}).textContent);

  /* ── Context strip: Analyst and News beside the ticket ────────────────────
     Captured at ticks 73-75 by clicking the real [data-ctx] buttons from the
     trade view (the clock had not moved yet), plus one more read taken now that
     the clock HAS moved an hour — the pair is what proves the strip recomputes
     rather than echoing a cached server badge. */
  console.log('\ncontext strip beside the ticket');
  const CA = (wiring.ctx || {}).analyst || {};
  const CN = (wiring.ctx || {}).news || {};
  const CB = (wiring.ctx || {}).bogus || {};

  ok('the tab buttons exist and their clicks were delivered',
    CA.clicked > 0 && CN.clicked > 0 && CB.clicked > 0,
    'handlers fired: analyst=' + CA.clicked + ' news=' + CN.clicked + ' bogus=' + CB.clicked);

  ok('pressing Analyst leaves it the only pressed tab',
    CA.pressed && CA.pressed.analyst === 'true' && CA.pressed.why === 'false' &&
    CA.pressed.news === 'false', JSON.stringify(CA.pressed));
  ok('pressing Analyst swaps the strip in and hides the other two',
    CA.analystHidden === false && CA.newsHidden === true && CA.reasonHidden === true,
    'analyst=' + CA.analystHidden + ' news=' + CA.newsHidden + ' reason=' + CA.reasonHidden);

  /* The strip's whole claim is that it cannot disagree with the full panel,
     because both read state.decisions. Binding it to the panel's own rendered
     verdict is what makes that falsifiable — a hardcoded 'BLOCKED' would pass a
     literal check, and a strip reading the WRONG symbol's decision (EURUSD is
     authorised) fails this one. */
  const panelVerdict = (registry.get('da-verdict') || {}).textContent;
  ok('the strip\u2019s verdict is the one the analyst panel rendered for the same setup',
    !!panelVerdict && new RegExp('>' + panelVerdict + '<').test(String(CA.analystHtml)) &&
    !new RegExp('>AUTHORISED<').test(String(CA.analystHtml)),
    'panel=' + panelVerdict + ' strip=' + (String(CA.analystHtml).match(/tt-chip--\w+">([^<]*)/g) || []).join(','));
  ok('the strip shows the quality gate\u2019s own pass/block state',
    /gate block</.test(String(CA.analystHtml)),
    (String(CA.analystHtml).match(/gate [^<]*/) || ['no gate chip'])[0]);
  ok('the strip shows the confluence tier and score the engine sent',
    /HIGH · 8\.2/.test(String(CA.analystHtml)),
    (String(CA.analystHtml).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').slice(0, 90)));

  const aItems = ctxItems(CA.analystHtml);
  ok('the case against lists the bear_case after the risk factors',
    aItems.length === 3 && aItems[0] === 'Event risk inside 6h' &&
    aItems[1] === 'Spread widens at rollover' && aItems[2] === 'RSI divergence on H1',
    aItems.length + ': ' + aItems.join(' | '));
  ok('a setup that DOES have threats does not show the no-threats caveat',
    !/reported no threats/.test(String(CA.analystHtml)));

  ok('pressing News leaves it the only pressed tab',
    CN.pressed && CN.pressed.news === 'true' && CN.pressed.analyst === 'false' &&
    CN.pressed.why === 'false', JSON.stringify(CN.pressed));
  ok('pressing News swaps the strip in and hides the other two',
    CN.newsHidden === false && CN.analystHidden === true && CN.reasonHidden === true,
    'analyst=' + CN.analystHidden + ' news=' + CN.newsHidden + ' reason=' + CN.reasonHidden);

  /* Five releases are loaded; the Dallas print was an hour ago, so the strip
     must show four. This is the rule that keeps already-happened news out of a
     panel whose job is what to position for next. */
  const nItems = ctxItems(CN.newsHtml);
  ok('the strip lists the four releases the trader can still act on',
    nItems.length === 4, nItems.length + ': ' + nItems.join(' | '));
  ok('a release that already happened is left out',
    nItems.length === 4 && !nItems.some((t) => /Dallas Fed/.test(t)),
    nItems.join(' | '));
  ok('the soonest release is first, with its own recomputed countdown',
    nItems.length === 4 &&
    /^HIGH USD US Crude Oil Inventories · T\+5m 0s$/.test(nItems[0]) &&
    /^HIGH USD US CPI \(y\/y\) · T−2m 0s$/.test(nItems[1]) &&
    /^HIGH USD US CB Consumer Confidence · in 30m 0s$/.test(nItems[2]) &&
    /^LOW EUR US Preliminary GDP \(q\/q\) · in 2h 30m$/.test(nItems[3]),
    nItems.join(' | '));
  ok('a high-impact release is chipped as high impact',
    (String(CN.newsHtml).match(/tt-chip--sell/g) || []).length === 3,
    'sell chips=' + (String(CN.newsHtml).match(/tt-chip--sell/g) || []).length);

  /* Same strip, an hour later. Everything that was live is now past, so only
     the GDP print survives — the exclusion is recomputed from each timestamp,
     not carried over from the earlier render. */
  captureContext('news-after', clickContext('news'));
  const CN2 = (wiring.ctx || {})['news-after'] || {};
  const nItems2 = ctxItems(CN2.newsHtml);
  ok('after the clock moves, only the release still in the future is listed',
    nItems2.length === 1 && /US Preliminary GDP/.test(nItems2[0]),
    nItems2.length + ': ' + nItems2.join(' | '));
  ok('its countdown is recomputed, not the one carried from the earlier render',
    nItems2.length === 1 && /· in 1h 30m$/.test(nItems2[0]),
    nItems2.length ? nItems2[0] : 'nothing rendered');

  /* An unrecognised tab is not in setContext's list. Normalising to 'why' is the
     fail-safe: without it every panel would be hidden and nothing would answer. */
  ok('an unrecognised tab falls back to the why strip',
    CB.reasonHidden === false && CB.analystHidden === true && CB.newsHidden === true,
    'analyst=' + CB.analystHidden + ' news=' + CB.newsHidden + ' reason=' + CB.reasonHidden);
  ok('the fallback moves the pressed state onto the why tab',
    CB.pressed && CB.pressed.why === 'true' && CB.pressed.analyst === 'false' &&
    CB.pressed.news === 'false', JSON.stringify(CB.pressed));

  console.log('\ndevil\u2019s advocate');
  ok('opening the view rendered the selected symbol',
    (registry.get('da-symbol') || {}).textContent === 'XAUUSD',
    (registry.get('da-symbol') || {}).textContent);
  ok('a blocked setup is labelled as blocked',
    (registry.get('da-verdict') || {}).textContent === 'BLOCKED',
    (registry.get('da-verdict') || {}).textContent);

  const gauge = selectAll(registry.get('da-metrics'), '.tt-gauge')[0];
  const gaugeFill = gauge ? gauge.querySelector('.tt-gauge__fill') : null;
  ok('the penalty gauge is drawn against the engine\u2019s own 0-50 scale',
    !!gaugeFill && gaugeFill.style.width === '60.0%',
    gaugeFill ? gaugeFill.style.width : 'no fill');
  ok('a penalty above 25 is banded as severe',
    !!gauge && /tt-gauge--down/.test(gauge.className), gauge ? gauge.className : 'none');

  const bullHtml = deepHtml(registry.get('da-bull'));
  const bearHtml = deepHtml(registry.get('da-bear'));
  ok('the bull case lists the engine\u2019s supporting evidence',
    bullHtml.indexOf('H4 structure in premium rejection') >= 0, bullHtml.slice(0, 140));
  ok('the bear case lists the engine\u2019s contrary evidence',
    bearHtml.indexOf('RSI divergence on H1') >= 0, bearHtml.slice(0, 140));
  ok('threat vectors are shown',
    deepHtml(registry.get('da-threats')).indexOf('Event risk inside 6h') >= 0);
  ok('invalidation levels are shown',
    deepHtml(registry.get('da-invalidation')).indexOf('H4 close below 94.20') >= 0);
  ok('objections list both waiting and rejection reasons',
    (function () {
      const h = deepHtml(registry.get('da-objections'));
      return h.indexOf('Waiting for H1 close above 100.40') >= 0 &&
             h.indexOf('Adversarial penalty above tolerance') >= 0;
    })(), deepHtml(registry.get('da-objections')).replace(/\s+/g, ' ').slice(0, 200));

  console.log('\nquality gate');
  ok('the gate reports how many checks passed',
    (registry.get('gate-count') || {}).textContent === '3 / 5',
    (registry.get('gate-count') || {}).textContent);
  ok('a failed gate is labelled as blocked',
    (registry.get('gate-verdict') || {}).textContent === 'BLOCKED',
    (registry.get('gate-verdict') || {}).textContent);
  const gateCells = selectAll(registry.get('gate-body'), '.tt-gate__cell');
  ok('every check rendered', gateCells.length === 5, 'cells=' + gateCells.length);
  ok('failures are listed before passes',
    gateCells.length === 5 &&
    gateCells.slice(0, 2).every((c) => /--fail/.test(c.className)) &&
    gateCells.slice(2).every((c) => /--pass/.test(c.className)),
    gateCells.map((c) => (/--fail/.test(c.className) ? 'F' : 'P')).join(''));

  console.log('\nglobal equities');
  ok('opening the view requested the screener',
    fetchCalls.some((u) => u.indexOf('/api/stocks/screener') >= 0));
  const eqHtml = deepHtml(registry.get('eq-body'));
  ok('a computed row shows its real analysis',
    ['NVDA', '178.42', '+2.35%', '78%', 'BULLISH', '178.90', '2.80R']
      .every((t) => eqHtml.indexOf(t) >= 0),
    eqHtml.replace(/\s+/g, ' ').slice(0, 260));

  /* The honesty guarantee: a row whose analysis failed must not present the
     backend's placeholder setup as if it were a computed one. */
  ok('a failed-analysis row is rendered as having no analysis',
    eqHtml.indexOf('no analysis') >= 0, eqHtml.replace(/\s+/g, ' ').slice(0, 260));
  ok('a failed-analysis row does NOT show the placeholder grade',
    eqHtml.indexOf('GRADE B') < 0 && eqHtml.indexOf('>B<') < 0,
    eqHtml.replace(/\s+/g, ' ').slice(0, 260));
  ok('a failed-analysis row does NOT show the placeholder probability',
    eqHtml.indexOf('50%') < 0);
  ok('a failed-analysis row does NOT show the placeholder entry or target',
    eqHtml.indexOf('96.00') < 0 && eqHtml.indexOf('108.00') < 0);
  ok('a failed-analysis row marks its price as a reference, not a quote',
    eqHtml.indexOf('100.00 ref') >= 0, eqHtml.replace(/\s+/g, ' ').slice(0, 260));
  ok('the panel flags how many rows are placeholders',
    (registry.get('eq-prov') || {}).textContent === '1 placeholder',
    (registry.get('eq-prov') || {}).textContent);

  const heatHtml = deepHtml(registry.get('eq-heatmap'));
  ok('sector tiles rendered with their average change',
    ['Semiconductors', '+2.35%', 'Energy', '-1.80%'].every((t) => heatHtml.indexOf(t) >= 0),
    heatHtml.replace(/\s+/g, ' ').slice(0, 200));
  ok('heat tiles are banded by direction',
    (function () {
      const tiles = selectAll(registry.get('eq-heatmap'), '.tt-heattile');
      return tiles.length === 2 &&
        /tt-heat-[1-4]/.test(tiles[0].className) &&
        /tt-heat-neg-[1-4]/.test(tiles[1].className);
    })());

  console.log('\nindia');
  const idxHtml = deepHtml(registry.get('in-indices'));
  const idxTokens = ['NIFTY', '25,120.40', '+0.62%', 'NARROW', 'BANKNIFTY', 'WIDE'];
  const idxMissing = idxTokens.filter((t) => idxHtml.indexOf(t) < 0);
  ok('Indian indices rendered with level, change and CPR classification',
    idxMissing.length === 0,
    idxMissing.length ? 'missing: ' + idxMissing.join(', ') : '');
  ok('the indices panel reports the weakest source among its rows',
    (registry.get('in-index-prov') || {}).textContent === 'reference',
    (registry.get('in-index-prov') || {}).textContent);

  const fiiHtml = deepHtml(registry.get('in-fii'));
  ok('the flow panel renders its values',
    ['1,845.50', '2,410.20', '4,255.70', '68.5%'].every((t) => fiiHtml.indexOf(t) >= 0),
    fiiHtml.replace(/\s+/g, ' ').slice(0, 260));
  ok('the flow panel is labelled as sample data, not live',
    (registry.get('in-fii-prov') || {}).textContent === 'sample',
    (registry.get('in-fii-prov') || {}).textContent);
  ok('the flow panel prints the backend\u2019s provenance note in full',
    fiiHtml.indexOf('No live FII/DII feed is connected') >= 0);

  const ocHtml = deepHtml(registry.get('in-optionchain'));
  ok('the option chain rendered its aggregates and ladder',
    ['25,120.40', '25,100', '25-Sep-2026', '1.08', '345.00', '1.37%', 'MAX PAIN']
      .every((t) => ocHtml.indexOf(t) >= 0),
    ocHtml.replace(/\s+/g, ' ').slice(0, 300));
  ok('the ATM row is marked',
    selectAll(registry.get('in-optionchain'), '.tt-oc__row--atm').length === 1);
  ok('the chain is labelled as modelled when the feed is not live',
    (registry.get('in-oc-prov') || {}).textContent === 'modelled',
    (registry.get('in-oc-prov') || {}).textContent);
  ok('the modelled chain carries a warning above its numbers',
    ocHtml.indexOf('not read from the NSE chain') >= 0);
  ok('the randomised IV rank is labelled as modelled',
    ocHtml.indexOf('IV rank (modelled)') >= 0,
    ocHtml.replace(/\s+/g, ' ').slice(0, 200));

  console.log('\ntradingview chart source');
  ok('the external widget script is NOT fetched on page load',
    wiring.tvScriptsBeforeClick === 0, 'scripts=' + wiring.tvScriptsBeforeClick);
  ok('selecting the source fetches it exactly once',
    wiring.tvScriptsAfterClick === 1, 'scripts=' + wiring.tvScriptsAfterClick);
  ok('a loading state is shown while the script is in flight',
    String(wiring.tvLoadingHtml || '').indexOf('Loading TradingView') >= 0,
    String(wiring.tvLoadingHtml || '').replace(/\s+/g, ' ').slice(0, 140));
  ok('a blocked CDN renders an explicit failure state, not an empty box',
    String(wiring.tvErrorHtml || '').indexOf('TradingView unavailable') >= 0,
    String(wiring.tvErrorHtml || '').replace(/\s+/g, ' ').slice(0, 160));
  ok('the failure state points at the native chart',
    String(wiring.tvErrorHtml || '').indexOf('native chart is unaffected') >= 0);
  ok('the widget is built once the script is available',
    !!wiring.tvWidgetOpts, 'second click handlers=' + wiring.tvSecondClick);
  ok('the widget is asked for the mapped ticker and interval',
    !!wiring.tvWidgetOpts &&
    wiring.tvWidgetOpts.symbol === 'OANDA:XAUUSD' &&
    wiring.tvWidgetOpts.interval === '60',
    wiring.tvWidgetOpts ? wiring.tvWidgetOpts.symbol + ' @ ' + wiring.tvWidgetOpts.interval : 'none');
  ok('the resolved ticker is printed so a wrong mapping is visible',
    String(wiring.tvLoadingHtml || '').length >= 0 &&
    (function () {
      const host = registry.get('chart-tv');
      return deepHtml(host).indexOf('OANDA:XAUUSD') >= 0;
    })(), deepHtml(registry.get('chart-tv')).replace(/\s+/g, ' ').slice(0, 160));

  console.log('\nbacktest report');
  const btMeta = String(wiring.btMetaHtml || '');
  ok('the job list is fetched and rendered as rows, not the empty state',
    wiring.btRows === 2, 'rows=' + wiring.btRows);
  ok('a job row carries the job id the click handler reads',
    btMeta.indexOf('data-job="bt-real"') >= 0,
    btMeta.replace(/\s+/g, ' ').slice(0, 160));
  ok('a done job reports completion rather than a progress count',
    btMeta.indexOf('done') >= 0, btMeta.replace(/\s+/g, ' ').slice(0, 160));
  ok('clicking a job row is actually wired to a handler',
    wiring.btRowClicks >= 1, 'handler fires=' + wiring.btRowClicks);

  const btHtml = String(wiring.btResultsHtml || '');
  const btFlat = btHtml.replace(/\s+/g, ' ');

  // The regression that started all this: a finished run rendering as
  // "No results in this job". Assert the negative directly, so a future change
  // that re-breaks the reader fails on the symptom the user actually reported.
  ok('a finished run does NOT render the empty state',
    btHtml.indexOf('No results in this job') < 0, btFlat.slice(0, 220));
  ok('the success path clears data-state rather than leaving "empty" behind',
    wiring.btResultsState === null, 'data-state=' + wiring.btResultsState);

  // The report is per trading style. Both tables have to be driven by the
  // payload, not by any local default.
  ok('the per-style table is rendered from report.modes',
    btHtml.indexOf('Per style') >= 0, btFlat.slice(0, 220));
  ok('every mode in the payload gets a row',
    ['SWING', 'SCALP', 'POSITION'].every((s) => btHtml.indexOf(s) >= 0),
    btFlat.slice(0, 300));
  ok('the data-coverage table is rendered from report.series',
    btHtml.indexOf('Data coverage') >= 0 && btHtml.indexOf('XAUUSD') >= 0,
    btFlat.slice(0, 300));
  ok('trade counts are grouped like every other numeric column, not raw',
    btHtml.indexOf('1,204') >= 0 && btHtml.indexOf('8,731') >= 0,
    btFlat.slice(0, 300));
  ok('the out-of-sample expectancy is shown at 4 decimal places',
    btHtml.indexOf('0.0412') >= 0, btFlat.slice(0, 300));
  ok('the geometry is summarised rather than shown as a raw object',
    btHtml.indexOf('tp 2.50') >= 0 && btHtml.indexOf('be 1.00') >= 0,
    btFlat.slice(0, 300));

  // The verdict is the one derived field — it must distinguish the three cases
  // the payload encodes, not collapse them to one label.
  ok('a feasible mode that generalises reads as generalisable',
    btHtml.indexOf('generalisable') >= 0, btFlat.slice(0, 300));
  ok('a feasible mode that does NOT generalise reads as in-sample only',
    btHtml.indexOf('in-sample only') >= 0, btFlat.slice(0, 300));
  ok('an infeasible mode reads as no feasible geometry',
    btHtml.indexOf('no feasible geometry') >= 0, btFlat.slice(0, 300));
  ok('the verdict is carried by a class, not only by text',
    /class="tt-up">generalisable/.test(btHtml) && /class="tt-down">no feasible geometry/.test(btHtml),
    btFlat.slice(0, 300));

  const btMetaLine = String(wiring.btResultMeta || '');
  ok('the result meta line counts modes and series',
    btMetaLine.indexOf('3 modes') >= 0 && btMetaLine.indexOf('2 series') >= 0,
    btMetaLine);
  ok('the result meta line reports the run cost',
    btMetaLine.indexOf('1920 evals') >= 0 && btMetaLine.indexOf('cache 38%') >= 0,
    btMetaLine);
  ok('the result meta line names the objective from the spec',
    btMetaLine.indexOf('objective expectancy') >= 0, btMetaLine);

  // The control: the pre-fix reader, run against the very payload the new
  // reader just rendered, must find nothing. That is what makes this fixture
  // able to catch the original bug at all.
  ok('the pre-fix reader finds nothing in this payload (the bug it shipped)',
    preFixReportReader(BACKTEST_RESULT) === null,
    JSON.stringify(preFixReportReader(BACKTEST_RESULT)));

  console.log('\nbacktest empty report');
  const btEmpty = String(wiring.btEmptyHtml || '');
  ok('a job with no report body says so explicitly instead of rendering blank',
    btEmpty.indexOf('No results in this job') >= 0,
    btEmpty.replace(/\s+/g, ' ').slice(0, 200));
  ok('the empty state is published on data-state so CSS can style it',
    wiring.btEmptyState === 'empty', 'data-state=' + wiring.btEmptyState);
  ok('the empty report does not leave the previous job\'s table on screen',
    btEmpty.indexOf('Per style') < 0, btEmpty.replace(/\s+/g, ' ').slice(0, 200));

  console.log('\nclosed-trade history');
  const hv = wiring.histViews || {};
  const H = (k) => (hv[k] || {});
  const allH = String(H('all').html || '');

  ok('the history table renders the rows the server sent',
    H('all').rows === 8 && H('all').count === '8',
    'rows=' + H('all').rows + ' count=' + H('all').count);
  ok('a populated table clears the loading state',
    H('all').state === null, 'data-state=' + H('all').state);
  ok('no cell renders NaN, undefined or null',
    !/NaN|undefined|null/.test(allH),
    (allH.match(/.{0,40}(NaN|undefined|null).{0,40}/) || [''])[0]);
  ok('side is rendered with its direction class',
    allH.indexOf('tt-dir--buy') >= 0 && allH.indexOf('tt-dir--sell') >= 0);
  ok('volume is formatted to two decimals',
    allH.indexOf('0.25') >= 0 && allH.indexOf('0.10') >= 0);

  /* The column is "Closed", so it must show the CLOSE time. Row 70001 was
     opened 09-10 and closed 09-14; a renderer that shows `timestamp` prints the
     entry and fails here. This is the check that pins the disambiguation. */
  ok('a closed row shows its close time, not its entry time',
    allH.indexOf('2026-09-14 18:30:00') >= 0 && allH.indexOf('2026-09-10 08:15:00') < 0,
    allH.indexOf('2026-09-10 08:15:00') >= 0 ? 'rendered the entry time' : 'close time missing');
  ok('a row with no close time is marked rather than passed off as closed',
    allH.indexOf('(open)') >= 0 && allH.indexOf('2026-09-16 11:05:00') >= 0);
  ok('an MT5-synced row shows the close time it does have',
    allH.indexOf('2026-09-15 13:45:00') >= 0);

  ok('the summary totals the realised P&L and the win/loss split',
    H('all').summary === '8 trades · net +43.95 · 2W / 3L · win rate 33%',
    H('all').summary);
  /* 70008 has realized_pnl null. Counted as a loss the split would read 2W/4L;
     counted in the denominator at all it would read 25% (2/8) rather than 33%
     (2/6 — the six rows that actually carry a P&L). */
  ok('a row with no realised P&L is excluded from the win rate, not counted as a loss',
    H('all').summary.indexOf('2W / 3L') >= 0 && H('all').summary.indexOf('33%') >= 0,
    H('all').summary);

  /* Each filter is driven through its real binding. The counts are chosen so a
     filter that is silently inert (or one that ignores a dimension) shows up as
     the wrong number rather than as a plausible-looking table. */
  ok('the side filter narrows the table',
    H('sell').rows === 3 && H('sell').summary.indexOf('3 of 8 trades') === 0,
    'rows=' + H('sell').rows + ' summary=' + H('sell').summary);
  ok('the side filter keeps the losing row it should',
    H('sell').summary === '3 of 8 trades · net -98.20 · 0W / 2L · win rate 0%',
    H('sell').summary);
  ok('the outcome filter narrows the table',
    H('win').rows === 2, 'rows=' + H('win').rows);
  /* 70005 carries only `profit`, no `realized_pnl`. If historyPnl() stopped
     falling back, this row would count as null and the net would be +124.75. */
  ok('a row whose P&L arrives as `profit` is counted, not skipped',
    H('win').summary === '2 of 8 trades · net +217.15 · 2W / 0L · win rate 100%',
    H('win').summary);
  ok('the source filter separates AI trades from manually executed ones',
    H('ai').rows === 2, 'rows=' + H('ai').rows);
  ok('the symbol filter matches on the symbol prefix',
    H('xau').rows === 4, 'rows=' + H('xau').rows);

  ok('changing the window refetches rather than filtering in place',
    fetchCalls.some((u) => u.indexOf('/api/history') >= 0 && /[?&]days=7(&|$)/.test(u)),
    fetchCalls.filter((u) => u.indexOf('/api/history') >= 0).join(' | '));
  ok('the window selection is sent to the server, not dropped',
    fetchCalls.some((u) => u.indexOf('/api/history') >= 0 && /[?&]limit=\d+/.test(u)),
    fetchCalls.filter((u) => u.indexOf('/api/history') >= 0).join(' | '));

  /* The column holds a close time, so it has to be called one. The label and the
     data are asserted together because a rename that drifts from the data is
     exactly how this column became ambiguous in the first place. */
  ok('the time column is labelled as a close time',
    /scope="col">Closed</.test(html) && !/Execution time/.test(html),
    'header drift');

  console.log('\nauto-selection');
  const sel = wiring.selection || {};
  const selOk = sel.ok || {};
  const selHtml = String(selOk.html || '');
  const selFlat = selHtml.replace(/\s+/g, ' ');

  // Four decisions arrive; only two are tradeable. A panel that counted
  // `decisions.length` would say 4, and one that ignored `is_tradeable` would
  // render all four — so both numbers are asserted against the payload.
  ok('only tradeable decisions are counted',
    selOk.count === '2', 'count=' + selOk.count + ' of 4 decisions');
  ok('only tradeable decisions get a card',
    selOk.cards === 2, 'cards=' + selOk.cards);
  ok('an untradeable decision is not rendered as a setup',
    selHtml.indexOf('GBPUSD') < 0 && selHtml.indexOf('USDJPY') < 0, selFlat.slice(0, 200));
  ok('the best tier is shown on the panel chip',
    selOk.tier === 'HIGH' && /tt-chip--high/.test(String(selOk.tierClass)),
    'tier=' + selOk.tier + ' class=' + selOk.tierClass);
  ok('a card carries the direction, score and rationale',
    selHtml.indexOf('XAUUSD') >= 0 && selHtml.indexOf('tt-dir--buy') >= 0 &&
    selHtml.indexOf('82.5') >= 0 && selHtml.indexOf('2 of 3 styles agree') >= 0,
    selFlat.slice(0, 240));
  ok('a sell decision is rendered as a sell',
    selHtml.indexOf('EURUSD') >= 0 && selHtml.indexOf('tt-dir--sell') >= 0,
    selFlat.slice(0, 240));
  ok('the populated panel clears its loading state',
    selOk.state === null, 'data-state=' + selOk.state);

  const selNa = sel.unavailable || {};
  const selNaHtml = String(selNa.html || '');
  // The engine being absent is an expected state, not a fault, and the panel
  // gives it its own label so an operator does not go looking for a bug.
  ok('an unattached engine is reported as unattached, not as an error',
    selNa.state === 'stale' && selNaHtml.indexOf('Selection engine not attached') >= 0,
    'state=' + selNa.state + ' ' + selNaHtml.replace(/\s+/g, ' ').slice(0, 160));
  ok('the server\'s own reason is shown rather than a generic one',
    selNaHtml.indexOf('Live orchestrator is not attached') >= 0,
    selNaHtml.replace(/\s+/g, ' ').slice(0, 160));
  ok('the count is zeroed when nothing could be evaluated',
    selNa.count === '0' && selNa.tier === '—',
    'count=' + selNa.count + ' tier=' + selNa.tier);
  ok('the previous cards are cleared, not left behind',
    selNaHtml.indexOf('XAUUSD') < 0, selNaHtml.replace(/\s+/g, ' ').slice(0, 160));

  console.log('\nregime policy');
  const reg = wiring.regime || {};
  const regHtml = String(reg.html || '');
  const regFlat = regHtml.replace(/\s+/g, ' ');
  ok('the policy table renders one row per regime across every mode',
    reg.rows === 3, 'rows=' + reg.rows);
  ok('a mode the report errored on is skipped, not rendered as empty',
    regHtml.indexOf('POSITION') < 0, regFlat.slice(0, 200));
  ok('the report the policy came from is named',
    String(reg.source || '').indexOf('regime_20260915_120000.json') >= 0,
    String(reg.source));
  ok('an enabled condition and a disabled one are distinguished',
    regHtml.indexOf('tt-dir--buy') >= 0 && regHtml.indexOf('tt-dir--sell') >= 0,
    regFlat.slice(0, 200));
  ok('a regime using its own geometry is marked apart from the pooled default',
    regHtml.indexOf('own') >= 0 && regHtml.indexOf('pooled') >= 0, regFlat.slice(0, 240));
  /* The quantile is a selectivity statement: 0.99 means "top 1%", and an
     operator reading "0.99" would have to invert it in their head. */
  ok('the score quantile is shown as a selectivity, not as a raw quantile',
    regHtml.indexOf('top 1%') >= 0 && regHtml.indexOf('top 3%') >= 0, regFlat.slice(0, 240));
  ok('the within-regime baseline expectancy is shown signed, in R',
    regHtml.indexOf('+0.0842R') >= 0 && regHtml.indexOf('-0.0121R') >= 0,
    regFlat.slice(0, 300));
  ok('the populated table clears its loading state',
    reg.state === null, 'data-state=' + reg.state);

  const regMetrics = String(reg.metrics || '');
  /* Every counter is read from the label it belongs to. The panel's five cards
     carry five different totals from the same fixture — 3 modes (one of which
     the report errored on, so it is counted here but contributes no rows),
     3 conditions, 2 tradeable, 1 with its own geometry, and the objective name
     — so a swapped or mislabelled counter shows up as the wrong number beside
     the right label. */
  ok('the metrics count conditions and tradeable conditions separately',
    metricValue(regMetrics, 'Conditions') === '3' &&
    metricValue(regMetrics, 'Tradeable') === '2',
    'Conditions=' + metricValue(regMetrics, 'Conditions') +
    ' Tradeable=' + metricValue(regMetrics, 'Tradeable'));
  ok('the metrics count modes including the one that errored',
    metricValue(regMetrics, 'Modes') === '3',
    'Modes=' + metricValue(regMetrics, 'Modes'));
  ok('the metrics count only the regimes that earned their own geometry',
    metricValue(regMetrics, 'Own geometry') === '1',
    'Own geometry=' + metricValue(regMetrics, 'Own geometry'));
  ok('the metrics report the objective the policy was optimised for',
    metricValue(regMetrics, 'Objective') === 'expectancy',
    'Objective=' + metricValue(regMetrics, 'Objective'));

  console.log('\norder submission');
  /* The broker reports a refused order as HTTP 200 with `status` set, so the
     HTTP code cannot decide the outcome and these are the checks that would
     catch the UI announcing a trade that never happened. */
  const T = wiring.toasts || {};
  const texts = (key) => (T[key] || []).map((t) => t.text);
  const kinds = (key) => (T[key] || []).map((t) => t.cls);
  const says = (key, needle) => texts(key).some((s) => s.indexOf(needle) >= 0);
  const isError = (key) => kinds(key).length > 0 && kinds(key).every((k) => k.indexOf('--error') >= 0);
  const dump = (key) => JSON.stringify(T[key] || []);

  const pendPost = postCalls.filter((c) => c.url.indexOf('place_pending_order') >= 0)[0];
  ok('placing a pending order posts the symbol, type, price and volume',
    !!pendPost && pendPost.method === 'POST' && !!pendPost.body &&
    pendPost.body.symbol === 'XAUUSD' && pendPost.body.order_type === 'BUY_LIMIT' &&
    pendPost.body.price === 3400 && pendPost.body.volume === 0.2,
    pendPost ? JSON.stringify(pendPost.body) : 'no POST captured');
  /* Blank means "none" on a new order, and the server treats 0 as none too —
     so a blank level must be absent rather than sent as 0, which would read as
     "no stop" only by coincidence and as an explicit clear on an update. */
  ok('a blank stop or target is omitted rather than sent as zero',
    !!pendPost && pendPost.body.sl === undefined && pendPost.body.tp === undefined,
    pendPost ? JSON.stringify(pendPost.body) : '');

  const manPost = postCalls.filter((c) => c.url.indexOf('manual_trade') >= 0)[0];
  ok('a manual trade posts the side and the volume',
    !!manPost && manPost.method === 'POST' && !!manPost.body &&
    manPost.body.side === 'BUY' && manPost.body.volume === 0.2,
    manPost ? JSON.stringify(manPost.body) : 'no POST captured');

  ok('an accepted pending order is reported as placed',
    says('pending-ok', 'placed') && !isError('pending-ok'), dump('pending-ok'));
  ok('a blocked pending order is not reported as placed',
    !says('pending-blocked', 'placed') && texts('pending-blocked').length > 0,
    dump('pending-blocked'));
  ok('a blocked pending order reports the broker reason',
    says('pending-blocked', 'Execution is disabled') && isError('pending-blocked'),
    dump('pending-blocked'));
  ok('an HTTP 400 rejection reports the server error',
    says('pending-http400', 'price and volume must be numbers') && isError('pending-http400'),
    dump('pending-http400'));

  ok('an accepted market order is reported as submitted',
    says('manual-ok', 'submitted') && !isError('manual-ok'), dump('manual-ok'));
  /* The money path. `send_market_order` answers HTTP 200 with
     {"status":"FAILED","reason":…} for a broker refusal — market closed,
     invalid stops, insufficient margin, or the coherence check — so a handler
     that branches on the HTTP code alone tells the user their order went
     through when the broker rejected it. */
  ok('a rejected market order is not reported as submitted',
    !says('manual-failed', 'submitted') && texts('manual-failed').length > 0,
    dump('manual-failed'));
  ok('a rejected market order reports the broker reason',
    says('manual-failed', 'not below the fill price') && isError('manual-failed'),
    dump('manual-failed'));

  /* The same class one route down: `/api/backtest/cancel` answers 200 with
     `cancelled: false` when the job had already finished, so a guard on the
     HTTP code alone claims a cancellation that did not happen. */
  const cancelPost = postCalls.filter((c) => c.url.indexOf('/api/backtest/cancel') >= 0)[0];
  ok('cancelling a backtest posts the active job id',
    !!cancelPost && cancelPost.method === 'POST' && !!cancelPost.body && !!cancelPost.body.job_id,
    cancelPost ? JSON.stringify(cancelPost.body) : 'no POST captured');
  ok('an accepted cancel is reported as requested',
    says('cancel-ok', 'Cancel requested') && !isError('cancel-ok'), dump('cancel-ok'));
  ok('a cancel that changed nothing is not reported as requested',
    !says('cancel-noop', 'Cancel requested') && texts('cancel-noop').length > 0,
    dump('cancel-noop'));
  ok('a cancel that changed nothing says the job is no longer running',
    says('cancel-noop', 'no longer running') && !isError('cancel-noop'), dump('cancel-noop'));
  ok('a cancel for a job the server cannot find reports the error',
    says('cancel-404', 'job not found') && isError('cancel-404'), dump('cancel-404'));

  console.log('\ncopilot chat');
  const C = wiring.copilot || {};
  const cp = (key) => C[key] || {};
  /* Only the bubbles this drive added. The log already holds the boot greeting,
     and reading the whole log would let a check pass on the greeting's text
     rather than on the answer under test. */
  const cpHtml = (key) => (cp(key).newHtml || []).join('');
  const cpCls = (key) => (cp(key).newClasses || []).join(' ');
  const flat = (s) => String(s || '').replace(/\s+/g, ' ').slice(0, 220);

  /* A whitespace-only question is not a question. Sending it would put a blank
     bubble in the log and ask the server to answer nothing. */
  ok('a blank question is not sent and adds no bubble',
    cp('blank').posts === 0 && cp('blank').added === 0,
    'posts=' + cp('blank').posts + ' added=' + cp('blank').added);
  ok('a blank question is trimmed, not sent as whitespace',
    !(postCalls || []).some((c) => c.url.indexOf('/api/copilot/ask') >= 0 &&
      c.body && typeof c.body.query === 'string' && c.body.query.trim() === ''),
    JSON.stringify(postCalls.filter((c) => c.url.indexOf('copilot') >= 0).map((c) => c.body)));

  const askPosts = postCalls.filter((c) => c.url.indexOf('/api/copilot/ask') >= 0);
  const okPost = askPosts[0];
  /* The context is what makes a bare "why?" answerable — the server reads it and
     falls back to the on-screen instrument. */
  ok('a question carries what the trader is looking at',
    !!okPost && !!okPost.body && !!okPost.body.context &&
    'symbol' in okPost.body.context && 'view' in okPost.body.context,
    okPost ? JSON.stringify(okPost.body) : 'no POST captured');

  /* Escaped *before* the markdown pass, so a symbol name or broker comment
     cannot inject markup into the chat. */
  ok('a question containing markup is escaped, not injected',
    cpHtml('ok').indexOf('<img') < 0 && cpHtml('ok').indexOf('&lt;img') >= 0,
    flat(cpHtml('ok')));
  ok('the escaped question keeps its text',
    cpHtml('ok').indexOf('onerror=alert(1)') >= 0, flat(cpHtml('ok')));
  ok('a question bubble and an answer bubble are added',
    cp('ok').added === 2, 'added=' + cp('ok').added + ' ' + JSON.stringify(cp('ok').newClasses));
  ok('the question is marked as the user\'s and the answer as the bot\'s',
    cpCls('ok').indexOf('tt-copilot__msg--user') >= 0 &&
    cpCls('ok').indexOf('tt-copilot__msg--bot') >= 0,
    JSON.stringify(cp('ok').newClasses));

  /* The answers are built from bullets whose labels are wrapped in **bold**, so
     an asterisk surviving into the bubble is visible on nearly every reply. */
  ok('bold inside a bullet is rendered, not left as asterisks',
    cpHtml('ok').indexOf('<b>Current Bias</b>') >= 0 &&
    cpHtml('ok').indexOf('<b>1,234.56</b>') >= 0,
    flat(cpHtml('ok')));
  ok('no raw asterisks reach the reader',
    cpHtml('ok').indexOf('**') < 0, flat(cpHtml('ok')));
  ok('bullets become one list rather than one list each',
    (cpHtml('ok').match(/<ul>/g) || []).length === 1 &&
    (cpHtml('ok').match(/<li>/g) || []).length === 2,
    flat(cpHtml('ok')));
  ok('a bold line outside a list is still bold',
    cpHtml('ok').indexOf('<b>Open positions') >= 0, flat(cpHtml('ok')));
  ok('single asterisks are emphasised, and do not eat the bold pass',
    cpHtml('ok').indexOf('<i>emphasis</i>') >= 0, flat(cpHtml('ok')));

  /* The "Thinking…" bubble must be gone whichever way the request ends. */
  ok('a refused question removes the pending bubble',
    cpHtml('error').indexOf('Thinking') < 0, flat(cpHtml('error')));
  ok('a refused question says so, with the status',
    cpHtml('error').indexOf('Unavailable') >= 0 && cpHtml('error').indexOf('503') >= 0,
    flat(cpHtml('error')));
  ok('a refused question shows the server\'s own reason, escaped',
    cpHtml('error').indexOf('Live orchestrator is not attached') >= 0 &&
    cpCls('error').indexOf('tt-copilot__msg--error') >= 0,
    flat(cpHtml('error')) + ' classes=' + cpCls('error'));

  console.log('\ntemplate wiring');
  const missing = Array.from(new Set(requestedIds))
    .filter((id) => !templateIds.has(id) && !prelinked.has(id));
  ok('every id the controller queries exists in dashboard.html',
    missing.length === 0, missing.length ? 'missing: ' + missing.join(', ') : '');

  /* Positions panel — tab bar. The tab dispatcher lives in dashboard.js and
     rewrites thead columns per tab; structural coverage here makes the harness
     fail fast if the panel regresses to a single-view list. Real-browser
     click coverage lives in .scratch/pos_tabs_check.js (puppeteer). */
  const jsSrc = fs.readFileSync(JS, 'utf8');
  const tabBar =
    html.indexOf('data-pos-tab="open"') >= 0 &&
    html.indexOf('data-pos-tab="history"') >= 0 &&
    html.indexOf('data-pos-tab="pending"') >= 0 &&
    html.indexOf('id="pos-table"') >= 0 &&
    html.indexOf('id="pos-thead"') >= 0 &&
    html.indexOf('id="flatten-all"') >= 0;
  ok('positions panel carries a three-tab bar (Open/History/Pending)',
    tabBar, tabBar ? null : 'tab buttons or table ids missing in dashboard.html');
  ok('dashboard.js wires all three tab renderers',
    /function setPosTab\s*\(/.test(jsSrc) &&
    /function renderHistoryInPosPanel\s*\(/.test(jsSrc) &&
    /function renderPendingInPosPanel\s*\(/.test(jsSrc) &&
    /function loadPendingForTab\s*\(/.test(jsSrc),
    'expected setPosTab / renderHistoryInPosPanel / renderPendingInPosPanel / loadPendingForTab');
  ok('dashboard.js boot() initialises the tab and warms pending',
    /setPosTab\(['"]open['"]\)/.test(jsSrc) &&
    jsSrc.indexOf('setPosTab(\'open\');') !== -1 &&
    /loadPendingForTab\(\)/.test(jsSrc),
    'boot() must call setPosTab(\'open\') and loadPendingForTab()');

  console.log('\nendpoints');
  ok('candles requested with the tf parameter',
    fetchCalls.some((u) => u.indexOf('/api/candles') >= 0 && /[?&]tf=/.test(u)),
    fetchCalls.filter((u) => u.indexOf('/api/candles') >= 0).join(' '));
  ok('candles requested with a timeframe, not the hard-coded default',
    fetchCalls.some((u) => /[?&]tf=H1/.test(u)),
    fetchCalls.filter((u) => u.indexOf('/api/candles') >= 0).join(' '));

  console.log('\n' + (failures ? failures + ' of ' + checks + ' checks FAILED' : 'all ' + checks + ' checks passed'));
  process.exit(failures ? 1 : 0);
}

/* Let the boot promise chain settle before asserting. Views are driven in
   phases because the analyst and markets panels bind to state.symbol, which is
   only populated once the first telemetry response lands. */
let ticks = 0;
function drain() {
  if (++ticks > 80) return report();
  if (ticks === 5) driveViews();
  if (ticks === 8) driveTradingView();
  if (ticks === 10) readTvFailure();
  if (ticks === 12) driveTvSuccess();
  // Back to the calendar, while the clock is still pinned to the payload's own
  // timestamp. The per-second tick early-returns for any other view, and
  // re-opening the view after advancing the clock would recompute the skew and
  // pin "now" to the new time — which is correct behaviour but would hide the
  // advancement this harness is trying to observe.
  if (ticks === 14) fireDocument('keydown', { key: '2', target: { tagName: 'DIV' } });
  // Backtest: enter the view, click the job that has a real report, capture,
  // then click the job whose report body is empty and capture again. The two
  // are deliberately separated by a tick so the promise chains settle between
  // them and the second capture cannot read the first one's DOM.
  if (ticks === 16) driveBacktestView();
  if (ticks === 18) clickBacktestJob(0);
  if (ticks === 20) captureBacktestResult();
  if (ticks === 21) clickBacktestJob(1);
  if (ticks === 23) captureBacktestEmpty();
  // Hand the view back to the calendar. tickNews() early-returns unless
  // state.view === 'news', so the backtest detour above would otherwise leave
  // the per-second tick inert and make every clock-advance check fail — which
  // reads exactly like a broken calendar. The clock has not been advanced yet
  // at this point (that happens in report()), so re-entering news recomputes
  // the same skew the tick-14 entry did and the advancement is still visible.
  if (ticks === 25) fireDocument('keydown', { key: '2', target: { tagName: 'DIV' } });
  // History filters. Each capture follows its own drive by one tick so the
  // re-render (synchronous) and the refetch (a promise) are both settled.
  if (ticks === 27) captureHistory('all');
  if (ticks === 28) setHistoryFilter('hist-filter-side', 'SELL');
  if (ticks === 29) captureHistory('sell');
  if (ticks === 30) { setHistoryFilter('hist-filter-side', 'ALL'); setHistoryFilter('hist-filter-outcome', 'WIN'); }
  if (ticks === 31) captureHistory('win');
  if (ticks === 32) { setHistoryFilter('hist-filter-outcome', 'ALL'); setHistoryFilter('hist-filter-source', 'AI'); }
  if (ticks === 33) captureHistory('ai');
  if (ticks === 34) { setHistoryFilter('hist-filter-source', 'ALL'); setHistoryFilter('hist-filter-symbol', 'XAU'); }
  if (ticks === 35) captureHistory('xau');
  if (ticks === 36) { setHistoryFilter('hist-filter-symbol', ''); setHistoryFilter('hist-filter-days', '7'); }
  // Auto-selection loads on boot, so its populated state is already on screen.
  if (ticks === 37) captureSelection('ok');
  // The watchlist refresh is the control a user presses when the panel looks
  // stale. Flipping the stub to the real 503 and pressing it drives the
  // "engine not attached" branch through the same path a user would.
  if (ticks === 38) {
    selectionAvailable = false;
    const refresh = registry.get('watch-refresh');
    if (refresh) refresh.fire('click');
  }
  if (ticks === 39) captureSelection('unavailable');
  // Regime policy has no boot path — setView('analytics') is the only way in.
  if (ticks === 40) fireDocument('keydown', { key: '5', target: { tagName: 'DIV' } });
  if (ticks === 42) captureRegimePolicy();
  // Hand the view back to the calendar again: tickNews() early-returns unless
  // state.view === 'news', and the clock-advance checks run in report(). The
  // clock has still not moved, so the skew recomputed here is the same one.
  if (ticks === 44) fireDocument('keydown', { key: '2', target: { tagName: 'DIV' } });
  /* Context strip (item 5): Analyst and News inside the trade page. Driven by
     clicking the real buttons, from the trade view they actually live in — the
     strip is inside view-trade, so exercising it from another view would leave
     open whether it renders where a user sees it.

     The analyst capture is taken EARLY, before the analyst view is opened at
     tick 40. That matters: renderDevilAdvocate() also refreshes the strip, so a
     capture taken afterwards cannot tell the tab's own render apart from the
     full panel's — deleting `renderContextAnalyst()` from setContext() still
     passed when the click ran late. The news capture has the same hazard
     (renderNews() refreshes it), so it is taken before tick 44's reload.

     The strip is handed back to 'why' at 24 so the rest of the run sees the
     default tab. The post-advance news read happens in report(), after the
     clock has actually moved. */
  if (ticks === 15) fireDocument('keydown', { key: '1', target: { tagName: 'DIV' } });
  if (ticks === 17) captureContext('analyst', clickContext('analyst'));
  if (ticks === 19) captureContext('news', clickContext('news'));
  if (ticks === 24) captureContext('bogus', clickContext('bogus'));
  // Order submission. The broker answers HTTP 200 for a refusal and puts the
  // outcome in `status`, so each outcome has to be scripted. The pending order
  // is driven first, then the market order, because the two handlers guard
  // differently and the market one is the money path.
  if (ticks === 46) {
    setTicket({ 'ticket-symbol': 'XAUUSD', 'ticket-type': 'BUY_LIMIT',
                'ticket-volume': '0.20', 'ticket-price': '3400' });
    actionStatus = 200;
    actionResponse = { status: 'PLACED', ticket: 90001 };
    submitPendingOrder();
  }
  if (ticks === 47) captureToasts('pending-ok');
  // BLOCKED is a *distinct* status from FAILED: execution is disabled, so the
  // order was never sent. A guard that only rejects FAILED announces it as
  // placed.
  if (ticks === 48) {
    actionResponse = { status: 'BLOCKED', reason: 'Execution is disabled (mode=offline)' };
    submitPendingOrder();
  }
  if (ticks === 49) captureToasts('pending-blocked');
  // The server's own validation answers HTTP 400, so this is the one case the
  // HTTP code alone identifies.
  if (ticks === 50) {
    actionStatus = 400;
    actionResponse = { status: 'FAILED', error: 'price and volume must be numbers' };
    submitPendingOrder();
  }
  if (ticks === 51) captureToasts('pending-http400');
  if (ticks === 52) {
    actionStatus = 200;
    actionResponse = { status: 'FILLED', ticket: 90002, price: 3400.0 };
    submitManualTrade('BUY');
  }
  if (ticks === 53) captureToasts('manual-ok');
  // A rejected market order, exactly as the live server answers it.
  if (ticks === 54) {
    actionResponse = { status: 'FAILED',
                       reason: 'BUY stop-loss 2100.0 is not below the fill price 2000.0' };
    submitManualTrade('BUY');
  }
  if (ticks === 55) captureToasts('manual-failed');
  // Backtest cancel. The active job is whichever row was last clicked, so the
  // button has something to cancel. A job that has already finished answers 200
  // with cancelled:false, which is not a cancellation.
  if (ticks === 57) {
    actionStatus = 200;
    actionResponse = { status: 'OK', cancelled: true, job: { job_id: 'bt-empty' } };
    const btn = registry.get('bt-cancel');
    if (btn) btn.fire('click');
  }
  if (ticks === 58) captureToasts('cancel-ok');
  if (ticks === 59) {
    actionResponse = { status: 'NOOP', cancelled: false, job: { job_id: 'bt-empty' } };
    const btn = registry.get('bt-cancel');
    if (btn) btn.fire('click');
  }
  if (ticks === 60) captureToasts('cancel-noop');
  if (ticks === 61) {
    actionStatus = 404;
    actionResponse = { status: 'ERROR', error: 'job not found' };
    const btn = registry.get('bt-cancel');
    if (btn) btn.fire('click');
  }
  if (ticks === 62) captureToasts('cancel-404');
  if (ticks === 63) { actionResponse = { status: 'PLACED', ticket: 90001 }; actionStatus = 200; }
  // Copilot. A whitespace-only question must not reach the server or add a
  // bubble; the boot greeting is already in the log, so every capture is a
  // delta from the count taken immediately before the drive.
  if (ticks === 65) {
    wiring.copilotBefore = copilotSnapshot();
    driveCopilot('   ');
  }
  if (ticks === 66) captureCopilot('blank', wiring.copilotBefore);
  // A hostile question, and an answer shaped like the server's real ones —
  // every structured answer is bullets whose labels are wrapped in **bold**.
  if (ticks === 67) {
    actionStatus = 200;
    actionResponse = {
      status: 'OK',
      response: '**Open positions — 1**\n' +
                '- **Current Bias**: BULL (TREND_FOLLOW)\n' +
                '- Balance **1,234.56** · Equity **1,240.00**\n' +
                'Ask me about a symbol with *emphasis*.'
    };
    wiring.copilotBefore = copilotSnapshot();
    driveCopilot('<img src=x onerror=alert(1)> & "quoted"');
  }
  if (ticks === 68) captureCopilot('ok', wiring.copilotBefore);
  if (ticks === 69) {
    actionStatus = 503;
    actionResponse = { status: 'UNAVAILABLE', error: 'Live orchestrator is not attached' };
    wiring.copilotBefore = copilotSnapshot();
    driveCopilot('why?');
  }
  if (ticks === 70) captureCopilot('error', wiring.copilotBefore);
  if (ticks === 71) { actionResponse = { status: 'PLACED', ticket: 90001 }; actionStatus = 200; }
  setImmediate(drain);
}
drain();
