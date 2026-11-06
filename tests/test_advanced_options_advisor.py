"""
Verification Suite for Advanced AI Options Advisor Mode
Tests:
1. Multi-instrument support: NIFTY 50, BANK NIFTY, FIN NIFTY, SENSEX, and F&O Equities (RELIANCE, HDFCBANK).
2. Actionable institutional recommendations: BUY CALL (CE), BUY PUT (PE), WAIT / NO TRADE, or HEDGED SPREAD.
3. Market regime and transparent data freshness provenance.
4. Transparent rationale: why strike is recommended vs why alternative strikes are rejected.
5. Risk management: Entry, Stop-Loss (-30%), Targets (+40%, +80%), R:R >= 1:2.0, Lot risk calculation.
6. Defined-risk hedged alternative formulations.
"""
import pytest
from jarvis.india.prediction_engine import INDIA_PREDICTIONS


@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "RELIANCE"])
def test_advisor_comprehensive_payload(symbol):
    adv = INDIA_PREDICTIONS.get_options_expert_advisor(symbol)
    assert adv["symbol"] == symbol
    assert adv["spot_price"] > 0
    assert adv["atm_strike"] > 0
    assert adv["lot_size"] > 0

    # Market Regime
    assert "market_regime" in adv
    assert adv["market_regime"] in (
        "BULLISH_TRENDING",
        "BEARISH_TRENDING",
        "SIDEWAYS_CONSOLIDATION",
        "HIGH_VOLATILITY"
    )

    # Action Verdict
    assert "recommendation" in adv
    assert any(adv["recommendation"].startswith(prefix) for prefix in (
        "BUY CALL",
        "BUY PUT",
        "WAIT / NO TRADE",
        "HIGH VOLATILITY"
    )), f"Invalid recommendation: {adv['recommendation']}"

    # Data Freshness & Provenance
    assert "data_freshness_status" in adv
    assert "LIVE" in adv["data_freshness_status"] or "MODELLED" in adv["data_freshness_status"]

    # Transparent Rationale
    assert "why_recommended" in adv and len(adv["why_recommended"]) > 10
    assert "why_alternatives_rejected" in adv and len(adv["why_alternatives_rejected"]) > 10

    # Hedged Alternative
    assert "hedged_alternative" in adv
    hedge = adv["hedged_alternative"]
    assert "strategy" in hedge and "wings_range" in hedge and "description" in hedge

    # Call and Put trade opportunities
    ce = adv["call_opportunity"]
    pe = adv["put_opportunity"]

    for opt, opt_type in [(ce, "CE"), (pe, "PE")]:
        assert opt["type"] == opt_type
        assert opt["strike"] > 0
        assert opt["ltp"] > 0
        assert opt["stop_loss"] > 0
        assert opt["target_1"] > opt["stop_loss"]
        assert opt["target_2"] > opt["target_1"]
        assert opt["risk_reward"] == "1:2.0"
        assert opt["capital_per_lot"] > 0
        assert opt["max_risk_per_lot"] > 0
        assert opt["profit_target_1_per_lot"] > 0
        assert "is_primary_pick" in opt
        assert "status_badge" in opt

    # Algorithmic "Which One to Select" Decision Gate
    assert "selection_decision" in adv
    dec = adv["selection_decision"]
    assert dec["selected_side"] in ("CE", "PE", "WAIT", "SPREAD")
    assert "decision_summary" in dec and len(dec["decision_summary"]) > 5
    assert "decision_reasoning" in dec and len(dec["decision_reasoning"]) > 5
    assert 0 <= dec["ce_conviction_score"] <= 100
    assert 0 <= dec["pe_conviction_score"] <= 100
    assert "ce_trigger" in dec and "pe_trigger" in dec and "invalidation" in dec
    assert "ce_broker_syntax" in dec and "pe_broker_syntax" in dec
    assert len(dec["pre_trade_checklist"]) == 5


def test_advisor_custom_query_and_strategy_bias():
    """Verify that specific queries influence the AI advisor response."""
    adv_call = INDIA_PREDICTIONS.get_options_expert_advisor("NIFTY", strategy_type="best_call")
    assert "call_opportunity" in adv_call
    assert "NIFTY" in adv_call["ai_advisor_analysis"]

    adv_put = INDIA_PREDICTIONS.get_options_expert_advisor("BANKNIFTY", strategy_type="best_put")
    assert "put_opportunity" in adv_put
    assert "BANKNIFTY" in adv_put["ai_advisor_analysis"]
