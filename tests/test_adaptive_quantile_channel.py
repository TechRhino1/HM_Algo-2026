"""
Tests for Adaptive Volatility Pivots, Asymmetric Quantile Dynamic Channels (AQDC),
and Recency-Weighted Micro Regression integrated from Trend Channel Navigator principles.
"""
import numpy as np
import pandas as pd
import pytest

from jarvis.data.schemas import MarketContext, StructureContext, MomentumContext, VolatilityContext, LiquidityContext, SessionContext
from jarvis.market.market_structure import MarketStructureEngine
from jarvis.market.momentum import MomentumEngine
from jarvis.intelligence.ai_dissector import AIDissector
from jarvis.intelligence.institutional_entry_engine import InstitutionalEntryEngine


def _generate_synthetic_candles(n: int = 100, trend: float = 0.5, noise: float = 0.2):
    np.random.seed(42)
    closes = [100.0]
    for i in range(1, n):
        step = trend + np.random.normal(0, noise)
        closes.append(closes[-1] + step)
    
    closes = np.array(closes)
    highs = closes + np.random.uniform(0.1, 0.5, size=n)
    lows = closes - np.random.uniform(0.1, 0.5, size=n)
    opens = closes - np.random.normal(0, 0.2, size=n)
    
    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.random.randint(100, 1000, size=n),
        "time": list(range(n))
    })
    return df


def test_adaptive_volatility_lookback_and_aqdc():
    df = _generate_synthetic_candles(n=80, trend=0.3, noise=0.1)
    engine = MarketStructureEngine(pivot_window=5, adaptive_window=True)
    ctx = engine.analyze_structure(df)

    assert isinstance(ctx, StructureContext)
    # Check adaptive window
    assert ctx.adaptive_pivot_window >= 3
    # Check channel metrics
    assert ctx.channel_upper >= ctx.channel_basis
    assert ctx.channel_basis >= ctx.channel_lower
    assert 0.0 <= ctx.channel_position_pct <= 100.0
    assert ctx.channel_quarter in ("LOWER_QUARTER", "MIDDLE", "UPPER_QUARTER")


def test_recency_weighted_micro_regression():
    df_uptrend = _generate_synthetic_candles(n=30, trend=1.0, noise=0.05)
    momentum_engine = MomentumEngine()
    ctx_up = momentum_engine.analyze_momentum(df_uptrend)

    assert isinstance(ctx_up, MomentumContext)
    assert ctx_up.micro_r2 >= 0.50, f"Expected high R2 for clean uptrend, got {ctx_up.micro_r2}"
    assert ctx_up.micro_slope_angle > 0.0, f"Expected positive angle, got {ctx_up.micro_slope_angle}"
    assert ctx_up.is_statistically_trending is True

    # Chop/flat dataframe
    df_flat = _generate_synthetic_candles(n=30, trend=0.0, noise=0.5)
    ctx_flat = momentum_engine.analyze_momentum(df_flat)
    assert ctx_flat.micro_r2 < 0.60 or abs(ctx_flat.micro_slope_angle) < 20.0


def test_ai_dissector_channel_and_micro_momentum_confluence():
    dissector = AIDissector()

    # Context with favorable lower quarter pullback in a bullish bias
    struct_favorable = StructureContext(
        bias="BULLISH",
        bos=True,
        channel_quarter="LOWER_QUARTER",
        channel_position_pct=20.0
    )
    mom_trending = MomentumContext(
        trend_score=35,
        adx=28.0,
        is_statistically_trending=True,
        micro_r2=0.85
    )

    ctx_fav = MarketContext(
        symbol="EURUSD",
        timestamp=pd.Timestamp.now(),
        current_price=1.1000,
        bid=1.0999,
        ask=1.1001,
        structure=struct_favorable,
        momentum=mom_trending,
        liquidity=LiquidityContext(sweep_detected=True, sweep_magnitude=1.2),
        volatility=VolatilityContext(state="NORMAL"),
        session=SessionContext()
    )

    report_fav = dissector.dissect(ctx_fav, None, rr_ratio=2.5, ev=1.2, ai_score=75.0, calibrated_win_p=0.68)
    scores_fav = report_fav["scores"]
    assert scores_fav["structure"] >= 8
    assert scores_fav["momentum"] >= 10

    # Context with middle quarter / non trending
    struct_neutral = StructureContext(
        bias="NEUTRAL",
        bos=False,
        channel_quarter="MIDDLE",
        channel_position_pct=50.0
    )
    mom_chop = MomentumContext(
        trend_score=10,
        adx=15.0,
        is_statistically_trending=False,
        micro_r2=0.10
    )
    ctx_neu = MarketContext(
        symbol="EURUSD",
        timestamp=pd.Timestamp.now(),
        current_price=1.1000,
        bid=1.0999,
        ask=1.1001,
        structure=struct_neutral,
        momentum=mom_chop,
        liquidity=LiquidityContext(sweep_detected=False),
        volatility=VolatilityContext(state="NORMAL"),
        session=SessionContext()
    )
    report_neu = dissector.dissect(ctx_neu, None, rr_ratio=2.5, ev=0.5, ai_score=50.0, calibrated_win_p=0.52)
    scores_neu = report_neu["scores"]
    assert scores_fav["structure"] > scores_neu["structure"]
    assert scores_fav["momentum"] > scores_neu["momentum"]


def test_institutional_entry_engine_records_channel_details():
    df_m5 = _generate_synthetic_candles(n=50, trend=0.2, noise=0.1)
    df_m1 = _generate_synthetic_candles(n=50, trend=0.2, noise=0.05)
    
    struct = MarketStructureEngine().analyze_structure(df_m5)
    mom = MomentumEngine().analyze_momentum(df_m5)
    
    last_close = float(df_m5["close"].iloc[-1])
    ctx = MarketContext(
        symbol="EURUSD",
        timestamp=pd.Timestamp.now(),
        current_price=last_close,
        bid=round(last_close - 0.0001, 5),
        ask=round(last_close + 0.0001, 5),
        structure=struct,
        momentum=mom,
        liquidity=LiquidityContext(sweep_detected=True),
        volatility=VolatilityContext(atr=0.0015, state="NORMAL"),
        session=SessionContext()
    )
    
    entry_engine = InstitutionalEntryEngine()
    result = entry_engine.calculate_entry_and_levels(
        context=ctx,
        regime=None,
        tentative_bias="BUY",
        trade_style="SCALP",
        mtf_data={"M5": df_m5, "M1": df_m1}
    )

    details = result["protocol_details"]
    assert "channel_quarter" in details
    assert "channel_position_pct" in details
    assert "micro_r2" in details
    assert details["channel_quarter"] in ("LOWER_QUARTER", "MIDDLE", "UPPER_QUARTER")
    assert "as_limit_price" in result
    assert "as_reservation_price" in result
    assert "as_limit_price" in details
