"""
Tests for CandlestickPriceActionEngine, Enhanced Liquidity Sweep lookback,
and Crypto Breakout Execution through Institutional Quality Gates.
"""
from datetime import datetime, timezone
import pytest
import numpy as np
import pandas as pd

from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext, VolatilityContext,
    MomentumContext, SessionContext, RegimeOutput, MarketRegime, AnalystReport, AnalystRole,
    DevilAdvocateReport
)
from jarvis.market.candlestick_patterns import CandlestickPriceActionEngine
from jarvis.market.liquidity import LiquidityEngine
from jarvis.intelligence.decision_engine import DecisionEngine


def test_pin_bar_hammer_detection():
    engine = CandlestickPriceActionEngine(atr_period=14)
    dates = pd.date_range("2026-10-11 12:00", periods=20, freq="15min")
    df = pd.DataFrame({
        "time": dates,
        "open": [2505.0] * 19 + [2503.0],
        "high": [2507.0] * 19 + [2504.0],
        "low": [2503.0] * 19 + [2498.0],   # lower wick sweeps down to 2498
        "close": [2505.0] * 19 + [2503.8], # closes high at 2503.8
        "volume": [100] * 19 + [250]       # volume surge 2.5x
    })
    res = engine.analyze(df, symbol="ETHUSD")
    assert res["pattern_detected"] is True
    assert res["bias"] == "BUY"
    assert "PIN_BAR_HAMMER" in [p["name"] for p in res["patterns_found"]]
    assert res["volume_confirmed"] is True


def test_bullish_marubozu_breakout_detection():
    engine = CandlestickPriceActionEngine(atr_period=14)
    dates = pd.date_range("2026-10-11 12:00", periods=20, freq="15min")
    df = pd.DataFrame({
        "time": dates,
        "open": [2505.0] * 19 + [2508.0],
        "high": [2507.0] * 19 + [2533.0],
        "low": [2503.0] * 19 + [2508.0],
        "close": [2505.0] * 19 + [2528.0], # massive green expansion bar
        "volume": [100] * 19 + [950]       # 9.5x volume spike
    })
    res = engine.analyze(df, symbol="ETHUSD")
    assert res["pattern_detected"] is True
    assert res["bias"] == "BUY"
    assert "BULLISH_EXPANSION_MARUBOZU" in [p["name"] for p in res["patterns_found"]]


def test_liquidity_sweep_lookback_retention():
    engine = LiquidityEngine()
    highs = [2520.0] * 35
    lows = [2510.0] * 35
    closes = [2515.0] * 35
    opens = [2515.0] * 35
    vols = [100.0] * 35

    # Prior swing low at bar 12, swing high at bar 8 and bar 20
    highs[8] = 2530.0
    highs[20] = 2535.0
    lows[12] = 2500.0
    opens[12] = 2505.0
    closes[12] = 2505.0
    highs[12] = 2510.0

    # Sweep candle at bar 28 (7 bars ago)
    lows[28] = 2497.0
    opens[28] = 2501.0
    closes[28] = 2507.0 # closes back above 2500 with displacement
    highs[28] = 2508.0
    vols[28] = 300.0

    df = pd.DataFrame({
        "high": highs, "low": lows, "close": closes, "open": opens, "volume": vols
    })
    res = engine.analyze_liquidity(df)
    assert res.sweep_detected is True
    assert res.sweep_type == "BULLISH_SWEEP"
    assert res.bars_since_sweep >= 0


def test_decision_engine_crypto_spread_and_breakout_gates():
    engine = DecisionEngine()
    
    ctx = MarketContext(
        symbol="ETHUSD",
        timestamp=datetime.now(timezone.utc),
        current_price=2530.0,
        bid=2528.10,
        ask=2530.00,
        structure=StructureContext(
            bias="BULLISH",
            bos=True,
            bos_type="BULLISH",
            discount_premium_zone="PREMIUM" # breakout is in premium!
        ),
        liquidity=LiquidityContext(
            sweep_detected=True,
            sweep_type="BULLISH_SWEEP",
            sweep_level=2500.0,
            bars_since_sweep=4
        ),
        volatility=VolatilityContext(
            atr=15.0,
            current_spread_pips=190.0, # 190 pips = $1.90 XM ETH spread
            max_allowed_spread_pips=450.0,
            state="EXPANSION"
        ),
        momentum=MomentumContext(
            rsi=62.0,
            adx=28.0,
            trend_score=30
        ),
        session=SessionContext(is_prime_session=True),
        trade_style="DAY_TRADING",
        candlestick_pattern={
            "pattern_detected": True,
            "pattern_name": "BULLISH_EXPANSION_MARUBOZU",
            "bias": "BUY",
            "confidence": 0.85
        }
    )

    regime = RegimeOutput(
        primary_regime=MarketRegime.BREAKOUT,
        probabilities={"BREAKOUT": 0.85},
        confidence=0.85
    )

    devil = DevilAdvocateReport(
        symbol="ETHUSD",
        counter_bias="SELL",
        invalidation_risk_coefficient=0.1,
        penalty_score=5.0,
        threats_detected=[]
    )

    gate_result = engine._apply_quality_gate(
        context=ctx,
        regime=regime,
        devil_report=devil,
        ai_score=82.0,
        rr_ratio=2.2,
        ev=1.5,
        final_win_p=0.62,
        spread=190.0,
        premium_discount_valid=True,
        account_balance=468.51, # small account micro mode
        strategy="MOMENTUM_CONTINUATION",
        tentative_bias="BUY",
        risk_dist=12.0,
        calibrated_win_p=0.60,
        planned_risk_dollars=5.0
    )

    # Spread protection must pass (not clamped to 3.0 pips)
    assert gate_result.checks["Spread Protection"] is True
    # Crypto Macro Trend Filter must pass (recognizes recent sweep & breakout expansion)
    assert gate_result.checks["Crypto Macro Trend Filter"] is True
    assert gate_result.passed is True
