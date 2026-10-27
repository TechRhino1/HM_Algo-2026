"""Tests for jarvis.risk.drawdown.DrawdownGuard.

This is the capital-preservation gate. `risk_engine` gates three separate checks
on `check_limits(...)["passed"]` — adaptive gate 10, the trade-rejection path and
the final authorization — and the last of those returns `authorized: True`, so
anything this guard fails to notice is tradable.

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
Both baselines used to be re-anchored on any single observation more than 33.3%
below them:

    if self.daily_start_equity <= 0 or self.daily_start_equity > current_equity * 1.5:
        self.daily_start_equity = current_equity
    if ... or self.peak_equity > current_equity * 1.5:
        self.peak_equity = current_equity

The `> current * 1.5` clause was meant to absorb withdrawals. But a loss of more
than 33.3% moves equity exactly as far as a withdrawal does, and there is no
single-instant test that separates them — a flat account that REALISED a 40% loss
has equity == balance and looks identical to one that had a withdrawal. `current_
balance` is passed in and cannot help: a realised loss lowers balance too.

So the guard failed open, and failed open hardest exactly where it mattered:
a 40% intraday drop reported `daily_loss_pct == 0.0`, `total_dd_pct == 0.0` and
`passed == True`, and the same re-anchor erased `peak_equity` and so cancelled the
portfolio circuit breaker as well. A 33.0% loss was caught correctly; 33.4% was
forgotten entirely. The worse the day, the safer the guard thought it was.

Baselines now move UP only. A real deposit or withdrawal is re-anchored explicitly
via `reset_baselines()`. Failing closed costs a day; failing open costs the account.

ALSO PINNED, NOT CHANGED
------------------------
* The daily-loss cap resets on a **UTC** date change, which is not the broker's
  trading day — a server on GMT+2/+3 rolls over two or three hours into the
  session. The clock is now injectable (`clock=` / `set_clock()`), matching
  `circuit_breaker`, so a broker-day clock can be supplied without touching this
  module. The default is left as UTC because changing it moves when the cap lifts.
* `current_balance` is accepted and never read. The comment at the anchor point
  explains why equity is the right basis; the parameter stays for API stability.
* `get_risk_multiplier` has no callers and returns **1.0** (full size) when
  `peak_equity <= 0`, i.e. before any equity has been recorded. Fail-open.
* `check_limits` is not read-only: it re-anchors and writes to SQLite as a side
  effect, and `risk_engine` calls it three times per decision.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from jarvis.risk.drawdown import DrawdownGuard

DAY0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
DAY1 = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)


def guard(db_path="", day=DAY0, **kw):
    """A guard with no persistence and a frozen clock, unless told otherwise."""
    return DrawdownGuard(db_path=db_path, clock=lambda: day, **kw)


def anchored(equity=1000.0, **kw):
    g = guard(**kw)
    g.update_equity_benchmarks(equity, equity)
    return g


# ---------------------------------------------------------------------------
# The regression: a large loss must not re-anchor the baselines
# ---------------------------------------------------------------------------

class TestALargeLossIsNeverReanchored:
    def test_a_40_percent_intraday_loss_is_reported_as_40_percent(self):
        """The headline case. Pre-fix this returned 0.0% and passed=True."""
        g = anchored(1000.0)
        r = g.check_limits(600.0, 600.0)
        assert r["daily_loss_pct"] == 40.0
        assert r["total_dd_pct"] == 40.0
        assert r["passed"] is False
        assert len(r["breaches"]) == 2

    def test_the_baselines_survive_the_loss(self):
        g = anchored(1000.0)
        g.check_limits(600.0, 600.0)
        assert g.daily_start_equity == 1000.0
        assert g.peak_equity == 1000.0

    def test_a_loss_arriving_in_two_steps_is_not_forgotten(self):
        """Pre-fix: 970 reported 3%, then 600 re-anchored and reported 0%.

        The guard saw the loss and then lost it — worse than never seeing it,
        because the 3% reading looked like the guard was working.
        """
        g = anchored(1000.0)
        assert g.check_limits(970.0, 970.0)["daily_loss_pct"] == 3.0
        r = g.check_limits(600.0, 600.0)
        assert r["daily_loss_pct"] == 40.0
        assert r["passed"] is False

    @pytest.mark.parametrize("equity,expected_pct", [
        (900.0, 10.0),    # well past the 4% cap
        (670.0, 33.0),    # just under the old 33.3% re-anchor cliff — was correct
        (666.0, 33.4),    # just over it — was silently zeroed pre-fix
        (500.0, 50.0),
        (100.0, 90.0),
        (1.0, 99.9),
    ])
    def test_every_magnitude_of_loss_is_measured_not_discarded(self, equity, expected_pct):
        g = anchored(1000.0)
        r = g.check_limits(equity, equity)
        assert r["daily_loss_pct"] == expected_pct
        assert r["passed"] is False

    def test_the_circuit_breaker_is_not_erased_with_the_peak(self):
        """Pre-fix the same clause reset peak_equity, cancelling the 10% breaker."""
        g = anchored(1000.0, max_total_drawdown_pct=10.0)
        r = g.check_limits(600.0, 600.0)
        assert r["total_dd_pct"] == 40.0
        assert any("Max Portfolio Drawdown" in b for b in r["breaches"])

    def test_a_withdrawal_is_reanchored_only_when_the_operator_says_so(self):
        g = anchored(1000.0)
        g.reset_baselines(600.0)
        assert g.daily_start_equity == 600.0
        assert g.peak_equity == 600.0
        assert g.check_limits(600.0, 600.0)["passed"] is True

    def test_a_further_loss_after_an_explicit_reset_is_still_caught(self):
        g = anchored(1000.0)
        g.reset_baselines(600.0)
        r = g.check_limits(570.0, 570.0)      # 5% below the new baseline
        assert r["daily_loss_pct"] == 5.0
        assert r["passed"] is False


# ---------------------------------------------------------------------------
# Baseline movement
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_the_default_caps_are_4_and_10_percent(self):
        """These are the shipped limits; a change here moves real money."""
        g = DrawdownGuard(db_path="")
        assert g.max_daily_loss_pct == 4.0
        assert g.max_total_drawdown_pct == 10.0

    def test_the_caps_are_overridable(self):
        g = DrawdownGuard(db_path="", max_daily_loss_pct=1.0, max_total_drawdown_pct=2.0)
        assert (g.max_daily_loss_pct, g.max_total_drawdown_pct) == (1.0, 2.0)


class TestUpdateEquityBenchmarks:
    def test_the_first_call_anchors_both_baselines(self):
        g = guard()
        assert g.daily_start_equity == 0.0 and g.peak_equity == 0.0
        g.update_equity_benchmarks(1000.0, 1000.0)
        assert g.daily_start_equity == 1000.0
        assert g.peak_equity == 1000.0

    def test_the_peak_rises_with_a_new_high(self):
        g = anchored(1000.0)
        g.update_equity_benchmarks(1500.0, 1500.0)
        assert g.peak_equity == 1500.0

    def test_the_peak_never_falls(self):
        g = anchored(1000.0)
        g.update_equity_benchmarks(1500.0, 1500.0)
        g.update_equity_benchmarks(800.0, 800.0)
        assert g.peak_equity == 1500.0

    def test_the_daily_baseline_does_not_move_on_a_gain(self):
        g = anchored(1000.0)
        g.update_equity_benchmarks(5000.0, 5000.0)
        assert g.daily_start_equity == 1000.0

    def test_the_daily_baseline_does_not_move_on_a_loss(self):
        g = anchored(1000.0)
        g.update_equity_benchmarks(400.0, 400.0)
        assert g.daily_start_equity == 1000.0

    def test_a_zeroed_baseline_is_reanchored(self):
        """The only downward path left: a baseline that was reset, not breached."""
        g = anchored(1000.0)
        g.daily_start_equity = 0.0
        g.update_equity_benchmarks(750.0, 750.0)
        assert g.daily_start_equity == 750.0

    def test_current_balance_is_accepted_and_never_read(self):
        """Equity is the right basis (the anchor comment says why); balance is unused."""
        a = guard()
        a.update_equity_benchmarks(1000.0, 1000.0)
        b = guard()
        b.update_equity_benchmarks(1000.0, 12_345.0)
        assert (a.daily_start_equity, a.peak_equity) == (b.daily_start_equity, b.peak_equity)
        assert a.check_limits(600.0, 600.0) == b.check_limits(600.0, 12_345.0)


# ---------------------------------------------------------------------------
# check_limits
# ---------------------------------------------------------------------------

class TestCheckLimits:
    def test_the_result_contract(self):
        r = anchored(1000.0).check_limits(1000.0, 1000.0)
        assert set(r) == {"passed", "daily_loss_pct", "total_dd_pct", "breaches"}
        assert isinstance(r["passed"], bool)
        assert isinstance(r["breaches"], list)

    @pytest.mark.parametrize("equity,passed", [
        (1000.0, True), (961.0, True), (960.01, True),
        (960.0, False),          # exactly 4.00% — the cap is >=
        (900.0, False),
    ])
    def test_the_daily_loss_cap_boundary(self, equity, passed):
        r = anchored(1000.0, max_daily_loss_pct=4.0).check_limits(equity, equity)
        assert r["passed"] is passed

    @pytest.mark.parametrize("equity,passed", [
        (1000.0, True), (900.1, True),
        (900.0, False),          # exactly 10.00%
        (800.0, False),
    ])
    def test_the_total_drawdown_boundary(self, equity, passed):
        """The daily cap is lifted out of the way so only the total binds."""
        g = anchored(1000.0, max_daily_loss_pct=100.0, max_total_drawdown_pct=10.0)
        r = g.check_limits(equity, equity)
        assert r["passed"] is passed

    def test_a_gain_reports_zero_loss_rather_than_a_negative(self):
        r = anchored(1000.0).check_limits(1200.0, 1200.0)
        assert r["daily_loss_pct"] == 0.0
        assert r["total_dd_pct"] == 0.0
        assert r["passed"] is True

    def test_both_breaches_are_reported_together(self):
        r = anchored(1000.0).check_limits(500.0, 500.0)
        assert len(r["breaches"]) == 2
        assert any("Max Daily Loss" in b for b in r["breaches"])
        assert any("Max Portfolio Drawdown" in b for b in r["breaches"])

    def test_the_message_carries_the_numbers(self):
        r = anchored(1000.0).check_limits(950.0, 950.0)
        assert "5.00%" in r["breaches"][0]
        assert "4.00%" in r["breaches"][0]

    def test_percentages_are_rounded_to_two_decimals(self):
        r = anchored(1000.0).check_limits(966.66, 966.66)
        assert r["daily_loss_pct"] == 3.33
        assert r["total_dd_pct"] == 3.33

    def test_it_anchors_on_first_use_so_the_first_reading_is_always_zero(self):
        r = guard().check_limits(1000.0, 1000.0)
        assert r == {"passed": True, "daily_loss_pct": 0.0, "total_dd_pct": 0.0, "breaches": []}

    def test_a_fresh_guard_at_zero_equity_passes(self):
        """peak<=0 short-circuits both percentages — unknown reads as safe. Pinned."""
        r = guard().check_limits(0.0, 0.0)
        assert r["passed"] is True

    def test_it_is_not_read_only(self):
        """check_limits re-anchors and persists as a side effect."""
        g = guard(db_path="")
        assert g.peak_equity == 0.0
        g.check_limits(1000.0, 1000.0)
        assert g.peak_equity == 1000.0

    def test_repeated_calls_are_stable(self):
        """risk_engine calls this three times per decision."""
        g = anchored(1000.0)
        first = g.check_limits(900.0, 900.0)
        assert g.check_limits(900.0, 900.0) == first
        assert g.check_limits(900.0, 900.0) == first


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.mark.drawdown_persistence
class TestPersistence:
    """Opts out of the in-memory fixture — these tests need real sqlite files."""

    def guard_at(self, tmp_path, **kw):
        return DrawdownGuard(db_path=str(tmp_path / "dd.db"), clock=lambda: DAY0, **kw)

    def test_state_survives_a_restart(self, tmp_path):
        g = self.guard_at(tmp_path)
        g.update_equity_benchmarks(1000.0, 1000.0)
        g.update_equity_benchmarks(1200.0, 1200.0)
        reborn = self.guard_at(tmp_path)
        assert reborn.daily_start_equity == 1000.0
        assert reborn.peak_equity == 1200.0

    def test_the_schema_is_created_on_first_use(self, tmp_path):
        self.guard_at(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "dd.db"))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(drawdown_state)")}
            assert {"id", "daily_start_equity", "peak_equity", "last_saved_date"} <= cols
        finally:
            conn.close()

    def test_an_empty_db_path_writes_nothing(self, tmp_path):
        g = guard(db_path="")
        g.update_equity_benchmarks(1000.0, 1000.0)
        g.check_limits(900.0, 900.0)
        assert list(tmp_path.iterdir()) == []

    def test_offline_mode_forces_an_in_memory_guard(self, tmp_path, monkeypatch):
        import jarvis.config.runtime as runtime
        monkeypatch.setattr(runtime, "is_offline", lambda: True)
        g = DrawdownGuard(db_path=str(tmp_path / "dd.db"))
        assert g.db_path == ""

    def test_an_explicit_reset_is_persisted(self, tmp_path):
        g = self.guard_at(tmp_path)
        g.update_equity_benchmarks(1000.0, 1000.0)
        g.reset_baselines(600.0)
        reborn = self.guard_at(tmp_path)
        assert reborn.daily_start_equity == 600.0
        assert reborn.peak_equity == 600.0

    # -- the day boundary ---------------------------------------------------

    def _stamp(self, tmp_path, date_iso, daily_start, peak):
        conn = sqlite3.connect(str(tmp_path / "dd.db"))
        try:
            with conn:
                conn.execute("UPDATE drawdown_state SET last_saved_date=?, daily_start_equity=?, peak_equity=? WHERE id=1",
                             (date_iso, daily_start, peak))
        finally:
            conn.close()

    def test_a_new_day_zeroes_the_daily_baseline(self, tmp_path):
        self.guard_at(tmp_path).update_equity_benchmarks(1000.0, 1000.0)
        self._stamp(tmp_path, "2026-02-28", 1000.0, 1200.0)
        reborn = self.guard_at(tmp_path)
        assert reborn.daily_start_equity == 0.0

    def test_a_new_day_keeps_the_peak(self, tmp_path):
        """The peak is a high-water mark, not a daily figure — it must survive."""
        self.guard_at(tmp_path).update_equity_benchmarks(1000.0, 1000.0)
        self._stamp(tmp_path, "2026-02-28", 1000.0, 1200.0)
        reborn = self.guard_at(tmp_path)
        assert reborn.peak_equity == 1200.0

    def test_after_a_day_rollover_the_baseline_reanchors_to_current_equity(self, tmp_path):
        self.guard_at(tmp_path).update_equity_benchmarks(1000.0, 1000.0)
        self._stamp(tmp_path, "2026-02-28", 1000.0, 1200.0)
        reborn = self.guard_at(tmp_path)
        r = reborn.check_limits(800.0, 800.0)
        assert r["daily_loss_pct"] == 0.0        # fresh day, fresh baseline
        assert r["total_dd_pct"] == 33.33        # but the peak is still 1200
        assert r["passed"] is False

    def test_a_stale_daily_loss_does_not_leak_into_the_next_day(self, tmp_path):
        """The whole point of the UTC reset: yesterday's 20% must not halt today."""
        g = self.guard_at(tmp_path)
        g.update_equity_benchmarks(1000.0, 1000.0)
        assert g.check_limits(800.0, 800.0)["passed"] is False
        self._stamp(tmp_path, "2026-02-28", 1000.0, 1200.0)
        reborn = self.guard_at(tmp_path)
        assert reborn.check_limits(800.0, 800.0)["daily_loss_pct"] == 0.0


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------

class TestClock:
    def test_the_injected_clock_decides_what_day_it_is(self):
        today = {"d": DAY0}
        g = DrawdownGuard(db_path="", clock=lambda: today["d"])
        g.update_equity_benchmarks(1000.0, 1000.0)
        assert g.check_limits(900.0, 900.0)["daily_loss_pct"] == 10.0
        today["d"] = DAY1                      # roll over without touching the db
        assert g.check_limits(900.0, 900.0)["daily_loss_pct"] == 0.0

    def test_set_clock_replaces_it(self):
        g = guard(day=DAY0)
        g.update_equity_benchmarks(1000.0, 1000.0)
        g.set_clock(lambda: DAY1)
        assert g.check_limits(900.0, 900.0)["daily_loss_pct"] == 0.0

    def test_set_clock_to_none_restores_utc(self):
        g = guard(day=DAY0)
        g.set_clock(None)
        assert g._now().tzinfo is not None
        assert g._now().utcoffset().total_seconds() == 0

    def test_the_default_clock_is_utc_not_local(self):
        g = DrawdownGuard(db_path="")
        assert g._now().tzinfo == timezone.utc

    def test_a_clock_that_only_returns_a_date_still_works(self):
        """Duck-typed on .date() — a date-only broker-day clock is enough."""
        class Day:
            def date(self):
                return DAY2.date()
        g = DrawdownGuard(db_path="", clock=Day)
        assert g._today() == "2026-03-03"

    def test_an_in_memory_guard_still_rolls_over(self):
        """Pre-fix the reset was gated on `if self.db_path:` — no DB, no reset.

        db_path="" is the documented hermetic-backtest path (`is_offline()` forces
        it), so a backtest could run for a year of bars and never once reset the
        daily cap.
        """
        g = guard()                       # db_path="", no SQLite at all
        g.update_equity_benchmarks(1000.0, 1000.0)
        assert g.check_limits(900.0, 900.0)["daily_loss_pct"] == 10.0
        g.set_clock(lambda: DAY1)
        assert g.check_limits(900.0, 900.0)["daily_loss_pct"] == 0.0

    def test_a_backtest_crossing_many_days_does_not_latch(self):
        """Each new day must start clean, or one bad day halts the whole run."""
        day = {"d": DAY0}
        g = DrawdownGuard(db_path="", clock=lambda: day["d"])
        for i in range(30):
            day["d"] = datetime(2026, 3, 1 + (i % 28), 12, 0, tzinfo=timezone.utc)
            # First sample of the day sets the baseline, so it reads 0% ...
            assert g.check_limits(1000.0, 1000.0)["daily_loss_pct"] == 0.0
            # ... and the same day's subsequent loss is measured against it.
            assert g.check_limits(950.0, 950.0)["daily_loss_pct"] == 5.0

    def test_the_first_sample_of_a_day_defines_the_baseline(self):
        """Consequence of the reset: a gap-down before the first tick is invisible.

        The daily baseline is whatever equity the first sample of the day shows,
        so an overnight gap is absorbed into the baseline rather than counted as
        loss. That matches "daily start equity" semantics, but it is a blind spot
        and it is why the guard can never be the only check on a gap.
        """
        day = {"d": DAY0}
        g = DrawdownGuard(db_path="", clock=lambda: day["d"])
        g.check_limits(1000.0, 1000.0)
        day["d"] = DAY1
        r = g.check_limits(700.0, 700.0)      # gapped down 30% overnight
        assert r["daily_loss_pct"] == 0.0     # invisible to the daily cap ...
        assert r["total_dd_pct"] == 30.0      # ... but the peak-based breaker sees it
        assert len(r["breaches"]) == 1 and "Max Portfolio Drawdown" in r["breaches"][0]


# ---------------------------------------------------------------------------
# get_risk_multiplier — no callers, fail-open on an unknown peak
# ---------------------------------------------------------------------------

class TestGetRiskMultiplier:
    @pytest.mark.parametrize("equity,expected", [
        (1000.0, 1.0), (971.0, 1.0),     # < 3%
        (970.0, 0.75), (951.0, 0.75),    # 3% – 5%
        (950.0, 0.50), (921.0, 0.50),    # 5% – 8%
        (920.0, 0.0),  (100.0, 0.0),     # >= 8%
    ])
    def test_the_bands(self, equity, expected):
        g = anchored(1000.0)
        assert g.get_risk_multiplier(equity) == expected

    def test_full_size_is_cut_before_the_halt_fires(self):
        """0.0 at 8% while the breaker only halts at 10% — a deliberate ramp."""
        g = anchored(1000.0, max_daily_loss_pct=100.0, max_total_drawdown_pct=10.0)
        assert g.get_risk_multiplier(920.0) == 0.0
        assert g.check_limits(920.0, 920.0)["passed"] is True

    def test_an_unknown_peak_returns_full_size(self):
        """peak<=0 -> 1.0 regardless of equity. Fail-open, and currently uncalled."""
        assert guard().get_risk_multiplier(1.0) == 1.0

    def test_gains_do_not_reduce_size(self):
        assert anchored(1000.0).get_risk_multiplier(5000.0) == 1.0

    def test_the_multiplier_is_never_negative(self):
        g = anchored(1000.0)
        assert g.get_risk_multiplier(0.0) >= 0.0


# ---------------------------------------------------------------------------
# Daily de-risk tier and the capped, opt-in recovery window
# ---------------------------------------------------------------------------

class TestDailyRiskMultiplier:
    @pytest.mark.parametrize("equity,expected", [
        (1000.0, 1.0), (975.1, 1.0),     # below 2.5%
        (975.0, 0.50), (951.0, 0.50),    # 2.5% - 5.0%
        (950.0, 0.0), (900.0, 0.0),      # at/after the 5% halt
    ])
    def test_the_bands(self, equity, expected):
        g = anchored(1000.0, max_daily_loss_pct=5.0)
        assert g.get_daily_risk_multiplier(equity) == expected

    def test_it_is_independent_of_the_peak_baseline(self):
        """A 2.6% day inside a flat peak still de-risks; it is not a DD call."""
        g = anchored(1000.0, max_daily_loss_pct=5.0, max_total_drawdown_pct=15.0)
        assert g.get_risk_multiplier(974.0) == 1.0
        assert g.get_daily_risk_multiplier(974.0) == 0.50

    def test_an_unset_daily_baseline_does_not_de_risk(self):
        assert guard().get_daily_risk_multiplier(100.0) == 1.0


class TestRecoveryWindow:
    def test_a_daily_breach_inside_the_cap_passes_only_when_allowed(self):
        g = anchored(1000.0, max_daily_loss_pct=5.0, max_total_drawdown_pct=15.0)
        assert g.check_limits(945.0, 945.0)["passed"] is False
        assert g.check_limits(945.0, 945.0, allow_recovery=True)["passed"] is True

    @pytest.mark.parametrize("equity,allowed", [
        (935.0, True),      # 6.50% - exactly at the recovery cap
        (934.9, False),     # 6.51% - past the cap
    ])
    def test_the_recovery_cap_is_inclusive_and_absolute(self, equity, allowed):
        g = anchored(1000.0, max_daily_loss_pct=5.0, max_total_drawdown_pct=15.0)
        r = g.check_limits(equity, equity, allow_recovery=True)
        assert r["passed"] is allowed

    def test_a_total_drawdown_breach_is_never_waived(self):
        """Daily 6% is recoverable, but the portfolio breaker still owns the call."""
        g = anchored(1050.0, max_daily_loss_pct=5.0, max_total_drawdown_pct=10.0)
        g.set_clock(lambda: DAY1)
        g.update_equity_benchmarks(1000.0, 1000.0)
        r = g.check_limits(940.0, 940.0, allow_recovery=True)
        assert r["daily_loss_pct"] == 6.0
        assert r["total_dd_pct"] > 10.0
        assert r["passed"] is False
        assert any("Portfolio Drawdown" in b for b in r["breaches"])

    def test_recovery_does_not_change_the_result_contract(self):
        g = anchored(1000.0, max_daily_loss_pct=5.0, max_total_drawdown_pct=15.0)
        r = g.check_limits(945.0, 945.0, allow_recovery=True)
        assert set(r) == {"passed", "daily_loss_pct", "total_dd_pct", "breaches"}
        assert r["breaches"] == []
