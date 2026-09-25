"""
HM Algo 2.0 — Advanced Institutional Market Structure Engine.
Detects Swing Pivots (HH, HL, LH, LL), BOS, CHoCH, Order Blocks, Fair Value Gaps (FVG), and Premium/Discount Zones.
"""
import numpy as np
import pandas as pd
from jarvis.data.schemas import StructureContext

class MarketStructureEngine:
    def __init__(self, pivot_window: int = 5, adaptive_window: bool = True):
        self.pivot_window = pivot_window
        self.adaptive_window = adaptive_window

    def analyze_structure(self, df: pd.DataFrame) -> StructureContext:
        if len(df) < self.pivot_window * 2 + 3:
            return StructureContext(bias="NEUTRAL")

        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        opens = df["open"].values
        times = df["time"].values if "time" in df else list(range(len(df)))

        # Adaptive Volatility Lookback Scaling (Trend Channel Navigator adaptation)
        # Scales pivot scan window with ATR14 vs ATR100 volatility regime
        w = self.pivot_window
        if self.adaptive_window and len(df) >= 30:
            tr = np.maximum(
                highs[1:] - lows[1:],
                np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
            )
            atr14 = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
            atr_long_len = min(100, len(tr))
            atr_long = float(np.mean(tr[-atr_long_len:])) if atr_long_len > 0 else atr14
            if atr_long > 1e-9:
                vol_ratio = float(np.clip(atr14 / atr_long, 0.6, 1.8))
                w = int(np.clip(round(self.pivot_window * vol_ratio), 3, 12))
        
        # Ensure sufficient bars exist for window w
        if len(df) < w * 2 + 3:
            w = max(2, (len(df) - 3) // 2)

        swing_highs = []
        swing_lows = []

        # Identify swing pivots (deterministic discovery)
        for i in range(w, len(df) - w):
            window_high = max(highs[i - w:i + w + 1])
            window_low = min(lows[i - w:i + w + 1])
            if highs[i] >= window_high and (not swing_highs or swing_highs[-1]["index"] != i):
                swing_highs.append({"index": i, "price": float(highs[i]), "time": times[i]})
            if lows[i] <= window_low and (not swing_lows or swing_lows[-1]["index"] != i):
                swing_lows.append({"index": i, "price": float(lows[i]), "time": times[i]})

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return StructureContext(bias="NEUTRAL", adaptive_pivot_window=w)

        recent_sh = swing_highs[-1]["price"]
        prev_sh = swing_highs[-2]["price"]
        recent_sl = swing_lows[-1]["price"]
        prev_sl = swing_lows[-2]["price"]
        latest_close = float(closes[-1])

        hh = recent_sh > prev_sh
        hl = recent_sl > prev_sl
        lh = recent_sh < prev_sh
        ll = recent_sl < prev_sl

        bos_bullish = latest_close > recent_sh
        bos_bearish = latest_close < recent_sl
        choch_bullish = (lh and ll) and (latest_close > recent_sh)
        choch_bearish = (hh and hl) and (latest_close < recent_sl)

        bias = "NEUTRAL"
        if hh and hl:
            bias = "BULLISH"
        elif lh and ll:
            bias = "BEARISH"
        elif bos_bullish or choch_bullish:
            bias = "BULLISH"
        elif bos_bearish or choch_bearish:
            bias = "BEARISH"

        # Premium / Discount / Equilibrium Zones
        recent_max = float(highs[-200:].max()) if len(highs) >= 200 else float(highs.max())
        recent_min = float(lows[-200:].min()) if len(lows) >= 200 else float(lows.min())
        equilibrium = (recent_max + recent_min) / 2.0
        range_span = recent_max - recent_min + 1e-9
        position_pct = ((latest_close - recent_min) / range_span) * 100.0

        if position_pct <= 45.0:
            discount_premium_zone = "DISCOUNT"
        elif position_pct >= 55.0:
            discount_premium_zone = "PREMIUM"
        else:
            discount_premium_zone = "EQUILIBRIUM"

        # Supply / Demand Zones
        demand_zone = (round(recent_sl, 4), round(recent_sl * 1.003, 4))
        supply_zone = (round(recent_sh * 0.997, 4), round(recent_sh, 4))

        # Detect Institutional Order Blocks with displacement validation (body >= 45%)
        order_blocks = []
        for i in range(max(2, len(df) - 15), len(df) - 1):
            range_next = max(highs[i + 1] - lows[i + 1], 1e-9)
            # Bullish OB: Bearish candle followed by strong bullish breakout with >= 45% displacement
            if closes[i] < opens[i] and closes[i + 1] > highs[i]:
                body_disp = (closes[i + 1] - opens[i + 1]) / range_next
                if body_disp >= 0.45:
                    order_blocks.append({
                        "type": "BULLISH_ORDER_BLOCK",
                        "high": round(float(highs[i]), 4),
                        "low": round(float(lows[i]), 4),
                        "mid": round(float((highs[i] + lows[i]) / 2.0), 4),
                        "index": i
                    })
            # Bearish OB: Bullish candle followed by strong bearish breakdown with >= 45% displacement
            elif closes[i] > opens[i] and closes[i + 1] < lows[i]:
                body_disp = (opens[i + 1] - closes[i + 1]) / range_next
                if body_disp >= 0.45:
                    order_blocks.append({
                        "type": "BEARISH_ORDER_BLOCK",
                        "high": round(float(highs[i]), 4),
                        "low": round(float(lows[i]), 4),
                        "mid": round(float((highs[i] + lows[i]) / 2.0), 4),
                        "index": i
                    })

        # Detect Fair Value Gaps (FVG)
        fair_value_gaps = []
        for i in range(max(2, len(df) - 20), len(df)):
            # Bullish FVG: Low of candle i > High of candle i-2
            if lows[i] > highs[i - 2]:
                fair_value_gaps.append({
                    "type": "BULLISH_FVG",
                    "top": round(float(lows[i]), 4),
                    "bottom": round(float(highs[i - 2]), 4),
                    "size": round(float(lows[i] - highs[i - 2]), 4),
                    "index": i
                })
            # Bearish FVG: High of candle i < Low of candle i-2
            elif highs[i] < lows[i - 2]:
                fair_value_gaps.append({
                    "type": "BEARISH_FVG",
                    "top": round(float(lows[i - 2]), 4),
                    "bottom": round(float(highs[i]), 4),
                    "size": round(float(lows[i - 2] - highs[i]), 4),
                    "index": i
                })

        # B1: mark consumed zones. StructureContext zones feed SL/TP, so an
        # already-mitigated FVG/OB must not be used as an anchor. Mitigation is
        # a trade-THROUGH, not a wick touch: bullish FVG is consumed once price
        # trades at/below its bottom; a bullish order block once price CLOSES
        # below its low (wick-only does not invalidate an institutional zone).
        for g in fair_value_gaps:
            i = g["index"]
            after_low = lows[i + 1:]
            after_high = highs[i + 1:]
            after_close = closes[i + 1:]
            if g["type"] == "BULLISH_FVG":
                g["mitigated"] = bool(len(after_low) and (after_low <= g["bottom"]).any())
            else:
                g["mitigated"] = bool(len(after_high) and (after_high >= g["top"]).any())
            _ = after_close
        for ob in order_blocks:
            i = ob["index"]
            after_close = closes[i + 1:]
            if ob["type"] == "BULLISH_ORDER_BLOCK":
                ob["mitigated"] = bool(len(after_close) and (after_close < ob["low"]).any())
            else:
                ob["mitigated"] = bool(len(after_close) and (after_close > ob["high"]).any())

        # Horizontal S/R Clustering (Key Levels)
        all_swings = [s["price"] for s in swing_highs] + [s["price"] for s in swing_lows]
        key_levels = []
        visited = set()
        for p in all_swings:
            if p in visited:
                continue
            # cluster within 0.2%
            cluster = [x for x in all_swings if abs(x - p) / (p + 1e-9) <= 0.002]
            if len(cluster) >= 3:
                level_price = sum(cluster) / len(cluster)
                if not any(abs(level_price - k["price"]) / (k["price"] + 1e-9) <= 0.002 for k in key_levels):
                    key_levels.append({"price": round(level_price, 4), "touches": len(cluster)})
            for c in cluster:
                visited.add(c)

        # Asymmetric Quantile Dynamic Channel (AQDC)
        # Authored mechanism: connects phase swing extremes with independent 90th percentile
        # upper and lower quantile deviation bands to reject rogue wick anomalies.
        channel_basis = latest_close
        channel_upper = latest_close * 1.01
        channel_lower = latest_close * 0.99
        channel_position_pct = 50.0
        channel_quarter = "MIDDLE"

        if swing_highs and swing_lows:
            last_sh = swing_highs[-1]
            last_sl = swing_lows[-1]
            i1 = min(last_sh["index"], last_sl["index"])
            i2 = max(last_sh["index"], last_sl["index"])
            p1 = float(highs[i1] if i1 == last_sh["index"] else lows[i1])
            p2 = float(highs[i2] if i2 == last_sh["index"] else lows[i2])

            span_x = max(i2 - i1, 1)
            slope_m = (p2 - p1) / span_x

            # Compute basis values and deviations across the active phase [i1, len(df)-1]
            phase_indices = np.arange(i1, len(df))
            basis_series = p1 + slope_m * (phase_indices - i1)

            # Projected basis at current bar
            channel_basis = float(basis_series[-1])
            # Bound basis to reasonable limits so wild projection doesn't drift away
            channel_basis = float(np.clip(channel_basis, recent_min * 0.90, recent_max * 1.10))

            phase_highs = highs[i1:]
            phase_lows = lows[i1:]

            upper_devs = phase_highs - basis_series
            lower_devs = basis_series - phase_lows

            pos_upper = upper_devs[upper_devs > 0]
            pos_lower = lower_devs[lower_devs > 0]

            fallback_band = max((recent_max - recent_min) * 0.15, latest_close * 0.003)
            up_band = float(np.percentile(pos_upper, 90.0)) if len(pos_upper) > 0 else fallback_band
            low_band = float(np.percentile(pos_lower, 90.0)) if len(pos_lower) > 0 else fallback_band

            channel_upper = round(channel_basis + up_band, 4)
            channel_lower = round(channel_basis - low_band, 4)

            c_span = max(channel_upper - channel_lower, 1e-9)
            channel_position_pct = round(float(np.clip(((latest_close - channel_lower) / c_span) * 100.0, 0.0, 100.0)), 1)

            if channel_position_pct <= 25.0:
                channel_quarter = "LOWER_QUARTER"
            elif channel_position_pct >= 75.0:
                channel_quarter = "UPPER_QUARTER"
            else:
                channel_quarter = "MIDDLE"

        return StructureContext(
            bias=bias,
            higher_highs=hh,
            higher_lows=hl,
            lower_highs=lh,
            lower_lows=ll,
            bos=bos_bullish or bos_bearish,
            bos_type="BULLISH" if bos_bullish else ("BEARISH" if bos_bearish else "NONE"),
            choch=choch_bullish or choch_bearish,
            choch_type="BULLISH" if choch_bullish else ("BEARISH" if choch_bearish else "NONE"),
            demand_zone=demand_zone,
            supply_zone=supply_zone,
            equilibrium_price=round(equilibrium, 4),
            discount_premium_zone=discount_premium_zone,
            order_blocks=order_blocks[-4:],
            fair_value_gaps=fair_value_gaps[-4:],
            key_levels=key_levels,
            channel_basis=round(channel_basis, 4),
            channel_upper=round(channel_upper, 4),
            channel_lower=round(channel_lower, 4),
            channel_position_pct=channel_position_pct,
            channel_quarter=channel_quarter,
            adaptive_pivot_window=w
        )
