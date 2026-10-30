"""Unit test suite for AutoTuneEngine and AI Selection enhancements."""
import pytest
from unittest.mock import MagicMock, patch
from jarvis.intelligence.auto_tune_engine import AutoTuneEngine
from jarvis.intelligence.opportunity_arbiter import CandidateOpportunity


def test_candidate_opportunity_to_dict_enrichment():
    """Verify CandidateOpportunity.to_dict includes tp1, tp2, tp3, and confluence_tier."""
    mock_decision = MagicMock()
    mock_decision.tp1_price = 2760.0
    mock_decision.tp2_price = 2775.0
    mock_decision.tp3_price = 2790.0
    mock_decision.master_confluence_tier = "HIGH"

    opp = CandidateOpportunity(
        symbol="XAUUSD",
        trade_style="DAY_TRADING",
        timeframe="M15",
        strategy="FVG_LIQUIDITY",
        bias="BUY",
        entry_price=2750.0,
        stop_loss=2740.0,
        take_profit=2775.0,
        risk_reward_ratio=2.5,
        expected_value=1.5,
        win_prob=68.0,
        ml_prob=72.0,
        confluence_score=82.5,
        confluence_factors=["FVG Fill", "OTE Zone"],
        adversarial_penalty=0.0,
        risk_factors=[],
        regime="TRENDING_BULL",
        regime_multiplier=1.2,
        utility_score=1.85,
        setup_grade="GRADE A",
        is_actionable=True,
        decision_obj=mock_decision
    )

    d = opp.to_dict()
    assert d["symbol"] == "XAUUSD"
    assert d["tp1_price"] == 2760.0
    assert d["tp2_price"] == 2775.0
    assert d["tp3_price"] == 2790.0
    assert d["confluence_tier"] == "HIGH"
    assert d["risk_reward_ratio"] == 2.5
    assert d["setup_grade"] == "GRADE A"
    assert d["is_actionable"] is True


@patch("jarvis.intelligence.auto_tune_engine._is_weekend_gap", return_value=False)
@patch("jarvis.intelligence.auto_tune_engine.SessionEngine.get_market_trading_status")
def test_auto_tune_conservative_profile(mock_session, mock_weekend):
    """Verify CONSERVATIVE mode only accepts Grade A+/A, enforces strict confluence & correlation caps."""
    mock_session.return_value = {"is_open": True, "current_session": "LONDON"}
    
    mock_state = MagicMock()
    mock_state.allowed_symbols = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "BTCUSD"]
    
    # Opportunities with varying grades & confluence
    mock_state.radar_opportunities = [
        {"symbol": "XAUUSD", "setup_grade": "GRADE A+", "confluence_score": 85.0, "win_prob": 70.0, "expected_value": 2.0, "regime": "TRENDING_BULL"},
        {"symbol": "EURUSD", "setup_grade": "GRADE A", "confluence_score": 75.0, "win_prob": 62.0, "expected_value": 1.2, "regime": "TRENDING_BEAR"},
        {"symbol": "GBPUSD", "setup_grade": "GRADE A", "confluence_score": 70.0, "win_prob": 60.0, "expected_value": 1.0, "regime": "TRENDING_BULL"},
        {"symbol": "USDJPY", "setup_grade": "GRADE A", "confluence_score": 68.0, "win_prob": 60.0, "expected_value": 0.9, "regime": "TRENDING_BULL"},
        {"symbol": "USDCAD", "setup_grade": "GRADE B", "confluence_score": 40.0, "win_prob": 50.0, "expected_value": 0.2, "regime": "RANGE"},
        {"symbol": "BTCUSD", "setup_grade": "GRADE C", "confluence_score": 30.0, "win_prob": 45.0, "expected_value": -0.2, "regime": "CHOP"},
    ]

    mock_orch = MagicMock()
    mock_orch.allowed_symbols = mock_state.allowed_symbols

    mock_mt5 = MagicMock()
    mock_mt5.get_symbol_trading_spec.return_value = {
        "spread": 15,
        "digits": 5,
        "point": 0.00001
    }

    res = AutoTuneEngine.tune_universe(
        orchestrator=mock_orch,
        state_manager=mock_state,
        mt5_client=mock_mt5,
        mode="CONSERVATIVE",
        asset_class="ALL"
    )

    assert res["mode"] == "CONSERVATIVE"
    active = res["active_symbols"]
    assert "XAUUSD" in active
    # Conservative profile caps max 2 USD forex pairs
    usd_forex = [s for s in active if s in ("EURUSD", "GBPUSD", "USDJPY", "USDCAD")]
    assert len(usd_forex) <= 2
    assert "BTCUSD" not in active  # Grade C rejected in Conservative


@patch("jarvis.intelligence.auto_tune_engine._is_weekend_gap", return_value=False)
@patch("jarvis.intelligence.auto_tune_engine.SessionEngine.get_market_trading_status")
def test_auto_tune_balanced_profile(mock_session, mock_weekend):
    """Verify BALANCED mode accepts Grade B setups if EV is positive and confluence passes."""
    mock_session.return_value = {"is_open": True, "current_session": "NEW_YORK"}
    
    mock_state = MagicMock()
    mock_state.allowed_symbols = ["XAUUSD", "EURUSD", "USDCAD"]
    mock_state.radar_opportunities = [
        {"symbol": "XAUUSD", "setup_grade": "GRADE A", "confluence_score": 70.0, "win_prob": 62.0, "expected_value": 1.2, "regime": "TRENDING"},
        {"symbol": "EURUSD", "setup_grade": "GRADE B", "confluence_score": 52.0, "win_prob": 55.0, "expected_value": 0.5, "regime": "TRENDING"},
        {"symbol": "USDCAD", "setup_grade": "GRADE D", "confluence_score": 20.0, "win_prob": 30.0, "expected_value": -0.5, "regime": "CHOP"}
    ]
    mock_orch = MagicMock()
    mock_mt5 = MagicMock()
    mock_mt5.get_symbol_trading_spec.return_value = {"spread": 10, "digits": 5, "point": 0.00001}

    res = AutoTuneEngine.tune_universe(
        orchestrator=mock_orch,
        state_manager=mock_state,
        mt5_client=mock_mt5,
        mode="BALANCED",
        asset_class="ALL"
    )

    assert "XAUUSD" in res["active_symbols"]
    assert "EURUSD" in res["active_symbols"]
    assert "USDCAD" not in res["active_symbols"]


@patch("jarvis.intelligence.auto_tune_engine._is_weekend_gap", return_value=True)
def test_auto_tune_weekend_mode(mock_weekend):
    """Verify that during weekend gaps, only 24/7 crypto markets are kept active."""
    mock_state = MagicMock()
    mock_state.radar_opportunities = [
        {"symbol": "BTCUSD", "confluence_score": 60.0},
        {"symbol": "ETHUSD", "confluence_score": 55.0},
        {"symbol": "XAUUSD", "confluence_score": 90.0},
        {"symbol": "EURUSD", "confluence_score": 85.0}
    ]

    res = AutoTuneEngine.tune_universe(
        state_manager=mock_state,
        mode="BALANCED",
        asset_class="ALL"
    )

    assert "BTCUSD" in res["active_symbols"]
    assert "XAUUSD" not in res["active_symbols"]
    assert "EURUSD" not in res["active_symbols"]
    assert "Weekend Mode" in res["summary"]
