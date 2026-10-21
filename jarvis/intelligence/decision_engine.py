"""
HM Algo 2.0 — Autonomous Decision Engine & Trade Quality Gate.
Synthesizes multi-agent confluences, applies Devil's Advocate risk penalties, calculates expected value, and gates execution.
"""
from typing import Callable, Dict, List, Any, Optional
from datetime import datetime, timezone
import logging
import math
import time

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
from jarvis.data.symbol_registry import resolve as resolve_symbol, is_crypto as check_is_crypto
from jarvis.risk.account_tier import is_micro_account, get_effective_min_ev
from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config

from jarvis.intelligence.meta_labeler import MetaLabeler
from jarvis.intelligence.gate_policy import AdaptiveGatePolicy, HARD_GATES
from jarvis.intelligence.ai_dissector import AIDissector
from jarvis.intelligence.realtime_optimizer import RealtimeOptimizer
from jarvis.intelligence.master_confluence import MasterConfluenceEngine
from jarvis.market.fair_value_gap import FairValueGapEngine
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine
from jarvis.intelligence.regime_engine import validate_strategy_for_regime


def _is_forex(symbol: str) -> bool:
    try:
        spec = resolve_symbol(symbol)
        return getattr(spec, "asset_class", "").upper() == "FOREX"
    except Exception:
        return False


class LevelsResult(tuple):
    """An 8-tuple result containing (bias, entry, sl, tp, risk_dist, rr, first_target, volume_pct)
    preserving 100% backward compatibility with tuple unpacking, while exposing 3-tier milestone
    attributes tp1_price, tp2_price, tp3_price, and as_limit_price."""
    def __new__(cls, tentative_bias, entry_price, sl_price, tp_price, risk_dist, rr_ratio, first_target_price, first_target_volume_pct, tp1_price=None, tp2_price=None, tp3_price=None, as_limit_price=None):
        return super().__new__(cls, (tentative_bias, entry_price, sl_price, tp_price, risk_dist, rr_ratio, first_target_price, first_target_volume_pct))

    def __init__(self, tentative_bias, entry_price, sl_price, tp_price, risk_dist, rr_ratio, first_target_price, first_target_volume_pct, tp1_price=None, tp2_price=None, tp3_price=None, as_limit_price=None):
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
        self.as_limit_price = as_limit_price


def _bounded_bayesian_probability_update(
    current_p: float,
    boost: float,
    trade_style: str = "SWING",
    is_micro: bool = False
) -> float:
    """
    Applies evidence boosts in bounded logit space to prevent unrealistic probability inflation.
    Ceilings based on empirical base rates:
      - Scalp / Day / Micro: strictly <= 0.65 (empirical sustainable max ~58-62%)
      - Swing: strictly <= 0.72 (empirical sustainable max ~65%)
      - Floor: 0.35
    Preserves exact relative confluence ranking while maintaining honest expected values.
    """
    if boost <= 0.0:
        return current_p
    style_u = str(trade_style or "SWING").upper()
    if "SCALP" in style_u or is_micro:
        p_max = 0.65
        p_min = 0.35
    elif any(x in style_u for x in ("DAY", "INTRADAY")):
        p_max = 0.68
        p_min = 0.35
    else:
        p_max = 0.72
        p_min = 0.35

    clamped_base = max(p_min + 0.001, min(p_max - 0.001, current_p))
    u0 = (clamped_base - p_min) / (p_max - p_min)
    u0_clamped = max(1e-6, min(1.0 - 1e-6, u0))
    l0 = math.log(u0_clamped / (1.0 - u0_clamped))

    b_clamped = max(-0.40, min(0.40, boost))
    delta_l = math.log((0.50 + b_clamped) / (0.50 - b_clamped))

    l_post = l0 + delta_l
    u_post = 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, l_post))))
    return round(p_min + u_post * (p_max - p_min), 4)


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
        max_devil_penalty: float = 50.0,
        strategy_bandit: Optional[Any] = None,
        recent_win_rate_fn: Optional[Callable[[], Optional[float]]] = None,
    ):
        self.strategy_selector = strategy_selector or StrategySelector(bandit=strategy_bandit)
        self.hypothesis_engine = hypothesis_engine or HypothesisEngine()
        self.calibrator = calibrator or ConfidenceCalibrationEngine()
        self.ml_predictor = ml_predictor or OnlineMLPredictor()
        self.min_ev_hurdle = min_ev_hurdle
        self.max_devil_penalty = max_devil_penalty

        # Live-expectancy quarantine (2026-10-20 ledger audit). The
        # snapshot is taken ONCE here and refreshed on a TTL thereafter,
        # never per decision: the dividend-rate of the demo ledger is
        # minutes, not milliseconds, and a per-decision read of live disk
        # state makes two back-to-back identical simulations diverge the
        # moment the live engine writes a trade between them (the exact
        # flake tests/test_backtest_is_hermetic.py guards). A simulation
        # constructs its engine inside offline_mode(), so its snapshot is
        # frozen empty forever - the guard takes itself out of the sim.
        try:
            from jarvis.config.runtime import is_offline as _is_offline

            self._quarantine_offline = bool(_is_offline())
        except Exception:
            self._quarantine_offline = False
        self._quarantine_stats: dict = {}
        self._quarantine_loaded_at: float = 0.0
        if not self._quarantine_offline:
            try:
                from jarvis.execution.strategy_quarantine import (
                    load_live_expectancy as _load_expectancy,
                )

                self._quarantine_stats = dict(_load_expectancy())
                self._quarantine_loaded_at = time.monotonic()
            except Exception:
                self._quarantine_stats = {}
        self.order_flow = InstitutionalVolumeOrderFlowEngine()
        self.self_learning = self_learning or SelfLearningEngine()
        self.meta_labeler = meta_labeler or MetaLabeler()
        self.gate_policy = AdaptiveGatePolicy()
        self.ai_dissector = ai_dissector or AIDissector()
        self.realtime_optimizer = realtime_optimizer or RealtimeOptimizer()
        # SOFTEN evidence supplier. Defaults to the realtime optimizer's live
        # query, which is inert (returns None) in offline/backtest mode — a
        # simulation must rebind this to its own running window instead.
        self.recent_win_rate_fn = recent_win_rate_fn or self.realtime_optimizer.get_recent_win_rate
        self.master_confluence = master_confluence or MasterConfluenceEngine()
        self.fvg_engine = FairValueGapEngine()
        self.dynamic_levels_engine = dynamic_levels_engine or DynamicRiskAndLevelsEngine()
        from jarvis.scalping.scalp_execution_engine import ScalpExecutionEngine
        self.scalp_engine = ScalpExecutionEngine()
        # Symbols already warned about an unobserved price — this runs per symbol per
        # cycle, so an undeduplicated warning would flood the log during an outage.
        self._no_price_warned: set = set()

    _QUARANTINE_REFRESH_SEC: float = 300.0
    _QUARANTINE_MIN_TRADES: int = 20
    _QUARANTINE_MIN_EXPECTANCY_R: float = -0.25

    def _strategy_quarantined(self, symbol: str, strategy: str) -> bool:
        """Consult the strategy quarantine governor with graduation support.

        Offline/backtest: never reads live disk at all.
        Live: evaluates whether this (symbol, strategy) pair has demonstrated
        a persistently negative expectancy on recent closed trades without graduation.
        """
        if not strategy or strategy == "?":
            return False
        if self._quarantine_offline:
            return False
        try:
            from jarvis.execution.strategy_quarantine import is_quarantined
            return is_quarantined(
                str(symbol or "").upper().strip(),
                str(strategy).upper().strip(),
                min_trades=self._QUARANTINE_MIN_TRADES,
                min_expectancy_r=self._QUARANTINE_MIN_EXPECTANCY_R
            )
        except Exception:
            return False

    def _resolve_market_trend_and_bias(
        self,
        context: MarketContext,
        regime: RegimeOutput,
        analyst_reports: Dict[str, AnalystReport],
        trade_style: str = "SWING"
    ) -> str:
        """
        Hierarchical Market Direction Engine.
        Synthesizes Macro MTF, Market Regime, Momentum Dynamics, and Micro Structure
        into a definitive directional consensus (BUY, SELL, or HOLD).
        Guarantees that minor intraday pullbacks in a bull trend are NOT mistaken for SELL signals,
        and minor bounces in a bear trend are NOT mistaken for BUY signals.
        """
        st = context.structure
        mom = getattr(context, "momentum", None)
        trend_score = float(getattr(mom, "trend_score", 0.0) or 0.0) if mom else 0.0
        adx_val = float(getattr(mom, "adx", 20.0) or 20.0) if mom else 20.0
        
        # 1. Resolve Higher-Timeframe (HTF) Direction based on trade style
        mtf_align = getattr(context, "mtf_alignment", {}) or {}
        style_upper = (trade_style or getattr(context, "trade_style", "SWING") or "SWING").upper()
        d1_anchor_bias = mtf_align.get("D1", "NEUTRAL")
        sym_name = str(getattr(context, "symbol", "")).upper()
        is_gold = ("XAU" in sym_name) or ("GOLD" in sym_name)
        is_index_asset = any(k in sym_name for k in ["US500", "NAS100", "US30", "SPX", "NDX", "DJ30", "DJI", "DOW", "GER40", "UK100"])
        is_crypto = any(c in sym_name for c in ("BTC", "ETH", "SOL")) or (check_is_crypto(sym_name) if sym_name else False)

        if "SCALP" in style_upper:
            htf_bias = mtf_align.get("H1", mtf_align.get("M15", "NEUTRAL"))
            context_bias = mtf_align.get("M15", mtf_align.get("M5", "NEUTRAL"))
            primary_tf_bias = mtf_align.get("M5", getattr(st, "bias", "NEUTRAL"))
        elif any(x in style_upper for x in ("DAY", "INTRADAY")):
            htf_bias = mtf_align.get("H4", mtf_align.get("H1", "NEUTRAL"))
            context_bias = mtf_align.get("H1", mtf_align.get("M15", "NEUTRAL"))
            primary_tf_bias = mtf_align.get("M15", getattr(st, "bias", "NEUTRAL"))
        else:  # SWING
            htf_bias = mtf_align.get("D1", mtf_align.get("H4", "NEUTRAL"))
            context_bias = mtf_align.get("H4", mtf_align.get("H1", "NEUTRAL"))
            primary_tf_bias = mtf_align.get("H1", getattr(st, "bias", "NEUTRAL"))

        mtf_score = float(getattr(context, "mtf_confluence_score", 0.0) or 0.0)

        # 2. Market Regime Direction
        reg_obj = regime.primary_regime if hasattr(regime, "primary_regime") else None
        is_bull_regime = reg_obj in (MarketRegime.TREND_BULL, MarketRegime.STRONG_TREND_BULL)
        is_bear_regime = reg_obj in (MarketRegime.TREND_BEAR, MarketRegime.STRONG_TREND_BEAR)
        is_breakout_regime = reg_obj in (MarketRegime.BREAKOUT, MarketRegime.POST_BREAKOUT, MarketRegime.HIGH_VOLATILITY)
        is_range_regime = reg_obj in (MarketRegime.RANGE, MarketRegime.CONSOLIDATION, MarketRegime.COMPRESSION)

        # 3. Decoupled Analyst Votes (excluding NEUTRAL)
        bull_votes = sum(1 for r in analyst_reports.values() if r.bias == "BULLISH")
        bear_votes = sum(1 for r in analyst_reports.values() if r.bias == "BEARISH")

        # 4. Authenticated Reversal & Order Flow Conditions
        has_bull_sweep = bool(getattr(context.liquidity, "sweep_detected", False) and "BULLISH" in getattr(context.liquidity, "sweep_type", ""))
        has_bear_sweep = bool(getattr(context.liquidity, "sweep_detected", False) and "BEARISH" in getattr(context.liquidity, "sweep_type", ""))

        # Check ICT Sweep & Order-Flow Absorption Reversal via InstitutionalEntryEngine
        sweep_rev_buy = False
        sweep_rev_sell = False
        try:
            df_m5_sample = getattr(context, "df_m5", None)
            from jarvis.intelligence.institutional_entry_engine import INSTITUTIONAL_ENTRY_ENGINE
            rev_res = INSTITUTIONAL_ENTRY_ENGINE.detect_institutional_sweep_reversal(df_m5_sample, context)
            if rev_res.get("is_valid"):
                if rev_res.get("reversal_direction") == "BUY":
                    sweep_rev_buy = True
                elif rev_res.get("reversal_direction") == "SELL":
                    sweep_rev_sell = True
        except Exception:
            pass

        has_bull_sweep = has_bull_sweep or sweep_rev_buy
        has_bear_sweep = has_bear_sweep or sweep_rev_sell
        has_bull_div = bool(getattr(mom, "bullish_divergence", False)) if mom else False
        has_bear_div = bool(getattr(mom, "bearish_divergence", False)) if mom else False
        dp_zone = getattr(st, "discount_premium_zone", "EQUILIBRIUM")

        of_data = getattr(context, "order_flow", {}) or {}
        delta_score = float(of_data.get("delta_score", 0.0)) if isinstance(of_data, dict) else getattr(of_data, "delta_score", 0.0)

        authenticated_bull_reversal = (
            (has_bull_sweep or st.choch_type == "BULLISH" or (st.bias == "BULLISH" and has_bull_div))
            and (has_bull_div or dp_zone in ("DISCOUNT", "EQUILIBRIUM"))
            and trend_score >= -80.0
        )
        authenticated_bear_reversal = (
            (has_bear_sweep or st.choch_type == "BEARISH" or (st.bias == "BEARISH" and has_bear_div))
            and (has_bear_div or dp_zone in ("PREMIUM", "EQUILIBRIUM"))
            and trend_score <= 80.0
        )

        # 5. Hierarchical Resolution
        # CASE 0: Volatility Breakout & Expansion (Momentum takes precedence over HTF lag)
        if is_breakout_regime:
            if trend_score >= 10.0 or st.bias == "BULLISH" or primary_tf_bias == "BULLISH" or bull_votes > bear_votes:
                return "BUY"
            elif trend_score <= -10.0 or st.bias == "BEARISH" or primary_tf_bias == "BEARISH" or bear_votes > bull_votes:
                return "SELL"
            elif st.bos:
                return "BUY" if trend_score >= 0.0 else "SELL"
            return "BUY" if bull_votes >= bear_votes else "SELL"

        # CASE A: Strong Bullish Macro / Structure / Regime / D1 Anchor
        if (
            (htf_bias == "BULLISH" and d1_anchor_bias != "BEARISH")
            or (d1_anchor_bias == "BULLISH" and htf_bias != "BEARISH")
            or (is_bull_regime and not (htf_bias == "BEARISH" and d1_anchor_bias == "BEARISH"))
            or mtf_score >= 30.0
        ):
            is_intraday = trade_style in ("SCALP", "DAY_TRADING")

            # 0. Intermarket Crypto Lead-Lag Guard: Never buy ETH when ETH/BTC ratio is breaking down
            eth_btc_trend = getattr(context, "crypto_lead_lag", {}).get("eth_btc_trend", "NEUTRAL") if hasattr(context, "crypto_lead_lag") else "NEUTRAL"
            if is_crypto and "ETH" in sym_name and eth_btc_trend == "BEARISH":
                if not authenticated_bull_reversal:
                    if trend_score <= -15.0 or st.bias == "BEARISH" or primary_tf_bias == "BEARISH" or st.bos_type == "BEARISH":
                        return "SELL"
                    return "HOLD"

            # 1. Structural / Momentum Guard: If primary timeframe is breaking down with negative momentum, do NOT buy blindly!
            if (st.bias == "BEARISH" or st.bos_type == "BEARISH" or primary_tf_bias == "BEARISH") and (trend_score <= -10.0 or delta_score <= -20.0):
                if (
                    authenticated_bear_reversal
                    or (trend_score <= -15.0 and (context_bias == "BEARISH" or st.bias == "BEARISH"))
                    or (st.bos and trend_score <= 0.0)
                    or (bear_votes >= 2 and bear_votes > bull_votes)
                ):
                    return "SELL"
                return "HOLD"

            # 1b. Momentum Exhaustion Top & Liquidity Sweep Guard:
            # If buy-side liquidity was swept at a premium, or momentum is at an overbought extreme,
            # buying is prohibited! Wait for pullback or flip to SELL if confirmed.
            # In expansion/breakout regimes or on index expansion, allow momentum up to 98.0 without false exhaustion.
            max_mom_bull = 98.0 if (is_breakout_regime or is_bull_regime or is_index_asset) else 80.0
            if ((has_bear_sweep and dp_zone == "PREMIUM") or trend_score >= max_mom_bull) and not is_breakout_regime:
                if authenticated_bear_reversal or (has_bear_sweep and (trend_score <= 30.0 or bear_votes > bull_votes)):
                    return "SELL"
                return "HOLD"

            # 2. Authenticated reversal: allow SELL when lower TFs have flipped bearish
            if authenticated_bear_reversal and (
                mtf_score <= -15.0
                or (trend_score <= -15.0 and context_bias == "BEARISH")
                or primary_tf_bias == "BEARISH"
            ):
                return "SELL"

            # 3. Strong analyst consensus override (>=3 bearish votes with negative momentum)
            if bear_votes >= 3 and trend_score <= -10.0:
                return "SELL"

            # 4. In a macro bull trend, look for BUY continuation or pullback completion
            if st.bos and trend_score >= 10.0:
                return "BUY"
            if st.bias == "BULLISH" or trend_score >= 10.0 or bull_votes > bear_votes:
                return "BUY"
            if dp_zone in ("DISCOUNT", "EQUILIBRIUM") and (has_bull_sweep or bull_votes >= 1 or is_bull_regime):
                return "BUY"
            return "BUY" if ((htf_bias == "BULLISH" or d1_anchor_bias == "BULLISH") and (bull_votes >= 1 or not is_intraday) and trend_score >= 0.0) else "HOLD"

        # CASE B: Strong Bearish Macro / Structure / Regime
        elif (
            (htf_bias == "BEARISH" and d1_anchor_bias != "BULLISH")
            or (d1_anchor_bias == "BEARISH" and htf_bias != "BULLISH")
            or (is_bear_regime and not (htf_bias == "BULLISH" and d1_anchor_bias == "BULLISH"))
            or mtf_score <= -30.0
        ):
            is_intraday = trade_style in ("SCALP", "DAY_TRADING")

            # 1. Structural / Momentum Guard for ALL STYLES (SCALP, DAY_TRADING, SWING):
            # If the primary timeframe or HTF has bullish structure or active upward displacement,
            # STRICTLY PROHIBIT selling! Either flip to BUY if confirmed, or HOLD.
            has_bullish_structure = (
                st.bias == "BULLISH"
                or st.bos_type == "BULLISH"
                or primary_tf_bias == "BULLISH"
                or htf_bias == "BULLISH"
                or d1_anchor_bias == "BULLISH"
            )
            has_bullish_expansion = (
                trend_score >= 10.0
                or delta_score >= 15.0
                or (mom is not None and getattr(mom, "short_term_atr_disp", 0.0) >= 0.35)
            )

            if has_bullish_structure or has_bullish_expansion:
                if (
                    authenticated_bull_reversal
                    or (trend_score >= 10.0 and (context_bias == "BULLISH" or st.bias == "BULLISH" or primary_tf_bias == "BULLISH"))
                    or (st.bos and trend_score >= 0.0)
                    or (bull_votes >= 2 and bull_votes > bear_votes)
                ):
                    return "BUY"
                # If market is rallying upward or structure is bullish, NEVER SELL blindly!
                return "HOLD"

            # 1b. Momentum Exhaustion Bottom & Liquidity Sweep Guard:
            # If sell-side liquidity was swept at a discount, or momentum is at an oversold extreme,
            # selling is prohibited! Wait for pullback or flip to BUY if confirmed.
            # In expansion/breakout regimes or on index expansion, allow momentum down to -98.0 without false exhaustion.
            min_mom_bear = -98.0 if (is_breakout_regime or is_bear_regime or is_index_asset) else -80.0
            if ((has_bull_sweep and dp_zone == "DISCOUNT") or trend_score <= min_mom_bear) and not is_breakout_regime:
                if authenticated_bull_reversal or (has_bull_sweep and (trend_score >= -30.0 or bull_votes > bear_votes)):
                    return "BUY"
                return "HOLD"

            # 2. Authenticated Reversal
            if authenticated_bull_reversal and (
                mtf_score >= 15.0
                or (trend_score >= 15.0 and (context_bias == "BULLISH" or primary_tf_bias == "BULLISH"))
                or (is_intraday and primary_tf_bias == "BULLISH")
            ):
                return "BUY"

            # 3. Swing Trading Alignment:
            # If trade_style is SWING and both HTF (D1) and Context (H4) are BEARISH,
            # SWING trades the HTF downtrend only if primary structure is also aligned.
            if not is_intraday and htf_bias == "BEARISH":
                if not (st.bias == "BULLISH" or primary_tf_bias == "BULLISH" or trend_score > 0):
                    if trend_score <= 5.0 or dp_zone in ("PREMIUM", "EQUILIBRIUM") or is_bear_regime:
                        return "SELL"

            # 4. Sell Continuation / Retracement short:
            # Strictly prohibit selling if D1 or HTF or primary structure is BULLISH
            if htf_bias == "BULLISH" or d1_anchor_bias == "BULLISH" or st.bias == "BULLISH" or primary_tf_bias == "BULLISH":
                return "HOLD"

            if trend_score <= -15.0 and st.bias == "BEARISH" and primary_tf_bias == "BEARISH":
                if st.bos or bear_votes > bull_votes or is_bear_regime:
                    return "SELL"
            return "HOLD"

        # CASE C: Ranging / Compression / Consolidation
        elif is_range_regime or abs(trend_score) < 20.0:
            if dp_zone in ("DISCOUNT", "EQUILIBRIUM") and (has_bull_sweep or (st.bias == "BULLISH" and (getattr(st, "choch", False) or trend_score >= 0)) or bull_votes > bear_votes):
                return "BUY"
            elif dp_zone in ("PREMIUM", "EQUILIBRIUM") and (has_bear_sweep or (st.bias == "BEARISH" and (getattr(st, "choch", False) or trend_score <= 0)) or bear_votes > bull_votes):
                return "SELL"
            elif st.bos and abs(trend_score) >= 20.0:
                return "BUY" if trend_score > 0 else "SELL"
            else:
                return "HOLD"

        # CASE D: Transitional / Neutral Market
        else:
            if bull_votes >= 2 and bull_votes > bear_votes and trend_score > 0:
                return "BUY"
            elif bear_votes >= 2 and bear_votes > bull_votes and trend_score < 0:
                return "SELL"
            elif st.bos and trend_score >= 25.0:
                return "BUY"
            elif st.bos and trend_score <= -25.0:
                return "SELL"
            elif st.bias in ("BULLISH", "BEARISH"):
                return "BUY" if st.bias == "BULLISH" else "SELL"
            else:
                return "HOLD"

    def _compute_bias_and_levels(
        self,
        context: MarketContext,
        regime: RegimeOutput,
        analyst_reports: Dict[str, AnalystReport],
        trade_style: str = "SWING",
        account_balance: float = 10000.0,
        risk_per_trade_pct: float = 0.5,
        mtf_data: Optional[Dict[str, Any]] = None,
    ):
        st = context.structure
        c_price = context.current_price

        # ── No observed price ⇒ no direction, no levels ────────────────────────
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

        style = trade_style or getattr(context, "trade_style", "SWING") or "SWING"
        tentative_bias = self._resolve_market_trend_and_bias(
            context=context,
            regime=regime,
            analyst_reports=analyst_reports,
            trade_style=style
        )

        # Log the resolved direction so it's visible in daemon output.
        _reg_label = getattr(regime, "primary_regime", "UNKNOWN")
        _mtf_align = getattr(context, "mtf_alignment", {}) or {}
        _ts = float(getattr(getattr(context, "momentum", None), "trend_score", 0.0) or 0.0) if getattr(context, "momentum", None) else 0.0
        logger.info(
            "[%s] Direction resolved: %s (%s) | regime=%s | trend_score=%.1f | mtf=%s",
            context.symbol, tentative_bias, style, _reg_label, _ts,
            {k: v for k, v in _mtf_align.items() if k in ("M5", "M15", "H1", "H4", "D1")},
        )

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
            trade_style=style,
            mtf_data=mtf_data
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
            tp3_price=levels.get("tp3_price"),
            as_limit_price=levels.get("as_limit_price")
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
        is_index_asset = getattr(spec, "asset_class", "") == "INDEX" or any(k in sym_name for k in ["US500", "NAS100", "US30", "SPX", "NDX", "DJ30", "DJI", "DOW", "GER40", "UK100"])
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
        if is_crypto:
            min_rr = 1.6 if "SCALP" in t_style_check else (1.8 if is_micro_mode else 2.0)
            min_sl_atr_mult = 0.25 if "SCALP" in t_style_check else (0.40 if is_micro_mode else 0.50)
            max_spread = spec.max_spread_pips * 0.95
        elif is_gold:
            min_rr = 1.4 if "SCALP" in t_style_check else (1.6 if is_micro_mode else 1.8)
            min_sl_atr_mult = 0.25 if "SCALP" in t_style_check else (0.40 if is_micro_mode else 0.45)
            max_spread = spec.max_spread_pips
        elif any(k in sym_name for k in ["US500", "NAS100", "US30", "SPX", "NDX"]):
            min_rr = 1.4 if "SCALP" in t_style_check else 1.8
            min_sl_atr_mult = 0.25 if "SCALP" in t_style_check else 0.35
            max_spread = spec.max_spread_pips * 0.90
        elif is_jpy:
            min_rr = 1.4 if "SCALP" in t_style_check else 1.8
            min_sl_atr_mult = 0.25 if "SCALP" in t_style_check else 0.45
            max_spread = spec.max_spread_pips
        elif "SCALP" in t_style_check:
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
            max_spread = min(3.5, spec.max_spread_pips * 0.8) if _is_forex(context.symbol) else spec.max_spread_pips * 0.85
            if current_drawdown_pct > 5.0:
                min_score = max(min_score, 74.0)
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

        # 4. Adaptive Macro MTF Confluence Guard (Zero Unvalidated Counter-Trend Trades)
        mtf_align = getattr(context, "mtf_alignment", {}) if isinstance(getattr(context, "mtf_alignment", {}), dict) else {}
        mom_ts = float(getattr(context.momentum, "trend_score", 0.0)) if hasattr(context, "momentum") else 0.0
        t_style_upper = (getattr(context, "trade_style", None) or getattr(context, "style", "SWING") or "SWING").upper()
        
        # Resolve governing macro timeframe per style:
        if "SCALP" in t_style_upper:
            macro_bias = mtf_align.get("H1", mtf_align.get("M15", "NEUTRAL"))
            context_bias = mtf_align.get("M15", "NEUTRAL")
        elif any(x in t_style_upper for x in ("DAY", "INTRADAY")):
            macro_bias = mtf_align.get("H4", mtf_align.get("H1", "NEUTRAL"))
            context_bias = mtf_align.get("H1", "NEUTRAL")
        else: # SWING
            macro_bias = mtf_align.get("D1", mtf_align.get("H4", "NEUTRAL"))
            context_bias = mtf_align.get("H4", "NEUTRAL")

        # Authenticated Institutional Reversal Setup
        is_validated_reversal = (
            strategy in ("LIQUIDITY_SWEEP_REVERSAL", "CHOCH_STRUCTURAL_REVERSAL")
            and bool(getattr(context.liquidity, "sweep_detected", False))
            and bool(getattr(context.structure, "choch", False))
            and (
                (tentative_bias == "BUY" and (getattr(context.momentum, "bullish_divergence", False) or getattr(context.structure, "discount_premium_zone", "") == "DISCOUNT") and mom_ts >= -10.0) or
                (tentative_bias == "SELL" and (getattr(context.momentum, "bearish_divergence", False) or getattr(context.structure, "discount_premium_zone", "") == "PREMIUM") and mom_ts <= 10.0)
            )
            and ai_score >= 74.0
            and rr_ratio >= 1.8
        )

        d1_anchor_bias = mtf_align.get("D1", "NEUTRAL")
        mtf_counter_trend = False
        if tentative_bias == "BUY":
            d1_veto = (d1_anchor_bias == "BEARISH" and ("SWING" in t_style_check or mom_ts <= 0.0))
            if macro_bias == "BEARISH" or (context_bias == "BEARISH" and mom_ts <= -15.0) or d1_veto or mom_ts <= -15.0:
                if not is_validated_reversal:
                    mtf_counter_trend = True
        elif tentative_bias == "SELL":
            d1_veto = (d1_anchor_bias == "BULLISH" and ("SWING" in t_style_check or mom_ts >= 0.0))
            # On strong intraday momentum breakdown (Bearish BOS on execution timeframe, or trend_score <= -30),
            # allow DAY_TRADING / SCALP sell even if lagging H4 hasn't flipped yet
            has_intraday_breakdown = (
                ("DAY" in t_style_check or "SCALP" in t_style_check)
                and (getattr(context.structure, "bos_type", "") == "BEARISH" or getattr(context.structure, "choch_type", "") == "BEARISH" or mom_ts <= -30.0)
                and context_bias in ("BEARISH", "NEUTRAL")
            )
            if (macro_bias == "BULLISH" and not has_intraday_breakdown) or (context_bias == "BULLISH" and mom_ts >= 15.0) or d1_veto or mom_ts >= 15.0:
                if not is_validated_reversal:
                    mtf_counter_trend = True

        # Regime Trend Consistency Guard: Never open counter-trend trades unless validated reversal
        # or intraday structural momentum/breakout alignment for SCALP/DAY_TRADING
        is_intraday_style = any(x in t_style_check for x in ("SCALP", "DAY", "INTRADAY"))
        has_intraday_struct_bull = (
            getattr(context.structure, "bos_type", "") == "BULLISH"
            or getattr(context.structure, "choch_type", "") == "BULLISH"
            or mom_ts >= 18.0
        )
        has_intraday_struct_bear = (
            getattr(context.structure, "bos_type", "") == "BEARISH"
            or getattr(context.structure, "choch_type", "") == "BEARISH"
            or mom_ts <= -18.0
        )
        regime_trend_consistent = True
        if regime.primary_regime in (MarketRegime.TREND_BULL, MarketRegime.STRONG_TREND_BULL):
            if tentative_bias == "SELL":
                if not (is_validated_reversal or (is_intraday_style and has_intraday_struct_bear)):
                    regime_trend_consistent = False
        elif regime.primary_regime in (MarketRegime.TREND_BEAR, MarketRegime.STRONG_TREND_BEAR):
            if tentative_bias == "BUY":
                if not (is_validated_reversal or (is_intraday_style and has_intraday_struct_bull)):
                    regime_trend_consistent = False

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
            cfg = get_symbol_profile_config(sym_name)
            c_ts = getattr(context, "timestamp", None)
            now_dt = c_ts if isinstance(c_ts, datetime) else datetime.now(timezone.utc)
            if cfg and cfg.session_restriction and cfg.allowed_utc_hours:
                start_h, end_h = cfg.allowed_utc_hours
                is_prime_session_valid = now_dt.weekday() < 5 and (start_h <= now_dt.hour <= end_h)
            else:
                is_prime_session_valid = SessionEngine.is_index_prime_session(c_ts)
        elif is_crypto:
            # Enforce institutional advanced-trading skill §7/§11:
            # Broker spread must never consume >= 20% of stop distance or ATR.
            # On weekend / off-hours compression, block scalps where spread >= 0.25 * atr.
            c_ts = getattr(context, "timestamp", None)
            now_dt = c_ts if isinstance(c_ts, datetime) else datetime.now(timezone.utc)
            is_weekend = now_dt.weekday() >= 5
            atr_val = getattr(context.volatility, "atr", 0.0)
            spread_dist = spread * spec.pip_size if spec.pip_size > 0 else 0.0
            
            if is_weekend and "SCALP" in t_style_check and atr_val > 0 and spread_dist > (0.65 * atr_val):
                is_prime_session_valid = False
            else:
                is_prime_session_valid = (spread <= spec.max_spread_pips) or (ai_score >= 70.0)
        elif is_gold:
            cfg = get_symbol_profile_config(sym_name)
            c_ts = getattr(context, "timestamp", None)
            now_dt = c_ts if isinstance(c_ts, datetime) else datetime.now(timezone.utc)
            if cfg and cfg.session_restriction and cfg.allowed_utc_hours:
                start_h, end_h = cfg.allowed_utc_hours
                # Gold session gate (profile-driven, hard). Research + ledger
                # measured on our own executed_trades: 15-17h UTC was the
                # largest gold bleed cluster (129 live rows, -$522.73),
                # 06-08h the second (-$252.44), while 12-14h was the only
                # consistently profitable segment. NY data-driven hours carry
                # the widest moves (13:00 UTC peak ~$24/h vs ~$2.80 at the
                # 21:00 UTC rollover), and Asia flow is chiefly uninformed
                # (Tokyo-dominance studies for the metals). The window is
                # enforced by a dedicated HARD gate below - the generic soft
                # "Forex Prime Session" slot is how 00-06h trades still fired
                # under the old gate forgiveness.
                #
                # 2026-10-20 premium exception: when the profile opts into
                # premium_window_exception, an off-window hour is still allowed
                # through IF the blended analyst score is elite (>= the
                # profile's premium floor, 88.0 for gold - above every normal
                # hard gate band of 70-85). The weekday<5 term is kept outside
                # the exception: a weekend is never premium, the book is shut.
                _in_window = (start_h <= now_dt.hour < end_h)
                if not _in_window and getattr(cfg, "premium_window_exception", False):
                    _premium_floor = float(getattr(cfg, "premium_out_of_window_min_ai_score", 0.0) or 0.0)
                    if _premium_floor > 0.0 and ai_score >= _premium_floor:
                        if not hasattr(self, "_premium_gold_warned_at") or (time.time() - self._premium_gold_warned_at) > 900:
                            logger.info(
                                "GOLD premium out-of-window entry admitted: ai_score %.1f >= floor %.1f "
                                "(window %02d:%02d UTC); off-session chaff remains filtered to elite scores.",
                                ai_score, _premium_floor, start_h, end_h,
                            )
                            self._premium_gold_warned_at = time.time()
                        is_prime_session_valid = True
                        _in_window = True
                is_prime_session_valid = (now_dt.weekday() < 5) and _in_window
            else:
                is_prime_session_valid = (now_dt.weekday() < 5) and (7 <= now_dt.hour < 19)
        elif is_oil_asset or ("XAG" in sym_name):
            cfg = get_symbol_profile_config(sym_name)
            c_ts = getattr(context, "timestamp", None)
            now_dt = c_ts if isinstance(c_ts, datetime) else datetime.now(timezone.utc)
            if cfg and cfg.session_restriction and cfg.allowed_utc_hours:
                start_h, end_h = cfg.allowed_utc_hours
                is_prime_session_valid = (now_dt.weekday() < 5) and (start_h <= now_dt.hour < end_h)
            else:
                is_prime_session_valid = (now_dt.weekday() < 5) and (8 <= now_dt.hour < 19)
        elif is_jpy:
            is_prime_session_valid = kz_active or (context.session.is_prime_session if hasattr(context, "session") and context.session else False) or (spread <= spec.typical_spread_pips * 1.5) or is_micro_mode or "SCALP" in t_style_check
        else:
            cfg = get_symbol_profile_config(sym_name)
            c_ts = getattr(context, "timestamp", None)
            now_dt = c_ts if isinstance(c_ts, datetime) else datetime.now(timezone.utc)
            if cfg and cfg.session_restriction and cfg.allowed_utc_hours:
                start_h, end_h = cfg.allowed_utc_hours
                is_prime_session_valid = (now_dt.weekday() < 5) and (start_h <= now_dt.hour < end_h)
            else:
                is_prime_session_valid = (
                    kz_active
                    or (context.session.is_prime_session if hasattr(context, "session") and context.session else False)
                    or is_micro_mode
                    or "SCALP" in t_style_check
                    or (spread <= spec.typical_spread_pips * 1.5 and ai_score >= 65.0)
                )

        # 6. Gold (XAUUSD) Directional Trend & Momentum Immunity Guard
        gold_trend_following_valid = True
        effective_strat = strategy or getattr(context, "strategy", "")
        if is_gold:
            st_zone = getattr(context.structure, "discount_premium_zone", "EQUILIBRIUM") if hasattr(context, "structure") else "EQUILIBRIUM"
            sweep_confirmed = bool(getattr(context.liquidity, "sweep_detected", False)) if hasattr(context, "liquidity") else False
            bos_active = bool(getattr(context.structure, "bos", False)) if hasattr(context, "structure") else False
            ts = getattr(context.momentum, "trend_score", 0.0) if hasattr(context, "momentum") else 0.0
            adx_val = getattr(context.momentum, "adx", 0.0) if hasattr(context, "momentum") else 0.0
            strong_expansion = (adx_val >= 20.0 and abs(ts) >= 20.0)

            # Never allow counter-trend entries on Gold when opposing momentum or HTF trend is active,
            # but allow pullbacks and liquidity sweep rebounds when structural confluence (sweep/BOS) is present
            if tentative_bias == "SELL":
                if (macro_bias == "BULLISH" or d1_anchor_bias == "BULLISH") and not is_validated_reversal:
                    gold_trend_following_valid = False
                elif ts >= 10.0:
                    gold_trend_following_valid = False
            elif tentative_bias == "BUY":
                if (macro_bias == "BEARISH" or d1_anchor_bias == "BEARISH") and not is_validated_reversal and ts <= -15.0:
                    gold_trend_following_valid = False

            if effective_strat in ("TREND_FOLLOWING", "TREND_PULLBACK", "BREAKDOWN", "MOMENTUM_CONTINUATION", "STRUCTURE"):
                if tentative_bias == "BUY":
                    if not (sweep_confirmed or st_zone in ("DISCOUNT", "EQUILIBRIUM") or bos_active):
                        gold_trend_following_valid = False
                elif tentative_bias == "SELL":
                    has_gold_sell_confluence = (sweep_confirmed or st_zone in ("PREMIUM", "EQUILIBRIUM") or bos_active)
                    if (macro_bias == "BULLISH" or d1_anchor_bias == "BULLISH") and not (sweep_confirmed or (bos_active and ts <= -10.0) or is_validated_reversal):
                        gold_trend_following_valid = False
                    elif not has_gold_sell_confluence:
                        gold_trend_following_valid = False

        # 7. Crypto Macro Trend Filter & Liquidity Sweep Entry Guard
        crypto_macro_trend_valid = True
        if is_crypto:
            ts_val = float(getattr(context.momentum, "trend_score", 0.0)) if hasattr(context, "momentum") else 0.0
            adx_val = float(getattr(context.momentum, "adx", 20.0)) if hasattr(context, "momentum") else 20.0
            min_crypto_adx = 24.0
            st_zone = getattr(context.structure, "discount_premium_zone", "EQUILIBRIUM")
            has_sweep = bool(getattr(context.liquidity, "sweep_detected", False))
            has_choch = bool(getattr(context.structure, "choch", False))
            has_bos = bool(getattr(context.structure, "bos", False))

            if adx_val < min_crypto_adx and not has_choch and not has_sweep:
                crypto_macro_trend_valid = False
            elif confluence_count < 2 or ai_score < 75.0:
                crypto_macro_trend_valid = False
            # Premature Entry Guard (SMC/ICT §4.1): In ranges or scalps, allow entry on sweep, discount/equilibrium, choch, breakout expansion, or candlestick pattern
            elif "SCALP" in t_style_check or "DAY" in t_style_check:
                vol_state = getattr(context.volatility, "state", "NORMAL") if hasattr(context, "volatility") else "NORMAL"
                is_breakout_expansion = (has_bos and (adx_val >= 20.0 or vol_state in ("EXPANSION", "EXTREME") or abs(ts_val) >= 25.0))
                pa_info = getattr(context, "candlestick_pattern", {}) or {}
                has_pa = bool(pa_info.get("pattern_detected", False) and pa_info.get("bias") == tentative_bias)

                # Intermarket Crypto Lead-Lag Guard (ETH/BTC relative strength):
                eth_btc_trend = getattr(context, "crypto_lead_lag", {}).get("eth_btc_trend", "NEUTRAL") if hasattr(context, "crypto_lead_lag") else "NEUTRAL"
                if "ETH" in sym_name and tentative_bias == "BUY" and eth_btc_trend == "BEARISH":
                    # Strictly prohibit buying ETH when ETH/BTC is falling unless extreme reversal at deep discount
                    if not (has_choch and has_sweep and st_zone == "DISCOUNT" and ai_score >= 85.0):
                        crypto_macro_trend_valid = False

                if tentative_bias == "BUY" and not (has_sweep or st_zone in ("DISCOUNT", "EQUILIBRIUM") or has_choch or is_breakout_expansion or has_pa or ts_val >= 30.0):
                    crypto_macro_trend_valid = False
                elif tentative_bias == "SELL" and not (has_sweep or st_zone in ("PREMIUM", "EQUILIBRIUM") or has_choch or is_breakout_expansion or has_pa or ts_val <= -30.0):
                    crypto_macro_trend_valid = False
            elif tentative_bias == "BUY":
                eth_btc_trend = getattr(context, "crypto_lead_lag", {}).get("eth_btc_trend", "NEUTRAL") if hasattr(context, "crypto_lead_lag") else "NEUTRAL"
                if "ETH" in sym_name and eth_btc_trend == "BEARISH":
                    if not (has_choch and has_sweep and st_zone == "DISCOUNT" and ai_score >= 85.0):
                        crypto_macro_trend_valid = False
                elif regime.primary_regime in (MarketRegime.STRONG_TREND_BEAR, MarketRegime.TREND_BEAR) or ts_val <= -15.0:
                    has_reversal = bool(has_choch and has_sweep)
                    if not (has_reversal and ai_score >= 82.0 and calibrated_win_p >= 0.65):
                        crypto_macro_trend_valid = False
            elif tentative_bias == "SELL":
                if regime.primary_regime in (MarketRegime.STRONG_TREND_BULL, MarketRegime.TREND_BULL) or ts_val >= 15.0:
                    has_reversal = bool(has_choch and has_sweep)
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
                if not ((d1_b == "BEARISH" or h4_b == "BEARISH") and has_rev and ts_v <= -25.0 and ai_score >= 80.0):
                    index_counter_trend_valid = False
            elif tentative_bias == "BUY":
                if (ts_v <= -25.0 and not bool(getattr(context.structure, "choch", False))) or rsi_v >= 78.0:
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

        # 14. Institutional Order Flow Alignment Guard
        order_flow_aligned = True
        if of_res:
            if of_res.get("institutional_activity", False) and of_res.get("signal") not in ("NEUTRAL", tentative_bias):
                order_flow_aligned = False
            trap = of_res.get("absorption_trap")
            if trap == "SELLER_ABSORPTION_TRAP" and tentative_bias == "BUY":
                order_flow_aligned = False
            elif trap == "BUYER_ABSORPTION_TRAP" and tentative_bias == "SELL":
                order_flow_aligned = False

        # 15. Strategy Viability Guard
        strategy_viable = True
        if strategy:
            cfg = get_symbol_profile_config(sym_name)
            if strategy in cfg.banned_strategies:
                strategy_viable = False
            else:
                # Regime-Strategy Compatibility validation
                try:
                    reg_str = regime.primary_regime.value if (regime and hasattr(regime, "primary_regime") and hasattr(regime.primary_regime, "value")) else str(getattr(regime, "primary_regime", ""))
                    adx_val = float(getattr(context.momentum, "adx", 0.0) or 0.0) if hasattr(context, "momentum") else 0.0
                    compat_ok, compat_reason = validate_strategy_for_regime(strategy, reg_str, adx=adx_val)
                    if not compat_ok:
                        strategy_viable = False
                        logger.debug("Strategy Viability: %s x %s incompatible with regime %s: %s", sym_name, strategy, reg_str, compat_reason)
                except Exception:
                    pass

                # Live-expectancy quarantine (2026-10-20 ledger audit): static
                # ban lists cannot see an edge dying. A (symbol, strategy)
                # pair that has burned >= 20 closed demo trades at <= -0.25R
                # per trade is disabled regardless of what the setup looks
                # like today. Consults the constructor's snapshot (refreshed
                # on a TTL in live, frozen empty in offline/backtest); fails
                # open on any problem.
                try:
                    if self._strategy_quarantined(sym_name, strategy):
                        strategy_viable = False
                        logger.debug(
                            "Strategy Viability: %s x %s quarantined by live ledger expectancy",
                            sym_name, strategy,
                        )
                except Exception:
                    pass

        # 16. Commodity (WTI & XAGUSD) Macro Trend Alignment Guard
        commodity_macro_valid = True
        if is_oil_asset or ("XAG" in sym_name):
            d1_b = mtf_align.get("D1", "NEUTRAL") if isinstance(mtf_align, dict) else "NEUTRAL"
            ts_v = getattr(context.momentum, "trend_score", 0.0) if hasattr(context, "momentum") else 0.0
            sweep_c = bool(getattr(context.liquidity, "sweep_detected", False)) if hasattr(context, "liquidity") else False
            choch_c = bool(getattr(context.structure, "choch", False)) if hasattr(context, "structure") else False
            is_valid_reversal = sweep_c or choch_c

            if tentative_bias == "SELL":
                if (d1_b == "BULLISH" or macro_bias == "BULLISH" or ts_v >= 20.0) and not is_valid_reversal:
                    commodity_macro_valid = False
            elif tentative_bias == "BUY":
                if (d1_b == "BEARISH" or macro_bias == "BEARISH" or ts_v <= -20.0) and not is_valid_reversal:
                    commodity_macro_valid = False

        # Institutional Quality Gate Matrix
        regime_viable = regime.primary_regime != MarketRegime.EVENT_RISK
        if regime.primary_regime == MarketRegime.WEAK_TREND:
            if not (ai_score >= 80.0 and calibrated_win_p >= 0.60):
                regime_viable = False

        gate_checks = {
            "Market Session Open": is_mkt_open,
            "Drawdown Safety Guard": current_drawdown_pct <= 10.0,
            "Regime Viability": regime_viable,
            "Regime Trend Consistency": regime_trend_consistent,
            "Directional Bias": tentative_bias in ["BUY", "SELL"],
            "Strategy Viable": strategy_viable,
            "Risk/Reward >= 1.5": rr_ratio >= min_rr,
            "Positive Expected Value": ev > 0 and ev >= effective_min_ev,
            "Spread Protection": spread <= max_spread and not context.volatility.is_excessive_spread,
            "Volatility Spike Circuit Breaker": not getattr(context.volatility, "volatility_spike_breaker", False),
            "AI Multi-Score Gate": ai_score >= min_score,
            "Devil Adversarial Guard": devil_report.penalty_score <= (self.max_devil_penalty + (5.0 if ai_score >= 80.0 else 0.0)) and len(devil_report.threats_detected) < 3,
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
            "Gold Session Window": (is_prime_session_valid if is_gold else True),
            "Gold Trend Following Alignment": gold_trend_following_valid,
            "Commodity Macro Alignment": commodity_macro_valid,
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
            mtf_data=mtf_data,
        )
        (
            tentative_bias, entry_price, sl_price, tp_price,
            risk_dist, rr_ratio, first_target_price, first_target_volume_pct
        ) = levels_res
        tp1_price = getattr(levels_res, "tp1_price", first_target_price)
        tp2_price = getattr(levels_res, "tp2_price", tp_price)
        tp3_price = getattr(levels_res, "tp3_price", None)
        as_limit_price = getattr(levels_res, "as_limit_price", None)

        # §B-5: Devil's Advocate Threat Feedback Adjustment
        threat_lvl = getattr(devil_report, "threat_price_level", None) if devil_report else None
        if threat_lvl is not None and isinstance(threat_lvl, (int, float)) and threat_lvl > 0:
            spec = resolve_symbol(context.symbol)
            atr_val = context.volatility.atr if context.volatility.atr > 0 else (entry_price * 0.005)
            min_target_rr_tuck = 1.40
            if tentative_bias == "BUY" and entry_price < threat_lvl < tp_price:
                adjusted_tp = round(threat_lvl - (atr_val * 0.1), spec.digits)
                tuck_r = (adjusted_tp - entry_price) / (risk_dist + 1e-9)
                if tuck_r >= min_target_rr_tuck:
                    logger.info(f"[{context.symbol}] Devil's Advocate threat level {threat_lvl} detected ahead of TP! Tucking TP: {tp_price} -> {adjusted_tp} ({tuck_r:.2f}R)")
                    tp_price = adjusted_tp
                    tp2_price = adjusted_tp
                    tp_dist = tp_price - entry_price
                    rr_ratio = round(tp_dist / (risk_dist + 1e-9), 2)
                else:
                    logger.info(f"[{context.symbol}] Devil's threat level {threat_lvl} gives {tuck_r:.2f}R < {min_target_rr_tuck}:1. Maintaining structural TP {tp_price}.")
            elif tentative_bias == "SELL" and tp_price < threat_lvl < entry_price:
                adjusted_tp = round(threat_lvl + (atr_val * 0.1), spec.digits)
                tuck_r = (entry_price - adjusted_tp) / (risk_dist + 1e-9)
                if tuck_r >= min_target_rr_tuck:
                    logger.info(f"[{context.symbol}] Devil's Advocate threat level {threat_lvl} detected ahead of TP! Tucking TP: {tp_price} -> {adjusted_tp} ({tuck_r:.2f}R)")
                    tp_price = adjusted_tp
                    tp2_price = adjusted_tp
                    tp_dist = entry_price - tp_price
                    rr_ratio = round(tp_dist / (risk_dist + 1e-9), 2)
                else:
                    logger.info(f"[{context.symbol}] Devil's threat level {threat_lvl} gives {tuck_r:.2f}R < {min_target_rr_tuck}:1. Maintaining structural TP {tp_price}.")



        strategy_probs = self.strategy_selector.select_strategy_probabilities(
            regime, context=context, account_equity=account_balance, style=style
        )
        best_strategy = max(strategy_probs.items(), key=lambda x: x[1])[0]

        ai_score = self._blended_ai_score(analyst_reports, context.symbol)

        of_res = {"signal": "NEUTRAL", "strength": 0.0, "institutional_activity": False}
        if mtf_data and "primary" in mtf_data and not mtf_data["primary"].empty:
            m_depth = mtf_data.get("market_depth") if isinstance(mtf_data, dict) else None
            of_res = self.order_flow.analyze_order_flow(mtf_data["primary"], market_depth=m_depth)
            try:
                context.order_flow = of_res
            except Exception:
                pass
            
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
            win_p_floor = 0.50 if ai_score >= 68.0 else 0.35
            calibrated_win_p = min(0.99, max(win_p_floor, calibrated_win_p * p_mult))
            final_win_p = min(0.99, max(win_p_floor, final_win_p * p_mult))
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
        is_micro_mode = is_micro_account(account_balance)
        if news_reaction.get("news_reversal_setup"):
            rev_bias = news_reaction.get("reversal_bias")
            if rev_bias == tentative_bias:
                c_boost = news_reaction.get("conviction_boost", 0.0)
                calibrated_win_p = _bounded_bayesian_probability_update(calibrated_win_p, c_boost, style, is_micro_mode)
                final_win_p = _bounded_bayesian_probability_update(final_win_p, c_boost, style, is_micro_mode)
                ai_score = min(100.0, ai_score + 8.0)
                logger.info(f"[{context.symbol}] Post-news sweep reversal aligns with {tentative_bias}: {news_reaction.get('reason')}")
            else:
                ai_score = max(0.0, ai_score - 10.0)
                logger.warning(f"[{context.symbol}] Post-news sweep reversal opposing {tentative_bias} (favors {rev_bias})! Penalizing AI score to {ai_score:.1f}")

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
                win_p_floor = 0.50 if ai_score >= 68.0 else 0.35
                p_ceil = 0.65 if ("SCALP" in str(style).upper() or is_micro_mode) else 0.72
                calibrated_win_p = min(p_ceil, max(win_p_floor, calibrated_win_p * sl_multiplier))
                final_win_p = min(p_ceil, max(win_p_floor, final_win_p * sl_multiplier))

        is_fx = _is_forex(context.symbol)

        # 4.5 High-Confluence Bonus — boost win prob when multiple independent signals align
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
            calibrated_win_p = _bounded_bayesian_probability_update(calibrated_win_p, bonus, style, is_micro_mode)
            final_win_p = _bounded_bayesian_probability_update(final_win_p, bonus, style, is_micro_mode)
            ai_score = min(100.0, ai_score + (confluence_count * 2.0))
            logger.info(f"[{context.symbol}] High confluence ({confluence_count}/5) bonus +{bonus:.3f} win prob")

        # Targeted extra boost for all symbols when 4+ confluence
        if confluence_count >= 4:
            extra_boost = 0.020 if is_fx else 0.015
            calibrated_win_p = _bounded_bayesian_probability_update(calibrated_win_p, extra_boost, style, is_micro_mode)
            final_win_p = _bounded_bayesian_probability_update(final_win_p, extra_boost, style, is_micro_mode)

        # Symbol-specific optimizations for 75% win rate target
        sym_upper = context.symbol.upper()

        if "EUR" in sym_upper or "GBP" in sym_upper:
            if is_prime:
                calibrated_win_p = _bounded_bayesian_probability_update(calibrated_win_p, 0.03, style, is_micro_mode)
                final_win_p = _bounded_bayesian_probability_update(final_win_p, 0.03, style, is_micro_mode)
        elif "USDJPY" in sym_upper:
            adx_val = getattr(context.momentum, "adx", 20.0) if hasattr(context, "momentum") else 20.0
            if adx_val >= 25:
                calibrated_win_p = _bounded_bayesian_probability_update(calibrated_win_p, 0.04, style, is_micro_mode)
                final_win_p = _bounded_bayesian_probability_update(final_win_p, 0.04, style, is_micro_mode)
        elif "BTC" in sym_upper or "ETH" in sym_upper:
            if regime.primary_regime in (MarketRegime.RANGE, MarketRegime.COMPRESSION):
                calibrated_win_p = _bounded_bayesian_probability_update(calibrated_win_p, 0.05, style, is_micro_mode)
                final_win_p = _bounded_bayesian_probability_update(final_win_p, 0.05, style, is_micro_mode)

        # 4.6 AI Dissection — 7-pillar real-time confluence scoring
        _dissection = self.ai_dissector.dissect(context, regime, rr_ratio, ev, ai_score, calibrated_win_p)
        _dissection_score = _dissection["dissection_score"]
        _dissection_tier = _dissection["tier"]
        _dissection_boost = float(_dissection.get("prob_boost", 0.0))
        if _dissection_boost > 0:
            calibrated_win_p = _bounded_bayesian_probability_update(calibrated_win_p, _dissection_boost, style, is_micro_mode)
            final_win_p = _bounded_bayesian_probability_update(final_win_p, _dissection_boost, style, is_micro_mode)
        if _dissection_score >= 70:
            logger.info(f"[{context.symbol}] AI Dissection HIGH {_dissection_score:.1f} tier={_dissection_tier} boost +{_dissection_boost:.3f}")

        # 4.7 Master Confluence — proven stacks from trading masters (Wyckoff+ICT+VCP+Triple)
        _master = self.master_confluence.score(context, regime, rr_ratio, ai_score, mtf_data)
        _master_score = _master["total"]
        _master_tier = _master["tier"]
        _master_boost = float(_master.get("prob_boost", 0.0))
        if _master_boost > 0:
            calibrated_win_p = _bounded_bayesian_probability_update(calibrated_win_p, _master_boost, style, is_micro_mode)
            final_win_p = _bounded_bayesian_probability_update(final_win_p, _master_boost, style, is_micro_mode)
        
        # HARD GATE: Horizon-Adaptive Master Confluence Threshold
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
            logger.info(f"[{context.symbol}] Master Confluence {_master_tier} {_master_score}/100 boost +{_master_boost:.3f} {_master['breakdown']}")

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
                        calibrated_win_p = _bounded_bayesian_probability_update(calibrated_win_p, _fvg_boost, style, is_micro_mode)
                        final_win_p = _bounded_bayesian_probability_update(final_win_p, _fvg_boost, style, is_micro_mode)
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

            # Prerequisite trigger validation across ALL assets:
            # A reversal strategy requires actual structural or liquidity trigger evidence
            if strat == "CHOCH_STRUCTURAL_REVERSAL" and not bool(getattr(context.structure, "choch", False)):
                continue
            if strat == "LIQUIDITY_SWEEP_REVERSAL" and not bool(getattr(context.liquidity, "sweep_detected", False)):
                continue

            # Strategy-specific edge & RR adjustments
            if strat == "RANGE_MEAN_REVERSION":
                if context.momentum.adx < 20:
                    strat_p = _bounded_bayesian_probability_update(strat_p, 0.03, style, is_micro_mode)
                strat_rr = min(2.0, max(1.6, strat_rr * 0.9))
            elif strat == "TREND_FOLLOWING":
                if context.momentum.adx >= 25 and context.structure.bos:
                    strat_p = _bounded_bayesian_probability_update(strat_p, 0.03, style, is_micro_mode)
                strat_rr = max(2.2, strat_rr * 1.1)
            elif strat == "BREAKOUT_EXPANSION":
                if context.volatility.state in ("EXPANSION", "EXTREME"):
                    strat_p = _bounded_bayesian_probability_update(strat_p, 0.03, style, is_micro_mode)
                strat_rr = max(2.5, strat_rr * 1.15)
            elif strat == "LIQUIDITY_SWEEP_REVERSAL":
                if context.liquidity.sweep_detected:
                    strat_p = _bounded_bayesian_probability_update(strat_p, 0.05, style, is_micro_mode)
                strat_rr = max(2.0, strat_rr * 1.05)
            elif strat == "CHOCH_STRUCTURAL_REVERSAL":
                if context.structure.choch:
                    strat_p = _bounded_bayesian_probability_update(strat_p, 0.05, style, is_micro_mode)
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
        is_index_sym = getattr(spec_eval, "asset_class", "") == "INDEX" or any(k in sym_name_eval for k in ["US500", "NAS100", "US30", "SPX", "NDX", "DJ30", "DJI", "DOW", "GER40", "UK100"])
        if _is_forex(context.symbol):
            if tentative_bias == "BUY" and st.discount_premium_zone == "PREMIUM" and trend_score_val < 65:
                premium_discount_valid = False
            elif tentative_bias == "SELL" and st.discount_premium_zone == "DISCOUNT" and trend_score_val > -65:
                premium_discount_valid = False
        elif is_crypto_sym:
            has_crypto_breakout = bool(st.bos or getattr(context.liquidity, "sweep_detected", False) or getattr(context, "candlestick_pattern", {}).get("pattern_detected", False))
            if tentative_bias == "BUY" and st.discount_premium_zone == "PREMIUM" and trend_score_val < 45 and not has_crypto_breakout:
                premium_discount_valid = False
            elif tentative_bias == "SELL" and st.discount_premium_zone == "DISCOUNT" and trend_score_val > -45 and not has_crypto_breakout:
                premium_discount_valid = False
        elif is_index_sym:
            if tentative_bias == "BUY" and st.discount_premium_zone == "PREMIUM" and trend_score_val < 40:
                premium_discount_valid = False
            elif tentative_bias == "SELL" and st.discount_premium_zone == "DISCOUNT" and trend_score_val > -40:
                premium_discount_valid = False
        else:
            if tentative_bias == "BUY" and st.discount_premium_zone == "PREMIUM" and trend_score_val < 50:
                if not bool(getattr(context.liquidity, "sweep_detected", False)):
                    premium_discount_valid = False
            elif tentative_bias == "SELL" and st.discount_premium_zone == "DISCOUNT" and trend_score_val > -50:
                if not bool(getattr(context.liquidity, "sweep_detected", False)):
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
            _recent_wr: Optional[float] = None
            try:
                _recent_wr = self.recent_win_rate_fn()
            except Exception as _wr_exc:
                logger.debug(f"Recent win-rate supplier failed: {_wr_exc}")
                _recent_wr = None
            _gp_decision, _gp_gates = self.gate_policy.decide(failing_reasons, recent_win_rate=_recent_wr)
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

        # Dedicated Scalp Execution Engine integration
        scalp_res = None
        if "SCALP" in style.upper() and is_mkt_open:
            df_m1 = None
            df_m5 = None
            if isinstance(mtf_data, dict):
                df_m1 = mtf_data.get("M1") if mtf_data.get("M1") is not None else mtf_data.get("timing")
                _m5_cand = mtf_data.get("M5")
                if _m5_cand is None:
                    _m5_cand = mtf_data.get("primary")
                if _m5_cand is None:
                    _m5_cand = mtf_data.get("setup")
                df_m5 = _m5_cand
            if df_m5 is None and isinstance(recent_candles, list) and len(recent_candles) >= 5:
                import pandas as pd
                df_m5 = pd.DataFrame(recent_candles)
            
            scalp_res = self.scalp_engine.evaluate_scalp(
                context=context,
                regime=regime,
                tentative_bias=tentative_bias,
                df_m1=df_m1,
                df_m5=df_m5,
                account_balance=account_balance,
                risk_per_trade_pct=risk_per_trade_pct
            )
            if scalp_res.action == "NO_TRADE":
                gate_passed = False
                failing_reasons.extend(scalp_res.failing_reasons)
            elif scalp_res.action == "WAIT":
                gate_passed = False
            elif scalp_res.action == "EXECUTE" and gate_passed:
                sl_price = scalp_res.stop_loss
                tp1_price = scalp_res.tp1
                tp2_price = scalp_res.tp2
                tp3_price = scalp_res.tp3
                tp_price = scalp_res.tp2
                rr_ratio = scalp_res.risk_reward

        if not is_mkt_open:
            decision_action = "NO_TRADE"
        elif gate_passed:
            if tentative_bias in ["BUY", "SELL"]:
                decision_action = "EXECUTE"
            else:
                decision_action = "NO_TRADE"
        elif scalp_res is not None and scalp_res.action == "WAIT":
            decision_action = "WAIT"
        elif not any(r in HARD_GATES for r in failing_reasons) and tentative_bias in ["BUY", "SELL"] and len(failing_reasons) <= 2 and quality_gate.checks.get("Regime Viability", False):
            decision_action = "WAIT"
        else:
            decision_action = "NO_TRADE"

        waiting_reasons = []
        if scalp_res is not None and scalp_res.action == "WAIT":
            waiting_reasons.extend(scalp_res.waiting_reasons)
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

        # Task 2a: Adaptive Institutional MARKET vs LIMIT order type routing
        cur_mkt_price = getattr(context, "current_price", entry_price)
        price_diff = abs(entry_price - cur_mkt_price)
        spec_info = resolve_symbol(context.symbol)
        digits = getattr(spec_info, "digits", 5) or 5
        pip_size = getattr(spec_info, "pip_size", 0.0001) or 0.0001
        tolerance_pip = (pip_size * 0.5) if pip_size > 0 else 0.00005
        atr_val = getattr(context.volatility, "atr", 0.0) or (cur_mkt_price * 0.005)
        spread_dist = max(0.0, context.ask - context.bid) if (context.ask > 0 and context.bid > 0) else (getattr(context.volatility, "current_spread_pips", 1.0) * pip_size)

        # Institutional Execution Rule: In active expansion, high momentum, or confirmed breakouts,
        # waiting for a deep limit order either causes missed trades or toxic fills on reversal.
        # Use MARKET order if price is within execution proximity (<= 0.40 ATR or <= 5x spread),
        # or if regime/momentum is actively trending.
        # Institutional Rule (§4.1 SMC/ICT & algo_methods_and_tricks.md):
        # "Sell from the Top" & "Buy from the Bottom" Execution:
        # A market order should NOT sell into an oversold discount floor (RSI < 32 or Discount zone)
        # or buy into an overbought premium roof (RSI > 68 or Premium zone), unless it is a verified momentum breakout expansion.
        st_zone = getattr(context.structure, "discount_premium_zone", "EQUILIBRIUM") if hasattr(context, "structure") else "EQUILIBRIUM"
        rsi_val = float(getattr(context.momentum, "rsi", 50.0) or 50.0) if hasattr(context, "momentum") else 50.0
        is_breakout_mode = best_strategy in ("BREAKOUT_EXPANSION", "BREAKDOWN") or is_expansion_val > 0 or is_extreme_val > 0

        has_bull_swp = bool(getattr(context.liquidity, "sweep_detected", False) and "BULLISH" in getattr(context.liquidity, "sweep_type", ""))
        has_bear_swp = bool(getattr(context.liquidity, "sweep_detected", False) and "BEARISH" in getattr(context.liquidity, "sweep_type", ""))
        # Chasing warning flags:
        is_chasing_discount_floor = (tentative_bias == "SELL" and (st_zone == "DISCOUNT" or rsi_val < 32.0 or trend_score_val <= -75.0 or has_bull_swp) and not is_breakout_mode)
        is_chasing_premium_roof = (tentative_bias == "BUY" and (st_zone == "PREMIUM" or rsi_val > 68.0 or trend_score_val >= 75.0 or has_bear_swp) and not is_breakout_mode)

        is_reversal_or_pullback = best_strategy in ("TREND_PULLBACK", "LIQUIDITY_SWEEP_REVERSAL", "CHOCH", "CHOCH_STRUCTURAL_REVERSAL", "RANGE_MEAN_REVERSION")
        is_trending_momentum = (abs(trend_score_val) >= 70.0 or is_breakout_mode) and not is_reversal_or_pullback
        is_close_proximity = price_diff <= max(0.25 * atr_val, spread_dist * 3.0)

        order_type_decision = "MARKET"
        if is_chasing_discount_floor or (tentative_bias == "SELL" and is_reversal_or_pullback and not is_close_proximity):
            # Sell from the top: Route to LIMIT order placed at Premium / Bearish Order Block / FVG CE
            order_type_decision = "LIMIT"
            ob_list = getattr(context.structure, "order_blocks", []) or []
            bear_obs = [ob for ob in ob_list if isinstance(ob, dict) and "BEAR" in str(ob.get("type", "")).upper()]
            fvg_list = getattr(context.structure, "fair_value_gaps", []) or []
            bear_fvgs = [f for f in fvg_list if isinstance(f, dict) and "BEAR" in str(f.get("type", "")).upper()]
            if bear_fvgs:
                # Target FVG Consequent Encroachment (50% midpoint)
                f_top = float(bear_fvgs[0].get("top", cur_mkt_price + 0.35 * atr_val))
                f_bot = float(bear_fvgs[0].get("bottom", cur_mkt_price + 0.15 * atr_val))
                entry_price = round((f_top + f_bot) / 2.0, digits)
            elif bear_obs:
                entry_price = round(float(bear_obs[0].get("low", cur_mkt_price + 0.35 * atr_val)), digits)
            elif hasattr(context.structure, "equilibrium_price") and context.structure.equilibrium_price > cur_mkt_price:
                entry_price = round(context.structure.equilibrium_price, digits)
            else:
                entry_price = round(cur_mkt_price + (0.35 * atr_val), digits)
            sl_price = round(entry_price + risk_dist, digits)
            tp_price = round(entry_price - tp_dist, digits)
        elif is_chasing_premium_roof or (tentative_bias == "BUY" and is_reversal_or_pullback and not is_close_proximity):
            # Buy from the bottom: Route to LIMIT order placed at Discount / Bullish Order Block / FVG CE
            order_type_decision = "LIMIT"
            ob_list = getattr(context.structure, "order_blocks", []) or []
            bull_obs = [ob for ob in ob_list if isinstance(ob, dict) and "BULL" in str(ob.get("type", "")).upper()]
            fvg_list = getattr(context.structure, "fair_value_gaps", []) or []
            bull_fvgs = [f for f in fvg_list if isinstance(f, dict) and "BULL" in str(f.get("type", "")).upper()]
            if bull_fvgs:
                # Target FVG Consequent Encroachment (50% midpoint)
                f_top = float(bull_fvgs[0].get("top", cur_mkt_price - 0.15 * atr_val))
                f_bot = float(bull_fvgs[0].get("bottom", cur_mkt_price - 0.35 * atr_val))
                entry_price = round((f_top + f_bot) / 2.0, digits)
            elif bull_obs:
                entry_price = round(float(bull_obs[0].get("high", cur_mkt_price - 0.35 * atr_val)), digits)
            elif hasattr(context.structure, "equilibrium_price") and 0 < context.structure.equilibrium_price < cur_mkt_price:
                entry_price = round(context.structure.equilibrium_price, digits)
            else:
                entry_price = round(cur_mkt_price - (0.35 * atr_val), digits)
            sl_price = round(entry_price - risk_dist, digits)
            tp_price = round(entry_price + tp_dist, digits)
        elif is_trending_momentum or is_close_proximity:
            # Execute at market to capture the move without hanging pending orders
            order_type_decision = "MARKET"
            if price_diff > 1e-5:
                # Re-anchor entry to observed market price and preserve structural risk distance
                live_market_entry = round(context.ask if tentative_bias == "BUY" and context.ask > 0 else (context.bid if tentative_bias == "SELL" and context.bid > 0 else cur_mkt_price), digits)
                entry_delta = live_market_entry - entry_price
                entry_price = live_market_entry
                sl_price = round(sl_price + entry_delta, digits)
                tp_price = round(tp_price + entry_delta, digits)
                if tp1_price is not None:
                    tp1_price = round(tp1_price + entry_delta, digits)
                if tp2_price is not None:
                    tp2_price = round(tp2_price + entry_delta, digits)
                if tp3_price is not None:
                    tp3_price = round(tp3_price + entry_delta, digits)
                tp_dist = abs(tp_price - entry_price)
        elif price_diff > max(0.40 * atr_val, tolerance_pip * 8):
            # Only use LIMIT order when price is resting genuinely far away in a calm market
            order_type_decision = "LIMIT"

        # Institutional Rule: When an adversarial threat barrier sits between entry and target:
        # If the barrier allows viable runway (>= 1.40R), tuck TP in front of it to capture profit before reversal.
        # If the barrier is too close (< 1.40R), maintain structural target so we don't prematurely abort setups.
        min_allowed_rr = 1.40 if is_micro_mode else (1.50 if _is_forex(context.symbol) else 1.60)
        if threat_lvl is not None and isinstance(threat_lvl, (int, float)) and threat_lvl > 0:
            if tentative_bias == "BUY" and entry_price < threat_lvl <= tp_price:
                potential_tp = round(threat_lvl - (atr_val * 0.1), digits)
                tuck_r = (potential_tp - entry_price) / max(risk_dist, 1e-9)
                if tuck_r >= 1.40:
                    tp_price = potential_tp
                    tp_dist = abs(tp_price - entry_price)
                else:
                    logger.info(
                        f"[{context.symbol}] Devil's threat level {threat_lvl} gives {tuck_r:.2f}R < 1.40R. "
                        f"Maintaining structural TP {tp_price}."
                    )
            elif tentative_bias == "SELL" and tp_price <= threat_lvl < entry_price:
                potential_tp = round(threat_lvl + (atr_val * 0.1), digits)
                tuck_r = (entry_price - potential_tp) / max(risk_dist, 1e-9)
                if tuck_r >= 1.40:
                    tp_price = potential_tp
                    tp_dist = abs(tp_price - entry_price)
                else:
                    logger.info(
                        f"[{context.symbol}] Devil's threat level {threat_lvl} gives {tuck_r:.2f}R < 1.40R. "
                        f"Maintaining structural TP {tp_price}."
                    )

        # Recalculate true physical distances and R:R ratio
        tp_dist = abs(tp_price - entry_price)
        risk_dist = abs(entry_price - sl_price)
        rr_ratio = round(tp_dist / max(risk_dist, 1e-9), 2)

        # Final Absolute Geometric Quality Gate Re-Check
        if gate_passed and rr_ratio < (min_allowed_rr - 0.05):
            decision_action = "NO_TRADE"
            gate_passed = False
            rejection_reasons.append(f"Geometric R:R ratio {rr_ratio:.2f}:1 fails minimum required {min_allowed_rr:.2f}:1")

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
            as_limit_price=as_limit_price,
            fallback_rung=getattr(self, "current_fallback_rung", "RUN"),
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

    def _check_pullback_reclaim(
        self,
        context: MarketContext,
        mtf_data: Optional[Dict[str, Any]] = None,
        bias: str = "BUY"
    ) -> bool:
        """
        Patch 3.2: Allow Bull-Flag Pullback Hook Re-Entries.
        Bypass momentum deceleration rejection when price reclaims the EMA and
        the latest closed M15/M5 bar closes in the direction of the trade (is_pullback_reclaim=True).
        """
        try:
            df = None
            if mtf_data and isinstance(mtf_data, dict):
                df = mtf_data.get("timing")
                if df is None or getattr(df, "empty", True):
                    df = mtf_data.get("primary")
            if df is None or getattr(df, "empty", True) or len(df) < 5:
                df = getattr(context, "df_m5", None)
            if df is None or getattr(df, "empty", True) or len(df) < 5:
                return False

            close = float(df["close"].iloc[-1])
            open_p = float(df["open"].iloc[-1])
            c_price = float(getattr(context, "current_price", close) or close)

            # Fast EMA (span=9)
            ema9 = float(df["close"].ewm(span=9, adjust=False).mean().iloc[-1])

            if bias == "BUY":
                # Price reclaimed EMA and latest closed candle is bullish
                if (c_price >= ema9 or close >= ema9) and close > open_p:
                    return True
            elif bias == "SELL":
                # Price broke below EMA and latest closed candle is bearish
                if (c_price <= ema9 or close <= ema9) and close < open_p:
                    return True
        except Exception:
            pass
        return False

    def _is_momentum_valid_for_regime(
        self,
        bias: str,
        mom_score: float,
        mom_delta: float,
        regime: str,
        order_flow_confirmed: bool = False,
        is_pullback_reclaim: bool = False,
        context: Optional[MarketContext] = None,
        mtf_data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """
        Regime-Adaptive Momentum Validator.
        Allows uncapped momentum (up to ±98) in expansion/breakout regimes and index trends,
        while strictly capping momentum (<= 75) in ranging/consolidation regimes.
        Accommodates healthy trend pullbacks where momentum temporarily decelerates before re-acceleration.
        Bypasses deceleration rejection when price reclaims the EMA and latest closed bar closes
        in the direction of the trade (Patch 3.2).
        """
        if not is_pullback_reclaim and context is not None:
            is_pullback_reclaim = self._check_pullback_reclaim(context, mtf_data, bias)

        expansion_regimes = {
            "TREND_BULL", "TREND_BEAR", "STRONG_TREND_BULL", "STRONG_TREND_BEAR",
            "BREAKOUT", "POST_BREAKOUT"
        }
        is_expansion = (str(regime).upper() in expansion_regimes) or order_flow_confirmed or is_pullback_reclaim

        max_abs_score = 98.0 if is_expansion else 75.0
        min_abs_score = 8.0 if is_pullback_reclaim else (10.0 if is_expansion else 15.0)
        decel_limit = -30.0 if is_pullback_reclaim else -15.0

        if bias == "BUY":
            if mom_score < min_abs_score:
                return False, f"BUY momentum too weak ({mom_score:.2f} < {min_abs_score})"
            if mom_score > max_abs_score:
                return False, f"BUY momentum exhausted for regime {regime} ({mom_score:.2f} > {max_abs_score})"
            if not is_expansion and mom_delta < decel_limit:
                return False, f"BUY momentum decelerating ({mom_delta:.2f})"
            return True, "OK"

        if bias == "SELL":
            if mom_score > -min_abs_score:
                return False, f"SELL momentum too weak ({mom_score:.2f} > -{min_abs_score})"
            if mom_score < -max_abs_score:
                return False, f"SELL momentum exhausted for regime {regime} ({mom_score:.2f} < -{max_abs_score})"
            if not is_expansion and mom_delta > -decel_limit:
                return False, f"SELL momentum decelerating ({mom_delta:.2f})"
            return True, "OK"

        return False, "NEUTRAL bias"
