"""P0-3 — measure the exit geometry instead of asserting it.

The audit's P0-3 says: replace every fixed multiplier in the exit geometry
with a quantity computed from current conditions.  Before any of that is
written into the live decision path, the *quantities* have to be measured,
because a "dynamic" rule fitted by eye is just a constant with extra steps.

Four independent measurements, each runnable on its own:

  A. **The stop's noise band.**  ``noise = |close - open| / ATR``.  A stop must
     sit outside the range an ordinary bar covers by itself, so ``k`` is the
     quantile of that distribution — not a regime name.  Also tests the second
     half of the spec's claim: does the required ``k`` actually *rise* with the
     current ATR percentile, or is ``noise/ATR`` scale-free?  If it is
     scale-free, the "scale by ATR percentile" clause is unnecessary and should
     not be added.

  B. **The target, from measured MFE.**  Sweeps ``tp_r`` and reports the payoff
     curve per symbol and per volatility bucket.  The question is not "what is
     the best tp_r" — fitting one number to one window is how the current 1.5
     got there — but "does the optimum *move* with the volatility bucket",
     which is what would justify conditioning it.

  C. **The time stop.**  Sweeps ``max_bars`` and reports where the realised
     edge decays, plus the share of exits that are the clock rather than the
     market.  The audit's specific claim — that at tp=3.0 the average win is
     2.65R rather than 3.0R *because* the 200-bar clock cuts winners short —
     is tested directly by reporting ``avg_win_r / tp_r``.

  D. **The cost gate.**  ``cost_r = (2 x spread + slippage) / risk_dist`` per
     candidate, in R units.  This is arithmetic, not simulation: it says what
     fraction of the target is eaten before the trade starts.

  E. **The stop multiple itself, priced.**  Part A measures what a given ``k``
     *prevents*; this measures what it *costs*.  The stop is forced to
     ``k x ATR`` with the entry and ``tp_r`` held fixed, so the payoff curve
     across ``k`` is the missing half of the answer — and it is the half that
     decides the SCALP cap.

Usage
-----
    python tools/p0_3_exit_geometry_measurement.py --parts A
    python tools/p0_3_exit_geometry_measurement.py --parts B,C,D
    python tools/p0_3_exit_geometry_measurement.py --parts E --tf M15 --window 183
    python tools/p0_3_exit_geometry_measurement.py --out reports/p0_3.json

Read-only with respect to the trading code; writes only its report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List

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
from tools.audit_trade_quality import (  # noqa: E402
    REAL_DIR,
    REALISED_RISK_PCT,
    dynamic_regimes,
    load_symbol,
    metrics,
    replay,
    wilder_atr,
)
from tools.p0_1_direction_audit import forced_long_benchmark  # noqa: E402

WINDOW_DEFAULT = 365

# Grid sizes chosen to bracket the current production values (1.5 / 2.0 R,
# 200 bars) on both sides, so "the current value is optimal" is a *result*
# rather than an assumption baked into the grid.
TP_GRID = (1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0)
MB_GRID = (12, 24, 48, 96, 200, 400, 1000)
# Part E: brackets the shipped style caps (SCALP 0.65, DAY index 0.90, DAY forex
# 1.05, DAY other 1.30, SWING 1.20-1.35) around the universal 1.11 that Part A
# measured, so "the current cap is right" is a result rather than an assumption.
K_GRID = (0.65, 0.85, 1.00, 1.11, 1.25, 1.50, 2.00)
NOISE_QUANTILES = (0.50, 0.75, 0.90, 0.95, 0.99)
ATR_PCT_WINDOW = 250
NOISE_LOOKBACK = 250


# ── part A: the stop's noise band ──────────────────────────────────────────
def noise_band(df: pd.DataFrame) -> Dict[str, object]:
    """Distribution of |close - open| / ATR, and whether it scales with ATR%.

    Both the ATR and the noise ratio are computed causally: the ATR used for
    bar *i* is the ATR as of bar *i-1*, so a bar cannot set its own yardstick.
    """
    open_ = df["open"].to_numpy(float)
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    atr = wilder_atr(df)

    prev_atr = np.roll(atr, 1)
    prev_atr[0] = atr[0]
    safe = np.where(prev_atr > 0, prev_atr, np.nan)

    body = np.abs(close - open_) / safe
    # The adverse excursion from the open, which is what a stop actually has
    # to survive — a doji with a huge wick is invisible to the body measure.
    #
    # Two variants, because they answer different questions and only one of
    # them is honest.  ``adverse`` takes the larger of the two directions,
    # which is the worst case for *any* stop; but a BUY stop sits below and a
    # SELL stop sits above, so the expected false-stop rate for a book of
    # balanced trades is the *mean* of the one-sided rates, not the max.
    # Reporting only the max overstates the defect by ~30% in quantile terms.
    up = (high - open_) / safe      # adverse for a SELL
    down = (open_ - low) / safe     # adverse for a BUY
    adverse = np.maximum(up, down)

    body = body[np.isfinite(body)]
    adverse = adverse[np.isfinite(adverse)]
    up = up[np.isfinite(up)]
    down = down[np.isfinite(down)]
    if len(body) < 50:
        return {"n": int(len(body))}

    qs = {f"q{int(q * 100)}": float(np.quantile(body, q)) for q in NOISE_QUANTILES}
    qs_adv = {f"q{int(q * 100)}": float(np.quantile(adverse, q)) for q in NOISE_QUANTILES}

    # Containment: the share of bars whose own range exceeds k x ATR.  This is
    # the "false stop" rate for a stop placed at k x ATR that the market never
    # even has to trend against you to take out.
    containment = {
        f"k={k:.2f}": float((body > k).mean())
        for k in (0.50, 0.75, 0.85, 0.95, 1.00, 1.25, 1.50, 2.00)
    }
    containment_adv = {
        f"k={k:.2f}": float((adverse > k).mean())
        for k in (0.50, 0.75, 0.85, 0.95, 1.00, 1.25, 1.50, 2.00)
    }
    # The defensible figure: for a balanced book, half the trades are exposed to
    # the up-side excursion and half to the down-side one.
    containment_dir = {
        f"k={k:.2f}": float(0.5 * ((up > k).mean() + (down > k).mean()))
        for k in (0.50, 0.75, 0.85, 0.95, 1.00, 1.25, 1.50, 2.00)
    }
    up_q = {f"q{int(q * 100)}": float(np.quantile(up, q)) for q in NOISE_QUANTILES}
    down_q = {f"q{int(q * 100)}": float(np.quantile(down, q)) for q in NOISE_QUANTILES}

    # Does the required k move with the ATR percentile?  Compute a *causal*
    # rolling quantile of the noise ratio, and a causal percentile rank of the
    # ATR, then correlate them.  A flat relationship means k needs no ATR
    # scaling at all.
    s_body = pd.Series(np.abs(close - open_) / safe)
    rolling_k = s_body.rolling(NOISE_LOOKBACK, min_periods=60).quantile(0.90)
    atr_pct_rank = pd.Series(prev_atr).rolling(ATR_PCT_WINDOW, min_periods=60).apply(
        lambda w: float((w[-1] >= w).mean()), raw=True
    )
    m = rolling_k.notna() & atr_pct_rank.notna() & np.isfinite(rolling_k) & np.isfinite(atr_pct_rank)
    if m.sum() > 100:
        k_vals = rolling_k[m].to_numpy(float)
        p_vals = atr_pct_rank[m].to_numpy(float)
        corr = float(np.corrcoef(k_vals, p_vals)[0, 1]) if np.std(k_vals) > 0 else 0.0
        # Bucketed means: the correlation could be flat overall but non-flat in
        # the tails, which is exactly where a stop's width matters.
        buckets = {}
        edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.01]
        for lo, hi in zip(edges[:-1], edges[1:]):
            sel = (p_vals >= lo) & (p_vals < hi)
            if sel.sum() > 20:
                buckets[f"atr_pct_{lo:.1f}-{hi:.1f}"] = {
                    "n": int(sel.sum()),
                    "median_rolling_k_q90": float(np.median(k_vals[sel])),
                }
    else:
        corr, buckets = 0.0, {}

    return {
        "n": int(len(body)),
        "body_quantiles": qs,
        "adverse_quantiles": qs_adv,
        "up_quantiles": up_q,
        "down_quantiles": down_q,
        "false_stop_rate_by_k": containment,
        "false_stop_rate_by_k_adverse": containment_adv,
        "false_stop_rate_by_k_directional": containment_dir,
        "corr_rolling_k_vs_atr_percentile": corr,
        "rolling_k_by_atr_percentile_bucket": buckets,
    }


# ── part B/C: target and time-stop sweeps ─────────────────────────────────
def sweep_tp(
    sym: str, df: pd.DataFrame, cands: pd.DataFrame, slip: float, tp_grid=TP_GRID
) -> Dict[str, object]:
    """Payoff curve over tp_r, overall and per volatility bucket.

    Every row carries an **always-long control at the same tp_r**, because
    without it this sweep does not measure the target — it measures drift.  A
    wider target with a fixed stop and a 200-bar clock simply holds longer, so
    on a rising instrument the expectancy climbs monotonically with tp_r and
    looks like a finding.  It is beta.  The only attributable quantity is
    ``edge_over_always_long``.
    """
    out: Dict[str, object] = {"overall": {}, "by_vol_bucket": {}}
    for tp in tp_grid:
        geom = Geometry(tp_r=tp, max_bars=200)
        tr = replay(sym, df, cands, geom, slip, 0.0)
        if tr is None or tr.empty:
            out["overall"][f"tp{tp:g}"] = {"n": 0}
            continue
        m = metrics(tr["pnl_r"].to_numpy(float), 1.0)
        m["avg_win_over_tp"] = float(m["avg_win_r"] / tp) if tp > 0 else 0.0
        long_r = forced_long_benchmark(sym, df, cands, geom, slip)
        if long_r is not None and len(long_r):
            m["always_long_mean_r"] = float(long_r.mean())
            m["edge_over_always_long"] = float(m["expectancy_r"] - long_r.mean())
            m["beats_always_long"] = bool(m["edge_over_always_long"] > 0)
        out["overall"][f"tp{tp:g}"] = m

    # Conditional on the volatility bucket: if the optimum tp_r is the same in
    # every bucket, conditioning it on volatility buys nothing.
    for tp in (1.0, 1.5, 2.0, 3.0):
        geom = Geometry(tp_r=tp, max_bars=200)
        tr = replay(sym, df, cands, geom, slip, 0.0)
        if tr is None or tr.empty:
            continue
        long_r = forced_long_benchmark(sym, df, cands, geom, slip)
        long_mean = float(long_r.mean()) if (long_r is not None and len(long_r)) else None
        for bucket, g in tr.groupby("vol_bucket"):
            slot = out["by_vol_bucket"].setdefault(str(bucket), {})
            bm = metrics(g["pnl_r"].to_numpy(float), 1.0)
            if long_mean is not None:
                bm["always_long_mean_r"] = long_mean
                bm["edge_over_always_long"] = float(bm["expectancy_r"] - long_mean)
            slot[f"tp{tp:g}"] = bm
    return out


def sweep_max_bars(
    sym: str, df: pd.DataFrame, cands: pd.DataFrame, slip: float, mb_grid=MB_GRID
) -> Dict[str, object]:
    """Where does the realised edge decay, and how often is the clock the exit?"""
    out: Dict[str, object] = {}
    for mb in mb_grid:
        for tp in (1.5, 3.0):
            geom = Geometry(tp_r=tp, max_bars=mb)
            tr = replay(sym, df, cands, geom, slip, 0.0)
            if tr is None or tr.empty:
                out[f"mb{mb}_tp{tp:g}"] = {"n": 0}
                continue
            m = metrics(tr["pnl_r"].to_numpy(float), 1.0)
            res = tr["result"].astype(str)
            m["share_time_stop"] = float(res.str.startswith("TIME_STOP").mean())
            m["share_tp"] = float((res == "TP").mean())
            m["share_sl"] = float(res.isin(["SL", "BE/TRAIL_SL"]).mean())
            # The audit's claim: the clock truncates winners, so the realised
            # average win lands below the nominal target.
            m["avg_win_over_tp"] = float(m["avg_win_r"] / tp) if tp > 0 else 0.0
            m["median_bars_held"] = float(np.median(tr["bars_held"].to_numpy(float))) \
                if "bars_held" in tr.columns else None
            # Same control as the tp sweep: a longer clock holds longer, which on
            # a drifting instrument raises expectancy with no skill involved.
            long_r = forced_long_benchmark(sym, df, cands, geom, slip)
            if long_r is not None and len(long_r):
                m["always_long_mean_r"] = float(long_r.mean())
                m["edge_over_always_long"] = float(m["expectancy_r"] - long_r.mean())
                m["beats_always_long"] = bool(m["edge_over_always_long"] > 0)
            out[f"mb{mb}_tp{tp:g}"] = m
    return out


# ── part D: the cost gate ─────────────────────────────────────────────────
def cost_gate(sym: str, df: pd.DataFrame, cands: pd.DataFrame, slip: float) -> Dict[str, object]:
    """Round-turn cost expressed in R, per candidate.  Pure arithmetic.

    Uses the candidates table's own ``spread_pips`` column.  **Do not re-derive
    it from the bars' ``spread`` column**: MT5 reports that one in *points*, and
    the scanner converts it with ``raw * point_size / pip_size`` before writing
    it out.  Multiplying the raw column by ``pip_size`` — the obvious thing to
    do — overstates every symbol's cost by exactly 10x, which is enough to
    "discover" that AUDUSD costs 1.9R per trade when it costs 0.19R.
    """
    spec = resolve(sym)
    pip = float(spec.pip_size or 0.0001)
    if "spread_pips" in cands.columns:
        spread_pips = cands["spread_pips"].to_numpy(float)
    else:  # fall back to the converted value only if the column is absent
        idx = np.clip(cands["bar_idx"].to_numpy(int), 0, len(df) - 1)
        spread_pips = df["spread"].to_numpy(float)[idx] * (
            10.0 ** (-int(spec.digits or 5)) / pip
        )
    spread_price = spread_pips * pip
    risk = cands["risk_dist"].to_numpy(float)
    risk = np.where(risk > 0, risk, np.nan)

    # A round turn crosses the spread twice: once on entry, once on exit.
    cost_r = (2.0 * spread_price + slip) / risk
    cost_r = cost_r[np.isfinite(cost_r)]
    if len(cost_r) == 0:
        return {"n": 0}
    return {
        "n": int(len(cost_r)),
        "median_cost_r": float(np.median(cost_r)),
        "q75_cost_r": float(np.quantile(cost_r, 0.75)),
        "q90_cost_r": float(np.quantile(cost_r, 0.90)),
        "q99_cost_r": float(np.quantile(cost_r, 0.99)),
        "share_over_10pct_of_1R": float((cost_r > 0.10).mean()),
        "share_over_20pct_of_1R": float((cost_r > 0.20).mean()),
        "share_over_33pct_of_1R": float((cost_r > 0.33).mean()),
        "median_spread_pips": float(np.median(spread_pips)),
        "median_risk_dist": float(np.median(risk[np.isfinite(risk)])),
    }


# ── E. the stop multiple itself, priced ───────────────────────────────────
def sweep_stop_multiple(symbol: str, df: pd.DataFrame, cands: pd.DataFrame,
                        slip_price: float, tp_r: float = 1.5) -> Dict[str, Dict[str, float]]:
    """Part E — what the stop multiple costs, not just what it prevents.

    Part A measures the *false-stop rate* of a given ``k`` and finds one
    universal ``k ≈ 1.11`` that buys 10% everywhere. That is only half an
    answer, because a wider stop is not free: every stop-out loses more, and
    because the target is ``tp_r x risk_dist`` it is pushed further away too.
    Whether widening the stop helps is an empirical question about the payoff,
    and it is the question that decides the SCALP cap.

    The stop is forced to ``k x ATR`` for every candidate, holding the entry and
    ``tp_r`` fixed, so the only thing varying across the sweep is the stop
    distance. Each point carries the always-long control at the *same* ``k``,
    so both legs are normalised by the same risk.
    """
    spec = resolve(symbol)
    money = get_dollar_risk_per_price_unit(symbol, None)
    bars = BarArrays.from_df(df)
    pip = float(getattr(spec, "pip_size", 0.0001) or 0.0001)
    n = len(df)
    out: Dict[str, Dict[str, float]] = {}

    for k in K_GRID:
        pnl: List[float] = []
        long_pnl: List[float] = []
        shares = {"sl": 0, "tp": 0, "time": 0}
        for r in cands.itertuples(index=False):
            i = int(getattr(r, "bar_idx"))
            if i + 1 >= n:
                continue
            side = str(getattr(r, "side", "")).upper()
            if side not in ("BUY", "SELL"):
                continue
            atr = float(getattr(r, "atr", 0.0) or 0.0)
            if atr <= 0:
                continue
            fill = float(getattr(r, "fill"))
            risk = k * atr
            if risk <= 0:
                continue
            is_long = side == "BUY"
            sl = fill - risk if is_long else fill + risk

            res = simulate_trade(
                symbol=symbol, side=side, entry_idx=i, fill=fill, sl=sl,
                geom=Geometry(tp_r=tp_r), money_per_unit=money, bars=bars,
                cost_price_equiv=0.0, slippage_price_equiv=slip_price,
                ai_score=float(getattr(r, "score", 0.0) or 0.0),
                calibrated_win_p=float(getattr(r, "score", 0.0) or 0.0),
                regime=str(getattr(r, "regime", "UNKNOWN")),
                strategy=str(getattr(r, "strategy", "UNKNOWN")),
                zone=str(getattr(r, "zone", "UNKNOWN")), spec=spec,
            )
            if res is None:
                continue
            pnl.append(float(res.pnl_r))
            reason = str(getattr(res, "result", "") or "").lower()
            if "sl" in reason or "stop" in reason:
                shares["sl"] += 1
            elif "tp" in reason or "target" in reason:
                shares["tp"] += 1
            else:
                shares["time"] += 1

            # Same risk distance, opposite direction. A SELL row's `fill` is the
            # bid, so the long entry is the ask: fill + 2 x spread.
            long_entry = fill
            if not is_long:
                sp = getattr(r, "spread_pips", None)
                if sp is not None and np.isfinite(float(sp)):
                    long_entry = fill + 2.0 * float(sp) * pip
            lres = simulate_trade(
                symbol=symbol, side="BUY", entry_idx=i, fill=long_entry,
                sl=long_entry - risk, geom=Geometry(tp_r=tp_r), money_per_unit=money,
                bars=bars, cost_price_equiv=0.0, slippage_price_equiv=slip_price, spec=spec,
            )
            if lres is not None:
                long_pnl.append(float(lres.pnl_r))

        if not pnl:
            continue
        arr = np.asarray(pnl, dtype=float)
        m = metrics(arr, REALISED_RISK_PCT)
        tot = max(sum(shares.values()), 1)
        m["share_sl"] = shares["sl"] / tot
        m["share_tp"] = shares["tp"] / tot
        m["share_time_stop"] = shares["time"] / tot
        m["avg_win_over_tp"] = float(m["avg_win_r"] / tp_r) if tp_r else 0.0
        if long_pnl:
            lm = float(np.mean(long_pnl))
            m["always_long_mean_r"] = lm
            m["edge_over_always_long"] = float(m["expectancy_r"] - lm)
            m["beats_always_long"] = bool(m["edge_over_always_long"] > 0)
        out[f"k={k:.2f}"] = m
    return out


# ── main ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="A", help="comma list of A,B,C,D,E")
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--window", type=int, default=WINDOW_DEFAULT)
    ap.add_argument("--symbols", default="")
    ap.add_argument(
        "--out", default=os.path.join(REPO, "reports", "p0_3_exit_geometry.json")
    )
    args = ap.parse_args()

    parts = {p.strip().upper() for p in args.parts.split(",") if p.strip()}
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(
            d for d in os.listdir(REAL_DIR) if os.path.isdir(os.path.join(REAL_DIR, d))
        )

    report: Dict[str, object] = {
        "generated": pd.Timestamp.utcnow().isoformat(),
        "tf": args.tf,
        "window_days": args.window,
        "parts": sorted(parts),
        "symbols": {},
    }

    for sym in symbols:
        df, cands = load_symbol(sym, args.tf, args.window)
        if df is None:
            print(f"[skip] {sym}: no data for {args.tf} {args.window}d", flush=True)
            continue
        spec = resolve(sym)
        pip = float(spec.pip_size or 0.0001)
        slip = 0.5 * pip  # engine default, engine.py:30
        entry: Dict[str, object] = {"bars": int(len(df))}

        if "A" in parts:
            entry["noise_band"] = noise_band(df)
        if "B" in parts or "C" in parts:
            cands_r = dynamic_regimes(df, cands)
            entry["candidates"] = int(len(cands_r))
            if "B" in parts:
                entry["tp_sweep"] = sweep_tp(sym, df, cands_r, slip)
            if "C" in parts:
                entry["max_bars_sweep"] = sweep_max_bars(sym, df, cands_r, slip)
        if "D" in parts:
            entry["cost_gate"] = cost_gate(sym, df, cands, slip)
        if "E" in parts:
            entry["stop_multiple_sweep"] = sweep_stop_multiple(sym, df, cands, slip)

        report["symbols"][sym] = entry

        if "A" in parts:
            nb = entry.get("noise_band", {})
            q = nb.get("body_quantiles", {})
            adv = nb.get("adverse_quantiles", {})
            print(
                f"[A] {sym:8s} body q50={q.get('q50', 0):.3f} q75={q.get('q75', 0):.3f} "
                f"q90={q.get('q90', 0):.3f} q95={q.get('q95', 0):.3f} | "
                f"adverse q90={adv.get('q90', 0):.3f} | "
                f"corr(k, ATR%ile)={nb.get('corr_rolling_k_vs_atr_percentile', 0):+.3f}",
                flush=True,
            )
        if "D" in parts:
            cg = entry.get("cost_gate", {})
            print(
                f"[D] {sym:8s} median cost={cg.get('median_cost_r', 0):.4f}R "
                f"q90={cg.get('q90_cost_r', 0):.4f}R "
                f">20% of 1R: {cg.get('share_over_20pct_of_1R', 0) * 100:.1f}%",
                flush=True,
            )
        if "B" in parts:
            ov = entry.get("tp_sweep", {}).get("overall", {})
            # Rank by the only attributable quantity, not by raw expectancy.
            best = max(
                ((k, v.get("edge_over_always_long", -9e9)) for k, v in ov.items() if v.get("n")),
                key=lambda kv: kv[1],
                default=("n/a", 0.0),
            )
            cur = ov.get("tp1.5", {})
            nbeat = sum(1 for v in ov.values() if v.get("beats_always_long"))
            print(
                f"[B] {sym:8s} best-edge tp={best[0]} edge={best[1]:+.4f}R | "
                f"tp1.5 E={cur.get('expectancy_r', 0):+.4f} "
                f"long={cur.get('always_long_mean_r', float('nan')):+.4f} "
                f"edge={cur.get('edge_over_always_long', 0):+.4f}R | "
                f"beats long at {nbeat}/{len(ov)} tp levels",
                flush=True,
            )
        if "C" in parts:
            ms = entry.get("max_bars_sweep", {})
            row = ms.get("mb200_tp3", {})
            print(
                f"[C] {sym:8s} mb200/tp3 avg_win/tp={row.get('avg_win_over_tp', 0):.3f} "
                f"time_stop={row.get('share_time_stop', 0) * 100:.1f}% "
                f"edge_over_long={row.get('edge_over_always_long', 0):+.4f}R",
                flush=True,
            )
        if "E" in parts:
            sw = entry.get("stop_multiple_sweep", {})
            parts_txt = []
            for k, v in sw.items():
                parts_txt.append(
                    f"{k}: E={v.get('expectancy_r', 0):+.3f} "
                    f"edge={v.get('edge_over_always_long', float('nan')):+.3f}"
                )
            best_k = max(
                ((k, v.get("edge_over_always_long", -9e9)) for k, v in sw.items() if v.get("n")),
                key=lambda kv: kv[1], default=("n/a", 0.0),
            )
            print(f"[E] {sym:8s} best-edge {best_k[0]} ({best_k[1]:+.4f}R)", flush=True)
            for t in parts_txt:
                print(f"      {t}", flush=True)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
