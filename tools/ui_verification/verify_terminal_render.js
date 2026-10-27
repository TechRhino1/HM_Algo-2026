/* ===========================================================================
   Headless render check for the classic terminal (tools/verify_terminal_render.js)

   WHY THIS EXISTS
   ---------------
   jarvis/ui/static/js/terminal.js is ~3,400 lines and had **no renderer
   coverage at all**. The dashboard has verify_dashboard_render.js; the terminal
   had only tools/verify_copilot_render.js (which evaluates the copilot helpers
   in isolation) and a check in verify_ui_live.py that index.html *references*
   the file. Neither proves that a single table row is ever built.

   That is the gap that let the same defect live in two places: the dashboard's
   history column was fixed to read `closed_at`, and the terminal's still reads
   the ambiguous `timestamp` — invisible, because nothing exercised it.

   HOW IT WORKS
   ------------
   The real terminal.js runs in a Node `vm` with the shared stubbed DOM
   (tools/dom_stub.js) and a fetch stub. Boot is driven the way a browser
   drives it — the captured DOMContentLoaded handler is fired — so the fetchers
   run and the renderers are reached through their real wiring. Calling
   renderXxxDOM() directly would prove the last hop only.

   The fixtures are the **real payload shapes**, captured from a running server
   (see the header of each fixture), not invented. A fixture whose shape is
   wrong turns every assertion downstream of it into a no-op.

   Run: node tools/verify_terminal_render.js
   Exits non-zero on the first failed expectation.
   =========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom } = require('./dom_stub');

const ROOT = path.resolve(__dirname, '..');
const JS = path.join(ROOT, 'jarvis', 'ui', 'static', 'js', 'terminal.js');
const HTML = path.join(ROOT, 'jarvis', 'ui', 'templates', 'index.html');
const DASH = path.join(ROOT, 'jarvis', 'ui', 'static', 'js', 'dashboard.js');

let failures = 0;
let checks = 0;
let findings = 0;

function ok(label, condition, detail) {
  checks++;
  if (condition) {
    console.log('  PASS  ' + label);
  } else {
    failures++;
    console.log('  FAIL  ' + label + (detail ? '  -> ' + detail : ''));
  }
}

/* A finding is not a pass and not a failure: it is a measurement of something
   the terminal does today that has been reported rather than silently fixed.
   Counting it separately keeps "the suite is green" honest — a FIND line is a
   reproduced defect, not an approved behaviour. */
function finding(label, reproduced, detail) {
  findings++;
  console.log((reproduced ? '  FIND  ' : '  CLEAR ') + label + (detail ? '  -> ' + detail : ''));
}

/* ── Read the template's real ids ─────────────────────────────────────────── */
const html = fs.readFileSync(HTML, 'utf8');
const templateIds = new Set();
{
  const re = /\bid="([^"]+)"/g;
  let m;
  while ((m = re.exec(html)) !== null) templateIds.add(m[1]);
}

/* ── A permissive stub for the charting libraries ─────────────────────────────
   The terminal drives TradingView's lightweight-charts at boot. Modelling that
   API would be a large amount of code that proves nothing about the terminal:
   what matters here is that the chart calls do not throw and do not stop the
   rest of the boot. So every property and call resolves to another stub.

   Two details that matter. `then` must be undefined or `await chart` would try
   to adopt a non-promise and hang. And the stub must stringify to '' so that a
   stub value interpolated into a template literal does not inject its own name
   into the HTML the assertions read. */
function autoStub(name) {
  const fn = function () { return autoStub(name + '()'); };
  const proxy = new Proxy(fn, {
    get(t, k) {
      if (k === 'then') return undefined;
      if (k === Symbol.toPrimitive) return () => 0;
      if (k === 'toString') return () => '';
      if (k === 'valueOf') return () => 0;
      if (k === 'constructor') return fn;
      if (k === Symbol.for('nodejs.util.inspect.custom')) return () => name;
      if (!(k in t)) t[k] = autoStub(name + '.' + String(k));
      return t[k];
    },
    set(t, k, v) { t[k] = v; return true; },
    apply() { return autoStub(name + '()'); },
    construct() { return autoStub('new ' + name); },
    has() { return true; }
  });
  return proxy;
}

/* ── Fixtures: the real payload shapes ────────────────────────────────────────
   Captured from a running server on 2026-09-17:

     GET /api/telemetry_state -> {account:{balance,company,currency,equity,
       free_margin,last_sync_time,leverage,login,margin,margin_level,name,profit,
       server,trade_allowed}, execution_mode, safe_mode, trade_style,
       radar_opportunities[], positions[], latest_decisions{}, market_statuses{},
       active_market_status, ...}

     GET /api/history        -> a BARE ARRAY; row keys: action, closed_at,
       entry_price, executor, id, realized_pnl, regime, sl, symbol, ticket,
       timestamp, tp, volume, ...

     GET /api/pending_orders -> a BARE ARRAY
     GET /api/radar          -> {opportunities:[...]}
     GET /api/news           -> {news:[...], timestamp}
     GET /api/candles?...    -> {candles:[...]} (shape read from the chart code)

   `positions` was empty on the live server, so the position row below uses the
   fields renderActiveTradesDOM actually reads. */

const ACCOUNT = {
  balance: 12345.67, equity: 12500.25, free_margin: 9000.5, margin: 3500.75,
  margin_level: 357.14, login: 169172945, server: 'XMGlobal-MT5 2',
  company: 'XM Global Limited', currency: 'USD', leverage: 500,
  name: 'Test Account', profit: 154.58, trade_allowed: true,
  last_sync_time: '2026-09-17T20:00:00'
};

const RADAR_ROWS = [
  { symbol: 'BTCUSD', action: 'BUY READY', bias: 'BUY', entry_price: 85594.06,
    stop_loss: 85247.84, take_profit: 86286.5, win_prob: 95.0,
    risk_reward_ratio: 2.0, strategy: 'TREND_FOLLOWING', regime: 'TREND_BULL',
    confluence_tier: 'LOW', decision: 'EXECUTE', status_label: 'BUY READY',
    timeframe: 'M15/M5/M1', score: 72.5, confluence_score: 61.0,
    gate_passed: true, ev: 1.25, current_price: 85600.0,
    mtf_alignment: 'BULLISH', invalidation_levels: {}, risk_factors: [] }
];

const NEWS_ROWS = [
  { event: 'CB Leading Index m/m', currency: 'AUD', impact: 'LOW',
    time_ist: 'Thu Sep 17, 08:00 PM IST', time_utc: 'Thu Sep 17, 02:30 PM UTC',
    status_badge: 'LATEST RELEASE (Ended at 08:15 PM IST)',
    direction_bias: 'NEUTRAL / DATA PENDING', shock_risk: 'MODERATE',
    forecast: '—', previous: '0.3%', actual: '—',
    affected_pairs: ['AUDUSD', 'AUDJPY'], category: 'ECONOMIC',
    description: 'Leading indicator of economic health.',
    impact_analysis: 'Low impact expected.', is_live: false, is_past: true,
    is_upcoming: false, is_most_recent: true, shock_alert: false,
    execution_warning: '', diff_seconds: -900,
    timestamp_iso: '2026-09-17T14:30:00' }
];

/* Two closed trades. The second has `closed_at: null` and a `timestamp` that
   differs from the first's `closed_at` — a journal row that was opened but not
   yet closed. That is the row that exposes the timestamp/closed_at question:
   the terminal prints `timestamp` for both, so an entry time and an exit time
   are rendered in the same unlabelled column. */
const HISTORY_ROWS = [
  { ticket: 88001, id: 88001, symbol: 'XAUUSD', action: 'BUY', executor: 'BOT',
    regime: 'TREND_BULL', volume: 0.2, entry_price: 2400.5, sl: 2395.0,
    tp: 2412.0, realized_pnl: 230.0, timestamp: '2026-09-16T10:15:00',
    closed_at: '2026-09-16T14:45:00' },
  { ticket: 88002, id: 88002, symbol: 'EURUSD', action: 'SELL', executor: 'MANUAL',
    regime: 'MANUAL_EXECUTION', volume: 0.1, entry_price: 1.0850, sl: 1.0900,
    tp: 1.0750, realized_pnl: -45.5, timestamp: '2026-09-15T08:00:00',
    closed_at: null }
];

const PENDING_ROWS = [
  { ticket: 77001, symbol: 'XAUUSD', type: 'BUY_LIMIT', price_open: 2380.0,
    sl: 2370.0, tp: 2400.0, volume: 0.15, comment: 'HM Algo 2.0' }
];

const POSITION_ROWS = [
  { ticket: 66001, symbol: 'XAUUSD', type: 'BUY', volume: 0.1,
    open_price: 2400.0, current_price: 2405.0, sl: 2390.0, tp: 2420.0,
    profit: 50.0 }
];

const CANDLES = [
  { time: 1758000000, open: 2400, high: 2406, low: 2398, close: 2404, volume: 120 },
  { time: 1758003600, open: 2404, high: 2410, low: 2402, close: 2408, volume: 140 }
];

/* ── One terminal run in its own DOM and sandbox ─────────────────────────────
   Returned as an object so the harness can run it twice: once with benign
   fixtures and once with hostile ones. Re-running beats trying to reach a
   renderer directly, because the renderers are inside an IIFE closure. */
function runTerminal(fixtures) {
  const dom = createDom({
    templateIds: templateIds,
    docRoots: ['radar-list', 'news-feed-list', 'history-tbody',
               'positions-tbody', 'pending-tbody', 'copilot-messages']
  });
  dom.linkTree();

  const fetchCalls = [];
  const warns = [];
  const errors = [];
  const intervals = [];

  const jsonResponse = (body, status) => ({
    ok: (status || 200) >= 200 && (status || 200) < 300,
    status: status || 200,
    json: async () => body
  });

  function fetchStub(url) {
    fetchCalls.push(String(url));
    const u = String(url);
    if (u.indexOf('/api/telemetry_state') >= 0) {
      return Promise.resolve(jsonResponse({
        account: fixtures.account,
        execution_mode: 'PAPER',
        safe_mode: false,
        trade_style: 'SWING',
        radar_opportunities: fixtures.radar,
        positions: fixtures.positions,
        latest_decisions: {},
        market_statuses: {},
        active_market_status: null,
        is_running: true
      }));
    }
    if (u.indexOf('/api/history') >= 0) return Promise.resolve(jsonResponse(fixtures.history));
    if (u.indexOf('/api/pending_orders') >= 0) return Promise.resolve(jsonResponse(fixtures.pending));
    if (u.indexOf('/api/radar') >= 0) return Promise.resolve(jsonResponse({ opportunities: fixtures.radar }));
    if (u.indexOf('/api/news') >= 0) return Promise.resolve(jsonResponse({ news: fixtures.news, timestamp: '2026-09-17T20:00:00' }));
    if (u.indexOf('/api/candles') >= 0) return Promise.resolve(jsonResponse({ candles: fixtures.candles }));
    if (u.indexOf('/api/auth/verify') >= 0) return Promise.resolve(jsonResponse({ valid: true, user: { username: 'admin', role: 'ADMIN' } }));
    return Promise.resolve(jsonResponse({ status: 'OK' }));
  }

  const store = () => {
    const m = new Map();
    return {
      getItem: (k) => (m.has(k) ? m.get(k) : null),
      setItem: (k, v) => m.set(k, String(v)),
      removeItem: (k) => m.delete(k),
      clear: () => m.clear()
    };
  };

  /* console is captured rather than passed through: renderTelemetryDOM wraps
     each sub-renderer in try/catch and only console.warn()s, so a renderer that
     throws is otherwise completely silent — the exact failure this harness
     exists to make visible. */
  const sandbox = {
    console: {
      log: () => {}, info: () => {}, debug: () => {},
      warn: (...a) => warns.push(a.map(String).join(' ')),
      error: (...a) => errors.push(a.map(String).join(' '))
    },
    document: dom.documentStub,
    window: {},
    fetch: fetchStub,
    localStorage: store(),
    sessionStorage: store(),
    setTimeout: () => 0,
    clearTimeout: () => {},
    /* setInterval is captured, not ignored. The boot handler starts
       fetchHistory ONLY inside `setInterval(fetchHistory, 5000)` — it is never
       called directly — so with an inert stub the history table is never
       populated and every history assertion is a no-op. Capturing the intervals
       lets the harness tick them, which is the path a live page takes. */
    setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
    clearInterval: () => {},
    requestAnimationFrame: () => 0,
    alert: () => {},
    ResizeObserver: function () { this.observe = () => {}; this.disconnect = () => {}; },
    LightweightCharts: autoStub('LightweightCharts'),
    TradingView: autoStub('TradingView'),
    location: { hostname: '127.0.0.1', href: 'http://127.0.0.1:8501/', origin: 'http://127.0.0.1:8501', protocol: 'http:' },
    innerWidth: 1440,
    innerHeight: 900,
    devicePixelRatio: 1,
    HM_AUTH: { getToken: () => 'test-token', updateHeaderUI: () => {}, clearSession: () => {} },
    Date, Math, JSON, Number, String, Array, Object, Promise,
    isNaN, isFinite, RegExp, Set, Map, encodeURIComponent, decodeURIComponent,
    parseFloat, parseInt
  };
  /* window === sandbox, so the window's own event API has to live here too. */
  const winListeners = {};
  sandbox.addEventListener = (type, fn) => {
    (winListeners[type] = winListeners[type] || []).push(fn);
  };
  sandbox.removeEventListener = () => {};
  sandbox.dispatchEvent = () => {};
  sandbox.scrollTo = () => {};
  sandbox.open = () => null;
  sandbox.close = () => {};

  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const source = fs.readFileSync(JS, 'utf8');
  vm.createContext(sandbox);
  let loadError = null;
  try {
    vm.runInContext(source, sandbox, { filename: 'terminal.js' });
  } catch (e) {
    loadError = e;
  }

  const drain = async () => {
    for (let i = 0; i < 12; i++) {
      await new Promise((r) => setImmediate(r));
    }
  };

  return {
    dom, sandbox, fetchCalls, warns, errors, intervals,
    get loadError() { return loadError; },
    callsAtBoot: [],
    /* Fire the captured DOMContentLoaded handler — the same entry point a
       browser uses. The handler is not async: it starts the fetchers and
       returns, so the microtask queue has to be drained afterwards. Then tick
       every registered interval once, which is what brings in the panels the
       boot handler leaves to a timer (history). */
    async boot() {
      dom.fireDocument('DOMContentLoaded', {});
      await drain();
      this.callsAtBoot = fetchCalls.slice();
      intervals.forEach((iv) => { try { iv.fn(); } catch (e) { errors.push(String(e && e.message)); } });
      await drain();
      return this;
    },
    html: (id) => dom.deepHtml(dom.registry.get(id)),
    text: (id) => {
      const el = dom.registry.get(id);
      return el ? String(el.textContent) : '';
    },
    cls: (id) => {
      const el = dom.registry.get(id);
      return el ? String(el.className) : '';
    }
  };
}

const BENIGN = {
  account: ACCOUNT, radar: RADAR_ROWS, news: NEWS_ROWS,
  history: HISTORY_ROWS, pending: PENDING_ROWS, positions: POSITION_ROWS,
  candles: CANDLES
};

/* ── Run it ───────────────────────────────────────────────────────────────── */
(async function main() {
  console.log('=== the classic terminal: boot ===');

  const t = runTerminal(BENIGN);

  ok('terminal.js evaluates without throwing', t.loadError === null,
     t.loadError && (t.loadError.message + ' @ ' + (t.loadError.stack || '').split('\n')[1]));
  if (t.loadError) { report(); return; }

  ok('it registered its DOMContentLoaded boot handler',
     (t.dom.documentListeners['DOMContentLoaded'] || []).length === 1,
     'handlers=' + (t.dom.documentListeners['DOMContentLoaded'] || []).length);

  await t.boot();

  /* A renderer that threw inside renderTelemetryDOM's try/catch leaves only a
     console.warn behind. Silence is the assertion. */
  ok('no renderer threw during boot (nothing on console.warn)', t.warns.length === 0,
     t.warns.slice(0, 3).join(' | '));
  ok('nothing reached console.error during boot', t.errors.length === 0,
     t.errors.slice(0, 3).join(' | '));

  /* ── The fetchers actually ran ──────────────────────────────────────────── */
  const called = (frag) => t.fetchCalls.some((u) => u.indexOf(frag) >= 0);
  const calledAtBoot = (frag) => t.callsAtBoot.some((u) => u.indexOf(frag) >= 0);
  ok('boot requested telemetry', calledAtBoot('/api/telemetry_state'));
  ok('boot requested the radar', calledAtBoot('/api/radar'));
  ok('boot requested the news feed', calledAtBoot('/api/news'));
  ok('boot requested candles', calledAtBoot('/api/candles'));
  /* History is the one panel the boot handler leaves to a timer, so nothing
     requests it on first paint and the table stays empty for up to 5s. Pinned
     as observed behaviour — the interval is real wiring, so ticking it is the
     honest way to reach the renderer. */
  ok('the boot handler defers history to an interval instead of fetching it',
     !calledAtBoot('/api/history') && t.intervals.length > 0,
     'atBoot=' + calledAtBoot('/api/history') + ' intervals=' + t.intervals.length);
  ok('ticking the registered intervals brings the history in', called('/api/history'));

  /* ── HUD and account ────────────────────────────────────────────────────── */
  console.log('\n=== HUD and account ===');
  ok('the balance reaches the HUD',
     t.text('hud-balance').indexOf('12,345.67') >= 0, t.text('hud-balance'));
  ok('the equity reaches the HUD',
     t.text('hud-equity').indexOf('12,500.25') >= 0, t.text('hud-equity'));
  ok('the login reaches the HUD',
     t.text('hud-login').indexOf('169172945') >= 0, t.text('hud-login'));
  ok('the server name reaches the HUD',
     t.text('hud-server').indexOf('XMGlobal') >= 0, t.text('hud-server'));
  ok('the execution mode badge shows the payload mode',
     t.text('exec-mode-badge') === 'PAPER', t.text('exec-mode-badge'));
  ok('the status badge reads OPERATIONAL when safe mode is off',
     t.html('status-badge').indexOf('OPERATIONAL') >= 0, t.html('status-badge'));

  /* ── History ────────────────────────────────────────────────────────────── */
  console.log('\n=== history (the restored feature) ===');
  const hist = t.html('history-tbody');
  const histRows = (hist.match(/<tr>/g) || []).length;
  ok('one row per trade', histRows === HISTORY_ROWS.length,
     'rows=' + histRows + ' expected=' + HISTORY_ROWS.length);
  ok('the row count label reads the payload length',
     t.text('history-count').indexOf('2') >= 0, t.text('history-count'));
  ok('ten columns per row', (hist.match(/<td/g) || []).length === HISTORY_ROWS.length * 10,
     'cells=' + (hist.match(/<td/g) || []).length);
  ok('a winning trade is signed and prefixed',
     hist.indexOf('+$230.00') >= 0, hist.slice(0, 200));
  ok('a losing trade is shown negative', hist.indexOf('$-45.50') >= 0);
  ok('a bot trade is badged BOT (AI)', hist.indexOf('BOT (AI)') >= 0);
  ok('a manual trade is badged MANUAL', hist.indexOf('MANUAL') >= 0);
  ok('the side badge reflects the action', hist.indexOf('badge-ready-buy') >= 0 && hist.indexOf('badge-ready-sell') >= 0);
  ok('the symbol is rendered', hist.indexOf('XAUUSD') >= 0 && hist.indexOf('EURUSD') >= 0);
  ok('the ticket is rendered', hist.indexOf('#88001') >= 0 && hist.indexOf('#88002') >= 0);
  ok('a stop and a target are formatted, not raw', hist.indexOf('2,395.00') >= 0 || hist.indexOf('2395.00') >= 0,
     hist.slice(0, 300));

  /* The column used to render `timestamp` under a header of "Execution Time",
     which is the ENTRY time for a journal row and the EXIT time for an
     MT5-synced one — so the header was false for half the rows. It now follows
     the dashboard's rule: prefer `closed_at`, and mark the rows that have none.
     Fixture row 1 has timestamp 10:15 and closed_at 14:45, so the cell must show
     14:45; row 2 has closed_at null, so it must be marked (open). */
  const dashSrc = fs.readFileSync(DASH, 'utf8');
  const termSrc = fs.readFileSync(JS, 'utf8');
  ok('the history column shows the close time, not the entry time',
     hist.indexOf('2026-09-16 14:45') >= 0 && hist.indexOf('2026-09-16 10:15') < 0,
     'close time should be rendered and the entry time should not');
  ok('a row with no close time is marked as still open',
     hist.indexOf('(open)') >= 0 && hist.indexOf('2026-09-15 08:00') >= 0,
     'expected the open row to show its stamp plus an (open) marker');
  ok('the open row carries a title that says so',
     hist.indexOf('Opened; not yet closed') >= 0);
  /* The divergence is closed: both front ends now read `closed_at`, so this
     guard flips from "dashboard only" to "both". */
  ok('both front ends read `closed_at` (the divergence is closed)',
     dashSrc.indexOf('closed_at') >= 0 && termSrc.indexOf('closed_at') >= 0,
     'dashboard=' + (dashSrc.indexOf('closed_at') >= 0) +
     ' terminal=' + (termSrc.indexOf('closed_at') >= 0));

  /* ── Pending orders ─────────────────────────────────────────────────────── */
  console.log('\n=== pending orders ===');
  const pend = t.html('pending-tbody');
  ok('one row per pending order', (pend.match(/<tr/g) || []).length === PENDING_ROWS.length,
     'rows=' + (pend.match(/<tr/g) || []).length);
  ok('the pending ticket is rendered', pend.indexOf('#77001') >= 0);
  ok('the pending side is badged', pend.indexOf('BUY_LIMIT') >= 0);
  ok('the pending volume is formatted to 2dp', pend.indexOf('0.15') >= 0);
  ok('a cancel control is offered', pend.indexOf('cancelPendingOrder') >= 0);
  ok('the pending count reflects the payload', t.text('pending-count').indexOf('1') >= 0,
     t.text('pending-count'));

  /* ── Positions ──────────────────────────────────────────────────────────── */
  console.log('\n=== active positions ===');
  const pos = t.html('positions-tbody');
  ok('one row per position', (pos.match(/<tr/g) || []).length === POSITION_ROWS.length,
     'rows=' + (pos.match(/<tr/g) || []).length);
  ok('the position ticket is rendered', pos.indexOf('#66001') >= 0);
  ok('the floating profit is rendered', pos.indexOf('50.00') >= 0, pos.slice(0, 300));
  ok('a close control is offered', pos.indexOf('closePosition') >= 0);
  ok('the position count reflects the payload', t.text('pos-count').indexOf('1') >= 0,
     t.text('pos-count'));

  /* ── Radar ──────────────────────────────────────────────────────────────── */
  console.log('\n=== opportunity radar ===');
  const radar = t.html('radar-list');
  ok('the radar renders the opportunity symbol', radar.indexOf('BTCUSD') >= 0, radar.slice(0, 200));
  ok('the radar renders the win probability', radar.indexOf('95') >= 0);
  ok('the radar renders the strategy', radar.indexOf('TREND_FOLLOWING') >= 0);
  ok('the radar count reflects the payload', t.text('left-panel-counter').indexOf('1') >= 0,
     t.text('left-panel-counter'));

  /* ── News ───────────────────────────────────────────────────────────────── */
  console.log('\n=== news feed ===');
  const news = t.html('news-feed-list');
  ok('the news feed renders the event name', news.indexOf('CB Leading Index') >= 0, news.slice(0, 200));
  ok('the news feed renders the currency', news.indexOf('AUD') >= 0);
  ok('the news feed renders the IST time', news.indexOf('08:00 PM IST') >= 0);

  /* ── Dock analytics ─────────────────────────────────────────────────────── */
  console.log('\n=== dock analytics ===');
  /* One win of +230 and one loss of -45.50: net 184.50, win rate 50%. */
  ok('the net P&L sums the closed trades',
     t.text('analytics-net-pnl').indexOf('184.50') >= 0, t.text('analytics-net-pnl'));
  ok('the trade count matches the payload',
     t.text('analytics-total-trades').indexOf('2') >= 0, t.text('analytics-total-trades'));
  ok('the win rate is computed from the trades',
     t.text('analytics-win-rate').indexOf('50') >= 0, t.text('analytics-win-rate'));

  /* ── Ids the controller asks for that the template does not define ───────── */
  console.log('\n=== controller ids vs the template ===');
  const missing = Array.from(new Set(t.dom.requestedIds))
    .filter((id) => !templateIds.has(id) && !t.dom.prelinked.has(id));
  const missingAccount = missing.filter((id) => id.indexOf('acc-') === 0);
  const missingBanner = missing.filter((id) => id.indexOf('desk-banner-') === 0);
  ok('only the two known dead groups are missing from index.html',
     missing.length === missingAccount.length + missingBanner.length,
     'unexpected: ' + missing.filter((id) => id.indexOf('acc-') !== 0 && id.indexOf('desk-banner-') !== 0).join(','));
  ok('the account-panel lookups are never read (dead entries)',
     missingAccount.length === 8 && fs.readFileSync(JS, 'utf8').indexOf('el.accBalance') < 0,
     'count=' + missingAccount.length);
  ok('the desk-banner writes are guarded, so the missing ids cannot throw',
     missingBanner.length === 8 &&
     (fs.readFileSync(JS, 'utf8').match(/if \(el\.deskBanner/g) || []).length >= 8,
     'count=' + missingBanner.length);

  /* ── The unescaped-innerHTML finding, measured ──────────────────────────────
     terminal.js interpolates server data into innerHTML. Rather than assert
     that from reading the source, run the real renderers with values a broker
     can actually produce and record what the markup parser receives.

     Two measurement subtleties, both of which produced a wrong answer first:

     * Read the **raw innerHTML string**, not deepHtml(). deepHtml() also
       concatenates each node's decoded `_text`, so text that legitimately
       contains "<img …" as characters reappears there and looks identical to a
       tag that was actually parsed. The security property is "does the markup
       parser see a tag", and only the raw string answers that.
     * For an inline handler, count the quotes that are NOT escaped. A single
       `'` closing the JS string is the breakout; an escaped `\'` is not. Just
       searching for the payload text reports a breakout that escaping fixed.

     These are FIND/CLEAR lines, not PASS/FAIL: a FIND is a reproduced defect,
     a CLEAR is one that no longer reproduces. */
  console.log('\n=== unescaped server data (measured) ===');

  /* A symbol carrying a quote. The pending-orders and positions rows build
     `onclick="window.setSymbol('<symbol>')"`, so an unescaped quote closes the
     JS string and the rest of the value is executed as code. */
  const HOSTILE_SYMBOL = "XAUUSD');alert(1);//";
  /* A broker order comment is free text the terminal prints. */
  const HOSTILE_COMMENT = '<img src=x onerror=alert(1)>';

  /* The JS-string delimiter is intact when exactly the two intended quotes
     survive. Remove escaped quotes, then count the rest. */
  const jsStringIntact = (attr) => {
    const body = String(attr || '').replace(/\\'/g, '');
    return (body.match(/'/g) || []).length === 2;
  };

  const hostile = {
    account: ACCOUNT, radar: [], news: [], history: [],
    pending: [{ ticket: 99001, symbol: HOSTILE_SYMBOL, type: 'BUY_LIMIT',
                price_open: 2380.0, sl: 2370.0, tp: 2400.0, volume: 0.1,
                comment: HOSTILE_COMMENT }],
    positions: [{ ticket: 99002, symbol: HOSTILE_SYMBOL, type: 'BUY', volume: 0.1,
                  open_price: 2400.0, current_price: 2405.0, sl: 2390.0,
                  tp: 2420.0, profit: 5.0 }],
    candles: CANDLES
  };

  const t2 = runTerminal(hostile);
  await t2.boot();

  const pendBody = t2.dom.registry.get('pending-tbody');
  const pendRow = pendBody && pendBody.children[0];
  const onclickAttr = pendRow ? String(pendRow.getAttribute('onclick') || '') : '';
  finding('a quote in the symbol breaks out of the pending row\'s onclick JS string',
          !jsStringIntact(onclickAttr), onclickAttr.slice(0, 90));
  finding('the pending row lets a broker comment reach the markup parser as a tag',
          String(pendBody && pendBody.innerHTML || '').indexOf('<img') >= 0,
          'raw innerHTML contains <img: ' + (String(pendBody && pendBody.innerHTML || '').indexOf('<img') >= 0));

  const posBody = t2.dom.registry.get('positions-tbody');
  const posRow = posBody && posBody.children[0];
  const posOnclick = posRow ? String(posRow.getAttribute('onclick') || '') : '';
  finding('the same breakout exists in the positions row',
          !jsStringIntact(posOnclick), posOnclick.slice(0, 90));

  /* The history row interpolates the symbol into a <b> — markup injection
     rather than a script-attribute breakout, but the same missing convention. */
  const hostileHist = runTerminal({
    account: ACCOUNT, radar: [], news: [], pending: [], positions: [], candles: CANDLES,
    history: [{ ticket: 99003, symbol: '<b onmouseover=alert(1)>X', action: 'BUY',
                executor: 'BOT', volume: 0.1, entry_price: 1, sl: 0, tp: 0,
                realized_pnl: 1, timestamp: '2026-09-16T10:00:00', closed_at: null }]
  });
  await hostileHist.boot();
  const histBody = hostileHist.dom.registry.get('history-tbody');
  finding('the history row lets a hostile symbol reach the markup parser as a tag',
          String(histBody && histBody.innerHTML || '').indexOf('<b onmouseover') >= 0,
          'raw innerHTML contains <b onmouseover: ' + (String(histBody && histBody.innerHTML || '').indexOf('<b onmouseover') >= 0));

  /* The escape hatch is in the same file. While this number stays low, the
     convention is not applied — see the terminal.js escaping note. */
  const termSrcForEscaping = fs.readFileSync(JS, 'utf8');
  const escSites = (termSrcForEscaping.match(/escapeHtml\(/g) || []).length - 1;
  finding('escapeHtml() is applied at fewer than a third of the render sites',
          escSites < 10, 'escapeHtml call sites: ' + escSites);

  report();
})();

function report() {
  console.log('\n' + '='.repeat(74));
  console.log(checks + ' checks, ' + (checks - failures) + ' passed, ' + failures + ' failed');
  if (findings) {
    console.log(findings + ' measurement(s) recorded as FIND/CLEAR — FIND is a reproduced');
    console.log('defect that has been reported, not an approved behaviour.');
  }
  console.log('='.repeat(74));
  process.exit(failures === 0 ? 0 : 1);
}
