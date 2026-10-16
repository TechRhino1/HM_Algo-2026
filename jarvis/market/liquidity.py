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
        pivot_window: int = 5,
        lookback_bars: int = 2,
        min_body_pct: float = 0.45,
        min_vol_ratio: float = 0.40,
        min_mag: float = 0.15,
        max_mag: float = 3.5,
        include_session_levels: bool = False,
        allow_wick_rejection: bool = False
    ) -> LiquidityContext:
        if len(df) < pivot_window * 2 + 5:
            return LiquidityContext()

        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        
        # Identify swing points
        swing_highs = []
        swing_lows = []
        for i in range(pivot_window, len(df) - pivot_window):
            if highs[i] == max(highs[i - pivot_window:i + pivot_window + 1]):
                swing_highs.append((i, float(highs[i])))
            if lows[i] == min(lows[i - pivot_window:i + pivot_window + 1]):
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

        sweep_detected = False
        sweep_type = "NONE"
        sweep_level = 0.0
        sweep_magnitude = 0.0
        bars_since_sweep = -1

        latest_high = float(highs[-1])
        latest_low = float(lows[-1])
        opens = df["open"].values if "open" in df.columns else closes

        sl_levels = [recent_sl]
        sh_levels = [recent_sh]

        # Institutional Session Liquidity Pools (PDH/PDL and Asian Session Range)
        if include_session_levels and "time" in df.columns and len(df) >= 24:
            try:
                times = pd.to_datetime(df["time"], utc=True)
                latest_date = times.iloc[-1].date()
                prev_day_mask = times.dt.date < latest_date
                if prev_day_mask.any():
                    last_prev_date = times[prev_day_mask].iloc[-1].date()
                    day_df = df[times.dt.date == last_prev_date]
                    if len(day_df) >= 4:
                        pdh = float(day_df["high"].max())
                        pdl = float(day_df["low"].min())
                        if not any(abs(pdh - x) / max(pdh, 1e-9) < 0.0005 for x in sh_levels):
                            sh_levels.append(pdh)
                        if not any(abs(pdl - x) / max(pdl, 1e-9) < 0.0005 for x in sl_levels):
                            sl_levels.append(pdl)

                asia_mask = (times.dt.hour >= 0) & (times.dt.hour < 7)
                if asia_mask.any():
                    asia_df = df[asia_mask]
                    if len(asia_df) >= 2:
                        asia_high = float(asia_df["high"].iloc[-24:].max())
                        asia_low = float(asia_df["low"].iloc[-24:].min())
                        if not any(abs(asia_high - x) / max(asia_high, 1e-9) < 0.0005 for x in sh_levels):
                            sh_levels.append(asia_high)
                        if not any(abs(asia_low - x) / max(asia_low, 1e-9) < 0.0005 for x in sl_levels):
                            sl_levels.append(asia_low)
            except Exception:
                pass

        scan_bars = min(lookback_bars, len(highs))
        for offset in range(1, scan_bars + 1):
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

            if allow_wick_rejection:
                has_displacement = (body_pct >= min_body_pct) or (lower_wick_pct >= min_body_pct)
                has_bear_displacement = (body_pct >= min_body_pct) or (upper_wick_pct >= min_body_pct)
            else:
                has_displacement = body_pct >= min_body_pct
                has_bear_displacement = body_pct >= min_body_pct

            vol_aligned = True
            if "volume" in df.columns or "tick_volume" in df.columns:
                v_col = "volume" if "volume" in df.columns else "tick_volume"
                c_vol = float(df[v_col].iloc[idx]) if abs(idx) <= len(df) else 0.0
                avg_vol = float(df[v_col].iloc[-14:].mean()) if len(df) >= 14 else c_vol
                if avg_vol > 0 and c_vol < (avg_vol * min_vol_ratio):
                    vol_aligned = False

            for sl_cand in sl_levels:
                mag_low = abs(c_low - sl_cand) / atr
                if (c_low < sl_cand and c_close > sl_cand and c_close >= c_open
                        and (min_mag <= mag_low <= max_mag) and has_displacement and vol_aligned):
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
                if (c_high > sh_cand and c_close < sh_cand and c_close <= c_open
                        and (min_mag <= mag_high <= max_mag) and has_bear_displacement and vol_aligned):
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
