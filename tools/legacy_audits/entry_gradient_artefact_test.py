"""Follow-up: decompose the two strongest gradients and inspect the odd ones.

Four questions the headline table raised:

  A. ``risk_dist`` and ``atr`` showed the largest rank correlations (+0.20) and
     both collapsed to ~+0.04 at zero slippage. Is that collapse exactly the
     mechanical cost term ``-slip / risk_dist``?
  B. Their Q1->Q5 SPREAD barely changed while rho collapsed. If the effect were
     a clean monotone cost gradient both should move together. What is the
     bucket shape actually doing?
  C. ``meta_label_prob`` reported rho = nan with a large NEGATIVE spread. A
     probability the model itself produces should not be anti-predictive; nan
     suggests it is degenerate.
  D. ``regime`` and ``strategy`` showed by far the largest spreads (+0.50R,
     +0.43R). Are those consistent across symbols, or carried by one?
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.config.runtime import offline_mode  # noqa: E402
from jarvis.intelligence.winrate_targeting import WRProfileStore  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tools"))
from entry_edge_diagnostic import (  # noqa: E402
    PROFILE_PATH,
    build_frame,
    discover,
    quintile_column,
)

PROFILE_PATH = Path(PROFILE_PATH)


def bucket_table(frame, feature, q=5):
    work = frame[[feature, "pnl_r", "symbol"]].copy()
    work[feature] = pd.to_numeric(work[feature], errors="coerce")
    work = work.dropna(subset=[feature, "pnl_r"])
    work["bucket"] = quintile_column(frame.loc[work.index], feature, q)
    work = work.dropna(subset=["bucket"])
    rows = []
    for b in sorted(work["bucket"].unique()):
        g = work[work["bucket"] == b]
        rows.append((int(b), len(g), g["pnl_r"].mean(), (g["pnl_r"] > 0).mean()))
    return work, rows


def main() -> int:
    store = WRProfileStore(PROFILE_PATH)
    profiles = store.load()

    frames = {}
    for slip in (0.5, 0.0):
        fs = []
        with offline_mode():
            for sym in discover():
                p = profiles.get(sym)
                if p is None:
                    continue
                f = build_frame(sym, p, slippage_pips=slip)
                if not f.empty:
                    fs.append(f)
        frames[slip] = pd.concat(fs, ignore_index=True)
        print(f"built slippage={slip}: {len(frames[slip]):,} outcomes")

    # ── A. Is the collapse exactly the mechanical cost term? ────────────────
    print("\n" + "=" * 92)
    print("A. Is the risk_dist gradient the mechanical cost term -slip/risk_dist?")
    print("=" * 92)
    a, b = frames[0.5], frames[0.0]
    m = a[["symbol", "risk_dist", "pnl_r", "bars_held", "exit_reason"]].copy()
    m["pnl_r_no_slip"] = b["pnl_r"].to_numpy()
    m["delta"] = m["pnl_r"] - m["pnl_r_no_slip"]
    # Slippage is only charged on protective-stop exits.
    m["is_stop"] = m["exit_reason"].isin(["SL", "BE/TRAIL_SL"])
    print(f"trades: {len(m):,}  stop exits: {int(m['is_stop'].sum()):,} "
          f"({m['is_stop'].mean():.1%})")
    print(f"total R lost to slippage: {m['delta'].sum():+.2f} R "
          f"({m['delta'].mean():+.5f} R/trade)")
    print(f"corr(delta, risk_dist)   : {m['delta'].corr(m['risk_dist'], method='spearman'):+.4f}")
    print(f"corr(delta, 1/risk_dist) : {m['delta'].corr(1.0/m['risk_dist'], method='spearman'):+.4f}")
    print("\nA purely mechanical charge would be delta = -slip/risk_dist on stop exits and 0")
    print("elsewhere. Predicting that and checking the residual:")
    # pip size differs per symbol; recover slip from the data instead of assuming.
    # delta = -slip/risk_dist on stops  =>  slip = -delta * risk_dist
    est = (-m.loc[m["is_stop"], "delta"] * m.loc[m["is_stop"], "risk_dist"])
    print("  implied slip per symbol (median, price units):")
    for sym, g in est.groupby(m.loc[m["is_stop"], "symbol"]):
        print(f"    {sym:8s} median={g.median():.8f}  iqr=({g.quantile(.25):.8f},{g.quantile(.75):.8f})")

    # ── B. Bucket shape with and without slippage ──────────────────────────
    print("\n" + "=" * 92)
    print("B. Bucket shape, with vs without slippage")
    print("=" * 92)
    for feat in ("risk_dist", "atr", "ev", "score"):
        print(f"\n{feat}:")
        print(f"  {'bucket':>7} | {'n':>6} {'slip=0.5':>10} {'WR':>6} | "
              f"{'n':>6} {'slip=0':>10} {'WR':>6}")
        _, r1 = bucket_table(frames[0.5], feat)
        _, r2 = bucket_table(frames[0.0], feat)
        for (bb, n1, e1, w1), (_, n2, e2, w2) in zip(r1, r2):
            print(f"  Q{bb+1:<6} | {n1:>6,} {e1:>+10.4f} {w1*100:>5.1f}% | "
                  f"{n2:>6,} {e2:>+10.4f} {w2*100:>5.1f}%")

    # ── C. meta_label_prob degeneracy ───────────────────────────────────────
    print("\n" + "=" * 92)
    print("C. meta_label_prob")
    print("=" * 92)
    f = frames[0.5]
    ml = pd.to_numeric(f["meta_label_prob"], errors="coerce")
    print(f"dtype={f['meta_label_prob'].dtype}  n={len(ml)}  "
          f"nan={ml.isna().sum():,}  nunique={ml.nunique()}")
    print(f"describe: {dict(ml.describe()[['min','25%','50%','75%','max']].round(6))}")
    print(f"corr with pnl_r (pearson): {ml.corr(f['pnl_r']):+.5f}")
    # Is it constant within a symbol?
    per = f.assign(ml=ml).groupby("symbol")["ml"].nunique()
    print(f"distinct meta_label_prob values per symbol: {dict(per)}")

    # ── D. Are regime / strategy gradients carried by one symbol? ───────────
    print("\n" + "=" * 92)
    print("D. regime / strategy consistency across symbols")
    print("=" * 92)
    for feat in ("regime", "strategy"):
        print(f"\n{feat}: pooled top-vs-bottom spread per symbol")
        per_sym = []
        for sym, g in f.groupby("symbol"):
            means = g.groupby(feat)["pnl_r"].agg(["mean", "size"])
            means = means[means["size"] >= 10]
            if len(means) < 2:
                continue
            best = means["mean"].idxmax()
            worst = means["mean"].idxmin()
            per_sym.append((sym, best, means.loc[best, "mean"], int(means.loc[best, "size"]),
                            worst, means.loc[worst, "mean"], int(means.loc[worst, "size"])))
        spread_sign = sum(1 for r in per_sym if r[2] > r[5])
        for r in per_sym:
            print(f"  {r[0]:8s} best={r[1]:<28}({r[2]:+.4f}R,n={r[3]:4d}) "
                  f"worst={r[4]:<28}({r[5]:+.4f}R,n={r[6]:4d})")
        print(f"  -> {spread_sign}/{len(per_sym)} symbols have best>worst (trivially true by construction)")

        # The real question: is the SAME category best across symbols?
        bests = pd.Series([r[1] for r in per_sym]).value_counts()
        print(f"  most common 'best' category: {dict(bests.head(4))}")
        # And is any category reliably positive?
        print("  pooled expectancy per category (n>=100):")
        agg = f.groupby(feat)["pnl_r"].agg(["mean", "size"])
        for k, row in agg[agg["size"] >= 100].sort_values("mean", ascending=False).iterrows():
            print(f"    {k:<30} {row['mean']:+.4f}R  n={int(row['size']):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
