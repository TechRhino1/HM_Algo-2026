"""
HM Algo 2.0 — Multi-Timeframe Market Context Synthesizer.
Orchestrates Market Structure, Liquidity, Volatility, Momentum, and Session intelligence across multiple timeframes.
"""
from datetime import datetime, timezone
from typing import Dict, Optional
import math
import pandas as pd

from jarvis.data.schemas import MarketContext
from jarvis.market.market_structure import MarketStructureEngine
from jarvis.market.liquidity import LiquidityEngine
from jarvis.market.volatility import VolatilityEngine
from jarvis.market.momentum import MomentumEngine
from jarvis.market.sessions import SessionEngine
from jarvis.intelligence.order_flow import InstitutionalVolumeOrderFlowEngine

class MarketContextEngine:
    def __init__(
        self,
        structure_engine: Optional[MarketStructureEngine] = None,
        liquidity_engine: Optional[LiquidityEngine] = None,
        volatility_engine: Optional[VolatilityEngine] = None,
        momentum_engine: Optional[MomentumEngine] = None,
        order_flow_engine: Optional[InstitutionalVolumeOrderFlowEngine] = None
    ):
        self.structure_engine = structure_engine or MarketStructureEngine()
        self.liquidity_engine = liquidity_engine or LiquidityEngine()
        self.volatility_engine = volatility_engine or VolatilityEngine()
        self.momentum_engine = momentum_engine or MomentumEngine()
        self.order_flow_engine = order_flow_engine or InstitutionalVolumeOrderFlowEngine()

    @staticmethod
    def _live_spread_pips_from_frame(df: pd.DataFrame, spec) -> Optional[float]:
        """MT5 points → pips, using the canonical conversion.

        ``pips = spread_points * 10**-digits / pip_size`` — the same formula the
        backtest scan uses (``backtesting/signal_scan.py:170-180``). Returns
        ``None`` when the frame carries no ``spread`` column, the last value is
        non-finite or non-positive, or the spec's ``pip_size``/``digits`` are
        unusable — so callers can tell "measured" from "not measured".
        """
        if df is None or getattr(df, "empty", True) or "spread" not in df.columns:
            return None
        try:
            raw = float(df["spread"].iloc[-1])
        except (TypeError, ValueError, IndexError):
            return None
        if not math.isfinite(raw) or raw <= 0:
            return None
        try:
            pip = float(getattr(spec, "pip_size", 0.0) or 0.0)
            digits = int(getattr(spec, "digits", 0) or 0)
        except (TypeError, ValueError):
            return None
        if pip <= 0 or digits <= 0:
            return None
        pips = raw * (10.0 ** -digits) / pip
        return pips if math.isfinite(pips) else None

    def build_context(
        self,
        symbol: str,
        mtf_data: Dict[str, pd.DataFrame],
        current_spread_pips: float = 2.0,
        max_allowed_spread_pips: float = 35.0,
        trade_style: str = "SWING",
        live_spread_pips: Optional[float] = None
    ) -> MarketContext:
        """
        Synthesizes multi-timeframe market data into a unified MarketContext object.
        Dynamically weights MTF confluence based on trade style:
        - SWING: D1 (40%), H4 (30%), H1 (20%), M15 (10%)
        - DAY_TRADING / INTRADAY: H4 (40%), H1 (35%), M15 (25%)
        - SCALP: H1 (40%), M15 (30%), M5 (20%), M1 (10%)

        The DAY_TRADING row previously read "H1 (40%), M15 (35%), M5 (25%)",
        which named the wrong frames: `df_macro` resolves to the **H4** frame
        for this style, and M5 is written into `mtf_alignment` but carries no
        weight. Corrected 2026-09-18 — the code was always self-consistent.
        """
        style = (trade_style or "SWING").upper()
        if style in ("DAY_TRADING", "INTRADAY", "DAY"):
            df_macro = mtf_data.get("macro", mtf_data.get("H4", pd.DataFrame()))
            df_context = mtf_data.get("context", mtf_data.get("H1", pd.DataFrame()))
            df_primary = mtf_data.get("primary", mtf_data.get("M15", pd.DataFrame()))
            df_setup = mtf_data.get("setup", mtf_data.get("H1", pd.DataFrame()))
            df_timing = mtf_data.get("timing", mtf_data.get("M5", pd.DataFrame()))
        elif style == "SCALP":
            df_macro = mtf_data.get("macro", mtf_data.get("H1", pd.DataFrame()))
            df_context = mtf_data.get("context", mtf_data.get("M15", pd.DataFrame()))
            df_primary = mtf_data.get("primary", mtf_data.get("M5", pd.DataFrame()))
            df_setup = mtf_data.get("setup", mtf_data.get("M5", pd.DataFrame()))
            df_timing = mtf_data.get("timing", mtf_data.get("M1", pd.DataFrame()))
        else:  # SWING (default)
            df_macro = mtf_data.get("macro", mtf_data.get("D1", pd.DataFrame()))
            df_context = mtf_data.get("context", mtf_data.get("H4", pd.DataFrame()))
            df_primary = mtf_data.get("primary", mtf_data.get("H1", pd.DataFrame()))
            df_setup = mtf_data.get("setup", mtf_data.get("H4", mtf_data.get("H1", pd.DataFrame())))
            df_timing = mtf_data.get("timing", mtf_data.get("M15", mtf_data.get("M5", pd.DataFrame())))

        if df_primary.empty:
            df_primary = list(mtf_data.values())[0] if mtf_data else pd.DataFrame()

        from jarvis.data.symbol_registry import resolve as _resolve_sym
        _spec = _resolve_sym(symbol)

        # Reporting only. `data_feed.fetch_rates` keeps MT5's per-bar `spread`
        # column (in **points**); convert to pips with the canonical formula the
        # backtest already uses (backtesting/signal_scan.py:170-180):
        #     pips = spread_points * 10**-digits / pip_size
        # This never feeds bid/ask or the `current_spread_pips` handed to the
        # volatility engine, so no gate, stop or size moves.
        if live_spread_pips is None:
            live_spread_pips = self._live_spread_pips_from_frame(df_primary, _spec)

        latest_close = float(df_primary["close"].iloc[-1]) if not df_primary.empty else 0.0
        bid = latest_close
        ask = latest_close + (current_spread_pips * _spec.pip_size)

        # 1. Structural Analysis (Primary & Setup timeframes)
        structure_primary = self.structure_engine.analyze_structure(df_primary)
        
        # 2. Liquidity & Sweep Analysis
        liquidity = self.liquidity_engine.analyze_liquidity(df_primary)

        # 3. Volatility & Spread Feasibility
        volatility = self.volatility_engine.analyze_volatility(
            df_primary,
            current_spread_pips=current_spread_pips,
            max_allowed_spread_pips=max_allowed_spread_pips
        )

        # 4. Multi-Factor Momentum Analysis
        momentum = self.momentum_engine.analyze_momentum(df_primary)

        # 5. Session Timing Context (Use bar timestamp if available, else current wall time)
        bar_timestamp = datetime.now(timezone.utc)
        ts_col = "time" if "time" in df_primary.columns else ("timestamp" if "timestamp" in df_primary.columns else None)
        if not df_primary.empty and ts_col:
            raw_ts = df_primary[ts_col].iloc[-1]
            if isinstance(raw_ts, datetime):
                bar_timestamp = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=timezone.utc)
            elif isinstance(raw_ts, pd.Timestamp):
                bar_timestamp = raw_ts.to_pydatetime()
                if not bar_timestamp.tzinfo:
                    bar_timestamp = bar_timestamp.replace(tzinfo=timezone.utc)
            elif isinstance(raw_ts, (int, float)):
                bar_timestamp = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
        elif not df_primary.empty and isinstance(df_primary.index, pd.DatetimeIndex):
            raw_ts = df_primary.index[-1]
            bar_timestamp = raw_ts.to_pydatetime()
            if not bar_timestamp.tzinfo:
                bar_timestamp = bar_timestamp.replace(tzinfo=timezone.utc)

        session = SessionEngine.get_current_session(bar_timestamp)

        # 6. Multi-Timeframe Alignment Matrix & Dynamic Top-Down Weighted Score
        mtf_alignment = {}
        def _score_bias(b: str) -> float:
            return 1.0 if b == "BULLISH" else (-1.0 if b == "BEARISH" else 0.0)

        def _refine_bias_with_ema(df: pd.DataFrame, bias: str) -> str:
            if df is not None and not df.empty and len(df) >= 20 and "close" in df.columns:
                c = df["close"]
                c_last = float(c.iloc[-1])
                ema20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
                ema50 = float(c.ewm(span=min(50, len(df)), adjust=False).mean().iloc[-1])
                if c_last > ema20 > ema50 and bias == "BEARISH":
                    return "BULLISH" if (c_last > ema20 * 1.002) else "NEUTRAL"
                elif c_last < ema20 < ema50 and bias == "BULLISH":
                    return "BEARISH" if (c_last < ema20 * 0.998) else "NEUTRAL"
            return bias

        if style in ("DAY_TRADING", "INTRADAY", "DAY"):
            # When trade_style == "DAY_TRADING": H4 (40%), H1 (35%), M15 (25%)
            raw_h4 = self.structure_engine.analyze_structure(df_macro).bias if not df_macro.empty else "NEUTRAL"
            raw_h1 = self.structure_engine.analyze_structure(df_context).bias if not df_context.empty else "NEUTRAL"
            raw_m15 = structure_primary.bias if not df_primary.empty else "NEUTRAL"
            raw_m5 = self.structure_engine.analyze_structure(df_timing).bias if not df_timing.empty else "NEUTRAL"

            h4_bias = _refine_bias_with_ema(df_macro, raw_h4)
            h1_bias = _refine_bias_with_ema(df_context, raw_h1)
            m15_bias = _refine_bias_with_ema(df_primary, raw_m15)
            m5_bias = _refine_bias_with_ema(df_timing, raw_m5)

            mtf_alignment["H4"] = h4_bias
            mtf_alignment["H1"] = h1_bias
            mtf_alignment["M15"] = m15_bias
            mtf_alignment["M5"] = m5_bias

            weighted_score = (
                _score_bias(h4_bias) * 0.40 +
                _score_bias(h1_bias) * 0.35 +
                _score_bias(m15_bias) * 0.25
            )
        elif style == "SCALP":
            raw_h1 = self.structure_engine.analyze_structure(df_macro).bias if not df_macro.empty else "NEUTRAL"
            raw_m15 = self.structure_engine.analyze_structure(df_context).bias if not df_context.empty else "NEUTRAL"
            raw_m5 = structure_primary.bias if not df_primary.empty else "NEUTRAL"
            raw_m1 = self.structure_engine.analyze_structure(df_timing).bias if not df_timing.empty else "NEUTRAL"

            h1_bias = _refine_bias_with_ema(df_macro, raw_h1)
            m15_bias = _refine_bias_with_ema(df_context, raw_m15)
            m5_bias = _refine_bias_with_ema(df_primary, raw_m5)
            m1_bias = _refine_bias_with_ema(df_timing, raw_m1)

            mtf_alignment["H1"] = h1_bias
            mtf_alignment["M15"] = m15_bias
            mtf_alignment["M5"] = m5_bias
            mtf_alignment["M1"] = m1_bias

            weighted_score = (
                _score_bias(h1_bias) * 0.40 +
                _score_bias(m15_bias) * 0.30 +
                _score_bias(m5_bias) * 0.20 +
                _score_bias(m1_bias) * 0.10
            )
        else:  # SWING (default): D1 (40%), H4 (30%), H1 (20%), M15 (10%)
            raw_d1 = self.structure_engine.analyze_structure(df_macro).bias if not df_macro.empty else "NEUTRAL"
            raw_h4 = self.structure_engine.analyze_structure(df_context).bias if not df_context.empty else "NEUTRAL"
            raw_h1 = structure_primary.bias if not df_primary.empty else "NEUTRAL"
            raw_m15 = (
                self.structure_engine.analyze_structure(df_timing).bias if not df_timing.empty else (
                    self.structure_engine.analyze_structure(df_setup).bias if not df_setup.empty else "NEUTRAL"
                )
            )

            d1_bias = _refine_bias_with_ema(df_macro, raw_d1)
            h4_bias = _refine_bias_with_ema(df_context, raw_h4)
            h1_bias = _refine_bias_with_ema(df_primary, raw_h1)
            m15_bias = _refine_bias_with_ema(df_timing if not df_timing.empty else df_setup, raw_m15)

            mtf_alignment["D1"] = d1_bias
            mtf_alignment["H4"] = h4_bias
            mtf_alignment["H1"] = h1_bias
            mtf_alignment["M15"] = m15_bias

            weighted_score = (
                _score_bias(d1_bias) * 0.40 +
                _score_bias(h4_bias) * 0.30 +
                _score_bias(h1_bias) * 0.20 +
                _score_bias(m15_bias) * 0.10
            )

        mtf_confluence_pct = round(weighted_score * 100.0, 1)

        # 7. VWAP Calculation
        vwap = 0.0
        if not df_primary.empty and "volume" in df_primary.columns and "high" in df_primary.columns:
            typical_price = (df_primary["high"] + df_primary["low"] + df_primary["close"]) / 3
            vol = df_primary["volume"]
            if vol.sum() > 0:
                vwap_series = (typical_price * vol).cumsum() / vol.cumsum()
                vwap = float(vwap_series.iloc[-1]) if not vwap_series.isna().all() else 0.0

        # 8. Order Flow & Footprint Delta Analysis (E1)
        order_flow = self.order_flow_engine.analyze_order_flow(df_primary)

        # 9. Context Quality Score
        quality = 0.0
        available_tfs = sum(1 for d in [df_macro, df_context, df_primary, df_setup, df_timing] if not d.empty)
        quality += (available_tfs / 5.0) * 50.0
        if not df_primary.empty and len(df_primary) >= 50:
            quality += 30.0
        if not df_context.empty and len(df_context) >= 20:
            quality += 20.0
        context_quality = min(100.0, quality)

        return MarketContext(
            symbol=symbol,
            timestamp=bar_timestamp,
            current_price=latest_close,
            bid=bid,
            ask=ask,
            vwap=round(vwap, 4),
            context_quality=round(context_quality, 1),
            structure=structure_primary,
            liquidity=liquidity,
            volatility=volatility,
            momentum=momentum,
            session=session,
            mtf_confluence_score=mtf_confluence_pct,
            mtf_alignment=mtf_alignment,
            order_flow=order_flow,
            trade_style=style,
            live_spread_pips=live_spread_pips
        )
