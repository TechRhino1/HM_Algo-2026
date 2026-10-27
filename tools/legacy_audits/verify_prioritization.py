"""Does the arbiter actually select the highest-priority trades?

THE QUESTION
------------
``UniversalOpportunityArbiter`` scores every simultaneous opportunity with

    Utility = P_ML * E[V] * (1 + Confluence/100) * (1 - Penalty) * RegimeMultiplier

and ``rank_and_select_best`` returns the top-ranked actionable candidate. The
claim under test is that this ranking picks the trades that actually work — in
every trade mode, and in every market regime.

WHY IT IS TESTED HERE AND NOT THROUGH THE ENGINE
------------------------------------------------
The arbiter is invoked in exactly one place: ``orchestrator._orchestration_loop_single_pass``,
which sweeps 20 symbols x 3 styles and keeps the single best candidate per pass.
``BacktestEngine`` never calls it. So the arbiter cannot be validated by running
a backtest — a backtest measures the *entry policy*, not the ranking. It has to
be measured against counterfactual outcomes: for every candidate the arbiter
ranked, what would it have returned had it been taken?

METHOD
------
1. Replay EVERY candidate for a symbol under one fixed exit schedule, so the only
   thing varying between buckets is the entry/ranking, not the exits.
2. Reconstruct the arbiter's utility exactly, calling the production
   ``calculate_regime_multiplier`` rather than re-deriving its matrix.
3. Test whether higher utility means better realised R:
     * pooled utility-quintile gradient (per-symbol ranks, so buckets are
       comparable across instruments);
     * top-1-vs-rest at each timestamp — the arbiter's literal contract is to
       pick one winner out of a simultaneous set;
     * the same, sliced by regime;
     * an INVERSE CONTROL: rank by lowest utility. If the inverse does better,
       the ranking is anti-predictive, not merely uninformative.

Everything is reported per mode (SWING / DAY_TRADING / SCALP) and per regime.
Run:  python tools/verify_prioritization.py --slippage-pips 0.5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.trade_simulator import (  # noqa: E402
    Geometry,
    simulate_all_candidates_with_rows,
)
from jarvis.config.paths import DATA_DIR  # noqa: E402
from jarvis.data.symbol_registry import get_dollar_risk_per_price_unit, resolve  # noqa: E402
from jarvis.intelligence.opportunity_arbiter import UniversalOpportunityArbiter  # noqa: E402
from jarvis.market.data_feed import style_timeframes, style_timeframe_set  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("verify_prioritization")

REAL_DIR = Path(DATA_DIR) / "market" / "real"
SIGNAL_DIR = Path(DATA_DIR) / "signals"
OUT_PATH = REPO_ROOT / "reports" / "prioritization_verification.json"

STYLES = ["SWING", "DAY_TRADING", "SCALP"]

# One fixed exit schedule for every candidate, so buckets differ only by entry.
GEOM = Geometry(tp_r=1.5, min_score=0.0)

_ARBITER = UniversalOpportunityArbiter()


# ── arbiter reconstruction ───────────────────────────────────────────────────
def regime_multipliers(regimes, strategies, style: str) -> np.ndarray:
    """Exact regime multipliers via the production method, memoised per triple."""
    cache: dict = {}
    out = np.ones(len(regimes), dtype=float)
    for i, (r, s) in enumerate(zip(regimes, strategies)):
        key = (str(r), str(s))
        if key not in cache:
            cache[key] = _ARBITER.calculate_regime_multiplier(key[0], style, key[1])
        out[i] = cache[key]
    return out


def _col(d: pd.DataFrame, name: str, default) -> pd.Series:
    """A column as a Series, or ``default`` broadcast over the index.

    ``DataFrame.get`` returns the *scalar* default when a column is absent, so
    calling Series methods on the result raises. Older candidate tables predate
    ``mtf_confluence_score``, so this is a real path, not a defensive nicety.
    """
    if name in d.columns:
        return d[name]
    return pd.Series(default, index=d.index)


def arbiter_columns(df: pd.DataFrame, style: str) -> pd.DataFrame:
    """Reconstruct utility/grade/actionable exactly as ``evaluate_opportunity`` does.

    The unit caveat is deliberate and preserved: ``expected_value`` is carried in
    DOLLARS by the decision engine, while the grade thresholds (0.40 / 0.80)
    read as R-multiples. The comparison is reproduced verbatim rather than
    corrected, because the question is what the deployed logic selects — not
    what a corrected version would select.
    """
    d = df.copy()
    win_pct = pd.to_numeric(_col(d, "score", 0.5), errors="coerce").fillna(0.5) * 100.0
    win_dec = win_pct / 100.0

    ml_raw = pd.to_numeric(_col(d, "meta_label_prob", -1.0), errors="coerce").fillna(-1.0)
    ml = np.where(ml_raw > 0, ml_raw, win_dec)
    ml = np.clip(ml, 0.35, 0.88)

    ev = pd.to_numeric(_col(d, "ev", 0.0), errors="coerce").fillna(0.0)
    conf = pd.to_numeric(_col(d, "master_score", 0.0), errors="coerce").fillna(0.0)
    fallback = pd.to_numeric(_col(d, "mtf_confluence_score", 50.0), errors="coerce").fillna(50.0)
    conf = np.where(conf == 0.0, fallback, conf)

    pen_raw = pd.to_numeric(_col(d, "adversarial_penalty", 0.0), errors="coerce").fillna(0.0)
    pen = np.minimum(0.60, np.maximum(0.0, np.where(pen_raw > 1.0, pen_raw / 100.0, pen_raw)))

    regmult = regime_multipliers(_col(d, "regime", "RANGE").values,
                                 _col(d, "strategy", "UNKNOWN").values, style)

    side = _col(d, "side", "").astype(str).str.upper()
    directional = side.isin(["BUY", "SELL"]).values
    positive_ev = (ev.values > 0.0) & directional
    utility = np.where(
        positive_ev,
        ml * np.maximum(0.0, ev.values) * (1.0 + conf / 100.0)
        * np.maximum(0.20, 1.0 - pen) * regmult,
        0.0,
    )

    d["win_prob_pct"] = win_pct.values
    d["ml_prob"] = ml
    d["confluence_score"] = conf
    d["penalty_norm"] = pen
    d["regime_multiplier"] = regmult
    d["utility_score"] = np.round(utility, 4)

    grade = np.full(len(d), "GRADE C", dtype=object)
    grade[(d["utility_score"] >= 0.95) & (ev.values > 0)] = "GRADE B"
    grade[(d["utility_score"] >= 1.30) & ((win_pct.values >= 55.0) | (ml >= 0.60))
          & (conf >= 22.0) & (ev.values >= 0.40)] = "GRADE A"
    grade[(d["utility_score"] >= 1.80) & ((win_pct.values >= 62.0) | (ml >= 0.70))
          & (conf >= 28.0) & (ev.values >= 0.80)] = "GRADE A+"
    d["setup_grade"] = grade

    gate = _col(d, "gate_passed", True).astype(bool).values
    nfail = pd.to_numeric(_col(d, "n_failed_gates", 0), errors="coerce").fillna(0).values
    decision = _col(d, "decision", "").astype(str).str.upper().values
    d["is_actionable"] = (
        directional & (d["utility_score"].values >= 0.95) & (ev.values > 0)
        & ((decision == "EXECUTE")
           | (np.isin(grade, ["GRADE A+", "GRADE A", "GRADE B"])
              & (gate | ((decision == "WAIT") & (nfail <= 1)))))
    )
    return d


# ── candidate replay ─────────────────────────────────────────────────────────
def candidate_path(symbol: str, timeframe: str, days: int) -> Path:
    if timeframe == "H1" and days == 95:
        return SIGNAL_DIR / f"{symbol}_candidates.parquet"
    return SIGNAL_DIR / f"{symbol}_{timeframe}_{days}d_candidates.parquet"


def _resolve_candidates(symbol: str, timeframe: str, days: int) -> Optional[Path]:
    """The candidate table for (symbol, timeframe), preferring the requested window.

    Price history and candidate tables are produced by separate passes and are
    not guaranteed to share a window: the broker's M1 cap forces SCALP onto a
    shorter primary series, and H1 candidates may exist at 95d while the price
    cache holds 183d. Falling back to whatever window actually exists beats
    silently reporting "no candidates".
    """
    preferred = candidate_path(symbol, timeframe, days)
    if preferred.exists():
        return preferred
    for pat in (f"{symbol}_{timeframe}_*d_candidates.parquet",
                f"{symbol}_candidates.parquet" if timeframe == "H1" else None):
        if not pat:
            continue
        hits = sorted(SIGNAL_DIR.glob(pat))
        if hits:
            return hits[-1]
    return None


def _resolve_prices(symbol: str, timeframe: str, days: int) -> Optional[Path]:
    preferred = REAL_DIR / symbol / f"{symbol}_{timeframe}_{days}d.parquet"
    if preferred.exists():
        return preferred
    hits = sorted((REAL_DIR / symbol).glob(f"{symbol}_{timeframe}_*d.parquet"))
    return hits[-1] if hits else None


def build_mode_frame(symbol: str, style: str, days: int,
                     slippage_pips: float) -> pd.DataFrame:
    """One row per candidate: features + arbiter columns + realised R."""
    roles = style_timeframes(style)
    primary = roles["primary"]
    dpath = _resolve_prices(symbol, primary, days)
    cpath = _resolve_candidates(symbol, primary, days)
    if dpath is None or cpath is None:
        return pd.DataFrame()

    df = pd.read_parquet(dpath).reset_index(drop=True)
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
    cands = pd.read_parquet(cpath)
    if cands is None or len(cands) == 0:
        return pd.DataFrame()
    cands = cands.reset_index(drop=True)
    cands["time"] = pd.to_datetime(cands["time"], utc=True).dt.tz_localize(None)

    # Restrict to the window where every timeframe the style needs exists, so a
    # missing timing frame cannot silently degrade the mode under test.
    need = style_timeframe_set(style)
    starts, ends = [], []
    for tf in need:
        p = _resolve_prices(symbol, tf, days)
        if p is None:
            return pd.DataFrame()
        t = pd.read_parquet(p, columns=["time"])["time"]
        t = pd.to_datetime(t, utc=True).dt.tz_localize(None)
        starts.append(t.iloc[0])
        ends.append(t.iloc[-1])
    lo, hi = max(starts), min(ends)
    df = df[(df["time"] >= lo) & (df["time"] <= hi)].reset_index(drop=True)
    cands = cands[(cands["time"] >= lo) & (cands["time"] <= hi)].reset_index(drop=True)
    if len(cands) == 0 or len(df) < 50:
        return pd.DataFrame()

    spec = resolve(symbol)
    mpu = get_dollar_risk_per_price_unit(symbol)
    pip = float(getattr(spec, "pip_size", 0.0001) or 0.0001)

    pairs = simulate_all_candidates_with_rows(
        df=df, candidates=cands, geom=GEOM, money_per_unit=mpu,
        slippage_price_equiv=float(slippage_pips) * pip, spec=spec, symbol=symbol,
    )
    if not pairs:
        return pd.DataFrame()

    row_idx = [i for i, _ in pairs]
    feats = cands.iloc[row_idx].reset_index(drop=True)
    out = pd.DataFrame({
        "pnl_r": [o.pnl_r for _, o in pairs],
        "is_win": [o.is_win for _, o in pairs],
        "bars_held": [o.bars_held for _, o in pairs],
        "exit_reason": [o.result for _, o in pairs],
    })
    frame = pd.concat([out, feats], axis=1)
    frame["symbol"] = symbol
    frame["style"] = style
    frame = arbiter_columns(frame, style)
    frame["window_start"] = lo
    frame["window_end"] = hi
    return frame


# ── tests ────────────────────────────────────────────────────────────────────
def _stats(rs: np.ndarray) -> dict:
    rs = np.asarray(rs, dtype=float)
    if len(rs) == 0:
        return {"n": 0, "expectancy_r": 0.0, "total_r": 0.0, "wr": 0.0, "payoff": 0.0}
    wins, losses = rs[rs > 0], rs[rs < 0]
    return {
        "n": int(len(rs)),
        "expectancy_r": round(float(rs.mean()), 4),
        "total_r": round(float(rs.sum()), 3),
        "wr": round(float(len(wins) / len(rs)), 4),
        "payoff": round(float(wins.mean() / abs(losses.mean())), 3)
        if len(wins) and len(losses) else 0.0,
    }


def quintile_gradient(frame: pd.DataFrame, col: str, q: int = 5) -> dict:
    """Per-symbol quantile buckets of ``col``, pooled — then compared per symbol."""
    def rank(s: pd.Series) -> pd.Series:
        s = pd.to_numeric(s, errors="coerce")
        if s.notna().sum() < q:
            return pd.Series(np.nan, index=s.index)
        try:
            return pd.qcut(s.rank(method="first"), q, labels=False)
        except Exception:
            return pd.Series(np.nan, index=s.index)

    work = frame.copy()
    work["_bucket"] = work.groupby("symbol", group_keys=False)[col].apply(rank)
    work = work.dropna(subset=["_bucket"])
    if len(work) == 0:
        return {"buckets": [], "spread_r": 0.0, "symbols_agreeing": 0, "symbols_total": 0}

    buckets = []
    for b in sorted(work["_bucket"].unique()):
        g = work[work["_bucket"] == b]
        s = _stats(g["pnl_r"].to_numpy())
        s["bucket"] = int(b)
        s["median_feature"] = round(float(pd.to_numeric(g[col], errors="coerce").median()), 4)
        buckets.append(s)

    # Per-symbol direction: does this symbol's own top bucket beat its bottom?
    agree = total = 0
    for _, g in work.groupby("symbol"):
        top = g[g["_bucket"] == g["_bucket"].max()]["pnl_r"]
        bot = g[g["_bucket"] == g["_bucket"].min()]["pnl_r"]
        if len(top) >= 5 and len(bot) >= 5:
            total += 1
            if top.mean() > bot.mean():
                agree += 1

    top_b = buckets[-1]["expectancy_r"] if buckets else 0.0
    bot_b = buckets[0]["expectancy_r"] if buckets else 0.0
    return {
        "buckets": buckets,
        "spread_r": round(top_b - bot_b, 4),
        "symbols_agreeing": agree,
        "symbols_total": total,
    }


def top1_vs_rest(frame: pd.DataFrame, rank_col: str) -> dict:
    """The arbiter's literal contract: pick one winner from a simultaneous set."""
    picks, rest = [], []
    for _, g in frame.groupby("time"):
        if len(g) < 2:
            continue
        i = int(pd.to_numeric(g[rank_col], errors="coerce").fillna(-1e18).idxmax())
        picks.append(float(g.loc[i, "pnl_r"]))
        rest.append(float(g.drop(index=i)["pnl_r"].mean()))
    if not picks:
        return {"n_sets": 0, "pick_expectancy_r": 0.0, "rest_expectancy_r": 0.0,
                "delta_r": 0.0, "pick_win_rate": 0.0, "pct_sets_pick_better": 0.0}
    picks_a, rest_a = np.array(picks), np.array(rest)
    return {
        "n_sets": int(len(picks)),
        "pick_expectancy_r": round(float(picks_a.mean()), 4),
        "rest_expectancy_r": round(float(rest_a.mean()), 4),
        "delta_r": round(float(picks_a.mean() - rest_a.mean()), 4),
        "pick_win_rate": round(float((picks_a > 0).mean()), 4),
        "pct_sets_pick_better": round(float((picks_a > rest_a).mean()), 4),
    }


def analyse(frame: pd.DataFrame, label: str) -> dict:
    out: dict = {"label": label, "n_candidates": int(len(frame)),
                 "overall": _stats(frame["pnl_r"].to_numpy())}
    out["utility_quintiles"] = quintile_gradient(frame, "utility_score")
    out["top1_by_utility"] = top1_vs_rest(frame, "utility_score")
    # Inverse control: rank by LOWEST utility. If this beats the real ranking the
    # arbiter is anti-predictive rather than merely uninformative.
    inv = frame.copy()
    inv["_neg_utility"] = -pd.to_numeric(inv["utility_score"], errors="coerce").fillna(0.0)
    out["top1_by_inverse_utility"] = top1_vs_rest(inv, "_neg_utility")

    # Does the arbiter's own actionability filter help?
    act = frame[frame["is_actionable"]]
    non = frame[~frame["is_actionable"]]
    out["actionable_subset"] = _stats(act["pnl_r"].to_numpy()) if len(act) else _stats(np.array([]))
    out["non_actionable_subset"] = _stats(non["pnl_r"].to_numpy()) if len(non) else _stats(np.array([]))
    out["grades"] = {
        gname: _stats(g["pnl_r"].to_numpy())
        for gname, g in frame.groupby("setup_grade")
    }
    out["regimes"] = {
        str(rname): {
            "n": int(len(g)),
            "overall": _stats(g["pnl_r"].to_numpy()),
            "utility_quintiles": quintile_gradient(g, "utility_score"),
            "top1_by_utility": top1_vs_rest(g, "utility_score"),
        }
        for rname, g in frame.groupby("regime")
        if len(g) >= 50
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=183)
    ap.add_argument("--slippage-pips", type=float, default=0.5)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--styles", default=",".join(STYLES))
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    from jarvis.data.symbol_registry import all_symbols

    symbols = [s.canonical for s in all_symbols()]
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    styles = [s.strip() for s in args.styles.split(",") if s.strip()]

    report: dict = {
        "slippage_pips": args.slippage_pips,
        "days": args.days,
        "geometry": {"tp_r": GEOM.tp_r, "min_score": GEOM.min_score,
                     "be_trigger_r": GEOM.be_trigger_r, "trail_atr": GEOM.trail_atr,
                     "max_bars": GEOM.max_bars},
        "modes": {},
    }

    for style in styles:
        frames = []
        missing = []
        for sym in symbols:
            f = build_mode_frame(sym, style, args.days, args.slippage_pips)
            if len(f):
                frames.append(f)
            else:
                missing.append(sym)
        if not frames:
            print(f"{style}: no candidates built", flush=True)
            continue
        allf = pd.concat(frames, ignore_index=True)
        allf.to_parquet(REPO_ROOT / "reports" / f"_prio_frame_{style}.parquet", index=False)
        res = analyse(allf, style)
        res["symbols_with_candidates"] = sorted(allf["symbol"].unique().tolist())
        res["symbols_missing"] = missing
        report["modes"][style] = res
        uq = res["utility_quintiles"]
        t1 = res["top1_by_utility"]
        inv = res["top1_by_inverse_utility"]
        print(f"{style:12s} cands={res['n_candidates']:6d} "
              f"expR={res['overall']['expectancy_r']:+.4f} | "
              f"Q5-Q1={uq['spread_r']:+.4f} ({uq['symbols_agreeing']}/{uq['symbols_total']} sym) | "
              f"top1 delta={t1['delta_r']:+.4f} | inverse delta={inv['delta_r']:+.4f}", flush=True)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\nreport -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
