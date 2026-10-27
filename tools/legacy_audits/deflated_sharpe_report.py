#!/usr/bin/env python
"""P2-1 — deflate the headline Sharpe for the search that produced it.

The audit's acceptance test is *"DSR > 0.95 before any parameter set is promoted
to live"*.  ``jarvis/learning/deflated_sharpe.py`` implements the estimator, but
a formula with no number attached is not a control.  This tool attaches it.

What it does, per symbol:

1. Replays the symbol's candidate table at every ``tp_r`` in the search grid.
   The grid is the one the project actually searched, so ``n_trials`` is a
   measured count rather than a guess.
2. Takes the **dispersion of the trial Sharpes** as the input to
   ``expected_max_sharpe``.  This matters more than the trial count: a search
   whose trials all land on the same Sharpe raises the bar far less than a
   search whose trials scatter, and only the data knows which one happened.
3. Reports three numbers in increasing order of honesty:

   * ``psr`` — ``P(SR > 0)`` for this track record alone, as if it were the only
     configuration ever tried.  This is the number the project has been quoting
     implicitly, and it is the one that is wrong.
   * ``dsr_selected`` — the same, deflated for having *chosen* the best ``tp_r``.
   * ``dsr_production`` — deflated, at the geometry that is actually live
     (``tp_r = 1.5``), so "the live config passes" is a separate question from
     "the best config passes".

The portfolio row pools every symbol's R series and deflates for the full
``grid x symbols`` search, because "promote to live" is a portfolio decision.

Usage
-----
    python tools/deflated_sharpe_report.py --tf H1 --window 365
    python tools/deflated_sharpe_report.py --tp-grid 1.5,2.0

Read-only with respect to the trading code; writes only its report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from jarvis.backtesting.trade_simulator import (  # noqa: E402
    BarArrays, Geometry, simulate_trade,
)
from jarvis.data.symbol_registry import (  # noqa: E402
    get_dollar_risk_per_price_unit, resolve,
)
from jarvis.learning.deflated_sharpe import (  # noqa: E402
    deflated_sharpe_ratio,
    expected_max_sharpe,
    min_track_record_length,
    probabilistic_sharpe_ratio,
    sharpe_stats,
)
from jarvis.learning.sample_weights import SampleUniquenessWeightEngine  # noqa: E402
from tools.audit_trade_quality import load_symbol  # noqa: E402

# The grid the geometry search actually walked (mirrors
# tools/p0_3_exit_geometry_measurement.py:TP_GRID).  Kept in sync deliberately:
# a deflation that under-counts the search is worse than no deflation.
TP_GRID_DEFAULT = (1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0)
PRODUCTION_TP = 1.5
ACCEPT_DSR = 0.95
BARS_PER_YEAR_H1 = 24 * 252


def replay_spans(symbol: str, df: pd.DataFrame, cands: pd.DataFrame, geom: Geometry,
                 slip_price: float) -> Optional[Dict[str, object]]:
    """Replay one geometry, keeping the bar span of every trade.

    Mirrors ``tools.audit_trade_quality.replay`` but also records ``entry_bar``
    and ``duration_bars``, which the uniqueness weighting needs. The spans are
    what turn "46,939 trades" into an honest independent-bet count.
    """
    spec = resolve(symbol)
    money = get_dollar_risk_per_price_unit(symbol, None)
    bars = BarArrays.from_df(df)
    n = len(df)
    r_out: List[float] = []
    trades: List[Dict[str, object]] = []
    for row in cands.itertuples(index=False):
        i = int(getattr(row, "bar_idx"))
        if i + 1 >= n:
            continue
        side = str(getattr(row, "side", "")).upper()
        if side not in ("BUY", "SELL"):
            continue
        out = simulate_trade(
            symbol=symbol, side=side, entry_idx=i,
            fill=float(getattr(row, "fill")), sl=float(getattr(row, "sl")),
            geom=geom, money_per_unit=money, bars=bars,
            cost_price_equiv=0.0, slippage_price_equiv=slip_price,
            ai_score=float(getattr(row, "score", 0.0) or 0.0),
            calibrated_win_p=float(getattr(row, "score", 0.0) or 0.0),
            regime=str(getattr(row, "regime", "UNKNOWN")),
            strategy=str(getattr(row, "strategy", "UNKNOWN")),
            zone=str(getattr(row, "zone", "UNKNOWN")), spec=spec,
        )
        if out is None:
            continue
        r_out.append(float(out.pnl_r))
        trades.append({
            "entry_bar": int(out.entry_idx),
            "duration_bars": max(1, int(out.exit_idx) - int(out.entry_idx) + 1),
            "pnl": float(out.pnl_r),
        })
    if not r_out:
        return None
    return {"r": np.asarray(r_out, dtype=float), "trades": trades}


def symbol_series(symbol: str, df: pd.DataFrame, cands: pd.DataFrame,
                  tp_grid: List[float], slip_price: float) -> Optional[Dict[str, object]]:
    """Replay every ``tp_r`` in the grid; return R series and spans per trial."""
    series: Dict[str, np.ndarray] = {}
    spans: Dict[str, List[Dict[str, object]]] = {}
    for tp in tp_grid:
        got = replay_spans(symbol, df, cands, Geometry(tp_r=float(tp)), slip_price)
        if got is None:
            continue
        key = f"{tp:.2f}"
        series[key] = got["r"]  # type: ignore[assignment]
        spans[key] = got["trades"]  # type: ignore[assignment]
    if not series:
        return None
    return {"series": series, "spans": spans}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--window", type=int, default=365)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--tp-grid", default=",".join(f"{t:g}" for t in TP_GRID_DEFAULT))
    ap.add_argument("--slippage-pips", type=float, default=0.5,
                    help="must match the audit's cost basis (audit_trade_quality)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tf = args.tf.upper()
    tp_grid = [float(x) for x in args.tp_grid.split(",") if x.strip()]
    args.out = args.out or os.path.join(REPO, "reports", "deflated_sharpe.json")

    from tools.audit_trade_quality import REAL_DIR  # noqa: E402
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(d for d in os.listdir(REAL_DIR)
                         if os.path.isdir(os.path.join(REAL_DIR, d)))

    prod_key = f"{PRODUCTION_TP:.2f}"
    rows: List[Dict[str, object]] = []
    pooled: List[np.ndarray] = []
    pooled_spans: List[Dict[str, object]] = []
    all_trial_sr: List[float] = []

    print(f"tf={tf} window={args.window}d  grid={tp_grid}  ({len(tp_grid)} trials/symbol)")
    print(f"{'sym':8s} {'n':>6s} {'n_eff':>8s} {'SR_obs':>7s} {'SR_ann':>7s} {'skew':>6s} "
          f"{'kurt':>6s} {'best':>5s} {'PSR':>6s} {'DSR':>7s} {'DSReff':>7s}  verdict")

    for sym in symbols:
        df, cands = load_symbol(sym, tf, args.window)
        if df is None:
            continue
        spec = resolve(sym)
        pip = float(spec.pip_size or 0.0001)
        got = symbol_series(sym, df, cands, tp_grid, args.slippage_pips * pip)
        if got is None:
            continue
        series: Dict[str, np.ndarray] = got["series"]  # type: ignore[assignment]
        spans: Dict[str, List[Dict[str, object]]] = got["spans"]  # type: ignore[assignment]

        # Trial Sharpes: the dispersion is the input the deflation actually needs.
        trial_sr = {}
        for key, r in series.items():
            st = sharpe_stats(r, periods_per_year=BARS_PER_YEAR_H1 if tf == "H1" else None)
            trial_sr[key] = st["sr"]
        all_trial_sr.extend(trial_sr.values())
        var_trial = float(np.var(list(trial_sr.values()), ddof=1)) if len(trial_sr) > 1 else 0.0

        best_key = max(trial_sr, key=lambda k: trial_sr[k])
        n_trials_sym = len(trial_sr)

        def _row(key: str) -> Dict[str, float]:
            r = series[key]
            st = sharpe_stats(r, periods_per_year=BARS_PER_YEAR_H1 if tf == "H1" else None)
            psr = probabilistic_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"])
            dsr = deflated_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"],
                                        n_trials_sym, var_trial)
            # Overlapping trades are not independent evidence, so the same test on
            # the *effective* sample size is the stricter of the two.
            n_eff = SampleUniquenessWeightEngine.effective_sample_size(spans[key])
            dsr_eff = deflated_sharpe_ratio(st["sr"], int(round(n_eff)), st["skew"], st["kurt"],
                                            n_trials_sym, var_trial)
            return {"n": st["n"], "n_eff": n_eff, "sr": st["sr"], "sr_ann": st["sr_annual"],
                    "skew": st["skew"], "kurt": st["kurt"], "psr": psr,
                    "dsr": dsr, "dsr_eff": dsr_eff}

        best = _row(best_key)
        prod = _row(prod_key) if prod_key in series else None

        rows.append({
            "symbol": sym, "trials": n_trials_sym,
            "var_of_trial_sharpes": round(var_trial, 8),
            "expected_max_sharpe": round(expected_max_sharpe(var_trial, n_trials_sym), 6),
            "best_tp_r": float(best_key),
            "psr": round(best["psr"], 6),
            "dsr_selected": round(best["dsr"], 6),
            "dsr_selected_effective_n": round(best["dsr_eff"], 6),
            "dsr_production": round(prod["dsr"], 6) if prod else None,
            "dsr_production_effective_n": round(prod["dsr_eff"], 6) if prod else None,
            "n_trades": int(best["n"]),
            "n_effective": round(best["n_eff"], 1),
            "uniqueness_ratio": round(best["n_eff"] / best["n"], 4) if best["n"] else None,
            "sr_per_obs": round(best["sr"], 6),
            "sr_annual": round(best["sr_ann"], 4),
            "skew": round(best["skew"], 4), "kurt": round(best["kurt"], 4),
            "min_track_record_len": (
                None if not np.isfinite(min_track_record_length(
                    best["sr"], best["skew"], best["kurt"],
                    benchmark_sr=expected_max_sharpe(var_trial, n_trials_sym)))
                else round(min_track_record_length(
                    best["sr"], best["skew"], best["kurt"],
                    benchmark_sr=expected_max_sharpe(var_trial, n_trials_sym)), 1)),
            "passes_dsr_095": bool(best["dsr"] > ACCEPT_DSR),
            "passes_dsr_095_effective_n": bool(best["dsr_eff"] > ACCEPT_DSR),
        })
        pooled.append(series[prod_key] if prod_key in series else series[best_key])
        if prod_key in spans:
            pooled_spans.extend(spans[prod_key])

        flag = "PASS" if best["dsr_eff"] > ACCEPT_DSR else "fail"
        print(f"{sym:8s} {best['n']:6d} {best['n_eff']:8.0f} {best['sr']:+7.4f} "
              f"{best['sr_ann']:+7.3f} {best['skew']:+6.2f} {best['kurt']:6.2f} {best_key:>5s} "
              f"{best['psr']:6.3f} {best['dsr']:7.3f} {best['dsr_eff']:7.3f}  {flag}")

    # ── portfolio ──────────────────────────────────────────────────────────
    book = np.concatenate(pooled) if pooled else np.array([])
    n_trials_book = len(tp_grid) * max(len(rows), 1)
    var_book = float(np.var(all_trial_sr, ddof=1)) if len(all_trial_sr) > 1 else 0.0
    st = sharpe_stats(book, periods_per_year=BARS_PER_YEAR_H1 if tf == "H1" else None)
    sr0 = expected_max_sharpe(var_book, n_trials_book)
    book_psr = probabilistic_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"])
    book_dsr = deflated_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"],
                                     n_trials_book, var_book)
    book_n_eff = SampleUniquenessWeightEngine.effective_sample_size(pooled_spans)
    book_dsr_eff = deflated_sharpe_ratio(st["sr"], int(round(book_n_eff)),
                                         st["skew"], st["kurt"], n_trials_book, var_book)

    print(f"\n{'BOOK':8s} {st['n']:6d} {book_n_eff:8.0f} {st['sr']:+7.4f} "
          f"{st['sr_annual']:+7.3f} {st['skew']:+6.2f} {st['kurt']:6.2f} {'—':>5s} "
          f"{book_psr:6.3f} {book_dsr:7.3f} {book_dsr_eff:7.3f}  "
          f"{'PASS' if book_dsr_eff > ACCEPT_DSR else 'fail'}")
    print(f"  portfolio deflation: n_trials={n_trials_book}  "
          f"var(trial SR)={var_book:.6f}  E[max SR]={sr0:+.6f}")
    print(f"  uniqueness: {st['n']} rows are worth {book_n_eff:.0f} independent bets "
          f"({book_n_eff / st['n']:.1%})")

    payload = {
        "generated": pd.Timestamp.now("UTC").isoformat(),
        "timeframe": tf, "window_days": args.window,
        "tp_grid": tp_grid, "production_tp_r": PRODUCTION_TP,
        "acceptance_dsr": ACCEPT_DSR,
        "cost_basis": f"slippage {args.slippage_pips} pip, commission 0 "
                      f"(matches audit_trade_quality)",
        "n_symbols": len(rows),
        "n_pass_dsr_selected": sum(1 for r in rows if r["passes_dsr_095"]),
        "n_pass_dsr_selected_effective_n": sum(
            1 for r in rows if r["passes_dsr_095_effective_n"]),
        "n_pass_dsr_production": sum(
            1 for r in rows if (r["dsr_production"] or 0.0) > ACCEPT_DSR),
        "portfolio": {
            "n": int(st["n"]), "n_effective": round(book_n_eff, 1),
            "uniqueness_ratio": round(book_n_eff / st["n"], 4) if st["n"] else None,
            "sr_per_obs": round(st["sr"], 6),
            "sr_annual": round(st["sr_annual"], 4),
            "skew": round(st["skew"], 4), "kurt": round(st["kurt"], 4),
            "n_trials": n_trials_book,
            "var_of_trial_sharpes": round(var_book, 8),
            "expected_max_sharpe": round(sr0, 6),
            "psr": round(book_psr, 6), "dsr": round(book_dsr, 6),
            "dsr_effective_n": round(book_dsr_eff, 6),
            "passes_dsr_095": bool(book_dsr > ACCEPT_DSR),
            "passes_dsr_095_effective_n": bool(book_dsr_eff > ACCEPT_DSR),
        },
        "symbols": rows,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    print(f"\n{n_pass(rows)}/{len(rows)} symbols pass DSR > {ACCEPT_DSR} at their selected tp_r "
          f"({payload['n_pass_dsr_selected_effective_n']}/{len(rows)} on effective n); "
          f"{payload['n_pass_dsr_production']}/{len(rows)} pass at the production tp_r "
          f"{PRODUCTION_TP}.")
    print(f"written -> {args.out}")
    return 0


def n_pass(rows: List[Dict[str, object]]) -> int:
    return sum(1 for r in rows if r.get("passes_dsr_095"))


if __name__ == "__main__":
    raise SystemExit(main())
