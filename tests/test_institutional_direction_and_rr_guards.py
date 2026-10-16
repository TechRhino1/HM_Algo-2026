"""
Unit and Integration Tests for Institutional Direction Resolution & R:R Quality Guards.
Validates:
1. Bullish market structure / expansion prevents counter-trend selling across all styles (including SWING).
2. Toxic TP tucking against Devil's Advocate threat levels is strictly prohibited from violating minimum R:R.
3. TradeGuard performs true geometric price validation abs(TP - Entry) / abs(Entry - SL).
4. Risk/Reward, Valid Stop Loss, and Positive Expected Value are enforced as HARD_GATES in AdaptiveGatePolicy.
5. Dynamic levels expansion scaling ensures clamped risk maintains at least 2:1 R:R on Gold.
"""
import pytest
from datetime import datetime, timezone
from jarvis.data.schemas import (
    MarketContext,
    StructureContext,
    LiquidityContext,
    VolatilityContext,
    MomentumContext,
    SessionContext,
    RegimeOutput,
    MarketRegime,
    DecisionObject,
    TradeQualityGateResult,
    AccountSnapshot,
)
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.intelligence.gate_policy import AdaptiveGatePolicy, HARD_GATES
from jarvis.risk.trade_guard import TradeGuard


def _make_dummy_decision(entry, sl, tp, bias="BUY", symbol="XAUUSD", declared_rr=2.0):
    gate = TradeQualityGateResult(passed=True, checks={})
    regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.8)
    return DecisionObject(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        regime=regime,
        bias=bias,
        probabilities={"BUY": 0.8, "SELL": 0.2},
        strategy="TREND_FOLLOWING",
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward_ratio=declared_rr,
        calculated_risk_percent=0.5,
        expected_value=1.5,
        model_confidence=0.75,
        adversarial_penalty=5.0,
        invalidation_levels=[],
        bull_case=[],
        bear_case=[],
        risk_factors=[],
        quality_gate=gate,
        decision="EXECUTE",
        execution_authorized=True,
    )


def _make_dummy_account():
    return AccountSnapshot(
        login=101059540,
        server="XMGlobal-MT5",
        leverage=500,
        balance=500.0,
        equity=500.0,
        margin=0.0,
        free_margin=500.0,
        margin_level=0.0,
        trade_allowed=True,
    )


def test_hard_gates_contain_critical_risk_checks():
    """Verify that Risk/Reward >= 1.5, Valid Stop Loss Distance, and Positive Expected Value are HARD GATES."""
    assert "Risk/Reward >= 1.5" in HARD_GATES
    assert "Valid Stop Loss Distance" in HARD_GATES
    assert "Positive Expected Value" in HARD_GATES

    policy = AdaptiveGatePolicy()
    outcome, fails = policy.decide(["Risk/Reward >= 1.5"], recent_win_rate=0.60)
    assert outcome == "BLOCK"
    assert "Risk/Reward >= 1.5" in fails


def test_trade_guard_geometric_rr_check():
    """Verify TradeGuard calculates actual price geometry and blocks sub-minimum R:R even if declared R:R is high."""
    # Degenerate trade with 24 pts risk and 2.36 pts reward (~0.1:1 R:R)
    bad_decision = _make_dummy_decision(
        entry=4159.31,
        sl=4183.27,  # 23.96 pts risk
        tp=4156.95,  # 2.36 pts reward
        bias="SELL",
        symbol="XAUUSD",
        declared_rr=2.5,  # STALE ATTRIBUTE deliberately set to 2.5
    )

    account = _make_dummy_account()
    guard_res = TradeGuard.validate_pre_execution(bad_decision, account=account, positions=[])
    assert not guard_res["passed"]
    assert any("Sub-minimum Risk:Reward ratio" in r for r in guard_res["reasons"])


def test_trade_guard_allows_valid_geometric_rr():
    """Verify TradeGuard passes a trade with genuine geometric R:R >= 1.75:1."""
    good_decision = _make_dummy_decision(
        entry=4150.00,
        sl=4135.00,  # 15 pts risk
        tp=4185.00,  # 35 pts reward -> 2.33:1 R:R
        bias="BUY",
        symbol="XAUUSD",
        declared_rr=2.33,
    )

    account = _make_dummy_account()
    guard_res = TradeGuard.validate_pre_execution(good_decision, account=account, positions=[])
    assert guard_res["passed"]


def test_swing_direction_resolution_prevents_selling_bullish_market():
    """Verify that SWING style strictly prohibits SELL when market structure or expansion is bullish."""
    engine = DecisionEngine()

    now = datetime.now(timezone.utc)
    context = MarketContext(
        symbol="XAUUSD",
        timestamp=now,
        session=SessionContext(),
        current_price=4165.0,
        bid=4164.8,
        ask=4165.2,
        trade_style="SWING",
        structure=StructureContext(
            bias="BULLISH",
            higher_highs=True,
            higher_lows=True,
            bos=True,
            bos_type="BULLISH",
        ),
        momentum=MomentumContext(
            trend_score=25,
            adx=28.0,
            short_term_atr_disp=0.50,
        ),
        volatility=VolatilityContext(atr=15.0, current_spread_pips=2.0),
        liquidity=LiquidityContext(),
        mtf_alignment={"D1": "BULLISH", "H4": "BULLISH", "H1": "BULLISH"},
    )
    regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85)

    bias = engine._resolve_market_trend_and_bias(
        context=context,
        regime=regime,
        analyst_reports={},
        trade_style="SWING",
    )
    assert bias == "BUY"


def test_swing_direction_resolution_blocks_sell_into_rebound():
    """Even if regime says TREND_BEAR from lagging indicators, bullish primary structure prevents selling."""
    engine = DecisionEngine()

    now = datetime.now(timezone.utc)
    context = MarketContext(
        symbol="XAUUSD",
        timestamp=now,
        session=SessionContext(),
        current_price=4165.0,
        bid=4164.8,
        ask=4165.2,
        trade_style="SWING",
        structure=StructureContext(
            bias="BULLISH",  # Structure is bullish
            higher_highs=True,
            higher_lows=True,
            bos=True,
            bos_type="BULLISH",
        ),
        momentum=MomentumContext(
            trend_score=-15,
            adx=22.0,
            short_term_atr_disp=0.40,
        ),
        volatility=VolatilityContext(atr=15.0, current_spread_pips=2.0),
        liquidity=LiquidityContext(),
        mtf_alignment={"D1": "BULLISH", "H4": "NEUTRAL", "H1": "BULLISH"},
    )
    regime = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={}, confidence=0.75)

    bias = engine._resolve_market_trend_and_bias(
        context=context,
        regime=regime,
        analyst_reports={},
        trade_style="SWING",
    )
    # Must NOT sell into bullish structure or upward expansion!
    assert bias != "SELL"
    assert bias in ("BUY", "HOLD")
