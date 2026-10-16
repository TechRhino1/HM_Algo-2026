import pandas as pd
import numpy as np
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger('JARVIS_OrderFlow')

class InstitutionalVolumeOrderFlowEngine:
    """
    Institutional Order Flow & Depth of Market (DOM) Engine:
    - Real-time Order Book Imbalance (OBI) & Micro-Price
    - Multi-bar Cumulative Volume Delta (CVD) & Divergence Tracking (Absorption)
    - Volume-Synchronized Probability of Toxicity (VPIN)
    - Footprint Delta & Institutional Absorption Trap Detection
    """
    def __init__(self, volume_ma_period: int = 20, extreme_vol_multiplier: float = 2.5):
        self.volume_ma_period = volume_ma_period
        self.extreme_vol_multiplier = extreme_vol_multiplier

    @staticmethod
    def compute_order_book_imbalance(
        market_depth: Optional[List[Dict[str, Any]]],
        top_k: int = 5
    ) -> Dict[str, float]:
        """
        Computes Order Book Imbalance (OBI) and micro-price across top K DOM levels.
        OBI = (V_bid - V_ask) / (V_bid + V_ask) in [-1.0, 1.0].
        """
        if not market_depth or not isinstance(market_depth, list):
            return {"obi": 0.0, "micro_price": 0.0, "bid_depth": 0.0, "ask_depth": 0.0}

        bids = [d for d in market_depth if str(d.get("type", "")).upper() == "BUY"]
        asks = [d for d in market_depth if str(d.get("type", "")).upper() == "SELL"]

        bids.sort(key=lambda x: float(x.get("price", 0.0)), reverse=True)
        asks.sort(key=lambda x: float(x.get("price", 0.0)))

        top_bids = bids[:top_k]
        top_asks = asks[:top_k]

        bid_vol_sum = float(sum(float(x.get("volume", 0.0)) for x in top_bids))
        ask_vol_sum = float(sum(float(x.get("volume", 0.0)) for x in top_asks))
        tot_vol = bid_vol_sum + ask_vol_sum

        if tot_vol <= 0:
            return {"obi": 0.0, "micro_price": 0.0, "bid_depth": 0.0, "ask_depth": 0.0}

        obi = float(np.clip((bid_vol_sum - ask_vol_sum) / tot_vol, -1.0, 1.0))
        best_bid = float(top_bids[0].get("price", 0.0)) if top_bids else 0.0
        best_ask = float(top_asks[0].get("price", 0.0)) if top_asks else 0.0

        if best_bid > 0 and best_ask > 0:
            micro_p = float((best_ask * bid_vol_sum + best_bid * ask_vol_sum) / tot_vol)
        else:
            micro_p = best_bid or best_ask

        return {
            "obi": round(obi, 3),
            "micro_price": round(micro_p, 5),
            "bid_depth": round(bid_vol_sum, 2),
            "ask_depth": round(ask_vol_sum, 2)
        }

    @staticmethod
    def compute_vpin(buy_vols: np.ndarray, sell_vols: np.ndarray) -> float:
        """
        Computes Volume-Synchronized Probability of Toxicity (VPIN).
        VPIN = sum(|V_buy - V_sell|) / sum(V_buy + V_sell).
        """
        tot = float(np.sum(buy_vols + sell_vols))
        if tot <= 0:
            return 0.0
        imbalance = float(np.sum(np.abs(buy_vols - sell_vols)))
        return float(np.clip(imbalance / tot, 0.0, 1.0))

    @staticmethod
    def detect_cvd_divergence(close_arr: np.ndarray, cum_deltas: np.ndarray) -> str:
        """
        Detects Cumulative Volume Delta (CVD) multi-bar divergence against price.
        - BULLISH_DIVERGENCE: Price making lower low while CVD makes higher low (Absorption)
        - BEARISH_DIVERGENCE: Price making higher high while CVD makes lower high (Exhaustion)
        """
        n_bars = len(cum_deltas)
        if n_bars < 8:
            return "NONE"

        half = n_bars // 2
        p_prior_min = float(np.min(close_arr[:half]))
        p_recent_min = float(np.min(close_arr[half:]))
        cvd_prior_min = float(np.min(cum_deltas[:half]))
        cvd_recent_min = float(np.min(cum_deltas[half:]))

        p_prior_max = float(np.max(close_arr[:half]))
        p_recent_max = float(np.max(close_arr[half:]))
        cvd_prior_max = float(np.max(cum_deltas[:half]))
        cvd_recent_max = float(np.max(cum_deltas[half:]))

        if p_recent_min < p_prior_min and cvd_recent_min > cvd_prior_min:
            return "BULLISH_DIVERGENCE"
        elif p_recent_max > p_prior_max and cvd_recent_max < cvd_prior_max:
            return "BEARISH_DIVERGENCE"

        return "NONE"

    def analyze_order_flow(
        self,
        df: pd.DataFrame,
        market_depth: Optional[List[Dict[str, Any]]] = None
    ) -> dict:
        default_res = {
            'signal': 'NEUTRAL',
            'strength': 0.0,
            'institutional_activity': False,
            'current_vol_ratio': 1.0,
            'delta_ratio': 0.0,
            'absorption_trap': None,
            'delta_score': 0.0,
            'obi': 0.0,
            'micro_price': 0.0,
            'cvd_divergence': 'NONE',
            'vpin': 0.0,
            'toxic_flow_detected': False
        }

        if df is None or len(df) < self.volume_ma_period:
            return default_res

        # Resolve volume column defensively
        vol_col = None
        for candidate in ("volume", "tick_volume", "real_volume"):
            if candidate in df.columns:
                vol_col = candidate
                break

        if vol_col is None:
            res = dict(default_res)
            res['note'] = 'no volume column; order flow unavailable'
            return res

        # Fast numpy slice extraction
        vol_arr = df[vol_col].values[-self.volume_ma_period:].astype(float)
        close_arr = df['close'].values[-self.volume_ma_period:].astype(float)
        open_arr = df['open'].values[-self.volume_ma_period:].astype(float)
        high_arr = df['high'].values[-self.volume_ma_period:].astype(float) if 'high' in df.columns else close_arr
        low_arr = df['low'].values[-self.volume_ma_period:].astype(float) if 'low' in df.columns else open_arr

        vol_ma = float(np.mean(vol_arr))
        current_vol = float(vol_arr[-1])
        is_institutional_vol = current_vol > (vol_ma * self.extreme_vol_multiplier)

        body_arr = np.abs(close_arr - open_arr)
        current_body = float(body_arr[-1])
        avg_body = float(np.mean(body_arr))
        is_bullish = close_arr[-1] > open_arr[-1]

        # ── Footprint Volume Delta Calculation ──
        ranges = np.maximum(high_arr - low_arr, 1e-9)
        buy_vols = vol_arr * ((close_arr - low_arr) / ranges)
        sell_vols = vol_arr * ((high_arr - close_arr) / ranges)
        deltas = buy_vols - sell_vols

        latest_delta = float(deltas[-1])
        recent_cum_delta = float(np.sum(deltas[-3:]))
        delta_ratio = float(latest_delta / (vol_ma + 1e-9))
        delta_score = float(np.clip((recent_cum_delta / (vol_ma * 3.0 + 1e-9)) * 100.0, -100.0, 100.0))

        # ── Cumulative Volume Delta (CVD) & Divergence ──
        cum_deltas = np.cumsum(deltas)
        cvd_divergence = self.detect_cvd_divergence(close_arr, cum_deltas)

        # ── Volume-Synchronized Probability of Toxicity (VPIN) ──
        vpin = self.compute_vpin(buy_vols, sell_vols)
        toxic_flow_detected = bool(vpin >= 0.65)

        # ── Order Book Imbalance (Level 2 DOM or Footprint Proxy) ──
        dom_metrics = self.compute_order_book_imbalance(market_depth, top_k=5)
        if dom_metrics["obi"] != 0.0 or dom_metrics["micro_price"] > 0:
            obi = dom_metrics["obi"]
            micro_price = dom_metrics["micro_price"]
        else:
            # Proxy from footprint delta if DOM not available
            obi = float(np.clip(latest_delta / (current_vol + 1e-9), -1.0, 1.0))
            micro_price = float(close_arr[-1])

        # ── Absorption Trap Detection ──
        absorption_trap = None
        if is_bullish and latest_delta < (-0.20 * current_vol) and is_institutional_vol:
            absorption_trap = "SELLER_ABSORPTION_TRAP"
            logger.debug(f"Order Flow: SELLER_ABSORPTION_TRAP detected! Bullish candle with negative delta ({latest_delta:.1f}).")
        elif (not is_bullish) and latest_delta > (0.20 * current_vol) and is_institutional_vol:
            absorption_trap = "BUYER_ABSORPTION_TRAP"
            logger.debug(f"Order Flow: BUYER_ABSORPTION_TRAP detected! Bearish candle with positive delta (+{latest_delta:.1f}).")

        # ── Base Signal Determination ──
        signal = 'NEUTRAL'
        strength = 0.0

        if is_institutional_vol:
            if current_body > (avg_body * 1.5):
                if absorption_trap == "SELLER_ABSORPTION_TRAP" and is_bullish:
                    signal = 'NEUTRAL'
                    strength = 0.0
                elif absorption_trap == "BUYER_ABSORPTION_TRAP" and (not is_bullish):
                    signal = 'NEUTRAL'
                    strength = 0.0
                else:
                    signal = 'BUY' if (is_bullish or latest_delta > 0) else 'SELL'
                    strength = 1.0
            elif current_body < (avg_body * 0.5):
                signal = 'SELL' if is_bullish else 'BUY'
                strength = 0.8
        else:
            if delta_score > 35.0:
                signal = 'BUY'
                strength = 0.6
            elif delta_score < -35.0:
                signal = 'SELL'
                strength = 0.6

        # ── Multi-Confluence Enhancements (CVD Divergence & OBI) ──
        if cvd_divergence == "BULLISH_DIVERGENCE":
            if signal == "BUY":
                strength = min(1.0, strength + 0.20)
            elif signal == "NEUTRAL":
                signal = "BUY"
                strength = 0.70
            elif signal == "SELL" and strength <= 0.6:
                signal = "NEUTRAL"
                strength = 0.0
        elif cvd_divergence == "BEARISH_DIVERGENCE":
            if signal == "SELL":
                strength = min(1.0, strength + 0.20)
            elif signal == "NEUTRAL":
                signal = "SELL"
                strength = 0.70
            elif signal == "BUY" and strength <= 0.6:
                signal = "NEUTRAL"
                strength = 0.0

        if obi > 0.35 and signal == "BUY":
            strength = min(1.0, strength + 0.15)
        elif obi < -0.35 and signal == "SELL":
            strength = min(1.0, strength + 0.15)

        return {
            'signal': signal,
            'strength': round(strength, 2),
            'institutional_activity': bool(is_institutional_vol),
            'current_vol_ratio': round(current_vol / (vol_ma + 1e-9), 2),
            'delta_ratio': round(delta_ratio, 2),
            'absorption_trap': absorption_trap,
            'delta_score': round(delta_score, 1),
            'obi': round(obi, 3),
            'micro_price': round(micro_price, 5),
            'cvd_divergence': cvd_divergence,
            'vpin': round(vpin, 3),
            'toxic_flow_detected': toxic_flow_detected
        }
