"""Unit tests for the 5-Rung Fallback Ladder, Avellaneda-Stoikov pricing, and Brier calibration."""
import pytest
import math
from jarvis.risk.fallback_ladder import (
    FallbackRung,
    FallbackLadderState,
    select_fallback_rung,
)
from jarvis.intelligence.as_pricing import (
    reservation_price,
    half_spread,
    optimal_quote_prices,
    compute_as_limit_entry,
)
from jarvis.intelligence.calibration_engine import (
    CalibrationEngine,
    PredictionRecord,
)


def test_fallback_ladder_precedence():
    # 1. Kill always takes priority
    assert select_fallback_rung(
        risk_kill=True, decision_late=False, ai_down=False
    ) == FallbackRung.KILL

    # 2. Late holds quotes
    assert select_fallback_rung(
        risk_kill=False, decision_late=True, ai_down=False
    ) == FallbackRung.HOLD_LATE

    # 3. AI down drops to deterministic rules
    assert select_fallback_rung(
        risk_kill=False, decision_late=False, ai_down=True
    ) == FallbackRung.RULES_ONLY

    # 4. Low confidence reduces size
    assert select_fallback_rung(
        risk_kill=False, decision_late=False, ai_down=False, decision_confidence=0.40
    ) == FallbackRung.REDUCE

    # 5. Degraded execution reduces size
    assert select_fallback_rung(
        risk_kill=False, decision_late=False, ai_down=False, decision_confidence=0.85, execution_health_score=0.8
    ) == FallbackRung.REDUCE

    # 6. Green runs normal
    assert select_fallback_rung(
        risk_kill=False, decision_late=False, ai_down=False, decision_confidence=0.85, execution_health_score=2.5
    ) == FallbackRung.RUN


def test_fallback_ladder_multipliers():
    state = FallbackLadderState(reduce_factor=0.5)
    rung = state.evaluate(risk_kill=False, decision_late=False, ai_down=False, confidence=0.85)
    assert rung == FallbackRung.RUN
    assert state.sizing_multiplier == 1.0

    state.evaluate(risk_kill=False, decision_late=False, ai_down=False, confidence=0.35)
    assert state.current_rung == FallbackRung.REDUCE
    assert state.sizing_multiplier == 0.5

    state.evaluate(risk_kill=True, decision_late=False, ai_down=False)
    assert state.current_rung == FallbackRung.KILL
    assert state.sizing_multiplier == 0.0


def test_avellaneda_stoikov_inventory_skew():
    mid = 2650.0
    # Flat inventory: reservation price == mid
    r_flat = reservation_price(mid=mid, inventory=0.0, gamma=0.1, sigma=0.002, time_left_s=60.0)
    assert r_flat == mid

    # Long inventory (q > 0): reservation price must be below mid to skew towards selling
    r_long = reservation_price(mid=mid, inventory=2.0, gamma=0.1, sigma=0.002, time_left_s=60.0)
    assert r_long < mid

    # Short inventory (q < 0): reservation price must be above mid to skew towards buying
    r_short = reservation_price(mid=mid, inventory=-2.0, gamma=0.1, sigma=0.002, time_left_s=60.0)
    assert r_short > mid


def test_as_limit_entry_pricing():
    entry_buy = compute_as_limit_entry(
        mid=2650.0,
        side="BUY",
        inventory=0.0,
        sigma=0.002,
        spread_pips=2.0,
        pip_size=0.1,
    )
    assert entry_buy["side"] == "BUY"
    assert entry_buy["optimal_limit_price"] <= 2650.0

    entry_sell = compute_as_limit_entry(
        mid=2650.0,
        side="SELL",
        inventory=0.0,
        sigma=0.002,
        spread_pips=2.0,
        pip_size=0.1,
    )
    assert entry_sell["side"] == "SELL"
    assert entry_sell["optimal_limit_price"] >= 2650.0


def test_brier_score_calibration():
    engine = CalibrationEngine(horizon_bars=2)
    # Record predictions
    engine.record_prediction(timestamp=100.0, symbol="XAUUSD", direction="up", confidence=0.8, start_mid=2650.0)
    engine.record_prediction(timestamp=100.0, symbol="EURUSD", direction="down", confidence=0.7, start_mid=1.0850)

    # Elapse 1 bar (still pending)
    engine.update_outcomes(current_mid=2655.0, bars_elapsed=1)
    assert len(engine.pending_predictions) == 2
    assert len(engine.completed_predictions) == 0

    # Elapse 2nd bar (matures and evaluates)
    engine.update_outcomes(current_mid=2658.0, bars_elapsed=1)
    assert len(engine.pending_predictions) == 0
    assert len(engine.completed_predictions) == 2

    # Check Brier score and reliability table
    score = engine.brier_score()
    assert 0.0 <= score <= 1.0
    table = engine.reliability_table(n_bins=5)
    assert len(table) == 5
    summary = engine.to_dashboard_dict()
    assert summary["total_decisions"] == 2
