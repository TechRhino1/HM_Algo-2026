"""
HM Algo 2.0 — Autonomous Decision Engine & Trade Quality Gate.
Synthesizes multi-agent confluences, applies Devil's Advocate risk penalties, calculates expected value, and gates execution.
"""
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone
import logging

logger = logging.getLogger("JARVIS_DecisionEngine")

from jarvis.intelligence.order_flow import InstitutionalVolumeOrderFlowEngine
from jarvis.intelligence.self_learning import SelfLearningEngine
from jarvis.data.schemas import (
    MarketContext,
    RegimeOutput,
    AnalystReport,
    DevilAdvocateReport,
    DecisionObject,
    TradeQualityGateResult,
    MarketRegime,
    is_observed_price,
    answered_reports,
)
from jarvis.intelligence.strategy_selector import StrategySelector
from jarvis.intelligence.hypothesis_engine import HypothesisEngine
from jarvis.intelligence.confidence import ConfidenceCalibrationEngine
from jarvis.intelligence.honest_base_rates import (
    get_base_rate as get_honest_base_rate,
    describe_gap as describe_honest_gap,
)
from jarvis.learning.online_ml_predictor import OnlineMLPredictor
from jarvis.market.news import GLOBAL_NEWS_ENGINE
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.risk.account_tier import is_micro_account, get_effective_min_ev
from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config

from jarvis.learning.fractional_diff import FractionalDifferentiationTransformer
from jarvis.learning.ensemble_bandit import EnsembleStrategyBandit
from jarvis.intelligence.meta_labeler import MetaLabeler
from jarvis.intelligence.gate_policy import AdaptiveGatePolicy
from jarvis.intelligence.ai_dissector import AIDissector
from jarvis.intelligence.realtime_optimizer import RealtimeOptimizer
from jarvis.intelligence.master_confluence import MasterConfluenceEngine
from jarvis.market.fair_value_gap import FairValueGapEngine
from jarvis.intelligence.mean_reversion import MeanReversionEngine
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine


def _is_forex(symbol: str) -> bool:
    try:
        spec = resolve_symbol(symbol)
        return getattr(spec, "asset_class", "").upper() == "FOREX"
    except Exception:
        return False


class LevelsResult(tuple):
    """An 8-tuple result containing (bias, entry, sl, tp, risk_dist, rr, first_target, volume_pct)
    preserving 100% backward compatibility with tuple unpacking, while exposing 3-tier milestone
    attributes tp1_price, tp2_price, and tp3_price."""
    def __new__(cls, tentative_bias, entry_price, sl_price, tp_price, risk_dist, rr_ratio, first_target_price, first_target_volume_pct, tp1_price=None, tp2_price=None, tp3_price=None):
        return super().__new__(cls, (tentative_bias, entry_price, sl_price, tp_price, risk_dist, rr_ratio, first_target_price, first_target_volume_pct))

    def __init__(self, tentative_bias, entry_price, sl_price, tp_price, risk_dist, rr_ratio, first_target_price, first_target_volume_pct, tp1_price=None, tp2_price=None, tp3_price=None):
        self.tentative_bias = tentative_bias
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.tp_price = tp_price
        self.risk_dist = risk_dist
        self.rr_ratio = rr_ratio
        self.first_target_price = first_target_price
        self.first_target_volume_pct = first_target_volume_pct
        self.tp1_price = tp1_price if tp1_price is not None else first_target_price
        self.tp2_price = tp2_price if tp2_price is not None else tp_price
        self.tp3_price = tp3_price


class DecisionEngine:
    def __init__(
        self,
        strategy_selector: Optional[StrategySelector] = None,
        hypothesis_engine: Optional[HypothesisEngine] = None,
        calibrator: Optional[ConfidenceCalibrationEngine] = None,
        ml_predictor: Optional[OnlineMLPredictor] = None,
        self_learning: Optional[SelfLearningEngine] = None,
        meta_labeler: Optional[MetaLabeler] = None,
        ai_dissector: Optional[AIDissector] = None,
        realtime_optimizer: Optional[RealtimeOptimizer] = None,
        master_confluence: Optional[MasterConfluenceEngine] = None,
        dynamic_levels_engine: Optional[DynamicRiskAndLevelsEngine] = None,
        min_ev_hurdle: float = 0.50,
        max_devil_penalty: float = 43.0
    ):
        self.strategy_selector = strategy_selector or StrategySelector()
        self.hypothesis_engine = hypothesis_engine or HypothesisEngine()
        self.calibrator = calibrator or ConfidenceCalibrationEngine()
        self.ml_predictor = ml_predictor or OnlineMLPredictor()
        self.ensemble_bandit = EnsembleStrategyBandit()
        self.frac_diff = FractionalDifferentiationTransformer()
        self.min_ev_hurdle = min_ev_hurdle
        self.max_devil_penalty = max_devil_penalty
        self.order_flow = InstitutionalVolumeOrderFlowEngine()
        self.self_learning = self_learning or SelfLearningEngine()
        self.meta_labeler = meta_labeler or MetaLabeler()
        self.gate_policy = AdaptiveGatePolicy()
        self.ai_dissector = ai_dissector or AIDissector()
        self.realtime_optimizer = realtime_optimizer or RealtimeOptimizer()
        self.master_confluence = master_confluence or MasterConfluenceEngine()
        self.fvg_engine = FairValueGapEngine()
        self.mean_reversion_engine = MeanReversionEngine()
        self.dynamic_levels_engine = dynamic_levels_engine or DynamicRiskAndLevelsEngine()
        # Symbols already warned about an unobserved price — this runs per symbol per
        # cycle, so an undeduplicated warning would flood the log during an outage.
        self._no_price_warned: set = set()



    def _compute_bias_and_levels(
        self,
        context: MarketContext,
        regime: RegimeOutput,
        analyst_reports: Dict[str, AnalystReport],
        trade_style: str = "SWING",
        account_balance: float = 10000.0,
        risk_per_trade_pct: float = 0.5,
    ):
        st = context.structure
        c_price = context.current_price

        # ── No observed price ⇒ no direction, no levels ────────────────────────
        # `calculate_levels` refuses independently, but the *bias* is decided here and
        # returned beside the levels. Returning a BUY/SELL verdict with a zero entry
        # would let `decision_action` mark the setup EXECUTE (it only checks
        # `gate_passed and bias in (BUY, SELL)`), so the direction must stand down in
        # the same breath as the prices. Returns the same 8-tuple shape as the normal
        # path with every price at 0.0 — a value no guard can mistake for a real one.
        if not (
            is_observed_price(c_price)
            and is_observed_price(context.bid)
            and is_observed_price(context.ask)
        ):
            if context.symbol not in self._no_price_warned:
                self._no_price_warned.add(context.symbol)
                logger.warning(
                    "No observed price for %s (current_price=%r, bid=%r, ask=%r); forcing HOLD "
                    "and refusing to emit entry/SL/TP. Logged once per symbol per outage.",
                    context.symbol, c_price, context.bid, context.ask,
                )
            return LevelsResult("HOLD", 0.0, 0.0, 0.0, 0.0, 0.0, None, 0.0, None, 0.0, 0.0)

        if self._no_price_warned:
            self._no_price_warned.clear()

        bull_votes = sum(1 for r in analyst_reports.values() if r.bias == "BULLISH")
        bear_votes = sum(1 for r in analyst_reports.values() if r.bias == "BEARISH")
        trend_score = getattr(context.momentum, "trend_score", 0.0) if hasattr(context, "momentum") else 0.0
        
        if st.choch and st.choch_type == "BEARISH":
            tentative_bias = "SELL"
        elif st.choch and st.choch_type == "BULLISH":
            tentative_bias = "BUY"
        elif st.bos and trend_score <= -20.0:
            tentative_bias = "SELL"
        elif st.bos and trend_score >= 20.0:
            tentative_bias = "BUY"
        elif bear_votes >= 3 and bear_votes > bull_votes:
            tentative_bias = "SELL"
        elif bull_votes >= 3 and bull_votes > bear_votes:
            tentative_bias = "BUY"
        elif st.bias == "BEARISH" and trend_score <= -20.0:
            tentative_bias = "SELL"
        elif st.bias == "BULLISH" and trend_score >= 20.0:
            tentative_bias = "BUY"
        else:
            tentative_bias = "HOLD"

        style = trade_style or getattr(context, "trade_style", "SWING") or "SWING"
        levels = self.dynamic_levels_engine.calculate_levels(
            context=context,
            regime=regime,
            tentative_bias=tentative_bias,
            # The caller's real balance must flow through. Hardcoding 10000.0 here
            # meant a $200 account was sized as if it held $10 000, and the SL
            # distance was computed off the wrong equity — a direct cause of
            # position sizing and risk being wrong on small accounts.
            account_balance=float(account_balance or 10000.0),
            risk_per_trade_pct=float(risk_per_trade_pct or 0.5),
            trade_style=style
        )

        return LevelsResult(
            tentative_bias,
            levels["entry_price"],
            levels["sl_price"],
            levels["tp_price"],
            levels["risk_dist"],
            levels["rr_ratio"],
            levels["first_target_price"],
            levels["first_target_volume_pct"],
            tp1_price=levels.get("tp1_price", levels["first_target_price"]),
            tp2_price=levels.get("tp2_price", levels["tp_price"]),
            tp3_price=levels.get("tp3_price")
        )

    def _compute_blended_probability(
        self,
        context: MarketContext,
        regime: RegimeOutput,
        analyst_reports: Dict[str, AnalystReport],
        devil_report: DevilAdvocateReport,
        tentative_bias: str,
        rr_ratio: float,
        risk_dist: float = 0.0,
        account_balance: float = 10000.0,
        risk_per_trade_pct: float = 0.5
    ):
        hypotheses = self.hypothesis_engine.construct_hypotheses(
            context, regime, analyst_reports, devil_report, tentative_bias
        )
        raw_prob = hypotheses.primary_probability if tentative_bias in ["BUY", "SELL"] else 0.33
        calibrated_win_p = self.calibrator.calibrate_probability(raw_prob)

        # Order flow volume delta continuous calibration (E1)
        of_data = getattr(context, "order_flow", {})
        delta_score = float(of_data.get("delta_score", 0.0)) if isinstance(of_data, dict) else 0.0
        if tentative_bias == "BUY":
            if delta_score > 20.0:
                calibrated_win_p = min(0.95, calibrated_win_p + (delta_score / 100.0) * 0.06)
            elif delta_score < -20.0:
                calibrated_win_p = max(0.05, calibrated_win_p - (abs(delta_score) / 100.0) * 0.08)
        elif tentative_bias == "SELL":
            if delta_score < -20.0:
                calibrated_win_p = min(0.95, calibrated_win_p + (abs(delta_score) / 100.0) * 0.06)
            elif delta_score > 20.0:
                calibrated_win_p = max(0.05, calibrated_win_p - (delta_score / 100.0) * 0.08)

        ml_features = self.ml_predictor.extract_feature_vector(
            context=context,
            regime=regime,
            tentative_bias=tentative_bias,
            devil_penalty=devil_report.penalty_score,
            target_rr=rr_ratio
        )
        ml_win_p = self.ml_predictor.predict_win_probability(ml_features)

        final_win_p = round((0.45 * calibrated_win_p) + (0.55 * ml_win_p), 2)
        loss_p = round(1.0 - final_win_p, 2)

        # --- Measured counterpart to the hand-authored gate probability (P0-1) ---
        # `final_win_p` is 100% hand-authored and 0% fitted: a hand-typed reliability
        # table blended with a hand-authored linear score. Refitting it on honest,
        # cost-bearing MT5 data produced 0/20 skillful symbols, and mean |AUC-0.5|
        # *shrank* from 0.0353 to 0.0211 as the sample doubled -- the score is noise.
        #
        # So we deliberately do neither of the two tempting things: we do not gate on
        # the measured rate (win rates ~0.33-0.44 sit against a 40% break-even, so it
        # would reject nearly every trade), and we do not try to fit the gate either.
        # We log the gap, so the overstatement is visible on every single trade.
        honest_base_rate = None
        try:
            _honest_style = getattr(context, "trade_style", None) or "SWING"
            honest_base_rate = get_honest_base_rate(context.symbol, _honest_style)
            if honest_base_rate is not None:
                _gap = describe_honest_gap(final_win_p, honest_base_rate, tp=rr_ratio)
                # INFO only for an actionable bias. This runs for every symbol on every
                # scan cycle, and logging all of them measured ~11k lines/hour and buried
                # every other message. The rate is still attached to the DecisionObject
                # either way, so nothing is lost for the non-actionable cases.
                if tentative_bias in ("BUY", "SELL"):
                    logger.info(f"[{context.symbol}] HONEST BASE RATE | {_gap}")
                else:
                    logger.debug(f"[{context.symbol}] HONEST BASE RATE | {_gap}")
        except Exception as _honest_exc:  # observability must never break the loop
            logger.debug(f"[{context.symbol}] honest base rate unavailable: {_honest_exc}")

        planned_risk_dollars = max(0.50, account_balance * (risk_per_trade_pct / 100.0))
        planned_win_dollars = planned_risk_dollars * rr_ratio
        
        spec = resolve_symbol(context.symbol)
        contract_size = spec.contract_size
        
        if tentative_bias not in ["BUY", "SELL"]:
            return final_win_p, loss_p, 0.0, hypotheses, calibrated_win_p, honest_base_rate, raw_prob

        est_lots = max(0.01, planned_risk_dollars / (max(risk_dist, 1e-4) * contract_size))
        pip_val_per_lot = spec.pip_value_per_lot
        spread_cost = context.volatility.current_spread_pips * pip_val_per_lot * est_lots
        expected_slippage = (context.volatility.atr * 0.02) * est_lots

        ev = (final_win_p * planned_win_dollars) - (loss_p * planned_risk_dollars) - spread_cost - expected_slippage
        ev = round(float(ev), 2)
        return final_win_p, loss_p, ev, hypotheses, calibrated_win_p, honest_base_rate, raw_prob

    def _apply_quality_gate(
        self,
        context: MarketContext,
        regime: RegimeOutput,
        devil_report: DevilAdvocateReport,
        ai_score: float,
        rr_ratio: float,
        ev: float,
        final_win_p: float,
        spread: float,
        premium_discount_valid: bool,
        account_balance: float,
        current_drawdown_pct: float = 0.0,
        tentative_bias: str = "HOLD",
        calibrated_win_p: float = 0.0,
        risk_dist: float = 0.0,
        planned_risk_dollars: float = 0.0,
        strategy: str = "",
        of_res: Optional[Dict[str, Any]] = None
    ) -> TradeQualityGateResult:
        is_micro_mode = is_micro_account(account_balance)
        effective_min_ev = get_effective_min_ev(account_balance, planned_risk_dollars)

        from jarvis.data.symbol_registry import resolve as resolve_symbol
        spec = resolve_symbol(context.symbol)

        sym_name = str(context.symbol).upper()
        is_jpy = "JPY" in sym_name
        is_crypto = ("BTC" in sym_name) or spec.is_crypto
        is_gold = ("XAU" in sym_name) or ("GOLD" in sym_name) or (getattr(spec, "asset_class", "") == "COMMODITY")
        is_fx = _is_forex(context.symbol) and not is_jpy
        is_index_asset = getattr(spec, "asset_class", "") == "INDEX" or any(k in sym_name for k in ["US500", "NAS100", "US30", "SPX", "NDX", "DJ"])
        is_oil_asset = any(k in sym_name for k in ["WTI", "OIL", "CRUDE"])

        # 1. Dynamic Confluence Count
        confluence_count = 0
        if bool(getattr(context.structure, "bos", False)):
            confluence_count += 1
        if bool(getattr(context.liquidity, "sweep_detected", False)):
            confluence_count += 1
        if abs(getattr(context.momentum, "trend_score", 0.0)) >= 20.0:
            confluence_count += 1
        if abs(getattr(context, "mtf_confluence_score", 0.0)) >= 30.0:
            confluence_count += 1

        # 2. Dynamic Kelly Win Probability Hurdle: Required_Win_P = 1.0 / (1.0 + Target_RR) + SlippageSafetyMargin (bounded [0.50, 0.68])
        typ_spread_pips = spec.typical_spread_pips if getattr(spec, "typical_spread_pips", 0) > 0 else 1.5
        spread_ratio = spread / max(typ_spread_pips, 0.1)
        kelly_base = 1.0 / (1.0 + max(0.5, rr_ratio))

        spread_penalty = 0.02 * min(2.0, max(0.0, spread_ratio - 1.0))
        if is_fx:
            base_safety_margin = 0.180  # Lower from 0.200 (was 0.240)
        elif is_gold:
            base_safety_margin = 0.200  # Lower from 0.230
        elif is_crypto:
            base_safety_margin = 0.220  # Keep higher for crypto
        elif is_jpy:
            base_safety_margin = 0.190  # Lower for JPY pairs
        else:
            base_safety_margin = 0.200

        dynamic_kelly_p = kelly_base + base_safety_margin + spread_penalty
        floor_win_p = 0.45 if is_micro_mode else 0.48
        if is_fx:
            floor_win_p = 0.46
        required_win_p = max(floor_win_p, min(0.65, round(dynamic_kelly_p, 2)))
        if is_micro_mode and current_drawdown_pct > 5.0:
            required_win_p = max(required_win_p, 0.52)

        # 3. Dynamic AI Minimum Score (Calibrated per Execution Horizon)
        is_transition_reg = (regime.primary_regime in (MarketRegime.TRANSITION, MarketRegime.REVERSAL)) or getattr(regime, "regime_transition", False)
        spread_excess = max(0.0, spread_ratio - 1.0)
        confluence_adj = 4.0 if confluence_count >= 4 else 0.0
        t_style_check = (getattr(context, "trade_style", None) or getattr(context, "style", "SWING") or "SWING").upper()

        if "SCALP" in t_style_check or is_micro_mode:
            base_score = 58.0
            floor_score_opt = 56.0
        elif any(x in t_style_check for x in ("DAY", "INTRADAY")):
            base_score = 60.0
            floor_score_opt = 58.0
        else:
            if is_fx:
                base_score = 65.0
                floor_score_opt = 60.0
            elif is_gold:
                base_score = 68.0
                floor_score_opt = 62.0
            elif is_crypto:
                base_score = 70.0
                floor_score_opt = 65.0
            else:
                base_score = 66.0
                floor_score_opt = 62.0

        if not is_fx and ev >= 1.5 and rr_ratio >= 2.0:
            base_score = max(58.0, base_score - 4.0)
            floor_score_opt = max(58.0, floor_score_opt - 4.0)

        dynamic_score = base_score + (5.0 if is_transition_reg else 0.0) + (6.0 * spread_excess) - confluence_adj
        min_score = max(floor_score_opt, min(80.0, dynamic_score))

        # Risk-Reward minimum & SL multiplier (adapted to trade horizon)
        t_style_check = (getattr(context, "trade_style", None) or getattr(context, "style", "SWING") or "SWING").upper()
        if "SCALP" in t_style_check:
            min_rr = 1.3
            min_sl_atr_mult = 0.20
            max_spread = spec.max_spread_pips
        elif any(x in t_style_check for x in ("DAY", "INTRADAY")):
            min_rr = 1.5
            min_sl_atr_mult = 0.35
            max_spread = spec.max_spread_pips
        elif is_micro_mode:
            min_rr = 1.5
            min_sl_atr_mult = 0.35
            max_spread = min(3.0, spec.max_spread_pips * 0.8)
            if current_drawdown_pct > 5.0:
                min_score = max(min_score, 74.0)
        elif is_gold:
            min_rr = 1.8
            min_sl_atr_mult = 0.45
            max_spread = spec.max_spread_pips
        elif is_crypto:
            min_rr = 2.0
            min_sl_atr_mult = 0.50
            max_spread = spec.max_spread_pips * 0.95
        elif any(k in sym_name for k in ["US500", "NAS100", "US30", "SPX", "NDX"]):
            min_rr = 1.8
            min_sl_atr_mult = 0.35
            max_spread = spec.max_spread_pips * 0.90
        elif is_jpy:
            min_rr = 1.8
            min_sl_atr_mult = 0.45
            max_spread = spec.max_spread_pips
        else:  # Forex Majors
            min_rr = 1.7  # Calibrated for XM Ultra Low tight spreads
            min_sl_atr_mult = 0.40
            max_spread = spec.max_spread_pips

        # Real-time per-symbol optimizer adjustments
        if is_micro_mode:
            floor_win_p_opt = 0.45
        elif rr_ratio >= 3.0 and ev > 0:
            floor_win_p_opt = 0.45
        elif rr_ratio >= 2.0 and ev > 0:
            floor_win_p_opt = 0.48
        elif rr_ratio >= 1.5:
            floor_win_p_opt = 0.55
        else:
            floor_win_p_opt = 0.58

        try:
            regime_str = regime.primary_regime.value if regime and hasattr(regime, "primary_regime") else "GLOBAL"
            adj = self.realtime_optimizer.get_adjustments(context.symbol, regime_str)
            required_win_p = max(floor_win_p_opt, min(0.65, required_win_p + float(adj.get("win_p_delta", 0))))
            min_score = max(floor_score_opt, min(80.0, min_score + float(adj.get("score_delta", 0))))
            min_rr = max(1.3, min(2.0, min_rr + float(adj.get("rr_delta", 0))))
        except Exception:
            required_win_p = max(floor_win_p_opt, min(0.65, required_win_p))
            min_score = max(floor_score_opt, min(80.0, min_score))

        if is_index_asset:
            min_score = max(min_score, 68.0)
        elif "BTC" in sym_name:
            min_score = max(min_score, 72.0)

        # 4. Macro MTF Confluence Guard
        mtf_align = getattr(context, "mtf_alignment", {})
        h4_bias = mtf_align.get("H4", "NEUTRAL") if isinstance(mtf_align, dict) else "NEUTRAL"
        d1_bias = mtf_align.get("D1", "NEUTRAL") if isinstance(mtf_align, dict) else "NEUTRAL"
        mom_ts = float(getattr(context.momentum, "trend_score", 0.0)) if hasattr(context, "momentum") else 0.0
        
        is_index_sym = getattr(spec, "asset_class", "") == "INDEX" or any(k in context.symbol.upper() for k in ["US500", "NAS100", "US30", "SPX", "NDX", "DJ"])
        is_crypto_sym = getattr(spec, "is_crypto", False) or (getattr(spec, "asset_class", "").upper() == "CRYPTO") or any(k in context.symbol.upper() for k in ["BTC", "ETH", "SOL"])

        mtf_counter_trend = False
        if is_index_sym:
            # Indices: Strictly follow H4/D1 institutional macro flow
            if tentative_bias == "BUY" and (h4_bias == "BEARISH" or d1_bias == "BEARISH" or mom_ts <= -25.0):
                mtf_counter_trend = True
            elif tentative_bias == "SELL" and (h4_bias == "BULLISH" or d1_bias == "BULLISH" or mom_ts >= 15.0):
                mtf_counter_trend = True
        elif is_crypto_sym:
            # Relax crypto - allow trades against H4 if D1 aligns
            if tentative_bias == "BUY" and d1_bias == "BEARISH" and mom_ts <= -30.0:
                mtf_counter_trend = True
            elif tentative_bias == "SELL" and d1_bias == "BULLISH" and mom_ts >= 25.0:
                mtf_counter_trend = True
        elif is_jpy:
            # JPY: Block buying falling knives when macro momentum is negative
            if tentative_bias == "BUY" and (h4_bias == "BEARISH" or d1_bias == "BEARISH" or mom_ts <= -10.0):
                mtf_counter_trend = True
            elif tentative_bias == "SELL" and (h4_bias == "BULLISH" or d1_bias == "BULLISH" or mom_ts >= 15.0):
                mtf_counter_trend = True
        else:
            # Relax Forex - Only block if D1 is strongly against AND no CHoCH
            if tentative_bias == "BUY" and d1_bias == "BEARISH":
                has_choch = bool(getattr(context.structure, "choch", False) and getattr(context.structure, "choch_type", "") == "BULLISH")
                if not has_choch and mom_ts <= -20.0:
                    mtf_counter_trend = True
            elif tentative_bias == "SELL" and d1_bias == "BULLISH":
                has_choch = bool(getattr(context.structure, "choch", False) and getattr(context.structure, "choch_type", "") == "BEARISH")
                if not has_choch and mom_ts >= 15.0:
                    mtf_counter_trend = True

        # 5. Dynamic RSI Exhaustion Bounds based on ADX and regime: 70 +- 15 * TrendPower
        adx_val = getattr(context.momentum, "adx", 20.0) if hasattr(context, "momentum") else 20.0
        trend_power = min(1.0, max(0.0, (adx_val - 20.0) / 25.0))
        rsi_upper = 70.0 + (15.0 * trend_power)
        rsi_lower = 30.0 - (15.0 * trend_power)
        rsi_val = getattr(context.momentum, "rsi", 50.0) if hasattr(context, "momentum") else 50.0

        is_expansion_reg = (
            regime.primary_regime in (
                MarketRegime.BREAKOUT, MarketRegime.POST_BREAKOUT, MarketRegime.HIGH_VOLATILITY,
                MarketRegime.TREND_BULL, MarketRegime.TREND_BEAR,
                MarketRegime.STRONG_TREND_BULL, MarketRegime.STRONG_TREND_BEAR
            )
            or getattr(context.structure, "bos", False)
            or (adx_val >= 22.0 and abs(getattr(context.momentum, "trend_score", 0.0)) >= 20.0)
        )
        has_bull_div = bool(getattr(context.momentum, "bullish_divergence", False)) if hasattr(context, "momentum") else False
        has_bear_div = bool(getattr(context.momentum, "bearish_divergence", False)) if hasattr(context, "momentum") else False
        has_counter_choch = (
            (tentative_bias == "SELL" and getattr(context.structure, "choch_type", "") == "BULLISH") or
            (tentative_bias == "BUY" and getattr(context.structure, "choch_type", "") == "BEARISH")
        )

        is_exhausted = False
        if tentative_bias == "BUY" and rsi_val > rsi_upper:
            if has_bear_div or has_counter_choch or not is_expansion_reg:
                is_exhausted = True
        elif tentative_bias == "SELL" and rsi_val < rsi_lower:
            if has_bull_div or has_counter_choch or not is_expansion_reg:
                is_exhausted = True

        from jarvis.market.sessions import SessionEngine
        mkt_status = SessionEngine.get_market_trading_status(context.symbol, dt=getattr(context, "timestamp", None))
        is_mkt_open = mkt_status.get("is_open", True)

        of_trap = context.order_flow.get("absorption_trap") if hasattr(context, "order_flow") and isinstance(context.order_flow, dict) else None
        is_of_trap = (tentative_bias == "BUY" and of_trap == "SELLER_ABSORPTION_TRAP") or (tentative_bias == "SELL" and of_trap == "BUYER_ABSORPTION_TRAP")
        kz_active = SessionEngine.is_forex_killzone_active(getattr(context, "timestamp", None))
        


        if is_index_asset:
            is_prime_session_valid = SessionEngine.is_index_prime_session(getattr(context, "timestamp", None))
        elif is_crypto or is_gold or is_oil_asset:
            is_prime_session_valid = True
        elif is_jpy:
            is_prime_session_valid = kz_active or (context.session.is_prime_session if hasattr(context, "session") and context.session else False) or (spread <= spec.typical_spread_pips * 1.5) or is_micro_mode or "SCALP" in t_style_check
        else:
            is_prime_session_valid = (
                kz_active
                or (context.session.is_prime_session if hasattr(context, "session") and context.session else False)
                or is_micro_mode
                or "SCALP" in t_style_check
                or (spread <= spec.typical_spread_pips * 1.2 and ai_score >= 70.0 and calibrated_win_p >= 0.55)
            )

        # 6. Gold (XAUUSD) Trend Following Gate: Require sweep confirmation or pullback to discount/premium
        gold_trend_following_valid = True
        effective_strat = strategy or getattr(context, "strategy", "")
        if is_gold and effective_strat in ("TREND_FOLLOWING", "BREAKDOWN", "MOMENTUM_CONTINUATION", "STRUCTURE"):
            st_zone = getattr(context.structure, "discount_premium_zone", "EQUILIBRIUM") if hasattr(context, "structure") else "EQUILIBRIUM"
            sweep_confirmed = bool(getattr(context.liquidity, "sweep_detected", False)) if hasattr(context, "liquidity") else False
            bos_active = bool(getattr(context.structure, "bos", False)) if hasattr(context, "structure") else False
            ts = getattr(context.momentum, "trend_score", 0.0) if hasattr(context, "momentum") else 0.0
            adx_val = getattr(context.momentum, "adx", 0.0) if hasattr(context, "momentum") else 0.0
            strong_expansion = (adx_val >= 20.0 and abs(ts) >= 20.0)

            if tentative_bias == "BUY":
                if not (sweep_confirmed or st_zone in ("DISCOUNT", "EQUILIBRIUM") or bos_active or (strong_expansion and ts > 0)):
                    gold_trend_following_valid = False
            elif tentative_bias == "SELL":
                if not (sweep_confirmed or st_zone in ("PREMIUM", "EQUILIBRIUM") or bos_active or (strong_expansion and ts < 0)):
                    gold_trend_following_valid = False

        # 7. Crypto Macro Trend Filter: Prevent buying into severe macro bear downtrends or shorting macro bull runs
        crypto_macro_trend_valid = True
        if is_crypto:
            ts_val = float(getattr(context.momentum, "trend_score", 0.0)) if hasattr(context, "momentum") else 0.0
            adx_val = float(getattr(context.momentum, "adx", 20.0)) if hasattr(context, "momentum") else 20.0
            min_crypto_adx = 24.0
            if adx_val < min_crypto_adx and not bool(getattr(context.structure, "choch", False)):
                crypto_macro_trend_valid = False
            elif confluence_count < 2 or ai_score < 75.0:
                crypto_macro_trend_valid = False
            elif tentative_bias == "BUY":
                if regime.primary_regime in (MarketRegime.STRONG_TREND_BEAR, MarketRegime.TREND_BEAR) or ts_val <= -15.0:
                    has_reversal = bool(getattr(context.structure, "choch", False) and getattr(context.liquidity, "sweep_detected", False))
                    if not (has_reversal and ai_score >= 82.0 and calibrated_win_p >= 0.65):
                        crypto_macro_trend_valid = False
            elif tentative_bias == "SELL":
                if regime.primary_regime in (MarketRegime.STRONG_TREND_BULL, MarketRegime.TREND_BULL) or ts_val >= 15.0:
                    has_reversal = bool(getattr(context.structure, "choch", False) and getattr(context.liquidity, "sweep_detected", False))
                    if not (has_reversal and ai_score >= 82.0 and calibrated_win_p >= 0.65):
                        crypto_macro_trend_valid = False

        # 8. Forex False Breakout Guard: Prevent trading false breakout expansions on choppy Forex pairs
        forex_breakout_valid = True
        if _is_forex(context.symbol) and not is_gold:
            if regime.primary_regime in (MarketRegime.BREAKOUT, MarketRegime.POST_BREAKOUT, MarketRegime.HIGH_VOLATILITY):
                adx_v = getattr(context.momentum, "adx", 0.0) if hasattr(context, "momentum") else 0.0
                bos_v = getattr(context.structure, "bos", False) if hasattr(context, "structure") else False
                if not (adx_v >= 30.0 and bos_v):
                    forex_breakout_valid = False

        # 9. Index Bull Run Counter-Trend Shorting Guard: Strictly protect equity indices from counter-trend shorting
        index_counter_trend_valid = True
        if is_index_asset:
            ts_v = getattr(context.momentum, "trend_score", 0.0) if hasattr(context, "momentum") else 0.0
            rsi_v = getattr(context.momentum, "rsi", 50.0) if hasattr(context, "momentum") else 50.0
            if tentative_bias == "SELL":
                d1_b = mtf_align.get("D1", "NEUTRAL") if isinstance(mtf_align, dict) else "NEUTRAL"
                h4_b = mtf_align.get("H4", "NEUTRAL") if isinstance(mtf_align, dict) else "NEUTRAL"
                has_rev = bool(getattr(context.structure, "choch", False) and getattr(context.structure, "choch_type", "") == "BEARISH")
                if not ((d1_b == "BEARISH" or h4_b == "BEARISH") and has_rev and ts_v <= -30.0 and ai_score >= 85.0):
                    index_counter_trend_valid = False
            elif tentative_bias == "BUY":
                if (ts_v <= -25.0 and not bool(getattr(context.structure, "choch", False))) or rsi_v >= 62.0:
                    index_counter_trend_valid = False

        # 10. Low-Beta FX (AUDUSD, USDCHF, NZDUSD, USDCAD) Macro Alignment Guard
        low_beta_fx_macro_valid = True
        if any(k in sym_name for k in ["AUD", "CHF", "NZD", "CAD"]) and not is_gold:
            d1_b = mtf_align.get("D1", "NEUTRAL") if isinstance(mtf_align, dict) else "NEUTRAL"
            ts_val = float(getattr(context.momentum, "trend_score", 0.0)) if hasattr(context, "momentum") else 0.0
            has_structure = bool(getattr(context.structure, "bos", False) or getattr(context.liquidity, "sweep_detected", False))
            if tentative_bias == "BUY" and d1_b == "BEARISH" and ts_val <= -25.0 and not has_structure:
                low_beta_fx_macro_valid = False
            elif tentative_bias == "SELL" and d1_b == "BULLISH" and ts_val >= 20.0 and not has_structure:
                low_beta_fx_macro_valid = False

        # 11. JPY Secular Bull/Carry Alignment: USDJPY is driven by US/JP rate differential; block counter-trend shorts unless D1 is decisively bearish
        jpy_momentum_valid = True
        if is_jpy:
            d1_b = mtf_align.get("D1", "NEUTRAL") if isinstance(mtf_align, dict) else "NEUTRAL"
            ts_v = getattr(context.momentum, "trend_score", 0.0) if hasattr(context, "momentum") else 0.0
            adx_v = getattr(context.momentum, "adx", 0.0) if hasattr(context, "momentum") else 0.0
            if tentative_bias == "SELL" and not (d1_b == "BEARISH" and ts_v <= -30.0):
                jpy_momentum_valid = False
            elif adx_v < 18.0 and abs(ts_v) < 15.0:
                jpy_momentum_valid = False
            elif ai_score < 72.0 or calibrated_win_p < 0.62:
                jpy_momentum_valid = False

        # 12. High-Beta Crypto (SOLUSD) Confluence Guard
        sol_confluence_valid = True
        if "SOL" in sym_name:
            if confluence_count < 2 or ai_score < 75.0:
                sol_confluence_valid = False

        # 13. US30 Industrial Index Confluence Guard
        us30_confluence_valid = True
        if "US30" in sym_name:
            if confluence_count < 2 or ai_score < 78.0:
                us30_confluence_valid = False

        # 14. Institutional Order Flow Alignment Guard (Strictly preserves Gold)
        order_flow_aligned = True
        if of_res and not is_gold:
            if of_res.get("institutional_activity", False) and of_res.get("signal") not in ("NEUTRAL", tentative_bias):
                order_flow_aligned = False
            trap = of_res.get("absorption_trap")
            if trap == "SELLER_ABSORPTION_TRAP" and tentative_bias == "BUY":
                order_flow_aligned = False
            elif trap == "BUYER_ABSORPTION_TRAP" and tentative_bias == "SELL":
                order_flow_aligned = False

        # 15. Strategy Viability Guard (Strictly preserves Gold)
        strategy_viable = True
        if strategy and not is_gold:
            cfg = get_symbol_profile_config(sym_name)
            if strategy in cfg.banned_strategies:
                strategy_viable = False

        # Institutional Quality Gate Matrix
        regime_viable = regime.primary_regime != MarketRegime.EVENT_RISK
        if regime.primary_regime == MarketRegime.WEAK_TREND:
            if not (ai_score >= 80.0 and calibrated_win_p >= 0.60):
                regime_viable = False

        gate_checks = {
            "Market Session Open": is_mkt_open,
            "Drawdown Safety Guard": current_drawdown_pct <= 10.0,
            "Regime Viability": regime_viable,
            "Directional Bias": tentative_bias in ["BUY", "SELL"],
            "Strategy Viable": strategy_viable,
            "Risk/Reward >= 1.5": rr_ratio >= min_rr,
            "Positive Expected Value": ev > 0 and ev >= effective_min_ev,
            "Spread Protection": spread <= max_spread and not context.volatility.is_excessive_spread,
            "AI Multi-Score Gate": ai_score >= min_score,
            "Devil Adversarial Guard": devil_report.penalty_score <= self.max_devil_penalty,
            "Calibrated Win Prob >= 50%": calibrated_win_p >= (required_win_p - 0.005),
            "Valid Stop Loss Distance": risk_dist >= (context.volatility.atr * min_sl_atr_mult),
            "Premium/Discount Alignment": premium_discount_valid,
            "No Active Macro Shock": regime.primary_regime != MarketRegime.EVENT_RISK,
            "Order Flow Momentum": abs(context.momentum.trend_score) >= 10 or context.structure.bos or context.liquidity.sweep_detected,
            "Order Flow Alignment": order_flow_aligned,
            "Macro MTF Alignment": not mtf_counter_trend,
            "Trend Not Exhausted": not is_exhausted,
            "No Order Flow Absorption Trap": not is_of_trap,
            "Forex Prime Session": is_prime_session_valid,
            "Gold Trend Following Alignment": gold_trend_following_valid,
            "Crypto Macro Trend Filter": crypto_macro_trend_valid,
            "Forex Breakout Guard": forex_breakout_valid,
            "Index Trend Alignment": index_counter_trend_valid,
            "Low-Beta FX Macro Alignment": low_beta_fx_macro_valid,
            "JPY Momentum Guard": jpy_momentum_valid,
            "SOL Confluence Guard": sol_confluence_valid,
            "US30 Confluence Guard": us30_confluence_valid,
            "Margin Capacity Limit": account_balance >= 10.0 and planned_risk_dollars > 0
        }

        failing_reasons = [name for name, passed in gate_checks.items() if not passed]
        gate_passed = len(failing_reasons) == 0

        return TradeQualityGateResult(passed=gate_passed, checks=gate_checks, failing_reasons=failing_reasons)

    def _blended_ai_score(self, analyst_reports: Dict[str, AnalystReport],
                          symbol: str = "") -> float:
        """The plain mean of the analyst scores -- over analysts that RAN.

        A timed-out analyst is substituted by `parallel_runner` with a hardcoded
        NEUTRAL 50.0 report. Counting that as a reading is the same defect as
        scoring a fabricated news calendar: an invented value entering a hard
        gate (70/72/75/78/80/82/85, all in this module). Measured context: the
        cluster's 2.0s budget is smaller than the socket timeout of the news
        fetch MACRO calls synchronously, so on a merely slow network the
        fallback is the EXPECTED outcome, not an edge case.

        Fail-open is preserved -- a partial panel still trades, on a SMALLER
        panel rather than a padded one. The degenerate case (nothing answered)
        yields 0.0, which fails every gate: that is the correct reading of "no
        analyst answered", and the alternative -- substituting a neutral 50 --
        would be inventing the very reading we are missing.
        """
        answered = answered_reports(analyst_reports)
        if not answered:
            if analyst_reports:
                logger.warning("[%s] All %d analyst reports are fallbacks -- "
                               "ai_score forced to 0.0 (no analyst answered).",
                               symbol, len(analyst_reports))
            return 0.0
        return sum(r.score for r in answered) / len(answered)

    def evaluate(
        self,
        context: MarketContext,
        regime: RegimeOutput,
        analyst_reports: Dict[str, AnalystReport],
        devil_report: DevilAdvocateReport,
        account_balance: float = 10000.0,
        risk_per_trade_pct: float = 0.5,
        current_drawdown_pct: float = 0.0,
        mtf_data=None,
        recent_candles: Optional[List[Dict[str, Any]]] = None,
        trade_style: str = "SWING"
    ) -> DecisionObject:
        style = trade_style or getattr(context, "trade_style", "SWING") or "SWING"
        levels_res = self._compute_bias_and_levels(
            context, regime, analyst_reports, trade_style=style,
            account_balance=account_balance,
            risk_per_trade_pct=risk_per_trade_pct,
        )
        (
            tentative_bias, entry_price, sl_price, tp_price,
            risk_dist, rr_ratio, first_target_price, first_target_volume_pct
        ) = levels_res
        tp1_price = getattr(levels_res, "tp1_price", first_target_price)
        tp2_price = getattr(levels_res, "tp2_price", tp_price)
        tp3_price = getattr(levels_res, "tp3_price", None)

        # §B-5: Devil's Advocate Threat Feedback Adjustment
        threat_lvl = getattr(devil_report, "threat_price_level", None) if devil_report else None
        if threat_lvl is not None and isinstance(threat_lvl, (int, float)) and threat_lvl > 0:
            spec = resolve_symbol(context.symbol)
            atr_val = context.volatility.atr if context.volatility.atr > 0 else (entry_price * 0.005)
            if tentative_bias == "BUY" and entry_price < threat_lvl < tp_price:
                adjusted_tp = round(threat_lvl - (atr_val * 0.1), spec.digits)
                if adjusted_tp >= entry_price + (risk_dist * 1.0):
                    logger.info(f"[{context.symbol}] Devil's Advocate threat level {threat_lvl} detected ahead of TP! Tucking TP: {tp_price} -> {adjusted_tp}")
                    tp_price = adjusted_tp
                    tp2_price = adjusted_tp
                    tp_dist = tp_price - entry_price
                    rr_ratio = round(tp_dist / (risk_dist + 1e-9), 2)
            elif tentative_bias == "SELL" and tp_price < threat_lvl < entry_price:
                adjusted_tp = round(threat_lvl + (atr_val * 0.1), spec.digits)
                if adjusted_tp <= entry_price - (risk_dist * 1.0):
                    logger.info(f"[{context.symbol}] Devil's Advocate threat level {threat_lvl} detected ahead of TP! Tucking TP: {tp_price} -> {adjusted_tp}")
                    tp_price = adjusted_tp
                    tp2_price = adjusted_tp
                    tp_dist = entry_price - tp_price
                    rr_ratio = round(tp_dist / (risk_dist + 1e-9), 2)



        strategy_probs = self.strategy_selector.select_strategy_probabilities(
            regime, context=context, account_equity=account_balance
        )
        best_strategy = max(strategy_probs.items(), key=lambda x: x[1])[0]

        ai_score = self._blended_ai_score(analyst_reports, context.symbol)

        of_res = {"signal": "NEUTRAL", "strength": 0.0, "institutional_activity": False}
        if mtf_data and "primary" in mtf_data and not mtf_data["primary"].empty:
            of_res = self.order_flow.analyze_order_flow(mtf_data["primary"])
            
            if of_res["institutional_activity"] and of_res["signal"] == tentative_bias:
                ai_score = min(100.0, ai_score + (of_res["strength"] * 10.0))
                logger.debug(f"[{context.symbol}] Institutional Order Flow aligns with {tentative_bias}! Boosting AI score to {ai_score:.1f}")
            elif of_res["institutional_activity"] and of_res["signal"] != "NEUTRAL":
                ai_score = max(0.0, ai_score - (of_res["strength"] * 10.0))
                logger.debug(f"[{context.symbol}] Institutional Order Flow opposes {tentative_bias}! Penalizing AI score to {ai_score:.1f}")

        # AI10: `raw_prob` is the pre-calibration hypothesis probability — the value
        # `calibrator.calibrate_probability()` was actually applied to. It is carried out
        # here so it can be persisted and later used to REFIT the curve. The refit must
        # never bin on `model_confidence`: that number is this pipeline's own downstream
        # output (blended with the ML predictor, then boosted and penalised through
        # lines below), so fitting on it is fitting on the calibrator's own output.
        (final_win_p, loss_p, ev, hypotheses, calibrated_win_p,
         honest_base_rate, raw_prob) = self._compute_blended_probability(
            context, regime, analyst_reports, devil_report, tentative_bias, rr_ratio, risk_dist, account_balance, risk_per_trade_pct
        )
        
        # 1. Apply Empirical Trade Pattern Memory Feedback (B1-WIRING)
        regime_str = regime.primary_regime.value if regime and hasattr(regime, "primary_regime") else "GLOBAL"
        session_str = context.session.current_session if context.session else "UNKNOWN"
        is_prime = context.session.is_prime_session if context.session else True
        pattern_memory = self.self_learning.get_pattern_win_rate_and_ev(
            symbol=context.symbol,
            regime=regime_str,
            session_name=session_str,
            is_prime=is_prime
        )
        if pattern_memory.get("sample_size", 0) >= 3:
            p_mult = pattern_memory.get("conviction_multiplier", 1.0)
            calibrated_win_p = min(0.99, max(0.10, calibrated_win_p * p_mult))
            final_win_p = min(0.99, max(0.10, final_win_p * p_mult))
            if pattern_memory.get("empirical_edge"):
                ai_score = min(100.0, ai_score + 5.0)
                logger.info(f"[{context.symbol}] Empirical pattern edge: WinRate={pattern_memory['win_rate']*100:.0f}%, EV={pattern_memory['avg_ev']:.2f}")

        # 2. Apply Post-News Stop-Hunt Liquidity Sweep Reaction (B5-WIRING)
        news_reaction = GLOBAL_NEWS_ENGINE.evaluate_post_news_sweep_reaction(
            symbol=context.symbol,
            sweep_detected=context.liquidity.sweep_detected,
            sweep_type=context.liquidity.sweep_type,
            sweep_magnitude_pips=context.liquidity.sweep_magnitude
        )
        if news_reaction.get("news_reversal_setup"):
            c_boost = news_reaction.get("conviction_boost", 0.0)
            calibrated_win_p = min(0.99, calibrated_win_p + c_boost)
            final_win_p = min(0.99, final_win_p + c_boost)
            ai_score = min(100.0, ai_score + 8.0)
            logger.info(f"[{context.symbol}] {news_reaction.get('reason')}")

        # 3. Apply Multi-Timeframe Top-Down Confluence Score (B7-WIRING)
        mtf_score = getattr(context, "mtf_confluence_score", 0.0)
        if tentative_bias == "BUY":
            if mtf_score >= 30.0:
                ai_score = min(100.0, ai_score + (mtf_score / 20.0))
            elif mtf_score <= -30.0:
                ai_score = max(0.0, ai_score - (abs(mtf_score) / 10.0))
        elif tentative_bias == "SELL":
            if mtf_score <= -30.0:
                ai_score = min(100.0, ai_score + (abs(mtf_score) / 20.0))
            elif mtf_score >= 30.0:
                ai_score = max(0.0, ai_score - (mtf_score / 10.0))

        # 3.5. Apply Order Flow Volume Delta Score (E1-WIRING)
        of_data = getattr(context, "order_flow", {})
        delta_score = float(of_data.get("delta_score", 0.0)) if isinstance(of_data, dict) else 0.0
        if tentative_bias == "BUY":
            if delta_score >= 35.0:
                ai_score = min(100.0, ai_score + 5.0)
            elif delta_score <= -35.0:
                ai_score = max(0.0, ai_score - 8.0)
        elif tentative_bias == "SELL":
            if delta_score <= -35.0:
                ai_score = min(100.0, ai_score + 5.0)
            elif delta_score >= 35.0:
                ai_score = max(0.0, ai_score - 8.0)

        # 4. Standard Regime Multiplier
        if regime:
            sl_multiplier = self.self_learning.get_regime_multiplier(regime_str)
            if sl_multiplier != 1.0:
                logger.info(f"[{context.symbol}] Self-Learning Engine adjusting {regime_str} Win Prob by {sl_multiplier}x")
                calibrated_win_p = min(0.99, calibrated_win_p * sl_multiplier)
                final_win_p = min(0.99, final_win_p * sl_multiplier)

        is_fx = _is_forex(context.symbol)

        # 4.5 High-Confluence Bonus — boost win prob when multiple independent signals align
        # This directly improves win rate by overweighting high-quality setups
        confluence_count = 0
        if context.structure.bos:
            confluence_count += 1
        if context.liquidity.sweep_detected:
            confluence_count += 1
        if abs(getattr(context.momentum, "trend_score", 0)) >= 20:
            confluence_count += 1
        if abs(mtf_score) >= 30:
            confluence_count += 1
        if of_res.get("institutional_activity"):
            confluence_count += 1
        if confluence_count >= 3:
            bonus = 0.035 + (0.015 * min(2, confluence_count - 3))  # 0.035 for 3, 0.050 for 4-5
            calibrated_win_p = min(0.95, calibrated_win_p + bonus)
            final_win_p = min(0.95, final_win_p + bonus)
            ai_score = min(100.0, ai_score + (confluence_count * 2.0))
            logger.info(f"[{context.symbol}] High confluence ({confluence_count}/5) bonus +{bonus:.3f} win prob")

        # Targeted extra boost for all symbols when 4+ confluence
        if confluence_count >= 4:
            extra_boost = 0.020 if is_fx else 0.015
            calibrated_win_p = min(0.95, calibrated_win_p + extra_boost)
            final_win_p = min(0.95, final_win_p + extra_boost)

        # Symbol-specific optimizations for 75% win rate target
        sym_upper = context.symbol.upper()

        if "EUR" in sym_upper or "GBP" in sym_upper:
            if is_prime:
                calibrated_win_p = min(0.95, calibrated_win_p + 0.03)
                final_win_p = min(0.95, final_win_p + 0.03)
        elif "USDJPY" in sym_upper:
            adx_val = getattr(context.momentum, "adx", 20.0) if hasattr(context, "momentum") else 20.0
            if adx_val >= 25:
                calibrated_win_p = min(0.95, calibrated_win_p + 0.04)
                final_win_p = min(0.95, final_win_p + 0.04)
        elif "BTC" in sym_upper or "ETH" in sym_upper:
            if regime.primary_regime in (MarketRegime.RANGE, MarketRegime.COMPRESSION):
                calibrated_win_p = min(0.95, calibrated_win_p + 0.05)
                final_win_p = min(0.95, final_win_p + 0.05)

        # 4.6 AI Dissection — 7-pillar real-time confluence scoring (boost only, no gate)
        _dissection = self.ai_dissector.dissect(context, regime, rr_ratio, ev, ai_score, calibrated_win_p)
        _dissection_score = _dissection["dissection_score"]
        _dissection_tier = _dissection["tier"]
        calibrated_win_p = min(0.95, max(0.05, calibrated_win_p + float(_dissection["prob_boost"])))
        final_win_p = min(0.95, max(0.05, final_win_p + float(_dissection["prob_boost"])))
        if _dissection_score >= 70:
            logger.info(f"[{context.symbol}] AI Dissection HIGH {_dissection_score:.1f} tier={_dissection_tier} boost +{_dissection['prob_boost']:.3f}")

        # 4.7 Master Confluence — proven stacks from trading masters (Wyckoff+ICT+VCP+Triple) — boost + HARD GATE
        _master = self.master_confluence.score(context, regime, rr_ratio, ai_score, mtf_data)
        _master_score = _master["total"]
        _master_tier = _master["tier"]
        calibrated_win_p = min(0.95, max(0.05, calibrated_win_p + float(_master["prob_boost"])))
        final_win_p = min(0.95, max(0.05, final_win_p + float(_master["prob_boost"])))
        
        # HARD GATE: Horizon-Adaptive Master Confluence Threshold
        is_micro_mode = is_micro_account(account_balance)
        is_crypto = "BTC" in context.symbol.upper() or "ETH" in context.symbol.upper() or "SOL" in context.symbol.upper()
        t_style = (getattr(context, "trade_style", None) or getattr(context, "style", "SWING") or "SWING").upper()
        if "SCALP" in t_style:
            _min_confluence = 18
        elif any(x in t_style for x in ("DAY", "INTRADAY")):
            _min_confluence = 20
        elif _is_forex(context.symbol):
            _min_confluence = 20
        elif is_crypto:
            _min_confluence = 24
        else:
            _min_confluence = 22

        if (rr_ratio >= 2.0 and ev > 0) or is_micro_mode:
            _min_confluence = max(18, _min_confluence - 4)

        master_confluence_valid = _master_score >= _min_confluence
        
        if _master_tier in ("ELITE", "HIGH"):
            logger.info(f"[{context.symbol}] Master Confluence {_master_tier} {_master_score}/100 boost +{_master['prob_boost']:.3f} {_master['breakdown']}")

        # 4.8 ICT FVG & Order Block Imbalance Analysis
        if mtf_data and "primary" in mtf_data and not mtf_data["primary"].empty:
            try:
                _fvg_res = self.fvg_engine.analyze(mtf_data["primary"])
                if _fvg_res:
                    _in_fvg = _fvg_res.get("price_in_fvg", False)
                    _in_ob = _fvg_res.get("price_in_ob", False)
                    _fvg_ob_conf = _fvg_res.get("fvg_ob_confluence", False)
                    _ote = _fvg_res.get("ote_zone", {})
                    _in_ote_dir = (tentative_bias == "BUY" and _ote.get("direction") == "BULLISH") or (tentative_bias == "SELL" and _ote.get("direction") == "BEARISH")
                    
                    _fvg_boost = 0.0
                    if _in_fvg or _in_ob:
                        _fvg_boost += 0.04
                        ai_score = min(100.0, ai_score + 6.0)
                    if _fvg_ob_conf:
                        _fvg_boost += 0.06
                        ai_score = min(100.0, ai_score + 8.0)
                    if _in_ote_dir:
                        _fvg_boost += 0.04
                        ai_score = min(100.0, ai_score + 5.0)
                        
                    if _fvg_boost > 0:
                        calibrated_win_p = min(0.95, calibrated_win_p + _fvg_boost)
                        final_win_p = min(0.95, final_win_p + _fvg_boost)
                        logger.info(f"[{context.symbol}] ICT FVG/OB Confluence boost +{_fvg_boost:.3f} (in_fvg={_in_fvg}, in_ob={_in_ob}, fvg_ob_conf={_fvg_ob_conf})")
            except Exception as e:
                logger.warning(f"[{context.symbol}] FVG analysis error: {e}")


        # =========================================================================
        # 5. DYNAMIC STRATEGY SELECTION BY EXPECTED VALUE (EV) & SETUP QUALITY
        # Regime -> Asset Class -> Strategy Candidates -> Setup Quality -> Expected Value -> Risk
        # =========================================================================
        candidate_strategies = [s for s, w in strategy_probs.items() if w > 0]
        if not candidate_strategies:
            candidate_strategies = ["TREND_FOLLOWING"]

        strategy_evaluations: Dict[str, Dict[str, Any]] = {}
        _risk_dollars = max(0.50, account_balance * (risk_per_trade_pct / 100.0))
        _spec = resolve_symbol(context.symbol)
        _est_lots = max(0.01, _risk_dollars / (max(risk_dist, 1e-4) * _spec.contract_size))
        _spread_cost = context.volatility.current_spread_pips * _spec.pip_value_per_lot * _est_lots
        _slippage = (context.volatility.atr * 0.02) * _est_lots

        is_gold_asset = any(k in context.symbol.upper() for k in ["XAU", "GOLD"])
        is_oil_asset = any(k in context.symbol.upper() for k in ["WTI", "OIL", "CRUDE"])

        for strat in candidate_strategies:
            strat_weight = strategy_probs.get(strat, 0.0)
            strat_p = final_win_p
            strat_rr = rr_ratio

            # Prerequisite trigger validation for non-benchmark assets:
            # A reversal strategy requires actual structural or liquidity trigger evidence
            if not (is_gold_asset or is_oil_asset):
                if strat == "CHOCH_STRUCTURAL_REVERSAL" and not bool(getattr(context.structure, "choch", False)):
                    continue
                if strat == "LIQUIDITY_SWEEP_REVERSAL" and not bool(getattr(context.liquidity, "sweep_detected", False)):
                    continue

            # Strategy-specific edge & RR adjustments
            if strat == "RANGE_MEAN_REVERSION":
                if context.momentum.adx < 20:
                    strat_p = min(0.95, strat_p + 0.03)
                strat_rr = min(2.0, max(1.6, strat_rr * 0.9))
            elif strat == "TREND_FOLLOWING":
                if context.momentum.adx >= 25 and context.structure.bos:
                    strat_p = min(0.95, strat_p + 0.03)
                strat_rr = max(2.2, strat_rr * 1.1)
            elif strat == "BREAKOUT_EXPANSION":
                if context.volatility.state in ("EXPANSION", "EXTREME"):
                    strat_p = min(0.95, strat_p + 0.03)
                strat_rr = max(2.5, strat_rr * 1.15)
            elif strat == "LIQUIDITY_SWEEP_REVERSAL":
                if context.liquidity.sweep_detected:
                    strat_p = min(0.95, strat_p + 0.05)
                strat_rr = max(2.0, strat_rr * 1.05)
            elif strat == "CHOCH_STRUCTURAL_REVERSAL":
                if context.structure.choch:
                    strat_p = min(0.95, strat_p + 0.05)
                strat_rr = max(2.0, strat_rr * 1.05)

            strat_loss_p = round(1.0 - strat_p, 2)
            strat_win_dollars = _risk_dollars * strat_rr
            strat_ev = (strat_p * strat_win_dollars) - (strat_loss_p * _risk_dollars) - _spread_cost - _slippage
            
            # Multi-objective fitness score: EV * (1 + (WinP - 0.50)/0.50) * StrategySuitabilityWeight
            prob_factor = max(0.5, 1.0 + (strat_p - 0.50) / 0.50)
            if strat_weight <= 0.001:
                fitness = -1e9
            else:
                fitness = strat_ev * prob_factor * strat_weight

            strategy_evaluations[strat] = {
                "win_p": strat_p,
                "rr": strat_rr,
                "ev": round(strat_ev, 2),
                "fitness": fitness,
                "weight": strat_weight
            }

        if not strategy_evaluations:
            fallback_strat = "TREND_PULLBACK" if "TREND_PULLBACK" in candidate_strategies else candidate_strategies[0]
            strategy_evaluations[fallback_strat] = {
                "win_p": final_win_p,
                "rr": rr_ratio,
                "ev": 0.0,
                "fitness": -1e6,
                "weight": 0.1
            }

        # Select strategy with highest validated fitness / EV
        best_strategy = max(strategy_evaluations.items(), key=lambda x: x[1]["fitness"])[0]
        selected_eval = strategy_evaluations[best_strategy]
        
        final_win_p = selected_eval["win_p"]
        loss_p = round(1.0 - final_win_p, 2)
        ev = selected_eval["ev"]

        st = context.structure
        premium_discount_valid = True
        trend_score_val = getattr(context.momentum, "trend_score", 0.0) if hasattr(context, "momentum") else 0.0
        # Institutional ICT Smart Money Rule: Never BUY in Premium, Never SELL in Discount without exception unless extreme momentum (|trend_score| >= 65)
        spec_eval = resolve_symbol(context.symbol)
        sym_name_eval = str(context.symbol).upper()
        is_crypto_sym = getattr(spec_eval, "is_crypto", False) or (getattr(spec_eval, "asset_class", "") == "CRYPTO") or any(k in sym_name_eval for k in ["BTC", "ETH", "SOL"])
        is_index_sym = getattr(spec_eval, "asset_class", "") == "INDEX" or any(k in sym_name_eval for k in ["US500", "NAS100", "US30", "SPX", "NDX", "DJ"])
        if _is_forex(context.symbol):
            if tentative_bias == "BUY" and st.discount_premium_zone == "PREMIUM" and trend_score_val < 65:
                premium_discount_valid = False
            elif tentative_bias == "SELL" and st.discount_premium_zone == "DISCOUNT" and trend_score_val > -65:
                premium_discount_valid = False
        elif is_crypto_sym:
            if tentative_bias == "BUY" and st.discount_premium_zone == "PREMIUM" and trend_score_val < 45:
                premium_discount_valid = False
            elif tentative_bias == "SELL" and st.discount_premium_zone == "DISCOUNT" and trend_score_val > -45:
                premium_discount_valid = False
        elif is_index_sym:
            if tentative_bias == "BUY" and st.discount_premium_zone == "PREMIUM" and trend_score_val < 40:
                premium_discount_valid = False
            elif tentative_bias == "SELL" and st.discount_premium_zone == "DISCOUNT" and trend_score_val > -40:
                premium_discount_valid = False
        else:
            if tentative_bias == "BUY" and st.discount_premium_zone == "PREMIUM" and trend_score_val < 40:
                if not (context.structure.bos or context.liquidity.sweep_detected):
                    premium_discount_valid = False
            elif tentative_bias == "SELL" and st.discount_premium_zone == "DISCOUNT" and trend_score_val > -40:
                if not (context.structure.bos or context.liquidity.sweep_detected):
                    premium_discount_valid = False

        planned_risk_dollars = max(0.50, account_balance * (risk_per_trade_pct / 100.0))

        quality_gate = self._apply_quality_gate(
            context=context, regime=regime, devil_report=devil_report, ai_score=ai_score,
            rr_ratio=rr_ratio, ev=ev, final_win_p=final_win_p, spread=context.volatility.current_spread_pips,
            premium_discount_valid=premium_discount_valid, account_balance=account_balance,
            current_drawdown_pct=current_drawdown_pct, tentative_bias=tentative_bias,
            calibrated_win_p=calibrated_win_p, risk_dist=risk_dist, planned_risk_dollars=planned_risk_dollars,
            strategy=best_strategy, of_res=of_res
        )
        
        from jarvis.market.sessions import SessionEngine
        mkt_status = SessionEngine.get_market_trading_status(context.symbol, dt=getattr(context, "timestamp", None))
        is_mkt_open = mkt_status.get("is_open", True)

        gate_passed = quality_gate.passed
        failing_reasons = list(quality_gate.failing_reasons)

        # Hard Master Confluence Gate — reject low-confluence setups
        if not master_confluence_valid:
            gate_passed = False
            failing_reasons.append(f"Master Confluence Below Minimum ({_master_score}/100 < {_min_confluence})")

        # ---- ML Meta-Label confirmation gate (safe: neutral until a model is trained) ----
        meta_label_prob = None
        gate_policy_decision = "PASS"
        softened_gates: List[str] = []
        META_CHECK = "ML Meta-Label Confirmation"
        if not (recent_candles and len(recent_candles) >= self.meta_labeler.MIN_WINDOW):
            # AI4: too little history to evaluate. The check is simply absent from
            # `checks` today, and "absent" is unreadable downstream — it does not block,
            # so it is indistinguishable from "evaluated and confirmed".
            quality_gate.not_evaluated.append(META_CHECK)
        else:
            _bias = 1.0 if tentative_bias == "BUY" else (-1.0 if tentative_bias == "SELL" else 0.0)
            meta_label_prob = self.meta_labeler.predict_proba(recent_candles, bias=_bias)
            if meta_label_prob is None:
                # AI4: no fitted model (or the prediction failed). Staying neutral here
                # is deliberate and evidence-based, not an oversight — see
                # MetaLabeler._load: measured test AUC 0.481 with top-decile selection
                # LOWERING the win rate (0.341 vs a 0.359 base). Wiring this model into
                # the gate or the sizing probability would add a coin flip to the number
                # that sizes positions. But "neutral" must not read as "confirmed".
                quality_gate.not_evaluated.append(META_CHECK)
            else:
                quality_gate.checks[META_CHECK] = meta_label_prob >= self.meta_labeler.MIN_PROB
                quality_gate.failing_reasons = [
                    r for r in quality_gate.failing_reasons if r != META_CHECK
                ]
                if meta_label_prob < self.meta_labeler.MIN_PROB:
                    quality_gate.failing_reasons.append(META_CHECK)
                quality_gate.passed = len(quality_gate.failing_reasons) == 0
                failing_reasons = quality_gate.failing_reasons
                gate_passed = quality_gate.passed

        # ---- Adaptive Quality-Gate Policy: Allow soft-gate bypass when no hard gates fail ----
        softened_gates: List[str] = []
        if gate_passed:
            gate_policy_decision = "PASS"
        else:
            _gp_decision, _gp_gates = self.gate_policy.decide(failing_reasons, recent_win_rate=None)
            if _gp_decision == "SOFTEN":
                gate_policy_decision = "SOFTEN"
                softened_gates = _gp_gates
                _penalty = self.gate_policy.confidence_penalty(softened_gates)
                calibrated_win_p = max(0.05, calibrated_win_p - _penalty)
                gate_passed = True
                logger.info(f"[QualityGate] SOFTENED {len(softened_gates)} gates (confidence penalty: -{_penalty:.3f})")
            elif _gp_decision == "PASS":
                gate_policy_decision = "PASS"
                gate_passed = True
            else:
                gate_policy_decision = "BLOCK"

        if not is_mkt_open:
            decision_action = "NO_TRADE"
        elif gate_passed:
            if tentative_bias in ["BUY", "SELL"]:
                decision_action = "EXECUTE"
            else:
                decision_action = "NO_TRADE"
        elif tentative_bias in ["BUY", "SELL"] and len(failing_reasons) <= 2 and quality_gate.checks.get("Regime Viability", False):
            decision_action = "WAIT"
        else:
            decision_action = "NO_TRADE"

        waiting_reasons = []
        rejection_reasons = []

        if gate_policy_decision == "SOFTEN" and softened_gates:
            waiting_reasons.extend([f"Softened gate (AI adaptive, win-rate evidence): {g}" for g in softened_gates])

        if not is_mkt_open:
            rejection_reasons.append(
                f"Market session is closed for the weekend ({mkt_status.get('reason', 'Weekend Close')}). Live order execution halted until session opens on {mkt_status.get('next_open_ist', 'Monday')}."
            )
        elif decision_action == "WAIT":
            for reason in failing_reasons:
                if "Calibrated Win Prob" in reason:
                    waiting_reasons.append(f"Calibrated probability ({calibrated_win_p*100:.1f}%) below required threshold (>= 55%).")
                elif "Order Flow" in reason:
                    waiting_reasons.append("Awaiting institutional volume / order flow momentum confirmation.")
                elif "Premium/Discount" in reason:
                    zone = context.structure.discount_premium_zone
                    waiting_reasons.append(f"Price currently in {zone} zone -- awaiting retracement into favorable discount/equilibrium.")
                elif "Risk/Reward" in reason:
                    waiting_reasons.append(f"Current setup R:R (1:{rr_ratio:.2f}) awaiting optimal price fill.")
                elif "Positive Expected Value" in reason:
                    waiting_reasons.append(f"Expected value (${ev:.2f}) awaiting higher statistical edge.")
                elif "AI Multi-Score" in reason:
                    waiting_reasons.append(f"Blended AI score ({ai_score:.1f}) pending multi-agent consensus.")
                elif "Spread Protection" in reason:
                    waiting_reasons.append(f"Spread ({context.volatility.current_spread_pips:.1f} pips) elevated — awaiting spread normalization.")
                else:
                    waiting_reasons.append(f"Awaiting validation check: {reason}.")
            if not waiting_reasons:
                waiting_reasons.append("Awaiting confirmation of institutional entry trigger and candle close.")

        elif decision_action == "NO_TRADE" or not gate_passed:
            for reason in failing_reasons:
                if "Positive Expected Value" in reason:
                    rejection_reasons.append(f"Negative / insufficient mathematical edge (EV: ${ev:.2f}).")
                elif "Devil Adversarial" in reason:
                    rejection_reasons.append(f"Adversarial counter-thesis penalty too high ({devil_report.penalty_score:.1f} / {self.max_devil_penalty:.1f}).")
                elif "Premium/Discount" in reason:
                    zone = context.structure.discount_premium_zone
                    rejection_reasons.append(f"Unfavorable pricing zone ({zone}) for {tentative_bias} execution.")
                elif "Regime Viability" in reason:
                    rejection_reasons.append(f"Regime {regime.primary_regime.value} classified as hazardous / non-tradable.")
                elif "Spread Protection" in reason:
                    rejection_reasons.append(f"Spread {context.volatility.current_spread_pips:.1f} pips exceeds maximum tolerable risk limit.")
                elif "Drawdown Safety" in reason:
                    rejection_reasons.append("Account drawdown exceeds safety threshold (5.0%).")
                elif "Calibrated Win Prob" in reason:
                    rejection_reasons.append(f"Model win probability ({calibrated_win_p*100:.0f}%) fails minimum hurdle.")
                else:
                    rejection_reasons.append(f"Failed quality check: {reason}.")
            for threat in devil_report.threats_detected[:2]:
                if threat not in rejection_reasons:
                    rejection_reasons.append(f"Adversarial risk: {threat}")
            if not rejection_reasons and tentative_bias == "HOLD":
                rejection_reasons.append("No actionable institutional market structure or clear directional bias detected.")

        bull_case = []
        bear_case = []
        for rep in analyst_reports.values():
            if rep.bias == "BULLISH":
                bull_case.extend(rep.evidence)
            elif rep.bias == "BEARISH":
                bear_case.extend(rep.evidence)

        probabilities = {
            "buy": round(calibrated_win_p if tentative_bias == "BUY" else loss_p * 0.4, 2),
            "sell": round(calibrated_win_p if tentative_bias == "SELL" else loss_p * 0.4, 2),
            "no_trade": round(hypotheses.no_trade_probability, 2)
        }

        tp_dist = abs(tp_price - entry_price)

        vol_state = getattr(context.volatility, "state", "NORMAL").upper()
        is_expansion_val = 1.0 if vol_state == "EXPANSION" else 0.0
        is_extreme_val = 1.0 if vol_state == "EXTREME" else 0.0
        runner_trail_distance_atr = round(1.0 + (0.4 * is_expansion_val) + (0.8 * is_extreme_val), 2)

        # Task 2a: Determine MARKET vs LIMIT order type
        cur_mkt_price = getattr(context, "current_price", entry_price)
        price_diff = abs(entry_price - cur_mkt_price)
        spec_info = resolve_symbol(context.symbol)
        tolerance_pip = spec_info.pip_size * 2.0 if hasattr(spec_info, "pip_size") else 0.0002

        order_type_decision = "MARKET"
        if price_diff > tolerance_pip:
            if best_strategy in ("LIQUIDITY_SWEEP_REVERSAL", "CHOCH_STRUCTURAL_REVERSAL", "TREND_PULLBACK", "RANGE_MEAN_REVERSION"):
                order_type_decision = "LIMIT"
            elif tentative_bias == "BUY" and entry_price < cur_mkt_price:
                order_type_decision = "LIMIT"
            elif tentative_bias == "SELL" and entry_price > cur_mkt_price:
                order_type_decision = "LIMIT"

        return DecisionObject(
            symbol=context.symbol,
            timestamp=datetime.now(timezone.utc),
            regime=regime,
            bias=tentative_bias,
            probabilities=probabilities,
            strategy=best_strategy,
            order_type=order_type_decision,
            entry_price=entry_price,
            stop_loss=sl_price,
            take_profit=tp_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            tp3_price=tp3_price,
            first_target_price=first_target_price,
            first_target_volume_pct=first_target_volume_pct,
            runner_trail_distance_atr=runner_trail_distance_atr,
            sl_distance=risk_dist,
            tp_distance=tp_dist,
            risk_reward_ratio=rr_ratio,
            calculated_risk_percent=round(risk_per_trade_pct * devil_report.invalidation_risk_coefficient, 2),
            expected_value=ev,
            model_confidence=calibrated_win_p,
            raw_win_prob=round(float(raw_prob), 4),
            ai_score=round(float(ai_score), 1),
            adversarial_penalty=devil_report.penalty_score,
            invalidation_levels=hypotheses.invalidation_criteria,
            bull_case=bull_case[:4],
            bear_case=bear_case[:4],
            risk_factors=devil_report.threats_detected[:4],
            quality_gate=quality_gate,
            waiting_reasons=waiting_reasons,
            rejection_reasons=rejection_reasons,
            decision=decision_action,
            honest_base_rate=(honest_base_rate.as_dict() if honest_base_rate is not None else None),
            execution_authorized=gate_passed,
            context=context,
            pattern_sample_size=pattern_memory.get("sample_size", 0) if "pattern_memory" in locals() and pattern_memory else 0,
            meta_label_prob=meta_label_prob,
            gate_policy_decision=gate_policy_decision,
            dissection_score=locals().get("_dissection_score", 0.0),
            dissection_tier=locals().get("_dissection_tier", "UNKNOWN"),
            master_confluence_score=locals().get("_master_score", 0.0),
            master_confluence_tier=locals().get("_master_tier", "UNKNOWN")
        )
