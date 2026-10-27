"""Mode-scoped persistence for risk-control state.

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
``RiskEngine`` and ``JarvisOrchestrator`` both build a ``DrawdownGuard`` (and a
``CircuitBreaker``) with a filename and no mode, so PAPER and LIVE wrote to the
same ``data/jarvis_drawdown_state.db``. The two modes do not measure equity on the
same scale — paper reports ``equity = 10000.0 + paper_pnl`` while the live account
here holds a few hundred dollars — and ``peak_equity`` moves **up only** and is
never reset daily (only ``daily_start_equity`` re-anchors on a date change).

Observed live consequence: a paper session had left ``peak_equity = 10150.0`` in
the shared file. Against a real equity of 777.00 that is a 92.34% drawdown versus
a 10% cap, so ``check_limits()["passed"]`` was False, ``get_risk_multiplier()``
returned 0.0, and the engine refused **every** trade — at ``risk_engine.py:201``,
``risk_engine.py:405`` (``authorized: False, lots: 0.0``) and the
``orchestrator.py`` EXECUTE->WAIT backstop. The account could not trade at all,
and nothing in the UI said why.

Note the asymmetry, which is what made this hard to read: ``daily_start_equity``
*does* self-heal to the current equity on a new UTC day, so the daily-loss cap was
never the problem. Only the portfolio peak is permanently poisoned.

The fix scopes each persistence file to the execution mode
(``jarvis.config.paths.mode_scoped_db_path``). ``live`` deliberately keeps the bare
filename so existing live baselines are not orphaned.

These tests are marked ``drawdown_persistence`` because ``tests/conftest.py``
otherwise forces ``db_path=""`` (in-memory) on every guard, which would make every
assertion about a path vacuous.
"""

import pytest

from jarvis.config import paths as paths_mod
from jarvis.config.paths import mode_scoped_db_path
from jarvis.risk.drawdown import DrawdownGuard
from jarvis.risk.risk_engine import RiskEngine


# ─── The path contract ────────────────────────────────────────────────────────

def test_live_keeps_the_bare_filename_so_no_state_is_orphaned():
    """Live must not move to a new file: its baselines already exist there."""
    assert mode_scoped_db_path("jarvis_drawdown_state.db", "live") == "jarvis_drawdown_state.db"
    assert mode_scoped_db_path("jarvis_circuit_state.db", "live") == "jarvis_circuit_state.db"


def test_default_mode_is_live():
    """A caller that forgets the argument must get today's behaviour, not a new file."""
    assert mode_scoped_db_path("jarvis_drawdown_state.db") == "jarvis_drawdown_state.db"


@pytest.mark.parametrize("mode", ["paper", "demo", "simulated"])
def test_non_live_modes_get_their_own_file(mode):
    out = mode_scoped_db_path("jarvis_drawdown_state.db", mode)
    assert out == f"jarvis_drawdown_state_{mode}.db"
    assert out != "jarvis_drawdown_state.db"


def test_every_mode_resolves_to_a_distinct_file():
    modes = ["live", "paper", "demo", "simulated"]
    resolved = [mode_scoped_db_path("jarvis_drawdown_state.db", m) for m in modes]
    assert len(set(resolved)) == len(modes), resolved


def test_backtest_persists_nothing():
    """Hermetic backtests: "" is the documented in-memory sentinel."""
    assert mode_scoped_db_path("jarvis_drawdown_state.db", "backtest") == ""
    assert mode_scoped_db_path("jarvis_drawdown_state.db", "live", is_backtest=True) == ""
    assert mode_scoped_db_path("jarvis_drawdown_state.db", "paper", is_backtest=True) == ""


def test_sentinels_pass_through_unchanged():
    assert mode_scoped_db_path("", "paper") == ""
    # resolve_db_path passes ":memory:" through; this must agree with it.
    assert mode_scoped_db_path(":memory:", "paper") == ":memory:"


@pytest.mark.parametrize("raw", ["PAPER", " paper ", "Paper"])
def test_mode_is_normalised(raw):
    assert mode_scoped_db_path("jarvis_drawdown_state.db", raw) == "jarvis_drawdown_state_paper.db"


def test_extension_is_preserved():
    assert mode_scoped_db_path("state.sqlite3", "paper") == "state_paper.sqlite3"


# ─── The regression, through RiskEngine ───────────────────────────────────────

@pytest.mark.drawdown_persistence
def test_paper_peak_cannot_trip_the_live_circuit_breaker(tmp_path, monkeypatch):
    """The headline defect: a paper session must not be able to stop live trading."""
    monkeypatch.setattr(paths_mod, "DATA_DIR", str(tmp_path))

    paper = RiskEngine(mode="paper")
    # A paper session measuring on the 10000.0 base.
    paper.drawdown_guard.update_equity_benchmarks(10000.0, 10000.0)
    paper.drawdown_guard.update_equity_benchmarks(10150.0, 10150.0)
    assert paper.drawdown_guard.peak_equity == 10150.0

    live = RiskEngine(mode="live")
    live.drawdown_guard.update_equity_benchmarks(777.0, 777.0)
    status = live.drawdown_guard.check_limits(777.0, 777.0)

    assert status["passed"] is True, status["breaches"]
    assert status["total_dd_pct"] < 10.0, status
    assert live.drawdown_guard.get_risk_multiplier(777.0) > 0.0


@pytest.mark.drawdown_persistence
def test_the_shared_file_still_reproduces_the_original_defect(tmp_path, monkeypatch):
    """Pins the *mechanism*, so the test above cannot pass for an unrelated reason.

    Reading the paper file as though it were live must still produce the >90%
    phantom drawdown that halted the account. If this ever stops reproducing, the
    scoping tests above have lost their meaning.
    """
    monkeypatch.setattr(paths_mod, "DATA_DIR", str(tmp_path))

    paper = RiskEngine(mode="paper")
    paper.drawdown_guard.update_equity_benchmarks(10000.0, 10000.0)
    paper.drawdown_guard.update_equity_benchmarks(10150.0, 10150.0)

    # Deliberately open the file the paper session wrote, as the old code did.
    shared = DrawdownGuard(db_path="jarvis_drawdown_state_paper.db")
    assert shared.peak_equity == 10150.0

    status = shared.check_limits(777.0, 777.0)
    assert status["passed"] is False
    assert status["total_dd_pct"] > 90.0, status
    assert shared.get_risk_multiplier(777.0) == 0.0


@pytest.mark.drawdown_persistence
def test_paper_and_live_engines_resolve_to_different_files(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "DATA_DIR", str(tmp_path))

    paper = RiskEngine(mode="paper")
    live = RiskEngine(mode="live")

    assert paper.drawdown_guard.db_path != live.drawdown_guard.db_path
    assert paper.circuit_breaker.db_path != live.circuit_breaker.db_path
    assert live.drawdown_guard.db_path.endswith("jarvis_drawdown_state.db")
    assert paper.drawdown_guard.db_path.endswith("jarvis_drawdown_state_paper.db")
    assert live.circuit_breaker.db_path.endswith("jarvis_circuit_state.db")
    assert paper.circuit_breaker.db_path.endswith("jarvis_circuit_state_paper.db")


@pytest.mark.drawdown_persistence
def test_backtest_engine_persists_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "DATA_DIR", str(tmp_path))
    engine = RiskEngine(is_backtest=True)
    assert engine.drawdown_guard.db_path == ""
    assert engine.circuit_breaker.db_path == ""


# ─── Wiring: the orchestrator's own backstop gate ─────────────────────────────
# JarvisOrchestrator builds a SECOND DrawdownGuard/CircuitBreaker, separate from
# RiskEngine's, and uses them as an execution backstop. Fixing only RiskEngine
# would leave that path still reading the shared file.

@pytest.mark.drawdown_persistence
def test_orchestrator_scopes_its_own_gate_and_passes_its_mode_to_risk_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "DATA_DIR", str(tmp_path))

    from jarvis.application.orchestrator import JarvisOrchestrator

    paper = JarvisOrchestrator(mode="paper")
    assert paper.mode == "paper"
    assert paper.risk_engine.mode == "paper"
    assert paper.drawdown_guard.db_path.endswith("jarvis_drawdown_state_paper.db")
    assert paper.circuit_breaker.db_path.endswith("jarvis_circuit_state_paper.db")
    # The orchestrator's own guard is a distinct object from RiskEngine's; both
    # must be scoped, so compare them to the live engine below rather than to
    # each other.
    assert paper.risk_engine.drawdown_guard.db_path.endswith("jarvis_drawdown_state_paper.db")

    monkeypatch.setenv("JARVIS_CONFIRM_LIVE", "1")
    live = JarvisOrchestrator(mode="live")
    assert live.mode == "live"
    assert live.risk_engine.mode == "live"
    assert live.drawdown_guard.db_path.endswith("jarvis_drawdown_state.db")
    assert live.drawdown_guard.db_path != paper.drawdown_guard.db_path
