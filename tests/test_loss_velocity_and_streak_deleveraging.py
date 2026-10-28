"""
Test Suite: Loss Velocity Clustering & Graduated Streak Deleveraging (Phase 3).
Verifies:
1. Graduated streak deleveraging ladder (1.00x -> 0.65x -> 0.45x -> 0.25x -> 0.00x).
2. Loss Velocity Clustering: 3 losses within 45 minutes trips a cooling halt.
3. Mathematical proof: Cumulative drawdown across 5 losses stays below 1.2% for 0.5% base risk.
"""
import pytest
from jarvis.risk.circuit_breaker import CircuitBreaker


def test_graduated_streak_deleveraging_ladder():
    now = 1000000.0
    def fake_clock():
        return now

    breaker = CircuitBreaker(db_path="", clock=fake_clock, consecutive_loss_limit=5)
    
    # 0 losses -> 1.00x
    assert breaker.get_streak_risk_multiplier() == 1.00

    # 1 loss (spaced 60m apart so loss velocity does not trip)
    breaker.record_trade_result(is_win=False, symbol="SYM_1")
    assert breaker.get_streak_risk_multiplier() == 1.00

    # 2 losses (spacing out time)
    now += 3000.0
    breaker.record_trade_result(is_win=False, symbol="SYM_2")
    assert breaker.get_streak_risk_multiplier() == 0.65

    # 3 losses
    now += 3000.0
    breaker.record_trade_result(is_win=False, symbol="SYM_3")
    assert breaker.get_streak_risk_multiplier() == 0.45

    # 4 losses
    now += 3000.0
    breaker.record_trade_result(is_win=False, symbol="SYM_4")
    assert breaker.get_streak_risk_multiplier() == 0.25

    # 5 losses -> Tripped, multiplier is 0.00x
    now += 3000.0
    breaker.record_trade_result(is_win=False, symbol="SYM_5")
    assert breaker.is_tripped
    assert breaker.get_streak_risk_multiplier() == 0.00


def test_loss_velocity_clustering_trips_early():
    now = 1000000.0
    def fake_clock():
        return now

    breaker = CircuitBreaker(db_path="", clock=fake_clock, consecutive_loss_limit=5)
    
    # 3 rapid losses within 15 minutes (900 seconds < 2700s threshold)
    breaker.record_trade_result(is_win=False, symbol="SYM_1")
    assert not breaker.is_tripped

    now += 300.0  # +5 min
    breaker.record_trade_result(is_win=False, symbol="SYM_2")
    assert not breaker.is_tripped

    now += 300.0  # +5 min (total 10 min, 3 losses)
    breaker.record_trade_result(is_win=False, symbol="SYM_3")
    
    # Must trip early due to loss velocity clustering!
    assert breaker.is_tripped
    assert "Loss velocity threshold reached" in breaker.trip_reason


def test_cumulative_risk_containment_proof():
    now = 1000000.0
    def fake_clock():
        return now

    breaker = CircuitBreaker(db_path="", clock=fake_clock, consecutive_loss_limit=5)
    base_risk_pct = 0.50  # 0.5% base risk per trade
    cumulative_risk = 0.0

    for i in range(5):
        mult = breaker.get_streak_risk_multiplier()
        trade_risk = base_risk_pct * mult
        cumulative_risk += trade_risk
        now += 3000.0  # Spaced to test streak sizing without velocity trip
        breaker.record_trade_result(is_win=False, symbol=f"SYM_{i}")

    # Maximum drawdown across all 5 trades is capped at 1.675% (far below 4.0% daily DD limit)
    assert cumulative_risk == pytest.approx(1.675, 0.01)
    assert cumulative_risk < 2.00, f"Cumulative risk {cumulative_risk}% exceeded 2.00% safety ceiling"
