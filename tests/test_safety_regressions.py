"""Safety regressions found in the 2026-11-06 audit.

Each test pins a control that had been weakened in the working tree:

* DEMO/LIVE account-type mismatch must BLOCK, not warn;
* a tripped circuit breaker must survive a process restart;
* the API drawdown gate must read the dict `check_limits` actually returns;
* execution must refuse a lot whose monetary risk exceeds the configured
  per-trade ceiling.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jarvis.api.server import JarvisRequestHandler
from jarvis.execution.execution_engine import ExecutionEngine
from jarvis.execution.mt5_client import MT5Client
from jarvis.risk.circuit_breaker import CircuitBreaker


def _fake_mt5(trade_mode: int):
    fake = MagicMock()
    fake.account_info.return_value = SimpleNamespace(login=12345678, trade_mode=trade_mode)
    fake.TRADE_ACTION_DEAL = 1
    fake.TRADE_ACTION_PENDING = 5
    fake.ORDER_TYPE_BUY = 0
    fake.ORDER_TYPE_SELL = 1
    fake.TRADE_RETCODE_DONE = 10009
    fake.TRADE_RETCODE_PLACED = 10008
    fake.ORDER_TIME_GTC = 0
    fake.ORDER_FILLING_RETURN = 2
    return fake


def _client(mode: str) -> MT5Client:
    client = MT5Client(mode=mode, auto_init=False)
    client.is_connected = True
    client._reconnect_if_needed = lambda: None
    return client


def test_demo_refuses_real_account_market_order():
    client = _client("demo")
    with patch("jarvis.execution.mt5_client.mt5", _fake_mt5(trade_mode=2)), \
         patch("jarvis.execution.mt5_client.MT5_AVAILABLE", True):
        res = client.send_market_order("EURUSD", "BUY", 0.01, sl_price=1.0, tp_price=2.0)
    assert res["status"] == "BLOCKED"
    assert res["reason"].startswith("SAFETY_BLOCK")


def test_demo_refuses_real_account_pending_order():
    client = _client("demo")
    with patch("jarvis.execution.mt5_client.mt5", _fake_mt5(trade_mode=2)), \
         patch("jarvis.execution.mt5_client.MT5_AVAILABLE", True):
        res = client.place_pending_order("EURUSD", "BUY_LIMIT", 1.0, 0.01)
    assert res["status"] == "BLOCKED"
    assert res["reason"].startswith("SAFETY_BLOCK")


def test_live_refuses_demo_account_market_order():
    client = _client("live")
    with patch("jarvis.execution.mt5_client.mt5", _fake_mt5(trade_mode=0)), \
         patch("jarvis.execution.mt5_client.MT5_AVAILABLE", True):
        res = client.send_market_order("EURUSD", "BUY", 0.01, sl_price=1.0, tp_price=2.0)
    assert res["status"] == "BLOCKED"
    assert res["reason"].startswith("SAFETY_BLOCK")


def test_circuit_breaker_survives_restart_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_RESET_CIRCUIT_BREAKER", raising=False)
    path = str(tmp_path / "circuit.db")

    first = CircuitBreaker(db_path=path)
    first.trip("test halt")
    assert first.is_tripped is True

    reopened = CircuitBreaker(db_path=path)
    assert reopened.is_tripped is True
    assert reopened.trip_reason == "test halt"


def test_circuit_breaker_explicit_env_still_resets(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RESET_CIRCUIT_BREAKER", "1")
    path = str(tmp_path / "circuit.db")

    first = CircuitBreaker(db_path=path)
    first.trip("test halt")

    reopened = CircuitBreaker(db_path=path)
    assert reopened.is_tripped is False


def test_api_drawdown_gate_reads_dict_result():
    class Guard:
        def check_limits(self, equity, balance):
            assert equity == 400.0
            assert balance == 481.0
            return {"passed": False, "breaches": ["daily loss 16.8% >= 5.0%"]}

    handler = SimpleNamespace(
        orchestrator=SimpleNamespace(
            circuit_breaker=SimpleNamespace(is_tripped=False),
            drawdown_guard=Guard(),
        ),
        mt5_client=SimpleNamespace(
            get_account_snapshot=lambda: {"equity": 400.0, "balance": 481.0}
        ),
    )
    reason = JarvisRequestHandler._validate_trade_risk(handler, 0.01)
    assert reason is not None
    assert "Drawdown limit reached" in reason
    assert "daily loss" in reason


def _risk_decision():
    from datetime import datetime, timezone

    from jarvis.data.schemas import (
        DecisionObject,
        LiquidityContext,
        MarketContext,
        MarketRegime,
        MomentumContext,
        RegimeOutput,
        SessionContext,
        StructureContext,
        TradeQualityGateResult,
        VolatilityContext,
    )

    ctx = MarketContext(
        symbol="EURUSD",
        timestamp=datetime.now(timezone.utc),
        current_price=1.0850,
        bid=1.0849,
        ask=1.0851,
        structure=StructureContext(bias="BULLISH"),
        liquidity=LiquidityContext(),
        volatility=VolatilityContext(atr=0.0015, current_spread_pips=1.0),
        momentum=MomentumContext(),
        session=SessionContext(),
    )
    return DecisionObject(
        symbol="EURUSD",
        timestamp=datetime.now(timezone.utc),
        regime=RegimeOutput(
            primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85
        ),
        bias="BUY",
        probabilities={"buy": 0.7, "sell": 0.1, "no_trade": 0.2},
        strategy="TREND_PULLBACK",
        order_type="MARKET",
        entry_price=1.0850,
        stop_loss=1.0750,
        take_profit=1.1150,
        risk_reward_ratio=3.0,
        calculated_risk_percent=0.5,
        expected_value=2.0,
        model_confidence=0.75,
        adversarial_penalty=0.0,
        invalidation_levels=[],
        bull_case=[],
        bear_case=[],
        risk_factors=[],
        quality_gate=TradeQualityGateResult(passed=True, checks={}),
        decision="EXECUTE",
        execution_authorized=True,
        context=ctx,
    )


def test_execution_blocks_risk_above_configured_ceiling():
    mt5 = MagicMock()
    mt5.is_connected = True
    mt5.mode = "paper"
    state = MagicMock()
    state.is_safe_mode = False
    state.execution_mode = "paper"
    state.account.equity = 481.0

    engine = ExecutionEngine(mt5_client=mt5, state_manager=state)
    res = engine.execute_decision(
        _risk_decision(),
        lots=0.01,
        sym_spec={
            "point": 0.00001,
            "digits": 5,
            "trade_tick_size": 0.00001,
            "trade_tick_value": 1.0,
            "trade_stops_level": 0,
            "spread": 0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        },
    )

    assert res["status"] == "BLOCKED"
    assert "Risk $" in res["reason"]
    assert "equity ceiling" in res["reason"]
    mt5.send_market_order.assert_not_called()
    mt5.place_pending_order.assert_not_called()
