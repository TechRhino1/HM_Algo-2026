"""
Unit tests for India Market Transformation:
- Live Market Overview Cards (NIFTY 50, SENSEX, BANK NIFTY)
- Options Expert Advisor & Options Engine
- Advanced Market Predictions (Intraday, Scalp, Breakout, Key Levels)
- Live Stock Scanner Views
"""
import pytest
from jarvis.india.prediction_engine import INDIA_PREDICTIONS
from jarvis.india.india_service import INDIA_SERVICE


def test_live_indices_overview_cards():
    cards = INDIA_PREDICTIONS.get_live_indices_cards()
    assert len(cards) == 3, f"Expected 3 benchmark cards, got {len(cards)}"
    
    symbols = [c["symbol"] for c in cards]
    assert symbols == ["NIFTY", "SENSEX", "BANKNIFTY"]

    for c in cards:
        assert c["price"] > 0, f"Price should be > 0 for {c['symbol']}"
        assert c["direction"] in ("BULLISH", "BEARISH")
        assert c["data_source"] == "LIVE_EXCHANGE"
        assert c["is_ai_prediction"] is False
        assert "advisor_prompt" in c


def test_market_predictions():
    preds = INDIA_PREDICTIONS.get_market_predictions()
    assert "intraday_setups" in preds
    assert "scalping_setups" in preds
    assert "breakouts" in preds
    assert "key_levels" in preds
    assert len(preds["intraday_setups"]) > 0
    assert len(preds["scalping_setups"]) > 0

    for item in preds["intraday_setups"]:
        assert item["is_ai_prediction"] is True
        assert item["confidence_score"] >= 50
        assert item["risk_reward"].startswith("1:")
        assert "key_catalyst_reasoning" in item


def test_options_expert_advisor():
    for sym in ("NIFTY", "BANKNIFTY", "SENSEX"):
        adv = INDIA_PREDICTIONS.get_options_expert_advisor(sym)
        assert adv["symbol"] == sym
        assert adv["spot_price"] > 0
        assert adv["atm_strike"] > 0
        assert "pcr" in adv
        assert "call_opportunity" in adv
        assert "put_opportunity" in adv
        assert adv["call_opportunity"]["action"] == "BUY"
        assert adv["call_opportunity"]["stop_loss"] > 0
        assert adv["put_opportunity"]["action"] == "BUY"
        assert adv["put_opportunity"]["stop_loss"] > 0
        assert "ai_advisor_analysis" in adv


def test_scanner_views():
    views = INDIA_PREDICTIONS.get_scanner_views()
    assert "intraday" in views
    assert "scalping" in views
    assert "bullish" in views
    assert "bearish" in views
    assert "key_levels" in views
    assert len(views["intraday"]) > 0
    assert len(views["scalping"]) > 0

    for item in views["intraday"]:
        assert "why_identified" in item
        assert len(item["why_identified"]) > 0
