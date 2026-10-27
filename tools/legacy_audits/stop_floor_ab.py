#!/usr/bin/env python
"""Stop-floor A/B/C — does raising the stop floor actually help?

WHAT THIS MEASURES
------------------
The live stop sits inside the noise band: `jarvis/intelligence/dynamic_levels.py`
floors the stop distance at ``max(3 * spread, 0.10 * ATR)`` (BUY at :273, SELL at
:403) and `institutional_entry_engine.py:131,341` floors at only ``pip_size * 5``.
The proposed one-line change raises the ATR term to ``1.11 * ATR``.

That change is *mechanical*: a wider stop cannot be stopped out by the entry
bar's own noise as often. But widening a stop on a signal with no edge is also a
classic way to lose MORE money, because every loser now costs more. This tool
measures the trade-off instead of assuming either outcome.

THE EXPERIMENT
--------------
Vary ONE variable — the stop floor — and hold everything else fixed:
entry signal, entry bar, entry fill, target rule and cost model are untouched.

For a candidate whose stored stop already sits at distance ``d0 = |fill - sl|``,
raising the floor to ``k * ATR`` gives exactly

    d_new = max(d0, k * ATR)

which is algebraically identical to re-running production with the new floor
(``max(underlying, old_floor, new_floor) == max(underlying, new_floor)`` whenever
``new_floor >= old_floor``). No pre-floor quantity is invented.

Arm A is ``k = 0.10`` (the incumbent ATR term). Arm B is ``k = 1.11`` (proposed).
The full sweep ``0.10 .. 1.50`` is reported so the shape of the curve — and the
break-even — is visible rather than asserted.

TWO TARGET CONVENTIONS
----------------------
``--tp-mode rr``  target = ``tp_r * risk`` (production fallback + the audit's
                  established basis; the target multiple is held fixed, so the
                  target moves out with the stop).
``--tp-mode abs`` target = the candidate's own stored absolute ``tp`` price
                  (the target is held fixed in price, so widening the stop
                  mechanically LOWERS the R:R). This is the harsher, more
                  literal reading of "hold the target fixed" and is reported as
                  a robustness check.

SPREAD SEMANTICS (verified before use, per repo incident history)
-----------------------------------------------------------------
The parquet ``spread`` column is MT5 **integer points**, not pips. The candidate
tables store ``spread_pips`` already converted by ``signal_scan._spread_for_bar``
as ``raw * 10**-digits / pip_size``. This tool consumes the **stored
``spread_pips``** column and never re-derives spread from the raw column, so the
cost model cannot double-charge. Spot checks: EURUSD raw 19 -> 1.9 pips (digits
5, pip 1e-4); XAUUSD raw 16 -> 1.6 pips (pip 0.1); US500 raw 55 -> 0.55 pips
(pip 1.0); BTCUSD raw 2250 -> 2250 (point == pip). ``spread_price`` is then
``spread_pips * pip_size``, which equals ``raw * point`` on every feed checked.

COST MODEL (identical in every arm — this is what makes the A/B valid)
---------------------------------------------------------------------
Entry fill already carries the spread (one side, ``fills.entry_fill``). Protective
stop exits pay the engine default ``0.5 pip`` slippage; target fills pay none.
Commission is 0. The tool reports cost per trade in PRICE terms per arm so a
reviewer can confirm the cost model did not silently stop charging the spread
while the R-normalised cost drag fell (a wider stop makes a fixed price cost a
smaller fraction of 1R — a mechanical, edge-free improvement).

Read-only with respect to ``jarvis/``. Writes only under ``reports/``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from jarvis.backtesting.trade_simulator import (  # noqa: E402
    BarArrays,
    Geometry,
    simulate_trade,
)
from jarvis.data.symbol_registry import (  # noqa: E402
    get_dollar_risk_per_price_unit,
    resolve,
)
from jarvis.learning.deflated_sharpe import (  # noqa: E402
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    sharpe_stats,
)
from jarvis.learning.sample_weights import SampleUniquenessWeightEngine  # noqa: E402

REAL_DIR = os.path.join(REPO, "data", "market", "real")
SIGNAL_DIR = os.path.join(REPO, "data", "signals")

BARS_PER_YEAR_H1 = 24 * 252
SLIP_PIPS_DEFAULT = 0.5          # engine.py default, matches audit_trade_quality
TP_R_DEFAULT = 1.5               # production tp_r, matches audit + DSR tools
ARM_A = 0.10                     # incumbent ATR term
ARM_B = 1.11                     # proposed ATR term
ARM_FLOORS_DEFAULT = (0.10, 0.30, 0.50, 0.70, 0.85, 1.00, 1.11, 1.30, 1.50)
DSR_TP_GRID_DEFAULT = (1.0, 1.5, 2.0, 3.0)


# ─────────────────────────────────────────────────────────────────────────────
# data
# ─────────────────────────────────────────────────────────────────────────────
def load_symbol(symbol: str, tf: str, window: int):
    bars_path = os.path.join(REAL_DIR, symbol, f"{symbol}_{tf}_{window}d.parquet")
    cand_path = os.path.join(SIGNAL_DIR, f"{symbol}_{tf}_{window}d_candidates.parquet")
    if not (os.path.exists(bars_path) and os.path.exists(cand_path)):
        return None, None
    df = pd.read_parquet(bars_path).sort_values("time").reset_index(drop=True)
    cands = pd.read_parquet(cand_path).sort_values("bar_idx").reset_index(drop=True)
    if "atr" not in cands.columns:
        return None, None
    return df, cands


def instant_stopout_rates(df: pd.DataFrame, cands: pd.DataFrame,
                          ks: Sequence[float]) -> Dict[str, float]:
    """Share of trades whose stop is breached by the ENTRY BAR's own adverse range.

    This is the audit's headline statistic ("16.9% at k=0.85, 10% at k=1.11"),
    measured independently: the trade goes live on bar ``i+1`` (the fill bar), so
    a BUY is stopped by that bar's own low if ``fill - low[i+1] >= k * ATR``. No
    simulation and no forward path involved — pure geometry on the entry bar.
    """
    low = df["low"].to_numpy(float)
    high = df["high"].to_numpy(float)
    n = len(df)
    idx = cands["bar_idx"].to_numpy(int)
    fill = cands["fill"].to_numpy(float)
    atr = cands["atr"].to_numpy(float)
    buy = cands["side"].astype(str).str.upper().str.startswith("BUY").to_numpy()
    keep = (idx + 1 < n) & np.isfinite(atr) & (atr > 0)
    idx, fill, atr, buy = idx[keep], fill[keep], atr[keep], buy[keep]
    j = idx + 1
    adverse = np.where(buy, fill - low[j], high[j] - fill)
    out: Dict[str, float] = {}
    for k in ks:
        out[f"{k:g}"] = round(float((adverse >= float(k) * atr).mean()), 4) if len(atr) else 0.0
    return out


def arm_stop(fill: float, sl: float, atr: float, side: str, k: float,
             stop_mode: str = "floor") -> Tuple[float, float]:
    """Stop for arm ``k`` and the resulting risk distance.

    ``floor`` -> ``d = max(|fill - sl|, k * ATR)``  : exactly the proposed change
                 applied on top of the stored (already floored) stop.
    ``width`` -> ``d = k * ATR``                    : pure stop-width curve, a
                 counterfactual that answers "if the floor were the binding term,
                 how does expectancy move with width?" — the break-even question.
    """
    d0 = abs(float(fill) - float(sl))
    d = float(k) * float(atr) if stop_mode == "width" else max(d0, float(k) * float(atr))
    return (float(fill) - d if str(side).upper().startswith("BUY") else float(fill) + d), d


# ─────────────────────────────────────────────────────────────────────────────
# simulation
# ─────────────────────────────────────────────────────────────────────────────
def replay_arm(
    symbol: str, df: pd.DataFrame, cands: pd.DataFrame, k: float, *,
    tp_r: float, tp_mode: str, slip_price: float, spec: Any, money: float,
    stop_mode: str = "floor",
) -> Optional[Dict[str, Any]]:
    """Replay every candidate under floor ``k``; return the R series + trade rows."""
    bars = BarArrays.from_df(df)
    n = len(df)
    rows: List[Dict[str, Any]] = []
    r_out: List[float] = []
    spans: List[Dict[str, Any]] = []

    for r in cands.itertuples(index=False):
        i = int(getattr(r, "bar_idx"))
        if i + 1 >= n:
            continue
        side = str(getattr(r, "side", "")).upper()
        if side not in ("BUY", "SELL"):
            continue
        fill = float(getattr(r, "fill"))
        sl = float(getattr(r, "sl"))
        atr = float(getattr(r, "atr"))
        if not np.isfinite(atr) or atr <= 0:
            continue
        d0 = abs(fill - sl)
        new_sl, d_new = arm_stop(fill, sl, atr, side, k, stop_mode)
        if d_new <= 0:
            continue

        if tp_mode == "abs":
            tp_eff = abs(float(getattr(r, "tp")) - fill) / d_new
            if not np.isfinite(tp_eff) or tp_eff <= 0:
                continue
            geom = Geometry(tp_r=float(tp_eff))
        else:
            geom = Geometry(tp_r=float(tp_r))

        out = simulate_trade(
            symbol=symbol, side=side, entry_idx=i, fill=fill, sl=new_sl, geom=geom,
            money_per_unit=money, bars=bars, cost_price_equiv=0.0,
            slippage_price_equiv=slip_price,
            ai_score=float(getattr(r, "score", 0.0) or 0.0),
            calibrated_win_p=float(getattr(r, "score", 0.0) or 0.0),
            regime=str(getattr(r, "regime", "UNKNOWN")),
            strategy=str(getattr(r, "strategy", "UNKNOWN")),
            zone=str(getattr(r, "zone", "UNKNOWN")), spec=spec,
        )
        if out is None:
            continue

        res = str(out.result)
        rows.append({
            "bar_idx": i, "side": side, "pnl_r": float(out.pnl_r), "result": res,
            "risk_dist": float(out.risk_dist), "risk_x_atr": float(out.risk_dist) / atr,
            "money_per_lot": float(out.pnl_money_per_lot),
            "changed": bool(d_new > d0 + 1e-15),
            # Price actually charged for this trade's cost (stop slippage only).
            "cost_price": float(slip_price) if res == "SL" else 0.0,
        })
        r_out.append(float(out.pnl_r))
        spans.append({
            "entry_bar": int(out.entry_idx),
            "duration_bars": max(1, int(out.exit_idx) - int(out.entry_idx) + 1),
            "pnl": float(out.pnl_r),
        })

    if not r_out:
        return None
    return {"r": np.asarray(r_out, float), "rows": pd.DataFrame(rows), "spans": spans}


def forced_long_arm(
    symbol: str, df: pd.DataFrame, cands: pd.DataFrame, k: float, *,
    tp_r: float, slip_price: float, spec: Any, money: float,
    stop_mode: str = "floor",
) -> Optional[np.ndarray]:
    """Always-LONG control: same entries, same risk distance as arm ``k``.

    Mirrors ``tools/p0_1_direction_audit.forced_long_benchmark``: a SELL row's long
    entry is ``fill + 2 * spread`` so the control does not inherit a free
    half-spread, and the stop is the same distance below the long entry.
    """
    bars = BarArrays.from_df(df)
    n = len(df)
    pip = float(getattr(spec, "pip_size", 0.0001) or 0.0001)
    geom = Geometry(tp_r=float(tp_r))
    out_r: List[float] = []
    for r in cands.itertuples(index=False):
        i = int(getattr(r, "bar_idx"))
        if i + 1 >= n:
            continue
        fill = float(getattr(r, "fill"))
        sl = float(getattr(r, "sl"))
        atr = float(getattr(r, "atr"))
        if not np.isfinite(atr) or atr <= 0:
            continue
        _, risk = arm_stop(fill, sl, atr, "BUY", k, stop_mode)
        if risk <= 0:
            continue
        side = str(getattr(r, "side", "")).upper()
        long_entry = fill
        if side == "SELL":
            sp = getattr(r, "spread_pips", None)
            if sp is not None and np.isfinite(float(sp)):
                long_entry = fill + 2.0 * float(sp) * pip
        res = simulate_trade(
            symbol=symbol, side="BUY", entry_idx=i, fill=long_entry,
            sl=long_entry - risk, geom=geom, money_per_unit=money, bars=bars,
            cost_price_equiv=0.0, slippage_price_equiv=slip_price, spec=spec,
        )
        if res is None:
            continue
        out_r.append(float(res.pnl_r))
    return np.asarray(out_r, float) if out_r else None


# ─────────────────────────────────────────────────────────────────────────────
# metrics
# ─────────────────────────────────────────────────────────────────────────────
def metrics_from_rows(rows: pd.DataFrame, baseline_r: Optional[np.ndarray] = None) -> Dict[str, Any]:
    r = rows["pnl_r"].to_numpy(float)
    n = len(r)
    if n == 0:
        return {"trades": 0}
    wins = r[r > 0]
    losses = r[r <= 0]
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    mean_r = float(r.mean())
    curve = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))
    dd = float((peak[1:] - curve).max())
    m: Dict[str, Any] = {
        "trades": int(n),
        "stop_out_rate": round(float((rows["result"] == "SL").mean()), 4),
        "target_rate": round(float((rows["result"] == "TP").mean()), 4),
        "time_stop_rate": round(float(rows["result"].str.startswith("TIME_STOP").mean()), 4),
        "close_at_end_rate": round(float((rows["result"] == "CLOSE_AT_END").mean()), 4),
        "win_rate": round(float(len(wins) / n), 4),
        "expectancy_r": round(mean_r, 5),
        "total_r": round(float(r.sum()), 3),
        "t_stat": round(float(mean_r / (sd / math.sqrt(n))), 3) if sd > 1e-12 else 0.0,
        "max_dd_r": round(dd, 3),
        "avg_win_r": round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.mean()), 4) if len(losses) else 0.0,
        "avg_loss_money_per_lot": round(float(rows.loc[rows["pnl_r"] <= 0, "money_per_lot"].mean()), 4)
        if int((rows["pnl_r"] <= 0).sum()) else 0.0,
        "median_risk_x_atr": round(float(rows["risk_x_atr"].median()), 4),
        "mean_risk_x_atr": round(float(rows["risk_x_atr"].mean()), 4),
        "pct_risk_below_0p85atr": round(float((rows["risk_x_atr"] < 0.85).mean()), 4),
        "pct_risk_below_1p11atr": round(float((rows["risk_x_atr"] < 1.11).mean()), 4),
        "pct_trades_widened": round(float(rows["changed"].mean()), 4),
        # Cost-model proof: price charged per trade must be arm-invariant.
        "cost_price_per_trade": round(float(rows["cost_price"].mean()), 8),
        "cost_r_per_trade": round(float(rows["cost_price"].mean() / rows["risk_dist"].mean()), 6),
    }
    if baseline_r is not None and len(baseline_r):
        m["always_long_mean_r"] = round(float(baseline_r.mean()), 5)
        m["edge_over_always_long_r"] = round(mean_r - float(baseline_r.mean()), 5)
        m["always_long_n"] = int(len(baseline_r))
    else:
        m["always_long_mean_r"] = None
        m["edge_over_always_long_r"] = None
    return m


def dsr_block(r: np.ndarray, spans: List[Dict[str, Any]], *, n_trials: int,
              var_trial: float, tf: str) -> Dict[str, Any]:
    ppy = BARS_PER_YEAR_H1 if tf.upper() == "H1" else None
    st = sharpe_stats(r, periods_per_year=ppy)
    psr = probabilistic_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"])
    dsr = deflated_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"], n_trials, var_trial)
    n_eff = float(SampleUniquenessWeightEngine.effective_sample_size(spans))
    dsr_eff = deflated_sharpe_ratio(st["sr"], int(round(n_eff)), st["skew"], st["kurt"],
                                    n_trials, var_trial)
    return {
        "n": int(st["n"]), "n_eff": round(n_eff, 1),
        "sr_per_obs": round(float(st["sr"]), 6),
        "sr_annual": round(float(st["sr_annual"]), 4),
        "skew": round(float(st["skew"]), 4), "kurt": round(float(st["kurt"]), 4),
        "psr": round(float(psr), 6),
        "dsr": round(float(dsr), 6),
        "dsr_effective_n": round(float(dsr_eff), 6),
        "passes_dsr_095": bool(dsr > 0.95),
        "passes_dsr_095_effective_n": bool(dsr_eff > 0.95),
    }


# ─────────────────────────────────────────────────────────────────────────────
# driver
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Stop-floor A/B/C experiment")
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--window", type=int, default=183)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--floors", default=",".join(f"{k:g}" for k in ARM_FLOORS_DEFAULT))
    ap.add_argument("--tp", type=float, default=TP_R_DEFAULT)
    ap.add_argument("--tp-mode", choices=("rr", "abs"), default="rr",
                    help="rr = target multiple fixed; abs = stored absolute target fixed")
    ap.add_argument("--stop-mode", choices=("floor", "width"), default="floor",
                    help="floor = max(stored, k*ATR) (the proposed change); "
                         "width = k*ATR (pure stop-width curve)")
    ap.add_argument("--slip-pips", type=float, default=SLIP_PIPS_DEFAULT)
    ap.add_argument("--dsr-tp-grid", default=",".join(f"{t:g}" for t in DSR_TP_GRID_DEFAULT))
    ap.add_argument("--probe-only", action="store_true",
                    help="only measure the entry-bar adverse-range stop-out rate and exit")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tf = args.tf.upper()
    floors = [float(x) for x in args.floors.split(",") if x.strip()]
    dsr_grid = [float(x) for x in args.dsr_tp_grid.split(",") if x.strip()]
    args.out = args.out or os.path.join(
        REPO, "reports", f"stop_floor_ab_{tf}_{args.window}d_{args.tp_mode}_{args.stop_mode}.json")

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else
               sorted(d for d in os.listdir(REAL_DIR) if os.path.isdir(os.path.join(REAL_DIR, d))))

    print(f"STOP-FLOOR A/B/C  tf={tf} window={args.window}d  tp_mode={args.tp_mode} "
          f"stop_mode={args.stop_mode}  tp={args.tp}R  floors={floors}  slip={args.slip_pips}pip")
    print(f"arms: A={ARM_A:g} (incumbent)  B={ARM_B:g} (proposed)  C=0.85 (intermediate)  "
          f"n_symbols={len(symbols)}")

    # main sweep: keep the raw per-arm rows so pooling is a real row concat
    store: Dict[str, Dict[str, Dict[str, Any]]] = {}   # sym -> floor -> {res, bl}
    all_trial_sr: List[float] = []
    skipped: List[str] = []
    probe_rows: List[Dict[str, Any]] = []
    probe_acc: Dict[str, List[Tuple[float, int]]] = {}

    for sym in symbols:
        df, cands = load_symbol(sym, tf, args.window)
        if df is None:
            skipped.append(sym)
            print(f"  [skip] {sym}: no bars/candidates for {tf} {args.window}d")
            continue
        spec = resolve(sym)
        money = get_dollar_risk_per_price_unit(sym, None)
        pip = float(getattr(spec, "pip_size", 0.0001) or 0.0001)
        slip = args.slip_pips * pip

        probe_rates = instant_stopout_rates(df, cands, floors)
        probe_rows.append({"symbol": sym, "n": int(len(cands)), "rates": probe_rates})
        for k in floors:
            probe_acc.setdefault(f"{k:g}", []).append((probe_rates[f"{k:g}"], len(cands)))
        if args.probe_only:
            print(f"  {sym:8s} n={len(cands):5d} instant-stopout: "
                  + " ".join(f"k={k}:{probe_rates[f'{k:g}'] * 100:.1f}%" for k in floors))
            continue

        store[sym] = {}
        for k in floors:
            res = replay_arm(sym, df, cands, k, tp_r=args.tp, tp_mode=args.tp_mode,
                             slip_price=slip, spec=spec, money=money, stop_mode=args.stop_mode)
            if res is None:
                continue
            bl = forced_long_arm(sym, df, cands, k, tp_r=args.tp, slip_price=slip,
                                 spec=spec, money=money, stop_mode=args.stop_mode)
            store[sym][f"{k:g}"] = {"res": res, "bl": bl}

        # trial sharpes for an honest search deflation: arm x reduced tp grid
        for k in floors:
            for tp in dsr_grid:
                rr = replay_arm(sym, df, cands, k, tp_r=float(tp), tp_mode=args.tp_mode,
                                slip_price=slip, spec=spec, money=money,
                                stop_mode=args.stop_mode)
                if rr is None or len(rr["r"]) < 3:
                    continue
                all_trial_sr.append(float(sharpe_stats(
                    rr["r"], periods_per_year=BARS_PER_YEAR_H1 if tf == "H1" else None)["sr"]))

        a = metrics_from_rows(store[sym].get(f"{ARM_A:g}", {}).get("res", {}).get(
            "rows", pd.DataFrame()), None) if f"{ARM_A:g}" in store[sym] else {}
        b = metrics_from_rows(store[sym].get(f"{ARM_B:g}", {}).get("res", {}).get(
            "rows", pd.DataFrame()), None) if f"{ARM_B:g}" in store[sym] else {}
        print(f"  {sym:8s} A: n={a.get('trades', 0):5d} stop%={a.get('stop_out_rate', 0) * 100:5.1f} "
              f"E={a.get('expectancy_r', 0):+.4f}R | B: stop%={b.get('stop_out_rate', 0) * 100:5.1f} "
              f"E={b.get('expectancy_r', 0):+.4f}R  dE={b.get('expectancy_r', 0) - a.get('expectancy_r', 0):+.4f}R",
              flush=True)

    pooled_probe: Dict[str, float] = {}
    for key, pairs in probe_acc.items():
        tot = sum(w for _, w in pairs)
        pooled_probe[key] = round(float(sum(r * w for r, w in pairs) / tot), 4) if tot else 0.0

    if args.probe_only:
        out = {"generated": pd.Timestamp.now("UTC").isoformat(),
               "config": {"tf": tf, "window_days": args.window, "floors": floors},
               "per_symbol": probe_rows, "portfolio": pooled_probe}
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
        print("\nPORTFOLIO entry-bar adverse-range stop-out rate (pooled, n-weighted):")
        for key in [f"{k:g}" for k in floors]:
            print(f"  k={key:>5} -> {pooled_probe[key] * 100:5.2f}%")
        print(f"\nwrote {args.out}")
        return 0

    n_arms = max(1, len(floors))
    n_trials = max(1, len(dsr_grid)) * n_arms
    var_trial = float(np.var(all_trial_sr, ddof=1)) if len(all_trial_sr) > 1 else 0.0
    emax = float(expected_max_sharpe(var_trial, n_trials))

    # per-symbol metrics with DSR on the shared deflation
    per_symbol: Dict[str, Any] = {}
    for sym, arms in store.items():
        entry: Dict[str, Any] = {"arms": {}}
        for key, payload in arms.items():
            m = metrics_from_rows(payload["res"]["rows"], payload["bl"])
            m["dsr"] = dsr_block(payload["res"]["r"], payload["res"]["spans"],
                                 n_trials=n_trials, var_trial=var_trial, tf=tf)
            entry["arms"][key] = m
        per_symbol[sym] = entry

    # portfolio: pool the actual per-trade rows (proper weighted aggregation)
    portfolio: Dict[str, Any] = {}
    for k in floors:
        key = f"{k:g}"
        row_parts = [store[s][key]["res"]["rows"] for s in store if key in store[s]]
        if not row_parts:
            continue
        pooled = pd.concat(row_parts, ignore_index=True)
        bl_parts = [store[s][key]["bl"] for s in store
                    if key in store[s] and store[s][key]["bl"] is not None]
        bl = np.concatenate(bl_parts) if bl_parts else None
        p = metrics_from_rows(pooled, bl)
        spans: List[Dict[str, Any]] = []
        for s in store:
            if key in store[s]:
                spans.extend(store[s][key]["res"]["spans"])
        p["dsr"] = dsr_block(pooled["pnl_r"].to_numpy(float), spans,
                             n_trials=n_trials, var_trial=var_trial, tf=tf)
        p["symbols_in_pool"] = len(row_parts)
        portfolio[key] = p

    # break-even curve: marginal expectancy gain per floor step
    keys = [f"{k:g}" for k in floors if f"{k:g}" in portfolio]
    breakeven: Dict[str, Any] = {}
    prev_e: Optional[float] = None
    for key in keys:
        e = portfolio[key]["expectancy_r"]
        breakeven[key] = {
            "expectancy_r": e,
            "stop_out_rate": portfolio[key]["stop_out_rate"],
            "median_risk_x_atr": portfolio[key]["median_risk_x_atr"],
            "avg_loss_money_per_lot": portfolio[key]["avg_loss_money_per_lot"],
            "marginal_d_expectancy_vs_prev": (round(e - prev_e, 5) if prev_e is not None else None),
        }
        prev_e = e

    payload = {
        "generated": pd.Timestamp.now("UTC").isoformat(),
        "config": {
            "tf": tf, "window_days": args.window, "tp_mode": args.tp_mode,
            "stop_mode": args.stop_mode, "tp_r": args.tp, "slip_pips": args.slip_pips,
            "floors": floors, "arm_A": ARM_A, "arm_B": ARM_B,
            "dsr_tp_grid": dsr_grid, "n_trials_for_deflation": n_trials,
            "var_of_trial_sharpes": var_trial, "expected_max_sharpe": emax,
            "symbols": list(store.keys()), "skipped_symbols": skipped,
            "spread_semantics": ("consumes stored spread_pips column; "
                                 "spread_price = spread_pips * pip_size = raw_points * point"),
            "cost_model": f"entry fill carries spread; stops pay {args.slip_pips} pip slippage; commission 0",
            "target_convention": ("tp = tp_r * risk (multiple fixed)" if args.tp_mode == "rr"
                                  else "tp = candidate's stored absolute tp price (price fixed)"),
        },
        "per_symbol": per_symbol,
        "portfolio": portfolio,
        "breakeven_curve": breakeven,
        "instant_stopout_entry_bar": {
            "definition": "share of trades where the entry bar's own adverse range >= k*ATR",
            "per_symbol": probe_rows, "portfolio": pooled_probe,
        },
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    print(f"\nPORTFOLIO (pooled, production tp_r={args.tp}, tp_mode={args.tp_mode})")
    print(f"{'floor':>6} {'trades':>7} {'stop%':>6} {'tgt%':>6} {'WR%':>6} {'E[R]':>10} "
          f"{'totR':>9} {'avgLoss$':>10} {'medATR':>7} {'DSReff':>7} {'vsLong':>8}")
    for key in keys:
        p = portfolio[key]
        vsl = p["edge_over_always_long_r"]
        print(f"{key:>6} {p['trades']:>7} {p['stop_out_rate'] * 100:>6.1f} "
              f"{p['target_rate'] * 100:>6.1f} {p['win_rate'] * 100:>6.1f} "
              f"{p['expectancy_r']:>+10.5f} {p['total_r']:>+9.1f} "
              f"{p['avg_loss_money_per_lot']:>10.2f} {p['median_risk_x_atr']:>7.3f} "
              f"{p['dsr']['dsr_effective_n']:>7.3f} "
              f"{(vsl if vsl is not None else float('nan')):>+8.4f}")
    print(f"\n  deflation: n_trials={n_trials}  var(trial SR)={var_trial:.6f}  E[max SR]={emax:+.6f}")
    print("  cost check (must be identical across arms): "
          + ", ".join(f"{key}:{portfolio[key]['cost_price_per_trade']:.6f}" for key in keys))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
