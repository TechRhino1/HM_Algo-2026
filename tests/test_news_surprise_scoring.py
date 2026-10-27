"""
Unit tests for Macro news surprise scoring and pre-event buffer detection.
"""
import pytest
from datetime import datetime, timezone, timedelta
from jarvis.data.schemas import (
    MarketContext,
    RegimeOutput,
    MarketRegime,
    SessionContext,
    StructureContext,
    LiquidityContext,
    VolatilityContext,
    MomentumContext
)
from jarvis.analysts.macro_analyst import MacroAnalyst, _parse_metric


def _make_context(symbol="XAUUSD"):
    return MarketContext(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        current_price=2700.0,
        bid=2699.9,
        ask=2700.1,
        structure=StructureContext(bias="NEUTRAL"),
        liquidity=LiquidityContext(),
        volatility=VolatilityContext(),
        momentum=MomentumContext(),
        session=SessionContext(is_prime_session=True)
    )


def test_parse_metric():
    assert _parse_metric("250K") == 250_000.0
    assert _parse_metric("3.5M") == 3_500_000.0
    assert _parse_metric("4.2%") == 4.2
    assert _parse_metric("-1.5B") == -1_500_000_000.0
    assert _parse_metric("") is None


def test_imminent_event_risk_window():
    """Verify that a high-impact event scheduled within 30 minutes triggers the freeze warning."""
    imminent_time = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    cal = [{
        "currency": "USD",
        "impact": "HIGH",
        "event": "Non-Farm Payrolls",
        "actual": "Upcoming",
        "forecast": "180K",
        "timestamp_iso": imminent_time
    }]
    ctx = _make_context()
    reg = RegimeOutput(primary_regime=MarketRegime.RANGE, probabilities={}, confidence=0.8)
    
    rep = MacroAnalyst(news_calendar=cal).analyze(ctx, reg)
    assert any("imminent" in r.lower() for r in rep.risk_factors)
    assert any("freeze window" in r.lower() for r in rep.risk_factors)


def test_quantitative_surprise_direction():
    """Verify positive USD surprise triggers bearish shock on Gold with surprise percentage logged."""
    recent_time = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    cal = [{
        "currency": "USD",
        "impact": "HIGH",
        "event": "US CPI MoM",
        "actual": "0.5%",
        "forecast": "0.2%",
        "timestamp_iso": recent_time
    }]
    ctx = _make_context()
    reg = RegimeOutput(primary_regime=MarketRegime.RANGE, probabilities={}, confidence=0.8)

    rep = MacroAnalyst(news_calendar=cal).analyze(ctx, reg)
    assert rep.bias == "BEARISH"
    assert rep.metadata.get("usd_bull_shock") is True
    assert any("surprise:" in e for e in rep.evidence)
