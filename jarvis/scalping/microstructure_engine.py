"""
HM Algo 2.0 — High-Frequency Microstructure Regime & Physics Engine.
Analyzes M1/M5 price action, order flow delta, tick velocity, spread expansion,
candle body displacement, and VWAP geometry to detect the exact microstructure phase.
States: TRENDING, PULLBACK, BREAKOUT, RANGE, CHOP, LIQUIDITY_EVENT.
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
import numpy as np
import pandas as pd
import logging

logger = logging.getLogger("JARVIS_MicrostructureEngine")


class MicrostructureState(str, Enum):
    TRENDING = "TRENDING"
    PULLBACK = "PULLBACK"
    BREAKOUT = "BREAKOUT"
    RANGE = "RANGE"
    CHOP = "CHOP"
    LIQUIDITY_EVENT = "LIQUIDITY_EVENT"


@dataclass
class MicrostructureMetrics:
    state: MicrostructureState = MicrostructureState.RANGE
    is_chop: bool = False
    spread_expansion_ratio: float = 1.0
    tick_velocity: float = 0.0
    displacement_ratio: float = 0.0  # Body / Total Range of most recent impulse candle
    vwap: float = 0.0
    vwap_distance_atr: float = 0.0   # Signed distance from VWAP in ATR units
    volume_imbalance_ratio: float = 0.0  # -1.0 (heavy sell) to +1.0 (heavy buy)
    short_term_volatility_state: str = "NORMAL"  # "COMPRESSION", "NORMAL", "EXPANSION", "EXTREME"
    sweep_detected: bool = False
    sweep_side: str = "NONE"  # "BUY_SIDE", "SELL_SIDE", "NONE"
    absorption_detected: bool = False
    confidence: float = 0.50
    reasons: List[str] = field(default_factory=list)


class MicrostructureEngine:
    """
    Dedicated Microstructure Physics Engine for high-precision M1/M5 scalping.
    Evaluates micro market dynamics to prevent scalping in low-edge or chaotic regimes.
    """

    def __init__(self, displacement_threshold: float = 0.50, max_spread_expansion: float = 1.8):
        self.displacement_threshold = displacement_threshold
        self.max_spread_expansion = max_spread_expansion

    def calculate_session_vwap(self, df: pd.DataFrame) -> float:
        """Calculates rolling intraday session VWAP."""
        if df.empty or len(df) < 5:
            return 0.0
        try:
            typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
            volume = df["volume"] if "volume" in df.columns and df["volume"].sum() > 0 else pd.Series(1.0, index=df.index)
            cum_vol = volume.cumsum()
            cum_pv = (typical_price * volume).cumsum()
            vwap_series = cum_pv / (cum_vol + 1e-9)
            return float(vwap_series.iloc[-1])
        except Exception:
            return float(df["close"].iloc[-1])

    def analyze(
        self,
        df_m1: Optional[pd.DataFrame] = None,
        df_m5: Optional[pd.DataFrame] = None,
        current_spread_pips: float = 1.5,
        typical_spread_pips: float = 1.5,
        order_flow: Optional[Dict[str, Any]] = None
    ) -> MicrostructureMetrics:
        """
        Analyzes M1/M5 microstructure and classifies regime.
        """
        df = df_m1 if (df_m1 is not None and not df_m1.empty and len(df_m1) >= 10) else df_m5
        if df is None or df.empty or len(df) < 10:
            return MicrostructureMetrics(
                state=MicrostructureState.CHOP,
                is_chop=True,
                reasons=["Insufficient M1/M5 bars to construct microstructure."]
            )

        reasons = []
        close = df["close"]
        high = df["high"]
        low = df["low"]
        open_ = df["open"]
        c_price = float(close.iloc[-1])

        # 1. Spread Expansion Ratio
        typ_spr = max(typical_spread_pips, 0.1)
        spread_ratio = round(current_spread_pips / typ_spr, 2)
        is_spread_expanded = spread_ratio > self.max_spread_expansion
        if is_spread_expanded:
            reasons.append(f"Spread expanded {spread_ratio:.1f}x above normal.")

        # 2. Tick Velocity & Short-term ATR
        tr = np.maximum(high - low, np.maximum(np.abs(high - close.shift(1)), np.abs(low - close.shift(1))))
        atr_14 = float(tr.rolling(14, min_periods=5).mean().iloc[-1]) or (c_price * 0.001)
        recent_ranges = (high.iloc[-5:] - low.iloc[-5:]).values
        avg_recent_range = float(np.mean(recent_ranges))
        tick_velocity = round(avg_recent_range / (atr_14 + 1e-9), 2)

        # 3. Candle Body Displacement (Impulse quality)
        recent_bodies = np.abs(close.iloc[-3:].values - open_.iloc[-3:].values)
        recent_spans = np.maximum(high.iloc[-3:].values - low.iloc[-3:].values, 1e-9)
        displacement_ratio = float(np.max(recent_bodies / recent_spans))
        has_displacement = displacement_ratio >= self.displacement_threshold

        # 4. Session VWAP & Distance
        vwap = self.calculate_session_vwap(df)
        vwap_dist_atr = round((c_price - vwap) / (atr_14 + 1e-9), 2) if vwap > 0 else 0.0

        # 5. Liquidity Sweep & Absorption Detection
        sweep_detected = False
        sweep_side = "NONE"
        absorption_detected = False

        prev_5_high = float(high.iloc[-10:-1].max()) if len(df) >= 10 else float(high.max())
        prev_5_low = float(low.iloc[-10:-1].min()) if len(df) >= 10 else float(low.min())
        curr_high = float(high.iloc[-1])
        curr_low = float(low.iloc[-1])
        curr_close = float(close.iloc[-1])

        # Bullish sweep: Price swept below previous low but closed back above it (liquidity hunt)
        if curr_low < prev_5_low and curr_close > prev_5_low:
            sweep_detected = True
            sweep_side = "SELL_SIDE"
            absorption_detected = True
            reasons.append("Sell-Side Liquidity Swept below micro swing low with immediate absorption.")
        # Bearish sweep: Price swept above previous high but closed back below it
        elif curr_high > prev_5_high and curr_close < prev_5_high:
            sweep_detected = True
            sweep_side = "BUY_SIDE"
            absorption_detected = True
            reasons.append("Buy-Side Liquidity Swept above micro swing high with immediate rejection.")

        # 6. Order Flow Imbalance
        vol_imbalance = 0.0
        if order_flow and isinstance(order_flow, dict):
            delta = float(order_flow.get("delta_score", 0.0))
            vol_imbalance = float(np.clip(delta / 100.0, -1.0, 1.0))
            if order_flow.get("absorption_trap"):
                absorption_detected = True
                reasons.append(f"Absorption footprint detected: {order_flow.get('absorption_trap')}")

        # 7. Short-Term Volatility State
        if avg_recent_range < atr_14 * 0.5:
            vol_state = "COMPRESSION"
        elif avg_recent_range > atr_14 * 2.2:
            vol_state = "EXTREME"
        elif avg_recent_range > atr_14 * 1.3:
            vol_state = "EXPANSION"
        else:
            vol_state = "NORMAL"

        # 8. Detect Microstructure Regime
        # Check for CHOP (whipsawing, overlapping candles, no displacement, alternating directions)
        consecutive_alternations = 0
        diffs = np.sign(close.diff().dropna().iloc[-6:].values)
        for i in range(1, len(diffs)):
            if diffs[i] != diffs[i - 1]:
                consecutive_alternations += 1

        is_choppy_structure = (consecutive_alternations >= 4 and not has_displacement)
        is_chop = bool(is_choppy_structure or is_spread_expanded or vol_state == "EXTREME")

        if sweep_detected and absorption_detected:
            state = MicrostructureState.LIQUIDITY_EVENT
            conf = 0.85
        elif is_chop:
            state = MicrostructureState.CHOP
            conf = 0.80
            if is_choppy_structure:
                reasons.append("Microstructure is CHOPPY (frequent alternations with zero displacement).")
        elif vol_state == "EXPANSION" and has_displacement:
            state = MicrostructureState.BREAKOUT
            conf = 0.82
            reasons.append("Microstructure in BREAKOUT EXPANSION with strong displacement candle.")
        elif abs(vwap_dist_atr) >= 0.8 and not has_displacement:
            state = MicrostructureState.PULLBACK
            conf = 0.75
            reasons.append("Microstructure in PULLBACK retesting VWAP / equilibrium.")
        elif abs(vwap_dist_atr) < 0.5 and vol_state in ("COMPRESSION", "NORMAL"):
            state = MicrostructureState.RANGE
            conf = 0.70
        else:
            state = MicrostructureState.TRENDING
            conf = 0.78
            reasons.append("Microstructure is TRENDING with directional continuity.")

        return MicrostructureMetrics(
            state=state,
            is_chop=is_chop,
            spread_expansion_ratio=spread_ratio,
            tick_velocity=tick_velocity,
            displacement_ratio=round(displacement_ratio, 2),
            vwap=round(vwap, 5),
            vwap_distance_atr=vwap_dist_atr,
            volume_imbalance_ratio=round(vol_imbalance, 2),
            short_term_volatility_state=vol_state,
            sweep_detected=sweep_detected,
            sweep_side=sweep_side,
            absorption_detected=absorption_detected,
            confidence=round(conf, 2),
            reasons=reasons
        )
