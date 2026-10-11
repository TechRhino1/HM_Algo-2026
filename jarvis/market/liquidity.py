"""
HM Algo 2.0 — Liquidity & Order Flow Sweep Intelligence Engine.
Identifies Equal Highs/Lows, Buy-Side/Sell-Side Liquidity Pools, Liquidity Sweeps, and Stop-Run Traps.
"""
import numpy as np
import pandas as pd
from jarvis.data.schemas import LiquidityContext

class LiquidityEngine:
    def __init__(self, eq_threshold_pct: float = 0.15):
        self.eq_threshold_pct = eq_threshold_pct

    def analyze_liquidity(
        self,
        df: pd.DataFrame,
        pivot_window: int = 5
    ) -> LiquidityContext:
        if len(df) < pivot_window * 2 + 5:
            return LiquidityContext()

        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        
        # Identify swing points (filtering consecutive duplicate levels)
        swing_highs = []
        swing_lows = []
        for i in range(pivot_window, len(df) - pivot_window):
            if highs[i] == max(highs[i - pivot_window:i + pivot_window + 1]):
                if not swing_highs or swing_highs[-1][1] != float(highs[i]):
                    swing_highs.append((i, float(highs[i])))
            if lows[i] == min(lows[i - pivot_window:i + pivot_window + 1]):
                if not swing_lows or swing_lows[-1][1] != float(lows[i]):
                    swing_lows.append((i, float(lows[i])))

        if not swing_highs or not swing_lows:
            return LiquidityContext()

        recent_sh = swing_highs[-1][1]
        recent_sl = swing_lows[-1][1]
        
        # Equal Highs / Lows check
        eq_highs = False
        if len(swing_highs) >= 2:
            diff_h = abs(swing_highs[-1][1] - swing_highs[-2][1]) / (recent_sh + 1e-9) * 100.0
            eq_highs = diff_h <= self.eq_threshold_pct

        eq_lows = False
        if len(swing_lows) >= 2:
            diff_l = abs(swing_lows[-1][1] - swing_lows[-2][1]) / (recent_sl + 1e-9) * 100.0
            eq_lows = diff_l <= self.eq_threshold_pct

        # Quick ATR 14 for sweep magnitude
        if len(df) >= 15:
            prev_closes = closes[-15:-1]
            curr_highs = highs[-14:]
            curr_lows = lows[-14:]
            tr1 = curr_highs - curr_lows
            tr2 = np.abs(curr_highs - prev_closes)
            tr3 = np.abs(curr_lows - prev_closes)
            tr = np.maximum(tr1, np.maximum(tr2, tr3))
            atr = float(np.mean(tr))
        else:
            atr = float(highs[-1] - lows[-1]) or 1e-9

        # Liquidity Sweep Detection on latest 2 completed candles with displacement & volume validation:
        # - Displacement / Rejection validation: Decisive body (>= 38%) OR institutional wick rejection (>= 45%)
        # - Volume alignment: Participation >= 35% of recent 14-period average volume
        # - Bullish Sweep: Candle low breaks below swing low, closes back above swing low
        # - Bearish Sweep: Candle high breaks above swing high, closes back below swing high
        sweep_detected = False
        sweep_type = "NONE"
        sweep_level = 0.0
        sweep_magnitude = 0.0
        bars_since_sweep = -1

        latest_high = float(highs[-1])
        latest_low = float(lows[-1])
        opens = df["open"].values if "open" in df.columns else closes

        sl_levels = []
        for s in reversed(swing_lows[-5:]):
            p = s[1]
            if not any(abs(p - x) / max(p, 1e-9) < 0.0005 for x in sl_levels):
                sl_levels.append(p)

        sh_levels = []
        for s in reversed(swing_highs[-5:]):
            p = s[1]
            if not any(abs(p - x) / max(p, 1e-9) < 0.0005 for x in sh_levels):
                sh_levels.append(p)

        lookback_bars = min(10, len(highs))
        for offset in range(1, lookback_bars + 1):
            idx = -offset
            c_high = float(highs[idx])
            c_low = float(lows[idx])
            c_close = float(closes[idx])
            c_open = float(opens[idx])

            c_range = max(c_high - c_low, 1e-9)
            body_size = abs(c_close - c_open)
            body_pct = body_size / c_range
            lower_wick_pct = (min(c_close, c_open) - c_low) / c_range
            upper_wick_pct = (c_high - max(c_close, c_open)) / c_range

            has_displacement = (body_pct >= 0.38) or (lower_wick_pct >= 0.45)
            has_bear_displacement = (body_pct >= 0.38) or (upper_wick_pct >= 0.45)

            vol_aligned = True
            if "volume" in df.columns or "tick_volume" in df.columns:
                v_col = "volume" if "volume" in df.columns else "tick_volume"
                c_vol = float(df[v_col].iloc[idx]) if abs(idx) <= len(df) else 0.0
                avg_vol = float(df[v_col].iloc[-14:].mean()) if len(df) >= 14 else c_vol
                if avg_vol > 0 and c_vol < (avg_vol * 0.35):
                    vol_aligned = False

            for sl_cand in sl_levels:
                mag_low = abs(c_low - sl_cand) / atr
                if (c_low < sl_cand and c_close > sl_cand and c_close >= c_open - (body_size * 0.15)
                        and (0.10 <= mag_low <= 4.0) and has_displacement and vol_aligned):
                    sweep_detected = True
                    sweep_type = "BULLISH_SWEEP"
                    sweep_level = sl_cand
                    sweep_magnitude = mag_low
                    bars_since_sweep = offset - 1
                    break
            if sweep_detected:
                break

            for sh_cand in sh_levels:
                mag_high = abs(c_high - sh_cand) / atr
                if (c_high > sh_cand and c_close < sh_cand and c_close <= c_open + (body_size * 0.15)
                        and (0.10 <= mag_high <= 4.0) and has_bear_displacement and vol_aligned):
                    sweep_detected = True
                    sweep_type = "BEARISH_SWEEP"
                    sweep_level = sh_cand
                    sweep_magnitude = mag_high
                    bars_since_sweep = offset - 1
                    break
            if sweep_detected:
                break

        pools = [
            {"type": "BUY_SIDE_LIQUIDITY", "price": round(recent_sh, 4), "status": "UNSWEPT" if not (latest_high > recent_sh) else "SWEPT"},
            {"type": "SELL_SIDE_LIQUIDITY", "price": round(recent_sl, 4), "status": "UNSWEPT" if not (latest_low < recent_sl) else "SWEPT"}
        ]

        return LiquidityContext(
            equal_highs=eq_highs,
            equal_lows=eq_lows,
            sweep_detected=sweep_detected,
            sweep_type=sweep_type,
            sweep_level=round(sweep_level, 4),
            sweep_magnitude=round(sweep_magnitude, 4),
            liquidity_pools=pools,
            buy_side_liquidity=round(recent_sh, 4),
            sell_side_liquidity=round(recent_sl, 4),
            bars_since_sweep=bars_since_sweep
        )
