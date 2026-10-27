"""
HM Algo 2.0 — Deepened Adversarial Intelligence Agent (Devil's Advocate).
Implements parameterized adversarial risk scoring, cross-asset correlation checks,
liquidity sweep detection, spread vulnerability, session timing, and empirical invalidation.
"""
import time
from typing import List, Optional
from jarvis.data.schemas import MarketContext, RegimeOutput, DevilAdvocateReport
from jarvis.market.correlations import DynamicCorrelationEngine
from jarvis.data.symbol_registry import resolve as resolve_symbol

class DevilAdvocateAnalyst:
    """
    Mandatory Adversarial Counter-Analyst.
    Calculates adversarial penalty scores and invalidation risk coefficients to stress-test trade hypotheses.
    """
    def __init__(self, correlation_engine: Optional[DynamicCorrelationEngine] = None):
        self.correlation_engine = correlation_engine or DynamicCorrelationEngine()

    def critique_opportunity(
        self,
        context: MarketContext,
        regime: RegimeOutput,
        proposed_bias: str  # "BUY" or "SELL"
    ) -> DevilAdvocateReport:
        t0 = time.perf_counter()
        threats: List[str] = []
        invalidation_triggers: List[str] = []
        traps: List[str] = []
        correlated_threats: List[str] = []

        penalty_score = 0.0
        st = context.structure
        mom = context.momentum
        vol = context.volatility
        liq = context.liquidity
        mtf = context.mtf_alignment
        sess = context.session

        is_buy = (proposed_bias.upper() == "BUY")
        counter_bias = "BEARISH" if is_buy else "BULLISH"

        # ── 1. Parameterized Structural & Momentum Analysis ─────────────────────────
        # Directional mapping
        trend_score_against = (mom.trend_score < -20) if is_buy else (mom.trend_score > 20)
        rsi_overextended = (mom.rsi > 70.0) if is_buy else (mom.rsi < 30.0)
        adx_strong_counter = (mom.adx > 25.0) and ((mom.minus_di > mom.plus_di + 8) if is_buy else (mom.plus_di > mom.minus_di + 8))
        adverse_divergence = (mom.divergence == "BEARISH_DIVERGENCE") if is_buy else (mom.divergence == "BULLISH_DIVERGENCE")
        unfavorable_zone = (st.discount_premium_zone == "PREMIUM") if is_buy else (st.discount_premium_zone == "DISCOUNT")
        trend_broken = (st.lower_highs and st.lower_lows) if is_buy else (st.higher_highs and st.higher_lows)
        htf_conflict = (mtf.get("H4") == ("BEARISH" if is_buy else "BULLISH")) or (mtf.get("D1") == ("BEARISH" if is_buy else "BULLISH"))
        unswept_equal_levels = liq.equal_lows if is_buy else liq.equal_highs

        if trend_score_against:
            penalty_score += 15.0
            threats.append(f"Counter-trend {proposed_bias} against opposing momentum score ({mom.trend_score}).")

        if rsi_overextended:
            penalty_score += 12.0
            threats.append(f"Overextended {proposed_bias} entry with RSI at {mom.rsi:.1f}.")

        if adx_strong_counter:
            penalty_score += 14.0
            threats.append(f"Strong adverse directional trend present (ADX={mom.adx:.1f}, Counter-DI dominant).")

        if adverse_divergence:
            penalty_score += 14.0
            threats.append(f"Adverse RSI divergence warning against {proposed_bias} impulse.")

        if unfavorable_zone:
            penalty_score += 10.0
            threats.append(f"Attempting {proposed_bias} in unfavorable {st.discount_premium_zone} zone relative to equilibrium.")

        if trend_broken:
            penalty_score += 18.0
            threats.append("Primary market structure actively prints counter-trend swing points.")

        if htf_conflict:
            macro_bias = mtf.get("H4") or mtf.get("D1")
            penalty_score += 12.0
            threats.append(f"Higher timeframe structural conflict: Macro timeframe is {macro_bias}.")

        # ── 2. Deep Liquidity Sweep & Order Book Traps ──────────────────────────────
        if unswept_equal_levels:
            zone_target = st.demand_zone[0] if is_buy else st.supply_zone[1]
            traps.append(f"Unswept Equal {'Lows' if is_buy else 'Highs'} near {zone_target} — smart money stop-hunt risk.")
            penalty_score += 8.0

        if liq.sweep_detected:
            # If a sweep just happened opposing our trade, it's a stop-run against us
            if (is_buy and liq.sweep_type == "BUY_SIDE") or (not is_buy and liq.sweep_type == "SELL_SIDE"):
                penalty_score += 10.0
                threats.append(f"Fresh {liq.sweep_type} sweep detected — active stop-hunt in progress ({liq.sweep_magnitude:.1f} pips).")

        # Liquidity imbalance threat (trapped volume)
        if liq.buy_side_liquidity > 0 and liq.sell_side_liquidity > 0:
            ratio = liq.sell_side_liquidity / (liq.buy_side_liquidity + 1e-9) if is_buy else liq.buy_side_liquidity / (liq.sell_side_liquidity + 1e-9)
            if ratio > 2.0:
                penalty_score += 6.0
                traps.append(f"Severe liquidity pool imbalance ({ratio:.1f}x opposing volume resting in order book).")

        # ── 2.5 Institutional Footprint Order Flow & Absorption Traps (E1) ──────────
        of_data = getattr(context, "order_flow", {})
        if isinstance(of_data, dict) and of_data:
            trap = of_data.get("absorption_trap")
            delta_score = float(of_data.get("delta_score", 0.0))

            if is_buy and trap == "SELLER_ABSORPTION_TRAP":
                penalty_score += 15.0
                threats.append("SELLER_ABSORPTION_TRAP detected: Institutional sellers quietly absorbing buyer flow at highs.")
            elif (not is_buy) and trap == "BUYER_ABSORPTION_TRAP":
                penalty_score += 15.0
                threats.append("BUYER_ABSORPTION_TRAP detected: Institutional buyers quietly absorbing seller flow at lows.")

            if is_buy and delta_score <= -35.0:
                opposing_penalty = round(min(15.0, abs(delta_score) * 0.15), 1)
                penalty_score += opposing_penalty
                threats.append(f"Strong adverse volume delta ({delta_score:.1f}) indicates institutional selling pressure opposing BUY setup.")
            elif (not is_buy) and delta_score >= 35.0:
                opposing_penalty = round(min(15.0, delta_score * 0.15), 1)
                penalty_score += opposing_penalty
                threats.append(f"Strong adverse volume delta (+{delta_score:.1f}) indicates institutional buying pressure opposing SELL setup.")

        # ── 3. Volatility, Spread & Session Timing Risk ─────────────────────────────
        spec = resolve_symbol(context.symbol)
        if vol.is_excessive_spread:
            penalty_score += 12.0
            threats.append(f"Excessive spread ({vol.current_spread_pips:.1f} pips > {spec.typical_spread_pips*2:.1f} typical) creates severe slippage drag.")

        if vol.state == "EXTREME":
            penalty_score += 18.0
            threats.append("Extreme volatility shock in progress — stop loss vulnerability elevated.")

        if not sess.is_prime_session and not getattr(spec, "is_crypto", False):
            penalty_score += 8.0
            threats.append(f"Trading during low-liquidity off-hours ({sess.current_session}) increases false breakout risk.")

        # ── 4. Market Regime Transition Threat ──────────────────────────────────────
        if regime.regime_transition:
            penalty_score += 10.0
            threats.append("Market regime currently transitioning — statistical edges temporarily unstable.")
        elif regime.confidence < 0.60:
            penalty_score += 8.0
            threats.append(f"Low regime classification confidence ({regime.confidence*100:.0f}%).")

        # ── 5. Cross-Asset Structural Correlation Check ─────────────────────────────
        # Example: If buying XAUUSD but USD is surging or EURUSD is collapsing
        correlated_pairs = [("EURUSD", 0.70), ("GBPUSD", 0.60)] if "XAU" in context.symbol or "GOLD" in context.symbol else []
        for pair_sym, min_corr in correlated_pairs:
            corr_val = self.correlation_engine.get_correlation(context.symbol, pair_sym)
            if corr_val >= min_corr and pair_sym in mtf:
                pair_bias = mtf[pair_sym] if isinstance(mtf.get(pair_sym), str) else ""
                if pair_bias and ((is_buy and pair_bias == "BEARISH") or (not is_buy and pair_bias == "BULLISH")):
                    penalty_score += 8.0
                    c_threat = f"Correlated instrument {pair_sym} (r={corr_val:.2f}) exhibits conflicting {pair_bias} structure."
                    threats.append(c_threat)
                    correlated_threats.append(c_threat)

        # ── 6. Invalidation Triggers & Bounds ───────────────────────────────────────
        if is_buy:
            invalidation_triggers.append(f"H1 candle close below demand zone {st.demand_zone[0]}.")
            invalidation_triggers.append("Bearish displacement breaking recent swing low.")
        else:
            invalidation_triggers.append(f"H1 candle close above supply zone {st.supply_zone[1]}.")
            invalidation_triggers.append("Bullish displacement breaking recent swing high.")

        # Concrete obstacle / threat price level detection across ALL symbols (§B-5)
        # Checks supply/demand zones, key pivot levels, and dynamic trendline/channel boundaries
        threat_price_level = None
        c_price = context.current_price
        atr_val = getattr(vol, "atr", 1.0) or 1.0

        if is_buy:
            candidate_res = []
            if st.supply_zone[0] > c_price:
                candidate_res.append(st.supply_zone[0])
            if getattr(st, "channel_upper", 0.0) > c_price:
                candidate_res.append(st.channel_upper)
            if getattr(st, "dynamic_resistance_trendline", 0.0) > c_price:
                candidate_res.append(st.dynamic_resistance_trendline)
            if hasattr(st, "key_levels") and st.key_levels:
                res_levels = [kl["price"] for kl in st.key_levels if kl.get("price", 0) > c_price]
                candidate_res.extend(res_levels)
            if candidate_res:
                threat_price_level = min(candidate_res)
                # Check for immediate ceiling collision (buying directly into overhead resistance/trendline)
                dist_to_res = threat_price_level - c_price
                if dist_to_res < (0.50 * atr_val):
                    col_penalty = 30.0 if dist_to_res < (0.25 * atr_val) else 20.0
                    penalty_score += col_penalty
                    threats.append(
                        f"Immediate overhead resistance/trendline collision at {threat_price_level:.2f} "
                        f"({dist_to_res:.1f} pts / {dist_to_res/max(atr_val,1e-6):.2f}R runway) — high rejection/reversal risk."
                    )
        else:
            candidate_sup = []
            if 0 < st.demand_zone[1] < c_price:
                candidate_sup.append(st.demand_zone[1])
            if 0 < getattr(st, "channel_lower", 0.0) < c_price:
                candidate_sup.append(st.channel_lower)
            if 0 < getattr(st, "dynamic_support_trendline", 0.0) < c_price:
                candidate_sup.append(st.dynamic_support_trendline)
            if hasattr(st, "key_levels") and st.key_levels:
                sup_levels = [kl["price"] for kl in st.key_levels if 0 < kl.get("price", 0) < c_price]
                candidate_sup.extend(sup_levels)
            if candidate_sup:
                threat_price_level = max(candidate_sup)
                # Check for immediate floor collision (selling directly into dynamic trendline support)
                dist_to_sup = c_price - threat_price_level
                if dist_to_sup < (0.50 * atr_val):
                    col_penalty = 30.0 if dist_to_sup < (0.25 * atr_val) else 20.0
                    penalty_score += col_penalty
                    threats.append(
                        f"Immediate dynamic support/trendline collision at {threat_price_level:.2f} "
                        f"({dist_to_sup:.1f} pts / {dist_to_sup/max(atr_val,1e-6):.2f}R runway) — high bounce/reversal risk."
                    )

        # Critique Confidence based on context quality & data completeness
        critique_conf = round(max(0.40, min(1.0, (context.context_quality / 100.0))), 2)

        # Cap penalty score between 0.0 and 50.0
        final_penalty = round(min(50.0, max(0.0, penalty_score)), 1)
        
        # Invalidation risk coefficient: 1.0 (no penalty) down to 0.2 (severe penalty)
        invalidation_coeff = round(max(0.20, 1.0 - (final_penalty / 60.0)), 2)

        elapsed = (time.perf_counter() - t0) * 1000.0

        return DevilAdvocateReport(
            symbol=context.symbol,
            counter_bias=counter_bias,
            penalty_score=final_penalty,
            invalidation_risk_coefficient=invalidation_coeff,
            threats_detected=threats,
            invalidation_triggers=invalidation_triggers,
            liquidity_traps=traps,
            threat_price_level=threat_price_level,
            critique_confidence=critique_conf,
            correlated_threats=correlated_threats,
            execution_time_ms=round(elapsed, 2)
        )
