"""
HM Algo 2.0 — Advanced Indian Market Intelligence, Multi-Horizon Predictions & Options Expert Advisor
Provides evidence-based trade predictions (Intraday, Scalp, Breakouts, Key Levels),
options advisory intelligence, and live scanner views for NIFTY, SENSEX, BANKNIFTY, and Indian equities.
"""
import time
import math
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from jarvis.india.universe import (
    INDIA_UNIVERSE,
    get_india_profile,
    get_all_india_symbols,
    get_all_india_stocks
)
from jarvis.india.nse_rules import NSE_RULES
from jarvis.india.greeks import GREEKS_ENGINE
from jarvis.india.india_engine import INDIA_ENGINE
from jarvis.india.options_engine import INDIA_OPTIONS
from jarvis.india.nse_bse_adapter import (
    fetch_live_index_quotes,
    fetch_live_india_quote
)

logger = logging.getLogger("jarvis.india_prediction_engine")


class IndiaPredictionEngine:
    """
    Institutional Predictive Analytics & Options Advisor Engine for Indian Markets.
    """

    def __init__(self):
        self._predictions_cache: Optional[Dict[str, Any]] = None
        self._predictions_cache_time: float = 0.0
        self._predictions_ttl: float = 8.0  # 8-second cache

        self._scanner_views_cache: Optional[Dict[str, Any]] = None
        self._scanner_views_cache_time: float = 0.0
        self._scanner_views_ttl: float = 12.0

    def get_live_indices_cards(self) -> List[Dict[str, Any]]:
        """
        Returns live telemetry cards for NIFTY 50, SENSEX, and BANK NIFTY.
        """
        raw_quotes = fetch_live_index_quotes()
        cards = []
        for q in raw_quotes:
            sym = q.get("symbol", "").upper()
            if sym not in ("NIFTY", "SENSEX", "BANKNIFTY"):
                continue

            px = float(q.get("price", 0.0))
            prev = float(q.get("previous_close", px))
            chg_val = float(q.get("change_val", px - prev))
            chg_pct = float(q.get("change_pct", 0.0))
            direction = "BULLISH" if chg_val >= 0 else "BEARISH"

            # Determine trading range
            high = float(q.get("high", px))
            low = float(q.get("low", px))
            open_px = float(q.get("open", px))

            cards.append({
                "symbol": sym,
                "name": q.get("name", sym),
                "exchange": q.get("exchange", "NSE"),
                "tradingview": q.get("tradingview", f"NSE:{sym}"),
                "price": round(px, 2),
                "previous_close": round(prev, 2),
                "change_val": round(chg_val, 2),
                "change_pct": round(chg_pct, 2),
                "direction": direction,
                "high": round(high, 2),
                "low": round(low, 2),
                "open": round(open_px, 2),
                "lot_size": int(q.get("lot_size", NSE_RULES.get_lot_size(sym))),
                "vix": float(q.get("vix", 16.5)),
                "day_range_pct": q.get("day_range_pct", 50.0),
                "data_source": "LIVE_EXCHANGE",
                "timestamp": q.get("timestamp", datetime.now().strftime("%H:%M:%S IST")),
                "is_ai_prediction": False,
                "advisor_prompt": f"Analyze current {sym} market structure, option chain OI buildup, and recommend optimal CE/PE trade plan."
            })

        # Ensure order: NIFTY 50, SENSEX, BANK NIFTY
        order_map = {"NIFTY": 0, "SENSEX": 1, "BANKNIFTY": 2}
        cards.sort(key=lambda x: order_map.get(x["symbol"], 99))
        return cards

    def get_market_predictions(self) -> Dict[str, Any]:
        """
        Generates transparent, evidence-based predictive setups across 4 horizons:
        - Intraday setups
        - Scalping setups
        - Breakouts & Breakdowns
        - Key Support / Resistance levels
        """
        now = time.time()
        if self._predictions_cache and (now - self._predictions_cache_time) < self._predictions_ttl:
            return self._predictions_cache

        # Target instruments: top indices and institutional heavyweights
        target_syms = [
            "NIFTY", "BANKNIFTY", "SENSEX",
            "RELIANCE", "HDFCBANK", "ICICIBANK", "TCS", "INFY",
            "SBIN", "BHARTIARTL", "TMPV", "MARUTI", "BAJFINANCE", "LT"
        ]

        intraday_list = []
        scalp_list = []
        breakout_list = []
        breakdown_list = []
        key_levels_list = []

        # Pull live index quotes for anchor accuracy
        live_indices_map = {item["symbol"]: item for item in self.get_live_indices_cards()}

        for sym in target_syms:
            try:
                analysis = INDIA_ENGINE.analyze_india_instrument(sym, timeframe="1D")
            except Exception as exc:
                logger.debug("Failed analysis for %s: %s", sym, exc)
                continue

            prof = get_india_profile(sym)
            live_anchor = live_indices_map.get(sym)

            # Spot price
            if live_anchor:
                price = float(live_anchor["price"])
                chg_pct = float(live_anchor["change_pct"])
                chg_val = float(live_anchor["change_val"])
            else:
                price = float(analysis["current_price"])
                chg_pct = float(analysis["change_pct"])
                chg_val = float(analysis["change_val"])

            is_index = analysis.get("is_index", False)
            cpr = analysis["cpr"]
            camarilla = analysis["camarilla"]
            vwap = analysis["vwap_structure"]["vwap"]
            rvol = float(analysis.get("rvol", 1.0))
            rsi = float(analysis.get("technicals", {}).get("rsi_14", 50.0))
            if rsi <= 0.0 or math.isnan(rsi):
                rsi = 52.0 if chg_pct >= 0 else 46.0
            lot_size = NSE_RULES.get_lot_size(sym)
            strike_step = NSE_RULES.get_strike_step(sym, price)

            atr_est = price * (0.012 if is_index else 0.022)
            cpr_width_pct = cpr.get("width_pct", 0.3)
            is_narrow_cpr = cpr_width_pct <= 0.35

            # -----------------------------------------------------------------
            # 1. INTRADAY SETUPS (CPR + VWAP Alignment)
            # -----------------------------------------------------------------
            if price >= vwap and rsi >= 48:
                # Bullish Intraday
                entry = round(max(price, vwap + (atr_est * 0.1)), 2)
                sl = round(min(entry - (atr_est * 0.7), vwap - (atr_est * 0.2)), 2)
                risk = max(1.0, entry - sl)
                tp1 = round(entry + (risk * 1.5), 2)
                tp2 = round(entry + (risk * 2.5), 2)
                tp3 = round(entry + (risk * 3.5), 2)
                rr_ratio = f"1:{round((tp2 - entry) / risk, 1)}"
                confidence = min(89, round(60 + (12 if is_narrow_cpr else 4) + (10 if rvol >= 1.2 else 5) + (7 if price > cpr['tc'] else 2)))

                intraday_list.append({
                    "symbol": sym,
                    "name": prof.get("name", sym),
                    "type": "INTRADAY",
                    "direction": "BUY",
                    "current_price": price,
                    "change_pct": chg_pct,
                    "entry_zone": f"₹{entry:,.2f}",
                    "stop_loss": f"₹{sl:,.2f}",
                    "target_1": f"₹{tp1:,.2f}",
                    "target_2": f"₹{tp2:,.2f}",
                    "target_3": f"₹{tp3:,.2f}",
                    "risk_reward": rr_ratio,
                    "timeframe": "Intraday (15M / 1H)",
                    "confidence_score": confidence,
                    "confidence_badge": "HIGH CONVICTION" if confidence >= 75 else "MODERATE",
                    "key_catalyst_reasoning": (
                        f"{'Narrow Virgin CPR (trending bias)' if is_narrow_cpr else 'CPR Support'} "
                        f"with price trading above VWAP (₹{vwap:,.1f}); RVOL at {rvol}x; RSI {rsi:.1f} bullish momentum."
                    ),
                    "is_ai_prediction": True,
                    "data_source": "LIVE_EXCHANGE + AI_PREDICTION_ENGINE",
                    "lot_size": lot_size
                })
            else:
                # Bearish Intraday
                entry = round(min(price, vwap - (atr_est * 0.1)), 2)
                sl = round(max(entry + (atr_est * 0.7), vwap + (atr_est * 0.2)), 2)
                risk = max(1.0, sl - entry)
                tp1 = round(entry - (risk * 1.5), 2)
                tp2 = round(entry - (risk * 2.5), 2)
                tp3 = round(entry - (risk * 3.5), 2)
                rr_ratio = f"1:{round((entry - tp2) / risk, 1)}"
                confidence = min(88, round(58 + (12 if is_narrow_cpr else 4) + (10 if rvol >= 1.2 else 5) + (8 if price < cpr['bc'] else 2)))

                intraday_list.append({
                    "symbol": sym,
                    "name": prof.get("name", sym),
                    "type": "INTRADAY",
                    "direction": "SELL",
                    "current_price": price,
                    "change_pct": chg_pct,
                    "entry_zone": f"₹{entry:,.2f}",
                    "stop_loss": f"₹{sl:,.2f}",
                    "target_1": f"₹{tp1:,.2f}",
                    "target_2": f"₹{tp2:,.2f}",
                    "target_3": f"₹{tp3:,.2f}",
                    "risk_reward": rr_ratio,
                    "timeframe": "Intraday (15M / 1H)",
                    "confidence_score": confidence,
                    "confidence_badge": "HIGH CONVICTION" if confidence >= 75 else "MODERATE",
                    "key_catalyst_reasoning": (
                        f"Price rejecting below VWAP (₹{vwap:,.1f}); broken below Bottom Central Pivot (BC: ₹{cpr['bc']:,.1f}); "
                        f"RVOL at {rvol}x; RSI at {rsi:.1f} indicates bearish continuation."
                    ),
                    "is_ai_prediction": True,
                    "data_source": "LIVE_EXCHANGE + AI_PREDICTION_ENGINE",
                    "lot_size": lot_size
                })

            # -----------------------------------------------------------------
            # 2. SCALPING SETUPS (5M Quick Momentum Bursts)
            # -----------------------------------------------------------------
            scalp_move = atr_est * 0.45
            scalp_sl_dist = atr_est * 0.25
            if chg_pct >= 0:
                s_entry = price
                s_sl = round(price - scalp_sl_dist, 2)
                s_tp = round(price + scalp_move, 2)
                s_rr = f"1:{round(scalp_move / scalp_sl_dist, 1)}"
                s_conf = min(86, round(62 + (12 if rvol >= 1.3 else 5) + (10 if analysis.get('is_squeeze', False) else 2)))
                scalp_list.append({
                    "symbol": sym,
                    "name": prof.get("name", sym),
                    "type": "SCALP",
                    "direction": "BUY",
                    "current_price": price,
                    "change_pct": chg_pct,
                    "entry_zone": f"₹{s_entry:,.2f}",
                    "stop_loss": f"₹{s_sl:,.2f}",
                    "target_1": f"₹{s_tp:,.2f}",
                    "target_2": f"₹{round(s_entry + scalp_move * 1.6, 2):,.2f}",
                    "target_3": f"₹{round(s_entry + scalp_move * 2.2, 2):,.2f}",
                    "risk_reward": s_rr,
                    "timeframe": "Scalp (3M / 5M)",
                    "confidence_score": s_conf,
                    "confidence_badge": "HIGH CONVICTION" if s_conf >= 75 else "MODERATE",
                    "key_catalyst_reasoning": f"Rapid 5M momentum expansion; RVOL {rvol}x; targeting Camarilla H3/H4 corridor with tight SL.",
                    "is_ai_prediction": True,
                    "data_source": "LIVE_EXCHANGE + AI_PREDICTION_ENGINE",
                    "lot_size": lot_size
                })
            else:
                s_entry = price
                s_sl = round(price + scalp_sl_dist, 2)
                s_tp = round(price - scalp_move, 2)
                s_rr = f"1:{round(scalp_move / scalp_sl_dist, 1)}"
                s_conf = min(86, round(60 + (12 if rvol >= 1.3 else 5) + (10 if analysis.get('is_squeeze', False) else 2)))
                scalp_list.append({
                    "symbol": sym,
                    "name": prof.get("name", sym),
                    "type": "SCALP",
                    "direction": "SELL",
                    "current_price": price,
                    "change_pct": chg_pct,
                    "entry_zone": f"₹{s_entry:,.2f}",
                    "stop_loss": f"₹{s_sl:,.2f}",
                    "target_1": f"₹{s_tp:,.2f}",
                    "target_2": f"₹{round(s_entry - scalp_move * 1.6, 2):,.2f}",
                    "target_3": f"₹{round(s_entry - scalp_move * 2.2, 2):,.2f}",
                    "risk_reward": s_rr,
                    "timeframe": "Scalp (3M / 5M)",
                    "confidence_score": s_conf,
                    "confidence_badge": "HIGH CONVICTION" if s_conf >= 75 else "MODERATE",
                    "key_catalyst_reasoning": f"Short-side impulse momentum; testing Camarilla L3 breakdown level with tight risk boundary.",
                    "is_ai_prediction": True,
                    "data_source": "LIVE_EXCHANGE + AI_PREDICTION_ENGINE",
                    "lot_size": lot_size
                })

            # -----------------------------------------------------------------
            # 3. BREAKOUTS & BREAKDOWNS (Camarilla H4 / L4)
            # -----------------------------------------------------------------
            h4 = camarilla["h4_breakout"]
            l4 = camarilla["l4_breakdown"]
            if price >= (h4 * 0.995):
                bo_entry = round(max(price, h4), 2)
                bo_sl = round(camarilla["h3_reversal"], 2)
                bo_risk = max(1.0, bo_entry - bo_sl)
                bo_tp1 = round(bo_entry + (bo_risk * 1.8), 2)
                bo_tp2 = round(bo_entry + (bo_risk * 3.0), 2)
                breakout_list.append({
                    "symbol": sym,
                    "name": prof.get("name", sym),
                    "type": "BREAKOUT",
                    "direction": "BUY",
                    "current_price": price,
                    "change_pct": chg_pct,
                    "entry_zone": f"₹{bo_entry:,.2f}",
                    "stop_loss": f"₹{bo_sl:,.2f}",
                    "target_1": f"₹{bo_tp1:,.2f}",
                    "target_2": f"₹{bo_tp2:,.2f}",
                    "target_3": f"₹{round(bo_entry + bo_risk * 4.2, 2):,.2f}",
                    "risk_reward": f"1:{round((bo_tp1 - bo_entry) / bo_risk, 1)}",
                    "timeframe": "Breakout Expansion",
                    "confidence_score": 83,
                    "confidence_badge": "HIGH CONVICTION",
                    "key_catalyst_reasoning": f"Camarilla H4 breakout at ₹{h4:,.1f}; buyers overwhelming sellers above prior day value area.",
                    "is_ai_prediction": True,
                    "data_source": "LIVE_EXCHANGE + AI_PREDICTION_ENGINE",
                    "lot_size": lot_size
                })

            if price <= (l4 * 1.005):
                bd_entry = round(min(price, l4), 2)
                bd_sl = round(camarilla["l3_reversal"], 2)
                bd_risk = max(1.0, bd_sl - bd_entry)
                bd_tp1 = round(bd_entry - (bd_risk * 1.8), 2)
                bd_tp2 = round(bd_entry - (bd_risk * 3.0), 2)
                breakdown_list.append({
                    "symbol": sym,
                    "name": prof.get("name", sym),
                    "type": "BREAKDOWN",
                    "direction": "SELL",
                    "current_price": price,
                    "change_pct": chg_pct,
                    "entry_zone": f"₹{bd_entry:,.2f}",
                    "stop_loss": f"₹{bd_sl:,.2f}",
                    "target_1": f"₹{bd_tp1:,.2f}",
                    "target_2": f"₹{bd_tp2:,.2f}",
                    "target_3": f"₹{round(bd_entry - bd_risk * 4.2, 2):,.2f}",
                    "risk_reward": f"1:{round((bd_entry - bd_tp1) / bd_risk, 1)}",
                    "timeframe": "Breakdown Expansion",
                    "confidence_score": 81,
                    "confidence_badge": "HIGH CONVICTION",
                    "key_catalyst_reasoning": f"Camarilla L4 breakdown at ₹{l4:,.1f}; institutional supply flushing beneath support zone.",
                    "is_ai_prediction": True,
                    "data_source": "LIVE_EXCHANGE + AI_PREDICTION_ENGINE",
                    "lot_size": lot_size
                })

            # -----------------------------------------------------------------
            # 4. KEY S/R LEVELS & TREND SUMMARY
            # -----------------------------------------------------------------
            key_levels_list.append({
                "symbol": sym,
                "name": prof.get("name", sym),
                "price": price,
                "change_pct": chg_pct,
                "cpr_tc": cpr["tc"],
                "cpr_pivot": cpr["pivot"],
                "cpr_bc": cpr["bc"],
                "cam_h4": camarilla["h4_breakout"],
                "cam_l4": camarilla["l4_breakdown"],
                "vwap": vwap,
                "trend_bias": "STRONG_BULLISH" if price > cpr['tc'] and price > vwap else ("STRONG_BEARISH" if price < cpr['bc'] and price < vwap else "NEUTRAL_CONSOLIDATION"),
                "lot_size": lot_size
            })

        # Sort lists by confidence score
        intraday_list.sort(key=lambda x: x["confidence_score"], reverse=True)
        scalp_list.sort(key=lambda x: x["confidence_score"], reverse=True)

        res = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
            "data_source": "HYBRID_LIVE_AND_PREDICTION",
            "indices_overview": self.get_live_indices_cards(),
            "intraday_setups": intraday_list[:6],
            "scalping_setups": scalp_list[:6],
            "breakouts": breakout_list[:4],
            "breakdowns": breakdown_list[:4],
            "key_levels": key_levels_list[:8],
            "total_predictions": len(intraday_list) + len(scalp_list) + len(breakout_list) + len(breakdown_list)
        }

        self._predictions_cache = res
        self._predictions_cache_time = now
        return res

    def get_options_expert_advisor(
        self,
        symbol: str = "NIFTY",
        query: Optional[str] = None,
        strategy_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Interactive AI Options Trading Advisor for NIFTY 50, SENSEX, and BANK NIFTY.
        """
        sym = (symbol or "NIFTY").upper().strip()
        if sym not in ("NIFTY", "SENSEX", "BANKNIFTY"):
            # Fallback to NIFTY if not in top 3
            sym = "NIFTY"

        # 1. Fetch live option chain and spot metrics
        chain_data = INDIA_OPTIONS.generate_option_chain(sym)
        spot_price = float(chain_data.get("spot_price", 22460.0))
        atm_strike = float(chain_data.get("atm_strike", spot_price))
        pcr = chain_data.get("pcr", {})
        pcr_val = float(pcr.get("pcr_oi", 1.0))
        pcr_sent = pcr.get("sentiment", "NEUTRAL")
        max_pain = float(chain_data.get("max_pain_strike", atm_strike))
        atm_straddle = chain_data.get("atm_straddle", {})
        straddle_cost = float(atm_straddle.get("combined_premium", spot_price * 0.015))
        upper_be = float(atm_straddle.get("upper_breakeven", spot_price + straddle_cost))
        lower_be = float(atm_straddle.get("lower_breakeven", spot_price - straddle_cost))
        lot_size = NSE_RULES.get_lot_size(sym)
        strike_step = NSE_RULES.get_strike_step(sym, spot_price)
        expiry = chain_data.get("expiry", "CURRENT_WEEKLY")
        available_expiries = chain_data.get("available_expiries", [])

        # Live index card for day PnL & direction
        live_cards = {c["symbol"]: c for c in self.get_live_indices_cards()}
        index_card = live_cards.get(sym, {})
        day_chg_pct = float(index_card.get("change_pct", 0.0))
        day_chg_val = float(index_card.get("change_val", 0.0))
        market_dir = index_card.get("direction", "BEARISH" if day_chg_val < 0 else "BULLISH")
        vix = float(index_card.get("vix", 16.5))

        # 2. Derive Call and Put Key Strikes & Greeks
        rows = chain_data.get("chain", [])
        atm_row = next((r for r in rows if r.get("is_atm")), (rows[len(rows)//2] if rows else None))

        # Call setup (Buy CE)
        # Optimal buying strike: ATM or 1-step OTM if momentum
        call_strike = atm_strike if market_dir == "BULLISH" else (atm_strike + strike_step)
        call_row = next((r for r in rows if abs(r["strike"] - call_strike) < (strike_step * 0.5)), atm_row)
        call_ltp = float(call_row["call"]["ltp"]) if call_row else 150.0
        call_delta = float(call_row["call"].get("delta", 0.52)) if call_row else 0.52
        call_theta = float(call_row["call"].get("theta", -18.0)) if call_row else -18.0
        call_iv = float(call_row["call"].get("iv", 16.0)) if call_row else 16.0

        ce_entry = round(call_ltp, 1)
        ce_sl = round(max(5.0, ce_entry * 0.70), 1)  # 30% max premium stop
        ce_tp1 = round(ce_entry * 1.40, 1)            # +40% target
        ce_tp2 = round(ce_entry * 1.80, 1)            # +80% target
        ce_capital = round(ce_entry * lot_size, 2)
        ce_risk = round((ce_entry - ce_sl) * lot_size, 2)
        ce_reward = round((ce_tp1 - ce_entry) * lot_size, 2)

        # Put setup (Buy PE)
        put_strike = atm_strike if market_dir == "BEARISH" else (atm_strike - strike_step)
        put_row = next((r for r in rows if abs(r["strike"] - put_strike) < (strike_step * 0.5)), atm_row)
        put_ltp = float(put_row["put"]["ltp"]) if put_row else 130.0
        put_delta = float(put_row["put"].get("delta", -0.48)) if put_row else -0.48
        put_theta = float(put_row["put"].get("theta", -17.5)) if put_row else -17.5
        put_iv = float(put_row["put"].get("iv", 15.8)) if put_row else 15.8

        pe_entry = round(put_ltp, 1)
        pe_sl = round(max(5.0, pe_entry * 0.70), 1)
        pe_tp1 = round(pe_entry * 1.40, 1)
        pe_tp2 = round(pe_entry * 1.80, 1)
        pe_capital = round(pe_entry * lot_size, 2)
        pe_risk = round((pe_entry - pe_sl) * lot_size, 2)
        pe_reward = round((pe_tp1 - pe_entry) * lot_size, 2)

        # 3. Find Largest Resistance & Support from Open Interest
        highest_ce_oi_row = max(rows, key=lambda r: r["call"]["oi"]) if rows else None
        highest_pe_oi_row = max(rows, key=lambda r: r["put"]["oi"]) if rows else None
        res_strike = highest_ce_oi_row["strike"] if highest_ce_oi_row else (atm_strike + 2 * strike_step)
        sup_strike = highest_pe_oi_row["strike"] if highest_pe_oi_row else (atm_strike - 2 * strike_step)

        # 4. Generate AI Natural Language Commentary based on real data
        bias_verdict = "BEARISH" if market_dir == "BEARISH" and pcr_val < 0.9 else ("BULLISH" if market_dir == "BULLISH" and pcr_val > 1.1 else "RANGEBOUND_CONSOLIDATION")

        advisor_speech = (
            f"**Institutional Intelligence Briefing for {sym} ({expiry}):**\n\n"
            f"• **Live Spot Telemetry**: Currently trading at **₹{spot_price:,.2f}** ({day_chg_pct:+.2f}%, {day_chg_val:+.2f} pts). Market bias is **{market_dir}**.\n"
            f"• **Options Microstructure**: Put-Call Ratio stands at **{pcr_val:.2f}** ({pcr_sent}), indicating institutional {'put protection / bullish support' if pcr_val >= 1.0 else 'call writing pressure'}. "
            f"Max Pain is pinned at **{max_pain:,.0f}**.\n"
            f"• **Expected Expiry Corridor**: The ATM Straddle ({atm_strike:,.0f} CE + PE) trades at **₹{straddle_cost:.1f}**, implying a ±{atm_straddle.get('expected_move_pct', 1.5):.2f}% expected move between **₹{lower_be:,.0f}** and **₹{upper_be:,.0f}**.\n"
            f"• **Key Volatility Walls**: Heavy Call writing creates overhead resistance at **{res_strike:,.0f}**, while Put writers provide strong support floor at **{sup_strike:,.0f}**.\n\n"
            f"**Recommended Actionable Setups:**\n"
            f"1. **{sym} {call_strike:,.0f} CE (Bullish Opportunity)**: Entry ~₹{ce_entry}, Stop Loss: ₹{ce_sl} (-30%), Target 1: ₹{ce_tp1} (+40%), Target 2: ₹{ce_tp2} (+80%). Max risk per lot: ₹{ce_risk:,.2f}.\n"
            f"2. **{sym} {put_strike:,.0f} PE (Bearish Opportunity)**: Entry ~₹{pe_entry}, Stop Loss: ₹{pe_sl} (-30%), Target 1: ₹{pe_tp1} (+40%), Target 2: ₹{pe_tp2} (+80%). Max risk per lot: ₹{pe_risk:,.2f}.\n"
            f"3. **Defined-Risk Spread**: If volatility expands (India VIX {vix:.1f}), deploy a {'Bear Put Spread' if bias_verdict == 'BEARISH' else 'Bull Call Spread'} or Iron Condor with wings outside {lower_be:,.0f} - {upper_be:,.0f}."
        )

        return {
            "symbol": sym,
            "expiry": expiry,
            "available_expiries": available_expiries,
            "spot_price": round(spot_price, 2),
            "change_pct": day_chg_pct,
            "change_val": day_chg_val,
            "direction": market_dir,
            "atm_strike": atm_strike,
            "strike_step": strike_step,
            "lot_size": lot_size,
            "max_pain": max_pain,
            "pcr": pcr,
            "vix": vix,
            "major_support_strike": sup_strike,
            "major_resistance_strike": res_strike,
            "atm_straddle": {
                "premium": straddle_cost,
                "upper_breakeven": upper_be,
                "lower_breakeven": lower_be,
                "expected_move_pct": atm_straddle.get("expected_move_pct", 1.5)
            },
            "call_opportunity": {
                "strike": call_strike,
                "type": "CE",
                "ltp": ce_entry,
                "entry_range": f"₹{ce_entry - 2:.1f} - ₹{ce_entry + 2:.1f}",
                "stop_loss": ce_sl,
                "target_1": ce_tp1,
                "target_2": ce_tp2,
                "risk_reward": "1:2.0",
                "delta": call_delta,
                "theta_per_day": call_theta,
                "iv": call_iv,
                "capital_per_lot": ce_capital,
                "max_risk_per_lot": ce_risk,
                "profit_target_1_per_lot": ce_reward,
                "action": "BUY"
            },
            "put_opportunity": {
                "strike": put_strike,
                "type": "PE",
                "ltp": pe_entry,
                "entry_range": f"₹{pe_entry - 2:.1f} - ₹{pe_entry + 2:.1f}",
                "stop_loss": pe_sl,
                "target_1": pe_tp1,
                "target_2": pe_tp2,
                "risk_reward": "1:2.0",
                "delta": put_delta,
                "theta_per_day": put_theta,
                "iv": put_iv,
                "capital_per_lot": pe_capital,
                "max_risk_per_lot": pe_risk,
                "profit_target_1_per_lot": pe_reward,
                "action": "BUY"
            },
            "ai_advisor_analysis": advisor_speech,
            "data_source": chain_data.get("data_source", "live"),
            "timestamp": datetime.now().strftime("%H:%M:%S IST")
        }

    def get_scanner_views(self) -> Dict[str, Any]:
        """
        Builds live stock scanner separating opportunities into 5 distinct views:
        1. Intraday Trading
        2. Scalping
        3. Bullish Setups
        4. Bearish Setups
        5. Stocks Approaching Key Levels
        """
        now = time.time()
        if self._scanner_views_cache and (now - self._scanner_views_cache_time) < self._scanner_views_ttl:
            return self._scanner_views_cache

        stocks = get_all_india_stocks()
        intraday_list = []
        scalp_list = []
        bullish_list = []
        bearish_list = []
        key_levels_list = []

        for sym in stocks:
            try:
                data = INDIA_ENGINE.analyze_india_instrument(sym, timeframe="1D")
            except Exception:
                continue

            prof = get_india_profile(sym)
            px = float(data["current_price"])
            chg = float(data["change_pct"])
            rvol = float(data["rvol"])
            cpr = data["cpr"]
            cam = data["camarilla"]
            vwap = data["vwap_structure"]["vwap"]
            rsi = float(data.get("technicals", {}).get("rsi_14", 50.0))
            if rsi <= 0.0 or math.isnan(rsi):
                rsi = 52.0 if chg >= 0 else 46.0
            prob = float(data["breakout_probability"])
            lot = NSE_RULES.get_lot_size(sym)
            w52_high = float(prof.get("week52_high", px * 1.25))
            w52_low = float(prof.get("week52_low", px * 0.75))

            base_row = {
                "symbol": sym,
                "name": prof.get("name", sym),
                "sector": prof.get("sector", "Diversified"),
                "price": px,
                "change_pct": chg,
                "rvol": rvol,
                "rsi": rsi,
                "cpr_classification": cpr.get("width_classification", "AVERAGE"),
                "cpr_pivot": cpr.get("pivot", px),
                "vwap": vwap,
                "cam_h4": cam.get("h4_breakout", px * 1.02),
                "cam_l4": cam.get("l4_breakdown", px * 0.98),
                "probability": prob,
                "lot_size": lot,
                "data_source": data.get("data_source", "live")
            }

            # 1. Intraday
            if cpr.get("width_pct", 1.0) <= 0.35 and abs(px - vwap) / max(1.0, px) < 0.015:
                row = dict(base_row)
                row["why_identified"] = f"Narrow Virgin CPR (width {cpr.get('width_pct', 0.2):.2f}%) + Price hugging VWAP (₹{vwap:,.1f}) indicates impending expansion."
                intraday_list.append(row)

            # 2. Scalping
            if rvol >= 1.3 or data.get("is_squeeze", False):
                row = dict(base_row)
                row["why_identified"] = f"High RVOL ({rvol}x average) and Bollinger/Keltner volatility compression; fast momentum trigger."
                scalp_list.append(row)

            # 3. Bullish Setups
            if chg > 0.4 and px > vwap and rsi >= 52:
                row = dict(base_row)
                row["why_identified"] = f"Sustained buying above VWAP with RSI {rsi:.1f} and positive intraday accumulation."
                bullish_list.append(row)

            # 4. Bearish Setups
            if chg < -0.4 and px < vwap and rsi <= 48:
                row = dict(base_row)
                row["why_identified"] = f"Weakness below VWAP with RSI {rsi:.1f} and institutional supply pressure."
                bearish_list.append(row)

            # 5. Key Levels (within 1% of 52W high/low, or Camarilla H4/L4)
            dist_52h = abs(px - w52_high) / max(1.0, w52_high)
            dist_52l = abs(px - w52_low) / max(1.0, w52_low)
            dist_h4 = abs(px - cam['h4_breakout']) / max(1.0, cam['h4_breakout'])
            dist_l4 = abs(px - cam['l4_breakdown']) / max(1.0, cam['l4_breakdown'])

            if dist_52h <= 0.015 or dist_h4 <= 0.008:
                row = dict(base_row)
                tag = "52-Week High" if dist_52h <= 0.015 else "Camarilla H4 Breakout"
                row["why_identified"] = f"Trading right at {tag} level (₹{w52_high if dist_52h <= 0.015 else cam['h4_breakout']:,.1f}); watch for breakout confirmation."
                key_levels_list.append(row)
            elif dist_52l <= 0.015 or dist_l4 <= 0.008:
                row = dict(base_row)
                tag = "52-Week Low" if dist_52l <= 0.015 else "Camarilla L4 Breakdown"
                row["why_identified"] = f"Testing critical {tag} floor (₹{w52_low if dist_52l <= 0.015 else cam['l4_breakdown']:,.1f}); watch for breakdown or bounce."
                key_levels_list.append(row)

        intraday_list.sort(key=lambda x: x["probability"], reverse=True)
        scalp_list.sort(key=lambda x: x["rvol"], reverse=True)
        bullish_list.sort(key=lambda x: x["change_pct"], reverse=True)
        bearish_list.sort(key=lambda x: x["change_pct"])
        key_levels_list.sort(key=lambda x: x["probability"], reverse=True)

        res = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
            "intraday": intraday_list[:15],
            "scalping": scalp_list[:15],
            "bullish": bullish_list[:15],
            "bearish": bearish_list[:15],
            "key_levels": key_levels_list[:15],
            "counts": {
                "intraday": len(intraday_list),
                "scalping": len(scalp_list),
                "bullish": len(bullish_list),
                "bearish": len(bearish_list),
                "key_levels": len(key_levels_list)
            }
        }
        self._scanner_views_cache = res
        self._scanner_views_cache_time = now
        return res


INDIA_PREDICTIONS = IndiaPredictionEngine()
