/**
 * HM Algo 2.0 — India Markets (NSE/BSE & F&O Intelligence) Client Controller
 */

(function () {
    'use strict';

    // Application State
    const state = {
        stocks: [],
        filteredStocks: [],
        activeSymbol: "NIFTY",
        activeTimeframe: "1D",
        filters: {
            sector: "all",
            market: "all",
            cpr: "all",
            minProb: 0,
            searchQuery: "",
            sortBy: "probability",
            sortDir: "desc"
        },
        chartInstance: null,
        candleSeries: null,
        volumeSeries: null,
        dossierOpen: false,
        currentDossier: null,
        strategyBias: "BULLISH",
        autoScanInterval: 30,
        countdown: 30,
        heatmapOpen: false,
        activeOptionsSymbol: "NIFTY",
        activePredictionHorizon: "intraday",
        activeScannerView: "intraday",
        predictionsData: null,
        optionsAdvisorData: null,
        scannerViewsData: null,
        overviewCardsData: []
    };

    // DOM Elements Cache
    const el = {
        searchInput: document.getElementById("india-search-input"),
        autocompleteDropdown: document.getElementById("india-search-autocomplete"),
        indicesTrack: document.getElementById("indices-track"),
        screenerTableBody: document.getElementById("screener-table-tbody"),
        tableCountBadge: document.getElementById("table-count-badge"),
        aiBuyNowGrid: document.getElementById("ai-buy-now-grid"),
        heatmapSection: document.getElementById("sector-heatmap-section"),
        heatmapGrid: document.getElementById("sector-heatmap-grid"),
        scanCountdown: document.getElementById("scan-countdown"),

        // FII / DII Ribbon
        fiiCashVal: document.getElementById("fii-cash-val"),
        diiCashVal: document.getElementById("dii-cash-val"),
        fiiLongVal: document.getElementById("fii-long-val"),
        niftyPcrVal: document.getElementById("nifty-pcr-val"),

        // Dossier Modal
        dossierModal: document.getElementById("india-dossier-modal"),
        dossierTicker: document.getElementById("dossier-ticker"),
        dossierGradeBadge: document.getElementById("dossier-grade-badge"),
        dossierOppState: document.getElementById("dossier-opp-state"),
        dossierCompany: document.getElementById("dossier-company"),
        dossierLivePrice: document.getElementById("dossier-live-price"),
        dossierChangeBadge: document.getElementById("dossier-change-badge"),
        dossierProbVal: document.getElementById("dossier-prob-val"),
        dossierProbFill: document.getElementById("dossier-prob-fill"),
        dossierCprTag: document.getElementById("dossier-cpr-tag"),
        dossierCamH4: document.getElementById("dossier-cam-h4"),
        dossierVwapTag: document.getElementById("dossier-vwap-tag"),
        dossierLotSize: document.getElementById("dossier-lot-size"),
        dossierOptSym: document.getElementById("dossier-opt-sym"),
        chartContainer: document.getElementById("dossier-tv-chart"),
        multiTfGrid: document.getElementById("multi-tf-grid"),
        newsContainer: document.getElementById("dossier-news-list"),

        // Trade Plan
        planEntry: document.getElementById("plan-entry"),
        planSl: document.getElementById("plan-sl"),
        planTp1: document.getElementById("plan-tp1"),
        planTp2: document.getElementById("plan-tp2"),
        planRr: document.getElementById("plan-rr"),

        // Position Calculator
        calcEquity: document.getElementById("calc-equity-input"),
        calcRiskPct: document.getElementById("calc-risk-pct-input"),
        calcLots: document.getElementById("calc-out-lots"),
        calcShares: document.getElementById("calc-out-shares"),
        calcCapital: document.getElementById("calc-out-capital"),
        calcLoss: document.getElementById("calc-out-loss"),
        calcProfit: document.getElementById("calc-out-profit"),
        calcRrTag: document.getElementById("calc-rr-tag"),

        // CPR breakdown
        cprTcVal: document.getElementById("cpr-tc-val"),
        cprPVal: document.getElementById("cpr-p-val"),
        cprBcVal: document.getElementById("cpr-bc-val"),
        cprWidthVal: document.getElementById("cpr-width-val"),

        // Option Chain Modal
        optModal: document.getElementById("india-opt-modal"),
        optChainTicker: document.getElementById("opt-chain-ticker"),
        optChainSpotInfo: document.getElementById("opt-chain-spot-info"),
        optMaxPainBadge: document.getElementById("opt-max-pain-badge"),
        optPcrBadge: document.getElementById("opt-pcr-badge"),
        optChainTbody: document.getElementById("opt-chain-tbody"),

        // Strategy Builder Modal
        stratModal: document.getElementById("india-strategy-modal"),
        stratModalTicker: document.getElementById("strat-modal-ticker"),
        stratPayloadContainer: document.getElementById("strategy-payload-container"),

        // Live Overview Cards
        liveOverviewCards: document.getElementById("live-overview-cards"),
        cardNifty: document.getElementById("card-nifty"),
        cardSensex: document.getElementById("card-sensex"),
        cardBanknifty: document.getElementById("card-banknifty"),
        liveExchangeClock: document.getElementById("live-exchange-clock"),

        // Options Expert Advisor Workspace
        sectionOptionsAdvisor: document.getElementById("section-options-advisor"),
        advSpotVal: document.getElementById("adv-spot-val"),
        advAtmVal: document.getElementById("adv-atm-val"),
        advMaxPainVal: document.getElementById("adv-maxpain-val"),
        advPcrVal: document.getElementById("adv-pcr-val"),
        advStraddleVal: document.getElementById("adv-straddle-val"),
        advCorridorVal: document.getElementById("adv-corridor-val"),
        advVixVal: document.getElementById("adv-vix-val"),
        advLotSizeVal: document.getElementById("adv-lotsize-val"),
        advBriefingContent: document.getElementById("adv-briefing-content"),
        advCallCard: document.getElementById("adv-call-card"),
        advPutCard: document.getElementById("adv-put-card"),
        advChatInput: document.getElementById("opt-advisor-chat-input"),
        advChainExpirySelect: document.getElementById("adv-chain-expiry-select"),
        advChainTbody: document.getElementById("adv-chain-tbody"),
        advChainSourceBadge: document.getElementById("adv-chain-source-badge"),

        // Predictions
        predictionsCardsGrid: document.getElementById("predictions-cards-grid"),

        // Scanner Views
        countTabIntraday: document.getElementById("count-tab-intraday"),
        countTabScalping: document.getElementById("count-tab-scalping"),
        countTabBullish: document.getElementById("count-tab-bullish"),
        countTabBearish: document.getElementById("count-tab-bearish"),
        countTabKeylevels: document.getElementById("count-tab-keylevels"),
        scannerTotalCount: document.getElementById("scanner-total-count")
    };

    /* ==========================================================================
       1. DATA FETCHING & TELEMETRY
       ========================================================================== */

    async function fetchIndicesTelemetry() {
        try {
            const res = await fetch("/api/india/indices");
            const data = await res.json();
            if (data && data.indices && el.indicesTrack) {
                renderIndicesTrackDOM(data.indices);
            }
        } catch (err) {
            console.error("Indices fetch error:", err);
        }
    }

    async function fetchFiiDiiData() {
        try {
            const res = await fetch("/api/india/fii_dii");
            const data = await res.json();
            if (data) {
                if (el.fiiCashVal) el.fiiCashVal.textContent = `+₹${data.fii_cash_net_cr.toLocaleString()} Cr`;
                if (el.diiCashVal) el.diiCashVal.textContent = `+₹${data.dii_cash_net_cr.toLocaleString()} Cr`;
                if (el.fiiLongVal) el.fiiLongVal.textContent = `${data.fii_index_futures_long_pct}%`;
                if (el.niftyPcrVal) el.niftyPcrVal.textContent = `${data.fii_index_options_pcr} (Bullish Floor)`;
            }
        } catch (err) {
            console.error("FII/DII fetch error:", err);
        }
    }

    async function fetchScannerData() {
        try {
            const params = new URLSearchParams({
                sector: state.filters.sector,
                market: state.filters.market,
                cpr: state.filters.cpr,
                min_prob: state.filters.minProb,
                sort_by: state.filters.sortBy,
                sort_dir: state.filters.sortDir
            });

            const res = await fetch(`/api/india/scanner?${params.toString()}`);
            const data = await res.json();
            if (data && data.stocks) {
                state.stocks = data.stocks;
                renderScreenerTableDOM();
                if (data.ai_recommended_buys && el.aiBuyNowGrid) {
                    renderAiBuyNowDOM(data.ai_recommended_buys);
                }
            }
        } catch (err) {
            console.error("Scanner fetch error:", err);
        }
    }

    async function fetchStockDossier(symbol) {
        try {
            const res = await fetch(`/api/india/details?symbol=${symbol}&tf=${state.activeTimeframe}`);
            const data = await res.json();
            if (data) {
                renderDossierDOM(data);
            }
        } catch (err) {
            console.error("Stock details fetch error:", err);
        }
    }

    /* ==========================================================================
       2. DOM RENDERING PIPELINE
       ========================================================================== */

    function renderIndicesTrackDOM(indices) {
        let html = "";
        indices.forEach(idx => {
            const isPos = idx.change_pct >= 0;
            const chgColor = isPos ? "var(--neon-bull)" : "var(--neon-bear)";
            const chgSign = isPos ? "+" : "";

            html += `
            <div class="index-pill" onclick="window.openIndiaDossier('${idx.symbol}')">
                <b style="color:#ffffff;">${idx.symbol}</b>
                <span class="mono-num" style="color:#ffffff;">₹${Number(idx.price).toLocaleString(undefined, {minimumFractionDigits:2})}</span>
                <span class="mono-num" style="color:${chgColor}; font-weight:800;">${chgSign}${idx.change_pct}%</span>
                <span class="badge ${idx.cpr_width === 'NARROW_CPR' ? 'badge-cpr-narrow' : 'badge-cpr-wide'}">
                    ${idx.cpr_width === 'NARROW_CPR' ? '⚡ Narrow CPR' : 'Wide CPR'}
                </span>
            </div>`;
        });
        el.indicesTrack.innerHTML = html;
    }

    function renderAiBuyNowDOM(buys) {
        if (!el.aiBuyNowGrid || !buys || buys.length === 0) return;

        let html = "";
        buys.forEach(b => {
            const isPos = b.change_pct >= 0;
            const chgSign = isPos ? "+" : "";

            html += `
            <div class="buy-now-card" onclick="window.openIndiaDossier('${b.symbol}')">
                <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                    <div>
                        <div style="display:flex; align-items:center; gap:6px;">
                            <span style="font-size:16px; font-weight:900; color:#ffffff;">${b.symbol}</span>
                            <span class="badge-grade-a-plus">${b.grade_badge || 'A+'}</span>
                        </div>
                        <div style="font-size:11px; color:var(--text-dim);">${b.name}</div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:15px; font-weight:900; font-family:var(--font-mono); color:#ffffff;">₹${Number(b.price).toLocaleString(undefined, {minimumFractionDigits:2})}</div>
                        <span style="font-size:11px; font-weight:800; color:${isPos ? 'var(--neon-bull)' : 'var(--neon-bear)'};">${chgSign}${b.change_pct}%</span>
                    </div>
                </div>

                <div style="display:flex; justify-content:space-between; align-items:center; font-size:11px;">
                    <span style="color:var(--accent-cyan); font-weight:800; font-family:var(--font-mono);">
                        🔥 ${b.breakout_probability}% AI Probability
                    </span>
                    <span style="color:var(--neon-bull); font-weight:800; font-family:var(--font-mono);">
                        Est. Gain: +${b.expected_gain_pct}%
                    </span>
                </div>

                <div class="buy-now-plan-grid">
                    <div>
                        <div style="color:var(--text-dim); font-size:9px;">Entry Trigger</div>
                        <div style="font-weight:800; color:var(--accent-cyan); font-family:var(--font-mono);">₹${Number(b.entry_zone).toFixed(2)}</div>
                    </div>
                    <div>
                        <div style="color:var(--text-dim); font-size:9px;">Target (TP2)</div>
                        <div style="font-weight:800; color:var(--neon-bull); font-family:var(--font-mono);">₹${Number(b.take_profit_2).toFixed(2)}</div>
                    </div>
                    <div>
                        <div style="color:var(--text-dim); font-size:9px;">Stop Loss</div>
                        <div style="font-weight:800; color:var(--neon-bear); font-family:var(--font-mono);">₹${Number(b.stop_loss).toFixed(2)}</div>
                    </div>
                </div>

                <button class="btn-buy-now-cta" onclick="event.stopPropagation(); window.openIndiaDossier('${b.symbol}')">
                    <span>⚡</span> Open NSE Intelligence Dossier ➔
                </button>
            </div>`;
        });

        el.aiBuyNowGrid.innerHTML = html;
    }

    function renderScreenerTableDOM() {
        if (!el.screenerTableBody) return;

        let list = state.stocks.filter(s => !s.is_index && s.sector !== "Indices");

        // Search query filter
        const q = state.filters.searchQuery.toLowerCase().trim();
        if (q) {
            list = list.filter(s => s.symbol.toLowerCase().includes(q) || s.name.toLowerCase().includes(q));
        }

        state.filteredStocks = list;
        if (el.tableCountBadge) el.tableCountBadge.textContent = `${list.length} Corporate Stocks Analyzed`;

        if (list.length === 0) {
            el.screenerTableBody.innerHTML = `
                <tr>
                    <td colspan="13" style="text-align:center; padding:35px 20px; color:var(--text-secondary);">
                        No Indian equities/indices matched the current filter. Try resetting criteria.
                    </td>
                </tr>`;
            return;
        }

        let html = "";
        list.forEach(st => {
            const isPos = st.change_pct >= 0;
            const chgColor = isPos ? "var(--neon-bull)" : "var(--neon-bear)";
            const chgSign = isPos ? "+" : "";

            let gradeBadge = "badge-grade-a";
            if (st.grade_badge === "A+") gradeBadge = "badge-grade-a-plus";
            else if (st.grade_badge === "B") gradeBadge = "badge-grade-a";

            const cprClass = st.cpr.width_classification === "NARROW_CPR" ? "badge-cpr-narrow" : "badge-cpr-wide";
            const cprText = st.cpr.width_classification === "NARROW_CPR" ? "⚡ Narrow CPR" : "Wide CPR";

            html += `
            <tr onclick="window.openIndiaDossier('${st.symbol}')">
                <td><span class="${gradeBadge}">${st.setup_grade || 'GRADE A'}</span></td>
                <td>
                    <div>
                        <b style="font-size:13px; color:#ffffff;">${st.symbol}</b>
                        <div style="font-size:10px; color:var(--text-dim);">${st.name}</div>
                    </div>
                </td>
                <td>
                    <div style="display:flex; flex-direction:column; gap:2px;">
                        <span class="badge ${cprClass}">${cprText} (${st.cpr.width_pct}%)</span>
                        <span style="font-size:10px; color:var(--text-dim);">P: ₹${st.cpr.pivot}</span>
                    </div>
                </td>
                <td class="mono-num" style="color:var(--neon-bull); font-weight:800;">₹${st.camarilla.h4_breakout}</td>
                <td class="mono-num" style="color:${st.vwap_dist_pct >= 0 ? 'var(--neon-bull)' : 'var(--neon-bear)'};">
                    ₹${st.vwap} (${st.vwap_dist_pct >= 0 ? '+' : ''}${st.vwap_dist_pct}%)
                </td>
                <td class="mono-num" style="color:#ffffff; font-size:13px; font-weight:800;">₹${Number(st.price).toLocaleString(undefined, {minimumFractionDigits:2})}</td>
                <td class="mono-num" style="color:${chgColor}; font-weight:800;">${chgSign}${st.change_pct}%</td>
                <td>
                    <div style="display:flex; align-items:center; gap:6px;">
                        <div style="flex:1; height:6px; background:rgba(30,41,59,0.8); border-radius:3px; overflow:hidden;">
                            <div style="width:${st.breakout_probability}%; height:100%; background:var(--neon-bull);"></div>
                        </div>
                        <span class="mono-num" style="font-weight:800; color:#ffffff; font-size:11px;">${st.breakout_probability}%</span>
                    </div>
                </td>
                <td class="mono-num" style="color:var(--accent-cyan); font-weight:800;">${st.rvol}x</td>
                <td class="mono-num" style="color:var(--text-secondary);">${st.lot_size} sh</td>
                <td><span class="badge badge-strong-buy">${st.opportunity_state}</span></td>
                <td><span style="font-size:11px; font-weight:700; color:var(--text-secondary);">${st.recommendation}</span></td>
                <td style="text-align:right;">
                    <div style="display:inline-flex; gap:4px;" onclick="event.stopPropagation();">
                        <button class="btn-action-tool" style="padding:3px 8px; font-size:10px;" onclick="window.openIndiaDossier('${st.symbol}')">Dossier ➔</button>
                        <button class="btn-action-tool" style="padding:3px 8px; font-size:10px; border-color:var(--neon-bull);" onclick="window.openOptionChainModal('${st.symbol}')">F&O ⚡</button>
                    </div>
                </td>
            </tr>`;
        });

        el.screenerTableBody.innerHTML = html;
    }

    /* ==========================================================================
       3. STOCK DOSSIER MODAL & CHART RENDERING
       ========================================================================== */

    function renderDossierDOM(data) {
        if (!data) return;
        state.activeSymbol = data.symbol;
        state.currentDossier = data;

        if (el.dossierTicker) el.dossierTicker.textContent = data.symbol;
        if (el.dossierOptSym) el.dossierOptSym.textContent = data.symbol;
        if (el.dossierCompany) el.dossierCompany.textContent = `${data.name} • ${data.sector} • ${data.market_cap}`;
        if (el.dossierLivePrice) el.dossierLivePrice.textContent = `₹${Number(data.current_price).toLocaleString(undefined, {minimumFractionDigits:2})}`;
        
        if (el.dossierChangeBadge) {
            const isPos = data.change_pct >= 0;
            el.dossierChangeBadge.textContent = `${isPos ? '+' : ''}${data.change_pct}% (${isPos ? '+₹' : '-₹'}${Math.abs(data.change_val)})`;
            el.dossierChangeBadge.style.color = isPos ? "var(--neon-bull)" : "var(--neon-bear)";
        }

        if (el.dossierProbVal) el.dossierProbVal.textContent = `${data.breakout_probability}%`;
        if (el.dossierProbFill) el.dossierProbFill.style.width = `${data.breakout_probability}%`;
        if (el.dossierCprTag) el.dossierCprTag.textContent = data.cpr.width_label;
        if (el.dossierCamH4) el.dossierCamH4.textContent = `₹${data.camarilla.h4_breakout}`;
        if (el.dossierVwapTag) el.dossierVwapTag.textContent = `${data.vwap_structure.distance_from_vwap_pct >= 0 ? '+' : ''}${data.vwap_structure.distance_from_vwap_pct}% vs VWAP`;
        if (el.dossierLotSize) el.dossierLotSize.textContent = `${data.lot_size} Shares`;

        // Trade Plan
        const plan = data.trade_setup || {};
        if (el.planEntry) el.planEntry.textContent = `₹${Number(plan.entry_zone || data.current_price).toFixed(2)}`;
        if (el.planSl) el.planSl.textContent = `₹${Number(plan.stop_loss || 0).toFixed(2)}`;
        if (el.planTp1) el.planTp1.textContent = `₹${Number(plan.take_profit_1 || 0).toFixed(2)}`;
        if (el.planTp2) el.planTp2.textContent = `₹${Number(plan.take_profit_2 || 0).toFixed(2)}`;
        if (el.planRr) el.planRr.textContent = plan.risk_reward_ratio || "1:2.25";

        // CPR breakdown
        if (el.cprTcVal) el.cprTcVal.textContent = `₹${data.cpr.tc}`;
        if (el.cprPVal) el.cprPVal.textContent = `₹${data.cpr.pivot}`;
        if (el.cprBcVal) el.cprBcVal.textContent = `₹${data.cpr.bc}`;
        if (el.cprWidthVal) el.cprWidthVal.textContent = `${data.cpr.width_pct}% (${data.cpr.width_classification})`;

        // Multi-TF Grid
        if (el.multiTfGrid && data.multi_timeframe) {
            let mHtml = "";
            const tfs = ["5M", "15M", "1H", "1D", "1W"];
            tfs.forEach(tfKey => {
                const item = data.multi_timeframe[tfKey] || { bias: "NEUTRAL", strength: 50 };
                const isBull = item.bias === "BULLISH";
                mHtml += `
                <div style="background:rgba(0,0,0,0.3); border:1px solid var(--border-subtle); border-radius:4px; padding:6px; text-align:center;">
                    <div style="font-size:10px; color:var(--text-dim);">${tfKey}</div>
                    <div style="font-weight:800; font-size:11px; color:${isBull ? 'var(--neon-bull)' : 'var(--neon-bear)'};">${item.bias}</div>
                    <div style="font-size:9px; color:var(--text-dim);">${item.strength}%</div>
                </div>`;
            });
            el.multiTfGrid.innerHTML = mHtml;
        }

        // News Feed
        if (el.newsContainer && data.news) {
            let nHtml = "";
            data.news.forEach(n => {
                nHtml += `
                <div style="border-bottom:1px solid rgba(51,65,85,0.2); padding-bottom:6px; margin-bottom:6px;">
                    <div style="display:flex; justify-content:space-between; font-size:10px; color:var(--text-dim);">
                        <span>${n.source}</span>
                        <span style="color:var(--neon-bull); font-weight:800;">${n.sentiment}</span>
                    </div>
                    <div style="font-size:11px; font-weight:700; color:#ffffff; margin-top:2px;">${n.headline}</div>
                </div>`;
            });
            el.newsContainer.innerHTML = nHtml;
        }

        // Display Modal FIRST so container dimensions are measurable
        if (el.dossierModal) {
            el.dossierModal.style.display = "flex";
            state.dossierOpen = true;
        }

        // Update position size calculator
        window.updatePositionCalculator();

        // Render Chart and Radar after DOM layout computation
        requestAnimationFrame(() => {
            initDossierChart(data.candles, data.cpr, data.camarilla);
            if (state.radarChart) state.radarChart.resize();
        });
    }

    function initDossierChart(candles, cpr, camarilla) {
        if (!el.chartContainer || !candles || candles.length === 0) return;
        if (typeof LightweightCharts === "undefined") return;

        el.chartContainer.innerHTML = "";

        const width = el.chartContainer.clientWidth > 0 ? el.chartContainer.clientWidth : (window.innerWidth <= 900 ? window.innerWidth - 24 : 550);
        const height = el.chartContainer.clientHeight > 0 ? el.chartContainer.clientHeight : (window.innerWidth <= 900 ? 280 : 320);

        const chart = LightweightCharts.createChart(el.chartContainer, {
            width: width,
            height: height,
            layout: {
                background: { color: "#060a14" },
                textColor: "#94a3b8",
                fontSize: 11,
                fontFamily: "'JetBrains Mono', monospace"
            },
            grid: {
                vertLines: { color: "rgba(51, 65, 85, 0.2)" },
                horzLines: { color: "rgba(51, 65, 85, 0.2)" }
            },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            rightPriceScale: { borderColor: "rgba(51, 65, 85, 0.4)" },
            timeScale: { borderColor: "rgba(51, 65, 85, 0.4)", timeVisible: true }
        });

        const candleSeries = chart.addCandlestickSeries({
            upColor: "#00f59b",
            downColor: "#ff3b5c",
            borderUpColor: "#00f59b",
            borderDownColor: "#ff3b5c",
            wickUpColor: "#00f59b",
            wickDownColor: "#ff3b5c"
        });

        const sorted = candles.map(c => ({
            time: typeof c.time === "number" ? c.time : Math.floor(new Date(c.time).getTime() / 1000),
            open: Number(c.open),
            high: Number(c.high),
            low: Number(c.low),
            close: Number(c.close)
        })).sort((a, b) => a.time - b.time);

        candleSeries.setData(sorted);
        chart.timeScale().fitContent();

        // Draw CPR Pivot lines
        if (cpr) {
            candleSeries.createPriceLine({
                price: cpr.tc,
                color: "#00d4ff",
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                title: `CPR TC: ₹${cpr.tc}`
            });
            candleSeries.createPriceLine({
                price: cpr.pivot,
                color: "#fbbf24",
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Solid,
                title: `CPR Pivot: ₹${cpr.pivot}`
            });
            candleSeries.createPriceLine({
                price: cpr.bc,
                color: "#00d4ff",
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                title: `CPR BC: ₹${cpr.bc}`
            });
        }

        // Draw Camarilla H4 breakout line
        if (camarilla && camarilla.h4_breakout) {
            candleSeries.createPriceLine({
                price: camarilla.h4_breakout,
                color: "#00f59b",
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Solid,
                title: `Cam H4 Breakout: ₹${camarilla.h4_breakout}`
            });
        }

        // Auto-resize
        if (window.ResizeObserver) {
            const ro = new ResizeObserver(entries => {
                for (const entry of entries) {
                    if (entry.contentRect && entry.contentRect.width > 0 && entry.contentRect.height > 0) {
                        chart.applyOptions({
                            width: entry.contentRect.width,
                            height: entry.contentRect.height
                        });
                    }
                }
            });
            ro.observe(el.chartContainer);
        }

        state.chartInstance = chart;
        state.candleSeries = candleSeries;
    }

    /* ==========================================================================
       4. POSITION SIZE & BROKER ORDER GENERATOR
       ========================================================================== */

    window.updatePositionCalculator = function () {
        if (!state.currentDossier) return;
        const d = state.currentDossier;
        const equity = parseFloat(el.calcEquity ? el.calcEquity.value : 500000) || 500000;
        const riskPct = parseFloat(el.calcRiskPct ? el.calcRiskPct.value : 1.0) || 1.0;

        const entry = Number(d.trade_setup ? d.trade_setup.entry_zone : d.current_price) || d.current_price;
        const sl = Number(d.trade_setup ? d.trade_setup.stop_loss : (entry * 0.98)) || (entry * 0.98);
        const tp = Number(d.trade_setup ? d.trade_setup.take_profit_2 : (entry * 1.05)) || (entry * 1.05);
        const lotSize = d.lot_size || 100;

        const riskBudgetInr = Math.max(100, equity * (riskPct / 100.0));
        const perShareRisk = Math.max(0.05, Math.abs(entry - sl));
        const perLotRisk = perShareRisk * lotSize;

        const rawLots = Math.floor(riskBudgetInr / perLotRisk);
        const lots = Math.max(1, rawLots);
        const shares = lots * lotSize;

        const totalCapital = shares * entry;
        const actualRisk = shares * perShareRisk;
        const actualProfit = shares * Math.abs(tp - entry);
        const rrRatio = (actualProfit / actualRisk).toFixed(2);

        if (el.calcLots) el.calcLots.textContent = `${lots} Lots`;
        if (el.calcShares) el.calcShares.textContent = `${shares.toLocaleString()} sh`;
        if (el.calcCapital) el.calcCapital.textContent = `₹${totalCapital.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}`;
        if (el.calcLoss) el.calcLoss.textContent = `-₹${actualRisk.toFixed(2)}`;
        if (el.calcProfit) el.calcProfit.textContent = `+₹${actualProfit.toFixed(2)}`;
        if (el.calcRrTag) el.calcRrTag.textContent = `R:R 1:${rrRatio}`;
    };

    window.copyBrokerOrder = function () {
        if (!state.currentDossier) return;
        const d = state.currentDossier;
        const equity = parseFloat(el.calcEquity ? el.calcEquity.value : 500000) || 500000;
        const riskPct = parseFloat(el.calcRiskPct ? el.calcRiskPct.value : 1.0) || 1.0;

        const entry = Number(d.trade_setup ? d.trade_setup.entry_zone : d.current_price) || d.current_price;
        const sl = Number(d.trade_setup ? d.trade_setup.stop_loss : (entry * 0.98)) || (entry * 0.98);
        const tp = Number(d.trade_setup ? d.trade_setup.take_profit_2 : (entry * 1.05)) || (entry * 1.05);
        const lotSize = d.lot_size || 100;

        const riskBudgetInr = Math.max(100, equity * (riskPct / 100.0));
        const perShareRisk = Math.max(0.05, Math.abs(entry - sl));
        const lots = Math.max(1, Math.floor(riskBudgetInr / (perShareRisk * lotSize)));
        const shares = lots * lotSize;

        const ticket = `BUY ${shares} ${d.symbol} (CNC/NRML) @ ₹${entry.toFixed(2)} | SL: ₹${sl.toFixed(2)} | TP: ₹${tp.toFixed(2)} | MaxRisk: ₹${(shares * perShareRisk).toFixed(2)}`;

        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(ticket).then(() => {
                const btn = document.getElementById("btn-copy-broker-order");
                if (btn) {
                    const orig = btn.innerHTML;
                    btn.innerHTML = `<span>✅</span> Order Copied to Clipboard!`;
                    btn.style.background = "var(--neon-bull)";
                    btn.style.color = "#000000";
                    setTimeout(() => {
                        btn.innerHTML = orig;
                        btn.style.background = "";
                        btn.style.color = "";
                    }, 2000);
                }
            });
        } else {
            prompt("Zerodha / Upstox Order Ticket:", ticket);
        }
    };

    /* ==========================================================================
       5. OPTION CHAIN CONTROLLER
       ========================================================================== */

    window.openOptionChainModal = async function (symbol) {
        const sym = symbol || state.activeSymbol || "NIFTY";
        if (el.optModal) {
            el.optModal.style.display = "flex";
            if (el.optChainTicker) el.optChainTicker.textContent = sym;
            if (el.optChainTbody) {
                el.optChainTbody.innerHTML = `<tr><td colspan="11" style="text-align:center; padding:30px; color:var(--text-dim);">Fetching live ${sym} Option Chain and calculating Greeks...</td></tr>`;
            }

            try {
                const res = await fetch(`/api/india/option_chain?symbol=${sym}`);
                const data = await res.json();
                if (data && data.chain) {
                    renderOptionChainDOM(data);
                }
            } catch (err) {
                if (el.optChainTbody) el.optChainTbody.innerHTML = `<tr><td colspan="11" style="color:var(--neon-bear); text-align:center; padding:20px;">Option Chain error: ${err.message}</td></tr>`;
            }
        }
    };

    window.closeOptionChainModal = function () {
        if (el.optModal) el.optModal.style.display = "none";
    };

    function renderOptionChainDOM(data) {
        if (!el.optChainTbody) return;

        if (el.optChainSpotInfo) {
            el.optChainSpotInfo.textContent = `Spot: ₹${Number(data.spot_price).toFixed(2)} • ATM Strike: ${data.atm_strike} • Lot Size: ${data.lot_size} • Expiry: ${data.expiry}`;
        }
        if (el.optMaxPainBadge) el.optMaxPainBadge.textContent = `Max Pain: ${data.max_pain_strike}`;
        if (el.optPcrBadge) el.optPcrBadge.textContent = `PCR: ${data.pcr.pcr_oi} (${data.pcr.sentiment})`;

        let html = "";
        data.chain.forEach(row => {
            const isAtm = row.is_atm;
            const rowClass = isAtm ? "atm-strike-row" : "";
            const call = row.call;
            const put = row.put;

            const callItm = call.is_itm ? "itm-call" : "";
            const putItm = put.is_itm ? "itm-put" : "";

            html += `
            <tr class="${rowClass}">
                <!-- CALLS -->
                <td class="${callItm}" style="color:${call.oi_change_pct >= 0 ? 'var(--neon-bull)' : 'var(--neon-bear)'}; font-weight:700;">
                    ${call.oi_change_pct >= 0 ? '+' : ''}${call.oi_change_pct}%
                </td>
                <td class="${callItm}">${call.oi.toLocaleString()}</td>
                <td class="${callItm}" style="color:var(--text-dim);">${call.volume.toLocaleString()}</td>
                <td class="${callItm}">${call.iv}%</td>
                <td class="${callItm}" style="color:#ffffff; font-weight:800;">₹${call.ltp}</td>

                <!-- STRIKE -->
                <td class="strike-cell">${row.strike}</td>

                <!-- PUTS -->
                <td class="${putItm}" style="color:#ffffff; font-weight:800;">₹${put.ltp}</td>
                <td class="${putItm}">${put.iv}%</td>
                <td class="${putItm}" style="color:var(--text-dim);">${put.volume.toLocaleString()}</td>
                <td class="${putItm}">${put.oi.toLocaleString()}</td>
                <td class="${putItm}" style="color:${put.oi_change_pct >= 0 ? 'var(--neon-bull)' : 'var(--neon-bear)'}; font-weight:700;">
                    ${put.oi_change_pct >= 0 ? '+' : ''}${put.oi_change_pct}%
                </td>
            </tr>`;
        });

        el.optChainTbody.innerHTML = html;
    }

    /* ==========================================================================
       6. AI DEFINED-RISK STRATEGY BUILDER
       ========================================================================== */

    window.openAiStrategyModal = async function (symbol) {
        const sym = symbol || state.activeSymbol || "NIFTY";
        if (el.stratModal) {
            el.stratModal.style.display = "flex";
            if (el.stratModalTicker) el.stratModalTicker.textContent = sym;
            fetchAiStrategy(sym, state.strategyBias);
        }
    };

    window.closeAiStrategyModal = function () {
        if (el.stratModal) el.stratModal.style.display = "none";
    };

    window.setStrategyBias = function (bias, btn) {
        state.strategyBias = bias;
        document.querySelectorAll(".strat-bias-btn").forEach(b => b.classList.remove("active"));
        if (btn) btn.classList.add("active");
        fetchAiStrategy(state.activeSymbol, bias);
    };

    async function fetchAiStrategy(symbol, bias) {
        if (!el.stratPayloadContainer) return;
        el.stratPayloadContainer.innerHTML = `<div style="text-align:center; padding:30px; color:var(--text-dim);">Optimizing defined-risk ${bias} options spread for ${symbol}...</div>`;

        try {
            const res = await fetch(`/api/india/options_ai?symbol=${symbol}&bias=${bias}`);
            const data = await res.json();
            if (data && data.legs) {
                renderStrategyDOM(data);
            }
        } catch (err) {
            el.stratPayloadContainer.innerHTML = `<div style="color:var(--neon-bear); text-align:center; padding:20px;">Strategy generation error: ${err.message}</div>`;
        }
    }

    function renderStrategyDOM(data) {
        let legsHtml = "";
        data.legs.forEach(leg => {
            const isBuy = leg.action === "BUY";
            legsHtml += `
            <div style="background:var(--bg-elevated); border:1px solid var(--border-subtle); border-radius:6px; padding:10px 12px; display:flex; justify-content:space-between; align-items:center;">
                <div style="display:flex; align-items:center; gap:8px;">
                    <span class="badge ${isBuy ? 'badge-strong-buy' : 'badge-prime'}">${leg.action}</span>
                    <b style="color:#ffffff; font-size:13px;">${leg.strike} ${leg.type}</b>
                    <span style="font-size:11px; color:var(--text-dim);">(${leg.expiry})</span>
                </div>
                <div class="mono-num" style="font-size:13px; font-weight:800; color:#ffffff;">
                    ₹${leg.price} (Lot: ${leg.lot_size})
                </div>
            </div>`;
        });

        el.stratPayloadContainer.innerHTML = `
        <div style="display:flex; flex-direction:column; gap:12px;">
            <div style="background:linear-gradient(135deg, rgba(0,245,155,0.08), rgba(0,212,255,0.08)); border:1px solid var(--border-bright); border-radius:8px; padding:12px;">
                <div style="font-size:15px; font-weight:900; color:#ffffff;">${data.strategy_name}</div>
                <div style="font-size:12px; color:var(--text-dim); margin-top:4px;">${data.rationale}</div>
            </div>

            <div style="display:flex; flex-direction:column; gap:8px;">
                ${legsHtml}
            </div>

            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:8px; margin-top:6px;">
                <div class="kpi-item">
                    <span class="kpi-label">Max Profit</span>
                    <span class="kpi-val text-bull">+₹${data.max_profit_inr.toLocaleString()}</span>
                </div>
                <div class="kpi-item">
                    <span class="kpi-label">Max Risk / Loss</span>
                    <span class="kpi-val text-bear">-₹${data.max_loss_inr.toLocaleString()}</span>
                </div>
                <div class="kpi-item">
                    <span class="kpi-label">Risk:Reward</span>
                    <span class="kpi-val text-gold">${data.risk_reward_ratio}</span>
                </div>
                <div class="kpi-item">
                    <span class="kpi-label">Prob of Profit</span>
                    <span class="kpi-val text-cyan">${data.probability_of_profit_pct}%</span>
                </div>
                <div class="kpi-item">
                    <span class="kpi-label">Margin Required</span>
                    <span class="kpi-val text-white">₹${data.estimated_margin_inr.toLocaleString()}</span>
                </div>
            </div>
        </div>`;
    }

    /* ==========================================================================
       7. SECTOR ROTATION HEATMAP CONTROLLER
       ========================================================================== */

    window.toggleSectorHeatmap = function () {
        if (!el.heatmapSection) return;
        state.heatmapOpen = !state.heatmapOpen;
        el.heatmapSection.style.display = state.heatmapOpen ? "flex" : "none";
        if (state.heatmapOpen) {
            fetchSectorHeatmap();
        }
    };

    async function fetchSectorHeatmap() {
        if (!el.heatmapGrid) return;
        try {
            const res = await fetch("/api/india/heatmap");
            const data = await res.json();
            if (data && data.sectors) {
                renderSectorHeatmapDOM(data.sectors);
            }
        } catch (err) {
            console.error("Heatmap fetch error:", err);
        }
    }

    function renderSectorHeatmapDOM(sectors) {
        if (!el.heatmapGrid) return;
        let html = "";
        sectors.forEach(sec => {
            const isPos = sec.avg_change_pct >= 0;
            html += `
            <div class="heatmap-tile" onclick="window.setSectorFilter('${sec.sector}', null)">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <b style="color:#ffffff; font-size:12px;">${sec.sector}</b>
                    <span class="mono-num" style="color:${isPos ? 'var(--neon-bull)' : 'var(--neon-bear)'}; font-weight:800;">
                        ${isPos ? '+' : ''}${sec.avg_change_pct}%
                    </span>
                </div>
                <div style="font-size:10px; color:var(--text-dim); margin-top:4px;">
                    Top Leader: <b style="color:var(--neon-bull);">${sec.top_leader_symbol} (${sec.top_leader_change > 0 ? '+' : ''}${sec.top_leader_change}%)</b>
                </div>
            </div>`;
        });
        el.heatmapGrid.innerHTML = html;
    }

    /* ==========================================================================
       8. GLOBAL WINDOW ACTIONS
       ========================================================================== */

    window.openIndiaDossier = function (symbol) {
        state.activeSymbol = symbol;
        fetchStockDossier(symbol);
    };

    window.closeIndiaDossier = function () {
        if (el.dossierModal) el.dossierModal.style.display = "none";
        state.dossierOpen = false;
    };

    window.getCurrentDossierSymbol = function () {
        return state.activeSymbol || "NIFTY";
    };

    window.setSectorFilter = function (sector, btn) {
        state.filters.sector = sector;
        document.querySelectorAll(".sector-pill").forEach(p => p.classList.remove("active"));
        if (btn) btn.classList.add("active");
        fetchScannerData();
    };

    window.setCprFilter = function (cprType, btn) {
        state.filters.cpr = cprType;
        document.querySelectorAll(".cpr-pill").forEach(p => p.classList.remove("active"));
        if (btn) btn.classList.add("active");
        fetchScannerData();
    };

    window.setProbFilter = function (minProb, btn) {
        state.filters.minProb = minProb;
        document.querySelectorAll(".prob-pill").forEach(p => p.classList.remove("active"));
        if (btn) btn.classList.add("active");
        fetchScannerData();
    };

    window.sortTableBy = function (columnKey) {
        if (state.filters.sortBy === columnKey) {
            state.filters.sortDir = (state.filters.sortDir === "asc") ? "desc" : "asc";
        } else {
            state.filters.sortBy = columnKey;
            state.filters.sortDir = "desc";
        }
        fetchScannerData();
    };

    window.setDossierTf = function (tf, btn) {
        state.activeTimeframe = tf;
        document.querySelectorAll(".stock-tf-btn").forEach(b => b.classList.remove("active"));
        if (btn) btn.classList.add("active");
        fetchStockDossier(state.activeSymbol);
    };

    window.exportIndiaCsv = function () {
        window.location.href = `/api/india/export_csv?sector=${encodeURIComponent(state.filters.sector)}`;
    };

    window.setAutoScanInterval = function (seconds) {
        state.autoScanInterval = parseInt(seconds, 10);
        state.countdown = state.autoScanInterval;
        if (el.scanCountdown) {
            el.scanCountdown.textContent = state.autoScanInterval > 0 ? `${state.countdown}s` : "OFF";
        }
    };

    function startAutoScanTicker() {
        setInterval(() => {
            if (state.autoScanInterval <= 0) return;
            state.countdown--;
            if (state.countdown <= 0) {
                state.countdown = state.autoScanInterval;
                fetchScannerData();
                fetchIndicesTelemetry();
                fetchFiiDiiData();
            }
            if (el.scanCountdown) {
                el.scanCountdown.textContent = `${state.countdown}s`;
            }
        }, 1000);
    }

    /* ==========================================================================
       9. SEARCH CONTROLLER
       ========================================================================== */

    function initSearchController() {
        if (!el.searchInput) return;

        let debounceTimer = null;

        async function doSearch(query) {
            if (query.trim().length > 0) {
                try {
                    const res = await fetch(`/api/india/search?q=${encodeURIComponent(query)}`);
                    const data = await res.json();
                    if (data && data.results && el.autocompleteDropdown) {
                        renderAutocompleteDOM(data.results);
                    }
                } catch (err) {}
            } else if (el.autocompleteDropdown) {
                el.autocompleteDropdown.style.display = "none";
            }
        }

        el.searchInput.addEventListener("input", (e) => {
            const query = e.target.value;
            state.filters.searchQuery = query;
            renderScreenerTableDOM();

            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => doSearch(query), 120);
        });

        el.searchInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                const query = e.target.value.trim().toUpperCase();
                if (query.length > 0) {
                    window.openIndiaDossier(query);
                    if (el.autocompleteDropdown) el.autocompleteDropdown.style.display = "none";
                }
            } else if (e.key === "Escape") {
                if (el.autocompleteDropdown) el.autocompleteDropdown.style.display = "none";
            }
        });

        document.addEventListener("click", (e) => {
            if (el.autocompleteDropdown && !el.searchInput.contains(e.target) && !el.autocompleteDropdown.contains(e.target)) {
                el.autocompleteDropdown.style.display = "none";
            }
        });
    }

    function renderAutocompleteDOM(results) {
        if (!el.autocompleteDropdown) return;
        if (results.length === 0) {
            el.autocompleteDropdown.style.display = "none";
            return;
        }

        let html = "";
        results.forEach(r => {
            html += `
            <div class="autocomplete-item" onclick="window.selectSearchResult('${r.symbol}')">
                <div>
                    <b style="color:#ffffff;">${r.symbol}</b>
                    <span style="font-size:10px; color:var(--text-dim); margin-left:6px;">${r.name}</span>
                </div>
                <span style="font-size:10px; color:var(--neon-saffron);">${r.sector}</span>
            </div>`;
        });

        el.autocompleteDropdown.innerHTML = html;
        el.autocompleteDropdown.style.display = "block";
    }

    window.selectSearchResult = function (symbol) {
        if (el.searchInput) el.searchInput.value = symbol;
        if (el.autocompleteDropdown) el.autocompleteDropdown.style.display = "none";
        state.filters.searchQuery = symbol;
        renderScreenerTableDOM();
        window.openIndiaDossier(symbol);
    };

    /* ==========================================================================
       10. MOBILE DOCK NAVIGATION CONTROLLER
       ========================================================================== */

    /* The dock's global is installed by mobile_dock.js, which loads BEFORE the
       dock markup so the buttons work from first paint. Defining it here instead
       meant the buttons were live ~540 lines of markup before this file ran, and
       an early tap threw "switchMobileIndiaView is not defined" and did nothing.
       Register the real behaviour with that bootstrap; fall back to assigning the
       global so this file still works if the bootstrap is absent. */
    function applyMobileIndiaView(view) {
        /* Record the selection. The resize handler and the init both read
           state.activeMobileView, but nothing ever wrote it - so a resize at
           <=900px silently reverted the user's tab to the default, and a tap made
           before the controller loaded was reverted on DOMContentLoaded.
           india_options.js already did this; india and stocks did not. */
        state.activeMobileView = view;
        const btnBuys = document.getElementById("mob-btn-buys");
        const btnScreener = document.getElementById("mob-btn-screener");
        const btnHeatmap = document.getElementById("mob-btn-heatmap");
        const btnAll = document.getElementById("mob-btn-all");

        if (btnBuys) btnBuys.classList.toggle("active", view === "buys");
        if (btnScreener) btnScreener.classList.toggle("active", view === "screener");
        if (btnHeatmap) btnHeatmap.classList.toggle("active", view === "heatmap");
        if (btnAll) btnAll.classList.toggle("active", view === "all");

        const secBuyNow = document.getElementById("section-buy-now");
        const secScreener = document.getElementById("section-screener");
        const secHeatmap = document.getElementById("sector-heatmap-section");
        const secFilters = document.querySelector(".filter-panel-card");

        if (window.innerWidth <= 900) {
            if (view === "buys") {
                if (secBuyNow) secBuyNow.style.display = "flex";
                if (secFilters) secFilters.style.display = "none";
                if (secScreener) secScreener.style.display = "none";
                if (secHeatmap) secHeatmap.style.display = "none";
            } else if (view === "screener") {
                if (secBuyNow) secBuyNow.style.display = "none";
                if (secFilters) secFilters.style.display = "flex";
                if (secScreener) secScreener.style.display = "block";
                if (secHeatmap) secHeatmap.style.display = "none";
            } else if (view === "heatmap") {
                if (secBuyNow) secBuyNow.style.display = "none";
                if (secFilters) secFilters.style.display = "none";
                if (secScreener) secScreener.style.display = "none";
                if (secHeatmap) {
                    secHeatmap.style.display = "flex";
                    // Same defect as stocks.js: fetchHeatmapData() does not exist, so
                    // this threw a ReferenceError on every Heatmap tap and left the
                    // just-revealed panel empty. The real fetcher is
                    // fetchSectorHeatmap(). The `state.heatmapLoaded` guard was dead —
                    // nothing writes it — so fetch unconditionally, as the toggle does.
                    fetchSectorHeatmap();
                }
            } else if (view === "all") {
                if (secBuyNow) secBuyNow.style.display = "flex";
                if (secFilters) secFilters.style.display = "flex";
                if (secScreener) secScreener.style.display = "block";
                if (secHeatmap) secHeatmap.style.display = "none";
            }
        }
    }

    if (typeof window.registerMobileView === "function") {
        window.registerMobileView("india", applyMobileIndiaView);
    } else {
        window.switchMobileIndiaView = applyMobileIndiaView;
    }

    /* ==========================================================================
       10b. MOBILE FILTER DROPDOWNS

       At <=900px the 15-pill filter row is hidden (it wrapped to 8 rows / 233px at
       360px wide) and replaced by three <select>s. The selects duplicate no filter
       logic: they are built FROM the pills and dispatch by clicking one, so the
       pill's own onclick stays the single owner of state, the .active marker and
       the refetch. The two sets are never visible together, so they cannot drift.
       Same pattern as stocks.js.
       ========================================================================== */

    function buildMobileFilterSelects() {
        document.querySelectorAll(".fd-select").forEach((sel) => {
            const pillSel = sel.getAttribute("data-pills");
            if (!pillSel) return;
            const pills = Array.from(document.querySelectorAll(pillSel));
            if (!pills.length) return;

            const previous = sel.value;
            sel.innerHTML = pills
                .map((p, i) => `<option value="${i}">${p.textContent.trim()}</option>`)
                .join("");

            const activeIdx = pills.findIndex((p) => p.classList.contains("active"));
            if (activeIdx >= 0) sel.value = String(activeIdx);
            else if (previous && pills[Number(previous)]) sel.value = previous;
            else sel.value = "0";
        });
    }

    window.applyMobileFilterSelect = function (selectEl, pillSelector) {
        if (!selectEl) return;
        const pills = Array.from(document.querySelectorAll(pillSelector));
        const pill = pills[parseInt(selectEl.value, 10)];
        if (!pill) return;
        // Let the pill's own handler do everything, exactly as a tap would.
        pill.click();
    };


    /* ==========================================================================
       ADVANCED INDIAN MARKET 2.0 — LIVE CARDS, OPTIONS ADVISOR & PREDICTIONS
       ========================================================================== */

    async function fetchLiveOverviewCards() {
        try {
            const res = await fetch("/api/india/live_indices");
            const data = await res.json();
            if (data && data.cards && data.cards.length) {
                state.overviewCardsData = data.cards;
                renderLiveOverviewCardsDOM(data.cards);
                const clockEl = document.getElementById("live-exchange-clock");
                if (clockEl && data.timestamp) {
                    clockEl.textContent = `NSE / BSE • ${data.timestamp}`;
                }
            }
        } catch (err) {
            console.error("Live overview cards fetch error:", err);
        }
    }

    function renderLiveOverviewCardsDOM(cards) {
        cards.forEach(card => {
            const sym = (card.symbol || "").toUpperCase();
            let cardEl = null;
            if (sym === "NIFTY") cardEl = document.getElementById("card-nifty");
            else if (sym === "SENSEX") cardEl = document.getElementById("card-sensex");
            else if (sym === "BANKNIFTY") cardEl = document.getElementById("card-banknifty");

            if (!cardEl) return;
            cardEl.classList.remove("loading");

            const isBull = card.direction === "BULLISH";
            const isSelected = state.activeOptionsSymbol === sym;
            if (isSelected) {
                cardEl.classList.add("active-selected");
            } else {
                cardEl.classList.remove("active-selected");
            }

            const chgSign = card.change_val >= 0 ? "+" : "";
            const chgColor = isBull ? "var(--neon-bull)" : "var(--neon-bear)";
            const dirClass = isBull ? "card-direction-bullish" : "card-direction-bearish";
            const dirIcon = isBull ? "▲" : "▼";
            const dirLabel = isBull ? "BULLISH" : "BEARISH";

            const low = Number(card.low || card.price * 0.99);
            const high = Number(card.high || card.price * 1.01);
            const price = Number(card.price);
            const rangeSpan = Math.max(0.01, high - low);
            const pipPos = Math.min(100, Math.max(0, ((price - low) / rangeSpan) * 100));

            cardEl.innerHTML = `
                <div class="card-top-row">
                    <div class="card-title-group">
                        <span class="card-symbol-name">${card.name || card.symbol}</span>
                        <span class="card-exchange-tag">${card.exchange}</span>
                    </div>
                    <span class="card-direction-badge ${dirClass}">
                        <span>${dirIcon}</span> ${dirLabel}
                    </span>
                </div>

                <div class="card-price-row">
                    <div class="card-price-value">₹${price.toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
                    <div class="card-change-box">
                        <span class="card-change-pct" style="color:${chgColor};">${chgSign}${Number(card.change_pct).toFixed(2)}%</span>
                        <span class="card-change-val">${chgSign}₹${Math.abs(Number(card.change_val)).toFixed(2)}</span>
                    </div>
                </div>

                <div class="card-range-row">
                    <div class="card-range-labels">
                        <span>L: ₹${low.toLocaleString("en-IN", {minimumFractionDigits: 1})}</span>
                        <span>Day Range</span>
                        <span>H: ₹${high.toLocaleString("en-IN", {minimumFractionDigits: 1})}</span>
                    </div>
                    <div class="card-range-track">
                        <div class="card-range-fill" style="width: 100%;"></div>
                        <div class="card-range-pip" style="left: ${pipPos.toFixed(1)}%;"></div>
                    </div>
                </div>

                <div class="card-action-bar">
                    <span class="card-meta-pill">Lot: ${card.lot_size} &bull; VIX: ${card.vix || '16.5'}</span>
                    <span class="card-cta-link">Options Advisor ➔</span>
                </div>
            `;
        });
    }

    window.selectOptionsAdvisorIndex = async function (symbol) {
        const sym = (symbol || "NIFTY").toUpperCase().trim();
        state.activeOptionsSymbol = sym;

        // Highlight selected overview card and tab
        ["NIFTY", "SENSEX", "BANKNIFTY"].forEach(s => {
            const cardEl = document.getElementById(`card-${s.toLowerCase()}`);
            if (cardEl) {
                if (s === sym) cardEl.classList.add("active-selected");
                else cardEl.classList.remove("active-selected");
            }
            const tabBtn = document.getElementById(`tab-opt-${s.toLowerCase()}`);
            if (tabBtn) {
                if (s === sym) {
                    tabBtn.classList.add("active");
                    tabBtn.setAttribute("aria-selected", "true");
                } else {
                    tabBtn.classList.remove("active");
                    tabBtn.setAttribute("aria-selected", "false");
                }
            }
        });

        await fetchOptionsAdvisorData(sym);
        await fetchOptionChainForAdvisor(sym);

        const advisorSec = document.getElementById("section-options-advisor");
        if (advisorSec) {
            advisorSec.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    };

    async function fetchOptionsAdvisorData(symbol, query = null, strategyType = null) {
        try {
            const sym = symbol || state.activeOptionsSymbol || "NIFTY";
            let url = `/api/india/options/ai_advisor?symbol=${sym}`;
            if (query) url += `&query=${encodeURIComponent(query)}`;
            if (strategyType) url += `&strategy=${encodeURIComponent(strategyType)}`;

            const res = await fetch(url);
            const data = await res.json();
            if (data) {
                state.optionsAdvisorData = data;
                renderOptionsAdvisorDOM(data);
            }
        } catch (err) {
            console.error("Options advisor fetch error:", err);
        }
    }

    function renderOptionsAdvisorDOM(data) {
        if (!data) return;

        const spotEl = document.getElementById("adv-spot-val");
        const atmEl = document.getElementById("adv-atm-val");
        const maxPainEl = document.getElementById("adv-maxpain-val");
        const pcrEl = document.getElementById("adv-pcr-val");
        const straddleEl = document.getElementById("adv-straddle-val");
        const corridorEl = document.getElementById("adv-corridor-val");
        const vixEl = document.getElementById("adv-vix-val");
        const lotEl = document.getElementById("adv-lotsize-val");

        if (spotEl) spotEl.textContent = `₹${Number(data.spot_price).toLocaleString("en-IN", {minimumFractionDigits: 2})}`;
        if (atmEl) atmEl.textContent = Number(data.atm_strike).toLocaleString("en-IN");
        if (maxPainEl) maxPainEl.textContent = Number(data.max_pain).toLocaleString("en-IN");
        if (pcrEl && data.pcr) {
            const pcrVal = Number(data.pcr.pcr_oi || 1.0).toFixed(2);
            pcrEl.textContent = `${pcrVal} (${data.pcr.sentiment || 'Neutral'})`;
            pcrEl.className = data.pcr.pcr_oi >= 1.05 ? "adv-kpi-val text-bull" : (data.pcr.pcr_oi <= 0.85 ? "adv-kpi-val text-bear" : "adv-kpi-val text-gold");
        }
        if (straddleEl && data.atm_straddle) {
            straddleEl.textContent = `₹${Number(data.atm_straddle.premium).toFixed(1)} (±${Number(data.atm_straddle.expected_move_pct).toFixed(1)}%)`;
        }
        if (corridorEl && data.atm_straddle) {
            corridorEl.textContent = `${Number(data.atm_straddle.lower_breakeven).toFixed(0)} – ${Number(data.atm_straddle.upper_breakeven).toFixed(0)}`;
        }
        if (vixEl) vixEl.textContent = `${Number(data.vix || 16.5).toFixed(1)} (${data.vix > 18 ? 'High Vol' : 'Normal'})`;
        if (lotEl) lotEl.textContent = `${data.lot_size} Qty`;

        // Render AI Briefing Card
        const briefingEl = document.getElementById("adv-briefing-content");
        if (briefingEl && data.ai_advisor_analysis) {
            let text = data.ai_advisor_analysis;
            text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
            text = text.replace(/• (.*?)(?=(\n|$))/g, '<div style="margin-bottom:4px;">• $1</div>');
            text = text.replace(/\n\n/g, '<br/>');
            briefingEl.innerHTML = text;
        }

        // Render Recommended Call and Put Cards
        if (data.call_opportunity) {
            const ce = data.call_opportunity;
            const titleEl = document.getElementById("adv-call-title");
            const entryEl = document.getElementById("adv-call-entry");
            const slEl = document.getElementById("adv-call-sl");
            const tpEl = document.getElementById("adv-call-tp");
            const greeksEl = document.getElementById("adv-call-greeks");
            const riskEl = document.getElementById("adv-call-risk");

            if (titleEl) titleEl.textContent = `${data.symbol} ${ce.strike} CE`;
            if (entryEl) entryEl.textContent = ce.entry_range || `₹${ce.ltp}`;
            if (slEl) slEl.textContent = `₹${ce.stop_loss} (-30%)`;
            if (tpEl) tpEl.textContent = `₹${ce.target_1} / ₹${ce.target_2}`;
            if (greeksEl) greeksEl.textContent = `Δ ${ce.delta} • θ ${ce.theta_per_day}`;
            if (riskEl) riskEl.textContent = `₹${Number(ce.max_risk_per_lot).toLocaleString("en-IN")}`;
        }

        if (data.put_opportunity) {
            const pe = data.put_opportunity;
            const titleEl = document.getElementById("adv-put-title");
            const entryEl = document.getElementById("adv-put-entry");
            const slEl = document.getElementById("adv-put-sl");
            const tpEl = document.getElementById("adv-put-tp");
            const greeksEl = document.getElementById("adv-put-greeks");
            const riskEl = document.getElementById("adv-put-risk");

            if (titleEl) titleEl.textContent = `${data.symbol} ${pe.strike} PE`;
            if (entryEl) entryEl.textContent = pe.entry_range || `₹${pe.ltp}`;
            if (slEl) slEl.textContent = `₹${pe.stop_loss} (-30%)`;
            if (tpEl) tpEl.textContent = `₹${pe.target_1} / ₹${pe.target_2}`;
            if (greeksEl) greeksEl.textContent = `Δ ${pe.delta} • θ ${pe.theta_per_day}`;
            if (riskEl) riskEl.textContent = `₹${Number(pe.max_risk_per_lot).toLocaleString("en-IN")}`;
        }
    }

    async function fetchOptionChainForAdvisor(symbol, expiry = null) {
        try {
            const sym = symbol || state.activeOptionsSymbol || "NIFTY";
            let url = `/api/india/option_chain?symbol=${sym}`;
            if (expiry) url += `&expiry=${encodeURIComponent(expiry)}`;

            const res = await fetch(url);
            const data = await res.json();
            if (data && (data.chain || data.strikes)) {
                renderAdvisorOptionChainDOM(data);
            }
        } catch (err) {
            console.error("Option chain fetch error:", err);
        }
    }

    function renderAdvisorOptionChainDOM(chainData) {
        const tbody = document.getElementById("adv-chain-tbody");
        const expirySelect = document.getElementById("adv-chain-expiry-select");
        const sourceBadge = document.getElementById("adv-chain-source-badge");

        if (sourceBadge && chainData.data_source) {
            sourceBadge.textContent = chainData.data_source.toUpperCase();
        }

        if (expirySelect && chainData.available_expiries && chainData.available_expiries.length) {
            let optHtml = "";
            chainData.available_expiries.forEach(exp => {
                const isSel = exp === chainData.expiry ? "selected" : "";
                optHtml += `<option value="${exp}" ${isSel}>${exp}</option>`;
            });
            expirySelect.innerHTML = optHtml;
        }

        if (!tbody) return;
        const rows = chainData.chain || chainData.strikes || [];
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="11" style="text-align:center; padding:20px; color:var(--text-dim);">No option chain strikes available.</td></tr>';
            return;
        }

        let html = "";
        rows.forEach(r => {
            const isAtm = r.is_atm;
            const isMaxPain = r.strike === chainData.max_pain_strike;
            const ce = r.call || {};
            const pe = r.put || {};

            const atmClass = isAtm ? "row-atm" : "";
            const ceItmClass = ce.is_itm ? "itm-call" : "";
            const peItmClass = pe.is_itm ? "itm-put" : "";

            const ceOiChg = Number(ce.oi_change_pct || 0);
            const peOiChg = Number(pe.oi_change_pct || 0);
            const ceOiColor = ceOiChg >= 0 ? "var(--neon-bull)" : "var(--neon-bear)";
            const peOiColor = peOiChg >= 0 ? "var(--neon-bull)" : "var(--neon-bear)";

            html += `
            <tr class="${atmClass}">
                <td class="${ceItmClass}" style="color:${ceOiColor};">${ceOiChg > 0 ? '+' : ''}${ceOiChg.toFixed(0)}%</td>
                <td class="${ceItmClass}">${Number(ce.oi || 0).toLocaleString()}</td>
                <td class="${ceItmClass}">${Number(ce.volume || 0).toLocaleString()}</td>
                <td class="${ceItmClass}">${Number(ce.delta || 0).toFixed(2)}</td>
                <td class="${ceItmClass}" style="font-weight:800; color:#ffffff;">₹${Number(ce.ltp || 0).toFixed(2)}</td>
                
                <td class="strike-cell">
                    ${r.strike}
                    ${isAtm ? ' <span style="font-size:9px; color:var(--neon-gold); font-weight:900;">ATM</span>' : ''}
                    ${isMaxPain ? ' <span style="font-size:9px; color:var(--accent-cyan); font-weight:900;">MP</span>' : ''}
                </td>

                <td class="${peItmClass}" style="font-weight:800; color:#ffffff;">₹${Number(pe.ltp || 0).toFixed(2)}</td>
                <td class="${peItmClass}">${Number(pe.delta || 0).toFixed(2)}</td>
                <td class="${peItmClass}">${Number(pe.volume || 0).toLocaleString()}</td>
                <td class="${peItmClass}">${Number(pe.oi || 0).toLocaleString()}</td>
                <td class="${peItmClass}" style="color:${peOiColor};">${peOiChg > 0 ? '+' : ''}${peOiChg.toFixed(0)}%</td>
            </tr>`;
        });

        tbody.innerHTML = html;
    }

    async function fetchMarketPredictions() {
        try {
            const res = await fetch("/api/india/predictions");
            const data = await res.json();
            if (data) {
                state.predictionsData = data;
                renderPredictionsGridDOM();
            }
        } catch (err) {
            console.error("Market predictions fetch error:", err);
        }
    }

    window.switchPredictionTab = function (horizon) {
        state.activePredictionHorizon = horizon;
        ["intraday", "scalping", "breakouts", "keylevels"].forEach(h => {
            const btn = document.getElementById(`pred-tab-${h}`);
            if (btn) {
                if (h === horizon) btn.classList.add("active");
                else btn.classList.remove("active");
            }
        });
        renderPredictionsGridDOM();
    };

    function renderPredictionsGridDOM() {
        const grid = document.getElementById("predictions-cards-grid");
        if (!grid || !state.predictionsData) return;

        const horizon = state.activePredictionHorizon || "intraday";
        let items = [];
        if (horizon === "intraday") items = state.predictionsData.intraday_setups || [];
        else if (horizon === "scalping") items = state.predictionsData.scalping_setups || [];
        else if (horizon === "breakouts") items = (state.predictionsData.breakouts || []).concat(state.predictionsData.breakdowns || []);
        else if (horizon === "keylevels") items = state.predictionsData.key_levels || [];

        if (!items.length) {
            grid.innerHTML = '<div style="grid-column:1 / -1; text-align:center; padding:30px; color:var(--text-dim);">No setups currently active for this horizon.</div>';
            return;
        }

        let html = "";
        items.forEach(item => {
            const isBuy = item.direction === "BUY";
            const dirIcon = isBuy ? "▲ BUY" : "▼ SELL";
            const badgeClass = isBuy ? "card-direction-bullish" : "card-direction-bearish";
            const conf = item.confidence_score || 80;

            if (horizon === "keylevels") {
                html += `
                <div class="pred-card" onclick="window.openIndiaDossier('${item.symbol}')" style="cursor:pointer;">
                    <div class="pred-card-top">
                        <span class="pred-card-symbol">${item.symbol} &bull; <span style="font-size:12px; font-weight:600; color:var(--text-dim);">${item.name}</span></span>
                        <span class="badge ${item.trend_bias && item.trend_bias.includes('BULLISH') ? 'badge-cpr-narrow' : 'badge-cpr-wide'}">${item.trend_bias || 'NEUTRAL'}</span>
                    </div>
                    <div style="font-size:20px; font-weight:900; font-family:var(--font-mono); color:#ffffff; margin-bottom:10px;">
                        ₹${Number(item.price).toLocaleString("en-IN", {minimumFractionDigits:2})}
                        <span style="font-size:12px; color:${item.change_pct >= 0 ? 'var(--neon-bull)' : 'var(--neon-bear)'};">${item.change_pct >= 0 ? '+' : ''}${item.change_pct}%</span>
                    </div>
                    <div class="pred-levels-grid">
                        <div class="pred-level-item"><span class="pred-level-label">CPR TC:</span><span class="pred-level-val text-cyan">₹${Number(item.cpr_tc).toFixed(1)}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">Pivot (P):</span><span class="pred-level-val text-gold">₹${Number(item.cpr_pivot).toFixed(1)}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">CPR BC:</span><span class="pred-level-val text-cyan">₹${Number(item.cpr_bc).toFixed(1)}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">Cam H4:</span><span class="pred-level-val text-bull">₹${Number(item.cam_h4).toFixed(1)}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">VWAP:</span><span class="pred-level-val text-white">₹${Number(item.vwap).toFixed(1)}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">Cam L4:</span><span class="pred-level-val text-bear">₹${Number(item.cam_l4).toFixed(1)}</span></div>
                    </div>
                </div>`;
            } else {
                html += `
                <div class="pred-card">
                    <div class="pred-card-top">
                        <div>
                            <span class="pred-card-symbol">${item.symbol}</span>
                            <span style="font-size:11.5px; color:var(--text-dim); margin-left:6px;">${item.timeframe}</span>
                        </div>
                        <span class="card-direction-badge ${badgeClass}">${dirIcon}</span>
                    </div>

                    <div class="pred-confidence-bar">
                        <div class="confidence-track">
                            <div class="confidence-fill" style="width:${conf}%;"></div>
                        </div>
                        <span class="confidence-score-text">${conf}% CONFIDENCE</span>
                    </div>

                    <div class="pred-levels-grid">
                        <div class="pred-level-item"><span class="pred-level-label">Entry Zone:</span><span class="pred-level-val text-cyan">${item.entry_zone}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">Stop Loss:</span><span class="pred-level-val text-bear">${item.stop_loss}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">Target 1:</span><span class="pred-level-val text-bull">${item.target_1}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">Target 2:</span><span class="pred-level-val text-bull">${item.target_2}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">Target 3:</span><span class="pred-level-val text-bull">${item.target_3 || 'Runner'}</span></div>
                        <div class="pred-level-item"><span class="pred-level-label">Risk : Reward:</span><span class="pred-level-val text-gold">${item.risk_reward}</span></div>
                    </div>

                    <div class="pred-reasoning-box">
                        <strong>Reasoning:</strong> ${item.key_catalyst_reasoning}
                    </div>
                </div>`;
            }
        });

        grid.innerHTML = html;
    }

    async function fetchScannerViews() {
        try {
            const res = await fetch("/api/india/scanner_views");
            const data = await res.json();
            if (data) {
                state.scannerViewsData = data;
                if (data.counts) {
                    const c = data.counts;
                    const cIntra = document.getElementById("count-tab-intraday");
                    const cScalp = document.getElementById("count-tab-scalping");
                    const cBull = document.getElementById("count-tab-bullish");
                    const cBear = document.getElementById("count-tab-bearish");
                    const cKey = document.getElementById("count-tab-keylevels");
                    const cTot = document.getElementById("scanner-total-count");

                    if (cIntra) cIntra.textContent = c.intraday || 0;
                    if (cScalp) cScalp.textContent = c.scalping || 0;
                    if (cBull) cBull.textContent = c.bullish || 0;
                    if (cBear) cBear.textContent = c.bearish || 0;
                    if (cKey) cKey.textContent = c.key_levels || 0;
                    if (cTot) {
                        const total = (c.intraday || 0) + (c.scalping || 0) + (c.bullish || 0) + (c.bearish || 0) + (c.key_levels || 0);
                        cTot.textContent = `${total} Live Opportunities`;
                    }
                }
                renderScannerTableDOM();
            }
        } catch (err) {
            console.error("Scanner views fetch error:", err);
        }
    }

    window.switchScannerView = function (viewKey) {
        state.activeScannerView = viewKey;
        ["intraday", "scalping", "bullish", "bearish", "keylevels"].forEach(k => {
            const tabBtn = document.getElementById(`scan-tab-${k}`);
            if (tabBtn) {
                const match = (k === "keylevels" && viewKey === "key_levels") || (k === viewKey);
                if (match) tabBtn.classList.add("active");
                else tabBtn.classList.remove("active");
            }
        });
        renderScannerTableDOM();
    };

    function renderScannerTableDOM() {
        const tbody = document.getElementById("screener-table-tbody");
        if (!tbody || !state.scannerViewsData) return;

        const viewKey = state.activeScannerView || "intraday";
        const items = state.scannerViewsData[viewKey] || [];

        if (!items.length) {
            tbody.innerHTML = '<tr><td colspan="10" style="text-align:center; padding:30px; color:var(--text-dim);">No stocks match this scanner view currently.</td></tr>';
            return;
        }

        let html = "";
        items.forEach(s => {
            const isPos = s.change_pct >= 0;
            const chgColor = isPos ? "var(--neon-bull)" : "var(--neon-bear)";
            const chgSign = isPos ? "+" : "";

            html += `
            <tr>
                <td>
                    <div style="font-weight:800; color:#ffffff; cursor:pointer;" onclick="window.openIndiaDossier('${s.symbol}')">${s.symbol}</div>
                    <div style="font-size:11px; color:var(--text-dim);">${s.name || ''} &bull; ${s.sector || ''}</div>
                </td>
                <td style="font-family:var(--font-mono); font-weight:800;">₹${Number(s.price).toLocaleString("en-IN", {minimumFractionDigits:2})}</td>
                <td style="font-family:var(--font-mono); font-weight:800; color:${chgColor};">${chgSign}${Number(s.change_pct).toFixed(2)}%</td>
                <td style="font-family:var(--font-mono);">${Number(s.rvol).toFixed(2)}x</td>
                <td><span class="badge ${s.cpr_classification === 'NARROW_CPR' ? 'badge-cpr-narrow' : 'badge-cpr-wide'}">${s.cpr_classification}</span></td>
                <td style="font-family:var(--font-mono); color:var(--neon-bull);">₹${Number(s.cam_h4).toFixed(1)}</td>
                <td style="font-family:var(--font-mono); color:#ffffff;">₹${Number(s.vwap).toFixed(1)}</td>
                <td><span class="scanner-why-tag">${s.why_identified || 'Quantitative momentum signal'}</span></td>
                <td style="font-family:var(--font-mono);">${s.lot_size}</td>
                <td style="text-align:right;">
                    <button class="btn-action-tool" style="padding:4px 8px; font-size:11px;" onclick="window.openIndiaDossier('${s.symbol}')">Dossier</button>
                    <button class="btn-action-tool" style="padding:4px 8px; font-size:11px;" onclick="window.openOptionChainModal('${s.symbol}')">Options</button>
                </td>
            </tr>`;
        });

        tbody.innerHTML = html;
    }

    window.sendAdvisorPrompt = function (promptKey) {
        const sym = state.activeOptionsSymbol || "NIFTY";
        let queryText = "";
        if (promptKey === "best_call") queryText = `Recommend the highest-probability Call (CE) buying setup for ${sym} today with entry, stop-loss, and target.`;
        else if (promptKey === "best_put") queryText = `Recommend the highest-probability Put (PE) buying setup for ${sym} today with entry, stop-loss, and target.`;
        else if (promptKey === "hero_zero") queryText = `Analyze expiry dynamics and recommend a safe Hero-Zero or 0DTE trade for ${sym}.`;
        else if (promptKey === "spread") queryText = `Formulate an institutional defined-risk spread (Bull Call, Bear Put, or Iron Condor) for ${sym}.`;
        else if (promptKey === "greeks") queryText = `Perform full Delta, Gamma, Theta decay, and IV Rank diagnostic for ${sym}.`;

        const inputEl = document.getElementById("opt-advisor-chat-input");
        if (inputEl) inputEl.value = queryText;
        fetchOptionsAdvisorData(sym, queryText, promptKey);
    };

    window.submitAdvisorQuery = function () {
        const inputEl = document.getElementById("opt-advisor-chat-input");
        if (!inputEl) return;
        const text = inputEl.value.trim();
        if (!text) return;
        const sym = state.activeOptionsSymbol || "NIFTY";
        fetchOptionsAdvisorData(sym, text);
    };

    window.onAdvisorExpiryChange = function (expiry) {
        const sym = state.activeOptionsSymbol || "NIFTY";
        fetchOptionChainForAdvisor(sym, expiry);
    };

    /* ==========================================================================
       11. INITIALIZATION
       ========================================================================== */

    document.addEventListener("DOMContentLoaded", () => {
        initSearchController();
        buildMobileFilterSelects();
        fetchLiveOverviewCards();
        fetchFiiDiiData();
        fetchIndicesTelemetry();
        fetchMarketPredictions();
        fetchScannerViews();
        window.selectOptionsAdvisorIndex("NIFTY");
        startAutoScanTicker();

        // 24/7 Real-Time Polling Loops
        setInterval(fetchLiveOverviewCards, 5000);
        setInterval(fetchMarketPredictions, 15000);
        setInterval(fetchScannerViews, 15000);
        if (el.dossierModal) {
            el.dossierModal.addEventListener("click", (e) => {
                if (e.target === el.dossierModal) {
                    window.closeIndiaDossier();
                }
            });
        }
        if (window.innerWidth <= 900) {
            /* Honour a selection made before this file loaded (the dock is live
               from first paint). Hardcoding "buys" here reverted it. */
            window.switchMobileIndiaView(state.activeMobileView || "buys");
        }
    });

    window.addEventListener("resize", () => {
        // The pills and the dropdowns are swapped by a media query, so whichever
        // set just became visible must reflect the current filter state.
        buildMobileFilterSelects();
        if (window.innerWidth <= 900) {
            window.switchMobileIndiaView(state.activeMobileView || "buys");
        } else {
            const secBuyNow = document.getElementById("section-buy-now");
            const secScreener = document.getElementById("section-screener");
            const secHeatmap = document.getElementById("sector-heatmap-section");
            const secFilters = document.querySelector(".filter-panel-card");
            if (secBuyNow) secBuyNow.style.display = "block";
            if (secFilters) secFilters.style.display = "block";
            if (secScreener) secScreener.style.display = "block";
            if (secHeatmap) secHeatmap.style.display = state.heatmapVisible ? "block" : "none";
        }
    });

})();

