"""
Test Suite: Decoupled Circuit Breaker & Asset Cooldown (Phase 2).
Verifies that individual asset cooldowns isolate symbol risk while global
portfolio trips trigger only at calibrated portfolio-wide loss limits.
"""
import pytest
import time
from jarvis.risk.circuit_breaker import CircuitBreaker


def test_asset_pause_isolation_without_global_trip():
    # Use in-memory DB and fake clock
    clock_state = {"now": 1000000.0}
    def fake_clock():
        return clock_state["now"]

    breaker = CircuitBreaker(db_path="", clock=fake_clock, consecutive_loss_limit=5)
    
    # 1st loss on Gold at t=0
    breaker.record_trade_result(is_win=False, symbol="XAUUSD", regime="TREND_BULL")
    assert not breaker.is_tripped
    assert not breaker.is_symbol_paused("XAUUSD")

    # 2nd loss on Gold 15 minutes later (t=900s). Velocity is 2 (below 3 threshold).
    clock_state["now"] += 900.0
    breaker.record_trade_result(is_win=False, symbol="XAUUSD", regime="TREND_BULL")
    assert not breaker.is_tripped
    # Gold is now paused for 45 minutes (until t=900 + 2700 = 3600s)
    assert breaker.is_symbol_paused("XAUUSD")
    # Other symbols must NOT be paused
    assert not breaker.is_symbol_paused("EURUSD")
    assert not breaker.is_symbol_paused("BTCUSD")

    # 3rd loss on EURUSD 35 minutes later (t=3000s).
    # Trade 1 (t=0) is now > 45m ago (3000s > 2700s), so recent losses in 45m = 2 (t=900 and t=3000).
    clock_state["now"] = 1003000.0
    breaker.record_trade_result(is_win=False, symbol="EURUSD", regime="RANGE")
    # Global breaker should NOT trip because limit is 5 and velocity is 2
    assert not breaker.is_tripped
    assert breaker.consecutive_losses == 3
    # XAUUSD still paused (pause until 1003600.0 > 1003000.0)
    assert breaker.is_symbol_paused("XAUUSD")
    # EURUSD has only 1 loss, so not paused
    assert not breaker.is_symbol_paused("EURUSD")


def test_global_portfolio_trip_at_threshold():
    clock_state = {"now": 1000000.0}
    def fake_clock():
        return clock_state["now"]

    breaker = CircuitBreaker(db_path="", clock=fake_clock, consecutive_loss_limit=5)
    
    # Space losses by 1 hour (3600s) to test pure streak limit without tripping 45m loss velocity
    for i in range(4):
        breaker.record_trade_result(is_win=False, symbol=f"SYM_{i}")
        assert not breaker.is_tripped
        assert breaker.consecutive_losses == i + 1
        clock_state["now"] += 3600.0

    # 5th loss trips the entire portfolio
    breaker.record_trade_result(is_win=False, symbol="SYM_5")
    assert breaker.is_tripped
    assert "consecutive loss limit reached" in breaker.trip_reason
