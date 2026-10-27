"""
Entry-edge attribution: does the entry signal carry edge anywhere?

WHY THIS EXISTS
---------------
Three geometry hypotheses have now been tested and settled:

  * **metadata** (a wrong pip_size / max_spread makes an instrument silently
    untradeable) -- confirmed and fixed;
  * **the win-rate objective** (a 75% target forces tp_r below the break-even
    floor) -- confirmed and fixed by the reachability guard;
  * **grid width** (10/16 symbols pinned at tp_r=1.5 looked like a binding
    boundary) -- tested and REFUTED; widening the grid made results 5.46R worse
    and the pile-up simply moved to the new edge.

After all three, the aggregate out-of-sample result is still negative. Geometry
selection can stop the system from hurting itself -- and it now does -- but it
cannot manufacture a signal that is not there. So the next question is not
"which geometry" but **"is there any edge in the entries at all, and if so which
feature dimension carries it?"**

WHAT IT DOES
------------
For each symbol it replays EVERY candidate under that symbol's *deployed*
geometry, so the exit schedule is held constant and the only thing that varies
between buckets is the entry. It then measures expectancy per feature bucket.

Two deliberate choices:

  * **Per-symbol quantile ranks, pooled.** Raw feature values are not comparable
    across symbols (a score of 0.7 means different things on gold and on BTC).
    Ranking each candidate within its own symbol and then pooling the ranks
    makes "top quintile" mean the same thing everywhere, and it removes the
    symbol-level expectancy differences that would otherwise dominate the
    pooled numbers.
  * **Economic size, not p-values.** With ~19k candidates almost any gradient is
    statistically significant. Significance is not the question; the size of the
    expectancy spread and its consistency across symbols is.

WHAT IT REPORTS
---------------
Per feature: the pooled expectancy per bucket, a trade-level Spearman rank
correlation between the feature and R, and -- the part that actually decides
whether a gradient is real -- how many individual symbols show the same
direction between their own top and bottom bucket. A gradient that exists only
in the pooled average and not within the symbols is a composition effect, not an
edge.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.trade_simulator import (  # noqa: E402
    simulate_all_candidates_with_rows,
)
from jarvis.config.paths import DATA_DIR  # noqa: E402
from jarvis.config.runtime import offline_mode  # noqa: E402
from jarvis.data.symbol_registry import get_dollar_risk_per_price_unit, resolve  # noqa: E402
from jarvis.intelligence.winrate_targeting import WRProfileStore  # noqa: E402
from jarvis.learning.sample_weights import SampleUniquenessWeightEngine  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("entry_edge")

REAL_DIR = Path(DATA_DIR) / "market" / "real"
SIGNAL_DIR = Path(DATA_DIR) / "signals"
PROFILE_PATH = REPO_ROOT / "config" / "winrate_profiles.json"
REPORT_DIR = REPO_ROOT / "reports"

# Numeric features worth scanning. Every one of these is produced by the signal
# scanner, so a gradient here is a gradient the live pipeline could act on.
NUMERIC_FEATURES = [
    "score", "dissection_score", "master_score", "ev", "meta_label_prob",
    "trend_score", "confluence_count", "rr", "adversarial_penalty",
    "n_failed_gates", "atr", "spread_pips", "risk_dist",
]
CATEGORICAL_FEATURES = ["regime", "strategy", "zone", "side", "decision", "order_type"]

_WINDOW_MARKER = "_183d"


def discover(days: int = 95) -> list[str]:
    if not SIGNAL_DIR.exists():
        return []
    out = []
    for p in sorted(SIGNAL_DIR.glob("*_candidates.parquet")):
        name = p.name.replace("_candidates.parquet", "")
        if _WINDOW_MARKER in name:
            continue
        dpath = REAL_DIR / name / f"{name}_H1_{days}d.parquet"
        if dpath.exists():
            out.append(name)
    return out


def build_frame(symbol: str, profile, days: int = 95,
                slippage_pips: float = 0.5) -> pd.DataFrame:
    """Every candidate for ``symbol`` replayed under its deployed geometry.

    Returns one row per resolved candidate: the candidate's own feature columns
    plus the simulated outcome. Nothing is filtered -- the deployed score
    threshold is applied later, as a column, so the gradient can be seen in the
    region the live system rejects as well as the region it trades.

    ``slippage_pips`` is exposed because the single largest apparent gradient in
    the feature set turns out to be an artefact of it. Slippage is charged as a
    fixed PIP distance, so its cost measured in R is ``slip / risk_dist``: it
    *falls* as the stop widens. Any feature that correlates with stop width
    (``risk_dist``, ``atr``) therefore shows a positive expectancy gradient
    purely from cost accounting. Running the same scan at ``--slippage-pips 0``
    separates a real entry edge from that mechanical effect.
    """
    dpath = REAL_DIR / symbol / f"{symbol}_H1_{days}d.parquet"
    cpath = SIGNAL_DIR / f"{symbol}_candidates.parquet"
    if not dpath.exists() or not cpath.exists():
        return pd.DataFrame()

    df = pd.read_parquet(dpath).reset_index(drop=True)
    cands = pd.read_parquet(cpath)
    if cands is None or len(cands) == 0:
        return pd.DataFrame()

    spec = resolve(symbol)
    mpu = get_dollar_risk_per_price_unit(symbol)
    pip = float(getattr(spec, "pip_size", 0.0001) or 0.0001)
    geom = profile.geometry

    pairs = simulate_all_candidates_with_rows(
        df=df, candidates=cands, geom=geom, money_per_unit=mpu,
        slippage_price_equiv=float(slippage_pips) * pip, spec=spec, symbol=symbol,
    )
    if not pairs:
        return pd.DataFrame()

    row_idx = [i for i, _ in pairs]
    feats = cands.iloc[row_idx].reset_index(drop=True)
    out = pd.DataFrame({
        "pnl_r": [o.pnl_r for _, o in pairs],
        "is_win": [o.is_win for _, o in pairs],
        "entry_idx": [o.entry_idx for _, o in pairs],
        "exit_idx": [o.exit_idx for _, o in pairs],
        "bars_held": [o.bars_held for _, o in pairs],
        "exit_reason": [o.result for _, o in pairs],
        "mfe_r": [o.mfe_r for _, o in pairs],
        "mae_r": [o.mae_r for _, o in pairs],
    })
    frame = pd.concat([out, feats], axis=1)
    frame["symbol"] = symbol
    # The deployed system only takes candidates at or above its calibrated
    # threshold. Kept as a flag so both populations can be reported.
    frame["traded"] = frame["score"] >= float(geom.min_score)
    frame["tp_r"] = float(geom.tp_r)
    return frame


def _bucket_stats(g: pd.DataFrame) -> dict:
    rs = g["pnl_r"].to_numpy(dtype=float)
    wins = rs[rs > 0]
    losses = rs[rs < 0]
    return {
        "n": int(len(rs)),
        "wr": float(len(wins) / len(rs)) if len(rs) else 0.0,
        "expectancy_r": float(rs.mean()) if len(rs) else 0.0,
        "total_r": float(rs.sum()) if len(rs) else 0.0,
        "payoff": float(abs(wins.mean() / losses.mean())) if len(wins) and len(losses) else 0.0,
    }


def _uniqueness_weighted(g: pd.DataFrame) -> float:
    """Expectancy down-weighting trades that overlap in time.

    Overlapping positions carry duplicated information, so an unweighted mean
    overstates the evidence. This is the arbiter of whether a bucket's result
    rests on independent bets.
    """
    if len(g) < 2:
        return 0.0
    enriched = [
        {"pnl": float(r), "duration_bars": max(1, int(d))}
        for r, d in zip(g["pnl_r"], g["bars_held"])
    ]
    try:
        w = SampleUniquenessWeightEngine.get_sample_weights(enriched)
        r = g["pnl_r"].to_numpy(dtype=float)
        if len(w) == len(r) and np.sum(w) > 0:
            return float(np.sum(w * r))
    except Exception:
        pass
    return float("nan")


def quintile_column(frame: pd.DataFrame, feature: str, q: int = 5) -> pd.Series:
    """Per-symbol quantile rank of ``feature``, pooled as 0..q-1.

    Ranking within each symbol is what makes the buckets comparable: the raw
    feature scales differ by an order of magnitude between gold and BTC, and a
    pooled cut on raw values would mostly be separating symbols.
    """
    def rank(s: pd.Series) -> pd.Series:
        s = pd.to_numeric(s, errors="coerce")
        if s.notna().sum() < q:
            return pd.Series(np.nan, index=s.index)
        try:
            r = s.rank(method="first", pct=True)
        except Exception:
            return pd.Series(np.nan, index=s.index)
        return np.minimum((r * q).astype(int), q - 1)

    return frame.groupby("symbol", group_keys=False)[feature].apply(rank)


def analyse_numeric(frame: pd.DataFrame, feature: str, q: int, min_n: int) -> dict:
    work = frame[[feature, "pnl_r", "symbol", "bars_held"]].copy()
    work[feature] = pd.to_numeric(work[feature], errors="coerce")
    work = work.dropna(subset=[feature, "pnl_r"])
    if len(work) < min_n * 2:
        return {}

    work["bucket"] = quintile_column(frame.loc[work.index], feature, q)
    work = work.dropna(subset=["bucket"])
    if work.empty:
        return {}

    # Trade-level rank correlation: uses every observation, unlike a 5-bucket
    # table, and is monotonicity-free (it does not assume the buckets are evenly
    # populated or that the relationship is linear).
    rho = float(work[feature].corr(work["pnl_r"], method="spearman"))

    buckets = []
    for b in sorted(work["bucket"].unique()):
        g = work[work["bucket"] == b]
        if len(g) < min_n:
            continue
        st = _bucket_stats(g)
        st["bucket"] = int(b)
        buckets.append(st)
    if len(buckets) < 3:
        return {}

    # Per-symbol agreement: the decisive test. A pooled gradient that does not
    # reappear inside the individual symbols is a composition effect.
    agree = 0
    considered = 0
    for sym, gs in work.groupby("symbol"):
        lo = gs[gs["bucket"] == gs["bucket"].min()]
        hi = gs[gs["bucket"] == gs["bucket"].max()]
        if len(lo) < 10 or len(hi) < 10:
            continue
        considered += 1
        if hi["pnl_r"].mean() > lo["pnl_r"].mean():
            agree += 1

    return {
        "feature": feature,
        "kind": "numeric",
        "n": int(len(work)),
        "spearman": round(rho, 4),
        "buckets": buckets,
        "spread_r": round(buckets[-1]["expectancy_r"] - buckets[0]["expectancy_r"], 4),
        "top_bucket_expectancy": round(buckets[-1]["expectancy_r"], 4),
        "top_bucket_n": buckets[-1]["n"],
        "top_bucket_wr": round(buckets[-1]["wr"], 4),
        "symbols_agreeing": agree,
        "symbols_considered": considered,
    }


def analyse_categorical(frame: pd.DataFrame, feature: str, min_n: int) -> dict:
    work = frame[[feature, "pnl_r", "symbol", "bars_held"]].copy()
    work = work.dropna(subset=[feature, "pnl_r"])
    if work.empty:
        return {}
    groups = []
    for val, g in work.groupby(feature):
        if len(g) < min_n:
            continue
        st = _bucket_stats(g)
        st["value"] = str(val)
        groups.append(st)
    if len(groups) < 2:
        return {}
    groups.sort(key=lambda d: -d["expectancy_r"])
    spread = groups[0]["expectancy_r"] - groups[-1]["expectancy_r"]
    return {
        "feature": feature,
        "kind": "categorical",
        "n": int(len(work)),
        "groups": groups,
        "spread_r": round(float(spread), 4),
        "best": groups[0],
        "worst": groups[-1],
    }


def render(results: list[dict], meta: dict, min_n: int) -> str:
    L: list[str] = []
    A = L.append
    A("# Entry-Edge Attribution — where does the signal actually carry edge?")
    A("")
    A(f"**Data:** {meta['symbols']} symbols, {meta['candidates']:,} candidates, "
      f"{meta['outcomes']:,} simulated outcomes  ")
    A("**Method:** every candidate replayed under its symbol's deployed geometry "
      "(identical exits), then bucketed by feature  ")
    A(f"**Stop slippage charged:** {meta.get('slippage_pips', 0.5)} pips  ")
    A(f"**Generated:** {meta['generated_utc']}")
    A("")
    A("## 0. Why this exists")
    A("")
    A("Three geometry hypotheses have been tested and settled: **metadata** "
      "(confirmed, fixed), **the win-rate objective** (confirmed, fixed by the "
      "reachability guard), and **grid width** (tested and refuted — widening the "
      "grid made results 5.46R worse and the pile-up simply moved to the new "
      "edge). The aggregate out-of-sample result is still negative. Geometry can "
      "stop the system hurting itself; it cannot manufacture an edge. So the "
      "question is now whether any entry feature carries one.")
    A("")
    A("**How to read this.** `rho` is the trade-level Spearman rank correlation "
      "between the feature and R. With ~19k candidates almost any gradient is "
      "statistically significant, so significance is not the test — the test is "
      "the *size* of the expectancy spread and, decisively, whether it reappears "
      "**inside the individual symbols**. A pooled gradient that vanishes within "
      "the symbols is a composition effect, not an edge. That is what the "
      "`symbols` column counts.")
    A("")

    numeric = [r for r in results if r.get("kind") == "numeric"]
    numeric.sort(key=lambda d: -abs(d["spearman"]))
    A("## 1. Numeric features, ranked by rank-correlation with R")
    A("")
    A("| Feature | rho | n | bottom bucket exp | top bucket exp | spread | top WR | top n | symbols agreeing |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in numeric:
        A(f"| `{r['feature']}` | {r['spearman']:+.4f} | {r['n']:,} | "
          f"{r['buckets'][0]['expectancy_r']:+.4f} | {r['top_bucket_expectancy']:+.4f} | "
          f"{r['spread_r']:+.4f} | {r['top_bucket_wr']*100:.1f}% | {r['top_bucket_n']:,} | "
          f"{r['symbols_agreeing']}/{r['symbols_considered']} |")
    A("")

    cat = [r for r in results if r.get("kind") == "categorical"]
    cat.sort(key=lambda d: -d["spread_r"])
    if cat:
        A("## 2. Categorical features")
        A("")
        for r in cat:
            A(f"### `{r['feature']}` (spread {r['spread_r']:+.4f}R, n={r['n']:,})")
            A("")
            A("| Value | n | WR | Expectancy (R) | Total R |")
            A("|---|---:|---:|---:|---:|")
            for g in r["groups"]:
                A(f"| {g['value']} | {g['n']:,} | {g['wr']*100:.1f}% | "
                  f"{g['expectancy_r']:+.4f} | {g['total_r']:+.2f} |")
            A("")

    A("## 3. Bucket detail for the strongest numeric gradients")
    A("")
    for r in numeric[:4]:
        A(f"### `{r['feature']}` (rho {r['spearman']:+.4f}, spread {r['spread_r']:+.4f}R)")
        A("")
        A("| Bucket (per-symbol quantile) | n | WR | Expectancy (R) | Total R | Payoff |")
        A("|---|---:|---:|---:|---:|---:|")
        for b in r["buckets"]:
            A(f"| Q{b['bucket']+1} | {b['n']:,} | {b['wr']*100:.1f}% | "
              f"{b['expectancy_r']:+.4f} | {b['total_r']:+.2f} | {b['payoff']:.3f} |")
        A("")

    A("## 4. Verdict")
    A("")
    strongest = max(numeric, key=lambda d: abs(d["spearman"])) if numeric else None
    if strongest is None:
        A("No numeric feature produced a usable sample.")
    else:
        A(f"The largest rank correlation in the set is `{strongest['feature']}` at "
          f"rho {strongest['spearman']:+.4f}, worth {strongest['spread_r']:+.4f}R "
          f"between the bottom and top quintile. Read against a per-trade "
          f"expectancy of a few hundredths of an R, and against the "
          f"{strongest['symbols_agreeing']}/{strongest['symbols_considered']} "
          f"symbols that reproduce the direction internally.")
        A("")
        A("A feature only justifies acting on it if **all three** hold: the spread "
          "is economically meaningful, most symbols agree, and the top bucket's "
          "expectancy is positive on its own. A high rho with a negative top "
          "bucket means the feature sorts *how badly* a trade loses, not how well "
          "it wins — that is a risk-sizing signal, not an entry filter.")
    A("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=None, help="comma-separated subset")
    ap.add_argument("--quintiles", type=int, default=5)
    ap.add_argument("--min-bucket-n", type=int, default=30)
    ap.add_argument("--slippage-pips", type=float, default=0.5,
                    help="stop slippage charged on every protective-stop fill "
                         "(default 0.5, matching the engine). Set 0 to test whether "
                         "a gradient is a real entry edge or an artefact of the "
                         "R-normalised cost falling as the stop widens.")
    ap.add_argument("--traded-only", action="store_true",
                    help="restrict to candidates at/above the deployed score threshold")
    ap.add_argument("--json", action="store_true", help="also write a JSON result")
    args = ap.parse_args()

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else discover()
    )
    if not symbols:
        logger.error("No candidate tables found.")
        return 2

    store = WRProfileStore(PROFILE_PATH)
    profiles = store.load()
    if not profiles:
        logger.error("No calibrated profiles. Run tools/calibrate_winrate.py first.")
        return 2

    print("=" * 100)
    print("HM Algo 2.0 — ENTRY-EDGE ATTRIBUTION")
    print("=" * 100)

    frames: list[pd.DataFrame] = []
    t0 = time.time()
    with offline_mode():
        for sym in symbols:
            prof = profiles.get(sym)
            if prof is None:
                print(f"  {sym:8s} SKIP (no profile)")
                continue
            f = build_frame(sym, prof, slippage_pips=args.slippage_pips)
            if f.empty:
                print(f"  {sym:8s} SKIP (no outcomes)")
                continue
            frames.append(f)
            print(f"  {sym:8s} candidates={len(f):5d} tp_r={prof.geometry.tp_r:g} "
                  f"thr={prof.geometry.min_score:.3f} "
                  f"traded={int(f['traded'].sum()):4d} "
                  f"exp_all={f['pnl_r'].mean():+.4f}R "
                  f"exp_traded={f.loc[f['traded'], 'pnl_r'].mean() if f['traded'].any() else float('nan'):+.4f}R")

    if not frames:
        logger.error("No outcomes produced.")
        return 1

    frame = pd.concat(frames, ignore_index=True)
    if args.traded_only:
        frame = frame[frame["traded"]].reset_index(drop=True)
        print(f"\nRestricted to traded candidates: {len(frame):,}")

    print(f"\nPooled: {len(frame):,} outcomes over {frame['symbol'].nunique()} symbols "
          f"({time.time()-t0:.0f}s)")
    print()

    results: list[dict] = []
    for feat in NUMERIC_FEATURES:
        if feat not in frame.columns:
            continue
        r = analyse_numeric(frame, feat, args.quintiles, args.min_bucket_n)
        if r:
            results.append(r)
    for feat in CATEGORICAL_FEATURES:
        if feat not in frame.columns:
            continue
        r = analyse_categorical(frame, feat, args.min_bucket_n)
        if r:
            results.append(r)

    numeric = sorted([r for r in results if r["kind"] == "numeric"],
                     key=lambda d: -abs(d["spearman"]))
    print(f"{'feature':<20} {'rho':>8} {'n':>7} {'Q1 exp':>9} {'Q5 exp':>9} "
          f"{'spread':>8} {'Q5 WR':>7} {'agree':>7}")
    print("-" * 100)
    for r in numeric:
        print(f"{r['feature']:<20} {r['spearman']:>+8.4f} {r['n']:>7,} "
              f"{r['buckets'][0]['expectancy_r']:>+9.4f} {r['top_bucket_expectancy']:>+9.4f} "
              f"{r['spread_r']:>+8.4f} {r['top_bucket_wr']*100:>6.1f}% "
              f"{r['symbols_agreeing']:>3}/{r['symbols_considered']:<3}")

    cat = sorted([r for r in results if r["kind"] == "categorical"], key=lambda d: -d["spread_r"])
    if cat:
        print()
        print("CATEGORICAL")
        print("-" * 100)
        for r in cat:
            print(f"{r['feature']:<20} spread {r['spread_r']:+.4f}R  "
                  f"best={r['best']['value']} ({r['best']['expectancy_r']:+.4f}R, n={r['best']['n']:,})  "
                  f"worst={r['worst']['value']} ({r['worst']['expectancy_r']:+.4f}R, n={r['worst']['n']:,})")

    meta = {
        "symbols": int(frame["symbol"].nunique()),
        "candidates": int(len(frame)),
        "outcomes": int(len(frame)),
        "traded_only": bool(args.traded_only),
        "quintiles": args.quintiles,
        "min_bucket_n": args.min_bucket_n,
        "slippage_pips": float(args.slippage_pips),
        "generated_utc": pd.Timestamp.now("UTC").isoformat(),
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md = render(results, meta, args.min_bucket_n)
    out_md = REPORT_DIR / "entry_edge_attribution.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"\nReport: {out_md.relative_to(REPO_ROOT)}")

    if args.json:
        out_json = REPORT_DIR / "entry_edge_attribution.json"
        out_json.write_text(json.dumps({"meta": meta, "results": results},
                                       indent=2, default=str), encoding="utf-8")
        print(f"JSON:   {out_json.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
