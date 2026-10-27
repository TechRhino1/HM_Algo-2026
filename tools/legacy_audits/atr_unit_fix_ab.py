#!/usr/bin/env python
"""ATR-unit-fix A/B/C — does making ``atr_median`` same-timeframe change anything?

THE BUG
-------
``jarvis/intelligence/dynamic_levels.py:141-144``::

    typical_atr_pct = getattr(spec, "typical_atr_pct", 0.5)
    typical_atr = c_price * (typical_atr_pct / 100.0) if typical_atr_pct > 0 else atr
    atr_median = typical_atr if typical_atr > 0 else atr
    atr_ratio = min(3.0, max(0.33, atr / max(atr_median, 1e-6)))

``symbol_registry.py:22`` documents ``typical_atr_pct`` as *"Typical **daily** ATR as
% of price"*. But ``atr = vol.atr`` is the **timeframe** ATR (H1). So ``atr_ratio``
divides an hourly ATR by a daily ATR and pins at the 0.33 lower clamp.

WHAT THIS TOOL MEASURES
-----------------------
Three arms that differ ONLY in how ``atr_median`` is derived:

  A (current)      atr_median = c_price * typical_atr_pct / 100      (daily, registry)
  B (same-TF)      atr_median = trailing median of the SAME timeframe's ATR
  C (sqrt-rescale) atr_median = daily_atr / sqrt(bars_per_day)        (sqrt-time scaling)

For each arm: the ``atr_ratio`` clamp distribution, the resulting
``effective_buffer`` and stop distance, and the trade outcomes.

THE CHAIN THAT MATTERS
----------------------
``atr_ratio`` is used in exactly ONE place (line 151)::

    buffer_mult     = alpha_base + beta_vol * atr_ratio + gamma_spread * spread_ratio
    dynamic_buffer  = atr * buffer_mult
    anti_wick_buffer= atr * anti_wick_mult
    effective_buffer= max(dynamic_buffer, anti_wick_buffer)

with ``alpha_base=0.12, beta_vol=0.05, gamma_spread=0.05`` (constructor defaults)
and ``anti_wick_mult`` in {0.35, 0.40, 0.45}. So a change to ``atr_ratio`` only
propagates if ``dynamic_buffer`` can *exceed* ``anti_wick_buffer``. Whether it
can is measured here rather than asserted.

SPREAD SEMANTICS
----------------
Consumes the candidate table's stored **``spread_pips``** column (already
converted by ``signal_scan._spread_for_bar``). The raw parquet ``spread`` column
is never re-derived, so cost cannot be double-charged.

Read-only with respect to ``jarvis/``. Writes only under ``reports/``.
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

from jarvis.backtesting.signal_scan import compute_atr  # noqa: E402
from jarvis.backtesting.trade_simulator import (  # noqa: E402
    BarArrays,
    Geometry,
    simulate_trade,
)
from jarvis.data.symbol_registry import (  # noqa: E402
    get_dollar_risk_per_price_unit,
    resolve,
)
from jarvis.intelligence.symbol_profile_config import (  # noqa: E402
    get_symbol_profile_config,
)
from jarvis.learning.deflated_sharpe import (  # noqa: E402
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
    sharpe_stats,
)
from jarvis.learning.sample_weights import SampleUniquenessWeightEngine  # noqa: E402

REAL_DIR = os.path.join(REPO, "data", "market", "real")
SIGNAL_DIR = os.path.join(REPO, "data", "signals")

# Constructor defaults of DynamicRiskAndLevelsEngine (dynamic_levels.py:36-38).
ALPHA_BASE = 0.12
BETA_VOL = 0.05
GAMMA_SPREAD = 0.05

BARS_PER_YEAR_H1 = 24 * 252
SLIP_PIPS_DEFAULT = 0.5
TP_R_DEFAULT = 1.5
DSR_TP_GRID_DEFAULT = (1.0, 1.5, 2.0, 3.0)

# Timeframe -> bars per trading day, for arm C's sqrt-time rescale.
BARS_PER_DAY = {"M1": 1440, "M5": 288, "M15": 96, "H1": 24, "H4": 6, "D1": 1}
BARS_PER_YEAR = {
    "M1": 1440 * 252, "M5": 288 * 252, "M15": 96 * 252,
    "H1": 24 * 252, "H4": 6 * 252, "D1": 252,
}
GOLD_ANTI_WICK = 0.35  # dynamic_levels.py:164 — gold's anti-wick multiplier is forced


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
    if "atr" not in cands.columns or len(cands) == 0:
        return None, None
    return df, cands


def anti_wick_mult(symbol: str) -> float:
    """The asset's anti-wick ATR multiple, mirroring dynamic_levels.py:156-166."""
    sym = str(symbol).upper()
    cfg = get_symbol_profile_config(sym)
    is_gold = ("XAU" in sym) or ("GOLD" in sym) or (
        str(getattr(cfg, "asset_class", "")).upper() == "COMMODITY"
    ) or ("WTI" in sym) or ("OIL" in sym)
    if is_gold:
        return GOLD_ANTI_WICK
    return float(getattr(cfg, "anti_wick_buffer_atr", 0.35))


def swing_cap_mult(symbol: str) -> float:
    """SWING-style upper clamp ``max_swing_sl`` as an ATR multiple."""
    sym = str(symbol).upper()
    cfg = get_symbol_profile_config(sym)
    is_gold = ("XAU" in sym) or ("GOLD" in sym) or (
        str(getattr(cfg, "asset_class", "")).upper() == "COMMODITY"
    ) or ("WTI" in sym) or ("OIL" in sym)
    if is_gold:
        return 2.80  # dynamic_levels.py:250,386
    return float(getattr(cfg, "sl_atr_multiplier", 1.80))


# ─────────────────────────────────────────────────────────────────────────────
# arm construction
# ─────────────────────────────────────────────────────────────────────────────
def build_arm_frame(df: pd.DataFrame, cands: pd.DataFrame, symbol: str,
                    tf: str, lookback: int) -> pd.DataFrame:
    """Per-candidate table of atr_median / atr_ratio / effective_buffer per arm."""
    spec = resolve(symbol)
    atr_series = compute_atr(df, 14).to_numpy(dtype=float)
    # Same-timeframe trailing median ATR: strictly causal (window ends at bar i,
    # and atr[i] itself only uses bars <= i). min_periods keeps early bars usable.
    atr_med_series = (
        pd.Series(atr_series)
        .rolling(window=int(lookback), min_periods=max(5, int(lookback) // 5))
        .median()
        .to_numpy(dtype=float)
    )

    close = df["close"].to_numpy(dtype=float)
    n = len(df)
    idx = cands["bar_idx"].to_numpy(int)
    keep = (idx >= 0) & (idx < n)
    c = cands.loc[keep].copy()
    idx = c["bar_idx"].to_numpy(int)

    atr = c["atr"].to_numpy(dtype=float)
    c_price = close[idx]                                  # proxy for context.current_price
    typical_atr_pct = float(getattr(spec, "typical_atr_pct", 0.5) or 0.5)
    typ_spread = float(getattr(spec, "typical_spread_pips", 0.0) or 0.0)
    if typ_spread <= 0:
        typ_spread = 1.5
    spread_pips = c["spread_pips"].to_numpy(dtype=float)

    spread_ratio = np.clip(spread_pips / typ_spread, 0.5, 4.0)

    daily_atr = c_price * (typical_atr_pct / 100.0)
    bpd = float(BARS_PER_DAY.get(tf.upper(), 24))
    sqrt_atr = daily_atr / math.sqrt(bpd)

    med_A = daily_atr
    med_B = atr_med_series[idx]
    med_C = sqrt_atr

    def ratio(med: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            r = atr / np.maximum(med, 1e-12)
        r = np.where(np.isfinite(r) & (med > 0), r, np.nan)
        return np.clip(r, 0.33, 3.0)

    a_ratio, b_ratio, c_ratio = ratio(med_A), ratio(med_B), ratio(med_C)
    awm = anti_wick_mult(symbol)

    out = c[["symbol", "bar_idx", "side", "fill", "sl", "tp", "risk_dist",
             "atr", "spread_pips", "regime", "strategy", "score"]].copy()
    out["c_price"] = c_price
    out["atr_med_A"] = med_A
    out["atr_med_B"] = med_B
    out["atr_med_C"] = med_C
    out["spread_ratio"] = spread_ratio
    out["anti_wick_mult"] = awm
    out["swing_cap_mult"] = swing_cap_mult(symbol)
    for name, r in (("A", a_ratio), ("B", b_ratio), ("C", c_ratio)):
        out[f"atr_ratio_{name}"] = r
        bm = ALPHA_BASE + BETA_VOL * r + GAMMA_SPREAD * spread_ratio
        out[f"buffer_mult_{name}"] = bm
        out[f"eff_buf_atr_{name}"] = np.maximum(bm, awm)      # effective_buffer / atr
    return out


# ─────────────────────────────────────────────────────────────────────────────
# metrics helpers
# ─────────────────────────────────────────────────────────────────────────────
def clamp_profile(r: np.ndarray) -> Dict[str, float]:
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return {"n": 0}
    return {
        "n": int(len(r)),
        "pct_at_0.33": round(float((r <= 0.33 + 1e-9).mean()) * 100, 3),
        "pct_at_3.0": round(float((r >= 3.0 - 1e-9).mean()) * 100, 3),
        "pct_between": round(float(((r > 0.33 + 1e-9) & (r < 3.0 - 1e-9)).mean()) * 100, 3),
        "median": round(float(np.median(r)), 4),
        "mean": round(float(np.mean(r)), 4),
    }


def replay_arm(symbol: str, df: pd.DataFrame, arm: pd.DataFrame, *,
               stop_col: str, tp_r: float, tp_mode: str, slip_price: float,
               spec: Any, money: float) -> Optional[Dict[str, Any]]:
    bars = BarArrays.from_df(df)
    n = len(df)
    rows: List[Dict[str, Any]] = []
    r_out: List[float] = []
    spans: List[Dict[str, Any]] = []

    for row in arm.itertuples(index=False):
        i = int(row.bar_idx)
        if i + 1 >= n:
            continue
        side = str(row.side).upper()
        if side not in ("BUY", "SELL"):
            continue
        fill = float(row.fill)
        atr = float(row.atr)
        if not np.isfinite(atr) or atr <= 0:
            continue
        d = float(getattr(row, stop_col))
        if not np.isfinite(d) or d <= 0:
            continue
        sl = fill - d if side == "BUY" else fill + d

        if tp_mode == "abs":
            tp_eff = abs(float(row.tp) - fill) / d
            if not np.isfinite(tp_eff) or tp_eff <= 0:
                continue
            geom = Geometry(tp_r=float(tp_eff))
        else:
            geom = Geometry(tp_r=float(tp_r))

        out = simulate_trade(
            symbol=symbol, side=side, entry_idx=i, fill=fill, sl=sl, geom=geom,
            money_per_unit=money, bars=bars, cost_price_equiv=0.0,
            slippage_price_equiv=slip_price,
            ai_score=float(getattr(row, "score", 0.0) or 0.0),
            calibrated_win_p=float(getattr(row, "score", 0.0) or 0.0),
            regime=str(getattr(row, "regime", "UNKNOWN")),
            strategy=str(getattr(row, "strategy", "UNKNOWN")),
            zone="UNKNOWN", spec=spec,
        )
        if out is None:
            continue
        res = str(out.result)
        rows.append({
            "bar_idx": i, "side": side, "pnl_r": float(out.pnl_r), "result": res,
            "risk_dist": float(out.risk_dist), "risk_x_atr": float(out.risk_dist) / atr,
            "money_per_lot": float(out.pnl_money_per_lot),
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


def metrics_from_rows(rows: pd.DataFrame) -> Dict[str, Any]:
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
    return {
        "trades": int(n),
        "win_rate": round(float(len(wins) / n), 4),
        "stop_out_rate": round(float((rows["result"] == "SL").mean()), 4),
        "target_rate": round(float((rows["result"] == "TP").mean()), 4),
        "expectancy_r": round(mean_r, 5),
        "total_r": round(float(r.sum()), 3),
        "t_stat": round(float(mean_r / (sd / math.sqrt(n))), 3) if sd > 1e-12 else 0.0,
        "max_dd_r": round(dd, 3),
        "avg_win_r": round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.mean()), 4) if len(losses) else 0.0,
        "median_risk_x_atr": round(float(rows["risk_x_atr"].median()), 4),
        "mean_risk_x_atr": round(float(rows["risk_x_atr"].mean()), 4),
        "total_pnl_money_per_lot": round(float(rows["money_per_lot"].sum()), 3),
        "cost_price_per_trade": round(float(rows["cost_price"].mean()), 8),
    }


def dsr_block(r: np.ndarray, spans: List[Dict[str, Any]], *, n_trials: int,
              var_trial: float, tf: str) -> Dict[str, Any]:
    ppy = BARS_PER_YEAR.get(tf.upper())
    st = sharpe_stats(r, periods_per_year=ppy)
    psr = probabilistic_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"])
    dsr = deflated_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"], n_trials, var_trial)
    n_eff = float(SampleUniquenessWeightEngine.effective_sample_size(spans))
    dsr_eff = deflated_sharpe_ratio(st["sr"], int(round(n_eff)), st["skew"], st["kurt"],
                                    n_trials, var_trial)
    return {
        "n": int(st["n"]), "n_eff": round(n_eff, 1),
        "sr_per_obs": round(float(st["sr"]), 6),
        "skew": round(float(st["skew"]), 4), "kurt": round(float(st["kurt"]), 4),
        "psr": round(float(psr), 6), "dsr": round(float(dsr), 6),
        "dsr_effective_n": round(float(dsr_eff), 6),
        "passes_dsr_095": bool(dsr > 0.95),
        "passes_dsr_095_effective_n": bool(dsr_eff > 0.95),
    }


# ─────────────────────────────────────────────────────────────────────────────
# driver
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="ATR-unit-fix A/B/C experiment")
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--window", type=int, default=183)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--lookback", type=int, default=100,
                    help="trailing window (bars) for arm B's same-timeframe median ATR")
    ap.add_argument("--tp", type=float, default=TP_R_DEFAULT)
    ap.add_argument("--tp-mode", choices=("rr", "abs"), default="rr")
    ap.add_argument("--slip-pips", type=float, default=SLIP_PIPS_DEFAULT)
    ap.add_argument("--dsr-tp-grid", default=",".join(f"{t:g}" for t in DSR_TP_GRID_DEFAULT))
    ap.add_argument("--probe-only", action="store_true",
                    help="only measure ratio/buffer distributions; no replay")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tf = args.tf.upper()
    dsr_grid = [float(x) for x in args.dsr_tp_grid.split(",") if x.strip()]
    args.out = args.out or os.path.join(
        REPO, "reports", f"atr_unit_fix_ab_{tf}_{args.window}d.json")

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else
               sorted(d for d in os.listdir(REAL_DIR)
                      if os.path.isdir(os.path.join(REAL_DIR, d))))

    print(f"ATR-UNIT-FIX A/B/C  tf={tf} window={args.window}d  lookback={args.lookback}  "
          f"tp_mode={args.tp_mode} tp={args.tp}R  slip={args.slip_pips}pip  "
          f"n_symbols={len(symbols)}")
    print(f"  alpha={ALPHA_BASE} beta={BETA_VOL} gamma={GAMMA_SPREAD}  "
          f"arms: A=daily registry  B=same-TF trailing median  C=daily/sqrt(bars_per_day)")

    per_symbol: Dict[str, Any] = {}
    skipped: List[str] = []
    pooled: Dict[str, List[pd.DataFrame]] = {"A": [], "B": [], "C": []}
    all_trial_sr: List[float] = []

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

        arm = build_arm_frame(df, cands, sym, tf, args.lookback)
        awm = anti_wick_mult(sym)

        # --- buffer-change diagnostics -------------------------------------
        d_buf_atr = (arm["eff_buf_atr_B"] - arm["eff_buf_atr_A"]).to_numpy(float)
        dynA = arm["buffer_mult_A"].to_numpy(float)
        dynB = arm["buffer_mult_B"].to_numpy(float)
        pct_anti_wick_binds_A = float((dynA <= awm + 1e-12).mean()) * 100
        pct_anti_wick_binds_B = float((dynB <= awm + 1e-12).mean()) * 100
        pct_buf_changed = float((np.abs(d_buf_atr) > 1e-9).mean()) * 100
        pct_buf_changed_1e4 = float((np.abs(d_buf_atr) > 1e-4).mean()) * 100

        # daily<->H1 empirical ATR ratio (for arm C's context)
        med_ratio = float(np.nanmedian(arm["atr_med_B"].to_numpy(float)
                                       / np.maximum(arm["atr_med_A"].to_numpy(float), 1e-12)))

        entry = {
            "n_candidates": int(len(arm)),
            "anti_wick_mult": awm,
            "swing_cap_mult": swing_cap_mult(sym),
            "median_atr_med_B_over_A": round(med_ratio, 4),
            "pct_anti_wick_binds_A": round(pct_anti_wick_binds_A, 3),
            "pct_anti_wick_binds_B": round(pct_anti_wick_binds_B, 3),
            "pct_eff_buf_changed_B_vs_A": round(pct_buf_changed, 4),
            "pct_eff_buf_changed_B_vs_A_gt1e-4atr": round(pct_buf_changed_1e4, 4),
            "max_eff_buf_delta_atr": round(float(np.nanmax(d_buf_atr)) if len(d_buf_atr) else 0.0, 5),
            "median_eff_buf_atr_A": round(float(np.nanmedian(arm["eff_buf_atr_A"])), 4),
            "median_eff_buf_atr_B": round(float(np.nanmedian(arm["eff_buf_atr_B"])), 4),
            "clamp": {a: clamp_profile(arm[f"atr_ratio_{a}"].to_numpy(float)) for a in "ABC"},
            "median_risk_x_atr_stored": round(float(np.nanmedian(
                arm["risk_dist"] / np.maximum(arm["atr"], 1e-12))), 4),
            "arms": {},
        }

        # --- stop distance per arm -----------------------------------------
        # d_A = stored production stop distance. d_B = d_A + Delta(effective_buffer),
        # capped at the SWING max (the only clamp that can bind upward); if the
        # cap already binds in arm A the stop cannot move, so d_B = d_A there too.
        dA = arm["risk_dist"].to_numpy(float)
        cap = arm["swing_cap_mult"].to_numpy(float) * arm["atr"].to_numpy(float)
        dB = np.where(dA >= cap - 1e-12, dA, np.minimum(dA + d_buf_atr * arm["atr"].to_numpy(float), cap))
        dC = np.where(dA >= cap - 1e-12, dA,
                      np.minimum(dA + (arm["eff_buf_atr_C"].to_numpy(float) - arm["eff_buf_atr_A"].to_numpy(float)) * arm["atr"].to_numpy(float), cap))
        arm = arm.assign(risk_dist_A=dA, risk_dist_B=dB, risk_dist_C=dC)

        entry["median_stop_x_atr_A"] = round(float(np.nanmedian(dA / arm["atr"])), 4)
        entry["median_stop_x_atr_B"] = round(float(np.nanmedian(dB / arm["atr"])), 4)
        entry["median_stop_x_atr_C"] = round(float(np.nanmedian(dC / arm["atr"])), 4)

        for a in "ABC":
            pooled[a].append(arm)

        if args.probe_only:
            cA, cB = entry["clamp"]["A"], entry["clamp"]["B"]
            print(f"  {sym:8s} n={entry['n_candidates']:5d} "
                  f"clampA@0.33={cA['pct_at_0.33']:5.1f}% clampB@0.33={cB['pct_at_0.33']:5.1f}% "
                  f"antiWickBindsA={pct_anti_wick_binds_A:5.1f}% "
                  f"bufChg={pct_buf_changed:6.3f}% maxD={entry['max_eff_buf_delta_atr']:.4f}atr "
                  f"medStopA={entry['median_stop_x_atr_A']:.3f}atr medStopB={entry['median_stop_x_atr_B']:.3f}atr",
                  flush=True)
            per_symbol[sym] = entry
            continue

        # --- replay --------------------------------------------------------
        for a in "ABC":
            res = replay_arm(sym, df, arm, stop_col=f"risk_dist_{a}", tp_r=args.tp,
                             tp_mode=args.tp_mode, slip_price=slip, spec=spec, money=money)
            if res is None:
                entry["arms"][a] = {"trades": 0}
                continue
            m = metrics_from_rows(res["rows"])
            m["_r"] = res["r"]
            m["_rows"] = res["rows"]
            m["_spans"] = res["spans"]
            entry["arms"][a] = m
        for a in "ABC":
            rr = entry["arms"][a]
            if "trades" in rr and rr["trades"]:
                all_trial_sr.append(float(sharpe_stats(
                    rr["_r"], periods_per_year=BARS_PER_YEAR.get(tf))["sr"]))
        per_symbol[sym] = entry

        a_ = entry["arms"].get("A", {})
        b_ = entry["arms"].get("B", {})
        print(f"  {sym:8s} A: n={a_.get('trades', 0):5d} stop%={a_.get('stop_out_rate', 0) * 100:5.1f} "
              f"E={a_.get('expectancy_r', 0):+.4f}R medStop={a_.get('median_risk_x_atr', 0):.3f}atr | "
              f"B: stop%={b_.get('stop_out_rate', 0) * 100:5.1f} E={b_.get('expectancy_r', 0):+.4f}R "
              f"medStop={b_.get('median_risk_x_atr', 0):.3f}atr "
              f"dE={b_.get('expectancy_r', 0) - a_.get('expectancy_r', 0):+.5f}R", flush=True)

    # --- pooled clamp/buffer stats -----------------------------------------
    pooled_arm = {a: (pd.concat(v, ignore_index=True) if v else pd.DataFrame())
                  for a, v in pooled.items()}
    portfolio_clamp: Dict[str, Any] = {}
    for a in "ABC":
        p = pooled_arm[a]
        if len(p) == 0:
            continue
        portfolio_clamp[a] = {
            "n": int(len(p)),
            "clamp": clamp_profile(p[f"atr_ratio_{a}"].to_numpy(float)),
            "median_eff_buf_atr": round(float(np.nanmedian(p[f"eff_buf_atr_{a}"])), 4),
            "median_stop_x_atr": round(float(np.nanmedian(
                p[f"risk_dist_{a}"] / np.maximum(p["atr"], 1e-12))), 4),
        }

    payload: Dict[str, Any] = {
        "generated": pd.Timestamp.now("UTC").isoformat(),
        "config": {
            "tf": tf, "window_days": args.window, "lookback_bars": args.lookback,
            "tp_mode": args.tp_mode, "tp_r": args.tp, "slip_pips": args.slip_pips,
            "alpha_base": ALPHA_BASE, "beta_vol": BETA_VOL, "gamma_spread": GAMMA_SPREAD,
            "symbols": [s for s in symbols if s not in skipped], "skipped": skipped,
            "spread_semantics": "consumes stored spread_pips column; never re-derives from raw spread",
            "cost_model": f"entry fill carries spread; stops pay {args.slip_pips} pip slippage; commission 0",
            "arm_B_definition": f"trailing median of same-TF Wilder ATR(14) over {args.lookback} bars",
            "arm_C_definition": "daily ATR (registry) / sqrt(bars_per_day)",
            "stop_model": "d_X = min(d_stored + (eff_buf_X - eff_buf_A), swing_cap*atr), unless d_stored already at cap",
        },
        "per_symbol": per_symbol,
        "portfolio_clamp": portfolio_clamp,
    }

    if not args.probe_only:
        # --- portfolio replay metrics --------------------------------------
        portfolio: Dict[str, Any] = {}
        n_trials = max(1, len(dsr_grid)) * 3
        var_trial = float(np.var(all_trial_sr, ddof=1)) if len(all_trial_sr) > 1 else 0.0
        for a in "ABC":
            parts = [per_symbol[s]["arms"][a]["_rows"] for s in per_symbol
                     if "arms" in per_symbol[s] and per_symbol[s]["arms"].get(a, {}).get("trades")]
            if not parts:
                continue
            pooled_rows = pd.concat(parts, ignore_index=True)
            m = metrics_from_rows(pooled_rows)
            spans: List[Dict[str, Any]] = []
            for s in per_symbol:
                if per_symbol[s]["arms"].get(a, {}).get("trades"):
                    spans.extend(per_symbol[s]["arms"][a]["_spans"])
            m["dsr"] = dsr_block(pooled_rows["pnl_r"].to_numpy(float), spans,
                                 n_trials=n_trials, var_trial=var_trial, tf=tf)
            portfolio[a] = m
        # per-symbol DSR (portfolio DSR pooled is untrustworthy per prior audit)
        for sym, e in per_symbol.items():
            for a in "ABC":
                rr = e.get("arms", {}).get(a)
                if rr and rr.get("trades"):
                    rr["dsr"] = dsr_block(rr["_r"], rr["_spans"], n_trials=n_trials,
                                          var_trial=var_trial, tf=tf)
        payload["portfolio"] = portfolio
        payload["deflation"] = {"n_trials": n_trials, "var_trial_sharpes": var_trial}
        # strip non-serialisable arrays
        for sym, e in per_symbol.items():
            for a in "ABC":
                rr = e.get("arms", {}).get(a)
                if rr:
                    rr.pop("_r", None); rr.pop("_rows", None); rr.pop("_spans", None)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    print("\nPORTFOLIO clamp distribution (pooled candidates):")
    for a in "ABC":
        pc = portfolio_clamp.get(a)
        if pc:
            print(f"  arm {a}: n={pc['n']:6d} @0.33={pc['clamp']['pct_at_0.33']:6.2f}% "
                  f"@3.0={pc['clamp']['pct_at_3.0']:5.2f}% between={pc['clamp']['pct_between']:5.2f}% "
                  f"median={pc['clamp']['median']:.3f}  medEffBuf={pc['median_eff_buf_atr']:.4f}atr "
                  f"medStop={pc['median_stop_x_atr']:.4f}atr")
    if not args.probe_only and "portfolio" in payload:
        print("\nPORTFOLIO replay (pooled rows, production tp_r):")
        print(f"{'arm':>4} {'trades':>7} {'stop%':>6} {'WR%':>6} {'E[R]':>10} {'totR':>10} "
              f"{'medATR':>8} {'DSReff':>8}")
        for a in "ABC":
            p = payload["portfolio"].get(a)
            if not p:
                continue
            print(f"{a:>4} {p['trades']:>7} {p['stop_out_rate'] * 100:>6.1f} "
                  f"{p['win_rate'] * 100:>6.1f} {p['expectancy_r']:>+10.5f} "
                  f"{p['total_r']:>+10.1f} {p['median_risk_x_atr']:>8.4f} "
                  f"{p['dsr']['dsr_effective_n']:>8.4f}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
