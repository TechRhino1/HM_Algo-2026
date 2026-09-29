"""
HM Algo 2.0 — Dedicated Scalp Execution Engine.
Treats scalping with dedicated microstructure physics (M1/M5, VWAP, liquidity sweep + 5-step displacement sequence),
adaptive multi-stage SL/TP milestones, cost-friction gates, and zero-tolerance unvalidated counter-trend trade filtering.
"""
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
import logging
import numpy as np
import pandas as pd

from jarvis.data.schemas import MarketContext, RegimeOutput, DecisionObject, TradeQualityGateResult
from jarvis.scalping.microstructure_engine import MicrostructureEngine, MicrostructureMetrics, MicrostructureState
from jarvis.market.sessions import SessionEngine
from jarvis.data.symbol_registry import resolve as resolve_symbol

logger = logging.getLogger("JARVIS_ScalpExecutionEngine")


@dataclass
class ScalpQualityScore:
    total_score: float = 0.0
    direction_score: float = 0.0      # max 25
    htf_confluence_score: float = 0.0 # max 20
    microstructure_score: float = 0.0 # max 15
    liquidity_score: float = 0.0      # max 15
    momentum_score: float = 0.0       # max 10
    volume_score: float = 0.0         # max 5
    vwap_score: float = 0.0           # max 5
    rr_score: float = 0.0             # max 5
    is_qualified: bool = False        # >= 75
    breakdown: Dict[str, float] = field(default_factory=dict)


@dataclass
class ScalpDecision:
    action: str                       # "BUY", "SELL", "WAIT", "NO_TRADE"
    bias: str                         # "BUY", "SELL", "HOLD"
    symbol: str
    quality_score: ScalpQualityScore
    entry_price: float = 0.0
    stop_loss: float = 0.0
    tp1: float = 0.0                  # 1.0R scale-out (50%) & move to breakeven
    tp2: float = 0.0                  # Opposing liquidity / VWAP (30%)
    tp3: float = 0.0                  # Trailing runner (20%)
    risk_reward: float = 0.0
    expected_profit_pips: float = 0.0
    total_friction_pips: float = 0.0
    friction_ratio: float = 0.0
    microstructure: Optional[MicrostructureMetrics] = None
    passed_cost_gate: bool = False
    passed_five_step_sequence: bool = False
    failing_reasons: List[str] = field(default_factory=list)
    waiting_reasons: List[str] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)


class ScalpExecutionEngine:
    """
    Dedicated Scalp Execution Engine.
    Enforces the 5-step precision entry sequence:
      1. Liquidity Sweep
      2. Rejection
      3. Displacement Candle (body >= 50%)
      4. Micro BOS
      5. Retracement into Discount/Equilibrium
    Enforces cost-friction bounds (< 25% of expected gross profit) and strictly forbids
    unvalidated counter-trend scalps.
    """

    MIN_QUALITY_SCORE = 75.0
    MAX_FRICTION_PCT = 0.25  # Max 25% of gross profit eaten by spread + slippage + commission
    NEWS_LOCKOUT_MINUTES_PRE = 10
    NEWS_LOCKOUT_MINUTES_POST = 5

    def __init__(self, microstructure_engine: Optional[MicrostructureEngine] = None):
        self.microstructure_engine = microstructure_engine or MicrostructureEngine()

    def is_in_scalp_killzone(self, dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Detects if current time falls within high-liquidity scalp killzones:
          - Asian Overlap: 00:00 - 03:00 UTC
          - London Open: 07:00 - 10:30 UTC
          - NY Open: 12:30 - 16:30 UTC
        """
        now = dt or datetime.now(timezone.utc)
        minute_of_day = now.hour * 60 + now.minute
        
        is_asian = (0 <= minute_of_day <= 180)
        is_london = (420 <= minute_of_day <= 630)
        is_ny = (750 <= minute_of_day <= 990)

        in_kz = is_asian or is_london or is_ny
        kz_name = "NONE"
        if is_london:
            kz_name = "LONDON_OPEN"
        elif is_ny:
            kz_name = "NY_OPEN"
        elif is_asian:
            kz_name = "ASIAN_OVERLAP"

        return {
            "in_killzone": in_kz,
            "killzone_name": kz_name,
            "utc_time": now.strftime("%H:%M UTC")
        }

    def check_economic_news_lockout(self, context: MarketContext) -> bool:
        """
        Hard kill switch: returns True if current time is within 10m pre or 5m post high-impact news.
        """
        macro = getattr(context, "macro", None)
        if not macro:
            return False
        
        # Check if macro event risk is flagged
        event_risk = getattr(macro, "event_risk", False) or getattr(macro, "high_impact_news_imminent", False)
        if event_risk:
            return True
            
        minutes_to_news = getattr(macro, "minutes_to_next_high_impact_news", None)
        if minutes_to_news is not None:
            if -self.NEWS_LOCKOUT_MINUTES_POST <= minutes_to_news <= self.NEWS_LOCKOUT_MINUTES_PRE:
                return True
        return False

    def evaluate_five_step_sequence(
        self,
        bias: str,
        context: MarketContext,
        micro: MicrostructureMetrics,
        df_m1_or_m5: Optional[pd.DataFrame] = None
    ) -> Dict[str, Any]:
        """
        Verifies the 5-step precision entry sequence:
          Step 1: Liquidity Sweep
          Step 2: Rejection
          Step 3: Displacement Candle (body >= 50% range)
          Step 4: Micro BOS
          Step 5: Retracement into Discount/Equilibrium
        """
        st = context.structure
        liq = context.liquidity
        c_price = context.current_price
        
        # Step 1: Liquidity Sweep
        step1_sweep = micro.sweep_detected or getattr(liq, "sweep_detected", False)
        sweep_side = micro.sweep_side or getattr(liq, "sweep_side", "NONE")
        if bias == "BUY" and sweep_side not in ("SELL_SIDE", "LIQUIDITY_POOL", "NONE"):
            step1_sweep = False
        elif bias == "SELL" and sweep_side not in ("BUY_SIDE", "LIQUIDITY_POOL", "NONE"):
            step1_sweep = False

        # Step 2: Rejection
        step2_rejection = micro.absorption_detected or step1_sweep

        # Step 3: Displacement Candle
        step3_displacement = micro.displacement_ratio >= 0.50

        # Step 4: Micro BOS
        step4_micro_bos = bool(getattr(st, "bos", False) or getattr(st, "choch", False) or micro.state in (MicrostructureState.BREAKOUT, MicrostructureState.TRENDING))

        # Step 5: Retracement into Discount/Equilibrium
        zone = getattr(st, "discount_premium_zone", "EQUILIBRIUM")
        if bias == "BUY":
            step5_retracement = (zone in ("DISCOUNT", "EQUILIBRIUM"))
        elif bias == "SELL":
            step5_retracement = (zone in ("PREMIUM", "EQUILIBRIUM"))
        else:
            step5_retracement = False

        all_passed = (step1_sweep and step2_rejection and step3_displacement and step4_micro_bos and step5_retracement)
        
        missing_steps = []
        if not step1_sweep:
            missing_steps.append("Step 1: Liquidity Sweep")
        if not step2_rejection:
            missing_steps.append("Step 2: Rejection / Absorption")
        if not step3_displacement:
            missing_steps.append("Step 3: Displacement Candle (>= 50% body)")
        if not step4_micro_bos:
            missing_steps.append("Step 4: Micro BOS")
        if not step5_retracement:
            missing_steps.append(f"Step 5: Retracement to Favorable Zone (currently {zone})")

        return {
            "all_passed": all_passed,
            "step1_sweep": step1_sweep,
            "step2_rejection": step2_rejection,
            "step3_displacement": step3_displacement,
            "step4_micro_bos": step4_micro_bos,
            "step5_retracement": step5_retracement,
            "missing_steps": missing_steps
        }

    def compute_scalp_quality_score(
        self,
        bias: str,
        context: MarketContext,
        regime: RegimeOutput,
        micro: MicrostructureMetrics,
        is_killzone: bool,
        is_counter_trend: bool,
        rr_ratio: float
    ) -> ScalpQualityScore:
        """
        Calculates composite scalp quality score (0 to 100).
        """
        direction_pts = 0.0
        htf_pts = 0.0
        micro_pts = 0.0
        liq_pts = 0.0
        mom_pts = 0.0
        vol_pts = 0.0
        vwap_pts = 0.0
        rr_pts = 0.0

        # 1. Direction & Regime Confluence (max 25 pts)
        reg_val = str(getattr(regime, "primary_regime", "RANGE")).upper()
        if (bias == "BUY" and "TREND_BULL" in reg_val) or (bias == "SELL" and "TREND_BEAR" in reg_val):
            direction_pts = 25.0
        elif "BREAKOUT" in reg_val:
            direction_pts = 20.0
        elif "RANGE" in reg_val:
            direction_pts = 15.0
        elif is_counter_trend:
            direction_pts = 5.0
        else:
            direction_pts = 12.0

        # 2. HTF Confluence (H1/M15) (max 20 pts)
        mtf_align = getattr(context, "mtf_alignment", {}) or {}
        h1_bias = mtf_align.get("H1", "NEUTRAL")
        m15_bias = mtf_align.get("M15", "NEUTRAL")
        if (bias == "BUY" and h1_bias == "BULLISH") or (bias == "SELL" and h1_bias == "BEARISH"):
            htf_pts += 12.0
        elif h1_bias == "NEUTRAL":
            htf_pts += 6.0
        
        if (bias == "BUY" and m15_bias == "BULLISH") or (bias == "SELL" and m15_bias == "BEARISH"):
            htf_pts += 8.0
        elif m15_bias == "NEUTRAL":
            htf_pts += 4.0

        # 3. Microstructure Quality (max 15 pts)
        if micro.state in (MicrostructureState.TRENDING, MicrostructureState.BREAKOUT):
            micro_pts = 15.0
        elif micro.state in (MicrostructureState.PULLBACK, MicrostructureState.LIQUIDITY_EVENT):
            micro_pts = 12.0
        elif micro.state == MicrostructureState.RANGE:
            micro_pts = 8.0
        elif micro.state == MicrostructureState.CHOP:
            micro_pts = 0.0

        if is_killzone:
            micro_pts = min(15.0, micro_pts + 3.0)

        # 4. Liquidity Sweep / Absorption (max 15 pts)
        if micro.sweep_detected:
            liq_pts += 10.0
        if micro.absorption_detected:
            liq_pts += 5.0

        # 5. Momentum & Velocity (max 10 pts)
        mom = getattr(context, "momentum", None)
        trend_score = float(getattr(mom, "trend_score", 0.0) or 0.0)
        adx = float(getattr(mom, "adx", 20.0) or 20.0)
        if (bias == "BUY" and trend_score > 20) or (bias == "SELL" and trend_score < -20):
            mom_pts += 6.0
        elif abs(trend_score) <= 20:
            mom_pts += 3.0
        if adx >= 22:
            mom_pts += 4.0

        # 6. Volume Imbalance & Delta (max 5 pts)
        delta = micro.volume_imbalance_ratio
        if (bias == "BUY" and delta > 0.15) or (bias == "SELL" and delta < -0.15):
            vol_pts = 5.0
        elif abs(delta) <= 0.15:
            vol_pts = 2.5

        # 7. VWAP Proximity & Alignment (max 5 pts)
        dist_atr = micro.vwap_distance_atr
        # Scalping likes entering near VWAP (within 1.0 ATR)
        if abs(dist_atr) <= 1.0:
            if (bias == "BUY" and dist_atr >= -0.5) or (bias == "SELL" and dist_atr <= 0.5):
                vwap_pts = 5.0
            else:
                vwap_pts = 3.5
        else:
            vwap_pts = 1.0

        # 8. R:R Geometry (max 5 pts)
        if rr_ratio >= 2.0:
            rr_pts = 5.0
        elif rr_ratio >= 1.5:
            rr_pts = 4.0
        elif rr_ratio >= 1.2:
            rr_pts = 2.0

        total = round(direction_pts + htf_pts + micro_pts + liq_pts + mom_pts + vol_pts + vwap_pts + rr_pts, 1)
        qualified = total >= self.MIN_QUALITY_SCORE

        breakdown = {
            "direction": direction_pts,
            "htf_confluence": htf_pts,
            "microstructure": micro_pts,
            "liquidity": liq_pts,
            "momentum": mom_pts,
            "volume": vol_pts,
            "vwap": vwap_pts,
            "rr": rr_pts
        }

        return ScalpQualityScore(
            total_score=total,
            direction_score=direction_pts,
            htf_confluence_score=htf_pts,
            microstructure_score=micro_pts,
            liquidity_score=liq_pts,
            momentum_score=mom_pts,
            volume_score=vol_pts,
            vwap_score=vwap_pts,
            rr_score=rr_pts,
            is_qualified=qualified,
            breakdown=breakdown
        )

    def evaluate_scalp(
        self,
        context: MarketContext,
        regime: RegimeOutput,
        tentative_bias: str,
        df_m1: Optional[pd.DataFrame] = None,
        df_m5: Optional[pd.DataFrame] = None,
        account_balance: float = 10000.0,
        risk_per_trade_pct: float = 0.5
    ) -> ScalpDecision:
        """
        Main entry point for scalp trade evaluation.
        """
        symbol = context.symbol
        spec = resolve_symbol(symbol)
        c_price = context.current_price
        atr = getattr(context.volatility, "atr", 0.0) or (c_price * 0.002)
        spread_pips = getattr(context.volatility, "current_spread_pips", 1.5) or 1.5
        typ_spread_pips = getattr(spec, "typical_spread_pips", 1.5) or 1.5

        failing_reasons = []
        waiting_reasons = []
        diagnostics = {}

        # 1. Microstructure Physics Assessment
        micro = self.microstructure_engine.analyze(
            df_m1=df_m1,
            df_m5=df_m5,
            current_spread_pips=spread_pips,
            typical_spread_pips=typ_spread_pips,
            order_flow=getattr(context, "order_flow", None)
        )
        diagnostics["microstructure"] = micro

        # Microstructure CHOP veto
        if micro.is_chop or micro.state == MicrostructureState.CHOP:
            failing_reasons.append(f"Microstructure in CHOP / Unstable Liquidity state: {', '.join(micro.reasons)}")
            return ScalpDecision(
                action="NO_TRADE",
                bias=tentative_bias,
                symbol=symbol,
                quality_score=ScalpQualityScore(),
                microstructure=micro,
                failing_reasons=failing_reasons,
                diagnostics=diagnostics
            )

        # 2. Economic News Hard Kill Switch
        if self.check_economic_news_lockout(context):
            failing_reasons.append("High-Impact Economic News lockout window active (10m pre / 5m post).")
            return ScalpDecision(
                action="NO_TRADE",
                bias=tentative_bias,
                symbol=symbol,
                quality_score=ScalpQualityScore(),
                microstructure=micro,
                failing_reasons=failing_reasons,
                diagnostics=diagnostics
            )

        # 3. Killzone Detection
        kz_info = self.is_in_scalp_killzone(getattr(context, "timestamp", None))
        is_killzone = kz_info["in_killzone"]
        diagnostics["killzone"] = kz_info

        # 4. Check Counter-Trend Status
        reg_val = str(getattr(regime, "primary_regime", "RANGE")).upper()
        is_counter_trend = (
            (tentative_bias == "BUY" and "TREND_BEAR" in reg_val) or
            (tentative_bias == "SELL" and "TREND_BULL" in reg_val)
        )
        diagnostics["is_counter_trend"] = is_counter_trend

        # 5. Five-Step Precision Entry Verification
        five_step = self.evaluate_five_step_sequence(
            bias=tentative_bias,
            context=context,
            micro=micro,
            df_m1_or_m5=df_m1 if df_m1 is not None else df_m5
        )
        diagnostics["five_step_sequence"] = five_step

        # ZERO UNVALIDATED COUNTER-TREND TRADES MANDATE
        if is_counter_trend and not five_step["all_passed"]:
            failing_reasons.append(
                f"ZERO UNVALIDATED COUNTER-TREND TRADES: Missing required reversal confirmations: {', '.join(five_step['missing_steps'])}"
            )
            return ScalpDecision(
                action="NO_TRADE",
                bias=tentative_bias,
                symbol=symbol,
                quality_score=ScalpQualityScore(),
                microstructure=micro,
                passed_five_step_sequence=False,
                failing_reasons=failing_reasons,
                diagnostics=diagnostics
            )

        # 6. Adaptive Structural SL & Multi-Stage TP Calculation
        pip_size = getattr(spec, "pip_size", 0.0001) or 0.0001
        st = context.structure
        
        # Stop loss placed beyond sweep extreme or micro swing extreme
        if tentative_bias == "BUY":
            swing_low = getattr(st, "swing_low", None) or (c_price - 1.2 * atr)
            sl_price = round(min(swing_low, c_price - 1.0 * atr) - (spread_pips * pip_size * 0.5), 5)
            risk_dist = max(c_price - sl_price, atr * 0.5)
            tp1 = round(c_price + (1.0 * risk_dist), 5)  # 1.0R scale out 50%
            tp2 = round(c_price + (1.8 * risk_dist), 5)  # Opposing liquidity / micro target
            tp3 = round(c_price + (2.8 * risk_dist), 5)  # Runner
        elif tentative_bias == "SELL":
            swing_high = getattr(st, "swing_high", None) or (c_price + 1.2 * atr)
            sl_price = round(max(swing_high, c_price + 1.0 * atr) + (spread_pips * pip_size * 0.5), 5)
            risk_dist = max(sl_price - c_price, atr * 0.5)
            tp1 = round(c_price - (1.0 * risk_dist), 5)
            tp2 = round(c_price - (1.8 * risk_dist), 5)
            tp3 = round(c_price - (2.8 * risk_dist), 5)
        else:
            sl_price = 0.0
            tp1 = tp2 = tp3 = 0.0
            risk_dist = 0.0

        rr_ratio = round(abs(tp2 - c_price) / max(risk_dist, 1e-9), 2) if risk_dist > 0 else 0.0

        # 7. Spread & Cost Friction Gate (< 25% of gross profit)
        expected_profit_pips = round(abs(tp1 - c_price) / pip_size, 1) if pip_size > 0 else 0.0
        slippage_pips = round(spread_pips * 0.35, 1)  # Realistic slippage estimate
        commission_pips = 0.4  # Realistic commission in pips equivalent
        total_friction_pips = round(spread_pips + slippage_pips + commission_pips, 1)

        friction_ratio = round(total_friction_pips / max(expected_profit_pips, 0.1), 3)
        passed_cost_gate = friction_ratio <= self.MAX_FRICTION_PCT

        if not passed_cost_gate:
            failing_reasons.append(
                f"Cost Friction Gate Failed: Total friction ({total_friction_pips:.1f} pips) exceeds 25% of expected scalp profit ({expected_profit_pips:.1f} pips, ratio={friction_ratio*100:.1f}% > 25%)."
            )

        # 8. Scalp Quality AI Scoring
        quality_score = self.compute_scalp_quality_score(
            bias=tentative_bias,
            context=context,
            regime=regime,
            micro=micro,
            is_killzone=is_killzone,
            is_counter_trend=is_counter_trend,
            rr_ratio=rr_ratio
        )
        diagnostics["quality_score"] = quality_score

        if not quality_score.is_qualified:
            failing_reasons.append(
                f"Scalp Quality AI Score below minimum hurdle ({quality_score.total_score:.1f}/100 < {self.MIN_QUALITY_SCORE:.0f})."
            )

        # 9. Determine Action: EXECUTE, WAIT, or NO_TRADE
        if tentative_bias not in ("BUY", "SELL"):
            action = "NO_TRADE"
        elif passed_cost_gate and quality_score.is_qualified and (not is_counter_trend or five_step["all_passed"]):
            action = "EXECUTE"
        elif quality_score.total_score >= 60.0 and len(failing_reasons) <= 2:
            action = "WAIT"
            if not five_step["all_passed"]:
                waiting_reasons.append(f"Awaiting 5-step precision trigger: {', '.join(five_step['missing_steps'])}")
            if not passed_cost_gate:
                waiting_reasons.append(f"Awaiting spread tightening (current friction {friction_ratio*100:.1f}% > 25%)")
            if not quality_score.is_qualified:
                waiting_reasons.append(f"Awaiting institutional volume / momentum boost to reach quality threshold ({quality_score.total_score:.1f}/75)")
        else:
            action = "NO_TRADE"

        return ScalpDecision(
            action=action,
            bias=tentative_bias,
            symbol=symbol,
            quality_score=quality_score,
            entry_price=c_price,
            stop_loss=sl_price,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            risk_reward=rr_ratio,
            expected_profit_pips=expected_profit_pips,
            total_friction_pips=total_friction_pips,
            friction_ratio=friction_ratio,
            microstructure=micro,
            passed_cost_gate=passed_cost_gate,
            passed_five_step_sequence=five_step["all_passed"],
            failing_reasons=failing_reasons,
            waiting_reasons=waiting_reasons,
            diagnostics=diagnostics
        )
