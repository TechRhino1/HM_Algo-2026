"""
HM Algo 2.0 — Verification and Parity Tests for Specification v2.1.

Verifies:
1. Zero direct discretionary close_position() calls in PositionMonitor.
2. 180s post-entry discretionary grace period suppression.
3. Manual trade isolation (PROTECT_ONLY).
4. Fail-closed execution mode matrix (demo / live fail closed if MT5 disconnected).
5. Default execution mode is demo.
6. Pre-fill broker-executable stop-loss distance calculation.
"""
import inspect
import re
import pytest
from unittest.mock import MagicMock, patch

from jarvis.config.settings import SETTINGS, verify_execution_mode
from jarvis.execution.position_monitor import (
    PositionMonitorEngine,
    DISCRETIONARY_GRACE_PERIOD_SEC,
    MANUAL_MANAGEMENT_MODE,
)
from jarvis.execution.mt5_client import MT5Client
from jarvis.execution.execution_engine import ExecutionEngine
from jarvis.data.schemas import PositionSnapshot, DecisionObject, ExecutionMode


def test_default_mode_is_demo():
    """Verify that default execution mode is demo per Spec v2.1."""
    from jarvis.config.settings import JarvisConfig
    cfg = JarvisConfig.load()
    assert cfg.trading.default_mode == "demo"
    assert verify_execution_mode("") == "demo"
    assert verify_execution_mode(None) == "demo"
    assert verify_execution_mode("live") == "live"
    assert verify_execution_mode("demo") == "demo"
    assert verify_execution_mode("paper") == "paper"


def test_zero_direct_discretionary_closes_in_position_monitor():
    """Static source code inspection: ensure no direct close_position(pos.ticket) outside _execute_exit."""
    source = inspect.getsource(PositionMonitorEngine)
    
    # Locate _manage_single_position in source
    assert "def _manage_single_position" in source
    start_idx = source.index("def _manage_single_position")
    # End is at next def or end of class
    next_def = source.find("\n    def ", start_idx + 10)
    manage_code = source[start_idx:next_def] if next_def != -1 else source[start_idx:]
    
    # Any call to close_position in _manage_single_position must be a partial volume close (volume=...)
    full_closes = re.findall(r"self\.mt5_client\.close_position\(\s*pos\.ticket\s*\)", manage_code)
    assert len(full_closes) == 0, f"Found rogue direct close_position calls in _manage_single_position: {full_closes}"


def test_discretionary_grace_period_suppression():
    """Verify that within 180s of opening, discretionary exits and modifications are suppressed."""
    mock_mt5 = MagicMock()
    mock_data = MagicMock()
    mock_ctx_engine = MagicMock()
    mock_state = MagicMock()
    
    pm = PositionMonitorEngine(
        mt5_client=mock_mt5,
        data_feed=mock_data,
        context_engine=mock_ctx_engine,
        state_manager=mock_state
    )
    
    # Position opened 30 seconds ago (well inside 180s grace period)
    pos = PositionSnapshot(
        ticket=12345,
        symbol="EURUSD",
        type="BUY",
        volume=0.10,
        open_price=1.1000,
        current_price=1.0995,  # slight spread/loss
        sl=1.0950,
        tp=1.1100,
        profit=-5.0,
        swap=0.0,
        commission=0.0,
        open_time="2026-09-25 12:00:00",
        magic=888999,  # bot trade
        comment="HMA2_TEST"
    )
    
    # Mock duration to 30.0s (< 180s)
    pm._get_position_duration_sec = MagicMock(return_value=30.0)
    
    # Mock context
    mock_ctx = MagicMock()
    mock_ctx.current_price = 1.0995
    mock_ctx.volatility.atr = 0.0010
    mock_ctx.volatility.current_spread_pips = 1.0
    pm._get_context = MagicMock(return_value=mock_ctx)
    pm._get_cached_regime = MagicMock(return_value=None)
    
    pm._manage_single_position(pos)
    
    # MT5 client must NOT have been called to close or modify position during grace period
    mock_mt5.close_position.assert_not_called()
    mock_mt5.modify_position.assert_not_called()


def test_manual_trade_isolation_protect_only():
    """Verify that manual trades are isolated: only set SL if missing, never trail or close."""
    mock_mt5 = MagicMock()
    mock_data = MagicMock()
    mock_ctx_engine = MagicMock()
    mock_state = MagicMock()
    
    pm = PositionMonitorEngine(
        mt5_client=mock_mt5,
        data_feed=mock_data,
        context_engine=mock_ctx_engine,
        state_manager=mock_state
    )
    
    # Manual trade with existing SL
    pos_with_sl = PositionSnapshot(
        ticket=55555,
        symbol="EURUSD",
        type="BUY",
        volume=0.50,
        open_price=1.1000,
        current_price=1.1050,  # in profit
        sl=1.0950,             # existing SL
        tp=0.0,
        profit=250.0,
        swap=0.0,
        commission=0.0,
        open_time="2026-09-25 10:00:00",
        magic=0,               # manual trade
        comment="Manual phone"
    )
    
    pm._get_position_duration_sec = MagicMock(return_value=7200.0)
    mock_ctx = MagicMock()
    mock_ctx.current_price = 1.1050
    mock_ctx.volatility.atr = 0.0010
    mock_ctx.volatility.current_spread_pips = 1.0
    pm._get_context = MagicMock(return_value=mock_ctx)
    pm._get_cached_regime = MagicMock(return_value=None)
    
    pm._manage_single_position(pos_with_sl)
    
    # Must NOT trail or close manual trade with existing SL
    mock_mt5.close_position.assert_not_called()
    mock_mt5.modify_position.assert_not_called()
    
    # Manual trade with MISSING SL (sl == 0)
    pos_no_sl = PositionSnapshot(
        ticket=55556,
        symbol="EURUSD",
        type="BUY",
        volume=0.50,
        open_price=1.1000,
        current_price=1.1010,
        sl=0.0,                # missing SL!
        tp=0.0,
        profit=50.0,
        swap=0.0,
        commission=0.0,
        open_time="2026-09-25 10:00:00",
        magic=0,               # manual trade
        comment="Manual phone"
    )
    
    pm._manage_single_position(pos_no_sl)
    # Should set emergency SL on manual trade with missing SL
    mock_mt5.modify_position.assert_called_once()
    assert mock_mt5.close_position.call_count == 0


def test_fail_closed_mode_matrix():
    """Verify that MT5Client fails closed in demo or live mode when MT5 is disconnected."""
    client_live = MT5Client(mode="live", auto_init=False)
    client_live.is_connected = False
    client_live._reconnect_if_needed = MagicMock()  # prevent real connection attempt during unit test
    
    with patch("jarvis.execution.mt5_client.mt5", None):
        res = client_live.send_market_order("EURUSD", "BUY", 0.01, 1.0900, 1.1100)
        assert res.get("status") == "FAILED"
        assert "FAIL_CLOSED" in res.get("reason", "")
        
        res_pending = client_live.place_pending_order("EURUSD", "BUY_LIMIT", 1.0950, 0.01, 1.0900, 1.1100)
        assert res_pending.get("status") == "FAILED"
        assert "FAIL_CLOSED" in res_pending.get("reason", "")
        
        client_demo = MT5Client(mode="demo", auto_init=False)
        client_demo.is_connected = False
        client_demo._reconnect_if_needed = MagicMock()
        
        res_demo = client_demo.send_market_order("EURUSD", "BUY", 0.01, 1.0900, 1.1100)
        assert res_demo.get("status") == "FAILED"
        assert "FAIL_CLOSED" in res_demo.get("reason", "")


def test_pre_fill_broker_safe_sl_adjustment():
    """Verify that ExecutionEngine adjusts SL to broker-executable minimum before dispatch."""
    mock_mt5 = MagicMock()
    mock_mt5.mode = "demo"
    mock_mt5.is_connected = True
    
    # Mock broker spec with 50 point stops_level (0.00050)
    mock_mt5.get_symbol_trading_spec = MagicMock(return_value={
        "symbol": "EURUSD",
        "trade_stops_level": 50,
        "spread": 10,
        "point": 0.00001,
        "digits": 5,
        "trade_tick_value": 1.0,
        "trade_tick_size": 0.00001,
        "volume_min": 0.01,
        "volume_step": 0.01,
    })
    
    mock_mt5.send_market_order = MagicMock(return_value={
        "status": "FILLED",
        "ticket": 99991,
        "price": 1.10000,
        "sl": 1.09950,
        "tp": 1.10500,
    })
    
    mock_state = MagicMock()
    mock_state.is_safe_mode = False
    mock_state.execution_mode = ExecutionMode.DEMO
    mock_state.account.equity = 10000.0
    
    ee = ExecutionEngine(mt5_client=mock_mt5, state_manager=mock_state)
    
    # Intended SL is only 10 points away (1.09990 vs entry 1.10000)
    decision = MagicMock()
    decision.symbol = "EURUSD"
    decision.bias = "BUY"
    decision.entry_price = 1.10000
    decision.stop_loss = 1.09990  # too tight!
    decision.take_profit = 1.10500
    decision.sl_distance = 0.00010
    decision.tp_distance = 0.00500
    decision.execution_authorized = True
    decision.strategy = "MOMENTUM_SCALP"
    decision.order_type = "MARKET"
    decision.risk_factors = []
    
    res = ee.execute_decision(decision, lots=0.10)
    
    # SL should have been widened to meet broker minimum (at least 50 points = 0.00050)
    assert decision.stop_loss <= 1.09950
    mock_mt5.send_market_order.assert_called_once()


def test_drawdown_reanchor_audit_table_and_post_reanchor_recalculation(tmp_path):
    """Verify that drawdown re-anchoring writes an immutable audit record and recalculates protections."""
    import sqlite3
    from tools.reanchor_drawdown_baseline import reanchor_drawdown_baseline
    
    test_db = str(tmp_path / "test_drawdown.db")
    
    # 1. Initialize test database with an old peak
    with sqlite3.connect(test_db) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drawdown_state (
                id INTEGER PRIMARY KEY,
                daily_start_equity REAL,
                peak_equity REAL,
                last_saved_date TEXT
            )
        """)
        conn.execute("""
            INSERT INTO drawdown_state (id, daily_start_equity, peak_equity, last_saved_date)
            VALUES (1, 1000.0, 1065.18, '2026-09-20')
        """)
        conn.commit()

    # 2. Run reanchor function
    result = reanchor_drawdown_baseline(
        db_path=test_db,
        target_equity=528.03,
        operator="TestOperator",
        reason="Spec v2.2 Automated Parity Test",
        login=101059540,
        server="XMGlobal-MT5",
        dry_run=False
    )

    assert result["audit_row_id"] > 0
    assert len(result["confirmation_token"]) == 16
    assert result["recalculated_status"] == "HEALTHY"
    assert result["drawdown_check"]["passed"] is True

    # 3. Verify baseline peak updated and audit table written
    with sqlite3.connect(test_db) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        # Verify baseline table
        cur.execute("SELECT * FROM drawdown_state WHERE id = 1")
        b_row = cur.fetchone()
        assert b_row is not None
        assert abs(b_row["peak_equity"] - 528.03) < 0.001
        assert abs(b_row["daily_start_equity"] - 528.03) < 0.001

        # Verify audit table exists and has row
        cur.execute("SELECT * FROM drawdown_reanchor_audit WHERE account_login = ?", (101059540,))
        audit_row = cur.fetchone()
        assert audit_row is not None
        assert audit_row["account_login"] == 101059540
        assert abs(audit_row["previous_peak_equity"] - 1065.18) < 0.001
        assert abs(audit_row["new_peak_equity"] - 528.03) < 0.001
        assert audit_row["confirmation_token"] == result["confirmation_token"]
        assert audit_row["reason"] == "Spec v2.2 Automated Parity Test"
        assert audit_row["operator"] == "TestOperator"


def test_broker_response_classification_taxonomy():
    """Verify broker response classification taxonomy per Spec v2.2."""
    mock_mt5 = MagicMock()
    mock_state = MagicMock()
    ee = ExecutionEngine(mt5_client=mock_mt5, state_manager=mock_state)

    # FILLED
    assert ee._classify_broker_response({"status": "FILLED", "ticket": 123}) == "FILLED"
    assert ee._classify_broker_response({"retcode": 10009, "ticket": 123}) == "FILLED"

    # ACCEPTED
    assert ee._classify_broker_response({"status": "PLACED"}) == "ACCEPTED"
    assert ee._classify_broker_response({"status": "ACCEPTED"}) == "ACCEPTED"
    assert ee._classify_broker_response({"retcode": 10008}) == "ACCEPTED"

    # PARTIAL
    assert ee._classify_broker_response({"status": "PARTIAL"}) == "PARTIAL"
    assert ee._classify_broker_response({"retcode": 10010}) == "PARTIAL"

    # REJECTED
    assert ee._classify_broker_response({"status": "REJECTED"}) == "REJECTED"
    assert ee._classify_broker_response({"retcode": 10013}) == "REJECTED"
    assert ee._classify_broker_response({"retcode": 10014}) == "REJECTED"

    # FAILED
    assert ee._classify_broker_response({"status": "FAILED"}) == "FAILED"
    assert ee._classify_broker_response({"status": "SAFETY_BLOCK"}) == "FAILED"

    # UNKNOWN
    assert ee._classify_broker_response(None) == "UNKNOWN"
    assert ee._classify_broker_response({}) == "UNKNOWN"
    assert ee._classify_broker_response({"status": "TIMEOUT"}) == "UNKNOWN"
    assert ee._classify_broker_response({"retcode": 10022}) == "UNKNOWN"

    # POSITION_CONFIRMED
    assert ee._classify_broker_response({"status": "POSITION_CONFIRMED"}) == "POSITION_CONFIRMED"


def test_setup_id_uuid_assignment_and_preservation():
    """Verify setup_id is generated as a UUID and preserved on decision."""
    mock_mt5 = MagicMock()
    mock_mt5.mode = "demo"
    mock_mt5.is_connected = True
    mock_mt5.get_symbol_trading_spec = MagicMock(return_value={
        "symbol": "EURUSD", "trade_stops_level": 0, "spread": 2, "point": 0.00001, "digits": 5
    })
    mock_mt5.send_market_order = MagicMock(return_value={"status": "FILLED", "ticket": 8888, "price": 1.10000})

    mock_state = MagicMock()
    mock_state.is_safe_mode = False
    mock_state.execution_mode = ExecutionMode.DEMO
    mock_state.account.equity = 10000.0

    ee = ExecutionEngine(mt5_client=mock_mt5, state_manager=mock_state)

    decision = MagicMock()
    decision.symbol = "EURUSD"
    decision.bias = "BUY"
    decision.entry_price = 1.10000
    decision.stop_loss = 1.09500
    decision.take_profit = 1.11000
    decision.sl_distance = 0.00500
    decision.tp_distance = 0.01000
    decision.execution_authorized = True
    decision.strategy = "TREND_PULSE"
    decision.order_type = "MARKET"
    decision.risk_factors = []
    decision.setup_id = None  # None initially
    decision.candidate_id = None

    with patch("jarvis.data.database.TRADE_DB.log_trade") as mock_log:
        res = ee.execute_decision(decision, lots=0.05)
        assert res.get("status") == "FILLED"
        # setup_id should have been generated as a valid UUID string
        assert decision.setup_id is not None
        import uuid
        parsed_uuid = uuid.UUID(decision.setup_id)
        assert str(parsed_uuid) == decision.setup_id

