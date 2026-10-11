"""
HM Algo 2.0 — Institutional Candlestick, Price Action & Volumetric Engine.
Detects institutional candlestick patterns, Support & Resistance confluence,
Fibonacci retracements, psychological round numbers, and Volume Spread Analysis (VSA).
Strictly aligned with the institutional advanced-trading skill (§3 Price Action, §4 SMC/ICT, §4.2 Order Flow).
"""
import math
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
import pandas as pd


class CandlestickPriceActionEngine:
    """
    Advanced Quantitative Candlestick & Volumetric Price Action Engine.
    Identifies high-probability reversal, continuation, and absorption patterns
    validated by volume and support/resistance geometry.
    """

    def __init__(self, atr_period: int = 14, vol_period: int = 14):
        self.atr_period = atr_period
        self.vol_period = vol_period

    def analyze(self, df: pd.DataFrame, symbol: str = "") -> Dict[str, Any]:
        """
        Analyzes multi-bar price action, candlestick structure, support/resistance,
        and volumetric order flow.
        """
        default_res = {
            "pattern_detected": False,
            "pattern_name": "NONE",
            "bias": "NEUTRAL",
            "confidence": 0.0,
            "at_support_resistance": False,
            "key_level_type": "NONE",
            "nearest_level": 0.0,
            "volume_confirmed": False,
            "volume_ratio": 1.0,
            "wyckoff_vsa_signal": "NONE",
            "bars_ago": 0,
            "sr_levels": [],
            "patterns_found": []
        }

        if df is None or len(df) < 15:
            return default_res

        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        closes = df["close"].values.astype(float)
        opens = df["open"].values.astype(float) if "open" in df.columns else closes

        n = len(df)
        curr_close = float(closes[-1])

        # 1. Volumetric calculations
        has_vol = "volume" in df.columns or "tick_volume" in df.columns
        v_col = "volume" if "volume" in df.columns else ("tick_volume" if "tick_volume" in df.columns else None)
        vols = df[v_col].values.astype(float) if v_col else np.ones(n)
        
        avg_vol = float(np.mean(vols[-self.vol_period:])) if n >= self.vol_period else float(np.mean(vols))
        avg_vol = max(avg_vol, 1.0)
        curr_vol = float(vols[-1])
        vol_ratio = curr_vol / avg_vol

        # 2. True Range & ATR
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
        )
        atr = float(np.mean(tr[-self.atr_period:])) if len(tr) >= self.atr_period else float(np.mean(tr))
        atr = max(atr, 1e-6)

        # 3. Dynamic Support & Resistance Levels
        sr_levels = self._calculate_sr_levels(df, curr_close, symbol)

        # Check proximity to S&R (within 0.5% or 0.6 ATR)
        sr_tolerance = max(curr_close * 0.005, atr * 0.65)
        near_support = False
        near_resistance = False
        nearest_level = 0.0
        nearest_type = "NONE"
        min_dist = float("inf")

        for lvl in sr_levels:
            p = lvl["price"]
            dist = abs(curr_close - p)
            if dist < min_dist:
                min_dist = dist
                nearest_level = p
                nearest_type = lvl["type"]
            if dist <= sr_tolerance:
                if p <= curr_close:
                    near_support = True
                else:
                    near_resistance = True

        at_sr = near_support or near_resistance

        # 4. Candlestick Pattern Discovery (Scan last 8 bars for freshest active pattern)
        # Institutional moves establish over 1 to 8 bars following a trigger
        patterns_found = []
        lookback = min(8, n - 2)

        for offset in range(lookback, 0, -1):
            idx = n - offset
            c_open = float(opens[idx])
            c_high = float(highs[idx])
            c_low = float(lows[idx])
            c_close = float(closes[idx])
            c_vol = float(vols[idx]) if has_vol else avg_vol
            c_vol_ratio = c_vol / avg_vol
            c_range = max(c_high - c_low, 1e-9)
            body = abs(c_close - c_open)
            upper_wick = c_high - max(c_close, c_open)
            lower_wick = min(c_close, c_open) - c_low

            # Candle i-1 metrics for multi-candle patterns
            prev_open = float(opens[idx - 1])
            prev_high = float(highs[idx - 1])
            prev_low = float(lows[idx - 1])
            prev_close = float(closes[idx - 1])
            prev_body = abs(prev_close - prev_open)
            prev_range = max(prev_high - prev_low, 1e-9)

            # Pattern A: Bullish Pin Bar / Hammer (Rejection of lows)
            # Long lower wick (>= 50% of range), small body (<= 35%), close in upper 40%
            if (lower_wick >= c_range * 0.50 and
                body <= c_range * 0.35 and
                upper_wick <= c_range * 0.25 and
                c_range >= atr * 0.40):
                conf = 0.65
                if c_vol_ratio >= 1.2: conf += 0.10
                if near_support: conf += 0.15
                patterns_found.append({
                    "name": "PIN_BAR_HAMMER",
                    "bias": "BUY",
                    "confidence": min(0.95, conf),
                    "bars_ago": offset - 1,
                    "vol_ratio": round(c_vol_ratio, 2),
                    "price": c_low
                })

            # Pattern B: Bearish Shooting Star / Pin Bar (Rejection of highs)
            # Long upper wick (>= 50% of range), small body (<= 35%), close in lower 40%
            elif (upper_wick >= c_range * 0.50 and
                  body <= c_range * 0.35 and
                  lower_wick <= c_range * 0.25 and
                  c_range >= atr * 0.40):
                conf = 0.65
                if c_vol_ratio >= 1.2: conf += 0.10
                if near_resistance: conf += 0.15
                patterns_found.append({
                    "name": "SHOOTING_STAR",
                    "bias": "SELL",
                    "confidence": min(0.95, conf),
                    "bars_ago": offset - 1,
                    "vol_ratio": round(c_vol_ratio, 2),
                    "price": c_high
                })

            # Pattern C: Bullish Engulfing
            # Bullish candle engulfs previous bearish body, volume expansion
            elif (c_close > c_open and
                  prev_close < prev_open and
                  c_open <= prev_close + (prev_body * 0.15) and
                  c_close >= prev_open and
                  body >= prev_body * 1.1 and
                  c_range >= atr * 0.50):
                conf = 0.70
                if c_vol_ratio >= 1.2: conf += 0.15
                if near_support: conf += 0.10
                patterns_found.append({
                    "name": "BULLISH_ENGULFING",
                    "bias": "BUY",
                    "confidence": min(0.95, conf),
                    "bars_ago": offset - 1,
                    "vol_ratio": round(c_vol_ratio, 2),
                    "price": c_low
                })

            # Pattern D: Bearish Engulfing
            elif (c_close < c_open and
                  prev_close > prev_open and
                  c_open >= prev_close - (prev_body * 0.15) and
                  c_close <= prev_open and
                  body >= prev_body * 1.1 and
                  c_range >= atr * 0.50):
                conf = 0.70
                if c_vol_ratio >= 1.2: conf += 0.15
                if near_resistance: conf += 0.10
                patterns_found.append({
                    "name": "BEARISH_ENGULFING",
                    "bias": "SELL",
                    "confidence": min(0.95, conf),
                    "bars_ago": offset - 1,
                    "vol_ratio": round(c_vol_ratio, 2),
                    "price": c_high
                })

            # Pattern E: Bullish Expansion / Marubozu Breakout Candle
            # Decisive full-body institutional displacement (body >= 65% of range, range >= 1.2 ATR, volume >= 1.4x)
            elif (c_close > c_open and
                  body >= c_range * 0.65 and
                  c_range >= atr * 1.10 and
                  c_vol_ratio >= 1.30):
                conf = 0.75
                if c_vol_ratio >= 2.0: conf += 0.15
                patterns_found.append({
                    "name": "BULLISH_EXPANSION_MARUBOZU",
                    "bias": "BUY",
                    "confidence": min(0.95, conf),
                    "bars_ago": offset - 1,
                    "vol_ratio": round(c_vol_ratio, 2),
                    "price": c_low
                })

            # Pattern F: Bearish Expansion / Marubozu Breakdown Candle
            elif (c_close < c_open and
                  body >= c_range * 0.65 and
                  c_range >= atr * 1.10 and
                  c_vol_ratio >= 1.30):
                conf = 0.75
                if c_vol_ratio >= 2.0: conf += 0.15
                patterns_found.append({
                    "name": "BEARISH_EXPANSION_MARUBOZU",
                    "bias": "SELL",
                    "confidence": min(0.95, conf),
                    "bars_ago": offset - 1,
                    "vol_ratio": round(c_vol_ratio, 2),
                    "price": c_high
                })

        # Pattern G: Multi-Bar Wick Absorption (Cluster of rejection wicks at key level)
        if len(df) >= 3:
            recent_lw = [min(closes[i], opens[i]) - lows[i] for i in [-1, -2, -3]]
            recent_ranges = [max(highs[i] - lows[i], 1e-9) for i in [-1, -2, -3]]
            lw_pcts = [recent_lw[i] / recent_ranges[i] for i in range(3)]
            if sum(1 for p in lw_pcts if p >= 0.45) >= 2 and near_support:
                patterns_found.append({
                    "name": "BULLISH_WICK_ABSORPTION",
                    "bias": "BUY",
                    "confidence": 0.80,
                    "bars_ago": 0,
                    "vol_ratio": round(vol_ratio, 2),
                    "price": float(lows[-3:].min())
                })

            recent_uw = [highs[i] - max(closes[i], opens[i]) for i in [-1, -2, -3]]
            uw_pcts = [recent_uw[i] / recent_ranges[i] for i in range(3)]
            if sum(1 for p in uw_pcts if p >= 0.45) >= 2 and near_resistance:
                patterns_found.append({
                    "name": "BEARISH_WICK_ABSORPTION",
                    "bias": "SELL",
                    "confidence": 0.80,
                    "bars_ago": 0,
                    "vol_ratio": round(vol_ratio, 2),
                    "price": float(highs[-3:].max())
                })

        # 5. Wyckoff Volume Spread Analysis (VSA)
        wyckoff_signal = "NONE"
        latest_range = highs[-1] - lows[-1]
        if vol_ratio >= 2.0 and latest_range >= atr * 1.2:
            wyckoff_signal = "INSTITUTIONAL_MARKUP" if closes[-1] > opens[-1] else "INSTITUTIONAL_MARKDOWN"
        elif vol_ratio >= 1.8 and latest_range <= atr * 0.65:
            # High volume with narrow spread = absorption / stopping volume
            wyckoff_signal = "STOPPING_VOLUME_ABSORPTION"

        # 6. Select Primary Active Pattern
        primary_pattern = None
        if patterns_found:
            # Sort by recency (smallest bars_ago) and then highest confidence
            patterns_found.sort(key=lambda x: (x["bars_ago"], -x["confidence"]))
            primary_pattern = patterns_found[0]

        return {
            "pattern_detected": primary_pattern is not None,
            "pattern_name": primary_pattern["name"] if primary_pattern else "NONE",
            "bias": primary_pattern["bias"] if primary_pattern else "NEUTRAL",
            "confidence": primary_pattern["confidence"] if primary_pattern else 0.0,
            "at_support_resistance": at_sr,
            "key_level_type": nearest_type,
            "nearest_level": round(nearest_level, 4),
            "volume_confirmed": vol_ratio >= 1.25 or (primary_pattern and primary_pattern["vol_ratio"] >= 1.25),
            "volume_ratio": round(vol_ratio, 2),
            "wyckoff_vsa_signal": wyckoff_signal,
            "bars_ago": primary_pattern["bars_ago"] if primary_pattern else 0,
            "sr_levels": sr_levels[:6],
            "patterns_found": patterns_found
        }

    def _calculate_sr_levels(self, df: pd.DataFrame, curr_price: float, symbol: str) -> List[Dict[str, Any]]:
        """
        Computes multi-tier Support and Resistance levels:
        1. Swing pivot highs and lows
        2. Psychological round numbers
        3. Fibonacci retracement levels from macro swing span
        """
        levels: List[Dict[str, Any]] = []
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        n = len(df)

        # 1. Swing Extremes
        macro_high = float(highs[-100:].max()) if n >= 100 else float(highs.max())
        macro_low = float(lows[-100:].min()) if n >= 100 else float(lows.min())
        span = max(macro_high - macro_low, 1e-9)

        levels.append({"price": round(macro_high, 4), "type": "MACRO_SWING_HIGH"})
        levels.append({"price": round(macro_low, 4), "type": "MACRO_SWING_LOW"})

        # Recent Swings (5-bar window)
        for i in range(max(5, n - 40), n - 3):
            if highs[i] == max(highs[i - 3:i + 4]):
                levels.append({"price": round(float(highs[i]), 4), "type": "SWING_HIGH"})
            if lows[i] == min(lows[i - 3:i + 4]):
                levels.append({"price": round(float(lows[i]), 4), "type": "SWING_LOW"})

        # 2. Fibonacci Retracements
        fib_ratios = [0.382, 0.500, 0.618, 0.786]
        for r in fib_ratios:
            fib_lvl = macro_low + span * r
            levels.append({"price": round(fib_lvl, 4), "type": f"FIBONACCI_{int(r*100)}"})

        # 3. Psychological Big Figures / Round Numbers
        # Round number interval depending on instrument price
        if curr_price >= 10000:
            step = 500.0  # BTC, US30
        elif curr_price >= 1000:
            step = 50.0   # ETH, Gold, SPX
        elif curr_price >= 100:
            step = 10.0   # Oil, JPY pairs
        elif curr_price >= 10:
            step = 1.0    # Silver, SOL
        else:
            step = 0.0050 # Forex majors (50 pips)

        base_round = math.floor(curr_price / step) * step
        for mult in [-2, -1, 0, 1, 2]:
            rnd_p = base_round + mult * step
            if rnd_p > 0:
                levels.append({"price": round(rnd_p, 4), "type": "PSYCHOLOGICAL_ROUND_NUMBER"})

        # Remove duplicates within 0.1% of each other, prioritizing swing levels
        unique_levels = []
        for lvl in levels:
            p = lvl["price"]
            if not any(abs(p - u["price"]) / max(p, 1e-9) < 0.0015 for u in unique_levels):
                unique_levels.append(lvl)

        return unique_levels
