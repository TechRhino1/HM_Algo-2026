"""
Test Suite: Direction Resolution and Devil's Advocate Synchronization (Phase 1).
Verifies that Orchestrator and DecisionEngine share canonical direction resolution,
and Devil's Advocate adversarial reports evaluate the exact trade bias being executed.
"""
from datetime import datetime, timezone
import pytest
from jarvis.data.schemas import (
    MarketContext,
    StructureContext,
    MomentumContext,
    VolatilityContext,
    LiquidityContext,
    SessionContext,
    RegimeOutput,
    MarketRegime,
)
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.analysts.devil_advocate import DevilAdvocateAnalyst


def _create_mock_context(bias="BEARISH", trend_score=-45.0, price=2000.0):
    st = StructureContext(
        bias=bias,
        bos=True,
        bos_type=bias,
        choch=False,
        demand_zone=(price - 25.0, price - 20.0),
        supply_zone=(price + 20.0, price + 25.0),
        discount_premium_zone="PREMIUM" if bias == "BEARISH" else "DISCOUNT",
    )
    mom = MomentumContext(
        trend_score=trend_score,
        rsi=45.0,
        adx=28.0,
        plus_di=15.0 if bias == "BEARISH" else 30.0,
        minus_di=30.0 if bias == "BEARISH" else 15.0,
    )
    vol = VolatilityContext(atr=5.0)
    liq = LiquidityContext(sweep_detected=False)
    sess = SessionContext(current_session="LONDON", is_prime_session=True)
    
    return MarketContext(
        symbol="EURUSD",
        timestamp=datetime.now(timezone.utc),
        current_price=price,
        bid=price - 0.1,
        ask=price + 0.1,
        structure=st,
        momentum=mom,
        volatility=vol,
        liquidity=liq,
        session=sess,
        mtf_alignment={"D1": bias, "H4": bias, "H1": bias, "M15": bias, "M5": bias},
        mtf_confluence_score=-35.0 if bias == "BEARISH" else 35.0,
    )


def test_resolve_direction_canonical_resolution():
    engine = DecisionEngine()
    context = _create_mock_context(bias="BEARISH", trend_score=-45.0)
    regime = RegimeOutput(
        primary_regime=MarketRegime.TREND_BEAR,
        probabilities={"TREND_BEAR": 0.85},
        confidence=0.85,
    )

    # Must resolve cleanly without analyst reports
    resolved = engine.resolve_direction(context=context, regime=regime, trade_style="DAY_TRADING")
    assert resolved == "SELL", f"Expected SELL, got {resolved}"


def test_devils_advocate_evaluates_exact_resolved_bias():
    analyst = DevilAdvocateAnalyst()
    context = _create_mock_context(bias="BEARISH", trend_score=-45.0, price=2000.0)
    regime = RegimeOutput(
        primary_regime=MarketRegime.TREND_BEAR,
        probabilities={"TREND_BEAR": 0.85},
        confidence=0.85,
    )

    engine = DecisionEngine()
    resolved_bias = engine.resolve_direction(context=context, regime=regime, trade_style="DAY_TRADING")
    assert resolved_bias == "SELL"

    # Critiquing SELL must check counter_bias = "BULLISH" and look for support threat level below current price
    report = analyst.critique_opportunity(context=context, regime=regime, proposed_bias=resolved_bias)
    assert report.counter_bias == "BULLISH"
    assert report.threat_price_level <= context.current_price
