"""Regressions for the live-readiness fixes.

Each test pins a defect that would have let the platform behave differently
from what it reported:

* an operator emergency stop must latch across restarts and never auto-expire;
* "resume trading" must not erase the peak-equity drawdown history;
* a break-even trade is not a loss;
* a broker "PLACED" ack is not a fill, and a partial fill is reported as such;
* the gold minimum-lot heat bypass needs a genuinely known lot size;
* a mode switch with live exposure must be refused;
* positions closed while the process was down must be reconciled at startup.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jarvis.api.server import JarvisRequestHandler
from jarvis.application.orchestrator import JarvisOrchestrator
from jarvis.execution.mt5_client import MT5Client
from jarvis.risk.circuit_breaker import CircuitBreaker
from jarvis.risk.drawdown import DrawdownGuard


# ── operator halt latch ─────────────────────────────────────────────────────

def _breaker(tmp_path, clock=None):
    return CircuitBreaker(db_path=str(tmp_path / "circuit.db"), clock=clock)


def test_operator_halt_never_auto_expires(tmp_path):
    # The breaker's injectable clock advances in EPOCH SECONDS, not datetimes.
    now = [1_800_000_000.0]
    cb = _breaker(tmp_path, clock=lambda: now[0])
    cb.trip("OPERATOR_EMERGENCY_STOP: kill switch")

    # A full day later -- many cooldowns -- the halt must still be active.
    now[0] += 86_400.0
    status = cb.check_status()
    assert status["active"] is True
    assert status.get("operator_halt") is True
    assert status["reason"].startswith("OPERATOR")


def test_operator_halt_survives_restart(tmp_path):
    path = str(tmp_path / "circuit.db")
    first = CircuitBreaker(db_path=path)
    first.trip("MANUAL_HALT: operator pressed stop")

    reopened = CircuitBreaker(db_path=path, clock=lambda: datetime.now(timezone.utc).timestamp() + 7 * 86_400.0)
    assert reopened.operator_halt is True
    assert reopened.check_status()["active"] is True


def test_only_reset_clears_a_latched_halt(tmp_path):
    cb = _breaker(tmp_path)
    cb.trip("EMERGENCY_STOP")
    assert cb.check_status()["active"] is True
    cb.reset()
    assert cb.operator_halt is False
    assert cb.check_status()["active"] is False


def test_loss_streak_trip_still_auto_expires(tmp_path):
    now = [1_800_000_000.0]
    cb = _breaker(tmp_path, clock=lambda: now[0])
    cb.trip("LOSS_STREAK: 5 consecutive losses")
    assert cb.operator_halt is False

    now[0] += 25 * 3600.0
    assert cb.check_status()["active"] is False


# ── resume must preserve peak equity ────────────────────────────────────────

def test_resume_baselines_keeps_peak_equity(tmp_path):
    guard = DrawdownGuard(max_daily_loss_pct=4.0, max_total_drawdown_pct=10.0,
                          db_path=str(tmp_path / "dd.db"))
    guard.daily_start_equity = 1000.0
    guard.peak_equity = 1000.0
    guard.update_equity_benchmarks(950.0, 950.0)
    assert guard.peak_equity == 1000.0

    guard.resume_baselines(700.0)
    # The day restarts from today's equity...
    assert guard.daily_start_equity == 700.0
    # ...but a 30% drawdown is still a 30% drawdown and must keep being refused.
    assert guard.peak_equity == 1000.0
    assert guard.check_limits(700.0, 700.0)["passed"] is False


def test_reset_baselines_is_the_only_way_to_forgive_the_drawdown(tmp_path):
    guard = DrawdownGuard(max_daily_loss_pct=4.0, max_total_drawdown_pct=10.0,
                          db_path=str(tmp_path / "dd.db"))
    guard.update_equity_benchmarks(1000.0, 1000.0)
    guard.update_equity_benchmarks(700.0, 700.0)
    assert guard.check_limits(700.0, 700.0)["passed"] is False

    guard.reset_baselines(700.0)
    assert guard.peak_equity == 700.0


# ── break-even is not a loss ────────────────────────────────────────────────

def _closed_trade_orchestrator():
    orch = object.__new__(JarvisOrchestrator)
    orch.order_manager = None
    orch._pending_features = {}
    orch._pending_trade_memory = {}
    orch._recover_pending_features = lambda ticket: None
    orch.position_monitor = SimpleNamespace(pop_excursions=lambda ticket: None)
    orch.trade_memory = MagicMock()
    orch.trade_memory.update_closed_trade.return_value = True
    orch.trade_memory.fetch_recent_trades.return_value = []
    orch.ml_predictor = MagicMock()
    orch.strategy_bandit = MagicMock()
    orch.circuit_breaker = MagicMock()
    orch.risk_engine = None
    orch.drawdown_guard = MagicMock()
    orch.decision_engine = MagicMock()
    return orch


def test_zero_pnl_is_neutral_not_a_loss():
    orch = _closed_trade_orchestrator()
    orch._on_trade_closed({"ticket": 111, "symbol": "EURUSD", "pnl": 0.0})

    orch.circuit_breaker.record_trade_result.assert_not_called()
    orch.strategy_bandit.record_outcome.assert_not_called()
    # The scratch is still persisted as a real, measured 0.0.
    kwargs = orch.trade_memory.update_closed_trade.call_args.kwargs
    assert kwargs["pnl"] == 0.0
    assert kwargs["is_win"] is None


def test_negative_pnl_is_still_a_loss():
    orch = _closed_trade_orchestrator()
    orch._on_trade_closed({"ticket": 112, "symbol": "EURUSD", "pnl": -12.5})

    orch.circuit_breaker.record_trade_result.assert_called_once()
    assert orch.circuit_breaker.record_trade_result.call_args.args[0] is False
    assert orch.trade_memory.update_closed_trade.call_args.kwargs["is_win"] == 0


def test_missing_pnl_is_not_counted_either():
    orch = _closed_trade_orchestrator()
    orch._on_trade_closed({"ticket": 113, "symbol": "EURUSD"})
    orch.circuit_breaker.record_trade_result.assert_not_called()


# ── truthful broker fill reporting ──────────────────────────────────────────

def _order_mt5(retcode: int, volume: float):
    fake = MagicMock()
    fake.account_info.return_value = SimpleNamespace(login=1, trade_mode=0)
    fake.symbol_info.return_value = SimpleNamespace(
        volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=5,
        point=0.00001, trade_stops_level=0, trade_freeze_level=0,
    )
    fake.symbol_info_tick.return_value = SimpleNamespace(bid=1.0850, ask=1.0852)
    fake.order_send.return_value = SimpleNamespace(
        retcode=retcode, deal=0, order=555, price=1.0852, volume=volume, comment="ok"
    )
    fake.TRADE_RETCODE_DONE = 10009
    fake.TRADE_RETCODE_PLACED = 10008
    fake.TRADE_ACTION_DEAL = 1
    fake.ORDER_TYPE_BUY = 0
    fake.ORDER_TYPE_SELL = 1
    fake.ORDER_TIME_GTC = 0
    fake.ORDER_FILLING_IOC = 1
    fake.ORDER_FILLING_FOK = 0
    fake.ORDER_FILLING_RETURN = 2
    fake.history_deals_get.return_value = None
    fake.positions_get.return_value = None
    return fake


def _demo_client():
    client = MT5Client(mode="demo", auto_init=False)
    client.is_connected = True
    client._reconnect_if_needed = lambda: None
    client.resolve_symbol_name = lambda s: s
    return client


@pytest.mark.parametrize("retcode,volume,expected", [
    (10008, 1.0, "PLACED"),
    (10009, 1.0, "FILLED"),
    (10009, 0.5, "PARTIAL"),
    (10009, 0.0, "UNKNOWN"),
])
def test_fill_status_is_truthful(retcode, volume, expected):
    client = _demo_client()
    with patch("jarvis.execution.mt5_client.mt5", _order_mt5(retcode, volume)), \
         patch("jarvis.execution.mt5_client.MT5_AVAILABLE", True):
        res = client.send_market_order("EURUSD", "BUY", 1.0, sl_price=1.0800, tp_price=1.0900)
    assert res["status"] == expected
    if expected in ("FILLED", "PARTIAL"):
        assert res["requested_volume"] == 1.0
        assert res["sent_volume"] == 1.0
        assert res["volume"] == volume


# ── gold heat allowance requires a known minimum lot ────────────────────────

def _heat_engine():
    from jarvis.risk.risk_engine import RiskEngine

    engine = object.__new__(RiskEngine)
    engine.exposure_manager = SimpleNamespace(
        calculate_portfolio_monetary_risk=lambda positions, account: 0.0
    )
    engine.get_total_reserved_risk_usd = lambda: 0.0
    engine.portfolio_heat_engine = SimpleNamespace(
        calculate_heat=lambda **kwargs: SimpleNamespace(
            allow_new_risk=False, score=50.0, zone="HIGH",
            risk_multiplier=0.0, reasons=["heat too high"],
        )
    )
    return engine


def test_gold_heat_bypass_does_not_apply_without_a_known_lot_size():
    engine = _heat_engine()
    decision = SimpleNamespace(symbol="XAUUSD")
    res = engine.check_portfolio_heat(None, [], decision=decision, symbol="XAUUSD")
    assert res["passed"] is False


def test_gold_heat_bypass_applies_to_a_real_minimum_lot():
    engine = _heat_engine()
    decision = SimpleNamespace(symbol="XAUUSD", lot_size=0.01)
    res = engine.check_portfolio_heat(None, [], decision=decision, symbol="XAUUSD")
    assert res["passed"] is True


def test_heat_gate_is_not_bypassed_for_other_symbols():
    engine = _heat_engine()
    decision = SimpleNamespace(symbol="EURUSD", lot_size=0.01)
    res = engine.check_portfolio_heat(None, [], decision=decision, symbol="EURUSD")
    assert res["passed"] is False


# ── mode switching ─────────────────────────────────────────────────────────

def test_mode_switch_refused_with_open_positions():
    orch = object.__new__(JarvisOrchestrator)
    orch.mode = "demo"
    orch.mt5_client = SimpleNamespace(get_open_positions=lambda: [object()])
    orch.state_manager = MagicMock()
    orch.risk_engine = MagicMock()

    res = orch.apply_execution_mode("paper")
    assert res["status"] == "FAILED"
    assert "open position" in res["error"]
    assert orch.mode == "demo"


def test_mode_switch_refused_when_positions_unreadable():
    orch = object.__new__(JarvisOrchestrator)
    orch.mode = "demo"

    def _boom():
        raise RuntimeError("terminal down")

    orch.mt5_client = SimpleNamespace(get_open_positions=_boom)
    orch.state_manager = MagicMock()

    res = orch.apply_execution_mode("paper")
    assert res["status"] == "FAILED"
    assert "verify open positions" in res["error"]


# ── startup reconciliation ─────────────────────────────────────────────────

def test_startup_reconciliation_closes_rows_closed_while_down(monkeypatch):
    import sys

    fake_mt5 = SimpleNamespace(
        history_deals_get=lambda position=None: [
            SimpleNamespace(profit=15.0, swap=0.0, commission=-1.0, entry=1, price=1.0900, symbol="EURUSD"),
        ]
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    orch = object.__new__(JarvisOrchestrator)
    orch.mode = "demo"
    orch.mt5_client = SimpleNamespace(
        get_open_positions=lambda: [],
        get_account_snapshot=lambda: SimpleNamespace(equity=500.0, balance=500.0),
    )
    orch.state_synchronizer = SimpleNamespace(
        _last_position_tickets=None, _last_position_volumes=None
    )
    orch.trade_memory = MagicMock()
    orch.trade_memory.fetch_all_trades.return_value = [
        {"ticket": 42, "symbol": "EURUSD", "type": "BUY", "exit_price": None, "pnl": None},
    ]
    orch._on_trade_closed = MagicMock()

    orch._reconcile_positions_closed_while_down()

    orch._on_trade_closed.assert_called_once()
    payload = orch._on_trade_closed.call_args.args[0]
    assert payload["ticket"] == 42
    assert payload["pnl"] == 14.0
    assert payload["source"] == "startup_reconciliation"


def test_startup_reconciliation_leaves_unknown_rows_unresolved(monkeypatch):
    import sys

    fake_mt5 = SimpleNamespace(history_deals_get=lambda position=None: [])
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    orch = object.__new__(JarvisOrchestrator)
    orch.mode = "demo"
    orch.mt5_client = SimpleNamespace(
        get_open_positions=lambda: [],
        get_account_snapshot=lambda: SimpleNamespace(equity=500.0, balance=500.0),
    )
    orch.state_synchronizer = SimpleNamespace(
        _last_position_tickets=None, _last_position_volumes=None
    )
    orch.trade_memory = MagicMock()
    orch.trade_memory.fetch_all_trades.return_value = [
        {"ticket": 43, "symbol": "EURUSD", "type": "BUY", "exit_price": None, "pnl": None},
    ]
    orch._on_trade_closed = MagicMock()

    orch._reconcile_positions_closed_while_down()
    orch._on_trade_closed.assert_not_called()


# ── manual-order gate ──────────────────────────────────────────────────────

def test_manual_order_ceiling_rejects_oversized_stop():
    handler = SimpleNamespace(
        orchestrator=SimpleNamespace(
            circuit_breaker=SimpleNamespace(is_tripped=False),
            drawdown_guard=None,
        ),
        mt5_client=SimpleNamespace(
            get_account_snapshot=lambda: {"equity": 500.0, "balance": 500.0},
            get_open_positions=lambda: [],
        ),
    )
    # 0.01 lot of gold with a 50-point stop is ~$50 = 10% of a $500 account.
    reason = JarvisRequestHandler._validate_trade_risk(
        handler, 0.01, symbol="XAUUSD", price=4000.0, sl=3950.0
    )
    assert reason is not None
    assert "risk $" in reason.lower()
