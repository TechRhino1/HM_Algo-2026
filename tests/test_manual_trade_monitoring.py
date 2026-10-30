"""
Unit and Integration Tests for Manual Trades Monitoring & Institutional AI Control.
Tests cover:
1. StateManager telemetry enrichment preservation across broker refreshes.
2. Dynamic trade style classification from ATR stop-geometry.
3. Naked manual trade auto-SL/TP protection.
4. Manual trade 3-tier milestone progression & BE locking under AUTO_PILOT.
5. Mode controls (AUTO_PILOT, TRAIL, PROTECT_ONLY, OFF) and API endpoint contract.
"""
import io
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

from jarvis.data.schemas import PositionSnapshot, MarketContext
from jarvis.application.state_manager import StateManager
from jarvis.execution.position_monitor import PositionMonitorEngine
from jarvis.api.server import JarvisRequestHandler
from tests.test_ai_smart_tp_sl_milestones import _build_test_context


class _RouteHarness(JarvisRequestHandler):
    """Drives real do_POST handler in-memory without a network socket."""
    def __init__(self, path, payload, state_manager=None, orchestrator=None):
        self.path = path
        self.headers = {"Content-Length": str(len(payload))}
        self.rfile = io.BytesIO(payload)
        self.client_address = ("127.0.0.1", 12345)
        self.mt5_client = MagicMock()
        self.state_manager = state_manager or StateManager()
        self.orchestrator = orchestrator
        self.sent = []

    def _send_json(self, data, status_code=200, cookies=None):
        self.sent.append((status_code, data))

    def _require_role(self, *allowed_roles):
        return True, None


def _post(path, body, state_manager=None, orchestrator=None):
    payload = json.dumps(body).encode("utf-8")
    handler = _RouteHarness(path, payload, state_manager=state_manager, orchestrator=orchestrator)
    handler.do_POST()
    assert handler.sent, f"{path} answered nothing"
    return handler.sent[-1]


def _make_snapshot(ticket=1001, symbol="XAUUSD", side="BUY", volume=0.1, open_price=2650.0,
                   current_price=2655.0, sl=2640.0, tp=2680.0, profit=50.0, magic=0, comment="", **kwargs):
    return PositionSnapshot(
        ticket=ticket,
        symbol=symbol,
        type=side,
        volume=volume,
        open_price=open_price,
        current_price=current_price,
        sl=sl,
        tp=tp,
        profit=profit,
        swap=0.0,
        commission=0.0,
        open_time="2026-10-28 10:00:00",
        magic=magic,
        comment=comment,
        **kwargs,
    )


def test_state_manager_preserves_manual_telemetry():
    """Verify that StateManager retains milestone, BE lock, and mode telemetry across raw refreshes."""
    sm = StateManager()

    # Initial raw MT5 position
    raw_pos = _make_snapshot(
        ticket=1001,
        open_price=2650.0,
        current_price=2665.0,
        sl=2640.0,
        tp=2680.0,
        profit=75.0,
        magic=0,
    )
    sm.update_positions([raw_pos])
    assert sm.positions[0].milestone_status == "OPEN"
    assert sm.positions[0].is_be_locked is False

    # Position monitor enriches telemetry
    sm.enrich_position_telemetry(
        ticket=1001,
        telemetry={
            "tp1": 2665.0,
            "tp2": 2680.0,
            "tp3": 2700.0,
            "milestone_status": "TP1_HIT_BE_LOCKED",
            "is_be_locked": True,
            "current_r": 1.5,
            "mfe": 15.0,
            "mae": -2.0,
            "trade_style": "DAY_TRADING",
            "monitoring_mode": "AUTO_PILOT",
            "is_manual": True,
        }
    )

    pos = sm.positions[0]
    assert pos.is_manual is True
    assert pos.milestone_status == "TP1_HIT_BE_LOCKED"
    assert pos.is_be_locked is True
    assert pos.current_r == 1.5
    assert pos.trade_style == "DAY_TRADING"
    assert pos.monitoring_mode == "AUTO_PILOT"

    # Now simulate periodic StateSynchronizer 1-second refresh with raw snapshot
    raw_pos_tick2 = _make_snapshot(
        ticket=1001,
        open_price=2650.0,
        current_price=2666.0,
        sl=2650.5,  # Breakeven SL
        tp=2680.0,
        profit=80.0,
        magic=0,
    )
    sm.update_positions([raw_pos_tick2])

    # Assert that enriched telemetry was NOT wiped by raw sync
    pos_after = sm.positions[0]
    assert pos_after.is_manual is True
    assert pos_after.current_price == 2666.0
    assert pos_after.sl == 2650.5
    assert pos_after.milestone_status == "TP1_HIT_BE_LOCKED"
    assert pos_after.is_be_locked is True
    assert pos_after.current_r == 1.5
    assert pos_after.trade_style == "DAY_TRADING"
    assert pos_after.monitoring_mode == "AUTO_PILOT"


def test_manual_trade_detection_and_dynamic_atr_style():
    """Verify manual trade detection and dynamic ATR style classification."""
    mock_mt5 = MagicMock()
    mock_ctx_eng = MagicMock()
    mock_ctx_eng.get_context.return_value = _build_test_context("XAUUSD", current_price=2600.0)

    pm = PositionMonitorEngine(
        mt5_client=mock_mt5,
        data_feed=MagicMock(),
        context_engine=mock_ctx_eng,
        state_manager=StateManager(),
        event_bus=MagicMock(),
    )

    # 1. Manual order (magic != JARVIS_MAGIC_NUMBER)
    manual_pos = _make_snapshot(
        ticket=2001,
        open_price=2600.0,
        current_price=2605.0,
        sl=2595.0,  # 5.0 distance = 0.5 ATR (< 0.85 ATR -> SCALP)
        tp=2620.0,
        magic=0,
    )
    assert pm._is_manual_trade(manual_pos) is True
    style = pm._extract_position_trade_style(manual_pos)
    assert style == "SCALP"

    # 2. Day trading stop distance (1.2 ATR = 12.0 distance)
    manual_pos_day = _make_snapshot(
        ticket=2002,
        open_price=2600.0,
        current_price=2605.0,
        sl=2588.0,  # 12.0 distance = 1.2 ATR -> DAY_TRADING
        tp=2630.0,
        magic=0,
    )
    assert pm._extract_position_trade_style(manual_pos_day) == "DAY_TRADING"

    # 3. Swing stop distance (2.5 ATR = 25.0 distance)
    manual_pos_swing = _make_snapshot(
        ticket=2003,
        open_price=2600.0,
        current_price=2605.0,
        sl=2575.0,  # 25.0 distance = 2.5 ATR -> SWING
        tp=2660.0,
        magic=0,
    )
    assert pm._extract_position_trade_style(manual_pos_swing) == "SWING"


def test_naked_manual_trade_auto_protection():
    """Verify naked manual trade without SL/TP gets protected with ATR-based SL and 3-tier TP."""
    mock_mt5 = MagicMock()
    mock_mt5.modify_position.return_value = {"status": "MODIFIED"}
    mock_ctx_eng = MagicMock()
    ctx = _build_test_context("XAUUSD", current_price=2652.0)
    mock_ctx_eng.get_context.return_value = ctx

    state_mgr = StateManager()
    pm = PositionMonitorEngine(
        mt5_client=mock_mt5,
        data_feed=MagicMock(),
        context_engine=mock_ctx_eng,
        state_manager=state_mgr,
        event_bus=MagicMock(),
    )
    pm._ctx_cache["XAUUSD"] = (ctx, time.monotonic())

    # Naked manual BUY trade
    naked_pos = _make_snapshot(
        ticket=3001,
        open_price=2650.0,
        current_price=2652.0,
        sl=0.0,  # Naked
        tp=0.0,  # Naked
        magic=0,
    )

    pm._manage_single_position(naked_pos, equity=10000.0, balance=10000.0)

    # Verify modify_position was invoked with valid calculated SL and TP
    mock_mt5.modify_position.assert_called_once()
    call_args = mock_mt5.modify_position.call_args[1]
    assert call_args["ticket"] == 3001
    assert call_args["sl"] < 2650.0  # SL placed below entry for BUY
    assert call_args["tp"] > 2650.0  # TP placed above entry for BUY


def test_manual_trade_milestone_progression_in_autopilot():
    """Verify that in AUTO_PILOT mode, manual trades ratchet SL to BE at TP1 and progress milestones."""
    mock_mt5 = MagicMock()
    mock_mt5.modify_position.return_value = {"status": "MODIFIED"}
    mock_ctx_eng = MagicMock()
    ctx = _build_test_context("XAUUSD", current_price=2655.0)
    mock_ctx_eng.get_context.return_value = ctx

    state_mgr = StateManager()
    pm = PositionMonitorEngine(
        mt5_client=mock_mt5,
        data_feed=MagicMock(),
        context_engine=mock_ctx_eng,
        state_manager=state_mgr,
        event_bus=MagicMock(),
    )
    pm._ctx_cache["XAUUSD"] = (ctx, time.monotonic())

    # Initial state: Manual trade, 1R = 10.0 (entry=2650, sl=2640)
    pos_open = _make_snapshot(
        ticket=4001,
        open_price=2650.0,
        current_price=2655.0,  # +0.5R
        sl=2640.0,
        tp=2680.0,
        magic=0,
    )

    # Initialize tracking
    pm._manage_single_position(pos_open, equity=10000.0, balance=10000.0)
    assert pm.get_manual_mode(4001) == "AUTO_PILOT"

    # Price rallies to +1.6R (2666.0), hitting TP1 threshold
    pos_rallied = _make_snapshot(
        ticket=4001,
        open_price=2650.0,
        current_price=2666.0,  # > 1.5R
        sl=2640.0,
        tp=2680.0,
        profit=160.0,
        magic=0,
    )
    ctx.current_price = 2666.0
    pm._ctx_cache["XAUUSD"] = (ctx, time.monotonic())

    pm._manage_single_position(pos_rallied, equity=10000.0, balance=10000.0)

    # Assert modify_position was called to lock BE
    assert mock_mt5.modify_position.called
    latest_sl = mock_mt5.modify_position.call_args[1]["sl"]
    assert latest_sl >= 2650.0  # Ratcheted to at least BE (entry + buffer)

    # Assert StateManager received enriched telemetry
    cached = state_mgr._position_telemetry_cache.get(4001)
    assert cached is not None
    assert cached["milestone_status"] == "TP1_HIT_BE_LOCKED"
    assert cached["is_be_locked"] is True


def test_api_set_manual_mode_endpoint():
    """Verify /api/action/set_manual_mode endpoint updates mode and enforces validation."""
    state_mgr = StateManager()
    mock_pm = MagicMock()
    mock_pm.set_manual_mode.return_value = "TRAIL"
    mock_orch = MagicMock()
    mock_orch.position_monitor = mock_pm

    # 1. Valid mode change
    status, payload = _post(
        "/api/action/set_manual_mode",
        {"ticket": 5001, "mode": "TRAIL"},
        state_manager=state_mgr,
        orchestrator=mock_orch,
    )
    assert status == 200
    assert payload["status"] == "SUCCESS"
    assert payload["success"] is True
    assert payload["ticket"] == 5001
    assert payload["mode"] == "TRAIL"
    mock_pm.set_manual_mode.assert_called_with(ticket=5001, mode="TRAIL")

    # 2. Invalid mode returns status FAILED with reason
    status_bad, payload_bad = _post(
        "/api/action/set_manual_mode",
        {"ticket": 5001, "mode": "SUPER_TURBO"},
        state_manager=state_mgr,
        orchestrator=mock_orch,
    )
    assert status_bad == 200
    assert payload_bad["status"] == "FAILED"
    assert payload_bad["success"] is False
    assert "Invalid mode" in payload_bad["reason"]


def test_master_trader_sl_tp_symbol_spec_compatibility():
    """Verify Master Trader AI handles get_symbol_trading_spec and get_symbol_info safely."""
    from jarvis.execution.mt5_client import MT5Client
    from jarvis.data.symbol_registry import resolve

    # Verify MT5Client class has get_symbol_info alias
    client = MT5Client(mode="paper")
    assert hasattr(client, "get_symbol_info")
    assert hasattr(client, "get_symbol_trading_spec")

    spec_dict = client.get_symbol_info("GOLD.i#")
    assert spec_dict["trade_contract_size"] > 0
    assert "point" in spec_dict
    assert "trade_stops_level" in spec_dict

    # Verify PositionMonitorEngine executes _calculate_master_trader_sl_tp without AttributeError
    pm = PositionMonitorEngine(
        mt5_client=client,
        data_feed=MagicMock(),
        context_engine=MagicMock(),
        state_manager=StateManager(),
    )
    pos = _make_snapshot(ticket=977052240, symbol="GOLD.i#", side="BUY", open_price=4058.29, sl=0.0, tp=0.0)
    spec = resolve("GOLD.i#")

    sl, tp, reason = pm._calculate_master_trader_sl_tp(
        pos=pos,
        ctx=None,
        c_price=4053.33,
        atr=10.0,
        equity=500.0,
        spec=spec,
        digits=spec.digits,
        pip_size=spec.pip_size,
    )
    assert sl > 0
    assert sl < pos.open_price  # Stop loss below entry for BUY
    assert tp > pos.open_price  # Take profit above entry for BUY
    assert "MASTER_TRADER_AI" in reason


def test_manual_mode_remains_autopilot_across_ticks():
    """Verify that a newly discovered manual trade remains in AUTO_PILOT and does not flip to TRAIL."""
    from jarvis.application.orchestrator import JarvisOrchestrator
    state_mgr = StateManager()
    mock_client = MagicMock()
    mock_client.get_symbol_trading_spec.return_value = {
        "point": 0.00001,
        "trade_stops_level": 0,
        "trade_freeze_level": 0,
        "spread": 5,
    }
    mock_ctx_eng = MagicMock()
    ctx = _build_test_context("EURUSD", current_price=1.1010)
    mock_ctx_eng.get_context.return_value = ctx

    pm = PositionMonitorEngine(
        mt5_client=mock_client,
        data_feed=MagicMock(),
        context_engine=mock_ctx_eng,
        state_manager=state_mgr,
    )
    pm._ctx_cache["EURUSD"] = (ctx, time.monotonic())
    # Default manual mode must be AUTO_PILOT
    assert pm.manual_mode == "AUTO_PILOT"

    # Simulate manual trade (magic=0)
    pos = _make_snapshot(ticket=99991, symbol="EURUSD", side="BUY", open_price=1.1000, current_price=1.1010, sl=1.0950, tp=1.1100, magic=0)
    assert pos.monitoring_mode == "AUTO_PILOT"

    pm._manage_single_position(pos, balance=1000.0, equity=1000.0)

    # Must stay in AUTO_PILOT
    assert pos.monitoring_mode == "AUTO_PILOT"
    assert pm.get_manual_mode(99991) == "AUTO_PILOT"


def test_get_manual_mode_cache_fallback():
    """Verify get_manual_mode recovers state from state_manager telemetry cache when not in local map."""
    state_mgr = StateManager()
    state_mgr.enrich_position_telemetry(8888, {"monitoring_mode": "TRAIL"})

    pm = PositionMonitorEngine(
        mt5_client=MagicMock(),
        data_feed=MagicMock(),
        context_engine=MagicMock(),
        state_manager=state_mgr,
    )
    assert 8888 not in pm._manual_trade_modes

    # get_manual_mode should restore from telemetry cache
    mode = pm.get_manual_mode(8888)
    assert mode == "TRAIL"
    assert pm._manual_trade_modes[8888] == "TRAIL"


def test_institutional_trailing_parity_automated_vs_manual():
    """Verify 1-way trailing stop ratchets, BE lock, and milestone parity between automated and manual trades."""
    from jarvis.execution.position_monitor import JARVIS_MAGIC_NUMBER

    state_mgr = StateManager()
    mock_client = MagicMock()
    mock_client.get_symbol_trading_spec.return_value = {
        "point": 0.01,
        "trade_stops_level": 0,
        "trade_freeze_level": 0,
        "spread": 10,
    }
    mock_client.modify_position.return_value = {"status": "MODIFIED"}
    mock_ctx_eng = MagicMock()
    ctx = _build_test_context("XAUUSD", current_price=2650.0)
    ctx.volatility.current_spread_pips = 1.0  # Normal spread
    mock_ctx_eng.get_context.return_value = ctx

    pm = PositionMonitorEngine(
        mt5_client=mock_client,
        data_feed=MagicMock(),
        context_engine=mock_ctx_eng,
        state_manager=state_mgr,
    )
    pm._ctx_cache["XAUUSD"] = (ctx, time.monotonic())

    # Create one automated trade and one manual trade with identical geometry
    auto_pos = _make_snapshot(ticket=1001, symbol="XAUUSD", side="BUY", open_price=2650.0, current_price=2650.0, sl=2640.0, tp=2680.0, magic=JARVIS_MAGIC_NUMBER, comment="HMA2_DAY_CHOC")
    man_pos = _make_snapshot(ticket=1002, symbol="XAUUSD", side="BUY", open_price=2650.0, current_price=2650.0, sl=2640.0, tp=2680.0, magic=0)

    # Tick 1: Open price (1R = 10.0)
    pm._manage_single_position(auto_pos, balance=1000.0, equity=1000.0)
    pm._manage_single_position(man_pos, balance=1000.0, equity=1000.0)

    # Initial risk distance must be frozen identically
    assert pm._initial_risk_dist[1001] == pytest.approx(10.0, abs=1e-5)
    assert pm._initial_risk_dist[1002] == pytest.approx(10.0, abs=1e-5)

    # Tick 2: Favorable excursion +1.6R (price = 2666.0) -> Triggers Fast Cash TP1 & BE ratchet
    auto_pos.current_price = 2666.0
    man_pos.current_price = 2666.0
    ctx.current_price = 2666.0
    pm._ctx_cache["XAUUSD"] = (ctx, time.monotonic())

    pm._manage_single_position(auto_pos, balance=1000.0, equity=1000.0)
    pm._manage_single_position(man_pos, balance=1000.0, equity=1000.0)

    # Both positions must have BE locked and milestones initialized
    assert 1001 in pm._be_locked
    assert 1002 in pm._be_locked
    assert pm._position_milestones[1001]["status"] in ("TP1_HIT_BE_LOCKED", "OPEN")
    assert pm._position_milestones[1002]["status"] in ("TP1_HIT_BE_LOCKED", "OPEN")
