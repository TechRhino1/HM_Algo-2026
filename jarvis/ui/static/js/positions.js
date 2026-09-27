/* ============================================================================
   Positions Page Controller — Institutional Bugatti Live Book & Trade History
   ----------------------------------------------------------------------------
   Connected directly to:
     - /api/telemetry_state (Open positions & broker account KPIs)
     - /api/history (Closed trades journal with full pagination)
     - /api/pending_orders (Working limit and stop orders)
     - /api/action/* (Execution: close, flatten all, cancel pending, modify)
   ========================================================================== */
(function () {
  'use strict';

  var state = {
    tab: 'open',
    openPositions: [],
    historyTrades: [],
    pendingOrders: [],
    account: null,
    searchQuery: '',
    historyPage: 1,
    historyPageSize: 10,
    pollTimer: null
  };

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  var fmtPrice = function (val, sym) {
    var n = Number(val);
    if (!isFinite(n) || n === 0) return '—';
    var d = 5;
    if (sym && (sym.indexOf('XAU') >= 0 || sym.indexOf('BTC') >= 0 || sym.indexOf('ETH') >= 0 || sym.indexOf('US500') >= 0)) d = 2;
    return n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  };

  var toast = function (text, kind) {
    var host = $('toast-host');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'ios-toast' + (kind ? ' ios-toast--' + kind : '');
    el.textContent = text;
    host.appendChild(el);
    setTimeout(function () {
      el.style.transition = 'opacity 200ms ease, transform 200ms ease';
      el.style.opacity = '0';
      el.style.transform = 'translateY(8px)';
      setTimeout(function () { el.remove(); }, 220);
    }, 2400);
  };

  /* ── Header Columns per Tab ────────────────────────────────────────── */
  var HEAD_COLS = {
    open:
      '<tr>' +
        '<th scope="col">Symbol</th>' +
        '<th scope="col">Side</th>' +
        '<th scope="col" class="tt-num">Vol</th>' +
        '<th scope="col" class="tt-num">Entry</th>' +
        '<th scope="col" class="tt-num">Now</th>' +
        '<th scope="col" class="tt-num">SL</th>' +
        '<th scope="col" class="tt-num">TP</th>' +
        '<th scope="col" class="tt-num">Floating P&amp;L</th>' +
        '<th scope="col" class="tt-pos-actions-col">Actions</th>' +
      '</tr>',
    history:
      '<tr>' +
        '<th scope="col">Symbol</th>' +
        '<th scope="col">Side</th>' +
        '<th scope="col" class="tt-num">Vol</th>' +
        '<th scope="col" class="tt-num">Entry</th>' +
        '<th scope="col" class="tt-num">Exit</th>' +
        '<th scope="col" class="tt-num">Realized P&amp;L</th>' +
        '<th scope="col">Closed Time</th>' +
        '<th scope="col" class="tt-pos-actions-col">Actions</th>' +
      '</tr>',
    pending:
      '<tr>' +
        '<th scope="col">Symbol</th>' +
        '<th scope="col">Type</th>' +
        '<th scope="col" class="tt-num">Vol</th>' +
        '<th scope="col" class="tt-num">Price</th>' +
        '<th scope="col" class="tt-num">SL</th>' +
        '<th scope="col" class="tt-num">TP</th>' +
        '<th scope="col">Status</th>' +
        '<th scope="col" class="tt-pos-actions-col">Actions</th>' +
      '</tr>'
  };

  /* ── Fetch Telemetry & Account ─────────────────────────────────────── */
  function loadTelemetry() {
    fetch('/api/telemetry_state')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data) return;
        state.openPositions = data.positions || [];
        state.account = data.account || null;
        renderAccountKPIs();
        if (state.tab === 'open') renderOpenTable();
        updateTabBadges();
      })
      .catch(function (e) {
        console.warn('Telemetry poll error:', e);
      });
  }

  /* ── Fetch History ─────────────────────────────────────────────────── */
  function loadHistory() {
    fetch('/api/history?limit=1000')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.historyTrades = Array.isArray(data) ? data : [];
        if (state.tab === 'history') renderHistoryTable();
        updateTabBadges();
      })
      .catch(function (e) {
        console.warn('History fetch error:', e);
      });
  }

  /* ── Fetch Pending Orders ──────────────────────────────────────────── */
  function loadPending() {
    fetch('/api/pending_orders')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.pendingOrders = (data && Array.isArray(data.orders)) ? data.orders : (Array.isArray(data) ? data : []);
        if (state.tab === 'pending') renderPendingTable();
        updateTabBadges();
      })
      .catch(function (e) {
        console.warn('Pending fetch error:', e);
      });
  }

  /* ── Render Top KPI Metrics Ribbon ─────────────────────────────────── */
  function renderAccountKPIs() {
    var acc = state.account;
    var openCount = state.openPositions.length;
    var totalPnl = 0;
    state.openPositions.forEach(function (p) { totalPnl += Number(p.pnl || p.profit || 0); });

    var elCount = $('kpi-open-count');
    if (elCount) elCount.textContent = String(openCount);

    var elPnl = $('kpi-floating-pnl');
    if (elPnl) {
      elPnl.textContent = (totalPnl > 0 ? '+' : '') + '$' + totalPnl.toFixed(2);
      elPnl.className = 'positions-kpi-val ' + (totalPnl > 0 ? 'text-bull' : (totalPnl < 0 ? 'text-bear' : ''));
    }

    if (acc) {
      var bal = Number(acc.balance || 0);
      var eq = Number(acc.equity || 0);
      var ml = Number(acc.margin_level || 0);

      var elBal = $('kpi-balance');
      if (elBal) elBal.textContent = '$' + bal.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

      var elEq = $('kpi-equity');
      if (elEq) elEq.textContent = '$' + eq.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

      var elMl = $('kpi-margin');
      if (elMl) elMl.textContent = ml > 0 ? ml.toFixed(1) + '%' : '--%';
    }
  }

  function updateTabBadges() {
    var bOpen = $('badge-open-count');
    if (bOpen) bOpen.textContent = String(state.openPositions.length);

    var bHist = $('badge-history-count');
    if (bHist) bHist.textContent = String(state.historyTrades.length);

    var bPend = $('badge-pending-count');
    if (bPend) bPend.textContent = String(state.pendingOrders.length);
  }

  /* ── Filter helper ─────────────────────────────────────────────────── */
  function matchesFilter(sym) {
    if (!state.searchQuery) return true;
    return String(sym || '').toLowerCase().indexOf(state.searchQuery.toLowerCase()) >= 0;
  }

  /* ── Render Open Positions ─────────────────────────────────────────── */
  function renderOpenTable() {
    var tbody = $('positions-live-tbody');
    var thead = $('positions-live-thead');
    var histPag = $('pos-hist-pagination');
    if (thead) thead.innerHTML = HEAD_COLS.open;
    if (histPag) histPag.hidden = true;
    if (!tbody) return;

    var filtered = state.openPositions.filter(function (p) { return matchesFilter(p.symbol); });
    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="9" style="text-align:center; padding:36px; color:var(--machined-silver);">No open positions matching criteria</td></tr>';
      return;
    }

    tbody.innerHTML = filtered.map(function (p) {
      var sym = p.symbol || '';
      var side = String(p.side || p.type || '').toUpperCase();
      var dirCls = /BUY|LONG/.test(side) ? 'tt-dir--buy' : 'tt-dir--sell';
      var vol = Number(p.volume || p.lots || 0).toFixed(2);
      var pnl = Number(p.pnl || p.profit || 0);
      var pnlCls = pnl > 0 ? 'tt-up' : (pnl < 0 ? 'tt-down' : '');
      var ticket = p.ticket || p.id || '';

      return '<tr>' +
        '<td><span class="tt-symbol">' + esc(sym) + '</span></td>' +
        '<td><span class="tt-dir ' + dirCls + '">' + esc(side || '—') + '</span></td>' +
        '<td class="tt-num">' + vol + '</td>' +
        '<td class="tt-num">' + fmtPrice(p.entry || p.entry_price, sym) + '</td>' +
        '<td class="tt-num">' + fmtPrice(p.now || p.current_price, sym) + '</td>' +
        '<td class="tt-num">' + fmtPrice(p.sl, sym) + '</td>' +
        '<td class="tt-num">' + fmtPrice(p.tp, sym) + '</td>' +
        '<td class="tt-num ' + pnlCls + '" style="font-weight:700;">' + (pnl > 0 ? '+' : '') + '$' + pnl.toFixed(2) + '</td>' +
        '<td class="tt-pos-actions">' +
          '<button class="tt-btn tt-btn--sm" data-action-modify="' + esc(ticket) + '" data-sym="' + esc(sym) + '" data-sl="' + (p.sl || '') + '" data-tp="' + (p.tp || '') + '" type="button" style="padding:2px 8px; margin-right:4px;">Edit</button>' +
          '<button class="tt-btn tt-btn--sm tt-btn--danger" data-action-close="' + esc(ticket) + '" type="button" style="padding:2px 8px;">Close</button>' +
        '</td>' +
      '</tr>';
    }).join('');

    // Attach actions
    Array.prototype.forEach.call(tbody.querySelectorAll('[data-action-close]'), function (btn) {
      btn.addEventListener('click', function () {
        var t = Number(btn.getAttribute('data-action-close'));
        closePosition(t);
      });
    });
    Array.prototype.forEach.call(tbody.querySelectorAll('[data-action-modify]'), function (btn) {
      btn.addEventListener('click', function () {
        var t = btn.getAttribute('data-action-modify');
        var s = btn.getAttribute('data-sym');
        var sl = btn.getAttribute('data-sl');
        var tp = btn.getAttribute('data-tp');
        openModifyModal(t, s, sl, tp);
      });
    });
  }

  /* ── Render Trade History (with pagination) ────────────────────────── */
  function renderHistoryTable() {
    var tbody = $('positions-live-tbody');
    var thead = $('positions-live-thead');
    var histPag = $('pos-hist-pagination');
    if (thead) thead.innerHTML = HEAD_COLS.history;
    if (!tbody) return;

    var filtered = state.historyTrades.filter(function (t) { return matchesFilter(t.symbol); });
    var total = filtered.length;

    if (!total) {
      if (histPag) histPag.hidden = true;
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:36px; color:var(--machined-silver);">No closed trade history available</td></tr>';
      return;
    }

    if (histPag) histPag.hidden = false;

    var pageSize = Number(state.historyPageSize) || 10;
    var totalPages = Math.max(1, Math.ceil(total / pageSize));
    if (state.historyPage > totalPages) state.historyPage = totalPages;
    if (state.historyPage < 1) state.historyPage = 1;

    var start = (state.historyPage - 1) * pageSize;
    var end = Math.min(start + pageSize, total);
    var pageRows = filtered.slice(start, end);

    // Update pagination controls
    var elRange = $('pos-hist-range');
    if (elRange) elRange.textContent = (start + 1) + '–' + end;
    var elTotal = $('pos-hist-total');
    if (elTotal) elTotal.textContent = String(total);
    var elInd = $('pos-hist-page-indicator');
    if (elInd) elInd.textContent = state.historyPage + ' / ' + totalPages;

    var btnFirst = $('pos-hist-first');
    var btnPrev = $('pos-hist-prev');
    var btnNext = $('pos-hist-next');
    var btnLast = $('pos-hist-last');
    if (btnFirst) btnFirst.disabled = (state.historyPage <= 1);
    if (btnPrev) btnPrev.disabled = (state.historyPage <= 1);
    if (btnNext) btnNext.disabled = (state.historyPage >= totalPages);
    if (btnLast) btnLast.disabled = (state.historyPage >= totalPages);

    tbody.innerHTML = pageRows.map(function (t) {
      var sym = t.symbol || '';
      var side = String(t.action || t.type || t.side || '').toUpperCase();
      var dirCls = /BUY|LONG/.test(side) ? 'tt-dir--buy' : 'tt-dir--sell';
      var vol = Number(t.volume || 0).toFixed(2);
      var pnl = Number(t.realized_pnl || t.profit || 0);
      var pnlCls = pnl > 0 ? 'tt-up' : (pnl < 0 ? 'tt-down' : '');
      var stamp = t.closed_at || t.timestamp || '';
      var when = stamp ? String(stamp).replace('T', ' ').replace(/\.\d+.*$/, '').slice(0, 16) : '—';

      return '<tr>' +
        '<td><span class="tt-symbol">' + esc(sym) + '</span></td>' +
        '<td><span class="tt-dir ' + dirCls + '">' + esc(side || '—') + '</span></td>' +
        '<td class="tt-num">' + vol + '</td>' +
        '<td class="tt-num">' + fmtPrice(t.entry_price || t.entry, sym) + '</td>' +
        '<td class="tt-num">' + fmtPrice(t.exit_price || t.exit, sym) + '</td>' +
        '<td class="tt-num ' + pnlCls + '" style="font-weight:700;">' + (pnl > 0 ? '+' : '') + '$' + pnl.toFixed(2) + '</td>' +
        '<td><span class="tt-muted">' + esc(when) + '</span></td>' +
        '<td class="tt-pos-actions tt-muted">—</td>' +
      '</tr>';
    }).join('');
  }

  /* ── Render Pending Orders ─────────────────────────────────────────── */
  function renderPendingTable() {
    var tbody = $('positions-live-tbody');
    var thead = $('positions-live-thead');
    var histPag = $('pos-hist-pagination');
    if (thead) thead.innerHTML = HEAD_COLS.pending;
    if (histPag) histPag.hidden = true;
    if (!tbody) return;

    var filtered = state.pendingOrders.filter(function (o) { return matchesFilter(o.symbol); });
    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:36px; color:var(--machined-silver);">No active pending orders open</td></tr>';
      return;
    }

    tbody.innerHTML = filtered.map(function (o) {
      var sym = o.symbol || '';
      var typeStr = String(o.type || o.order_type || '').toUpperCase();
      var dirCls = /BUY/i.test(typeStr) ? 'tt-dir--buy' : 'tt-dir--sell';
      var vol = Number(o.volume || o.lots || 0).toFixed(2);
      var ticket = o.ticket || o.id || '';

      return '<tr>' +
        '<td><span class="tt-symbol">' + esc(sym) + '</span></td>' +
        '<td><span class="tt-dir ' + dirCls + '">' + esc(typeStr) + '</span></td>' +
        '<td class="tt-num">' + vol + '</td>' +
        '<td class="tt-num">' + fmtPrice(o.price, sym) + '</td>' +
        '<td class="tt-num">' + fmtPrice(o.sl, sym) + '</td>' +
        '<td class="tt-num">' + fmtPrice(o.tp, sym) + '</td>' +
        '<td><span class="tt-chip tt-chip--buy">PLACED</span></td>' +
        '<td class="tt-pos-actions">' +
          '<button class="tt-btn tt-btn--sm tt-btn--danger" data-action-cancel="' + esc(ticket) + '" type="button" style="padding:2px 8px;">Cancel</button>' +
        '</td>' +
      '</tr>';
    }).join('');

    Array.prototype.forEach.call(tbody.querySelectorAll('[data-action-cancel]'), function (btn) {
      btn.addEventListener('click', function () {
        var t = Number(btn.getAttribute('data-action-cancel'));
        cancelPendingOrder(t);
      });
    });
  }

  /* ── Actions: Close Position ───────────────────────────────────────── */
  function closePosition(ticket) {
    if (!ticket) return;
    if (!confirm('Close active position #' + ticket + '?')) return;
    fetch('/api/action/close_position', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticket: ticket })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.status === 'SUCCESS' || res.status === 'FILLED' || res.closed) {
          toast('Position #' + ticket + ' closed successfully', 'success');
        } else {
          toast('Close order dispatched for #' + ticket, 'info');
        }
        loadTelemetry();
        loadHistory();
      })
      .catch(function (e) {
        toast('Close failed: ' + e, 'warn');
      });
  }

  /* ── Actions: Flatten All ──────────────────────────────────────────── */
  function flattenAll() {
    if (!state.openPositions.length) {
      toast('No open positions to flatten', 'info');
      return;
    }
    if (!confirm('EMERGENCY: Close all ' + state.openPositions.length + ' open positions immediately?')) return;
    fetch('/api/action/close_all_positions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        toast('Closed all open positions', 'success');
        loadTelemetry();
        loadHistory();
      })
      .catch(function (e) {
        toast('Flatten failed: ' + e, 'warn');
      });
  }

  /* ── Actions: Cancel Pending ───────────────────────────────────────── */
  function cancelPendingOrder(ticket) {
    if (!ticket) return;
    if (!confirm('Cancel pending order #' + ticket + '?')) return;
    fetch('/api/action/cancel_pending_order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticket: ticket })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        toast('Pending order #' + ticket + ' cancelled', 'success');
        loadPending();
      })
      .catch(function (e) {
        toast('Cancel failed: ' + e, 'warn');
      });
  }

  /* ── Modify Modal ──────────────────────────────────────────────────── */
  function openModifyModal(ticket, sym, sl, tp) {
    var modal = $('modify-modal');
    if (!modal) return;
    $('modify-ticket-val').value = ticket;
    $('modify-modal-sub').textContent = 'Order #' + ticket + ' · ' + sym;
    $('modify-sl-input').value = sl || '';
    $('modify-tp-input').value = tp || '';
    modal.hidden = false;
  }

  function closeModifyModal() {
    var modal = $('modify-modal');
    if (modal) modal.hidden = true;
  }

  function saveModifyOrder() {
    var ticket = Number($('modify-ticket-val').value);
    var sl = parseFloat($('modify-sl-input').value) || 0;
    var tp = parseFloat($('modify-tp-input').value) || 0;
    if (!ticket) return;

    fetch('/api/action/modify_pending_order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticket: ticket, sl: sl, tp: tp })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        toast('Protection levels updated for #' + ticket, 'success');
        closeModifyModal();
        loadTelemetry();
      })
      .catch(function (e) {
        toast('Update failed: ' + e, 'warn');
      });
  }

  /* ── Tab Dispatcher ────────────────────────────────────────────────── */
  function setTab(tab) {
    state.tab = tab;
    Array.prototype.forEach.call(document.querySelectorAll('[data-pos-tab]'), function (btn) {
      var isSel = btn.getAttribute('data-pos-tab') === tab;
      btn.className = 'positions-seg-btn' + (isSel ? ' active' : '');
      btn.setAttribute('aria-selected', isSel ? 'true' : 'false');
    });

    if (tab === 'open') renderOpenTable();
    else if (tab === 'history') {
      state.historyPage = 1;
      renderHistoryTable();
    } else if (tab === 'pending') renderPendingTable();
  }

  /* ── Event Wiring ──────────────────────────────────────────────────── */
  function wireEvents() {
    // Segmented tabs
    Array.prototype.forEach.call(document.querySelectorAll('[data-pos-tab]'), function (btn) {
      btn.addEventListener('click', function () { setTab(btn.getAttribute('data-pos-tab')); });
    });

    // Refresh & Flatten buttons
    var btnRef = $('btn-refresh-positions');
    if (btnRef) btnRef.addEventListener('click', function () {
      loadTelemetry();
      loadHistory();
      loadPending();
      toast('Refreshed portfolio book', 'info');
    });

    var btnFlat = $('btn-flatten-positions');
    if (btnFlat) btnFlat.addEventListener('click', flattenAll);

    // Search input
    var search = $('pos-search');
    if (search) {
      search.addEventListener('input', function () {
        state.searchQuery = search.value.trim();
        if (state.tab === 'open') renderOpenTable();
        else if (state.tab === 'history') {
          state.historyPage = 1;
          renderHistoryTable();
        } else if (state.tab === 'pending') renderPendingTable();
      });
    }

    // History Pagination controls
    var pSize = $('pos-hist-page-size');
    if (pSize) {
      pSize.addEventListener('change', function () {
        state.historyPageSize = Number(pSize.value) || 10;
        state.historyPage = 1;
        renderHistoryTable();
      });
    }

    var btnFirst = $('pos-hist-first');
    if (btnFirst) btnFirst.addEventListener('click', function () {
      state.historyPage = 1;
      renderHistoryTable();
    });

    var btnPrev = $('pos-hist-prev');
    if (btnPrev) btnPrev.addEventListener('click', function () {
      if (state.historyPage > 1) {
        state.historyPage--;
        renderHistoryTable();
      }
    });

    var btnNext = $('pos-hist-next');
    if (btnNext) btnNext.addEventListener('click', function () {
      state.historyPage++;
      renderHistoryTable();
    });

    var btnLast = $('pos-hist-last');
    if (btnLast) btnLast.addEventListener('click', function () {
      var filtered = state.historyTrades.filter(function (t) { return matchesFilter(t.symbol); });
      var totalPages = Math.max(1, Math.ceil(filtered.length / state.historyPageSize));
      state.historyPage = totalPages;
      renderHistoryTable();
    });

    // Modal controls
    var modClose = $('modify-close-btn');
    if (modClose) modClose.addEventListener('click', closeModifyModal);
    var modCancel = $('modify-cancel-btn');
    if (modCancel) modCancel.addEventListener('click', closeModifyModal);
    var modConfirm = $('modify-confirm-btn');
    if (modConfirm) modConfirm.addEventListener('click', saveModifyOrder);

    var modal = $('modify-modal');
    if (modal) {
      modal.addEventListener('click', function (e) {
        if (e.target === modal) closeModifyModal();
      });
    }
  }

  /* ── Init ──────────────────────────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', function () {
    wireEvents();
    loadTelemetry();
    loadHistory();
    loadPending();

    // Regular background polling every 2.5s for live book parity
    state.pollTimer = setInterval(function () {
      loadTelemetry();
      if (state.tab === 'pending') loadPending();
    }, 2500);
  });
})();
