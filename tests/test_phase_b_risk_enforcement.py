"""
Test Suite for Phase B: Risk Controls Enforcement.
Verifies:
1. Drawdown tier multiplier (1.0 -> 0.75 -> 0.50 -> 0.0) scales lot sizes and halts trading when DD >= 8%.
2. RiskEngine.get_risk_status returns full risk diagnostics.
3. JarvisOrchestrator emergency_stop trips circuit breaker and cancels pending orders.
4. JarvisOrchestrator resume_trading clears the trip state.
"""
import pytest
from datetime import datetime, timezone
from jarvis.risk.risk_engine import RiskEngine
from jarvis.risk.drawdown import DrawdownGuard
from jarvis.data.schemas import DecisionObject, AccountSnapshot, RegimeOutput, MarketRegime, TradeQualityGateResult


def _mock_account(balance=10000.0, equity=10000.0):
    return AccountSnapshot(
        login=123456,
        server="XMGlobal-Demo",
        balance=balance,
        equity=equity,
        margin=0.0,
        free_margin=equity,
        margin_level=10000.0,
        leverage=100
    )


def _mock_decision(symbol="EURUSD", bias="BUY", entry=1.1000, sl=1.0950, tp=1.1100):
    regime = RegimeOutput(
        primary_regime=MarketRegime.TREND_BULL,
        probabilities={"TREND_BULL": 0.8},
        confidence=0.8,
        timestamp=datetime.now(timezone.utc)
    )
    gate = TradeQualityGateResult(
        passed=True,
        checks={"R:R >= 1.5": True, "Spread Acceptable": True, "Trend Alignment": True},
        failing_reasons=[]
    )
    return DecisionObject(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        regime=regime,
        bias=bias,
        probabilities={"BUY": 0.75, "SELL": 0.25},
        strategy="MOMENTUM_EXPANSION",
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward_ratio=2.0,
        calculated_risk_percent=1.0,
        expected_value=1.5,
        model_confidence=0.65,
        adversarial_penalty=5.0,
        invalidation_levels=[],
        bull_case=["Momentum strong"],
        bear_case=[],
        risk_factors=[],
        quality_gate=gate,
        decision="EXECUTE",
        execution_authorized=True,
        order_type="MARKET",
        sl_distance=abs(entry - sl),
        tp_distance=abs(tp - entry)
    )


def test_drawdown_tier_multiplier_scaling():
    """Verify that drawdown tier multiplier scales lots and halts at >= 8% DD."""
    re = RiskEngine(is_backtest=True)
    # Re-anchor peak equity at 10,000
    re.drawdown_guard.reset_baselines(10000.0)

    sym_info = {
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "trade_contract_size": 100000.0,
        "digits": 5
    }

    # Case 1: Normal equity (10,000) -> 0% DD -> multiplier 1.0
    acc_normal = _mock_account(balance=10000.0, equity=10000.0)
    dec = _mock_decision()
    res_normal = re.authorize_execution(dec, acc_normal, [], sym_info, entry_authorized_override=True)
    assert res_normal["authorized"] is True
    lots_normal = res_normal["lots"]
    assert lots_normal > 0

    # Case 2: Multi-day DD -> Equity 9600 (Day started at 9650) -> 4% total DD -> multiplier 0.75
    re.drawdown_guard.daily_start_equity = 9650.0
    acc_tier1 = _mock_account(balance=10000.0, equity=9600.0)
    res_tier1 = re.authorize_execution(dec, acc_tier1, [], sym_info, entry_authorized_override=True)
    assert res_tier1["authorized"] is True
    lots_tier1 = res_tier1["lots"]
    assert lots_tier1 < lots_normal

    # Case 3: Multi-day DD -> Equity 9300 (Day started at 9350) -> 7% total DD -> multiplier 0.50
    re.drawdown_guard.daily_start_equity = 9350.0
    acc_tier2 = _mock_account(balance=10000.0, equity=9300.0)
    res_tier2 = re.authorize_execution(dec, acc_tier2, [], sym_info, entry_authorized_override=True)
    assert res_tier2["authorized"] is True
    lots_tier2 = res_tier2["lots"]
    assert lots_tier2 < lots_tier1

    # Case 4: Equity 9150 -> 8.5% DD -> multiplier 0.0 -> Halt
    acc_tier3 = _mock_account(balance=10000.0, equity=9150.0)
    res_tier3 = re.authorize_execution(dec, acc_tier3, [], sym_info, entry_authorized_override=True)
    assert res_tier3["authorized"] is False
    assert any("DRAWDOWN_TIER_HALT" in r for r in res_tier3["reasons"])


def test_get_risk_status_diagnostics():
    """Verify that get_risk_status returns comprehensive metrics."""
    re = RiskEngine(is_backtest=True)
    re.drawdown_guard.reset_baselines(10000.0)
    status = re.get_risk_status(9600.0, 10000.0)
    assert status["total_dd_pct"] == 4.0
    assert status["drawdown_multiplier"] == 0.75
    assert status["circuit_breaker_active"] is False
    assert status["daily_start_equity"] == 10000.0
    assert status["peak_equity"] == 10000.0


def test_emergency_stop_and_resume():
    """Verify emergency stop trips breaker and resume resets it."""
    from jarvis.application.orchestrator import JarvisOrchestrator
    orch = JarvisOrchestrator(mode="paper")
    
    # Emergency stop
    stop_res = orch.emergency_stop("TEST_EMERGENCY_STOP", close_positions=False)
    assert stop_res["status"] == "SUCCESS"
    assert stop_res["circuit_breaker"] == "TRIPPED"
    assert orch.circuit_breaker.is_tripped is True
    assert orch.risk_engine.circuit_breaker.is_tripped is True

    # Trades should be blocked while tripped
    acc = _mock_account(balance=10000.0, equity=10000.0)
    sym_info = {"volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01, "trade_contract_size": 100000.0, "digits": 5}
    auth_res = orch.risk_engine.authorize_execution(_mock_decision(), acc, [], sym_info, entry_authorized_override=True)
    assert auth_res["authorized"] is False
    assert any("Circuit Breaker" in r for r in auth_res["reasons"])

    # Resume trading
    resume_res = orch.resume_trading()
    assert resume_res["status"] == "SUCCESS"
    assert resume_res["circuit_breaker"] == "RESET"
    assert orch.circuit_breaker.is_tripped is False
    assert orch.risk_engine.circuit_breaker.is_tripped is False
