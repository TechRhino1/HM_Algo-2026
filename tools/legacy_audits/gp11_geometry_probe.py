"""Probe 2 (corrected): geometry impact of the real spread on dynamic_levels SL distances.

pips = spread_points * point / spec.pip_size  (point = 10**-digits)
Run:  python tools/gp11_geometry_probe.py
Writes reports/gp11_spread_geometry_probe.json
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath("."))
from jarvis.data.symbol_registry import resolve  # noqa: E402

ROOT = "data/market/real"


def atr_series(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    p = c.shift(1)
    tr = np.maximum(h - l, np.maximum((h - p).abs(), (l - p).abs()))
    return tr.rolling(period, min_periods=1).mean()


out = []
for sym in sorted(os.listdir(ROOT)):
    path = f"{ROOT}/{sym}/{sym}_H1_183d.parquet"
    if not os.path.exists(path):
        continue
    spec = resolve(sym)
    if spec.canonical != sym:
        continue
    df = pd.read_parquet(path)
    pt = 10.0 ** (-int(spec.digits))
    pip = spec.pip_size
    pips = (df["spread"].astype(float) * pt / pip).to_numpy()
    atr = atr_series(df).to_numpy()
    sd_real = pips * pip
    sd_reg = spec.typical_spread_pips * pip
    # SL floor used at dynamic_levels.py:280 (BUY) / :410 (SELL)
    floor_real = np.maximum(3.0 * sd_real, 0.10 * atr)
    floor_reg = np.maximum(3.0 * sd_reg, 0.10 * atr)
    out.append({
        "symbol": sym,
        "typ_pips": spec.typical_spread_pips,
        "real_median_pips": round(float(np.median(pips)), 3),
        "atr_pips_median": round(float(np.nanmedian(atr / pip)), 2),
        "add_delta_pips_median": round(float(np.median((sd_real - sd_reg) / pip)), 3),
        "add_delta_pct_of_atr_median": round(
            float(np.nanmedian((sd_real - sd_reg) / np.where(atr > 0, atr, np.nan))) * 100.0, 2),
        "floor_delta_pips_median": round(float(np.nanmedian((floor_real - floor_reg) / pip)), 3),
        "frac_floor_spread_binds_reg": round(float((3.0 * sd_reg > 0.10 * atr).mean()), 4),
        "frac_floor_spread_binds_real": round(float((3.0 * sd_real > 0.10 * atr).mean()), 4),
    })

with open("reports/gp11_spread_geometry_probe.json", "w") as f:
    json.dump(out, f, indent=1)

hdr = ["symbol", "typ_pips", "real_median_pips", "atr_pips_median", "add_delta_pips_median",
       "add_delta_pct_of_atr_median", "floor_delta_pips_median",
       "frac_floor_spread_binds_reg", "frac_floor_spread_binds_real"]
print("\t".join(hdr))
for r in out:
    print("\t".join(str(r.get(k)) for k in hdr))
