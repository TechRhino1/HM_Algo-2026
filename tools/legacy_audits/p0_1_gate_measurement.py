"""P0-1 — does the entry gate have ANY measurable edge?

Measured on real MT5 bars (365d H1, 20 symbols, provenance `MT5_TERMINAL_REAL`,
0 quality issues), replayed through the official `trade_simulator` under the
honest cost model for this account: the spread is already inside the candidate's
`fill` (the scanner adjusts the next-bar open), plus 0.5 pip slippage on stop
exits, plus zero commission (XM standard accounts are spread-only, so the
existing `comm_zero` is correct rather than lazy).

WHY THIS TOOL EXISTS
--------------------
The audit's acceptance bar is `PF >= 1.3`. That number is arbitrary, and it
answers the wrong question. The question that decides the gate is:

    is mean R per trade distinguishable from zero, after costs?

Three statistical traps have to be handled or the answer is worthless:

1. **Trades within a symbol are not independent.** They overlap in time, share a
   regime, and share a symbol's cost structure. A pooled t-test over ~95k trades
   treats them as independent draws and manufactures significance out of
   nothing. The honest aggregate treats each **symbol as a cluster** (n=20) and
   tests the symbol-level means.
2. **20 symbols is 20 hypotheses.** At alpha=0.05 you expect ~1 false positive
   even with no edge anywhere. So the best symbol must clear the distribution of
   the *maximum* of 20 null draws, not the 5% point of a single one.
3. **A symbol can look good because it was picked after the fact.** The bar is
   the max-statistic, not the per-symbol bar.

Usage
-----
    python tools/p0_1_gate_measurement.py
    python tools/p0_1_gate_measurement.py --tp 2.0 --out reports/p0_1.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Dict, List

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from jarvis.backtesting.trade_simulator import Geometry  # noqa: E402
from jarvis.data.symbol_registry import resolve  # noqa: E402
from tools.audit_trade_quality import (  # noqa: E402
    breakeven_wr,
    dynamic_regimes,
    load_symbol,
    replay,
)

BOOTSTRAP_N = 20000
MAXSTAT_SIMS = 200000
SEED = 20260917


def bootstrap_mean_ci(x: np.ndarray, n: int = BOOTSTRAP_N, seed: int = SEED) -> tuple:
    """Percentile bootstrap CI for the mean. Seeded: the answer must not move."""
    if len(x) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    draws = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return (float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5)))


def two_sided_p(t: float, df: int) -> float:
    """Two-sided Student-t p-value, no scipy.

    p = I_x(df/2, 1/2) with x = df/(df + t^2). The continued fraction converges
    fastest for x < (a+1)/(a+b+2); past that, use the symmetry
    I_x(a,b) = 1 - I_{1-x}(b,a).
    """
    if df <= 0 or not math.isfinite(t):
        return float("nan")
    x = df / (df + t * t)
    a, b = df / 2.0, 0.5
    if x < (a + 1.0) / (a + b + 2.0):
        p = _ibeta_cont(x, a, b)
    else:
        p = 1.0 - _ibeta_cont(1.0 - x, b, a)
    return float(min(1.0, max(0.0, p)))


def _ibeta_cont(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta I_x(a,b) by the Lentz continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    f, c, d = 1.0, 1.0, 0.0
    for i in range(0, 300):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        d = 1.0 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1.0 + num / c
        c = c if abs(c) > 1e-30 else 1e-30
        f *= c * d
        if abs(1.0 - c * d) < 1e-12:
            break
    return front * (f - 1.0)


def bh_fdr(pvals: List[float], q: float = 0.05) -> List[bool]:
    """Benjamini-Hochberg. Returns a pass/fail flag per hypothesis (input order)."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    passed = [False] * m
    kmax = -1
    for rank, idx in enumerate(order, start=1):
        if pvals[idx] <= q * rank / m:
            kmax = rank
    for rank, idx in enumerate(order, start=1):
        if rank <= kmax:
            passed[idx] = True
    return passed


def null_max_abs_t(m: int, sims: int = MAXSTAT_SIMS, seed: int = SEED) -> Dict[str, float]:
    """Distribution of max|t| across m symbols when there is NO edge anywhere.

    This is the bar a 'best symbol' has to clear. Using the 5% point of a single
    normal (1.96) instead is how a backtest selects noise.
    """
    rng = np.random.default_rng(seed)
    draws = np.abs(rng.standard_normal((sims, m)))
    mx = draws.max(axis=1)
    return {
        "median": float(np.percentile(mx, 50)),
        "p90": float(np.percentile(mx, 90)),
        "p95": float(np.percentile(mx, 95)),
        "p99": float(np.percentile(mx, 99)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--window", type=int, default=365)
    ap.add_argument("--tp", type=float, default=1.5)
    ap.add_argument("--slip-pips", type=float, default=0.5)
    ap.add_argument("--out", default=os.path.join(REPO, "reports", "p0_1_gate_measurement.json"))
    args = ap.parse_args()

    real_dir = os.path.join(REPO, "data", "market", "real")
    symbols = sorted(
        d for d in os.listdir(real_dir)
        if os.path.isfile(os.path.join(real_dir, d, f"{d}_{args.tf}_{args.window}d.parquet"))
    )

    print("=" * 100)
    print(f"P0-1  entry-gate edge measurement  |  {args.tf}  |  {args.window}d  |  tp={args.tp}R  |  "
          f"slippage={args.slip_pips} pip on stops  |  0 commission")
    print("=" * 100)

    per_symbol: Dict[str, Dict[str, float]] = {}
    all_r: List[np.ndarray] = []

    for sym in symbols:
        df, cands = load_symbol(sym, args.tf, args.window)
        if df is None:
            continue
        cands = dynamic_regimes(df, cands)
        pip = float(resolve(sym).pip_size or 0.0001)
        tr = replay(sym, df, cands, Geometry(tp_r=args.tp), args.slip_pips * pip, 0.0)
        if tr is None or tr.empty:
            continue

        r = tr["pnl_r"].to_numpy(float)
        all_r.append(r)
        n = len(r)
        mean = float(r.mean())
        sd = float(r.std(ddof=1))
        t = mean / (sd / math.sqrt(n)) if sd > 1e-12 else 0.0
        wins, losses = r[r > 0], r[r <= 0]
        gp, gl = float(wins.sum()), float(-losses.sum())
        pf = gp / gl if gl > 1e-12 else (99.0 if gp > 0 else 0.0)
        lo, hi = bootstrap_mean_ci(r)
        per_symbol[sym] = {
            "n": n,
            "win_rate": float((r > 0).mean()),
            "break_even_wr": breakeven_wr(args.tp),
            "mean_r": mean,
            "sd_r": sd,
            "t_stat": t,
            "p_value": two_sided_p(t, n - 1),
            "profit_factor": pf,
            "total_r": float(r.sum()),
            "ci95_low": lo,
            "ci95_high": hi,
            "ci_excludes_zero": bool(lo > 0 or hi < 0),
        }

    names = sorted(per_symbol)
    pvals = [per_symbol[s]["p_value"] for s in names]
    fdr_pass = bh_fdr(pvals, 0.05)

    print(f"\n{'symbol':<8} {'n':>6} {'WR%':>6} {'BE%':>6} {'PF':>6} {'meanR':>9} "
          f"{'t':>7} {'p':>7} {'95% CI on mean R':>24}  {'FDR':<4} {'CI>0':<5}")
    print("-" * 100)
    for s, ok in zip(names, fdr_pass):
        d = per_symbol[s]
        ci = f"[{d['ci95_low']:+.4f}, {d['ci95_high']:+.4f}]"
        print(f"{s:<8} {d['n']:>6} {d['win_rate'] * 100:>6.2f} {d['break_even_wr'] * 100:>6.2f} "
              f"{d['profit_factor']:>6.3f} {d['mean_r']:>+9.4f} {d['t_stat']:>+7.2f} "
              f"{d['p_value']:>7.4f} {ci:>24}  {'PASS' if ok else '.':<4} "
              f"{'yes' if d['ci_excludes_zero'] else 'no':<5}")

    # ── cluster-robust aggregate: the SYMBOL is the unit of independence ────
    sym_means = np.array([per_symbol[s]["mean_r"] for s in names])
    k = len(sym_means)
    m_mean = float(sym_means.mean())
    m_sd = float(sym_means.std(ddof=1)) if k > 1 else 0.0
    m_t = m_mean / (m_sd / math.sqrt(k)) if m_sd > 1e-12 else 0.0
    m_p = two_sided_p(m_t, k - 1)
    clo, chi = bootstrap_mean_ci(sym_means)

    # Naive pooled test, reported ONLY to show how much it inflates significance.
    pooled = np.concatenate(all_r)
    p_mean = float(pooled.mean())
    p_sd = float(pooled.std(ddof=1))
    p_t = p_mean / (p_sd / math.sqrt(len(pooled))) if p_sd > 1e-12 else 0.0

    mx = null_max_abs_t(k)
    best_sym = max(names, key=lambda s: per_symbol[s]["t_stat"])
    best_t = per_symbol[best_sym]["t_stat"]

    n_pass_fdr = int(sum(fdr_pass))
    n_ci = int(sum(per_symbol[s]["ci_excludes_zero"] for s in names))
    n_pf_ge_1 = int(sum(per_symbol[s]["profit_factor"] >= 1.0 for s in names))
    n_pf_ge_13 = int(sum(per_symbol[s]["profit_factor"] >= 1.3 for s in names))

    print("\n" + "=" * 100)
    print("AGGREGATE")
    print("=" * 100)
    print(f"  trades replayed                     : {len(pooled):,} across {k} symbols")
    print(f"  mean R per trade (pooled)           : {p_mean:+.5f}   (t={p_t:+.2f} — INFLATED, trades are")
    print("                                        not independent within a symbol; shown only as a contrast)")
    print(f"  mean of per-symbol mean R           : {m_mean:+.5f}")
    print(f"  cluster-robust t (symbols as units) : {m_t:+.3f}  p={m_p:.4f}   n={k} clusters")
    print(f"  95% CI on the mean of symbol means  : [{clo:+.5f}, {chi:+.5f}]"
          f"   {'EXCLUDES 0' if (clo > 0 or chi < 0) else 'CONTAINS 0'}")
    print(f"  symbols PF >= 1.0 / >= 1.3          : {n_pf_ge_1}/{k} / {n_pf_ge_13}/{k}")
    print(f"  symbols with CI on mean R > 0       : {n_ci}/{k}")
    print(f"  symbols passing BH-FDR at q=0.05    : {n_pass_fdr}/{k}")

    print("\nMULTIPLE-TESTING BAR (no edge anywhere, {0} symbols)".format(k))
    print(f"  E[max|t|] under the null, median    : {mx['median']:.3f}")
    print(f"  90th / 95th / 99th percentile       : {mx['p90']:.3f} / {mx['p95']:.3f} / {mx['p99']:.3f}")
    print(f"  best symbol by t                    : {best_sym} (t={best_t:+.3f})")
    if best_t < mx["median"]:
        verdict = "NO EDGE — the best symbol is inside the null distribution of the maximum"
    elif best_t < mx["p95"]:
        verdict = "NO EDGE — best symbol clears the median but not the 95th percentile of the max"
    else:
        verdict = "POSSIBLE SIGNAL — best symbol clears the 95th percentile of the max"
    print(f"  VERDICT                             : {verdict}")

    report = {
        "generated": pd.Timestamp.utcnow().isoformat(),
        "timeframe": args.tf,
        "window_days": args.window,
        "tp_r": args.tp,
        "slippage_pips": args.slip_pips,
        "commission_per_lot": 0.0,
        "cost_model_note": ("Spread is already inside the candidate fill (scanner adjusts the "
                            "next-bar open); 0.5 pip slippage is applied on stop exits only; "
                            "commission is 0 because XM standard accounts are spread-only."),
        "provenance": "data/market/real/*/*_H1_365d.parquet, manifest provenance MT5_TERMINAL_REAL",
        "per_symbol": per_symbol,
        "aggregate": {
            "trades": int(len(pooled)),
            "symbols": k,
            "pooled_mean_r": p_mean,
            "pooled_t_inflated": p_t,
            "cluster_mean_r": m_mean,
            "cluster_t": m_t,
            "cluster_p": m_p,
            "cluster_ci95": [clo, chi],
            "cluster_ci_excludes_zero": bool(clo > 0 or chi < 0),
            "symbols_pf_ge_1": n_pf_ge_1,
            "symbols_pf_ge_13": n_pf_ge_13,
            "symbols_ci_excludes_zero": n_ci,
            "symbols_pass_bh_fdr": n_pass_fdr,
        },
        "null_max_abs_t": mx,
        "best_symbol": {"symbol": best_sym, "t": best_t},
        "verdict": verdict,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
