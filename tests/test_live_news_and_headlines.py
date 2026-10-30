"""Unit test suite for Live Breaking Headlines Wire, Sentiment NLP, and At-Risk News Sentinel."""
import pytest
from unittest.mock import MagicMock, patch
from jarvis.market.news import LiveNewsEngine, GLOBAL_NEWS_ENGINE


def test_analyze_headline_sentiment_bullish():
    """Verify that hawkish / strong economic headlines score BULLISH with correct currencies."""
    h = "Australian Dollar surges as US Dollar rallies following robust employment beat"
    res = LiveNewsEngine.analyze_headline_sentiment(h)
    assert res["sentiment"] == "BULLISH"
    assert res["sentiment_score"] > 0
    assert "USD" in res["affected_currencies"]
    assert "AUD" in res["affected_currencies"]
    assert "FOREX" in res["affected_assets"]


def test_analyze_headline_sentiment_bearish():
    """Verify that dovish / contractionary headlines score BEARISH."""
    h = "UK growth forecasts cut as economic slump deepens ahead of rate cut"
    res = LiveNewsEngine.analyze_headline_sentiment(h)
    assert res["sentiment"] == "BEARISH"
    assert res["sentiment_score"] < 0
    assert "GBP" in res["affected_currencies"]


def test_analyze_headline_sentiment_crypto_breaking():
    """Verify crypto classification and urgency tagging."""
    h = "BREAKING: SEC approves Ethereum staking ETF application as Bitcoin jumps"
    res = LiveNewsEngine.analyze_headline_sentiment(h)
    assert res["urgency"] == "BREAKING"
    assert "CRYPTO" in res["affected_assets"]
    assert res["sentiment"] == "BULLISH"


def test_analyze_headline_sentiment_gold():
    """Verify gold commodity classification."""
    h = "Gold prices surge to all-time highs on geopolitical safe-haven demand"
    res = LiveNewsEngine.analyze_headline_sentiment(h)
    assert "COMMODITY" in res["affected_assets"]
    assert "USD" in res["affected_currencies"]
    assert res["sentiment"] == "BULLISH"


def test_at_risk_positions_cross_referencing():
    """Verify open positions are correctly categorized under News Sentinel rules."""
    engine = LiveNewsEngine()
    
    # Mock calendar with a High-Impact USD release in 12 minutes (720 seconds)
    mock_calendar = [
        {
            "event": "US Non-Farm Payrolls",
            "currency": "USD",
            "impact": "HIGH",
            "diff_seconds": 720.0,
            "is_past": False,
            "affected_pairs": ["XAUUSD", "EURUSD", "USDJPY", "GBPUSD"]
        }
    ]

    # Position 1: Runner reaching banking threshold (peak 0.95R, current 0.70R)
    # Position 2: Unconfirmed position (peak 0.30R, current 0.10R)
    # Position 3: Breakeven locked position
    mock_positions = [
        {
            "ticket": 201,
            "symbol": "XAUUSD",
            "direction": "BUY",
            "volume": 0.05,
            "profit": 120.0,
            "current_r": 0.70,
            "mfe": 0.95,
            "is_be_locked": False
        },
        {
            "ticket": 202,
            "symbol": "EURUSD",
            "direction": "SELL",
            "volume": 0.10,
            "profit": 15.0,
            "current_r": 0.10,
            "mfe": 0.30,
            "is_be_locked": False
        },
        {
            "ticket": 203,
            "symbol": "USDJPY",
            "direction": "BUY",
            "volume": 0.08,
            "profit": 50.0,
            "current_r": 0.40,
            "mfe": 0.50,
            "is_be_locked": True
        }
    ]

    with patch.object(engine, "get_news_calendar", return_value=mock_calendar):
        at_risk = engine.get_at_risk_positions(mock_positions, lookforward_minutes=30)
        assert len(at_risk) == 3

        # Ticket 201 should be AUTO_BANK_PROFIT (Peak >= 0.60R, Current >= 0.20R)
        p1 = next(p for p in at_risk if p["ticket"] == 201)
        assert p1["sentinel_action"] == "AUTO_BANK_PROFIT"
        assert p1["catalyst_event"] == "US Non-Farm Payrolls"
        assert p1["catalyst_countdown_min"] == 12

        # Ticket 202 should be RATCHET_PROTECT_FLOOR (Current < 0.60R)
        p2 = next(p for p in at_risk if p["ticket"] == 202)
        assert p2["sentinel_action"] == "RATCHET_PROTECT_FLOOR"

        # Ticket 203 should be BE_PROTECTED (is_be_locked=True)
        p3 = next(p for p in at_risk if p["ticket"] == 203)
        assert p3["sentinel_action"] == "BE_PROTECTED"


def test_imminent_event_detection():
    """Verify closest High-Impact event within 30 min is returned."""
    engine = LiveNewsEngine()
    mock_calendar = [
        {
            "event": "US Consumer Price Index (CPI)",
            "currency": "USD",
            "impact": "HIGH",
            "diff_seconds": 900.0,  # 15 minutes away
            "forecast": "3.1%",
            "previous": "3.2%",
            "actual": "",
            "affected_pairs": ["XAUUSD", "EURUSD"]
        },
        {
            "event": "German Retail Sales",
            "currency": "EUR",
            "impact": "MEDIUM",
            "diff_seconds": 600.0,
            "forecast": "0.2%",
            "previous": "-0.1%",
            "actual": "",
            "affected_pairs": ["EURUSD"]
        }
    ]

    with patch.object(engine, "get_news_calendar", return_value=mock_calendar):
        imm = engine.get_imminent_event(max_minutes=30)
        assert imm is not None
        assert imm["event"] == "US Consumer Price Index (CPI)"
        assert imm["currency"] == "USD"
        assert imm["impact"] == "HIGH"
        assert "15" in imm["status_text"]


def test_unified_macro_state_contract():
    """Verify get_unified_macro_state provides backward-compatible and enriched keys."""
    state = GLOBAL_NEWS_ENGINE.get_unified_macro_state()
    assert "news" in state
    assert "calendar" in state
    assert "headlines" in state
    assert "at_risk_positions" in state
    assert "imminent_event" in state
    assert "source" in state
    assert "timestamp" in state
    assert isinstance(state["headlines"], list)
    assert isinstance(state["calendar"], list)
