"""
HM Algo 2.0 — Volatility & Spread Protection Engine.
Computes ATR, Bollinger bandwidth, Parkinson & Garman-Klass Volatility,
volatility regime states (Compression, Normal, Expansion, Extreme),
volatility spike circuit breakers, and spread feasibility.
"""
import numpy as np
import pandas as pd
from jarvis.data.schemas import VolatilityContext

class VolatilityEngine:
    """
    Advanced Quantitative Volatility Engine:
    - Parkinson Volatility: Extreme value estimator using High/Low (5x efficiency over close-to-close)
    - Garman-Klass Volatility: OHLC estimator (8x efficiency over close-to-close)
    - ATR & Forward Clustering Forecast
    - Bollinger Bandwidth & Squeeze Detection
    - Dynamic Volatility Ratio & Extreme Spike Circuit Breaker
    """
    def __init__(self, atr_period: int = 14, bb_period: int = 20, bb_std: float = 2.0):
        self.atr_period = atr_period
        self.bb_period = bb_period
        self.bb_std = bb_std

    @staticmethod
    def compute_parkinson_volatility(
        high: pd.Series,
        low: pd.Series,
        window: int = 20
    ) -> pd.Series:
        """
        Computes Parkinson realized volatility:
        sigma_P = sqrt( 1 / (4 * ln(2) * N) * sum( ln(H / L)^2 ) )
        """
        hl_ratio = np.maximum(high.values / np.maximum(low.values, 1e-9), 1.0)
        log_hl_sq = (np.log(hl_ratio)) ** 2
        factor = 1.0 / (4.0 * np.log(2.0))
        # Rolling mean of squared log ratios multiplied by factor
        rolling_mean = pd.Series(log_hl_sq, index=high.index).rolling(window=window, min_periods=1).mean()
        return np.sqrt(rolling_mean * factor)

    @staticmethod
    def compute_garman_klass_volatility(
        open_: pd.Series,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        window: int = 20
    ) -> pd.Series:
        """
        Computes Garman-Klass realized volatility:
        sigma_GK = sqrt( 1 / N * sum( 0.5 * ln(H / L)^2 - (2*ln(2) - 1) * ln(C / O)^2 ) )
        """
        hl_ratio = np.maximum(high.values / np.maximum(low.values, 1e-9), 1.0)
        co_ratio = np.maximum(close.values / np.maximum(open_.values, 1e-9), 1e-9)

        log_hl_sq = (np.log(hl_ratio)) ** 2
        log_co_sq = (np.log(co_ratio)) ** 2

        gk_const = 2.0 * np.log(2.0) - 1.0  # ~0.386294
        gk_terms = 0.5 * log_hl_sq - gk_const * log_co_sq
        # Clamp negative values due to numerical precision
        gk_terms = np.maximum(gk_terms, 0.0)

        rolling_mean = pd.Series(gk_terms, index=high.index).rolling(window=window, min_periods=1).mean()
        return np.sqrt(rolling_mean)

    def analyze_volatility(
        self,
        df: pd.DataFrame,
        current_spread_pips: float = 2.0,
        max_allowed_spread_pips: float = 35.0
    ) -> VolatilityContext:
        if len(df) < self.bb_period:
            return VolatilityContext(
                atr=0.0,
                atr_percent=0.0,
                state="NORMAL",
                bollinger_bandwidth=0.0,
                current_spread_pips=current_spread_pips,
                max_allowed_spread_pips=max_allowed_spread_pips,
                is_excessive_spread=current_spread_pips > max_allowed_spread_pips,
                parkinson_volatility=0.0,
                garman_klass_volatility=0.0,
                volatility_ratio=1.0,
                volatility_spike_breaker=False
            )

        high = df["high"]
        low = df["low"]
        close = df["close"]
        open_ = df["open"] if "open" in df.columns else close
        close_prev = close.shift(1)

        # True Range & ATR
        tr = np.maximum(high - low, np.maximum(np.abs(high - close_prev), np.abs(low - close_prev)))
        atr_series = tr.rolling(window=self.atr_period, min_periods=1).mean()
        current_atr = float(atr_series.iloc[-1])
        current_price = float(close.iloc[-1])
        atr_pct = (current_atr / (current_price + 1e-9)) * 100.0

        # Bollinger Bandwidth
        sma = close.rolling(window=self.bb_period).mean()
        std = close.rolling(window=self.bb_period).std()
        upper_bb = sma + (std * self.bb_std)
        lower_bb = sma - (std * self.bb_std)
        bb_bandwidth = float(((upper_bb.iloc[-1] - lower_bb.iloc[-1]) / (sma.iloc[-1] + 1e-9)) * 100.0)

        # Quantitative Volatility Estimators: Parkinson & Garman-Klass
        parkinson_series = self.compute_parkinson_volatility(high, low, window=self.bb_period)
        gk_series = self.compute_garman_klass_volatility(open_, high, low, close, window=self.bb_period)
        current_parkinson = float(parkinson_series.iloc[-1])
        current_gk = float(gk_series.iloc[-1])

        # Historical baseline comparisons
        atr_historical_median = float(atr_series.tail(100).median()) if len(atr_series) >= 20 else current_atr
        atr_vol_ratio = current_atr / (atr_historical_median + 1e-9)

        gk_historical_median = float(gk_series.tail(100).median()) if len(gk_series) >= 20 else current_gk
        gk_vol_ratio = current_gk / (gk_historical_median + 1e-9) if gk_historical_median > 0 else 1.0

        # ATR Volatility Ratio
        vol_ratio = atr_vol_ratio

        # Master Volatility Ratio (blended GK & ATR)
        blended_vol_ratio = float(max(atr_vol_ratio, gk_vol_ratio))

        # Instantaneous spike check: single-bar True Range vs historical median
        instant_tr_spike = bool(float(tr.iloc[-1]) > (atr_historical_median * 3.0))

        # Volatility Spike Circuit Breaker (fires when realized vol > 2.5x baseline or instant single-bar range > 3.0x)
        volatility_spike_breaker = bool(blended_vol_ratio > 2.5 or instant_tr_spike)

        if vol_ratio > 2.5:
            state = "EXTREME"
        elif vol_ratio > 1.4:
            state = "EXPANSION"
        elif vol_ratio < 0.65:
            state = "COMPRESSION"
        else:
            state = "NORMAL"

        # Forward Volatility Clustering Forecast & Spread Impact Ratio
        forward_forecast = current_atr
        spread_risk_score = 0.0
        if len(atr_series) >= 10:
            atr_momentum = float(atr_series.iloc[-1] - atr_series.iloc[-5]) / (float(atr_series.iloc[-5]) + 1e-9)
            forward_forecast = float(current_atr * (1.0 + np.clip(atr_momentum * 0.5, -0.3, 0.5)))
            if current_atr > 0 and max_allowed_spread_pips > 0:
                spread_risk_score = float(np.clip((current_spread_pips / max_allowed_spread_pips) * 100.0, 0.0, 100.0))

        is_excessive_spread = current_spread_pips > max_allowed_spread_pips
        return VolatilityContext(
            atr=round(current_atr, 4),
            atr_percent=round(atr_pct, 4),
            state=state,
            bollinger_bandwidth=round(bb_bandwidth, 3),
            current_spread_pips=round(current_spread_pips, 2),
            max_allowed_spread_pips=max_allowed_spread_pips,
            is_excessive_spread=is_excessive_spread,
            forward_atr_forecast=round(forward_forecast, 4),
            spread_risk_score=round(spread_risk_score, 2),
            parkinson_volatility=round(current_parkinson, 6),
            garman_klass_volatility=round(current_gk, 6),
            volatility_ratio=round(vol_ratio, 2),
            volatility_spike_breaker=volatility_spike_breaker
        )
