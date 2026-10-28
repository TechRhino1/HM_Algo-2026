"""
Test Suite: Partial Close State Tracking & Reconciliation (Phase 4).
Verifies that MT5StateSynchronizer detects volume reductions on surviving positions
and fires POSITION_PARTIALLY_CLOSED on the event bus.
"""
import pytest
from unittest.mock import MagicMock
from jarvis.data.schemas import PositionSnapshot, AccountSnapshot
from jarvis.execution.state_synchronizer import MT5StateSynchronizer
from jarvis.application.state_manager import StateManager
from jarvis.application.event_bus import EventBus


def test_partial_close_detection():
    mock_mt5 = MagicMock()
    mock_mt5.is_connected = True
    mock_mt5.mode = "paper"
    
    account = AccountSnapshot(
        login=123456,
        server="TestServer",
        balance=10000.0,
        equity=10050.0,
        margin=50.0,
        free_margin=10000.0,
        margin_level=20000.0,
        leverage=100,
    )
    mock_mt5.get_account_snapshot.return_value = account

    # 1. Initial position with 0.10 lots
    pos1 = PositionSnapshot(
        ticket=999111,
        symbol="XAUUSD",
        type="BUY",
        volume=0.10,
        open_price=2000.0,
        current_price=2010.0,
        sl=1990.0,
        tp=2030.0,
        profit=100.0,
        swap=0.0,
        commission=0.0,
        open_time="2026-10-28 12:00:00",
        magic=888999,
    )
    mock_mt5.get_open_positions.return_value = [pos1]

    event_bus = EventBus()
    state_mgr = StateManager()
    sync = MT5StateSynchronizer(mt5_client=mock_mt5, state_manager=state_mgr, event_bus=event_bus)

    # First sync - registers ticket #999111 with volume 0.10
    sync.sync_once()
    assert sync._last_position_volumes.get(999111) == 0.10

    # Capture events
    partial_events = []
    closed_events = []
    event_bus.subscribe("POSITION_PARTIALLY_CLOSED", lambda e: partial_events.append(e))
    event_bus.subscribe("POSITION_CLOSED", lambda e: closed_events.append(e))

    # 2. Position partially closed: volume dropped to 0.05 lots
    pos2 = PositionSnapshot(
        ticket=999111,
        symbol="XAUUSD",
        type="BUY",
        volume=0.05,
        open_price=2000.0,
        current_price=2015.0,
        sl=2000.0,
        tp=2030.0,
        profit=75.0,
        swap=0.0,
        commission=0.0,
        open_time="2026-10-28 12:00:00",
        magic=888999,
    )
    mock_mt5.get_open_positions.return_value = [pos2]

    sync.sync_once()

    assert len(partial_events) == 1
    ev = partial_events[0]
    assert ev["ticket"] == 999111
    assert ev["closed_volume"] == 0.05
    assert ev["remaining_volume"] == 0.05
    assert len(closed_events) == 0
    assert sync._last_position_volumes.get(999111) == 0.05

    # 3. Position fully closed (empty positions list)
    mock_mt5.get_open_positions.return_value = []
    sync.sync_once()

    assert len(closed_events) == 1
    assert closed_events[0]["ticket"] == 999111
    assert 999111 not in sync._last_position_volumes
