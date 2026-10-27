"""Regressions for the 2026-10-23 P0 audit fixes.

These tests intentionally exercise the failure modes rather than the happy
path: a governor spread baseline that silently fell back to 2.0 pips, a
live cooldown that never expired, a daily baseline that reset itself, and a
gate registry whose hard/soft sets disagreed.
"""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.config.settings import verify_execution_mode
from jarvis.intelligence.gate_registry import (
    GATE_NAMES,
    HARD_GATES,
    SOFT_GATE_PENALTIES,
    canonical_gate,
    split_failures,
)
from jarvis.intelligence.llm_controller import AutonomousLLMSystemController
from jarvis.risk.drawdown import DrawdownGuard
from jarvis.risk.loss_cooldown import LossCooldownManager


def test_gate_registry_is_the_single_source_of_truth():
    assert "Spread Protection" in HARD_GATES
    assert "Positive Expected Value" in HARD_GATES
    assert "Strategy Triggered" in HARD_GATES
    assert not (set(HARD_GATES) & set(SOFT_GATE_PENALTIES))
    assert len(GATE_NAMES) == len(set(GATE_NAMES))


def test_gate_failures_classify_in_one_canonicalised_pass():
    hard, soft, unknown = split_failures([
        "Spread Protection",
        "Spread Protection",
        "Master Confluence Below Minimum (12/100 < 30)",
        "Gold Session Window",
        "A Brand New Gate",
    ])
    assert hard == ["Spread Protection", "Master Confluence Minimum", "Gold Session Window"]
    assert soft == ["A Brand New Gate"]
    assert unknown == ["A Brand New Gate"]


def test_free_text_vetoes_canonicalise_to_hard_gates():
    assert canonical_gate("Metals out-of-killzone (02:00 UTC)") == "Gold Session Window"
    assert canonical_gate("Master Confluence Below Minimum (12/100 < 30)") == "Master Confluence Minimum"
    assert canonical_gate("Pullback entry too far from OTE (1.20 > 0.65 ATR)") == "Metals Pullback OTE Proximity"
    assert canonical_gate("Metals BUY requires deep DISCOUNT") == "Metals Premium/Discount Discipline"


def test_governor_uses_registry_spread_baseline_not_two_pips():
    class FakeState:
        account = SimpleNamespace(equity=500.0, balance=500.0)
        positions = []
        active_symbols = ["BTCUSD", "ETHUSD"]
        market_contexts = {
            "BTCUSD": SimpleNamespace(live_spread_pips=1500.0),
            "ETHUSD": SimpleNamespace(live_spread_pips=345.0),
        }
        latest_decisions = {}

    fake_self = SimpleNamespace(state_manager=FakeState())
    ctx = AutonomousLLMSystemController._gather_system_context(fake_self)
    assert ctx["symbols"]["BTCUSD"]["spread_ratio"] == 1.0
    assert ctx["symbols"]["ETHUSD"]["spread_ratio"] == 1.0


def test_live_loss_cooldown_expires_and_daily_counters_roll_over():
    mgr = LossCooldownManager()
    # `should_skip_trade` rolls the day against the wall clock in live mode, so
    # the recorded date must be today's UTC date or the rollover happens first
    # and the counter resets underneath the assertion.
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    with patch("jarvis.config.runtime.is_offline", return_value=False):
        for i in range(3):
            mgr.record_trade_result(
                pnl=-1.0,
                is_win=False,
                symbol="EURUSD",
                current_date=today.isoformat(),
            )
        assert mgr.cooldown_bars_remaining == 2
        skip, reason = mgr.should_skip_trade("GBPUSD")
        assert skip and "Cooldown active" in reason

        # Simulate wall-clock expiry: no tick_bar() exists in the live loop.
        mgr.cooldown_expiry_global = time.time() - 1
        skip, reason = mgr.should_skip_trade("GBPUSD")
        assert not skip, reason

        assert mgr.trades_today == 3
        mgr.record_trade_result(
            pnl=1.0, is_win=True, symbol="EURUSD", current_date=tomorrow.isoformat()
        )
        assert mgr.trades_today == 1


def test_daily_loss_baseline_is_not_reanchored_by_deep_drawdown():
    g = DrawdownGuard(db_path="", max_daily_loss_pct=5.0, max_total_drawdown_pct=10.0)
    g.update_equity_benchmarks(1000.0, 1000.0)
    result = g.check_limits(920.0, 920.0)
    assert result["daily_loss_pct"] == 8.0
    assert result["total_dd_pct"] == 8.0
    assert g.peak_equity == 1000.0
    assert g.get_risk_multiplier(920.0) == 0.0


def test_live_mode_requires_explicit_confirmation(monkeypatch):
    monkeypatch.delenv("JARVIS_CONFIRM_LIVE", raising=False)
    assert verify_execution_mode("live") == "paper"
    monkeypatch.setenv("JARVIS_CONFIRM_LIVE", "1")
    assert verify_execution_mode("live") == "live"
    assert verify_execution_mode(None) == "demo"
    assert verify_execution_mode("paper") == "paper"


def _recovery_decision(**overrides):
    gate = SimpleNamespace(
        passed=True,
        effective_passed=True,
        failing_reasons=[],
        softened_gates=[],
    )
    values = dict(
        decision="EXECUTE",
        quality_gate=gate,
        gate_policy_decision="PASS",
        master_confluence_tier="A+",
        expected_value=10.0,
        model_confidence=0.75,
        risk_reward_ratio=2.5,
        strategy="TREND_PULLBACK",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_recovery_channel_requires_a_clean_a_plus_setup():
    from jarvis.risk.risk_engine import RiskEngine

    assert RiskEngine._is_perfect_recovery_setup(_recovery_decision())

    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(decision="WAIT")
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(quality_gate=None)
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(
            quality_gate=SimpleNamespace(
                passed=True,
                effective_passed=False,
                failing_reasons=[],
                softened_gates=[],
            )
        )
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(
            quality_gate=SimpleNamespace(
                passed=True,
                effective_passed=True,
                failing_reasons=["Gold Session Window"],
                softened_gates=[],
            )
        )
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(
            quality_gate=SimpleNamespace(
                passed=True,
                effective_passed=True,
                failing_reasons=[],
                softened_gates=["Spread Protection"],
            )
        )
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(gate_policy_decision="BLOCK")
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(master_confluence_tier="B")
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(expected_value=0.0)
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(model_confidence=0.69)
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(risk_reward_ratio=1.99)
    )
    assert not RiskEngine._is_perfect_recovery_setup(
        _recovery_decision(strategy="NO_STRATEGY")
    )


def test_recovery_attempts_are_capped_and_roll_over_daily():
    from jarvis.risk.risk_engine import RiskEngine

    engine = SimpleNamespace(
        _recovery_day="",
        _recovery_attempts=0,
        recovery_max_attempts_per_day=1,
        _recovery_today=lambda: "2026-10-23",
    )
    assert RiskEngine._can_take_recovery_attempt(engine) is True
    RiskEngine._note_recovery_attempt(engine)
    assert engine._recovery_attempts == 1
    assert RiskEngine._can_take_recovery_attempt(engine) is False

    engine._recovery_today = lambda: "2026-10-24"
    assert RiskEngine._can_take_recovery_attempt(engine) is True
    assert engine._recovery_attempts == 0
