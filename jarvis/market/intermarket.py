"""
HM Algo 2.0 — Synthetic US Dollar Index (DXY) & Intermarket Analysis Engine.
Computes real-time synthetic DXY from broker FX rates and derives cross-asset macro impulses for Gold, FX, and Commodities.
"""
import time
import math
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np

logger = logging.getLogger("JARVIS_Intermarket")


@dataclass
class IntermarketDXYReport:
    """Intermarket Dollar Index Intelligence Report."""
    dxy_value: float = 104.0
    trend: str = "NEUTRAL"               # "BULLISH", "BEARISH", "NEUTRAL"
    momentum_score: float = 0.0          # -100.0 (bearish collapse) to +100.0 (bullish surge)
    ema20: float = 104.0
    ema50: float = 104.0
    rsi: float = 50.0
    gold_impact: str = "NEUTRAL"         # "OPPOSES_BUY", "CONFIRMS_BUY", "OPPOSES_SELL", "CONFIRMS_SELL", "NEUTRAL"
    gold_penalty: float = 0.0            # Penalty points against opposing Gold setup
    fx_bias: Dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    is_live: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dxy_value": round(self.dxy_value, 2),
            "trend": self.trend,
            "momentum_score": round(self.momentum_score, 1),
            "ema20": round(self.ema20, 2),
            "ema50": round(self.ema50, 2),
            "rsi": round(self.rsi, 1),
            "gold_impact": self.gold_impact,
            "gold_penalty": round(self.gold_penalty, 1),
            "fx_bias": self.fx_bias,
            "timestamp": self.timestamp,
            "is_live": self.is_live,
        }


class SyntheticDollarIndexEngine:
    """
    Computes real-time synthetic US Dollar Index (DXY) from live broker quotes.
    ICE DXY formula adjusted for 5 standard majors:
    EURUSD (57.6%), USDJPY (13.6%), GBPUSD (11.9%), USDCAD (9.1%), USDCHF (3.6%).
    """

    # Weights normalized across the 5 pairs (SEK 4.2% distributed proportionally)
    WEIGHTS: Dict[str, float] = {
        "EURUSD": -0.601,
        "USDJPY": 0.142,
        "GBPUSD": -0.124,
        "USDCAD": 0.095,
        "USDCHF": 0.038,
    }
    # Base constant calibrated to standard DXY level (~104.0 baseline)
    BASE_CONSTANT: float = 50.1435 * (1.04 ** (0.042 / 0.958))

    def __init__(self, cache_ttl_seconds: float = 15.0):
        self.cache_ttl = cache_ttl_seconds
        self._lock = threading.Lock()
        self._cached_report: Optional[IntermarketDXYReport] = None
        self._last_calc_time: float = 0.0

    @classmethod
    def calculate_dxy_instant(cls, prices: Dict[str, float]) -> Optional[float]:
        """
        Calculates instantaneous DXY value from a price dictionary.
        Requires at least EURUSD and USDJPY to estimate.
        """
        if not prices:
            return None

        eur = prices.get("EURUSD")
        jpy = prices.get("USDJPY")
        gbp = prices.get("GBPUSD", 1.28)
        cad = prices.get("USDCAD", 1.38)
        chf = prices.get("USDCHF", 0.88)

        if not eur or not jpy or eur <= 0 or jpy <= 0:
            return None

        try:
            val = cls.BASE_CONSTANT * (
                (eur ** cls.WEIGHTS["EURUSD"]) *
                (jpy ** cls.WEIGHTS["USDJPY"]) *
                (gbp ** cls.WEIGHTS["GBPUSD"]) *
                (cad ** cls.WEIGHTS["USDCAD"]) *
                (chf ** cls.WEIGHTS["USDCHF"])
            )
            return float(val) if math.isfinite(val) and 50.0 < val < 200.0 else None
        except Exception:
            return None

    def get_dxy_series_from_frames(self, frames: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """
        Constructs a historical DXY OHLC series from aligned timeframe DataFrames.
        """
        if not frames or "EURUSD" not in frames or "USDJPY" not in frames:
            return pd.DataFrame()

        df_eur = frames["EURUSD"]
        df_jpy = frames["USDJPY"]
        if df_eur.empty or df_jpy.empty:
            return pd.DataFrame()

        # Merge on time
        base_df = df_eur[["time", "close"]].rename(columns={"close": "EURUSD"})
        for sym in ("USDJPY", "GBPUSD", "USDCAD", "USDCHF"):
            if sym in frames and not frames[sym].empty and "time" in frames[sym].columns:
                sub = frames[sym][["time", "close"]].rename(columns={"close": sym})
                base_df = pd.merge(base_df, sub, on="time", how="inner")
            else:
                # Use typical fallback price if missing
                fallbacks = {"USDJPY": 152.0, "GBPUSD": 1.29, "USDCAD": 1.38, "USDCHF": 0.88}
                base_df[sym] = fallbacks.get(sym, 1.0)

        if len(base_df) < 5:
            return pd.DataFrame()

        # Vectorized DXY computation
        try:
            dxy_vals = self.BASE_CONSTANT * (
                (base_df["EURUSD"] ** self.WEIGHTS["EURUSD"]) *
                (base_df["USDJPY"] ** self.WEIGHTS["USDJPY"]) *
                (base_df["GBPUSD"] ** self.WEIGHTS["GBPUSD"]) *
                (base_df["USDCAD"] ** self.WEIGHTS["USDCAD"]) *
                (base_df["USDCHF"] ** self.WEIGHTS["USDCHF"])
            )
            res_df = pd.DataFrame({"time": base_df["time"], "close": dxy_vals})
            return res_df
        except Exception as exc:
            logger.debug(f"Vectorized DXY computation failed: {exc}")
            return pd.DataFrame()

    def analyze(self) -> IntermarketDXYReport:
        """
        Analyzes current DXY trend, momentum, and cross-asset Gold impact.
        Caches results for `cache_ttl_seconds` to provide sub-millisecond retrieval.
        """
        now = time.time()
        with self._lock:
            if self._cached_report is not None and (now - self._last_calc_time) < self.cache_ttl:
                return self._cached_report

        report = self._fetch_and_calculate_live()
        with self._lock:
            self._cached_report = report
            self._last_calc_time = now
        return report

    def _fetch_and_calculate_live(self) -> IntermarketDXYReport:
        """Fetches latest quotes from MT5 and calculates DXY report."""
        try:
            import MetaTrader5 as mt5
            from jarvis.data.broker_symbols import resolve_broker_symbol

            symbols = ["EURUSD", "USDJPY", "GBPUSD", "USDCAD", "USDCHF"]
            frames = {}

            for sym in symbols:
                b_sym = resolve_broker_symbol(sym)
                if not b_sym:
                    continue
                # Fetch last 40 H1 bars for reliable trend analysis
                rates = mt5.copy_rates_from_pos(b_sym, mt5.TIMEFRAME_H1, 0, 45)
                if rates is not None and len(rates) >= 20:
                    frames[sym] = pd.DataFrame(rates)

            if "EURUSD" not in frames or "USDJPY" not in frames:
                return IntermarketDXYReport(
                    dxy_value=104.0,
                    trend="NEUTRAL",
                    momentum_score=0.0,
                    is_live=False
                )

            dxy_df = self.get_dxy_series_from_frames(frames)
            if dxy_df.empty or len(dxy_df) < 15:
                return IntermarketDXYReport(
                    dxy_value=104.0,
                    trend="NEUTRAL",
                    momentum_score=0.0,
                    is_live=False
                )

            close_series = dxy_df["close"]
            dxy_now = float(close_series.iloc[-1])
            ema20 = float(close_series.ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(close_series.ewm(span=min(50, len(close_series)), adjust=False).mean().iloc[-1])

            # Calculate 14-period RSI on DXY series
            delta = close_series.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain.iloc[-1] / (loss.iloc[-1] + 1e-9)
            rsi = float(100.0 - (100.0 / (1.0 + rs)))

            # Trend & Momentum calculation
            trend = "NEUTRAL"
            momentum_score = 0.0

            if dxy_now > max(ema20, ema50) and ema20 >= ema50:
                trend = "BULLISH"
                momentum_score = min(100.0, max(25.0, (dxy_now - ema50) / ema50 * 5000.0))
            elif dxy_now < min(ema20, ema50) and ema20 <= ema50:
                trend = "BEARISH"
                momentum_score = max(-100.0, min(-25.0, (dxy_now - ema50) / ema50 * 5000.0))
            else:
                trend = "NEUTRAL"
                momentum_score = (dxy_now - ema20) / ema20 * 2000.0

            # Gold Impact Analysis (Inverse Correlation: Gold trades inversely to US Dollar)
            gold_impact = "NEUTRAL"
            gold_penalty = 0.0

            if trend == "BULLISH" and momentum_score >= 35.0:
                gold_impact = "OPPOSES_BUY"
                gold_penalty = 15.0  # US Dollar surging: penalize Gold BUY
            elif trend == "BEARISH" and momentum_score <= -35.0:
                gold_impact = "CONFIRMS_BUY"
                gold_penalty = 0.0   # US Dollar weakening: strong tailwind for Gold BUY
            elif trend == "BULLISH":
                gold_impact = "CONFIRMS_SELL"
            elif trend == "BEARISH":
                gold_impact = "OPPOSES_SELL"
                gold_penalty = 15.0

            # Derive FX biases based on Dollar strength
            fx_bias = {
                "EURUSD": "BEARISH" if trend == "BULLISH" else ("BULLISH" if trend == "BEARISH" else "NEUTRAL"),
                "GBPUSD": "BEARISH" if trend == "BULLISH" else ("BULLISH" if trend == "BEARISH" else "NEUTRAL"),
                "USDJPY": "BULLISH" if trend == "BULLISH" else ("BEARISH" if trend == "BEARISH" else "NEUTRAL"),
                "USDCAD": "BULLISH" if trend == "BULLISH" else ("BEARISH" if trend == "BEARISH" else "NEUTRAL"),
                "USDCHF": "BULLISH" if trend == "BULLISH" else ("BEARISH" if trend == "BEARISH" else "NEUTRAL"),
            }

            return IntermarketDXYReport(
                dxy_value=round(dxy_now, 2),
                trend=trend,
                momentum_score=round(momentum_score, 1),
                ema20=round(ema20, 2),
                ema50=round(ema50, 2),
                rsi=round(rsi, 1),
                gold_impact=gold_impact,
                gold_penalty=gold_penalty,
                fx_bias=fx_bias,
                timestamp=now,
                is_live=True
            )

        except Exception as exc:
            logger.debug(f"Intermarket DXY live calculation error: {exc}")
            return IntermarketDXYReport(
                dxy_value=104.0,
                trend="NEUTRAL",
                momentum_score=0.0,
                is_live=False
            )


# Global singleton engine
GLOBAL_INTERMARKET_ENGINE = SyntheticDollarIndexEngine(cache_ttl_seconds=15.0)


def get_intermarket_dxy_report() -> IntermarketDXYReport:
    """Returns the cached or freshly evaluated Intermarket DXY Report."""
    return GLOBAL_INTERMARKET_ENGINE.analyze()
