/**
 * HM Algo 2.0 — India F&O Options & Derivatives Terminal Client Controller
 * Renders live interactive payoff curves (Sensibull/Opstra algorithm), strike-wise OI bar visualizers,
 * multi-leg strategy builder, and Zerodha/Upstox basket orders.
 */

(function () {
    'use strict';

    // Application State
    const state = {
        symbol: "NIFTY",
        spotPrice: 24850.0,
        lotSize: 25,
        atmStrike: 24850,
        strikeStep: 50,
        expiry: "CURRENT",
        daysToExpiry: 4.0,
        daysToTarget: 0.0,
        simulatedSpot: 24850.0,
        chainData: null,
        masterTraderAdvisor: null,
        payoffChart: null,
        oiChart: null,
        legs: [
            { action: "BUY", type: "CE", strike: 24850, price: 185.0, lots: 1 },
            { action: "SELL", type: "CE", strike: 24950, price: 110.0, lots: 1 }
        ]
    };

    // DOM Elements Cache
    const el = {
        searchInput: document.getElementById("opt-search-input"),
        autocompleteDropdown: document.getElementById("opt-search-autocomplete"),
        expirySelect: document.getElementById("opt-expiry-select"),
        
        // Spot KPIs
        spotPriceVal: document.getElementById("spot-price-val"),
        atmStrikeVal: document.getElementById("atm-strike-val"),
        maxPainVal: document.getElementById("max-pain-val"),
        pcrVal: document.getElementById("pcr-val"),
        straddlePremVal: document.getElementById("straddle-prem-val"),
        ivRankVal: document.getElementById("iv-rank-val"),
        lotSizeVal: document.getElementById("lot-size-val"),
        chainUndTag: document.getElementById("chain-und-tag"),

        // Sliders
        sliderDays: document.getElementById("slider-days"),
        sliderDaysVal: document.getElementById("slider-days-val"),
        sliderSpot: document.getElementById("slider-spot"),
        sliderSpotVal: document.getElementById("slider-spot-val"),

        // Canvas
        payoffCanvas: document.getElementById("payoff-canvas"),
        oiBarCanvas: document.getElementById("oi-bar-canvas"),

        // Legs Table & KPIs
        legsTbody: document.getElementById("legs-tbody"),
        stratMaxProfit: document.getElementById("strat-max-profit"),
        stratMaxLoss: document.getElementById("strat-max-loss"),
        stratBreakeven: document.getElementById("strat-breakeven"),
        stratPop: document.getElementById("strat-pop"),
        stratNetCost: document.getElementById("strat-net-cost"),
        stratMargin: document.getElementById("strat-margin"),

        // Portfolio Greeks
        portDelta: document.getElementById("port-delta"),
        portGamma: document.getElementById("port-gamma"),
        portTheta: document.getElementById("port-theta"),
        portVega: document.getElementById("port-vega"),

        // Option Chain Table
        fullChainTbody: document.getElementById("full-chain-tbody"),

        // AI Multi-Leg Spreads Grid
        aiOptRecGrid: document.getElementById("ai-opt-rec-grid"),

        // AI Single Entry Option Buying Signals Grid
        aiSingleSignalsGrid: document.getElementById("ai-single-signals-grid")
    };

    /* ==========================================================================
       1. DATA FETCHING & API INTERACTION
       ========================================================================== */

    async function fetchMasterOptionTraderAdvisor(symbol) {
        try {
            const sym = (symbol || state.symbol || "NIFTY").toUpperCase().trim();
            const res = await fetch(`/api/india/options/ai_advisor?symbol=${sym}`);
            const data = await res.json();
            if (data) {
                state.masterTraderAdvisor = data;
                renderMasterTraderHUD(data);
            }
        } catch (err) {
            console.error("Master Option Trader advisor fetch error:", err);
        }
    }

    function renderMasterTraderHUD(data) {
        if (!data) return;

        // 0. Algorithmic Decision Gate: Which One to Select
        if (data.selection_decision) {
            const dec = data.selection_decision;
            const targetBadge = document.getElementById("mot-active-target-badge");
            const decTitle = document.getElementById("mot-decision-title");
            const decDesc = document.getElementById("mot-decision-desc");
            const popVal = document.getElementById("mot-pop-val");
            const ceConvLbl = document.getElementById("mot-ce-conv-lbl");
            const peConvLbl = document.getElementById("mot-pe-conv-lbl");
            const ceConvBar = document.getElementById("mot-conv-bar-ce");
            const peConvBar = document.getElementById("mot-conv-bar-pe");
            const ceTrig = document.getElementById("mot-ce-trigger");
            const peTrig = document.getElementById("mot-pe-trigger");
            const invTrig = document.getElementById("mot-inv-trigger");
            const cardCe = document.getElementById("mot-card-ce");
            const cardPe = document.getElementById("mot-card-pe");

            if (decTitle) decTitle.textContent = dec.decision_summary;
            if (decDesc) decDesc.textContent = dec.decision_reasoning;
            if (popVal) popVal.textContent = dec.trade_expectancy_pop || "70%";

            if (ceConvLbl) ceConvLbl.textContent = `CALL (CE) Conviction: ${dec.ce_conviction_score}%`;
            if (peConvLbl) peConvLbl.textContent = `PUT (PE) Conviction: ${dec.pe_conviction_score}%`;
            if (ceConvBar) ceConvBar.style.width = `${dec.ce_conviction_score}%`;
            if (peConvBar) peConvBar.style.width = `${dec.pe_conviction_score}%`;

            if (ceTrig) ceTrig.textContent = dec.ce_trigger;
            if (peTrig) peTrig.textContent = dec.pe_trigger;
            if (invTrig) invTrig.textContent = dec.invalidation;

            if (dec.selected_side === "CE") {
                if (targetBadge) {
                    targetBadge.textContent = "★ ALGO SELECTS: CALL (CE)";
                    targetBadge.className = "mot-badge-target";
                }
                if (cardCe) cardCe.className = "mot-card mot-card-ce mot-card-active";
                if (cardPe) cardPe.className = "mot-card mot-card-pe mot-card-inactive";
            } else if (dec.selected_side === "PE") {
                if (targetBadge) {
                    targetBadge.textContent = "★ ALGO SELECTS: PUT (PE)";
                    targetBadge.className = "mot-badge-target mot-badge-target-pe";
                }
                if (cardPe) cardPe.className = "mot-card mot-card-pe mot-card-active";
                if (cardCe) cardCe.className = "mot-card mot-card-ce mot-card-inactive";
            } else {
                if (targetBadge) {
                    targetBadge.textContent = "⚠️ AVOID NAKED BUYING (WAIT / SPREAD)";
                    targetBadge.className = "mot-badge-target mot-badge-target-wait";
                }
                if (cardCe) cardCe.className = "mot-card mot-card-ce mot-card-inactive";
                if (cardPe) cardPe.className = "mot-card mot-card-pe mot-card-inactive";
            }
        }

        // 1. Badges
        const verdictEl = document.getElementById("mot-verdict-badge");
        if (verdictEl && data.recommendation) {
            verdictEl.textContent = data.recommendation;
            const rec = data.recommendation.toUpperCase();
            if (rec.startsWith("BUY CALL")) verdictEl.className = "adv-badge-pill badge-verdict-buy";
            else if (rec.startsWith("BUY PUT")) verdictEl.className = "adv-badge-pill badge-verdict-sell";
            else if (rec.includes("HEDGED") || rec.includes("SPREAD")) verdictEl.className = "adv-badge-pill badge-verdict-spread";
            else verdictEl.className = "adv-badge-pill badge-verdict-wait";
        }

        const regimeEl = document.getElementById("mot-regime-badge");
        if (regimeEl && data.market_regime) {
            regimeEl.textContent = `REGIME: ${data.market_regime.replace(/_/g, " ")}`;
        }

        const freshnessEl = document.getElementById("mot-freshness-badge");
        if (freshnessEl && data.data_freshness_status) {
            freshnessEl.textContent = data.data_freshness_status;
            freshnessEl.className = data.data_freshness_status.includes("LIVE")
                ? "adv-badge-pill badge-freshness-live"
                : "adv-badge-pill badge-freshness-warn";
        }

        // 2. CE Card
        const ce = data.call_opportunity;
        if (ce) {
            const ceStrikeEl = document.getElementById("mot-ce-strike");
            const ceEntryEl = document.getElementById("mot-ce-entry");
            const ceSlEl = document.getElementById("mot-ce-sl");
            const ceTp1El = document.getElementById("mot-ce-tp1");
            const ceTp2El = document.getElementById("mot-ce-tp2");
            const ceRiskEl = document.getElementById("mot-ce-risk");
            const ceCapEl = document.getElementById("mot-ce-cap");
            const ceWhyEl = document.getElementById("mot-ce-why");
            const ceDeltaPill = document.getElementById("mot-ce-delta-pill");

            if (ceStrikeEl) ceStrikeEl.textContent = `${data.symbol} ${ce.strike} CE`;
            if (ceEntryEl) ceEntryEl.textContent = ce.entry_range || `₹${ce.ltp}`;
            if (ceSlEl) ceSlEl.textContent = `₹${ce.stop_loss} (-30%)`;
            if (ceTp1El) ceTp1El.textContent = `₹${ce.target_1} (+40%)`;
            if (ceTp2El) ceTp2El.textContent = `₹${ce.target_2} (+80%)`;
            if (ceRiskEl) ceRiskEl.textContent = `₹${Number(ce.max_risk_per_lot).toLocaleString("en-IN")}`;
            if (ceCapEl) ceCapEl.textContent = `₹${Number(ce.capital_per_lot).toLocaleString("en-IN")}`;
            if (ceWhyEl) ceWhyEl.textContent = data.why_recommended || "Delta-optimized strike aligned with momentum.";
            if (ceDeltaPill) ceDeltaPill.textContent = `Δ ${ce.delta} (θ ${ce.theta_per_day}/day)`;
        }

        // 3. PE Card
        const pe = data.put_opportunity;
        if (pe) {
            const peStrikeEl = document.getElementById("mot-pe-strike");
            const peEntryEl = document.getElementById("mot-pe-entry");
            const peSlEl = document.getElementById("mot-pe-sl");
            const peTp1El = document.getElementById("mot-pe-tp1");
            const peTp2El = document.getElementById("mot-pe-tp2");
            const peRiskEl = document.getElementById("mot-pe-risk");
            const peCapEl = document.getElementById("mot-pe-cap");
            const peWhyEl = document.getElementById("mot-pe-why");
            const peDeltaPill = document.getElementById("mot-pe-delta-pill");

            if (peStrikeEl) peStrikeEl.textContent = `${data.symbol} ${pe.strike} PE`;
            if (peEntryEl) peEntryEl.textContent = pe.entry_range || `₹${pe.ltp}`;
            if (peSlEl) peSlEl.textContent = `₹${pe.stop_loss} (-30%)`;
            if (peTp1El) peTp1El.textContent = `₹${pe.target_1} (+40%)`;
            if (peTp2El) peTp2El.textContent = `₹${pe.target_2} (+80%)`;
            if (peRiskEl) peRiskEl.textContent = `₹${Number(pe.max_risk_per_lot).toLocaleString("en-IN")}`;
            if (peCapEl) peCapEl.textContent = `₹${Number(pe.capital_per_lot).toLocaleString("en-IN")}`;
            if (peWhyEl) peWhyEl.textContent = data.why_alternatives_rejected || "Evaluating risk-decay profile and low-probability OTM traps.";
            if (peDeltaPill) peDeltaPill.textContent = `Δ ${pe.delta} (θ ${pe.theta_per_day}/day)`;
        }

        // 4. Matrix Row
        const selCrit = document.getElementById("mot-selection-criteria");
        if (selCrit && data.why_recommended) {
            selCrit.textContent = data.why_recommended;
        }

        const rejCrit = document.getElementById("mot-rejection-criteria");
        if (rejCrit && data.why_alternatives_rejected) {
            rejCrit.textContent = data.why_alternatives_rejected;
        }

        const movCrit = document.getElementById("mot-movement-criteria");
        if (movCrit && data.hedged_alternative) {
            const h = data.hedged_alternative;
            movCrit.innerHTML = `<strong>Hedged Alternative:</strong> ${h.strategy} (${h.wings_range}) &mdash; ${h.description}`;
        }
    }

    window.loadBestStrikeToPayoff = function (type) {
        if (!state.masterTraderAdvisor) {
            console.warn("Master trader advisor data not yet loaded");
            return;
        }
        const opt = type === "CE" ? state.masterTraderAdvisor.call_opportunity : state.masterTraderAdvisor.put_opportunity;
        if (!opt) return;

        state.legs = [
            {
                action: "BUY",
                type: opt.type,
                strike: parseFloat(opt.strike),
                price: parseFloat(opt.ltp),
                lots: 1
            }
        ];

        renderLegsTableDOM();
        recalculatePayoff();

        if (typeof window.switchOptionsView === "function") {
            window.switchOptionsView("payoff");
        }
        const payoffSec = document.getElementById("section-payoff");
        if (payoffSec) {
            payoffSec.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    };

    window.loadPrimaryBestStrikeToPayoff = function () {
        if (!state.masterTraderAdvisor) return;
        const side = state.masterTraderAdvisor.selection_decision
            ? state.masterTraderAdvisor.selection_decision.selected_side
            : "CE";
        window.loadBestStrikeToPayoff(side === "PE" ? "PE" : "CE");
    };

    window.copyActiveBrokerOrder = function () {
        if (!state.masterTraderAdvisor || !state.masterTraderAdvisor.selection_decision) return;
        const dec = state.masterTraderAdvisor.selection_decision;
        const orderText = dec.selected_side === "PE" ? dec.pe_broker_syntax : dec.ce_broker_syntax;
        if (navigator.clipboard && orderText) {
            navigator.clipboard.writeText(orderText).then(() => {
                const btn = document.getElementById("mot-copy-btn");
                if (btn) {
                    const orig = btn.textContent;
                    btn.textContent = "✓ Copied!";
                    setTimeout(() => { btn.textContent = orig; }, 2000);
                }
            }).catch(err => {
                console.error("Clipboard copy error:", err);
            });
        }
    };

    async function fetchSingleOptionSignals() {
        if (!el.aiSingleSignalsGrid) return;
        try {
            const res = await fetch('/api/india/options/single_signals?limit=12');
            const data = await res.json();
            if (data && data.signals && data.signals.length > 0) {
                state.allSingleSignals = data.signals;
                renderSingleSignalsDOM(data.signals);
            }
        } catch (err) {
            console.error("Single option signals fetch error:", err);
        }
    }

    function renderSingleSignalsDOM(signals) {
        if (!el.aiSingleSignalsGrid || !signals || signals.length === 0) return;

        let html = "";
        signals.forEach((s, idx) => {
            const isCall = s.option_type === "CE";
            const cardClass = isCall ? "sig-call" : "sig-put";
            const pillClass = isCall ? "ce" : "pe";
            const tpGain = s.trade_plan.expected_gain_pct;
            const slLoss = s.trade_plan.max_loss_pct;

            html += `
            <div class="single-sig-card ${cardClass}" onclick="window.loadSingleSignalByIndex(${idx})">
                <div class="sig-card-top-row">
                    <div>
                        <div style="display:flex; align-items:center; gap:6px;">
                            <span class="sig-contract-name">${s.contract_symbol}</span>
                            <span class="sig-type-pill ${pillClass}">🟢 ${s.action_label}</span>
                        </div>
                        <div style="font-size:10px; color:var(--text-dim); margin-top:2px;">
                            ${s.name} • Expiry: ${s.expiry} • Lot: ${s.lot_size}
                        </div>
                    </div>
                    <div style="text-align:right;">
                        <span class="badge-grade-a-plus">🔥 ${s.conviction_score}% POP</span>
                        <div style="font-size:10px; color:var(--text-dim); margin-top:2px;">Spot: ₹${Number(s.spot_price).toLocaleString()}</div>
                    </div>
                </div>

                <div class="sig-plan-grid">
                    <div>
                        <div class="sig-plan-item-lbl">Entry Zone</div>
                        <div class="sig-plan-item-val text-cyan">₹${s.trade_plan.entry_premium}</div>
                    </div>
                    <div>
                        <div class="sig-plan-item-lbl">Target (TP1)</div>
                        <div class="sig-plan-item-val text-bull">₹${s.trade_plan.target_1_premium} <span style="font-size:9px;">(+${tpGain}%)</span></div>
                    </div>
                    <div>
                        <div class="sig-plan-item-lbl">Target (TP2)</div>
                        <div class="sig-plan-item-val text-gold">₹${s.trade_plan.target_2_premium}</div>
                    </div>
                    <div>
                        <div class="sig-plan-item-lbl">Stop Loss</div>
                        <div class="sig-plan-item-val text-bear">₹${s.trade_plan.stop_loss_premium} <span style="font-size:9px;">(-${slLoss}%)</span></div>
                    </div>
                </div>

                <div class="sig-capital-row">
                    <span>Capital Required (1 Lot): <b class="text-white">₹${Number(s.trade_plan.capital_required_per_lot_inr).toLocaleString()}</b></span>
                    <span>R:R: <b class="text-bull">${s.trade_plan.risk_reward}</b></span>
                    <span>Delta: <b class="text-cyan">${s.greeks.delta}</b></span>
                </div>

                <div class="sig-rationale-box">
                    ${s.rationale}
                </div>

                <div class="sig-card-actions">
                    <button class="btn-sig-load" onclick="event.stopPropagation(); window.loadSingleSignalByIndex(${idx})">
                        ⚡ Load into Payoff & Chart
                    </button>
                    <button class="btn-sig-order" onclick="event.stopPropagation(); window.copySingleOrderTicketByIndex(${idx})">
                        📋 Copy Order Ticket
                    </button>
                </div>
            </div>`;
        });

        el.aiSingleSignalsGrid.innerHTML = html;
    }

    window.filterSingleSignals = function (filterType, btn) {
        document.querySelectorAll(".sig-filter-btn").forEach(b => b.classList.remove("active"));
        if (btn) btn.classList.add("active");

        if (!state.allSingleSignals) return;

        let filtered = state.allSingleSignals;
        if (filterType === "CE") {
            filtered = state.allSingleSignals.filter(s => s.option_type === "CE");
        } else if (filterType === "PE") {
            filtered = state.allSingleSignals.filter(s => s.option_type === "PE");
        } else if (filterType === "INDEX") {
            filtered = state.allSingleSignals.filter(s => s.is_index);
        } else if (filterType === "EQUITY") {
            filtered = state.allSingleSignals.filter(s => !s.is_index);
        }

        renderSingleSignalsDOM(filtered);
    };

    window.loadSingleSignalByIndex = function (idx) {
        if (!state.allSingleSignals || !state.allSingleSignals[idx]) return;
        const sig = state.allSingleSignals[idx];

        // Switch underlying
        state.symbol = sig.symbol;
        document.querySelectorAll(".und-pill").forEach(p => {
            if (p.textContent.trim().toUpperCase().includes(sig.symbol)) {
                p.classList.add("active");
            } else {
                p.classList.remove("active");
            }
        });

        // Set single leg in strategy builder
        state.legs = [{
            action: "BUY",
            type: sig.option_type,
            strike: sig.strike,
            price: sig.trade_plan.entry_premium,
            lots: 1
        }];

        document.querySelectorAll(".sidebar-inst-btn").forEach(b => {
            const s = (b.getAttribute("data-symbol") || "").toUpperCase();
            b.classList.toggle("active", s === sig.symbol);
        });

        fetchOptionChain(sig.symbol);
        fetchMasterOptionTraderAdvisor(sig.symbol);

        if (typeof window.switchOptionsView === "function") {
            window.switchOptionsView("payoff");
        }
    };

    window.copySingleOrderTicketByIndex = function (idx) {
        if (!state.allSingleSignals || !state.allSingleSignals[idx]) return;
        const sig = state.allSingleSignals[idx];
        const ticket = sig.broker_order_ticket;
        const ticketJson = JSON.stringify(ticket, null, 2);

        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(ticketJson).then(() => {
                alert(`📋 Copied Order Ticket for ${sig.contract_symbol} (Entry ₹${sig.trade_plan.entry_premium}, SL ₹${sig.trade_plan.stop_loss_premium}, TP ₹${sig.trade_plan.target_1_premium})!`);
            });
        } else {
            prompt("Broker Order Ticket JSON:", ticketJson);
        }
    };

    async function fetchOptionRecommendations() {
        if (!el.aiOptRecGrid) return;
        try {
            const res = await fetch('/api/india/options/recommendations');
            const data = await res.json();
            if (data && data.recommendations && data.recommendations.length > 0) {
                renderOptionRecommendationsDOM(data.recommendations);
            }
        } catch (err) {
            console.error("Option recommendations fetch error:", err);
        }
    }

    function renderOptionRecommendationsDOM(recs) {
        if (!el.aiOptRecGrid || !recs || recs.length === 0) return;

        let html = "";
        recs.forEach((r, idx) => {
            const isBull = r.bias === "BULLISH";
            const isBear = r.bias === "BEARISH";
            const dirColor = isBull ? "var(--neon-bull)" : (isBear ? "var(--neon-bear)" : "var(--neon-gold)");
            const dirText = isBull ? "🟢 Bullish" : (isBear ? "🔴 Bearish" : "⚖️ Delta-Neutral");

            html += `
            <div class="opt-rec-card" onclick="window.loadRecommendedStrategyByIndex(${idx})">
                <div class="opt-rec-top-row">
                    <div>
                        <div style="display:flex; align-items:center; gap:6px;">
                            <span class="opt-rec-sym">${r.symbol}</span>
                            <span class="badge badge-nse">${r.badge}</span>
                        </div>
                        <div class="opt-rec-strat-name">${r.strategy_name}</div>
                    </div>
                    <div style="text-align:right;">
                        <span style="font-size:10px; font-weight:800; color:${dirColor};">${dirText}</span>
                        <div style="font-size:10px; color:var(--text-dim);">₹${Number(r.spot_price).toLocaleString()}</div>
                    </div>
                </div>

                <div class="opt-rec-legs-text">
                    ⚡ ${r.legs_desc}
                </div>

                <div class="opt-rec-kpis">
                    <div>
                        <div class="opt-rec-kpi-lbl">Max Profit</div>
                        <div class="opt-rec-kpi-val text-bull">+₹${Number(r.max_profit_inr).toLocaleString()}</div>
                    </div>
                    <div>
                        <div class="opt-rec-kpi-lbl">Max Loss</div>
                        <div class="opt-rec-kpi-val text-bear">-₹${Math.abs(Number(r.max_loss_inr)).toLocaleString()}</div>
                    </div>
                    <div>
                        <div class="opt-rec-kpi-lbl">Win Prob (POP)</div>
                        <div class="opt-rec-kpi-val text-cyan">${r.pop_pct}%</div>
                    </div>
                </div>

                <button class="btn-load-opt-strat" onclick="event.stopPropagation(); window.loadRecommendedStrategyByIndex(${idx})">
                    ⚡ Load Strategy in Payoff & Option Chain
                </button>
            </div>`;
        });

        el.aiOptRecGrid.innerHTML = html;
        state.cachedOptionRecs = recs;
    }

    window.loadRecommendedStrategyByIndex = function (idx) {
        if (!state.cachedOptionRecs || !state.cachedOptionRecs[idx]) return;
        const rec = state.cachedOptionRecs[idx];
        
        // Switch underlying
        state.symbol = rec.symbol;
        document.querySelectorAll(".und-pill").forEach(p => {
            if (p.textContent.trim().toUpperCase().includes(rec.symbol)) {
                p.classList.add("active");
            } else {
                p.classList.remove("active");
            }
        });

        state.legs = rec.legs.map(l => ({
            action: l.action,
            type: l.type,
            strike: l.strike,
            price: l.price,
            lots: l.lots || 1
        }));

        fetchOptionChain(rec.symbol);
    };

    async function fetchOptionChain(symbol) {
        try {
            const res = await fetch(`/api/india/options/chain?symbol=${symbol}`);
            const data = await res.json();
            if (data && data.chain) {
                state.chainData = data;
                state.spotPrice = data.spot_price;
                state.lotSize = data.lot_size;
                state.atmStrike = data.atm_strike;
                state.strikeStep = data.strike_step;
                state.simulatedSpot = data.spot_price;
                state.daysToExpiry = data.expiry_schedule ? data.expiry_schedule.days_to_expiry : 4.0;

                updateSpotKPIs(data);
                renderOptionChainDOM(data);
                renderOiBarChart(data);
                
                // Initialize default legs if first load
                if (state.legs.length === 0 || state.legs[0].strike !== data.atm_strike) {
                    window.applyPresetStrategy("BULLISH");
                } else {
                    recalculatePayoff();
                }
            }
        } catch (err) {
            console.error("Option chain fetch error:", err);
        }
    }

    /* ==========================================================================
       2. KPI & OPTION CHAIN DOM RENDERING
       ========================================================================== */

    function updateSpotKPIs(data) {
        if (el.spotPriceVal) el.spotPriceVal.textContent = `₹${Number(data.spot_price).toLocaleString(undefined, {minimumFractionDigits:2})}`;
        if (el.atmStrikeVal) el.atmStrikeVal.textContent = data.atm_strike.toLocaleString();
        if (el.maxPainVal) el.maxPainVal.textContent = data.max_pain_strike.toLocaleString();
        if (el.pcrVal) el.pcrVal.textContent = `${data.pcr.pcr_oi} (${data.pcr.sentiment})`;
        if (el.straddlePremVal && data.atm_straddle) {
            el.straddlePremVal.textContent = `₹${data.atm_straddle.combined_premium} (±${data.atm_straddle.expected_move_pct}%)`;
        }
        if (el.ivRankVal) el.ivRankVal.textContent = `${data.iv_rank} (IV Rank)`;
        if (el.lotSizeVal) el.lotSizeVal.textContent = `${data.lot_size} Qty`;
        if (el.chainUndTag) el.chainUndTag.textContent = data.symbol;

        if (el.sliderSpot) {
            el.sliderSpot.min = Math.floor(data.spot_price * 0.85);
            el.sliderSpot.max = Math.ceil(data.spot_price * 1.15);
            el.sliderSpot.value = data.spot_price;
            if (el.sliderSpotVal) el.sliderSpotVal.textContent = `₹${Number(data.spot_price).toFixed(2)}`;
        }
    }

    function renderOptionChainDOM(data) {
        if (!el.fullChainTbody) return;

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
                <td class="${callItm} trade-btns-cell">
                    <button class="btn-opt-trade-b" title="Buy Call (Long CE)" onclick="window.addLegFromChain('BUY', 'CE', ${row.strike}, ${call.ltp})">B</button>
                    <button class="btn-opt-trade-s" title="Sell / Write Call (Short CE)" onclick="window.addLegFromChain('SELL', 'CE', ${row.strike}, ${call.ltp})">S</button>
                </td>
                <td class="${callItm}">${call.delta}</td>
                <td class="${callItm}">${call.iv}%</td>
                <td class="${callItm}" style="color:${call.oi_change_pct >= 0 ? 'var(--neon-bull)' : 'var(--neon-bear)'};">${call.oi_change_pct >= 0 ? '+' : ''}${call.oi_change_pct}%</td>
                <td class="${callItm}">${call.oi.toLocaleString()}</td>
                <td class="${callItm}" style="color:#ffffff; font-weight:800;">₹${call.ltp}</td>

                <!-- STRIKE -->
                <td class="strike-cell">${row.strike}</td>

                <!-- PUTS -->
                <td class="${putItm}" style="color:#ffffff; font-weight:800;">₹${put.ltp}</td>
                <td class="${putItm}">${put.oi.toLocaleString()}</td>
                <td class="${putItm}" style="color:${put.oi_change_pct >= 0 ? 'var(--neon-bull)' : 'var(--neon-bear)'};">${put.oi_change_pct >= 0 ? '+' : ''}${put.oi_change_pct}%</td>
                <td class="${putItm}">${put.iv}%</td>
                <td class="${putItm}">${put.delta}</td>
                <td class="${putItm} trade-btns-cell">
                    <button class="btn-opt-trade-b" title="Buy Put (Long PE)" onclick="window.addLegFromChain('BUY', 'PE', ${row.strike}, ${put.ltp})">B</button>
                    <button class="btn-opt-trade-s" title="Sell / Write Put (Short PE)" onclick="window.addLegFromChain('SELL', 'PE', ${row.strike}, ${put.ltp})">S</button>
                </td>
            </tr>`;
        });

        el.fullChainTbody.innerHTML = html;
    }

    /* ==========================================================================
       3. INTERACTIVE MULTI-LEG PAYOFF DIAGRAM & MATH
       ========================================================================== */

    async function recalculatePayoff() {
        renderLegsTableDOM();

        try {
            const legsPayload = encodeURIComponent(JSON.stringify(state.legs));
            const res = await fetch(`/api/india/options/payoff?symbol=${state.symbol}&days_to_target=${state.daysToTarget}&legs=${legsPayload}`);
            const data = await res.json();
            if (data && data.curve_expiry) {
                updatePayoffKPIs(data);
                renderPayoffChart(data);
            }
        } catch (err) {
            console.error("Payoff calculation error:", err);
        }
    }

    function renderLegsTableDOM() {
        if (!el.legsTbody) return;

        let html = "";
        state.legs.forEach((leg, idx) => {
            const isBuy = leg.action.toUpperCase() === "BUY";
            const isCall = leg.type.toUpperCase() === "CE";

            html += `
            <tr>
                <td>
                    <select onchange="window.updateLegField(${idx}, 'action', this.value)">
                        <option value="BUY" ${isBuy ? 'selected' : ''}>BUY</option>
                        <option value="SELL" ${!isBuy ? 'selected' : ''}>SELL</option>
                    </select>
                </td>
                <td>
                    <select onchange="window.updateLegField(${idx}, 'type', this.value)">
                        <option value="CE" ${isCall ? 'selected' : ''}>CE</option>
                        <option value="PE" ${!isCall ? 'selected' : ''}>PE</option>
                    </select>
                </td>
                <td>
                    <input type="number" value="${leg.strike}" step="${state.strikeStep}" style="width:75px;" onchange="window.updateLegField(${idx}, 'strike', parseFloat(this.value))">
                </td>
                <td>
                    <input type="number" value="${leg.price}" step="0.5" style="width:65px;" onchange="window.updateLegField(${idx}, 'price', parseFloat(this.value))">
                </td>
                <td>
                    <input type="number" value="${leg.lots || 1}" min="1" max="50" style="width:45px;" onchange="window.updateLegField(${idx}, 'lots', parseInt(this.value, 10))">
                </td>
                <td style="color:${isCall ? 'var(--neon-bull)' : 'var(--neon-bear)'};">${isCall ? '+0.48' : '-0.52'}</td>
                <td style="color:var(--neon-bear);">-₹18.5</td>
                <td>
                    <button class="btn-remove-leg" onclick="window.removeLeg(${idx})">✕</button>
                </td>
            </tr>`;
        });

        el.legsTbody.innerHTML = html;
    }

    function updatePayoffKPIs(data) {
        if (el.stratMaxProfit) el.stratMaxProfit.textContent = `+₹${Number(data.max_profit_inr).toLocaleString(undefined, {minimumFractionDigits:2})}`;
        if (el.stratMaxLoss) el.stratMaxLoss.textContent = `-₹${Math.abs(Number(data.max_loss_inr)).toLocaleString(undefined, {minimumFractionDigits:2})}`;
        if (el.stratBreakeven) {
            el.stratBreakeven.textContent = data.breakevens && data.breakevens.length > 0 ? data.breakevens.join(", ") : "None";
        }
        if (el.stratPop) el.stratPop.textContent = `${data.probability_of_profit_pct}%`;
        if (el.stratNetCost) {
            const cost = data.net_debit_or_credit_inr;
            el.stratNetCost.textContent = `${cost >= 0 ? 'Debit' : 'Credit'} ₹${Math.abs(cost).toLocaleString(undefined, {minimumFractionDigits:2})}`;
        }
        if (el.stratMargin) {
            let marginHtml = `₹${Number(data.estimated_margin_inr).toLocaleString(undefined, {minimumFractionDigits:2})}`;
            if (data.margin_breakdown && data.margin_breakdown.hedge_benefit_inr > 0) {
                marginHtml += ` <span class="badge-benefit" title="SEBI Margin Relief for Hedged Positions">Save ₹${Number(data.margin_breakdown.hedge_benefit_inr).toLocaleString()}</span>`;
            }
            el.stratMargin.innerHTML = marginHtml;
        }

        if (data.portfolio_greeks) {
            const g = data.portfolio_greeks;
            if (el.portDelta) el.portDelta.textContent = `${g.net_delta > 0 ? '+' : ''}${g.net_delta}`;
            if (el.portGamma) el.portGamma.textContent = `${g.net_gamma > 0 ? '+' : ''}${g.net_gamma}`;
            if (el.portTheta) el.portTheta.textContent = `${g.net_theta_day_inr >= 0 ? '+' : ''}₹${g.net_theta_day_inr}/day`;
            if (el.portVega) el.portVega.textContent = `${g.net_vega_inr >= 0 ? '+' : ''}₹${g.net_vega_inr}`;
        }
    }

    function renderPayoffChart(data) {
        if (!el.payoffCanvas || typeof Chart === "undefined") return;

        const ctx = el.payoffCanvas.getContext("2d");
        if (state.payoffChart) {
            state.payoffChart.destroy();
        }

        const labels = data.spot_grid;
        const expiryData = data.curve_expiry;
        const targetData = data.curve_target;

        state.payoffChart = new Chart(ctx, {
            type: "line",
            data: {
                labels: labels,
                datasets: [
                    {
                        label: "P&L at Expiry",
                        data: expiryData,
                        borderColor: "#00f59b",
                        borderWidth: 2.5,
                        fill: {
                            target: { value: 0 },
                            above: "rgba(0, 245, 155, 0.12)",
                            below: "rgba(255, 59, 92, 0.12)"
                        },
                        tension: 0.1,
                        pointRadius: 0
                    },
                    {
                        label: "P&L at Target Date",
                        data: targetData,
                        borderColor: "#00d4ff",
                        borderWidth: 2,
                        borderDash: [4, 4],
                        fill: false,
                        tension: 0.2,
                        pointRadius: 0
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: "index", intersect: false },
                scales: {
                    x: {
                        grid: { color: "rgba(51, 65, 85, 0.2)" },
                        ticks: { color: "#94a3b8", font: { family: "'JetBrains Mono', monospace", size: 10 } }
                    },
                    y: {
                        grid: { color: "rgba(51, 65, 85, 0.2)" },
                        ticks: {
                            color: "#94a3b8",
                            font: { family: "'JetBrains Mono', monospace", size: 10 },
                            callback: (val) => `₹${val}`
                        }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => `${ctx.dataset.label}: ₹${ctx.raw.toLocaleString()}`
                        }
                    }
                }
            }
        });
    }

    /* ==========================================================================
       4. STRIKE-WISE OI BAR CHART (CHART.JS)
       ========================================================================== */

    function renderOiBarChart(data) {
        if (!el.oiBarCanvas || typeof Chart === "undefined" || !data.chain) return;

        const ctx = el.oiBarCanvas.getContext("2d");
        if (state.oiChart) {
            state.oiChart.destroy();
        }

        const labels = data.chain.map(r => r.strike);
        const callOIs = data.chain.map(r => r.call.oi);
        const putOIs = data.chain.map(r => r.put.oi);

        state.oiChart = new Chart(ctx, {
            type: "bar",
            data: {
                labels: labels,
                datasets: [
                    {
                        label: "Call OI (CE)",
                        data: callOIs,
                        backgroundColor: "rgba(255, 59, 92, 0.65)",
                        borderColor: "#ff3b5c",
                        borderWidth: 1
                    },
                    {
                        label: "Put OI (PE)",
                        data: putOIs,
                        backgroundColor: "rgba(0, 245, 155, 0.65)",
                        borderColor: "#00f59b",
                        borderWidth: 1
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { color: "#94a3b8", font: { family: "'JetBrains Mono', monospace", size: 9 } }
                    },
                    y: {
                        grid: { color: "rgba(51, 65, 85, 0.2)" },
                        ticks: { color: "#94a3b8", font: { family: "'JetBrains Mono', monospace", size: 9 } }
                    }
                },
                plugins: {
                    legend: { display: false }
                }
            }
        });
    }

    /* ==========================================================================
       5. STRATEGY PRESETS & ACTIONS
       ========================================================================== */

    window.selectUnderlying = function (sym, btn) {
        if (!sym) return;
        state.symbol = sym.toUpperCase().trim();
        document.querySelectorAll(".und-pill").forEach(p => {
            const txt = p.textContent.trim().toUpperCase();
            p.classList.toggle("active", txt.includes(state.symbol));
        });
        document.querySelectorAll(".sidebar-inst-btn").forEach(b => {
            const s = (b.getAttribute("data-symbol") || "").toUpperCase();
            b.classList.toggle("active", s === state.symbol);
        });
        state.legs = [];
        fetchOptionChain(state.symbol);
        fetchMasterOptionTraderAdvisor(state.symbol);

        if (window.innerWidth <= 900 && typeof window.toggleOptionsSidebar === "function") {
            window.toggleOptionsSidebar(false);
        }
    };

    window.applyPresetStrategy = async function (presetType) {
        document.querySelectorAll(".preset-btn").forEach(btn => {
            const oc = btn.getAttribute("onclick") || "";
            if (oc.includes(presetType)) {
                btn.classList.add("active");
            } else {
                btn.classList.remove("active");
            }
        });

        try {
            const res = await fetch(`/api/india/options_ai?symbol=${state.symbol}&bias=${presetType}`);
            const data = await res.json();
            if (data && data.legs && data.legs.length > 0) {
                state.legs = data.legs.map(l => ({
                    action: l.action.toUpperCase(),
                    type: l.type.toUpperCase(),
                    strike: parseFloat(l.strike),
                    price: parseFloat(l.price),
                    lots: parseInt(l.lots || 1, 10)
                }));
                recalculatePayoff();
                return;
            }
        } catch (err) {
            console.error("Preset strategy fetch error:", err);
        }

        // Local algorithmic fallback
        const spot = state.spotPrice;
        const step = state.strikeStep;
        const atm = state.atmStrike;

        if (presetType === "BUY_CALL") {
            state.legs = [{ action: "BUY", type: "CE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 }];
        } else if (presetType === "BUY_PUT") {
            state.legs = [{ action: "BUY", type: "PE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 }];
        } else if (presetType === "BULL_PUT_SPREAD") {
            state.legs = [
                { action: "BUY", type: "PE", strike: atm - (3 * step), price: 18.0, lots: 1 },
                { action: "SELL", type: "PE", strike: atm - (1 * step), price: 48.0, lots: 1 }
            ];
        } else if (presetType === "BEAR_CALL_SPREAD") {
            state.legs = [
                { action: "SELL", type: "CE", strike: atm + (1 * step), price: 50.0, lots: 1 },
                { action: "BUY", type: "CE", strike: atm + (3 * step), price: 16.0, lots: 1 }
            ];
        } else if (presetType === "SHORT_STRANGLE") {
            state.legs = [
                { action: "SELL", type: "PE", strike: atm - (2 * step), price: 35.0, lots: 1 },
                { action: "SELL", type: "CE", strike: atm + (2 * step), price: 38.0, lots: 1 }
            ];
        } else if (presetType === "IRON_BUTTERFLY") {
            state.legs = [
                { action: "BUY", type: "PE", strike: atm - (3 * step), price: 18.0, lots: 1 },
                { action: "SELL", type: "PE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 },
                { action: "SELL", type: "CE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 },
                { action: "BUY", type: "CE", strike: atm + (3 * step), price: 19.0, lots: 1 }
            ];
        } else if (presetType === "LONG_STRADDLE") {
            state.legs = [
                { action: "BUY", type: "CE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 },
                { action: "BUY", type: "PE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 }
            ];
        } else if (presetType === "BULL_CALL_SPREAD" || presetType === "BULLISH") {
            state.legs = [
                { action: "BUY", type: "CE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 },
                { action: "SELL", type: "CE", strike: atm + (2 * step), price: roundTo50(spot * 0.008), lots: 1 }
            ];
        } else if (presetType === "BEAR_PUT_SPREAD" || presetType === "BEARISH") {
            state.legs = [
                { action: "BUY", type: "PE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 },
                { action: "SELL", type: "PE", strike: atm - (2 * step), price: roundTo50(spot * 0.008), lots: 1 }
            ];
        } else if (presetType === "SHORT_STRADDLE") {
            state.legs = [
                { action: "SELL", type: "CE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 },
                { action: "SELL", type: "PE", strike: atm, price: roundTo50(spot * 0.018), lots: 1 }
            ];
        } else {
            state.legs = [
                { action: "BUY", type: "PE", strike: atm - (4 * step), price: 12.0, lots: 1 },
                { action: "SELL", type: "PE", strike: atm - (2 * step), price: 42.0, lots: 1 },
                { action: "SELL", type: "CE", strike: atm + (2 * step), price: 45.0, lots: 1 },
                { action: "BUY", type: "CE", strike: atm + (4 * step), price: 14.0, lots: 1 }
            ];
        }
        recalculatePayoff();
    };

    window.quickAddAtm = function (action, type) {
        if (!state.chainData || !state.chainData.chain) return;
        const atm = state.atmStrike;
        const isCall = type.toUpperCase() === "CE";
        const row = state.chainData.chain.find(r => r.strike === atm) || state.chainData.chain[Math.floor(state.chainData.chain.length / 2)];
        const prem = isCall ? row.call.ltp : row.put.ltp;
        window.addLegFromChain(action, type, atm, prem);
    };

    window.addNewLeg = function () {
        state.legs.push({
            action: "BUY",
            type: "CE",
            strike: state.atmStrike,
            price: 50.0,
            lots: 1
        });
        recalculatePayoff();
    };

    window.addLegFromChain = function (action, type, strike, price) {
        state.legs.push({
            action: action.toUpperCase(),
            type: type.toUpperCase(),
            strike: parseFloat(strike),
            price: parseFloat(price),
            lots: 1
        });
        recalculatePayoff();
    };

    window.removeLeg = function (idx) {
        state.legs.splice(idx, 1);
        recalculatePayoff();
    };

    window.updateLegField = function (idx, field, value) {
        if (state.legs[idx]) {
            state.legs[idx][field] = value;
            recalculatePayoff();
        }
    };

    window.onPayoffDaysSlider = function (val) {
        state.daysToTarget = parseFloat(val);
        if (el.sliderDaysVal) {
            el.sliderDaysVal.textContent = state.daysToTarget === 0 ? "0 Days (Expiry)" : `${state.daysToTarget} Days to Target`;
        }
        recalculatePayoff();
    };

    window.onPayoffSpotSlider = function (val) {
        state.simulatedSpot = parseFloat(val);
        if (el.sliderSpotVal) {
            el.sliderSpotVal.textContent = `₹${Number(val).toFixed(2)}`;
        }
    };

    window.copyBrokerBasket = function () {
        // Sort BUY orders first for instant SEBI hedge margin benefit
        const sortedLegs = [...state.legs].sort((a, b) => {
            return (a.action.toUpperCase() === "BUY" ? 0 : 1) - (b.action.toUpperCase() === "BUY" ? 0 : 1);
        });

        const basket = [];
        sortedLegs.forEach(leg => {
            basket.push({
                variety: "regular",
                tradingsymbol: `${state.symbol}${parseInt(leg.strike, 10)}${leg.type.toUpperCase()}`,
                exchange: "NFO",
                transaction_type: leg.action.toUpperCase(),
                order_type: "LIMIT",
                quantity: (leg.lots || 1) * state.lotSize,
                price: parseFloat(leg.price),
                product: "NRML"
            });
        });

        const basketJson = JSON.stringify(basket, null, 2);
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(basketJson).then(() => {
                alert(`📋 Copied Smart-Sequenced Zerodha Kite / Upstox Basket (${state.legs.length} legs) to clipboard!\n(BUY orders prioritized first for SEBI margin benefit)`);
            });
        } else {
            prompt("Zerodha Kite Basket JSON (BUY orders prioritized):", basketJson);
        }
    };

    function roundTo50(num) {
        return Math.round(num * 2.0) / 2.0;
    }

    /* ==========================================================================
       6. WORKSPACE VIEWPORT & DYNAMIC SIDEBAR CONTROLLER
       ========================================================================== */

    window.toggleOptionsSidebar = function (forceState) {
        const sidebar = document.getElementById("options-sidebar");
        const appShell = document.getElementById("options-app-shell");
        const backdrop = document.getElementById("options-sidebar-backdrop");
        if (!sidebar || !appShell) return;

        const isMobile = window.innerWidth <= 900;
        if (isMobile) {
            const shouldOpen = typeof forceState === "boolean"
                ? forceState
                : !sidebar.classList.contains("mobile-open");
            sidebar.classList.toggle("mobile-open", shouldOpen);
            if (backdrop) backdrop.classList.toggle("active", shouldOpen);
            document.body.style.overflow = shouldOpen ? "hidden" : "";
        } else {
            const isCollapsed = typeof forceState === "boolean"
                ? !forceState
                : !sidebar.classList.contains("collapsed");
            sidebar.classList.toggle("collapsed", isCollapsed);
            appShell.classList.toggle("sidebar-collapsed", isCollapsed);
            try {
                localStorage.setItem("options_sidebar_collapsed", isCollapsed ? "true" : "false");
            } catch (e) {}
            setTimeout(() => {
                if (state.payoffChart) state.payoffChart.resize();
                if (state.oiChart) state.oiChart.resize();
            }, 300);
        }
    };

    window.switchOptionsView = function (view) {
        const targetView = (view || "advisor").toLowerCase();
        state.activeView = targetView;
        state.activeMobileView = targetView;
        try {
            localStorage.setItem("options_active_view", targetView);
        } catch (e) {}

        // 1. Update Sidebar Nav Items
        const navIds = ["advisor", "singles", "spreads", "payoff", "chain", "all"];
        navIds.forEach(id => {
            const navBtn = document.getElementById(`nav-item-${id}`);
            if (navBtn) navBtn.classList.toggle("active", id === targetView);
        });

        // 2. Update Top Tabs
        navIds.forEach(id => {
            const tabBtn = document.getElementById(`tab-btn-${id}`);
            if (tabBtn) {
                const isActive = id === targetView;
                tabBtn.classList.toggle("active", isActive);
                tabBtn.setAttribute("aria-selected", isActive ? "true" : "false");
            }
        });

        // 3. Update Mobile Dock
        navIds.forEach(id => {
            const mobBtn = document.getElementById(`mob-btn-${id}`);
            if (mobBtn) {
                const isActive = id === targetView;
                mobBtn.classList.toggle("active", isActive);
                mobBtn.setAttribute("aria-selected", isActive ? "true" : "false");
            }
        });

        // 4. Viewport Panes Visibility
        const secAdvisor = document.getElementById("section-master-trader-strike");
        const secSingles = document.getElementById("section-singles");
        const secSpreads = document.getElementById("section-spreads");
        const secPayoff = document.getElementById("section-payoff");
        const secChain = document.getElementById("section-chain");
        const optWorkspaceGrid = document.querySelector(".opt-workspace-grid") || document.getElementById("opt-workspace-grid");

        if (targetView === "advisor") {
            if (secAdvisor) secAdvisor.style.display = "block";
            if (secSingles) secSingles.style.display = "none";
            if (secSpreads) secSpreads.style.display = "none";
            if (optWorkspaceGrid) optWorkspaceGrid.style.display = "none";
            if (secPayoff) secPayoff.style.display = "none";
            if (secChain) secChain.style.display = "none";
        } else if (targetView === "singles") {
            if (secAdvisor) secAdvisor.style.display = "none";
            if (secSingles) secSingles.style.display = "block";
            if (secSpreads) secSpreads.style.display = "none";
            if (optWorkspaceGrid) optWorkspaceGrid.style.display = "none";
            if (secPayoff) secPayoff.style.display = "none";
            if (secChain) secChain.style.display = "none";
        } else if (targetView === "spreads") {
            if (secAdvisor) secAdvisor.style.display = "none";
            if (secSingles) secSingles.style.display = "none";
            if (secSpreads) secSpreads.style.display = "block";
            if (optWorkspaceGrid) optWorkspaceGrid.style.display = "none";
            if (secPayoff) secPayoff.style.display = "none";
            if (secChain) secChain.style.display = "none";
        } else if (targetView === "payoff") {
            if (secAdvisor) secAdvisor.style.display = "none";
            if (secSingles) secSingles.style.display = "none";
            if (secSpreads) secSpreads.style.display = "none";
            if (optWorkspaceGrid) {
                optWorkspaceGrid.style.display = "grid";
                optWorkspaceGrid.style.gridTemplateColumns = "1fr";
            }
            if (secPayoff) {
                secPayoff.style.display = "flex";
                secPayoff.style.width = "100%";
                setTimeout(() => {
                    if (state.payoffChart) state.payoffChart.resize();
                    if (state.oiChart) state.oiChart.resize();
                }, 60);
            }
            if (secChain) secChain.style.display = "none";
        } else if (targetView === "chain") {
            if (secAdvisor) secAdvisor.style.display = "none";
            if (secSingles) secSingles.style.display = "none";
            if (secSpreads) secSpreads.style.display = "none";
            if (optWorkspaceGrid) {
                optWorkspaceGrid.style.display = "grid";
                optWorkspaceGrid.style.gridTemplateColumns = "1fr";
            }
            if (secPayoff) secPayoff.style.display = "none";
            if (secChain) {
                secChain.style.display = "flex";
                secChain.style.width = "100%";
            }
        } else if (targetView === "all") {
            if (secAdvisor) secAdvisor.style.display = "block";
            if (secSingles) secSingles.style.display = "block";
            if (secSpreads) secSpreads.style.display = "block";
            if (optWorkspaceGrid) {
                optWorkspaceGrid.style.display = "grid";
                optWorkspaceGrid.style.gridTemplateColumns = window.innerWidth <= 1100 ? "1fr" : "1.1fr 1fr";
            }
            if (secPayoff) {
                secPayoff.style.display = "flex";
                secPayoff.style.width = "auto";
                setTimeout(() => {
                    if (state.payoffChart) state.payoffChart.resize();
                    if (state.oiChart) state.oiChart.resize();
                }, 60);
            }
            if (secChain) {
                secChain.style.display = "flex";
                secChain.style.width = "auto";
            }
        }

        // Close mobile drawer if open
        if (window.innerWidth <= 900) {
            window.toggleOptionsSidebar(false);
        }
    };

    function applyMobileOptionsView(view) {
        window.switchOptionsView(view);
    }

    if (typeof window.registerMobileView === "function") {
        window.registerMobileView("options", applyMobileOptionsView);
    } else {
        window.switchMobileOptionsView = applyMobileOptionsView;
    }

    /* ==========================================================================
       7. INITIALIZATION & SEARCH
       ========================================================================== */

    document.addEventListener("DOMContentLoaded", () => {
        // Restore sidebar collapse preference on desktop
        try {
            const savedCollapsed = localStorage.getItem("options_sidebar_collapsed") === "true";
            if (savedCollapsed && window.innerWidth > 900) {
                const sidebar = document.getElementById("options-sidebar");
                const appShell = document.getElementById("options-app-shell");
                if (sidebar) sidebar.classList.add("collapsed");
                if (appShell) appShell.classList.add("sidebar-collapsed");
            }
        } catch (e) {}

        const urlSym = new URLSearchParams(window.location.search).get("symbol");
        if (urlSym) {
            state.symbol = urlSym.toUpperCase().trim();
        }
        const activeSym = state.symbol || "NIFTY";

        document.querySelectorAll(".und-pill").forEach(p => {
            p.classList.toggle("active", p.textContent.trim().toUpperCase().includes(activeSym));
        });
        document.querySelectorAll(".sidebar-inst-btn").forEach(b => {
            const s = (b.getAttribute("data-symbol") || "").toUpperCase();
            b.classList.toggle("active", s === activeSym);
        });

        fetchOptionChain(activeSym);
        fetchMasterOptionTraderAdvisor(activeSym);
        fetchSingleOptionSignals();
        fetchOptionRecommendations();

        // Restore active view (default to "all" on desktop, "advisor" on mobile)
        let savedView = window.innerWidth > 900 ? "all" : "advisor";
        try {
            const stored = localStorage.getItem("options_active_view");
            if (stored) savedView = stored;
        } catch (e) {}
        window.switchOptionsView(savedView);
    });

    window.addEventListener("resize", () => {
        if (state.payoffChart) state.payoffChart.resize();
        if (state.oiChart) state.oiChart.resize();

        if (state.activeView) {
            window.switchOptionsView(state.activeView);
        }
    });

})();
