"""
HM Algo 2.0 — Smart Money Technique (SMT) Divergence Engine.
Institutional intermarket analysis across:
1. Precious Metals: Gold (XAUUSD) vs Silver (XAGUSD)
2. Equity Indices: US500 vs NAS100 vs US30
3. Forex Majors: EURUSD vs GBPUSD

Detects non-confirmation between correlated assets at key swing highs and lows:
- Bullish SMT: One asset makes a lower low while the correlated peer makes a higher low (Accumulation).
- Bearish SMT: One asset makes a higher high while the correlated peer makes a lower high (Distribution).
"""
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np

logger = logging.getLogger("JARVIS_SMTDivergence")


def _get_swings(df: pd.DataFrame) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Detects 3-bar swing highs and swing lows from high/low columns."""
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    if df is None or len(df) < 5 or "high" not in df.columns or "low" not in df.columns:
        return highs, lows

    n = len(df)
    for i in range(1, n - 1):
        h_prev, h_curr, h_next = df['high'].iloc[i-1], df['high'].iloc[i], df['high'].iloc[i+1]
        l_prev, l_curr, l_next = df['low'].iloc[i-1], df['low'].iloc[i], df['low'].iloc[i+1]
        if h_curr > h_prev and h_curr >= h_next:
            highs.append((i, float(h_curr)))
        if l_curr < l_prev and l_curr <= l_next:
            lows.append((i, float(l_curr)))
    return highs, lows


def _evaluate_smt_pair(
    highs_a: List[Tuple[int, float]],
    lows_a: List[Tuple[int, float]],
    highs_b: List[Tuple[int, float]],
    lows_b: List[Tuple[int, float]],
    name_a: str,
    name_b: str
) -> Tuple[str, str, float, str]:
    """
    Evaluates SMT divergence between asset A and asset B.
    Returns (smt_type, leader, score_bonus, description).
    """
    smt_type = "NONE"
    leader = "NONE"
    score_bonus = 0.0
    desc = "No SMT divergence detected"

    # Bearish SMT at Swing Highs (Distribution)
    if len(highs_a) >= 2 and len(highs_b) >= 2:
        a_h1, a_h2 = highs_a[-2][1], highs_a[-1][1]
        b_h1, b_h2 = highs_b[-2][1], highs_b[-1][1]

        if a_h2 > a_h1 and b_h2 < b_h1:
            smt_type = "BEARISH_SMT"
            leader = name_b
            score_bonus = 15.0
            desc = f"Bearish SMT: {name_a} made Higher High while {name_b} made Lower High (Distribution)"
        elif b_h2 > b_h1 and a_h2 < a_h1:
            smt_type = "BEARISH_SMT"
            leader = name_a
            score_bonus = 15.0
            desc = f"Bearish SMT: {name_b} made Higher High while {name_a} made Lower High (Distribution)"

    # Bullish SMT at Swing Lows (Accumulation)
    if len(lows_a) >= 2 and len(lows_b) >= 2:
        a_l1, a_l2 = lows_a[-2][1], lows_a[-1][1]
        b_l1, b_l2 = lows_b[-2][1], lows_b[-1][1]

        if a_l2 < a_l1 and b_l2 > b_l1:
            smt_type = "BULLISH_SMT"
            leader = name_b
            score_bonus = 15.0
            desc = f"Bullish SMT: {name_a} made Lower Low while {name_b} made Higher Low (Accumulation)"
        elif b_l2 < b_l1 and a_l2 > a_l1:
            smt_type = "BULLISH_SMT"
            leader = name_a
            score_bonus = 15.0
            desc = f"Bullish SMT: {name_b} made Lower Low while {name_a} made Higher Low (Accumulation)"

    return smt_type, leader, score_bonus, desc


class MetalsSMTEngine:
    """
    Computes SMT Divergence between XAUUSD (Gold) and XAGUSD (Silver).
    Preserves 100% backward compatibility for all Gold/Silver pipelines.
    """
    def __init__(self, cache_ttl_seconds: float = 20.0):
        self.cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    @staticmethod
    def is_metal(symbol: str) -> bool:
        s = (symbol or "").upper()
        return any(k in s for k in ("XAU", "GOLD", "XAG", "SILVER"))

    def detect_smt(
        self,
        symbol: str,
        df_primary: Optional[pd.DataFrame] = None,
        dt: Optional[datetime] = None
    ) -> Dict[str, Any]:
        if not self.is_metal(symbol):
            return {
                "smt_active": False,
                "smt_type": "NONE",
                "score_bonus": 0.0,
                "leader": "NONE",
                "description": "Non-metal asset"
            }

        now_t = time.time()
        cache_key = f"{symbol.upper()}"
        if cache_key in self._cache:
            c_time, c_val = self._cache[cache_key]
            if (now_t - c_time) < self.cache_ttl:
                return dict(c_val)

        try:
            import MetaTrader5 as mt5
            from jarvis.data.broker_symbols import resolve_broker_symbol

            gold_sym = resolve_broker_symbol("XAUUSD")
            silver_sym = resolve_broker_symbol("XAGUSD")

            if not gold_sym or not silver_sym:
                res = {
                    "smt_active": False,
                    "smt_type": "NONE",
                    "score_bonus": 0.0,
                    "leader": "NONE",
                    "description": "Metals pair symbol unavailable at broker"
                }
                self._cache[cache_key] = (now_t, res)
                return res

            rates_gold = mt5.copy_rates_from_pos(gold_sym, mt5.TIMEFRAME_M15, 0, 32)
            rates_silver = mt5.copy_rates_from_pos(silver_sym, mt5.TIMEFRAME_M15, 0, 32)

            if rates_gold is None or rates_silver is None or len(rates_gold) < 15 or len(rates_silver) < 15:
                rates_gold = mt5.copy_rates_from_pos(gold_sym, mt5.TIMEFRAME_H1, 0, 32)
                rates_silver = mt5.copy_rates_from_pos(silver_sym, mt5.TIMEFRAME_H1, 0, 32)

            if rates_gold is None or rates_silver is None or len(rates_gold) < 15 or len(rates_silver) < 15:
                res = {
                    "smt_active": False,
                    "smt_type": "NONE",
                    "score_bonus": 0.0,
                    "leader": "NONE",
                    "description": "Insufficient MT5 broker rates for pair"
                }
                self._cache[cache_key] = (now_t, res)
                return res

            df_g = pd.DataFrame(rates_gold)
            df_s = pd.DataFrame(rates_silver)

            # Liveness & Cross-Bar Timestamp Synchronization Check:
            # If one market is closed/stale for weekend/maintenance, don't trigger false SMT!
            if "time" in df_g.columns and "time" in df_s.columns and len(df_g) > 0 and len(df_s) > 0:
                ts_g = int(df_g["time"].iloc[-1])
                ts_s = int(df_s["time"].iloc[-1])
                if abs(ts_g - ts_s) > 7200:  # > 2 hours desync
                    res = {
                        "smt_active": False,
                        "smt_type": "NONE",
                        "score_bonus": 0.0,
                        "leader": "NONE",
                        "description": "Secondary metal (Silver) out of sync or market closed"
                    }
                    self._cache[cache_key] = (now_t, res)
                    return res

            g_highs, g_lows = _get_swings(df_g)
            s_highs, s_lows = _get_swings(df_s)

            smt_type, leader, score_bonus, desc = _evaluate_smt_pair(
                g_highs, g_lows, s_highs, s_lows, "Gold", "Silver"
            )

            res = {
                "smt_active": (smt_type != "NONE"),
                "smt_type": smt_type,
                "leader": leader,
                "score_bonus": score_bonus,
                "description": desc
            }
            self._cache[cache_key] = (now_t, res)
            return res

        except Exception as e:
            logger.debug("Metals SMT check offline/exception: %s", e)
            res = {
                "smt_active": False,
                "smt_type": "NONE",
                "score_bonus": 0.0,
                "leader": "NONE",
                "description": f"Offline ({e})"
            }
            self._cache[cache_key] = (now_t, res)
            return res


class IndicesSMTEngine:
    """Computes SMT Divergence across US Equity Indices (US500 vs NAS100)."""
    def __init__(self, cache_ttl_seconds: float = 20.0):
        self.cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    @staticmethod
    def is_index(symbol: str) -> bool:
        s = (symbol or "").upper()
        return any(k in s for k in ("US500", "SPX", "NAS100", "NDX", "US30", "DJ30"))

    def detect_smt(self, symbol: str) -> Dict[str, Any]:
        if not self.is_index(symbol):
            return {"smt_active": False, "smt_type": "NONE", "score_bonus": 0.0, "leader": "NONE", "description": "Non-index"}

        now_t = time.time()
        cache_key = "INDICES"
        if cache_key in self._cache:
            c_time, c_val = self._cache[cache_key]
            if (now_t - c_time) < self.cache_ttl:
                return dict(c_val)

        try:
            import MetaTrader5 as mt5
            from jarvis.data.broker_symbols import resolve_broker_symbol

            spx_sym = resolve_broker_symbol("US500")
            ndx_sym = resolve_broker_symbol("NAS100")

            if not spx_sym or not ndx_sym:
                res = {
                    "smt_active": False,
                    "smt_type": "NONE",
                    "score_bonus": 0.0,
                    "leader": "NONE",
                    "description": "Secondary index unmapped at broker"
                }
                self._cache[cache_key] = (now_t, res)
                return res

            r_spx = mt5.copy_rates_from_pos(spx_sym, mt5.TIMEFRAME_M15, 0, 32)
            r_ndx = mt5.copy_rates_from_pos(ndx_sym, mt5.TIMEFRAME_M15, 0, 32)

            if r_spx is None or r_ndx is None or len(r_spx) < 15 or len(r_ndx) < 15:
                r_spx = mt5.copy_rates_from_pos(spx_sym, mt5.TIMEFRAME_H1, 0, 32)
                r_ndx = mt5.copy_rates_from_pos(ndx_sym, mt5.TIMEFRAME_H1, 0, 32)

            if r_spx is None or r_ndx is None or len(r_spx) < 15 or len(r_ndx) < 15:
                res = {"smt_active": False, "smt_type": "NONE", "score_bonus": 0.0, "leader": "NONE", "description": "Rates unavailable"}
                self._cache[cache_key] = (now_t, res)
                return res

            df_spx = pd.DataFrame(r_spx)
            df_ndx = pd.DataFrame(r_ndx)

            if "time" in df_spx.columns and "time" in df_ndx.columns and len(df_spx) > 0 and len(df_ndx) > 0:
                ts_spx = int(df_spx["time"].iloc[-1])
                ts_ndx = int(df_ndx["time"].iloc[-1])
                if abs(ts_spx - ts_ndx) > 7200:
                    res = {
                        "smt_active": False,
                        "smt_type": "NONE",
                        "score_bonus": 0.0,
                        "leader": "NONE",
                        "description": "Secondary index out of sync or market closed"
                    }
                    self._cache[cache_key] = (now_t, res)
                    return res

            spx_h, spx_l = _get_swings(df_spx)
            ndx_h, ndx_l = _get_swings(df_ndx)

            smt_type, leader, score_bonus, desc = _evaluate_smt_pair(
                spx_h, spx_l, ndx_h, ndx_l, "US500", "NAS100"
            )

            res = {
                "smt_active": (smt_type != "NONE"),
                "smt_type": smt_type,
                "leader": leader,
                "score_bonus": score_bonus,
                "description": desc
            }
            self._cache[cache_key] = (now_t, res)
            return res
        except Exception as e:
            res = {"smt_active": False, "smt_type": "NONE", "score_bonus": 0.0, "leader": "NONE", "description": str(e)}
            self._cache[cache_key] = (now_t, res)
            return res


class ForexSMTEngine:
    """Computes SMT Divergence across major Forex pairs (EURUSD vs GBPUSD)."""
    def __init__(self, cache_ttl_seconds: float = 20.0):
        self.cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    @staticmethod
    def is_fx_major(symbol: str) -> bool:
        s = (symbol or "").upper()
        return any(k in s for k in ("EURUSD", "GBPUSD", "USDCHF", "USDJPY"))

    def detect_smt(self, symbol: str) -> Dict[str, Any]:
        if not self.is_fx_major(symbol):
            return {"smt_active": False, "smt_type": "NONE", "score_bonus": 0.0, "leader": "NONE", "description": "Non-FX"}

        now_t = time.time()
        cache_key = "FOREX_MAJORS"
        if cache_key in self._cache:
            c_time, c_val = self._cache[cache_key]
            if (now_t - c_time) < self.cache_ttl:
                return dict(c_val)

        try:
            import MetaTrader5 as mt5
            from jarvis.data.broker_symbols import resolve_broker_symbol

            eur_sym = resolve_broker_symbol("EURUSD")
            gbp_sym = resolve_broker_symbol("GBPUSD")

            if not eur_sym or not gbp_sym:
                res = {
                    "smt_active": False,
                    "smt_type": "NONE",
                    "score_bonus": 0.0,
                    "leader": "NONE",
                    "description": "Forex peer unmapped at broker"
                }
                self._cache[cache_key] = (now_t, res)
                return res

            r_eur = mt5.copy_rates_from_pos(eur_sym, mt5.TIMEFRAME_M15, 0, 32)
            r_gbp = mt5.copy_rates_from_pos(gbp_sym, mt5.TIMEFRAME_M15, 0, 32)

            if r_eur is None or r_gbp is None or len(r_eur) < 15 or len(r_gbp) < 15:
                r_eur = mt5.copy_rates_from_pos(eur_sym, mt5.TIMEFRAME_H1, 0, 32)
                r_gbp = mt5.copy_rates_from_pos(gbp_sym, mt5.TIMEFRAME_H1, 0, 32)

            if r_eur is None or r_gbp is None or len(r_eur) < 15 or len(r_gbp) < 15:
                res = {"smt_active": False, "smt_type": "NONE", "score_bonus": 0.0, "leader": "NONE", "description": "Rates unavailable"}
                self._cache[cache_key] = (now_t, res)
                return res

            df_eur = pd.DataFrame(r_eur)
            df_gbp = pd.DataFrame(r_gbp)

            if "time" in df_eur.columns and "time" in df_gbp.columns and len(df_eur) > 0 and len(df_gbp) > 0:
                ts_eur = int(df_eur["time"].iloc[-1])
                ts_gbp = int(df_gbp["time"].iloc[-1])
                if abs(ts_eur - ts_gbp) > 7200:
                    res = {
                        "smt_active": False,
                        "smt_type": "NONE",
                        "score_bonus": 0.0,
                        "leader": "NONE",
                        "description": "Secondary FX pair out of sync or market closed"
                    }
                    self._cache[cache_key] = (now_t, res)
                    return res

            eur_h, eur_l = _get_swings(df_eur)
            gbp_h, gbp_l = _get_swings(df_gbp)

            smt_type, leader, score_bonus, desc = _evaluate_smt_pair(
                eur_h, eur_l, gbp_h, gbp_l, "EURUSD", "GBPUSD"
            )

            res = {
                "smt_active": (smt_type != "NONE"),
                "smt_type": smt_type,
                "leader": leader,
                "score_bonus": score_bonus,
                "description": desc
            }
            self._cache[cache_key] = (now_t, res)
            return res
        except Exception as e:
            res = {"smt_active": False, "smt_type": "NONE", "score_bonus": 0.0, "leader": "NONE", "description": str(e)}
            self._cache[cache_key] = (now_t, res)
            return res


class CrossMarketSMTEngine:
    """Unified cross-market SMT divergence dispatcher covering Metals, Indices, and Forex."""
    def __init__(self, cache_ttl_seconds: float = 15.0):
        self.metals_engine = MetalsSMTEngine(cache_ttl_seconds)
        self.indices_engine = IndicesSMTEngine(cache_ttl_seconds)
        self.forex_engine = ForexSMTEngine(cache_ttl_seconds)

    def detect_smt(self, symbol: str) -> Dict[str, Any]:
        s = (symbol or "").upper()
        if MetalsSMTEngine.is_metal(s):
            return self.metals_engine.detect_smt(s)
        elif IndicesSMTEngine.is_index(s):
            return self.indices_engine.detect_smt(s)
        elif ForexSMTEngine.is_fx_major(s):
            return self.forex_engine.detect_smt(s)
        return {
            "smt_active": False,
            "smt_type": "NONE",
            "score_bonus": 0.0,
            "leader": "NONE",
            "description": "No SMT pair configured for asset"
        }


# Global singleton
GLOBAL_SMT_ENGINE = CrossMarketSMTEngine(cache_ttl_seconds=15.0)
