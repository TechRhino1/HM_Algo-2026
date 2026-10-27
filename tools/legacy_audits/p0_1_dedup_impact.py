"""Read-side dedup guard — how much of the measured edge is duplicate overlap?

Backlog item "read-side dedup guard", pending since 2026-09-11, where applying
one-position-at-a-time filtering flipped XAUUSD +0.157R -> -0.034R and US30
+0.132R -> -0.145R. The audit tools (`p0_1_direction_audit`, `deflated_sharpe_report`,
`p0_3_exit_geometry_measurement`) replay EVERY candidate and apply no such
filter, so their absolute counts are inflated.

This quantifies the damage instead of assuming it: same simulation, same costs,
only the sequential one-position-at-a-time walk is toggled.

    python tools/p0_1_dedup_impact.py --tf M5 --window 183
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from jarvis.backtesting.trade_simulator import Geometry, evaluate_geometry   # noqa: E402
from jarvis.data.symbol_registry import get_dollar_risk_per_price_unit, resolve  # noqa: E402
from tools.audit_trade_quality import dynamic_regimes, load_symbol           # noqa: E402

TP_R = 1.5
SLIP_PIPS = 0.5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tf", default="M5")
    ap.add_argument("--window", type=int, default=183)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tf = args.tf.strip().upper()
    win = args.window
    real_dir = os.path.join(REPO, "data", "market", "real")
    symbols = sorted(d for d in os.listdir(real_dir) if os.path.isdir(os.path.join(real_dir, d)))

    print(f"{tf} {win}d — all candidates vs one-position-at-a-time  (tp={TP_R}R)", flush=True)
    print(f"{'symbol':8s} {'n_all':>8s} {'n_dedup':>8s} {'kept%':>7s} "
          f"{'R_all':>9s} {'R_dedup':>9s} {'delta':>9s}  sign", flush=True)
    print("-" * 78, flush=True)

    rows = {}
    for sym in symbols:
        df, cands = load_symbol(sym, tf, win)
        if df is None or cands is None or cands.empty:
            continue
        cands = dynamic_regimes(df, cands)
        pip = float(resolve(sym).pip_size or 0.0001)
        geom = Geometry(tp_r=TP_R)
        slip = SLIP_PIPS * pip
        money = get_dollar_risk_per_price_unit(sym, None)

        common = dict(df=df, candidates=cands, geom=geom, money_per_unit=money,
                      cost_price_equiv=0.0, slippage_price_equiv=slip,
                      spec=resolve(sym), symbol=sym)
        allout = evaluate_geometry(one_position_at_a_time=False, **common)
        dedup = evaluate_geometry(one_position_at_a_time=True, **common)

        ra = np.array([o.pnl_r for o in allout], float)
        rd = np.array([o.pnl_r for o in dedup], float)
        ma = float(ra.mean()) if len(ra) else float("nan")
        md = float(rd.mean()) if len(rd) else float("nan")
        kept = (len(rd) / len(ra) * 100) if len(ra) else float("nan")
        sign = "" if np.sign(ma) == np.sign(md) else "  <-- FLIPS"
        if (ma > 0) != (md > 0):
            sign = "  <-- FLIPS"

        rows[sym] = {"n_all": int(len(ra)), "n_dedup": int(len(rd)), "kept_pct": kept,
                     "mean_r_all": ma, "mean_r_dedup": md, "delta": md - ma}
        print(f"{sym:8s} {len(ra):8d} {len(rd):8d} {kept:6.1f}% "
              f"{ma:+9.4f} {md:+9.4f} {md-ma:+9.4f}{sign}", flush=True)

    tot_all = sum(r["n_all"] for r in rows.values())
    tot_ded = sum(r["n_dedup"] for r in rows.values())
    flips = sum(1 for r in rows.values() if (r["mean_r_all"] > 0) != (r["mean_r_dedup"] > 0))
    pos_all = sum(1 for r in rows.values() if r["mean_r_all"] > 0)
    pos_ded = sum(1 for r in rows.values() if r["mean_r_dedup"] > 0)
    wavg_all = sum(r["mean_r_all"] * r["n_all"] for r in rows.values()) / max(1, tot_all)
    wavg_ded = sum(r["mean_r_dedup"] * r["n_dedup"] for r in rows.values()) / max(1, tot_ded)

    print("-" * 78, flush=True)
    print(f"trades: {tot_all:,} all -> {tot_ded:,} deduped "
          f"({tot_ded/max(1,tot_all)*100:.1f}% kept)", flush=True)
    print(f"pooled mean R: {wavg_all:+.4f} all -> {wavg_ded:+.4f} deduped "
          f"({wavg_ded-wavg_all:+.4f})", flush=True)
    print(f"profitable symbols: {pos_all}/20 all -> {pos_ded}/20 deduped; "
          f"sign FLIPS = {flips}/20", flush=True)

    out = {"tf": tf, "window_days": win, "tp_r": TP_R, "symbols": rows,
           "n_all": tot_all, "n_dedup": tot_ded,
           "pooled_mean_r_all": wavg_all, "pooled_mean_r_dedup": wavg_ded,
           "profitable_all": pos_all, "profitable_dedup": pos_ded, "sign_flips": flips}
    path = args.out or os.path.join(REPO, "reports", f"p0_1_dedup_impact_{tf}_{win}d.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(f"\nwrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
