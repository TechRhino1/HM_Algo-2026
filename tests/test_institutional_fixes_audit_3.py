"""
Unit & regression tests for institutional fixes in hm_algo_2026_issues_and_fixes (3).txt:
1. Issue 1: Proximity Harvesting Guard (_should_harvest_proximity) with stall/rejection and R:R ceiling.
2. Issue 2: Safe Relative Progress-to-TP and Breakeven Lock floor (r_multiple >= 0.45, tp > 0).
3. Patch 3.2: Bull-Flag Pullback Hook Re-Entry (_check_pullback_reclaim and _is_momentum_valid_for_regime).
4. Patch 3.3: Ignore Break-Even+ Runners in per-symbol limit (get_active_risk_positions_for_symbol).
5. Patch 3.4: Minimum-Lot (0.01) Portfolio Heat Allowance for Gold (check_portfolio_heat).
"""

import pytest
import pandas as pd
from datetime import datetime, timezone
from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit
from jarvis.execution.position_monitor import PositionMonitorEngine
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.risk.exposure import ExposureManager, ExposureEngine
from jarvis.risk.risk_engine import RiskEngine
from jarvis.data.schemas import (
    PositionSnapshot,
    AccountSnapshot,
    MarketContext,
    StructureContext,
    LiquidityContext,
    VolatilityContext,
    MomentumContext,
    SessionContext,
)
from jarvis.data.symbol_registry import resolve


def test_issue_1_proximity_harvesting_guard():
    """Verify _should_harvest_proximity prevents premature harvest and clips below 1.15R."""
    class DummyMonitor(PositionMonitorEngine):
        def __init__(self):
            pass

    monitor = DummyMonitor()

    # Case 1: Below 1.15R -> Must return False even at 95% progress
    assert not monitor._should_harvest_proximity(
        progress_to_tp=0.95, current_r=1.05, bias="BUY", last_bar_open=4150.0, last_bar_close=4149.0
    )

    # Case 2: Below 92% progress -> Must return False even with rejection
    assert not monitor._should_harvest_proximity(
        progress_to_tp=0.89, current_r=1.50, bias="BUY", last_bar_open=4150.0, last_bar_close=4149.0
    )

    # Case 3: Progress 93%, current_r 1.30R, BUY candle expanding into TP (close > open) -> Must return False (let it hit TP limit)
    assert not monitor._should_harvest_proximity(
        progress_to_tp=0.93, current_r=1.30, bias="BUY", last_bar_open=4150.0, last_bar_close=4152.0
    )

    # Case 4: Progress 93%, current_r 1.30R, BUY candle stalling/rejecting (close < open) -> Must return True (harvest to bank)
    assert monitor._should_harvest_proximity(
        progress_to_tp=0.93, current_r=1.30, bias="BUY", last_bar_open=4152.0, last_bar_close=4150.0
    )

    # Case 5: Progress 93%, current_r 1.30R, SELL candle stalling/rejecting (close > open) -> Must return True
    assert monitor._should_harvest_proximity(
        progress_to_tp=0.93, current_r=1.30, bias="SELL", last_bar_open=4140.0, last_bar_close=4142.0
    )

    # Case 6: Progress 98% (within spread noise of TP), current_r 1.50R -> Must harvest unconditionally
    assert monitor._should_harvest_proximity(
        progress_to_tp=0.98, current_r=1.50, bias="BUY", last_bar_open=4150.0, last_bar_close=4152.0
    )


def test_issue_2_exit_policy_relative_progress_and_be_safeguards():
    """Verify safe progress-to-tp and excursion floor prevent choking trades inside spread noise."""
    policy = ExitPolicy.for_symbol("GOLD.i#", resolve("GOLD.i#"))

    # Case 1: Unassigned TP (tp == 0.0) -> No spurious BE lock
    dec_zero_tp = evaluate_exit(
        side="BUY",
        entry=4150.0,
        initial_sl=4140.0,
        current_sl=4140.0,
        tp=0.0,
        price=4153.0,
        favorable_dist=3.0,
        atr=8.0,
        policy=policy,
    )
    assert dec_zero_tp.be_locked is False

    # Case 2: Sub-0.45R excursion on short-horizon scalp (e.g. 0.30R) -> No premature BE stop
    dec_small_r = evaluate_exit(
        side="BUY",
        entry=4150.0,
        initial_sl=4140.0,  # 10 pts risk
        current_sl=4140.0,
        tp=4156.0,          # 6 pts target (0.60R)
        price=4153.0,
        favorable_dist=3.0,  # 50% of target move, but only 0.30R excursion
        atr=8.0,
        policy=policy,
    )
    assert dec_small_r.be_locked is False

    # Case 3: Legitimate >= 0.50R excursion on short-horizon scalp (e.g. 0.50R) -> Locks BE safely
    dec_valid_be = evaluate_exit(
        side="BUY",
        entry=4150.0,
        initial_sl=4140.0,  # 10 pts risk
        current_sl=4140.0,
        tp=4160.0,          # 10 pts target (1.0R)
        price=4155.0,
        favorable_dist=5.0,  # 50% progress, 0.50R excursion
        atr=8.0,
        policy=policy,
    )
    assert dec_valid_be.be_locked is True


def test_patch_3_2_bull_flag_pullback_hook_reclaim():
    """Verify bull-flag hook reclaim bypasses momentum deceleration."""
    engine = DecisionEngine()

    # Create synthetic DataFrame where price reclaims EMA9 and closes bullish
    df = pd.DataFrame({
        "open": [4140.0, 4142.0, 4145.0, 4143.0, 4142.0, 4141.0, 4144.0],
        "high": [4143.0, 4146.0, 4148.0, 4145.0, 4144.0, 4143.0, 4147.0],
        "low": [4139.0, 4141.0, 4142.0, 4141.0, 4140.0, 4140.0, 4143.0],
        "close": [4142.0, 4145.0, 4143.0, 4142.0, 4141.0, 4144.0, 4146.0],
    })

    ctx = MarketContext(
        symbol="GOLD.i#",
        timestamp=datetime.now(timezone.utc),
        current_price=4146.0,
        bid=4145.8,
        ask=4146.2,
        structure=StructureContext(bias="BULLISH"),
        liquidity=LiquidityContext(),
        volatility=VolatilityContext(atr=5.0),
        momentum=MomentumContext(trend_score=25),
        session=SessionContext(),
    )

    is_reclaim = engine._check_pullback_reclaim(ctx, mtf_data={"timing": df}, bias="BUY")
    assert is_reclaim is True

    # Momentum deceleration test with pullback reclaim active
    valid, reason = engine._is_momentum_valid_for_regime(
        bias="BUY",
        mom_score=15.0,
        mom_delta=-20.0,  # Decelerating, but within -30.0 reclaim limit
        regime="RANGE",
        is_pullback_reclaim=True,
    )
    assert valid is True
    assert reason == "OK"


def test_patch_3_3_exposure_ignores_breakeven_runners():
    """Verify get_active_risk_positions_for_symbol ignores risk-free runners."""
    exp = ExposureManager()

    p_risk = PositionSnapshot(
        ticket=101, symbol="GOLD.i#", type="BUY", volume=0.01,
        open_price=4150.0, sl=4135.0, tp=4180.0, current_price=4155.0, profit=5.0,
        swap=0.0, commission=0.0, open_time="2026-10-17 00:00:00", magic=2026
    )
    p_derisked = PositionSnapshot(
        ticket=102, symbol="GOLD.i#", type="BUY", volume=0.01,
        open_price=4150.0, sl=4150.5, tp=4180.0, current_price=4165.0, profit=15.0,
        swap=0.0, commission=0.0, open_time="2026-10-17 00:00:00", magic=2026
    )

    # 1 open position at risk
    assert exp.get_active_risk_positions_for_symbol("GOLD.i#", [p_risk]) == 1

    # 1 open position locked at BE -> 0 active risk positions!
    assert exp.get_active_risk_positions_for_symbol("GOLD.i#", [p_derisked]) == 0

    # 1 at risk + 1 locked at BE -> 1 active risk position
    assert exp.get_active_risk_positions_for_symbol("GOLD.i#", [p_risk, p_derisked]) == 1


def test_patch_3_4_gold_min_lot_portfolio_heat_allowance():
    """Verify check_portfolio_heat grants allowance to minimum-lot Gold entry under heat."""
    risk_eng = RiskEngine()

    acc = AccountSnapshot(
        login=101059540, server="XMGlobal-MT5", balance=450.0, equity=435.0, margin=50.0,
        free_margin=385.0, margin_level=870.0, leverage=100, currency="USD"
    )

    class DummyDecision:
        symbol = "GOLD.i#"
        lot_size = 0.01

    res = risk_eng.check_portfolio_heat(
        account=acc,
        positions=[],
        decision=DummyDecision(),
        symbol="GOLD.i#"
    )

    # Must pass authorization for minimum-lot Gold
    assert res["passed"] is True
