#!/usr/bin/env python
"""Spread-symmetry A/B — what does making the BUY stop symmetric with SELL cost?

WHAT THIS MEASURES
------------------
`jarvis/intelligence/dynamic_levels.py` adds ``spread_dist`` to the structural
stop distance in the SELL branch (``:363``, ``:365`` and the style caps at
``:368/:373/:377/:381/:399``) but not in the BUY branch. The proposed fix adds
``spread_dist`` to the BUY branch at the mirrored sites, which widens every BUY
stop. This tool measures that widening and its P&L consequence before shipping.

EXACT WIDENING (no approximation for the SWING candidates stored here)
---------------------------------------------------------------------
Every candidate table was produced by ``SignalScanner``, which calls
``DecisionEngine.evaluate`` without a ``trade_style`` argument, so the style is
``SWING``. For SWING the map from the pre-spread structural distance ``x`` to the
final stop distance is

    g(x) = min(M, max(mf, x))       (M = sl_atr_multiplier * ATR, mf = floor)
    d(x) = max(g(x), F)             (F = max(3 * spread, 0.10 * ATR))

The fix replaces ``x`` by ``x + s`` and ``M`` by ``M + s`` (s = spread_dist),
i.e. ``g'(x) = min(M+s, max(mf, x+s))``. Because the interior slope is exactly 1,

    g'(x) - g(x) = s   for every x with g(x) > F

so ``d_post = d_pre + s`` whenever the stop floor at ``:273``/``:404`` does not
bind. When the floor *does* bind the widening lies in ``[0, s]``; ``s`` is used
there, i.e. the reported effect is an UPPER BOUND for those (rare) trades.

The candidate table's ``risk_dist`` column is verified equal to ``|fill - sl|``,
so ``d_pre`` is read directly and no pre-floor quantity is invented.

ARMS
----
A = pre-fix  : BUY stop unchanged, SELL unchanged (the incumbent).
B = post-fix : BUY stop distance + spread_dist, SELL unchanged.

Both arms replay every candidate through the same ``trade_simulator`` with the
same cost model, so the only moving part is the BUY stop distance.

TARGET CONVENTIONS (reported side by side)
------------------------------------------
``rr``  target = stored per-candidate R:R applied to the NEW risk distance
        (production's fallback + structural cap move the target out with risk).
``tp15`` target = 1.5 * new risk distance (the production default multiple).
``abs`` target = the candidate's stored absolute tp price (price fixed; the
        harsher reading where widening the stop mechanically lowers R:R).

Read-only with respect to ``jarvis/``. Writes only under ``.scratch/``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional

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

REAL_DIR = os.path.join(REPO, "data", "market", "real")
SIGNAL_DIR = os.path.join(REPO, "data", "signals")
SLIP_PIPS_DEFAULT = 0.5
FLOOR_SPREAD_MULT = 3.0
FLOOR_ATR_MULT = 0.10


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


def widening_stats(cands: pd.DataFrame, pip: float) -> Dict[str, Any]:
    """Per-candidate BUY stop widening: exact ``s`` when the floor does not bind."""
    side = cands["side"].astype(str).str.upper()
    buy = side.str.startswith("BUY").to_numpy()
    sp_price = cands["spread_pips"].to_numpy(float) * pip
    atr = cands["atr"].to_numpy(float)
    r = cands["risk_dist"].to_numpy(float)
    F = np.maximum(FLOOR_SPREAD_MULT * sp_price, FLOOR_ATR_MULT * atr)
    floor_binds = r <= F * (1.0 + 1e-9)
    delta = np.where(buy, sp_price, 0.0)
    denom = np.where(r > 0, r, np.nan)
    return {
        "n_buy": int(buy.sum()),
        "n_sell": int((~buy).sum()),
        "buy_floor_binding_share": round(float(floor_binds[buy].mean()), 5) if buy.any() else 0.0,
        "buy_delta_pips_median": round(float(np.median(sp_price[buy] / pip)), 5) if buy.any() else 0.0,
        "buy_delta_pips_mean": round(float(np.mean(sp_price[buy] / pip)), 5) if buy.any() else 0.0,
        "buy_delta_over_atr_median": round(float(np.median(sp_price[buy] / atr[buy])), 5) if buy.any() else 0.0,
        "buy_delta_over_atr_mean": round(float(np.mean(sp_price[buy] / atr[buy])), 5) if buy.any() else 0.0,
        "buy_risk_over_atr_median": round(float(np.median(r[buy] / atr[buy])), 5) if buy.any() else 0.0,
        "buy_relative_widening_median": round(float(np.median(sp_price[buy] / denom[buy])), 5) if buy.any() else 0.0,
        "buy_relative_widening_mean": round(float(np.mean(sp_price[buy] / denom[buy])), 5) if buy.any() else 0.0,
        "_delta": delta,
        "_buy": buy,
    }


def replay(symbol: str, df: pd.DataFrame, cands: pd.DataFrame, delta: np.ndarray, *,
           tp_mode: str, slip_price: float, spec: Any, money: float) -> Optional[Dict[str, Any]]:
    bars = BarArrays.from_df(df)
    n = len(df)
    rows: List[Dict[str, Any]] = []
    for k, r in enumerate(cands.itertuples(index=False)):
        i = int(getattr(r, "bar_idx"))
        if i + 1 >= n:
            continue
        side = str(getattr(r, "side", "")).upper()
        if side not in ("BUY", "SELL"):
            continue
        fill = float(getattr(r, "fill"))
        atr = float(getattr(r, "atr"))
        if not np.isfinite(atr) or atr <= 0:
            continue
        d0 = abs(fill - float(getattr(r, "sl")))
        d_new = d0 + float(delta[k])
        if d_new <= 0:
            continue
        if tp_mode == "abs":
            tp_eff = abs(float(getattr(r, "tp")) - fill) / d_new
            if not np.isfinite(tp_eff) or tp_eff <= 0:
                continue
            geom = Geometry(tp_r=float(tp_eff))
        elif tp_mode == "rr":
            rr = float(getattr(r, "rr", 0.0) or 0.0)
            if not np.isfinite(rr) or rr <= 0:
                continue
            geom = Geometry(tp_r=float(rr))
        else:  # tp15
            geom = Geometry(tp_r=1.5)
        new_sl = fill - d_new if side == "BUY" else fill + d_new
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
        rows.append({
            "symbol": symbol, "cand_idx": int(k), "pair_key": f"{symbol}:{int(k)}",
            "bar_idx": i, "side": side,
            "pnl_r": float(out.pnl_r), "result": str(out.result),
            "risk_dist": float(out.risk_dist), "risk_x_atr": float(out.risk_dist) / atr,
            "entry_idx": int(out.entry_idx), "exit_idx": int(out.exit_idx),
            "widened": bool(d_new > d0 + 1e-15),
        })
    if not rows:
        return None
    return pd.DataFrame(rows)


def paired_stats(a: pd.DataFrame, b: pd.DataFrame, key: str = "cand_idx") -> Dict[str, Any]:
    """Per-trade paired difference B - A on the trades both arms share.

    The arms replay the same candidates, so the informative quantity is the
    distribution of the paired difference, not the difference of two pooled means
    (which two independent samples of this size would resolve to ~0 anyway).
    """
    m = a[[key, "pnl_r", "result"]].merge(b[[key, "pnl_r", "result"]], on=key, suffixes=("_a", "_b"))
    if len(m) == 0:
        return {"n": 0}
    diff = (m["pnl_r_b"] - m["pnl_r_a"]).to_numpy(float)
    sd = float(diff.std(ddof=1)) if len(diff) > 1 else 0.0
    # A trade "changes fate" only when its exit REASON changes (SL <-> TP <-> TIME_STOP).
    # Most of the 40%+ of rows with a non-zero R delta are stop-outs whose R moved by the
    # slippage normalisation only (slip/risk_dist), which is not a change of outcome.
    fate = (m["result_a"] != m["result_b"]).to_numpy()
    return {
        "n": int(len(diff)),
        "mean_diff_r": round(float(diff.mean()), 6),
        "sd_diff_r": round(sd, 5),
        "t_stat": round(float(diff.mean() / (sd / math.sqrt(len(diff)))), 3) if sd > 1e-12 else 0.0,
        "pct_r_changed": round(float((np.abs(diff) > 1e-12).mean()), 4),
        "pct_fate_changed": round(float(fate.mean()), 4),
        "n_fate_changed": int(fate.sum()),
        "pct_r_changed_by_gt_0p01": round(float((np.abs(diff) > 0.01).mean()), 4),
        "pct_improved": round(float((diff > 1e-12).mean()), 4),
        "pct_worsened": round(float((diff < -1e-12).mean()), 4),
    }


def metrics(rows: pd.DataFrame) -> Dict[str, Any]:
    r = rows["pnl_r"].to_numpy(float)
    n = len(r)
    if n == 0:
        return {"trades": 0}
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    mean_r = float(r.mean())
    curve = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))
    return {
        "trades": int(n),
        "stop_rate": round(float((rows["result"] == "SL").mean()), 4),
        "target_rate": round(float((rows["result"] == "TP").mean()), 4),
        "win_rate": round(float((r > 0).mean()), 4),
        "expectancy_r": round(mean_r, 5),
        "total_r": round(float(r.sum()), 3),
        "t_stat": round(float(mean_r / (sd / math.sqrt(n))), 3) if sd > 1e-12 else 0.0,
        "max_dd_r": round(float((peak[1:] - curve).max()), 3),
        "mean_risk_x_atr": round(float(rows["risk_x_atr"].mean()), 4),
        "pct_widened": round(float(rows["widened"].mean()), 4),
    }


def sequential_total(rows: pd.DataFrame, lo: int, hi: int) -> Dict[str, Any]:
    """One-position-at-a-time walk over a contiguous entry window (selection check)."""
    sub = rows[(rows["entry_idx"] >= lo) & (rows["entry_idx"] <= hi)].sort_values("entry_idx")
    chosen: List[pd.Series] = []
    blocked = -1
    for _, row in sub.iterrows():
        if row["entry_idx"] <= blocked:
            continue
        chosen.append(row)
        blocked = int(row["exit_idx"])
    if not chosen:
        return {"trades": 0, "total_r": 0.0, "expectancy_r": 0.0}
    rr = np.array([float(c["pnl_r"]) for c in chosen])
    return {"trades": len(rr), "total_r": round(float(rr.sum()), 3),
            "expectancy_r": round(float(rr.mean()), 5)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--window", type=int, default=183)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--slip-pips", type=float, default=SLIP_PIPS_DEFAULT)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    args.out = args.out or os.path.join(REPO, ".scratch", f"spread_symmetry_ab_{args.tf}_{args.window}d.json")

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else
               sorted(d for d in os.listdir(REAL_DIR) if os.path.isdir(os.path.join(REAL_DIR, d))))

    per_symbol: Dict[str, Any] = {}
    pooled: Dict[str, Dict[str, List[pd.DataFrame]]] = {}
    skipped: List[str] = []

    print(f"SPREAD-SYMMETRY A/B  tf={args.tf} window={args.window}d  slip={args.slip_pips}pip")
    print(f"{'symbol':8} {'nBUY':>6} {'dPips':>7} {'d/ATR':>7} {'d/risk':>7} {'floorB':>7} "
          f"{'A_E':>9} {'B_E':>9} {'dE':>9} {'pairedT':>8}")

    for sym in symbols:
        df, cands = load_symbol(sym, args.tf, args.window)
        if df is None:
            skipped.append(sym)
            continue
        spec = resolve(sym)
        pip = float(getattr(spec, "pip_size", 0.0001) or 0.0001)
        slip = args.slip_pips * pip
        money = get_dollar_risk_per_price_unit(sym, None)

        w = widening_stats(cands, pip)
        delta = w.pop("_delta")
        w.pop("_buy")  # drop the key from `w` before it is embedded below (value itself unused)

        entry = {"widening": w, "targets": {}}
        for tp_mode in ("rr", "tp15", "abs"):
            a = replay(sym, df, cands, np.zeros(len(cands)), tp_mode=tp_mode,
                       slip_price=slip, spec=spec, money=money)
            b = replay(sym, df, cands, delta, tp_mode=tp_mode,
                       slip_price=slip, spec=spec, money=money)
            if a is None or b is None:
                continue
            ma, mb = metrics(a), metrics(b)
            # BUY-only subset, where the change actually lives.
            ab, bb = a[a["side"] == "BUY"], b[b["side"] == "BUY"]
            sel_a = sequential_total(a, int(a["entry_idx"].min()), int(a["entry_idx"].max()))
            sel_b = sequential_total(b, int(b["entry_idx"].min()), int(b["entry_idx"].max()))
            entry["targets"][tp_mode] = {
                "pooled": {"A": ma, "B": mb, "d_expectancy_r": round(mb.get("expectancy_r", 0) - ma.get("expectancy_r", 0), 5),
                           "d_total_r": round(mb.get("total_r", 0) - ma.get("total_r", 0), 3)},
                "buy_only": {"A": metrics(ab) if len(ab) else {"trades": 0},
                             "B": metrics(bb) if len(bb) else {"trades": 0}},
                "paired": paired_stats(a, b),
                "paired_buy": paired_stats(ab, bb),
                "sequential": {"A": sel_a, "B": sel_b,
                               "d_trades": sel_b["trades"] - sel_a["trades"],
                               "d_total_r": round(sel_b["total_r"] - sel_a["total_r"], 3)},
            }
            pooled.setdefault(tp_mode, {"A": [], "B": []})
            pooled[tp_mode]["A"].append(a)
            pooled[tp_mode]["B"].append(b)
        per_symbol[sym] = entry

        e = entry["targets"].get("rr", {})
        pe = e.get("pooled", {})
        pr = e.get("paired", {})
        print(f"{sym:8} {w['n_buy']:>6} {w['buy_delta_pips_median']:>7.2f} "
              f"{w['buy_delta_over_atr_median']:>7.3f} {w['buy_relative_widening_median']:>7.3f} "
              f"{w['buy_floor_binding_share'] * 100:>6.2f}% "
              f"{pe.get('A', {}).get('expectancy_r', 0):>+9.5f} "
              f"{pe.get('B', {}).get('expectancy_r', 0):>+9.5f} "
              f"{pe.get('d_expectancy_r', 0):>+9.5f} "
              f"{pr.get('t_stat', 0):>+8.2f}", flush=True)

    # pooled across symbols (row concat)
    pooled_out: Dict[str, Any] = {}
    for tp_mode, arms in pooled.items():
        if not arms["A"]:
            continue
        pa = pd.concat(arms["A"], ignore_index=True)
        pb = pd.concat(arms["B"], ignore_index=True)
        pooled_out[tp_mode] = {
            "A": metrics(pa), "B": metrics(pb),
            "d_expectancy_r": round(metrics(pb)["expectancy_r"] - metrics(pa)["expectancy_r"], 5),
            "d_total_r": round(metrics(pb)["total_r"] - metrics(pa)["total_r"], 3),
            "buy_only_A": metrics(pa[pa["side"] == "BUY"]),
            "buy_only_B": metrics(pb[pb["side"] == "BUY"]),
            "paired": paired_stats(pa, pb, key="pair_key"),
            "paired_buy": paired_stats(pa[pa["side"] == "BUY"], pb[pb["side"] == "BUY"], key="pair_key"),
        }

    payload = {
        "generated": pd.Timestamp.now("UTC").isoformat(),
        "config": {"tf": args.tf, "window_days": args.window, "slip_pips": args.slip_pips,
                   "symbols": list(per_symbol), "skipped": skipped,
                   "floor": "max(3*spread_dist, 0.10*ATR)  (unchanged by the fix)"},
        "per_symbol": per_symbol,
        "pooled": pooled_out,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    print("\nPOOLED (rr mode = stored per-trade R:R):")
    for tp_mode, p in pooled_out.items():
        print(f"  {tp_mode:5} A: n={p['A']['trades']} E={p['A']['expectancy_r']:+.5f} "
              f"totR={p['A']['total_r']:+.1f} | B: n={p['B']['trades']} E={p['B']['expectancy_r']:+.5f} "
              f"totR={p['B']['total_r']:+.1f} | dE={p['d_expectancy_r']:+.5f} dtotR={p['d_total_r']:+.1f}")
        pq = p["paired"]
        print(f"        paired: n={pq['n']} mean={pq['mean_diff_r']:+.6f} sd={pq['sd_diff_r']:.5f} "
              f"t={pq['t_stat']:+.2f} fate_changed={pq['n_fate_changed']} ({pq['pct_fate_changed'] * 100:.2f}%) "
              f"R_changed>0.01R={pq['pct_r_changed_by_gt_0p01'] * 100:.2f}% "
              f"improved={pq['pct_improved'] * 100:.2f}% worsened={pq['pct_worsened'] * 100:.2f}%")
        print(f"        BUY-only A: E={p['buy_only_A']['expectancy_r']:+.5f} "
              f"(risk/ATR {p['buy_only_A']['mean_risk_x_atr']:.3f}, widened {p['buy_only_A']['pct_widened'] * 100:.1f}%) "
              f"| B: E={p['buy_only_B']['expectancy_r']:+.5f} "
              f"(risk/ATR {p['buy_only_B']['mean_risk_x_atr']:.3f})")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
