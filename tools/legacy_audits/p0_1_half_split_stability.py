"""Window-stability check for the direction audit on M15 / M5.

No 95d market data exists for these timeframes (only 183d), so a second
independent window cannot be scanned without re-downloading from MT5. Instead
split the 183d window into two DISJOINT halves and re-run the exact same
measurement on each. That is a stricter test than the H1 365-vs-183 comparison,
which used nested/overlapping windows.

Reuses the audit tool's own functions so the methodology is identical.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from jarvis.backtesting.trade_simulator import Geometry          # noqa: E402
from jarvis.data.symbol_registry import resolve                  # noqa: E402
from tools.audit_trade_quality import dynamic_regimes, load_symbol, replay  # noqa: E402
from tools.p0_1_direction_audit import forced_long_benchmark     # noqa: E402

TP_R = 1.5
SLIP_PIPS = 0.5


def measure(sym: str, df: pd.DataFrame, cands: pd.DataFrame, geom: Geometry, slip: float):
    tr = replay(sym, df, cands, geom, slip, 0.0)
    if tr is None or tr.empty:
        return None
    gate_r = float(tr["pnl_r"].to_numpy(float).mean())
    long_r = forced_long_benchmark(sym, df, cands, geom, slip)
    long_mean = float(long_r.mean()) if long_r is not None and len(long_r) else float("nan")
    return {
        "n": int(len(tr)),
        "gate_mean_r": gate_r,
        "always_long_mean_r": long_mean,
        "edge_over_always_long": gate_r - long_mean if np.isfinite(long_mean) else float("nan"),
    }


def halves(df: pd.DataFrame, cands: pd.DataFrame):
    """Yield (label, sub_df, sub_cands) for two disjoint contiguous halves.

    bar_idx is re-based onto the half so alignment is preserved — this is the
    whole point: candidates must index the frame they are replayed against.
    """
    n = len(df)
    mid = n // 2
    for label, start, end in (("H1", 0, mid), ("H2", mid, n)):
        sub_df = df.iloc[start:end].reset_index(drop=True)
        keep = cands[(cands["bar_idx"] >= start) & (cands["bar_idx"] < end - 1)].copy()
        keep["bar_idx"] = keep["bar_idx"] - start
        yield label, sub_df, keep.reset_index(drop=True)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tf", default="M15,M5", help="comma-separated timeframes")
    ap.add_argument("--window", type=int, default=183, help="candidate-cache window in days")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tfs = [t.strip().upper() for t in args.tf.split(",") if t.strip()]
    win = args.window

    real_dir = os.path.join(REPO, "data", "market", "real")
    symbols = sorted(d for d in os.listdir(real_dir) if os.path.isdir(os.path.join(real_dir, d)))
    out: dict = {"tp_r": TP_R, "slip_pips": SLIP_PIPS,
                 "split": f"{win}d -> two disjoint halves"}

    for tf in tfs:
        print("=" * 92, flush=True)
        print(f"{tf}  {win}d split into two disjoint halves   tp={TP_R}R", flush=True)
        print("=" * 92, flush=True)
        per: dict = {}
        for sym in symbols:
            df, cands = load_symbol(sym, tf, win)
            if df is None or cands is None or cands.empty:
                continue
            cands = dynamic_regimes(df, cands)
            pip = float(resolve(sym).pip_size or 0.0001)
            geom = Geometry(tp_r=TP_R)
            slip = SLIP_PIPS * pip

            row = {"full": measure(sym, df, cands, geom, slip)}
            for label, sub_df, sub_c in halves(df, cands):
                row[label] = measure(sym, sub_df, sub_c, geom, slip)
            per[sym] = row
            f, a, b = row["full"], row["H1"], row["H2"]
            if not f or not a or not b:
                print(f"{sym:<8} skipped (empty half)", flush=True)
                continue
            print(f"{sym:<8} full n={f['n']:>6} edge={f['edge_over_always_long']:+.4f} | "
                  f"H1 n={a['n']:>6} edge={a['edge_over_always_long']:+.4f} | "
                  f"H2 n={b['n']:>6} edge={b['edge_over_always_long']:+.4f}", flush=True)

        # Stability: does the per-symbol verdict survive across the two halves?
        flips_edge = flips_prof = 0
        considered = 0
        for sym, row in per.items():
            a, b = row.get("H1"), row.get("H2")
            if not a or not b:
                continue
            considered += 1
            if (a["edge_over_always_long"] > 0) != (b["edge_over_always_long"] > 0):
                flips_edge += 1
            if (a["gate_mean_r"] > 0) != (b["gate_mean_r"] > 0):
                flips_prof += 1

        print("-" * 92, flush=True)
        print(f"{tf}: symbols compared = {considered}", flush=True)
        print(f"{tf}: 'beats always-long' verdict FLIPS between halves = {flips_edge}/{considered}",
              flush=True)
        print(f"{tf}: 'gate profitable'  verdict FLIPS between halves = {flips_prof}/{considered}",
              flush=True)
        out[tf] = {"symbols": per, "flips_edge": flips_edge,
                   "flips_profitable": flips_prof, "considered": considered}

    path = args.out or os.path.join(
        REPO, "reports", f"p0_1_half_split_stability_{'_'.join(tfs)}_{win}d.json"
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(f"\nwrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
