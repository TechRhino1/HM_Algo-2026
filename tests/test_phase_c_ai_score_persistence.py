"""
Test Suite for Phase C: News & AI Integrity.
Verifies:
1. DecisionObject contains ai_score attribute and serializes it in to_dict().
2. TRADE_DB.log_trade persists ai_score in executed_trades table and recovers it accurately.
3. MacroAnalyst filters out synthetic fallback news items (is_fallback=True).
"""
import pytest
import sqlite3
from datetime import datetime, timezone
from jarvis.data.schemas import DecisionObject, RegimeOutput, MarketRegime, TradeQualityGateResult
from jarvis.data.database import SQLiteTradeDB
from jarvis.analysts.macro_analyst import MacroAnalyst


def test_decision_object_ai_score_attribute_and_dict():
    """Verify DecisionObject stores and serializes ai_score."""
    regime = RegimeOutput(
        primary_regime=MarketRegime.TREND_BULL,
        probabilities={"TREND_BULL": 0.8},
        confidence=0.8,
        timestamp=datetime.now(timezone.utc)
    )
    gate = TradeQualityGateResult(passed=True, checks={"R:R >= 1.5": True})
    dec = DecisionObject(
        symbol="XAUUSD",
        timestamp=datetime.now(timezone.utc),
        regime=regime,
        bias="BUY",
        probabilities={"BUY": 0.70, "SELL": 0.30},
        strategy="MOMENTUM_EXPANSION",
        entry_price=2650.0,
        stop_loss=2640.0,
        take_profit=2670.0,
        risk_reward_ratio=2.0,
        calculated_risk_percent=1.0,
        expected_value=1.8,
        model_confidence=0.62,
        ai_score=84.5,
        adversarial_penalty=4.0,
        invalidation_levels=[],
        bull_case=["Gold trend"],
        bear_case=[],
        risk_factors=[],
        quality_gate=gate,
        decision="EXECUTE"
    )
    assert dec.ai_score == 84.5
    d = dec.to_dict()
    assert d.get("ai_score") == 84.5


def test_trade_db_ai_score_persistence(tmp_path):
    """Verify TRADE_DB writes and reads ai_score without distortion."""
    db_file = str(tmp_path / "test_trades.db")
    db = SQLiteTradeDB(db_path=db_file)
    
    # Log a trade with explicit ai_score=78.5
    db.log_trade(
        ticket=999001,
        symbol="XAUUSD",
        action="BUY",
        entry=2650.50,
        sl=2640.00,
        tp=2670.00,
        volume=0.10,
        score=78.5,
        regime="TREND_BULL",
        ev=1.50,
        executor="BOT (AI)",
        origin="broker",
        execution_mode="live"
    )

    trades = db.fetch_recent_trades(limit=10)
    assert len(trades) >= 1
    t = [x for x in trades if x.get("ticket") == 999001][0]
    assert t["ai_score"] == 78.5
    assert t["symbol"] == "XAUUSD"
    assert t["action"] == "BUY"


def test_macro_analyst_filters_fabricated_events():
    """Verify MacroAnalyst filters out is_fallback=True events."""
    from jarvis.data.schemas import MarketContext, StructureContext, LiquidityContext, VolatilityContext, MomentumContext, SessionContext
    
    ctx = MarketContext(
        symbol="XAUUSD",
        timestamp=datetime.now(timezone.utc),
        current_price=2650.0,
        bid=2649.8,
        ask=2650.2,
        structure=StructureContext(bias="BULLISH"),
        liquidity=LiquidityContext(),
        volatility=VolatilityContext(),
        momentum=MomentumContext(),
        session=SessionContext(is_prime_session=True, current_session="LONDON")
    )
    regime = RegimeOutput(
        primary_regime=MarketRegime.TREND_BULL,
        probabilities={"TREND_BULL": 0.8},
        confidence=0.8,
        timestamp=datetime.now(timezone.utc)
    )

    calendar_with_fallback = [
        {"currency": "USD", "impact": "HIGH", "event": "Non-Farm Payrolls", "actual": "Upcoming", "forecast": "150K", "is_fallback": True},
        {"currency": "USD", "impact": "HIGH", "event": "CPI MoM", "actual": "0.4%", "forecast": "0.2%", "is_fallback": False}
    ]
    
    analyst = MacroAnalyst(news_calendar=calendar_with_fallback)
    report = analyst.analyze(ctx, regime)
    
    # Check risk_factors mentions that synthetic event was excluded
    assert any("synthetic and were excluded from scoring" in r for r in report.risk_factors)
    # The real event (CPI higher -> USD stronger -> bearish gold) should be scored
    assert report.bias == "BEARISH"
