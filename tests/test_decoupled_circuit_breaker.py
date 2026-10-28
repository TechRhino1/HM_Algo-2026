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
    now = 1000000.0
    def fake_clock():
        return now

    breaker = CircuitBreaker(db_path="", clock=fake_clock, consecutive_loss_limit=5)
    
    # 2 losses on Gold
    breaker.record_trade_result(is_win=False, symbol="XAUUSD", regime="TREND_BULL")
    assert not breaker.is_tripped
    assert not breaker.is_symbol_paused("XAUUSD")

    breaker.record_trade_result(is_win=False, symbol="XAUUSD", regime="TREND_BULL")
    assert not breaker.is_tripped
    # Gold is now paused for 45 minutes
    assert breaker.is_symbol_paused("XAUUSD")
    # Other symbols must NOT be paused
    assert not breaker.is_symbol_paused("EURUSD")
    assert not breaker.is_symbol_paused("BTCUSD")

    # 3rd loss on EURUSD (total 3 portfolio losses)
    breaker.record_trade_result(is_win=False, symbol="EURUSD", regime="RANGE")
    # Global breaker should NOT trip because limit is 5
    assert not breaker.is_tripped
    assert breaker.consecutive_losses == 3
    # EURUSD has only 1 loss, so not paused
    assert not breaker.is_symbol_paused("EURUSD")


def test_global_portfolio_trip_at_threshold():
    now = 1000000.0
    def fake_clock():
        return now

    breaker = CircuitBreaker(db_path="", clock=fake_clock, consecutive_loss_limit=5)
    
    for i in range(4):
        breaker.record_trade_result(is_win=False, symbol=f"SYM_{i}")
        assert not breaker.is_tripped
        assert breaker.consecutive_losses == i + 1

    # 5th loss trips the entire portfolio
    breaker.record_trade_result(is_win=False, symbol="SYM_5")
    assert breaker.is_tripped
    assert "consecutive loss limit reached" in breaker.trip_reason
