"""
Can the entry score be used for SIZING, given it fails as an entry filter?

THE HYPOTHESIS
--------------
The attribution scan found that `score` weakly but *monotonically* orders
outcomes -- the bucket ladder runs Q1 -0.1016R -> Q5 -0.0608R -- while every
bucket is negative. That pattern says the score ranks **how badly a trade loses**,
not **how likely it is to win**. If so, the same information is useless as a gate
but potentially useful as a position-size input: scale up what the score likes,
scale down what it dislikes, and the risk-weighted expectancy rises even though
nothing about which trades are taken has changed.

WHY THIS IS NOT JUST LEVERAGE
-----------------------------
Weights are normalised **within each symbol** so that the total risk budget is
unchanged (`sum(w) == n`). Only the *distribution* of that budget across trades
changes. A variant that simply took more risk would show up as a higher mean with
no need for the test; normalising removes that explanation.

THE TEST THAT MATTERS
---------------------
A weighted mean is guaranteed to beat an equal-weighted one in-sample if the
weights were chosen by looking at the outcomes. They were not chosen that way --
they come from the score, an ex-ante feature -- but a weak correlation plus a
large sample can still produce an improvement by chance. So the decisive
statistic is a **within-symbol permutation test**: shuffle the weight vector
inside each symbol (which preserves both the weight distribution and the per-
symbol risk budget) and rebuild the null distribution of the improvement. If the
observed improvement sits inside that null, the score adds nothing.

Note the honest ceiling: down-weighting losers can lift a negative mean towards
zero but cannot make a signal that has no edge profitable. The question is how
far it moves, and whether it is distinguishable from noise.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.config.runtime import offline_mode  # noqa: E402
from jarvis.intelligence.winrate_targeting import WRProfileStore  # noqa: E402
from jarvis.learning.sample_weights import SampleUniquenessWeightEngine  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tools"))
from entry_edge_diagnostic import (  # noqa: E402
    PROFILE_PATH,
    build_frame,
    discover,
)


def non_overlapping(frame: pd.DataFrame) -> pd.DataFrame:
    """Mirror ``select_sequential``: one position at a time, per symbol.

    Sizing only means anything on trades the system would actually hold, so the
    overlapping candidates have to go first. Same rule as the calibration: walk
    in entry order and block until the previous trade's exit bar.
    """
    keep = []
    for _, g in frame.groupby("symbol"):
        blocked_until = -1
        for idx, row in g.sort_values("entry_idx").iterrows():
            if row["entry_idx"] <= blocked_until:
                continue
            keep.append(idx)
            blocked_until = int(row["exit_idx"])
    return frame.loc[keep].reset_index(drop=True)


def weight_variants(g: pd.DataFrame) -> dict[str, np.ndarray]:
    """Sizing weight vectors for one symbol, all summing to ``n``."""
    n = len(g)
    score = pd.to_numeric(g["score"], errors="coerce").to_numpy(dtype=float)
    # Within-symbol percentile so the same weight rule means the same thing on
    # every instrument regardless of the score's scale there.
    pct = pd.Series(score).rank(method="average", pct=True).to_numpy(dtype=float)
    pct = np.clip(pct, 1e-6, 1.0)

    def norm(w: np.ndarray) -> np.ndarray:
        w = np.clip(np.asarray(w, dtype=float), 0.0, None)
        s = w.sum()
        return w * (n / s) if s > 0 else np.ones(n)

    return {
        "equal": np.ones(n),
        "linear_pct": norm(pct),
        "linear_pct_min25": norm(0.25 + 0.75 * pct),
        "top_heavy_2x": norm(np.where(pct >= 0.5, 1.5, 0.5)),
        "top_half_only": norm((pct >= 0.5).astype(float)),
        # Control: an inverted rule must be WORSE if the gradient is real. If it
        # is not, the "effect" is symmetric noise and neither direction is edge.
        "inverse_control": norm(1.0 - pct),
    }


def aggregate(frames: dict[str, pd.DataFrame], variant: str) -> tuple[float, list[tuple[str, float, float]]]:
    """Risk-weighted aggregate expectancy under one sizing rule.

    Returns the pooled weighted mean and the per-symbol (equal, weighted) pair.
    """
    num = den = 0.0
    per_sym: list[tuple[str, float, float]] = []
    for sym, g in frames.items():
        w = weight_variants(g)[variant]
        r = g["pnl_r"].to_numpy(dtype=float)
        wm = float(np.sum(w * r) / np.sum(w))
        em = float(r.mean())
        num += float(np.sum(w * r))
        den += float(np.sum(w))
        per_sym.append((sym, em, wm))
    return (num / den if den else 0.0), per_sym


def permutation_p(frames: dict[str, pd.DataFrame], variant: str,
                  observed: float, n_perm: int, rng: np.random.RandomState) -> tuple[float, float]:
    """Null distribution of the improvement from shuffling weights per symbol.

    Shuffling inside each symbol preserves the weight vector exactly, so the null
    is precisely "these weights, assigned to trades at random".
    """
    base, _ = aggregate(frames, "equal")
    obs_gain = observed - base
    gains = np.empty(n_perm, dtype=float)
    for i in range(n_perm):
        num = den = 0.0
        for sym, g in frames.items():
            w = weight_variants(g)[variant]
            w = w[rng.permutation(len(w))]
            r = g["pnl_r"].to_numpy(dtype=float)
            num += float(np.sum(w * r))
            den += float(np.sum(w))
        gains[i] = (num / den if den else 0.0) - base
    p = float((gains >= obs_gain).mean())
    return p, float(gains.std())


def uniqueness_weighted(frames: dict[str, pd.DataFrame], variant: str) -> float:
    """Pooled expectancy with BOTH sizing and overlap down-weighting applied."""
    num = den = 0.0
    for sym, g in frames.items():
        w = weight_variants(g)[variant]
        enriched = [
            {"pnl": float(r), "duration_bars": max(1, int(d))}
            for r, d in zip(g["pnl_r"], g["bars_held"])
        ]
        try:
            u = SampleUniquenessWeightEngine.get_sample_weights(enriched)
            u = np.asarray(u, dtype=float)
        except Exception:
            u = np.ones(len(g))
        if len(u) != len(w) or u.sum() <= 0:
            u = np.ones(len(w))
        cw = w * u
        r = g["pnl_r"].to_numpy(dtype=float)
        num += float(np.sum(cw * r))
        den += float(np.sum(cw))
    return num / den if den else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--slippage-pips", type=float, default=0.5)
    ap.add_argument("--permutations", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else discover()
    )
    store = WRProfileStore(PROFILE_PATH)
    profiles = store.load()

    print("=" * 100)
    print("CAN THE SCORE BE USED FOR SIZING?")
    print("=" * 100)

    frames: dict[str, pd.DataFrame] = {}
    t0 = time.time()
    with offline_mode():
        for sym in symbols:
            prof = profiles.get(sym)
            if prof is None:
                continue
            f = build_frame(sym, prof, slippage_pips=args.slippage_pips)
            if f.empty:
                continue
            f = f[f["traded"]].copy()
            if len(f) < 20:
                continue
            frames[sym] = non_overlapping(f)

    total = sum(len(g) for g in frames.values())
    print(f"{len(frames)} symbols | {total:,} non-overlapping traded positions "
          f"| slippage {args.slippage_pips} pips ({time.time()-t0:.0f}s)")
    print()

    base, _ = aggregate(frames, "equal")
    rng = np.random.RandomState(args.seed)

    print(f"{'sizing rule':<18} {'wtd exp (R)':>12} {'gain vs equal':>14} "
          f"{'p':>8} {'null sd':>9} {'uniq-wtd':>10} {'symbols up':>11}")
    print("-" * 100)
    rows = []
    for variant in ("equal", "linear_pct", "linear_pct_min25", "top_heavy_2x",
                    "top_half_only", "inverse_control"):
        val, per_sym = aggregate(frames, variant)
        gain = val - base
        if variant == "equal":
            p, sd = float("nan"), float("nan")
        else:
            p, sd = permutation_p(frames, variant, val, args.permutations, rng)
        up = sum(1 for _, e, w in per_sym if w > e)
        uw = uniqueness_weighted(frames, variant)
        rows.append({
            "variant": variant, "weighted_expectancy_r": round(val, 5),
            "gain_vs_equal_r": round(gain, 5), "p_value": None if np.isnan(p) else round(p, 4),
            "null_sd": None if np.isnan(sd) else round(sd, 5),
            "uniqueness_weighted_r": round(uw, 5),
            "symbols_improved": up, "symbols_total": len(per_sym),
        })
        print(f"{variant:<18} {val:>+12.5f} {gain:>+14.5f} "
              f"{'--' if np.isnan(p) else f'{p:>8.4f}'} "
              f"{'--' if np.isnan(sd) else f'{sd:>9.5f}'} {uw:>+10.5f} "
              f"{up:>5}/{len(per_sym):<5}")

    print()
    print(f"equal-weighted baseline expectancy: {base:+.5f} R/trade")
    best = max((r for r in rows if r["variant"] != "equal"),
               key=lambda r: r["weighted_expectancy_r"])
    print(f"best sizing rule: {best['variant']} at {best['weighted_expectancy_r']:+.5f} R "
          f"({best['gain_vs_equal_r']:+.5f} R, p={best['p_value']})")
    inv = next(r for r in rows if r["variant"] == "inverse_control")
    print(f"inverse control : {inv['weighted_expectancy_r']:+.5f} R "
          f"({inv['gain_vs_equal_r']:+.5f} R, p={inv['p_value']})")
    print()
    print("Read: the inverse control must be clearly WORSE for the gradient to be")
    print("directional. If both directions improve, the 'effect' is symmetric noise.")

    if args.json:
        out = Path(REPO_ROOT) / "reports" / "score_sizing_test.json"
        out.write_text(json.dumps({
            "symbols": len(frames), "positions": total,
            "slippage_pips": args.slippage_pips, "permutations": args.permutations,
            "equal_weighted_baseline_r": round(base, 5),
            "results": rows,
        }, indent=2), encoding="utf-8")
        print(f"\nJSON: {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
