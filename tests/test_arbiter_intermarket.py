"""
Unit tests for UniversalOpportunityArbiter Intermarket & SMT divergence multipliers.
"""
import pytest
from datetime import datetime, timezone
from jarvis.data.schemas import (
    MarketContext,
    DecisionObject,
    StructureContext,
    LiquidityContext,
    VolatilityContext,
    MomentumContext,
    SessionContext,
    TradeQualityGateResult,
    MarketRegime,
    RegimeOutput
)
from jarvis.intelligence.opportunity_arbiter import UniversalOpportunityArbiter


def _create_mock_context(symbol="XAUUSD", intermarket_dxy=None, cross_market_smt=None):
    return MarketContext(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        current_price=2700.0,
        bid=2699.8,
        ask=2700.2,
        structure=StructureContext(bias="BULLISH"),
        liquidity=LiquidityContext(),
        volatility=VolatilityContext(current_spread_pips=1.0),
        momentum=MomentumContext(rsi=55.0),
        session=SessionContext(is_prime_session=True),
        intermarket_dxy=intermarket_dxy or {},
        cross_market_smt=cross_market_smt or {}
    )


def _create_mock_decision(symbol="XAUUSD", bias="BUY", expected_value=1.0):
    reg = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.8)
    return DecisionObject(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        regime=reg,
        bias=bias,
        probabilities={"buy": 0.75, "sell": 0.25},
        strategy="TREND_PULLBACK",
        entry_price=2700.0,
        stop_loss=2690.0,
        take_profit=2720.0,
        risk_reward_ratio=2.0,
        calculated_risk_percent=1.0,
        expected_value=expected_value,
        model_confidence=0.75,
        adversarial_penalty=0.0,
        invalidation_levels=[],
        bull_case=["Trend alignment"],
        bear_case=[],
        risk_factors=[],
        decision="EXECUTE",
        master_confluence_score=70.0,
        meta_label_prob=0.70,
        quality_gate=TradeQualityGateResult(passed=True, checks={})
    )


def test_gold_dxy_confirmation_boost():
    arbiter = UniversalOpportunityArbiter()
    
    # 1. Baseline context with no DXY data
    ctx_base = _create_mock_context("XAUUSD")
    dec_buy = _create_mock_decision("XAUUSD", "BUY")
    cand_base = arbiter.evaluate_opportunity(dec_buy, ctx_base, trade_style="SWING")
    
    # 2. Context where DXY is BEARISH (confirms Gold BUY)
    dxy_confirm = {
        "is_live": True,
        "dxy_value": 103.5,
        "trend": "BEARISH",
        "gold_impact": "CONFIRMS_BUY",
        "momentum_score": -35.0
    }
    ctx_confirm = _create_mock_context("XAUUSD", intermarket_dxy=dxy_confirm)
    cand_confirm = arbiter.evaluate_opportunity(dec_buy, ctx_confirm, trade_style="SWING")
    
    # 3. Context where DXY is BULLISH (opposes Gold BUY)
    dxy_oppose = {
        "is_live": True,
        "dxy_value": 105.5,
        "trend": "BULLISH",
        "gold_impact": "OPPOSES_BUY",
        "momentum_score": 45.0
    }
    ctx_oppose = _create_mock_context("XAUUSD", intermarket_dxy=dxy_oppose)
    cand_oppose = arbiter.evaluate_opportunity(dec_buy, ctx_oppose, trade_style="SWING")
    
    assert cand_confirm.utility_score > cand_base.utility_score
    assert cand_oppose.utility_score < cand_base.utility_score
    assert any("DXY confirms Gold BUY" in c for c in cand_confirm.confluence_factors)
    assert any("DXY opposes Gold BUY" in r for r in cand_oppose.risk_factors)


def test_cross_market_smt_divergence_multiplier():
    arbiter = UniversalOpportunityArbiter()
    
    ctx_base = _create_mock_context("EURUSD")
    dec_buy = _create_mock_decision("EURUSD", "BUY")
    cand_base = arbiter.evaluate_opportunity(dec_buy, ctx_base, trade_style="SWING")
    
    # Bullish SMT (accumulation) confirms EURUSD BUY
    smt_bullish = {
        "smt_active": True,
        "smt_type": "BULLISH_SMT",
        "leader": "GBPUSD",
        "score_bonus": 15.0
    }
    ctx_smt_bull = _create_mock_context("EURUSD", cross_market_smt=smt_bullish)
    cand_smt_bull = arbiter.evaluate_opportunity(dec_buy, ctx_smt_bull, trade_style="SWING")
    
    # Bearish SMT (distribution) opposes EURUSD BUY
    smt_bearish = {
        "smt_active": True,
        "smt_type": "BEARISH_SMT",
        "leader": "GBPUSD",
        "score_bonus": -15.0
    }
    ctx_smt_bear = _create_mock_context("EURUSD", cross_market_smt=smt_bearish)
    cand_smt_bear = arbiter.evaluate_opportunity(dec_buy, ctx_smt_bear, trade_style="SWING")
    
    assert cand_smt_bull.utility_score > cand_base.utility_score
    assert cand_smt_bear.utility_score < cand_base.utility_score
    assert any("SMT Bullish Divergence" in c for c in cand_smt_bull.confluence_factors)
    assert any("Opposing SMT Bearish Divergence" in r for r in cand_smt_bear.risk_factors)
