"""
Tests for AI-Powered Smart 3-Tier TP/SL Milestones Engine.
Verifies dynamic level calculation, institutional entry targets, DecisionObject serialization,
PositionMonitorEngine milestone tracking, and backward compatibility.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

from jarvis.data.schemas import (
    MarketContext,
    RegimeOutput,
    MarketRegime,
    StructureContext,
    LiquidityContext,
    VolatilityContext,
    MomentumContext,
    SessionContext,
    DecisionObject,
    TradeQualityGateResult,
    PositionSnapshot,
)
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine
from jarvis.intelligence.institutional_entry_engine import InstitutionalEntryEngine
from jarvis.intelligence.decision_engine import DecisionEngine, LevelsResult
from jarvis.execution.position_monitor import PositionMonitorEngine
from jarvis.application.state_manager import StateManager


def _build_test_context(symbol: str = "XAUUSD", current_price: float = 2400.0, bias: str = "BULLISH") -> MarketContext:
    return MarketContext(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        current_price=current_price,
        bid=current_price - 0.20,
        ask=current_price + 0.20,
        structure=StructureContext(
            bias=bias,
            higher_highs=True if bias == "BULLISH" else False,
            higher_lows=True if bias == "BULLISH" else False,
            lower_highs=False if bias == "BULLISH" else True,
            lower_lows=False if bias == "BULLISH" else True,
            demand_zone=(current_price - 15.0, current_price - 10.0),
            supply_zone=(current_price + 20.0, current_price + 30.0),
        ),
        liquidity=LiquidityContext(
            buy_side_liquidity=current_price + 25.0,
            sell_side_liquidity=current_price - 25.0,
        ),
        volatility=VolatilityContext(
            atr=10.0,
            atr_percent=0.41,
            state="NORMAL",
            current_spread_pips=2.0,
        ),
        momentum=MomentumContext(
            rsi=55.0,
            adx=28.0,
            trend_score=35.0 if bias == "BULLISH" else -35.0,
        ),
        session=SessionContext(is_prime_session=True),
    )


def test_dynamic_levels_computes_3_tier_milestones():
    """Verify DynamicRiskAndLevelsEngine returns valid tp1_price, tp2_price, tp3_price."""
    engine = DynamicRiskAndLevelsEngine()
    regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85)
    ctx = _build_test_context(symbol="XAUUSD", current_price=2400.0, bias="BULLISH")

    res = engine.calculate_levels(
        context=ctx,
        regime=regime,
        tentative_bias="BUY",
        trade_style="SWING",
    )

    assert res["bias"] == "BUY"
    assert res["entry_price"] > 0
    assert res["sl_price"] < res["entry_price"]
    assert res["tp1_price"] > res["entry_price"]
    assert res["tp2_price"] > res["tp1_price"]
    assert res["tp3_price"] > res["tp2_price"]

    # Sell direction verification
    ctx_bear = _build_test_context(symbol="EURUSD", current_price=1.0800, bias="BEARISH")
    regime_bear = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={}, confidence=0.85)
    res_sell = engine.calculate_levels(
        context=ctx_bear,
        regime=regime_bear,
        tentative_bias="SELL",
        trade_style="SWING",
    )

    assert res_sell["bias"] == "SELL"
    assert res_sell["sl_price"] > res_sell["entry_price"]
    assert res_sell["tp1_price"] < res_sell["entry_price"]
    assert res_sell["tp2_price"] < res_sell["tp1_price"]
    assert res_sell["tp3_price"] < res_sell["tp2_price"]


def test_institutional_entry_engine_targets():
    """Verify InstitutionalEntryEngine returns tp1_price, tp2_price, tp3_price across horizons."""
    inst = InstitutionalEntryEngine()
    ctx = _build_test_context(symbol="XAUUSD", current_price=2400.0, bias="BULLISH")
    regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85)

    for style in ["SCALP", "DAY_TRADING", "SWING"]:
        levels = inst.calculate_entry_and_levels(ctx, regime, "BUY", trade_style=style)
        assert "tp1_price" in levels
        assert "tp2_price" in levels
        assert "tp3_price" in levels
        assert levels["tp1_price"] > levels["entry_price"]
        assert levels["tp2_price"] > levels["tp1_price"]
        assert levels["tp3_price"] > levels["tp2_price"]


def test_levels_result_tuple_backward_compatibility():
    """Verify LevelsResult unpacks exactly as an 8-tuple and exposes milestone attributes."""
    lr = LevelsResult(
        tentative_bias="BUY",
        entry_price=2400.0,
        sl_price=2390.0,
        tp_price=2430.0,
        risk_dist=10.0,
        rr_ratio=3.0,
        first_target_price=2410.0,
        first_target_volume_pct=0.50,
        tp1_price=2410.0,
        tp2_price=2430.0,
        tp3_price=2450.0,
    )

    # 1. Standard 8-tuple unpacking must work without error
    bias, entry, sl, tp, risk_dist, rr, ftp, ftv = lr
    assert bias == "BUY"
    assert entry == 2400.0
    assert sl == 2390.0
    assert tp == 2430.0
    assert risk_dist == 10.0
    assert rr == 3.0
    assert ftp == 2410.0
    assert ftv == 0.50

    # 2. Slice unpacking must work
    bias_slice, entry_slice, *_ = lr
    assert bias_slice == "BUY"
    assert entry_slice == 2400.0

    # 3. Milestone attributes
    assert lr.tp1_price == 2410.0
    assert lr.tp2_price == 2430.0
    assert lr.tp3_price == 2450.0


def test_decision_object_serialization():
    """Verify DecisionObject stores and serializes tp1_price, tp2_price, and tp3_price."""
    dec = DecisionObject(
        symbol="XAUUSD",
        timestamp=datetime.now(timezone.utc),
        regime=RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85),
        bias="BUY",
        probabilities={"buy": 0.80, "sell": 0.10, "no_trade": 0.10},
        strategy="MOMENTUM_BREAKOUT",
        entry_price=2400.0,
        stop_loss=2390.0,
        take_profit=2430.0,
        tp1_price=2410.0,
        tp2_price=2430.0,
        tp3_price=2455.0,
        risk_reward_ratio=3.0,
        calculated_risk_percent=0.50,
        expected_value=25.0,
        model_confidence=0.80,
        adversarial_penalty=2.0,
        invalidation_levels=[],
        bull_case=[],
        bear_case=[],
        risk_factors=[],
        quality_gate=TradeQualityGateResult(passed=True, checks={}),
        decision="EXECUTE",
        execution_authorized=True,
    )

    d_dict = dec.to_dict()
    assert d_dict["tp1_price"] == 2410.0
    assert d_dict["tp2_price"] == 2430.0
    assert d_dict["tp3_price"] == 2455.0
    assert d_dict["take_profit"] == 2430.0


def test_position_monitor_milestone_progression():
    """Verify PositionMonitorEngine initializes milestones and progresses them from OPEN to TP3."""
    mock_mt5 = MagicMock()
    mock_feed = MagicMock()
    mock_ctx_eng = MagicMock()
    state_mgr = StateManager()

    pm = PositionMonitorEngine(
        mt5_client=mock_mt5,
        data_feed=mock_feed,
        context_engine=mock_ctx_eng,
        state_manager=state_mgr,
        event_bus=MagicMock(),
    )

    from jarvis.execution.position_monitor import JARVIS_MAGIC_NUMBER

    pos = PositionSnapshot(
        ticket=999,
        symbol="XAUUSD",
        type="BUY",
        volume=0.10,
        open_price=2400.0,
        current_price=2405.0,
        sl=2390.0,  # 1R = 10.0
        tp=2430.0,
        profit=50.0,
        swap=0.0,
        commission=0.0,
        open_time=datetime.now(timezone.utc).isoformat(),
        magic=JARVIS_MAGIC_NUMBER,
    )

    ctx = _build_test_context(symbol="XAUUSD", current_price=2405.0, bias="BULLISH")
    import time
    pm._ctx_cache["XAUUSD"] = (ctx, time.monotonic())

    # Step 1: Baseline entry evaluation
    pm._manage_single_position(pos, equity=10000.0, balance=10000.0)
    assert 999 in pm._position_milestones
    m = pm.get_position_milestones(999)
    assert m["status"] == "OPEN"
    assert m["tp1"] > 2400.0
    assert m["tp2"] >= 2420.0
    assert m["tp3"] >= 2435.0
    assert pos.tp1 == m["tp1"]
    assert pos.tp2 == m["tp2"]
    assert pos.tp3 == m["tp3"]
    assert pos.milestone_status == "OPEN"

    # Step 2: Price hits TP1 (+1.0R / 2410.50) -> should mark TP1_HIT_BE_LOCKED
    pos.current_price = 2411.0
    ctx.current_price = 2411.0
    pm._ctx_cache["XAUUSD"] = (ctx, time.monotonic())
    pm._manage_single_position(pos, equity=10000.0, balance=10000.0)
    assert pos.milestone_status == "TP1_HIT_BE_LOCKED"

    # Step 3: Price hits TP2 (+2.1R / 2421.00) -> should mark TP2_HIT_PROFIT_LOCKED
    pos.current_price = 2421.0
    ctx.current_price = 2421.0
    pm._ctx_cache["XAUUSD"] = (ctx, time.monotonic())
    pm._manage_single_position(pos, equity=10000.0, balance=10000.0)
    assert pos.milestone_status == "TP2_HIT_PROFIT_LOCKED"

    # Step 4: Price exceeds TP3 runner threshold (+3.2R / 2432.00) -> should mark TP3_RUNNER_TRAILING
    pos.current_price = 2432.0
    ctx.current_price = 2432.0
    pm._ctx_cache["XAUUSD"] = (ctx, time.monotonic())
    pm._manage_single_position(pos, equity=10000.0, balance=10000.0)
    assert pos.milestone_status == "TP3_RUNNER_TRAILING"
