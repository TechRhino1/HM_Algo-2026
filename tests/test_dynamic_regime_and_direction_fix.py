"""
Unit tests verifying dynamic regime adaptation, breakout direction resolution,
Gold unlock on micro accounts, and pending order deduplication.
"""
from datetime import datetime, timezone
import pytest
from jarvis.data.schemas import (
    MarketContext, MarketRegime, RegimeOutput, StructureContext,
    MomentumContext, VolatilityContext, LiquidityContext, SessionContext,
    AnalystReport, AnalystRole
)
from jarvis.intelligence.strategy_selector import StrategySelector
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.risk.position_sizing import PositionSizer


def test_strategy_selector_dynamic_regime_mutual_exclusion():
    """Verify that in strong trending or breakout regimes, RANGE_MEAN_REVERSION is 0.0."""
    selector = StrategySelector()

    ctx = MarketContext(
        timestamp=datetime.now(timezone.utc),
        symbol="BTCUSD",
        current_price=85000.0,
        bid=84995.0,
        ask=85005.0,
        session=SessionContext(),
        structure=StructureContext(bias="BULLISH", bos=True),
        momentum=MomentumContext(adx=32.0, trend_score=28.0),
        volatility=VolatilityContext(atr=1200.0),
        liquidity=LiquidityContext(sweep_detected=False),
    )

    # 1. Strong Bull Trend
    regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, confidence=0.85, probabilities={})
    probs = selector.select_strategy_probabilities(regime, context=ctx, account_equity=500.0)
    assert probs.get("RANGE_MEAN_REVERSION", 0.0) == 0.0
    assert probs.get("TREND_FOLLOWING", 0.0) > 0.0 or probs.get("TREND_PULLBACK", 0.0) > 0.0

    # 2. Breakout Expansion
    regime_bo = RegimeOutput(primary_regime=MarketRegime.BREAKOUT, confidence=0.90, probabilities={})
    probs_bo = selector.select_strategy_probabilities(regime_bo, context=ctx, account_equity=500.0)
    assert probs_bo.get("RANGE_MEAN_REVERSION", 0.0) == 0.0
    assert probs_bo.get("BREAKOUT_EXPANSION", 0.0) > 0.0

    # 3. Range Regime -> Trend Following must be 0.0
    ctx_range = MarketContext(
        timestamp=datetime.now(timezone.utc),
        symbol="BTCUSD",
        current_price=85000.0,
        bid=84995.0,
        ask=85005.0,
        session=SessionContext(),
        structure=StructureContext(bias="NEUTRAL"),
        momentum=MomentumContext(adx=15.0, trend_score=2.0),
        volatility=VolatilityContext(atr=400.0),
        liquidity=LiquidityContext(sweep_detected=False),
    )
    regime_range = RegimeOutput(primary_regime=MarketRegime.RANGE, confidence=0.80, probabilities={})
    probs_range = selector.select_strategy_probabilities(regime_range, context=ctx_range, account_equity=500.0)
    assert probs_range.get("TREND_FOLLOWING", 0.0) == 0.0
    assert probs_range.get("RANGE_MEAN_REVERSION", 0.0) > 0.0


def test_gold_position_sizing_on_micro_account():
    """Verify that a $418 account allows minimum 0.01 lot on Gold with ~$30 stop distance."""
    sizer = PositionSizer()
    sym_info = {
        "name": "GOLD.i#",
        "trade_contract_size": 100.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "trade_tick_value": 1.0,
        "trade_tick_size": 0.01,
        "typical_spread_pips": 2.5,
        "max_spread_pips": 5.0
    }
    lots = sizer.calculate_lot_size(
        account_balance=418.11,
        entry_price=2650.0,
        sl_price=2620.0,  # $30 stop distance
        risk_pct=1.0,
        symbol_info=sym_info
    )
    assert lots == 0.01, f"Expected 0.01 lots for Gold on $418 account, got {lots}"


def test_decision_engine_breakout_direction():
    """Verify that in BREAKOUT regime, direction aligns directly with momentum and structure."""
    engine = DecisionEngine()

    # Bullish Breakout
    ctx_bull = MarketContext(
        timestamp=datetime.now(timezone.utc),
        symbol="BTCUSD",
        current_price=85000.0,
        bid=84995.0,
        ask=85005.0,
        session=SessionContext(),
        structure=StructureContext(bias="BULLISH", bos=True),
        momentum=MomentumContext(adx=30.0, trend_score=25.0),
        volatility=VolatilityContext(atr=1000.0),
        liquidity=LiquidityContext(sweep_detected=False),
        mtf_alignment={"D1": "BEARISH", "H4": "BULLISH", "H1": "BULLISH", "M15": "BULLISH"}
    )
    regime_bo = RegimeOutput(primary_regime=MarketRegime.BREAKOUT, confidence=0.88, probabilities={})
    reports = {
        "analyst1": AnalystReport(role=AnalystRole.MOMENTUM, symbol="BTCUSD", bias="BULLISH", score=78.0, confidence=0.80)
    }

    bias_bull = engine._resolve_market_trend_and_bias(ctx_bull, regime_bo, reports, trade_style="DAY_TRADING")
    assert bias_bull == "BUY"

    # Bearish Breakout
    ctx_bear = MarketContext(
        timestamp=datetime.now(timezone.utc),
        symbol="BTCUSD",
        current_price=82000.0,
        bid=81995.0,
        ask=82005.0,
        session=SessionContext(),
        structure=StructureContext(bias="BEARISH", bos=True),
        momentum=MomentumContext(adx=30.0, trend_score=-25.0),
        volatility=VolatilityContext(atr=1000.0),
        liquidity=LiquidityContext(sweep_detected=False),
        mtf_alignment={"D1": "BULLISH", "H4": "BEARISH", "H1": "BEARISH", "M15": "BEARISH"}
    )
    reports_bear = {
        "analyst1": AnalystReport(role=AnalystRole.MOMENTUM, symbol="BTCUSD", bias="BEARISH", score=78.0, confidence=0.80)
    }
    bias_bear = engine._resolve_market_trend_and_bias(ctx_bear, regime_bo, reports_bear, trade_style="DAY_TRADING")
    assert bias_bear == "SELL"


def test_decision_engine_authenticated_reversal_on_gold():
    """Verify Gold can short on authenticated bear reversal even if D1 anchor is Bullish."""
    engine = DecisionEngine()

    mom = MomentumContext(trend_score=-22.0, divergence="BEARISH_DIVERGENCE")
    setattr(mom, "bearish_divergence", True)

    ctx = MarketContext(
        timestamp=datetime.now(timezone.utc),
        symbol="GOLD.i#",
        current_price=2680.0,
        bid=2679.5,
        ask=2680.5,
        session=SessionContext(),
        structure=StructureContext(
            bias="BEARISH",
            choch_type="BEARISH",
            discount_premium_zone="PREMIUM"
        ),
        momentum=mom,
        volatility=VolatilityContext(atr=25.0),
        liquidity=LiquidityContext(sweep_detected=True, sweep_type="BEARISH_SWEEP"),
        mtf_alignment={"D1": "BULLISH", "H4": "BEARISH", "H1": "BEARISH", "M15": "BEARISH"},
        mtf_confluence_score=-25.0
    )
    regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, confidence=0.70, probabilities={})
    reports = {
        "analyst1": AnalystReport(role=AnalystRole.MOMENTUM, symbol="GOLD.i#", bias="BEARISH", score=75.0, confidence=0.75),
        "analyst2": AnalystReport(role=AnalystRole.STRUCTURE, symbol="GOLD.i#", bias="BEARISH", score=70.0, confidence=0.70),
        "analyst3": AnalystReport(role=AnalystRole.LIQUIDITY, symbol="GOLD.i#", bias="BEARISH", score=72.0, confidence=0.72),
    }

    direction = engine._resolve_market_trend_and_bias(ctx, regime, reports, trade_style="DAY_TRADING")
    assert direction == "SELL"
