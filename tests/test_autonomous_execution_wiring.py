"""Regression tests for the autonomous entry pipeline.

The scan must be side-effect free: all symbol/style candidates are collected
before the universal arbiter selects one final opportunity for execution.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_scan_all_modes_forces_side_effect_free_discovery():
    src = _source("jarvis/application/orchestrator.py")
    marker = "future_to_task = {"
    start = src.index(marker)
    end = src.index("for fut in as_completed", start)
    block = src[start:end]
    assert "run_cycle_for_symbol, sym, style, True" in block
    assert "run_cycle_for_symbol, sym, style, dry_run" not in block


def test_run_cycle_has_no_direct_entry_submission():
    src = _source("jarvis/application/orchestrator.py")
    start = src.index("    def run_cycle_for_symbol(")
    end = src.index("    def scan_all_modes(", start)
    cycle = src[start:end]
    assert "execution_engine.execute_decision" not in cycle
    assert "mt5_client.send_market_order" not in cycle
    assert "mt5_client.place_pending_order" not in cycle


def test_only_orchestration_arbiter_dispatches_autonomous_entry():
    src = _source("jarvis/application/orchestrator.py")
    start = src.index("    def _orchestration_loop_single_pass(")
    end = src.index("    def _orchestration_loop(", start)
    dispatch = src[start:end]
    assert "self.execution_engine.execute_decision" in dispatch
    assert "risk_engine.authorize_execution" in dispatch


def test_live_gateway_does_not_silently_rewrite_sl_tp():
    src = _source("jarvis/execution/mt5_client.py")
    assert "BROKER_STOP_DISTANCE_CHANGED" in src
    assert "final_sl = tick.bid - min_stop_dist" not in src
    assert "final_sl = tick.ask + min_stop_dist" not in src
