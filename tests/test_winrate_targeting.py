"""
Tests for the win-rate targeting stack.

Covers the pieces that a "75% win rate" claim actually depends on:

  * conservative intrabar ordering in the simulator (a bar that spans both the
    stop and the target must be booked as a LOSS — the previous engine resolved
    this optimistically and inflated win rate);
  * geometry/threshold separation (thresholds must not change a simulated path);
  * non-overlapping sequential selection;
  * isotonic calibration monotonicity;
  * the learned regime policy switching off regimes with no edge;
  * profile serialisation round-trip;
  * calibrated entry policy ordering (capital protection before edge filter);
  * hermetic mode (backtests must not read or write live state).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from jarvis.backtesting.trade_simulator import (
    BarArrays,
    Geometry,
    evaluate_geometry,
    select_sequential,
    simulate_all_candidates,
    simulate_trade,
    summarise,
)
from jarvis.config.runtime import is_offline, offline_mode, set_offline
from jarvis.execution.entry_policy import (
    CAPITAL_PROTECTION_GATES,
    capital_protection_failures,
    evaluate_entry,
)
from jarvis.intelligence.winrate_targeting import (
    RegimeEdge,
    WRTargetCalibrator,
    WRTargetProfile,
    default_geometry_grid,
    isotonic_calibrate,
    min_tp_r_for_target,
    regime_edge_table,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def make_bars(highs, lows, closes, atr=None):
    n = len(highs)
    return BarArrays(
        high=np.array(highs, dtype=float),
        low=np.array(lows, dtype=float),
        close=np.array(closes, dtype=float),
        atr=np.array(atr if atr is not None else [1.0] * n, dtype=float),
        time=np.arange(n),
        n=n,
    )


def make_df(highs, lows, closes):
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=len(highs), freq="1h", tz="UTC"),
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "atr": [1.0] * len(highs),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Simulator
# ─────────────────────────────────────────────────────────────────────────────
def test_stop_tested_before_target_when_bar_spans_both():
    """A bar whose range covers both the stop and the target is a LOSS.

    Entry 100, stop 99 (1R = 1.0), target at tp_r=1.0 => 101. The next bar
    ranges 98..102, spanning both. Conservative convention => stop first.
    """
    bars = make_bars(
        highs=[100.0, 102.0], lows=[100.0, 98.0], closes=[100.0, 100.0]
    )
    out = simulate_trade(
        symbol="TEST", side="BUY", entry_idx=0, fill=100.0, sl=99.0,
        geom=Geometry(tp_r=1.0, be_trigger_r=None, fast_cash_r=None, max_bars=10),
        money_per_unit=100_000.0, bars=bars,
    )
    assert out is not None
    assert out.result == "SL"
    assert out.pnl_r < 0
    assert out.is_win is False


def test_target_hit_when_stop_not_touched():
    bars = make_bars(
        highs=[100.0, 101.5], lows=[100.0, 99.9], closes=[100.0, 101.4]
    )
    out = simulate_trade(
        symbol="TEST", side="BUY", entry_idx=0, fill=100.0, sl=99.0,
        geom=Geometry(tp_r=1.0, be_trigger_r=None, fast_cash_r=None, max_bars=10),
        money_per_unit=100_000.0, bars=bars,
    )
    assert out is not None
    assert out.result == "TP"
    assert out.pnl_r == pytest.approx(1.0, abs=1e-6)
    assert out.is_win is True


def test_sell_direction_symmetry():
    bars = make_bars(
        highs=[100.0, 100.1], lows=[100.0, 98.5], closes=[100.0, 98.6]
    )
    out = simulate_trade(
        symbol="TEST", side="SELL", entry_idx=0, fill=100.0, sl=101.0,
        geom=Geometry(tp_r=1.0, be_trigger_r=None, fast_cash_r=None, max_bars=10),
        money_per_unit=100_000.0, bars=bars,
    )
    assert out is not None
    assert out.result == "TP"
    assert out.pnl_r == pytest.approx(1.0, abs=1e-6)


def test_time_stop_closes_at_close():
    bars = make_bars(
        highs=[100.0, 100.2, 100.3, 100.1],
        lows=[100.0, 99.9, 99.8, 99.9],
        closes=[100.0, 100.1, 100.2, 100.05],
    )
    out = simulate_trade(
        symbol="TEST", side="BUY", entry_idx=0, fill=100.0, sl=99.0,
        geom=Geometry(tp_r=5.0, be_trigger_r=None, fast_cash_r=None, max_bars=3),
        money_per_unit=100_000.0, bars=bars,
    )
    assert out is not None
    assert out.result == "TIME_STOP_3B"


def test_cost_reduces_pnl_by_exactly_cost_over_risk():
    bars = make_bars(
        highs=[100.0, 101.5], lows=[100.0, 99.9], closes=[100.0, 101.4]
    )
    geom = Geometry(tp_r=1.0, be_trigger_r=None, fast_cash_r=None, max_bars=10)
    free = simulate_trade(symbol="T", side="BUY", entry_idx=0, fill=100.0, sl=99.0,
                          geom=geom, money_per_unit=100_000.0, bars=bars,
                          cost_price_equiv=0.0)
    paid = simulate_trade(symbol="T", side="BUY", entry_idx=0, fill=100.0, sl=99.0,
                          geom=geom, money_per_unit=100_000.0, bars=bars,
                          cost_price_equiv=0.10)
    assert free is not None and paid is not None
    assert free.pnl_r - paid.pnl_r == pytest.approx(0.10, abs=1e-6)


def test_degenerate_risk_returns_none():
    bars = make_bars(highs=[100.0, 101.0], lows=[100.0, 99.0], closes=[100.0, 100.0])
    assert simulate_trade(symbol="T", side="BUY", entry_idx=0, fill=100.0, sl=100.0,
                          geom=Geometry(), money_per_unit=100_000.0, bars=bars) is None


def test_stop_slippage_deepens_loss_by_exactly_slippage_over_risk():
    """The simulator must model the same stop slippage BacktestEngine charges.

    Without it, breakeven scratches the engine closes as small losses are booked
    as 0R, and OOS expectancy over-promises the edge.
    """
    bars = make_bars(highs=[100.0, 98.5], lows=[100.0, 98.5], closes=[100.0, 99.0])
    base = simulate_trade(symbol="T", side="BUY", entry_idx=0, fill=100.0, sl=99.0,
                          geom=Geometry(tp_r=5.0), money_per_unit=100_000.0, bars=bars)
    slipped = simulate_trade(symbol="T", side="BUY", entry_idx=0, fill=100.0, sl=99.0,
                             geom=Geometry(tp_r=5.0), money_per_unit=100_000.0, bars=bars,
                             slippage_price_equiv=0.1)
    assert base is not None and slipped is not None
    assert base.result == "SL" and slipped.result == "SL"
    # risk_dist = 1.0, so 0.1 slippage deepens the loss by exactly 0.1R.
    assert slipped.pnl_r == pytest.approx(base.pnl_r - 0.1, abs=1e-6)


def test_slippage_does_not_affect_target_win():
    bars = make_bars(highs=[100.0, 101.5], lows=[100.0, 99.9], closes=[100.0, 101.4])
    base = simulate_trade(symbol="T", side="BUY", entry_idx=0, fill=100.0, sl=99.0,
                          geom=Geometry(tp_r=1.0), money_per_unit=100_000.0, bars=bars)
    slipped = simulate_trade(symbol="T", side="BUY", entry_idx=0, fill=100.0, sl=99.0,
                             geom=Geometry(tp_r=1.0), money_per_unit=100_000.0, bars=bars,
                             slippage_price_equiv=0.1)
    assert base.result == "TP" and slipped.result == "TP"
    assert slipped.pnl_r == pytest.approx(base.pnl_r, abs=1e-6)


def test_slippage_applies_to_sell_stop_too():
    bars = make_bars(highs=[100.0, 101.5], lows=[100.0, 101.5], closes=[100.0, 101.0])
    base = simulate_trade(symbol="T", side="SELL", entry_idx=0, fill=100.0, sl=101.0,
                          geom=Geometry(tp_r=5.0), money_per_unit=100_000.0, bars=bars)
    slipped = simulate_trade(symbol="T", side="SELL", entry_idx=0, fill=100.0, sl=101.0,
                             geom=Geometry(tp_r=5.0), money_per_unit=100_000.0, bars=bars,
                             slippage_price_equiv=0.1)
    assert base is not None and slipped is not None
    assert base.result == "SL" and slipped.result == "SL"
    # SELL stop is above entry; slippage pushes the exit higher (worse) by 0.1R.
    assert slipped.pnl_r == pytest.approx(base.pnl_r - 0.1, abs=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry / threshold separation
# ─────────────────────────────────────────────────────────────────────────────
def _candidates(symbol="TEST", n=6):
    return pd.DataFrame({
        "symbol": [symbol] * n,
        "bar_idx": list(range(n)),
        "side": ["BUY"] * n,
        "fill": [100.0] * n,
        "sl": [99.0] * n,
        "score": [0.2, 0.4, 0.6, 0.8, 0.9, 0.95][:n],
        "regime": ["TREND"] * n,
        "strategy": ["S"] * n,
        "zone": ["DISCOUNT"] * n,
    })


def test_threshold_does_not_change_simulated_path():
    """Simulating all candidates must be independent of min_score.

    The whole calibration speed-up rests on this: geometry determines the path,
    the threshold only filters afterwards.
    """
    bars = make_bars(
        highs=[100.0, 101.5, 100.0, 101.5, 100.0, 101.5, 100.0, 101.5],
        lows=[100.0, 99.9, 100.0, 99.9, 100.0, 99.9, 100.0, 99.9],
        closes=[100.0, 101.4, 100.0, 101.4, 100.0, 101.4, 100.0, 101.4],
    )
    df = make_df(bars.high.tolist(), bars.low.tolist(), bars.close.tolist())
    cands = _candidates()
    g_low = Geometry(tp_r=1.0, be_trigger_r=None, fast_cash_r=None, max_bars=5, min_score=0.0)
    g_high = Geometry(tp_r=1.0, be_trigger_r=None, fast_cash_r=None, max_bars=5, min_score=0.9)
    a = simulate_all_candidates(df=df, candidates=cands, geom=g_low,
                                money_per_unit=100_000.0, symbol="TEST")
    b = simulate_all_candidates(df=df, candidates=cands, geom=g_high,
                                money_per_unit=100_000.0, symbol="TEST")
    assert [o.entry_idx for o in a] == [o.entry_idx for o in b]
    assert [o.pnl_r for o in a] == [o.pnl_r for o in b]


def test_select_sequential_is_non_overlapping():
    bars = make_bars(
        highs=[100.0] * 12, lows=[100.0] * 12, closes=[100.0] * 12
    )
    df = make_df(bars.high.tolist(), bars.low.tolist(), bars.close.tolist())
    cands = _candidates(n=6)
    geom = Geometry(tp_r=5.0, be_trigger_r=None, fast_cash_r=None, max_bars=3)
    outs = evaluate_geometry(df=df, candidates=cands, geom=geom,
                             money_per_unit=100_000.0, symbol="TEST")
    assert len(outs) >= 2
    for prev, nxt in zip(outs, outs[1:]):
        assert nxt.entry_idx > prev.exit_idx


def test_select_sequential_respects_score_threshold():
    bars = make_bars(highs=[100.0] * 12, lows=[100.0] * 12, closes=[100.0] * 12)
    df = make_df(bars.high.tolist(), bars.low.tolist(), bars.close.tolist())
    cands = _candidates(n=6)
    geom = Geometry(tp_r=5.0, be_trigger_r=None, fast_cash_r=None, max_bars=3)
    all_out = simulate_all_candidates(df=df, candidates=cands, geom=geom,
                                     money_per_unit=100_000.0, symbol="TEST")
    high = select_sequential(all_out, min_score=0.9)
    assert all(o.ai_score >= 0.9 for o in high)
    assert len(high) <= len(all_out)


def test_select_sequential_regime_filter_frees_the_slot():
    """A trade blocked by the regime policy must not keep its slot.

    The engine applies the regime policy *before* choosing a position, so a
    blocked trade leaves the slot free for a later eligible trade. Filtering
    after the walk would drop that later trade too and understate the system —
    exactly the mismatch that made calibration (192 OOS trades) disagree with
    the engine (7 trades) for EURUSD.
    """
    import dataclasses

    bars = make_bars(highs=[100.0] * 12, lows=[100.0] * 12, closes=[100.0] * 12)
    df = make_df(bars.high.tolist(), bars.low.tolist(), bars.close.tolist())
    cands = _candidates(n=6)
    geom = Geometry(tp_r=5.0, be_trigger_r=None, fast_cash_r=None, max_bars=3)
    outs = simulate_all_candidates(df=df, candidates=cands, geom=geom,
                                   money_per_unit=100_000.0, symbol="TEST")
    assert len(outs) >= 3, "need at least three sequential trades for this test"

    blocked = outs[0]
    labelled = [
        dataclasses.replace(o, regime="DEAD") if o.entry_idx == blocked.entry_idx else o
        for o in outs
    ]

    all_on = select_sequential(labelled)
    filtered = select_sequential(labelled, regimes={"TREND"})

    assert blocked.entry_idx in [o.entry_idx for o in all_on]
    assert blocked.entry_idx not in [o.entry_idx for o in filtered]
    # Removing one trade must not cascade: at most one trade is lost, and the
    # slot is taken by a later eligible entry rather than left empty.
    assert len(filtered) >= len(all_on) - 1
    assert filtered, "filter must not empty a sample that has eligible regimes"
    assert filtered[0].entry_idx > blocked.entry_idx


def test_select_sequential_regime_filter_none_means_no_filtering():
    bars = make_bars(highs=[100.0] * 12, lows=[100.0] * 12, closes=[100.0] * 12)
    df = make_df(bars.high.tolist(), bars.low.tolist(), bars.close.tolist())
    cands = _candidates(n=6)
    geom = Geometry(tp_r=5.0, be_trigger_r=None, fast_cash_r=None, max_bars=3)
    outs = simulate_all_candidates(df=df, candidates=cands, geom=geom,
                                   money_per_unit=100_000.0, symbol="TEST")
    assert [o.entry_idx for o in select_sequential(outs, regimes=None)] == \
           [o.entry_idx for o in select_sequential(outs)]


def test_summarise_win_rate_and_expectancy():
    bars = make_bars(
        highs=[100.0, 101.5, 100.0, 101.5],
        lows=[100.0, 99.9, 100.0, 99.9],
        closes=[100.0, 101.4, 100.0, 101.4],
    )
    df = make_df(bars.high.tolist(), bars.low.tolist(), bars.close.tolist())
    cands = _candidates(n=2)
    geom = Geometry(tp_r=1.0, be_trigger_r=None, fast_cash_r=None, max_bars=1)
    outs = evaluate_geometry(df=df, candidates=cands, geom=geom,
                             money_per_unit=100_000.0, symbol="TEST")
    s = summarise(outs)
    assert s["trades"] == len(outs)
    assert 0.0 <= s["win_rate"] <= 1.0
    assert s["expectancy_r"] == pytest.approx(sum(o.pnl_r for o in outs) / len(outs), abs=1e-6)


def test_summarise_empty():
    s = summarise([])
    assert s["trades"] == 0 and s["win_rate"] == 0.0 and s["expectancy_r"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Geometry contract
# ─────────────────────────────────────────────────────────────────────────────
def test_geometry_disabled_features_are_unreachable():
    g = Geometry(tp_r=0.5, be_trigger_r=None, fast_cash_r=None, trail_atr=None)
    p = g.to_policy("TEST", None)
    assert p.be_trigger_r >= 1e9
    assert p.fast_cash_r >= 1e9
    assert p.trail_activation_r >= 1e9
    assert p.milestones == []


def test_geometry_key_distinguishes_thresholds():
    a = Geometry(tp_r=1.0, min_score=0.0)
    b = Geometry(tp_r=1.0, min_score=0.5)
    assert a.key() != b.key()


def test_geometry_roundtrip():
    g = Geometry(tp_r=0.75, be_trigger_r=1.0, fast_cash_r=0.5, max_bars=24, min_score=0.6)
    assert Geometry.from_dict(g.to_dict()) == g


def test_grid_includes_short_and_long_time_stops():
    grid = default_geometry_grid(coarse=True)
    assert {g.max_bars for g in grid} == {24, 48}
    assert len({g.key() for g in grid}) == len(grid)


# ─────────────────────────────────────────────────────────────────────────────
# Calibration primitives
# ─────────────────────────────────────────────────────────────────────────────
def test_isotonic_calibration_is_monotone():
    scores = [0.1, 0.15, 0.2, 0.4, 0.45, 0.5, 0.8, 0.85, 0.9, 0.95]
    wins = [0, 0, 1, 1, 0, 1, 1, 1, 1, 1]
    cal = isotonic_calibrate(scores, wins, n_bins=5)
    assert cal.bin_win_rate == sorted(cal.bin_win_rate)
    assert cal.n == len(scores)


def test_isotonic_calibration_predict_is_monotone():
    cal = isotonic_calibrate([0.1, 0.3, 0.5, 0.7, 0.9], [0, 1, 0, 1, 1], n_bins=4)
    preds = [cal.predict(x) for x in [0.05, 0.2, 0.4, 0.6, 0.8, 1.0]]
    assert preds == sorted(preds)


def test_isotonic_calibration_handles_empty_and_constant():
    assert isotonic_calibrate([], []).n == 0
    const = isotonic_calibrate([0.5, 0.5, 0.5], [1, 1, 0])
    assert const.n == 3


def test_regime_edge_disables_negative_expectancy():
    from jarvis.backtesting.trade_simulator import TradeOutcome

    def mk(regime, pnl_r):
        return TradeOutcome(
            symbol="T", side="BUY", entry_idx=0, entry_time=0, exit_idx=1,
            exit_time=1, bars_held=1, entry=100.0, exit=100.0 + pnl_r,
            sl_initial=99.0, sl_final=99.0, tp=101.0, risk_dist=1.0,
            result="X", mfe_r=0.0, mae_r=0.0, pnl_r=pnl_r,
            pnl_money_per_lot=pnl_r, is_win=pnl_r > 0,
            partial_taken=False, be_locked=False, regime=regime,
        )

    outs = [mk("GOOD", 1.0)] * 15 + [mk("BAD", -1.0)] * 15
    table = regime_edge_table(outs, min_trades=5)
    assert table["GOOD"].enabled is True
    assert table["BAD"].enabled is False
    assert "worse than" in table["BAD"].reason


def test_regime_edge_does_not_disable_on_marginal_negative():
    """A marginal negative expectancy must NOT switch a regime off.

    An early version disabled gold's dominant regime on -0.011R, removing 490 of
    ~1,500 bars for no demonstrated reason.
    """
    from jarvis.backtesting.trade_simulator import TradeOutcome

    def mk(regime, pnl_r):
        return TradeOutcome(
            symbol="T", side="BUY", entry_idx=0, entry_time=0, exit_idx=1,
            exit_time=1, bars_held=1, entry=100.0, exit=100.0 + pnl_r,
            sl_initial=99.0, sl_final=99.0, tp=101.0, risk_dist=1.0,
            result="X", mfe_r=0.0, mae_r=0.0, pnl_r=pnl_r,
            pnl_money_per_lot=pnl_r, is_win=pnl_r > 0,
            partial_taken=False, be_locked=False, regime=regime,
        )

    outs = [mk("MARGINAL", -0.011)] * 40
    table = regime_edge_table(outs, min_trades=5, disable_margin_r=0.05)
    assert table["MARGINAL"].enabled is True
    assert "within tolerance" in table["MARGINAL"].reason


def test_regime_edge_leaves_small_samples_enabled_even_if_negative():
    from jarvis.backtesting.trade_simulator import TradeOutcome

    o = TradeOutcome(
        symbol="T", side="BUY", entry_idx=0, entry_time=0, exit_idx=1, exit_time=1,
        bars_held=1, entry=100.0, exit=99.0, sl_initial=99.0, sl_final=99.0,
        tp=101.0, risk_dist=1.0, result="X", mfe_r=0.0, mae_r=1.0, pnl_r=-1.0,
        pnl_money_per_lot=-1.0, is_win=False, partial_taken=False, be_locked=False,
        regime="THIN",
    )
    table = regime_edge_table([o] * 8, min_trades=5, min_trades_to_disable=12)
    assert table["THIN"].enabled is True
    assert "too few to disable" in table["THIN"].reason


def test_regime_edge_leaves_thin_samples_enabled():
    from jarvis.backtesting.trade_simulator import TradeOutcome

    o = TradeOutcome(
        symbol="T", side="BUY", entry_idx=0, entry_time=0, exit_idx=1, exit_time=1,
        bars_held=1, entry=100.0, exit=99.0, sl_initial=99.0, sl_final=99.0,
        tp=101.0, risk_dist=1.0, result="X", mfe_r=0.0, mae_r=1.0, pnl_r=-1.0,
        pnl_money_per_lot=-1.0, is_win=False, partial_taken=False, be_locked=False,
        regime="RARE",
    )
    table = regime_edge_table([o], min_trades=5)
    assert table["RARE"].enabled is True


def test_profile_roundtrip():
    p = WRTargetProfile(
        symbol="TEST", target_wr=0.75,
        geometry=Geometry(tp_r=0.5, be_trigger_r=None, fast_cash_r=None, max_bars=24, min_score=0.7),
        score_col="score", n_trades=30, win_rate=0.76, expectancy_r=0.15,
        oos_trades=25, oos_win_rate=0.72, oos_expectancy_r=0.09,
        score_calibration=isotonic_calibrate([0.5, 0.6, 0.7, 0.8], [0, 1, 1, 1]),
        regime_edge={"TREND": RegimeEdge("TREND", 10, 0.7, 0.2, 2.0, True, "ok")},
        regime_geometry={"TREND": Geometry(tp_r=1.5)},
    )
    p2 = WRTargetProfile.from_dict(p.to_dict())
    assert p2.symbol == p.symbol
    assert p2.geometry == p.geometry
    assert p2.regime_edge["TREND"].enabled is True
    assert p2.regime_geometry["TREND"].tp_r == 1.5
    assert p2.score_calibration is not None


def test_profile_geometry_for_regime_override():
    p = WRTargetProfile(
        symbol="T", target_wr=0.75, geometry=Geometry(tp_r=0.5),
        regime_geometry={"TREND": Geometry(tp_r=1.5)},
    )
    assert p.geometry_for("TREND").tp_r == 1.5
    assert p.geometry_for("UNKNOWN").tp_r == 0.5
    assert p.effective_target_r("TREND") == 1.5


# ─────────────────────────────────────────────────────────────────────────────
# Entry policy
# ─────────────────────────────────────────────────────────────────────────────
class _Gate:
    def __init__(self, checks, passed=True):
        self.checks = checks
        self.passed = passed
        self.failing_reasons = [k for k, v in checks.items() if not v]


class _Profile:
    def __init__(self, min_score=0.0, regime_edge=None,
                 oos_expectancy_r=0.0, oos_trades=0):
        self.geometry = Geometry(tp_r=0.5, min_score=min_score)
        self.regime_edge = regime_edge or {}
        # Purged out-of-sample expectancy produced by WRTargetCalibrator.
        # The entry gate refuses to trade a symbol whose validated OOS edge
        # is non-positive (the root cause of the negative portfolio expectancy).
        self.oos_expectancy_r = oos_expectancy_r
        self.oos_trades = oos_trades


def test_capital_protection_checked_before_edge_filter():
    gate = _Gate({"Market Session Open": False, "Drawdown Safety Guard": True})
    dec = evaluate_entry(quality_gate=gate, score=0.99, regime="TREND",
                         profile=_Profile(min_score=0.0))
    assert dec.allowed is False
    assert "capital protection" in dec.reason
    assert "Market Session Open" in dec.protection_failures


def test_capital_protection_set_is_protective_only():
    for g in CAPITAL_PROTECTION_GATES:
        assert not any(k in g for k in ("Score", "Reward", "Expected Value", "Devil"))


def test_gold_session_window_blocks_calibrated_path():
    """2026-10-20 regression: the 12-16 UTC gold window must be a PROTECTION gate.

    "Forex Prime Session" and "Strategy Viable" used to live in EDGE_GATES, so
    the calibrated path ignored the legacy gate verdict: on 2026-10-20 the
    engine opened gold #1014 (BUY 09:15 UTC), #1016 (SELL 09:48) and #1017
    (SELL 10:08) -- all outside the profile's 12:00-16:00 window -- because
    their calibrated scores cleared the profile threshold. A failing session
    check must now refuse the candidate outright regardless of score.
    """
    gate = _Gate({
        g: True for g in CAPITAL_PROTECTION_GATES
    } | {"Gold Session Window": False, "Forex Prime Session": False})
    dec = evaluate_entry(quality_gate=gate, score=0.99, regime="TREND",
                         profile=_Profile(min_score=0.0))
    assert dec.allowed is False
    assert "capital protection" in dec.reason
    assert "Gold Session Window" in dec.protection_failures
    assert "Forex Prime Session" in dec.protection_failures


def test_gold_strategy_viable_blocks_calibrated_path():
    """A banned strategy must be refused on the calibrated path, not merely
    score-suppressed: gold trades #1015/#1016 on 2026-10-13/20 used
    TREND_FOLLOWING/TREND_PULLBACK while the profile blacklisted
    RANGE_MEAN_REVERSION/TREND_FOLLOWING -- the edge gate only deducts
    points, it never says no.
    """
    gate = _Gate({
        g: True for g in CAPITAL_PROTECTION_GATES
    } | {"Strategy Viable": False})
    dec = evaluate_entry(quality_gate=gate, score=0.99, regime="TREND",
                         profile=_Profile(min_score=0.0))
    assert dec.allowed is False
    assert "Strategy Viable" in dec.protection_failures


def test_gold_protection_gates_ok_when_window_open():
    """Sanity: within the window with a viable strategy the trade still passes."""
    gate = _Gate({g: True for g in CAPITAL_PROTECTION_GATES})
    dec = evaluate_entry(quality_gate=gate, score=0.99, regime="TREND",
                         profile=_Profile(min_score=0.5))
    assert dec.allowed is True


def test_edge_threshold_blocks_low_score():
    gate = _Gate({g: True for g in CAPITAL_PROTECTION_GATES})
    dec = evaluate_entry(quality_gate=gate, score=0.4, regime="TREND",
                         profile=_Profile(min_score=0.7))
    assert dec.allowed is False
    assert "below calibrated threshold" in dec.reason


def test_disabled_regime_blocks_even_with_high_score():
    gate = _Gate({g: True for g in CAPITAL_PROTECTION_GATES})
    edge = {"TREND": RegimeEdge("TREND", 10, 0.3, -0.2, -2.0, False, "no edge")}
    dec = evaluate_entry(quality_gate=gate, score=0.99, regime="TREND",
                         profile=_Profile(min_score=0.5, regime_edge=edge))
    assert dec.allowed is False
    assert "disabled by learned policy" in dec.reason


def test_entry_allowed_when_all_clear():
    gate = _Gate({g: True for g in CAPITAL_PROTECTION_GATES})
    dec = evaluate_entry(quality_gate=gate, score=0.9, regime="TREND",
                         profile=_Profile(min_score=0.5))
    assert dec.allowed is True


def test_oos_negative_expectancy_refused():
    """A symbol whose validated OOS expectancy is non-positive must be refused.

    This is the regression guard for the negative-portfolio-expectancy bug:
    six symbols (EURUSD, USDCHF, NZDUSD, USDJPY, SOLUSD, US30) deployed with a
    positive in-sample expectancy that decayed to a negative OOS expectancy,
    and the engine traded them anyway because nothing checked the OOS number.
    """
    gate = _Gate({g: True for g in CAPITAL_PROTECTION_GATES})
    dec = evaluate_entry(
        quality_gate=gate, score=0.99, regime="TREND",
        profile=_Profile(min_score=0.5, oos_expectancy_r=-0.042, oos_trades=40),
    )
    assert dec.allowed is False
    assert "no validated edge" in dec.reason
    assert "OOS expectancy" in dec.reason


def test_oos_positive_expectancy_allowed_past_gate():
    """A positive validated OOS expectancy must not trip the refusal gate.

    The symbol should still fall through to the normal edge gate, so a high
    enough score clears it.
    """
    gate = _Gate({g: True for g in CAPITAL_PROTECTION_GATES})
    dec = evaluate_entry(
        quality_gate=gate, score=0.95, regime="TREND",
        profile=_Profile(min_score=0.5, oos_expectancy_r=0.09, oos_trades=25),
    )
    assert dec.allowed is True
    assert "calibrated edge filter passed" in dec.reason


def test_oos_negative_but_thin_sample_not_refused():
    """A negative OOS expectancy with too few OOS trades must NOT be refused.

    The gate only refuses when ``oos_trades >= 10`` so it cannot reject a
    symbol on noise. A thin/missing purged-fold sample falls through to the
    normal edge gate rather than refusing on an untrustworthy number.
    """
    gate = _Gate({g: True for g in CAPITAL_PROTECTION_GATES})
    dec = evaluate_entry(
        quality_gate=gate, score=0.95, regime="TREND",
        profile=_Profile(min_score=0.5, oos_expectancy_r=-0.5, oos_trades=3),
    )
    # Not refused by the OOS gate — falls through to the edge filter and passes.
    assert dec.allowed is True
    assert "no validated edge" not in dec.reason


def test_no_profile_falls_back_to_legacy_verdict():
    gate = _Gate({g: True for g in CAPITAL_PROTECTION_GATES}, passed=True)
    assert evaluate_entry(quality_gate=gate, score=0.1, regime="X", profile=None).allowed is True
    gate2 = _Gate({g: True for g in CAPITAL_PROTECTION_GATES}, passed=False)
    assert evaluate_entry(quality_gate=gate2, score=0.1, regime="X", profile=None).allowed is False


def test_capital_protection_failures_handles_missing_checks():
    assert capital_protection_failures(None) == ()
    assert capital_protection_failures(object()) == ()


# ─────────────────────────────────────────────────────────────────────────────
# Hermetic mode
# ─────────────────────────────────────────────────────────────────────────────
def test_offline_mode_toggles_and_restores():
    original = is_offline()
    with offline_mode():
        assert is_offline() is True
    assert is_offline() == original


def test_offline_mode_nests():
    with offline_mode():
        with offline_mode():
            assert is_offline() is True
        assert is_offline() is True
    assert is_offline() is False


def test_realtime_optimizer_is_neutral_offline():
    from jarvis.intelligence.realtime_optimizer import RealtimeOptimizer

    opt = RealtimeOptimizer()
    with offline_mode():
        adj = opt.get_adjustments("EURUSD", "TREND_BULL")
    assert adj == {"win_p_delta": 0.0, "score_delta": 0.0, "rr_delta": 0.0}


def test_online_ml_predictor_does_not_write_offline(tmp_path, monkeypatch):
    from jarvis.learning.online_ml_predictor import OnlineMLPredictor

    p = OnlineMLPredictor()
    target = tmp_path / "weights.json"
    monkeypatch.setattr(p, "model_file", str(target), raising=False)
    with offline_mode():
        p._save_model()
    assert not target.exists()


def test_meta_labeler_is_neutral_offline():
    from jarvis.intelligence.meta_labeler import MetaLabeler

    ml = MetaLabeler()
    with offline_mode():
        assert ml.model is None


def test_set_offline_roundtrip():
    set_offline(True)
    assert is_offline() is True
    set_offline(False)
    assert is_offline() is False


# ─────────────────────────────────────────────────────────────────────────────
# Reachability guard
#
# The calibrator is asked to *reach* a win-rate target, and the cheapest way to
# raise a win rate is to move the target closer. Unconstrained, the search walks
# tp_r down into the region where the target cannot break even: at tp_r=0.25 a
# strategy needs 80% just to break even, so a 75% win rate loses money. Measured
# on the 16-symbol portfolio: 7 of 16 symbols were calibrated to tp_r < 0.3333
# and all 7 lost out of sample. These tests pin the guard that prevents it.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("target", [0.50, 0.60, 0.6667, 0.75, 0.80, 0.90])
def test_min_tp_r_for_target_is_exactly_the_breakeven_point(target):
    floor = min_tp_r_for_target(target)
    # At the floor, the break-even win rate equals the target exactly.
    assert 1.0 / (1.0 + floor) == pytest.approx(target, rel=1e-9)


def test_min_tp_r_for_target_known_value_for_75_percent():
    # 1/0.75 - 1 = 1/3. This is the number quoted in the portfolio report.
    assert min_tp_r_for_target(0.75) == pytest.approx(1.0 / 3.0, rel=1e-12)
    assert min_tp_r_for_target(0.75) == pytest.approx(0.333333, abs=1e-6)


def test_min_tp_r_for_target_margin_raises_the_floor():
    base = min_tp_r_for_target(0.75)
    wider = min_tp_r_for_target(0.75, margin=0.17)
    assert wider > base
    # 0.3333 + 0.17 == 0.5033, i.e. "require tp_r >= 0.5 at a 75% target".
    assert wider == pytest.approx(0.503333, abs=1e-5)
    # A negative margin must not lower the floor below break-even.
    assert min_tp_r_for_target(0.75, margin=-5.0) == pytest.approx(base)


@pytest.mark.parametrize("bad", [0.0, -0.5, 1.0, 1.5, float("nan")])
def test_min_tp_r_for_target_handles_degenerate_targets(bad):
    # No meaningful floor can be derived; return 0.0 rather than raising, so a
    # misconfigured target degrades to "no constraint" instead of crashing.
    assert min_tp_r_for_target(bad) == 0.0


def test_frontier_grid_still_contains_the_unreachable_corner():
    # The low targets must REMAIN in the grid: the frontier diagnostic needs
    # them to show how much win rate is buyable by shrinking the target, and
    # what it costs. Only *selection* is restricted.
    grid = default_geometry_grid(coarse=True)
    tps = {float(g.tp_r) for g in grid}
    assert 0.25 in tps
    assert 0.3 in tps


def _calibrator(**kw):
    base = dict(target_wr=0.75, min_trades=20, folds=4, coarse_grid=True, slippage_pips=0.5)
    base.update(kw)
    return WRTargetCalibrator(**base)


def test_reachability_guard_excludes_unbreakable_geometries_from_selection():
    cands = pd.DataFrame({"score": [0.1, 0.4, 0.6, 0.9], "bar_idx": [1, 2, 3, 4]})
    combos = _calibrator()._grid_for(cands, "score")
    tps = {float(g.tp_r) for g, _ in combos}
    floor = min_tp_r_for_target(0.75)
    assert tps, "selection space must not be empty"
    assert all(tp >= floor for tp in tps), f"unbreakable geometry survived: {sorted(tps)}"
    assert 0.25 not in tps
    assert 0.3 not in tps
    # The smallest offered target is the smallest one that can break even.
    assert min(tps) == pytest.approx(0.4)


def test_reachability_guard_can_be_disabled_to_reproduce_the_trap():
    cands = pd.DataFrame({"score": [0.1, 0.4, 0.6, 0.9], "bar_idx": [1, 2, 3, 4]})
    combos = _calibrator(enforce_reachable_target=False)._grid_for(cands, "score")
    tps = {float(g.tp_r) for g, _ in combos}
    assert 0.25 in tps and 0.3 in tps


def test_reachability_guard_scales_with_the_target():
    cands = pd.DataFrame({"score": [0.1, 0.4, 0.6, 0.9], "bar_idx": [1, 2, 3, 4]})
    # A LOWER win-rate target demands a WIDER payoff. At 60% the geometry must
    # clear 1/0.6 - 1 = 0.6667, so a 0.5R target is NOT admissible...
    tps60 = {float(g.tp_r) for g, _ in _calibrator(target_wr=0.60)._grid_for(cands, "score")}
    assert 0.5 not in tps60
    assert 0.75 in tps60
    # ...whereas at 90% a close target is affordable: 1/0.9 - 1 = 0.1111, so
    # even 0.25R qualifies.
    tps90 = {float(g.tp_r) for g, _ in _calibrator(target_wr=0.90)._grid_for(cands, "score")}
    assert 0.25 in tps90
    # At 75% it does not.
    tps75 = {float(g.tp_r) for g, _ in _calibrator(target_wr=0.75)._grid_for(cands, "score")}
    assert 0.25 not in tps75
    # The floor is monotonically decreasing in the target.
    assert min_tp_r_for_target(0.60) > min_tp_r_for_target(0.75) > min_tp_r_for_target(0.90)


def test_reachability_guard_never_empties_the_selection_space():
    # A target low enough that NO grid geometry can clear it (1/0.35 - 1 = 1.857
    # exceeds the widest grid target of 1.5) must not leave the calibrator with
    # nothing to search. The guard falls back to the full grid rather than
    # producing an empty selection space.
    cands = pd.DataFrame({"score": [0.1, 0.4, 0.6, 0.9], "bar_idx": [1, 2, 3, 4]})
    widest = max(float(g.tp_r) for g in default_geometry_grid(coarse=True))
    assert min_tp_r_for_target(0.35) > widest
    combos = _calibrator(target_wr=0.35)._grid_for(cands, "score")
    assert combos, "guard must not empty the selection space"


# ─────────────────────────────────────────────────────────────────────────────
# Simulator / engine exit-policy parity
#
# The calibrator measures out-of-sample expectancy on a geometry, and the engine
# is supposed to trade that same geometry. If the two resolve ``trail_atr=None``
# differently they silently trade different exit schedules. The engine used to
# coerce None to 1.5 ("or 1.5"), turning a disabled trail into a live 1.5xATR
# trail. That is invisible below tp_r=2.0 -- the trail only engages at
# trail_activation_r, which a 1.5R trade never reaches -- and severe at and above
# it: on NAS100 the calibrator predicted 44.2% WR / +0.125R while the engine
# realised 23.3% WR / -0.246R.
# ─────────────────────────────────────────────────────────────────────────────
def _nas100():
    from jarvis.data.symbol_registry import resolve

    return resolve("NAS100")


@pytest.mark.parametrize("trail_atr", [None, 1.5, 2.0])
def test_engine_and_simulator_resolve_the_same_exit_policy(trail_atr):
    from jarvis.backtesting.exit_geometry import build_exit_geometry

    spec = _nas100()
    engine_policy = build_exit_geometry(
        "A_fixed_tp", tp_r=2.0, trail_atr=trail_atr, trail_activation_r=2.0
    ).to_policy("NAS100", spec)
    sim_policy = Geometry(
        tp_r=2.0, be_trigger_r=None, fast_cash_r=None, max_bars=48,
        trail_atr=trail_atr, trail_activation_r=2.0,
    ).to_policy("NAS100", spec)

    assert engine_policy.trail_activation_r == sim_policy.trail_activation_r
    assert engine_policy.runner_trail_atr == sim_policy.runner_trail_atr
    assert engine_policy.be_trigger_r == sim_policy.be_trigger_r


def test_trail_atr_none_disables_the_trail_on_both_paths():
    from jarvis.backtesting.exit_geometry import build_exit_geometry

    spec = _nas100()
    engine_policy = build_exit_geometry(
        "A_fixed_tp", tp_r=2.0, trail_atr=None, trail_activation_r=2.0
    ).to_policy("NAS100", spec)
    sim_policy = Geometry(
        tp_r=2.0, be_trigger_r=None, fast_cash_r=None, max_bars=48,
        trail_atr=None, trail_activation_r=2.0,
    ).to_policy("NAS100", spec)

    # Disabled is expressed as an unreachable activation, not a magic value.
    for policy in (engine_policy, sim_policy):
        assert policy.trail_activation_r >= 1e9, "trail must be unreachable when disabled"


def test_trail_atr_set_enables_the_trail_on_both_paths():
    from jarvis.backtesting.exit_geometry import build_exit_geometry

    spec = _nas100()
    engine_policy = build_exit_geometry(
        "A_fixed_tp", tp_r=2.0, trail_atr=1.5, trail_activation_r=2.0
    ).to_policy("NAS100", spec)
    assert engine_policy.trail_activation_r == pytest.approx(2.0)
    assert engine_policy.runner_trail_atr == pytest.approx(1.5)


def test_calibration_grid_never_enables_a_runner_trail():
    # The grid is a fixed-TP search space. If a geometry ever shipped with a live
    # trail, the engine's realised exit schedule would stop matching the one the
    # out-of-sample expectancy was measured on.
    for g in default_geometry_grid(coarse=True):
        assert g.trail_atr is None, f"grid geometry enables a trail: {g.key()}"


# ─────────────────────────────────────────────────────────────────────────────
# Stop slippage must survive the WHOLE call chain
# ─────────────────────────────────────────────────────────────────────────────
# ``simulate_trade`` modelled stop slippage correctly from the start, and the
# three tests above pin that. What was NOT pinned is that the argument survives
# the aggregators above it.
#
# ``simulate_all_candidates`` accepted ``slippage_price_equiv`` and never passed
# it to ``simulate_trade``, and ``WRTargetCalibrator.calibrate_symbol`` computed
# ``slip = slippage_pips * pip_size`` and dropped it -- that line was the only
# reference to ``slip`` in the entire module. Net effect: the calibration
# instrument charged NO stop slippage while ``BacktestEngine`` charged
# ``actual_slippage_delta`` on every protective-stop fill. A 1000-pip argument
# left every outcome bit-identical, i.e. the parameter was inert.
#
# Same failure class as the ``trail_atr=None`` bug: the instrument and the engine
# silently resolved different costs, so the calibration reported an expectancy the
# engine could not realise. Measured on EURUSD at the deployed geometry, 793 of
# 1113 exits are stop exits and the real 0.5-pip charge is 0.0220 R per stop --
# -0.0157 R per trade, against a per-symbol expectancy of a few hundredths of R.
def _stop_exit_only(symbol="TEST"):
    """Candidates whose forward path reaches the stop and never the target."""
    n = 4
    df = make_df(
        highs=[100.0] * (n + 2),
        lows=[100.0] + [98.0] * (n + 1),
        closes=[100.0] + [98.5] * (n + 1),
    )
    cands = pd.DataFrame({
        "symbol": [symbol] * n,
        "bar_idx": [0, 1, 2, 3],
        "side": ["BUY"] * n,
        "fill": [100.0] * n,
        "sl": [99.0] * n,
        "score": [0.5] * n,
        "regime": ["TREND"] * n,
        "strategy": ["S"] * n,
        "zone": ["DISCOUNT"] * n,
    })
    return df, cands


def test_simulate_all_candidates_forwards_slippage():
    """The aggregator must not swallow ``slippage_price_equiv``.

    Regression: it accepted the argument and discarded it, so a 1000-pip
    argument left every outcome bit-identical.
    """
    df, cands = _stop_exit_only()
    geom = Geometry(tp_r=5.0, be_trigger_r=None, fast_cash_r=None, max_bars=10)
    free = simulate_all_candidates(
        df=df, candidates=cands, geom=geom, money_per_unit=100_000.0, symbol="TEST",
    )
    slipped = simulate_all_candidates(
        df=df, candidates=cands, geom=geom, money_per_unit=100_000.0, symbol="TEST",
        slippage_price_equiv=1000.0,
    )
    assert free and slipped
    assert all(o.result == "SL" for o in free)
    # risk_dist = 1.0, so every stop is deepened by exactly the slippage.
    assert [o.pnl_r for o in free] != [o.pnl_r for o in slipped]
    assert slipped[0].pnl_r == pytest.approx(free[0].pnl_r - 1000.0, abs=1e-6)


def test_evaluate_geometry_forwards_slippage():
    """The convenience wrapper must forward slippage too (it delegates)."""
    df, cands = _stop_exit_only()
    geom = Geometry(tp_r=5.0, be_trigger_r=None, fast_cash_r=None,
                    max_bars=10, min_score=0.0)
    free = evaluate_geometry(
        df=df, candidates=cands, geom=geom, money_per_unit=100_000.0, symbol="TEST",
    )
    slipped = evaluate_geometry(
        df=df, candidates=cands, geom=geom, money_per_unit=100_000.0, symbol="TEST",
        slippage_price_equiv=0.1,
    )
    assert free and slipped
    assert slipped[0].pnl_r == pytest.approx(free[0].pnl_r - 0.1, abs=1e-6)


def test_aggregator_slippage_never_touches_target_fills():
    """Slippage is charged on protective stops only -- never on the target.

    BacktestEngine fills a TP at exactly ``tp``; if the simulator charged
    slippage there as well the two paths would disagree in the opposite
    direction, and this fix would overshoot into over-charging.
    """
    n = 4
    df = make_df(
        highs=[100.0] + [101.5] * (n + 1),
        lows=[100.0] * (n + 2),
        closes=[100.0] + [101.4] * (n + 1),
    )
    cands = pd.DataFrame({
        "symbol": ["TEST"] * n, "bar_idx": [0, 1, 2, 3], "side": ["BUY"] * n,
        "fill": [100.0] * n, "sl": [99.0] * n, "score": [0.5] * n,
        "regime": ["TREND"] * n, "strategy": ["S"] * n, "zone": ["DISCOUNT"] * n,
    })
    geom = Geometry(tp_r=1.0, be_trigger_r=None, fast_cash_r=None, max_bars=10)
    free = simulate_all_candidates(
        df=df, candidates=cands, geom=geom, money_per_unit=100_000.0, symbol="TEST",
    )
    slipped = simulate_all_candidates(
        df=df, candidates=cands, geom=geom, money_per_unit=100_000.0, symbol="TEST",
        slippage_price_equiv=0.1,
    )
    assert free and all(o.result == "TP" for o in free)
    assert [o.pnl_r for o in free] == [o.pnl_r for o in slipped]


def _calibration_fixture(n_bars: int = 400, seed: int = 7):
    """A self-contained FX-scale sample the calibrator can actually fit.

    Two properties matter, both arrived at the hard way:

    * the risk distance must be in PIP SCALE. An earlier version used price
      100.0 with 1R = 1.0 -- 10,000 pips -- against which a 0.5-pip slippage is
      0.00005 R, i.e. invisible. Slippage is a pip quantity and only means
      something against a pip-scale stop.
    * the sample must not be degenerate. A strict sawtooth made every candidate
      resolve the same way (0% win rate), and a single-regime sample had that
      regime switched off by the learned policy, leaving zero trades. A sample
      that cannot respond to anything cannot test anything.

    Stops sit on the losing side: below entry for a BUY, above for a SELL.
    Putting a SELL stop below its entry makes it a +1R profit target instead.
    """
    rng = np.random.RandomState(seed)
    close = 1.1000 + np.cumsum(rng.normal(0.0, 0.0009, n_bars))
    high = close + np.abs(rng.normal(0.0, 0.0006, n_bars))
    low = close - np.abs(rng.normal(0.0, 0.0006, n_bars))
    df = pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n_bars, freq="1h", tz="UTC"),
        "open": np.concatenate([[1.1000], close[:-1]]),
        "high": high, "low": low, "close": close,
        "atr": [0.0009] * n_bars, "spread": [0.0001] * n_bars,
    })
    risk = 0.0020  # 20 pips
    # Spacing matters for stability, not just sample size: at one candidate per
    # 6 bars the slippage flips the fold-selected geometry (tp_r 0.75 -> 0.4) and
    # the win rate moves with it, which would confound "slippage lowers
    # expectancy" with "a different geometry was chosen". One per 4 bars holds
    # the geometry fixed, so the only thing that moves is the cost.
    idx = list(range(5, n_bars - 60, 4))
    side = ["BUY" if (i // 4) % 2 == 0 else "SELL" for i in idx]
    fill = [float(close[i]) for i in idx]
    sl = [f - risk if s == "BUY" else f + risk for f, s in zip(fill, side)]
    cands = pd.DataFrame({
        "symbol": ["EURUSD"] * len(idx), "bar_idx": idx, "side": side,
        "fill": fill, "sl": sl, "score": [0.5] * len(idx),
        "regime": ["TREND"] * len(idx), "strategy": ["S"] * len(idx),
        "zone": ["DISCOUNT"] * len(idx),
    })
    return df, cands


def test_calibrator_charges_stop_slippage():
    """Integration: the calibrator must not drop the slippage it computes.

    ``calibrate_symbol`` derived ``slip = slippage_pips * pip_size`` explicitly
    to mirror the engine's ``actual_slippage_delta`` and then never used it, so
    ``slippage_pips`` had NO effect on any calibrated number whatsoever. Three
    assertions, weakest to strongest:

      1. a higher slippage strictly lowers expectancy (accounting);
      2. the win rate is untouched -- slippage deepens stop exits, it never
         reclassifies a win as a loss;
      3. an absurd slippage leaves nothing tradeable. This is the real
         regression test: with the parameter dropped, the 1000-pip run would
         return the same healthy profile as zero slippage.
    """
    df, cands = _calibration_fixture()

    def calibrate(slippage_pips):
        return WRTargetCalibrator(
            target_wr=0.75, min_trades=12, folds=4, coarse_grid=True,
            slippage_pips=slippage_pips, regime_geometry=False,
        ).calibrate_symbol(df=df, candidates=cands, symbol="EURUSD")

    free = calibrate(0.0)
    slipped = calibrate(0.5)
    absurd = calibrate(1000.0)

    assert free.n_trades > 0 and slipped.n_trades > 0
    assert slipped.win_rate == pytest.approx(free.win_rate, abs=1e-9)
    assert slipped.expectancy_r < free.expectancy_r
    # Measured on this fixture: +0.13600R -> +0.12640R, i.e. 0.0096R per trade.
    # 1 pip is 0.05R here, so half a pip on each stop exit is material against a
    # per-symbol expectancy of a few hundredths of an R.
    assert free.expectancy_r - slipped.expectancy_r > 0.004
    # 1000 pips is 50R per stop exit: nothing can reach positive expectancy.
    assert absurd.n_trades == 0, (
        "a 1000-pip slippage left tradeable configurations, so the calibrator "
        "is not charging stop slippage at all"
    )
