"""Probe: quantify what a REAL per-bar spread would do to the live pipeline.

Read-only. Uses the parquet `spread` column (MT5 points) with the production
conversion  pips = spread_points * point / spec.pip_size  where
point = 10 ** -spec.digits  (see jarvis/backtesting/signal_scan.py:93,170-180).
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath("."))
from jarvis.data.symbol_registry import resolve  # noqa: E402
from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config  # noqa: E402

ROOT = "data/market/real"
SYMS = sorted(os.listdir(ROOT))

ALPHA, BETA, GAMMA = 0.12, 0.05, 0.05


def point_of(spec):
    return 10.0 ** (-int(spec.digits))


def atr_series(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - prev).abs(), (low - prev).abs()))
    return tr.rolling(period, min_periods=1).mean()


def analyse(symbol, tf, tag):
    path = f"{ROOT}/{symbol}/{symbol}_{tf}_{tag}.parquet"
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    spec = resolve(symbol)
    if spec.canonical != symbol:
        return None
    pt = point_of(spec)
    pip = spec.pip_size
    sp = df["spread"].astype(float)
    pips = sp * pt / pip

    typ = spec.typical_spread_pips
    mx = spec.max_spread_pips
    ratio = np.clip(pips / max(typ, 1e-4), 0.5, 4.0)

    # ATR-based buffer analysis (mirrors dynamic_levels.calculate_levels)
    atr = atr_series(df)
    c_price = df["close"]
    typical_atr_pct = getattr(spec, "typical_atr_pct", 0.5)
    typical_atr = c_price * (typical_atr_pct / 100.0)
    atr_ratio = np.clip(atr / typical_atr.replace(0, np.nan), 0.33, 3.0).fillna(1.0)

    cfg = get_symbol_profile_config(symbol)
    if "XAU" in symbol or "GOLD" in symbol or "WTI" in symbol or "OIL" in symbol:
        w = 0.35
    else:
        w = cfg.anti_wick_buffer_atr

    dyn_real = atr * (ALPHA + BETA * atr_ratio + GAMMA * ratio)
    dyn_reg = atr * (ALPHA + BETA * atr_ratio + GAMMA * 1.0)
    aw = atr * w
    eff_real = np.maximum(dyn_real, aw)
    eff_reg = np.maximum(dyn_reg, aw)
    moved = (eff_real > eff_reg * 1.0001)

    return {
        "symbol": symbol, "tf": tf, "tag": tag, "n": int(len(df)),
        "pip": pip, "point": pt,
        "median_pips": round(float(np.median(pips)), 3),
        "p95_pips": round(float(np.percentile(pips, 95)), 3),
        "max_pips": round(float(pips.max()), 3),
        "typ": typ, "max": mx,
        "frac_over_max": round(float((pips > mx).mean()), 4),
        "frac_over_090max": round(float((pips > mx * 0.90).mean()), 4),
        "frac_over_080max": round(float((pips > mx * 0.80).mean()), 4),
        "ratio_median": round(float(np.median(ratio)), 3),
        "ratio_p95": round(float(np.percentile(ratio, 95)), 3),
        "frac_ratio_gt1": round(float((ratio > 1.0001).mean()), 4),
        "frac_ratio_gt1.2": round(float((ratio > 1.2).mean()), 4),
        "frac_ratio_gt1.5": round(float((ratio > 1.5).mean()), 4),
        "frac_ratio_gt2": round(float((ratio > 2.0).mean()), 4),
        "frac_ratio_eq1": round(float((ratio <= 1.0001).mean()), 4),
        "anti_wick_w": w,
        "frac_dyn_gt_aw_real": round(float((dyn_real > aw).mean()), 4),
        "frac_dyn_gt_aw_reg": round(float((dyn_reg > aw).mean()), 4),
        "frac_eff_buffer_moved": round(float(moved.mean()), 4),
        "median_eff_buf_delta_pct": round(
            float(np.median((eff_real - eff_reg) / np.maximum(eff_reg, 1e-12)) * 100.0), 3),
    }


out = []
for sym in SYMS:
    for tf, tag in (("M1", "183d"), ("H1", "183d"), ("H1", "365d")):
        r = analyse(sym, tf, tag)
        if r:
            out.append(r)

print(json.dumps(out, indent=1))
with open("reports/gp11_spread_impact_probe.json", "w") as f:
    json.dump(out, f, indent=1)
print("\nWROTE reports/gp11_spread_impact_probe.json", file=sys.stderr)
