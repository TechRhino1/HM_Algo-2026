"""Read-only measurement: how wrong is jarvis/data/symbol_registry.py's spread calibration?

Semantics (verified, do NOT recompute as raw*pip_size):
  * the parquet `spread` column is in MT5 POINTS (see jarvis/data/mt5_history.py:320,
    the canonical converter: pips = spread_points * point / pip_size).
  * `point` and the MT5 `pip_size` come from each <SYM>_<TF>_183d.manifest.json meta.
  * the registry's own `pip_size` is the unit its typical/max_spread_pips are quoted in,
    and is what the live code compares against (context.volatility.current_spread_pips
    is set from spec.typical_spread_pips; the gate compares it to spec.max_spread_pips).

So a measured spread is expressed in the SAME unit as the registry by:
    spread_price   = spread_points * point
    spread_pips    = spread_price / registry_pip_size
The naive `spread_points * pip_size` (price-distance) is NOT a pip count and is not used.

Run: python .scratch/measure_spread_calibration.py
Outputs JSON to reports/spread_calibration_measured.json
"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, ".")
from jarvis.data.symbol_registry import resolve

SYMBOLS = [
    "AUDUSD", "BTCUSD", "ETHUSD", "EURJPY", "EURUSD", "GBPJPY", "GBPUSD", "GER40",
    "NAS100", "NZDUSD", "SOLUSD", "UK100", "US30", "US500", "USDCAD", "USDCHF",
    "USDJPY", "WTI", "XAGUSD", "XAUUSD",
]
ROOT = "data/market/real"
PRIMARY_TF = "M1_183d"
CROSSCHECK_TF = "H1_183d"


def load_manifest(sym, tf):
    p = os.path.join(ROOT, sym, f"{sym}_{tf}.manifest.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def spread_stats(sym, tf):
    pq = os.path.join(ROOT, sym, f"{sym}_{tf}.parquet")
    man = load_manifest(sym, tf)
    if not os.path.exists(pq) or man is None:
        return None
    df = pd.read_parquet(pq, columns=["spread"])
    s = pd.to_numeric(df["spread"], errors="coerce").dropna()
    meta = man.get("meta", {})
    point = float(meta.get("point", 0.0) or 0.0)
    mt5_pip = float(meta.get("pip_size", 0.0) or 0.0)
    reg_pip = float(resolve(sym).pip_size or 0.0)
    if point <= 0 or reg_pip <= 0:
        return None
    price = s.astype(float) * point                     # spread in price units
    pips_reg = price / reg_pip                          # in the registry's pip unit
    pips_mt5 = price / mt5_pip if mt5_pip > 0 else None
    return {
        "n_bars": int(len(s)),
        "point": point,
        "mt5_pip_size": mt5_pip,
        "registry_pip_size": reg_pip,
        "median_points": float(s.median()),
        "p95_points": float(s.quantile(0.95)),
        "p99_points": float(s.quantile(0.99)),
        "max_points": float(s.max()),
        "median_pips_reg": float(pips_reg.median()),
        "p95_pips_reg": float(pips_reg.quantile(0.95)),
        "p99_pips_reg": float(pips_reg.quantile(0.99)),
        "max_pips_reg": float(pips_reg.max()),
        "median_pips_mt5": (float(pips_mt5.median()) if pips_mt5 is not None else None),
        "_pips_reg_series": pips_reg,
    }


def main():
    rows = []
    for sym in SYMBOLS:
        spec = resolve(sym)
        prim = spread_stats(sym, PRIMARY_TF)
        if prim is None:
            rows.append({"symbol": sym, "error": "no M1_183d parquet/manifest"})
            continue
        cross = spread_stats(sym, CROSSCHECK_TF)
        series = prim.pop("_pips_reg_series")
        frac_over_max = float((series > spec.max_spread_pips).mean())
        frac_over_typ = float((series > spec.typical_spread_pips).mean())
        prim.update({
            "symbol": sym,
            "asset_class": spec.asset_class,
            "registry_typical": spec.typical_spread_pips,
            "registry_max": spec.max_spread_pips,
            "ratio_median_over_typical": round(prim["median_pips_reg"] / spec.typical_spread_pips, 3)
                if spec.typical_spread_pips else None,
            "ratio_p95_over_max": round(prim["p95_pips_reg"] / spec.max_spread_pips, 3)
                if spec.max_spread_pips else None,
            "frac_bars_over_max": round(frac_over_max, 4),
            "frac_bars_over_typical": round(frac_over_typ, 4),
            "cross_H1_median_pips_reg": (cross["median_pips_reg"] if cross else None),
            "cross_H1_p95_pips_reg": (cross["p95_pips_reg"] if cross else None),
            "unit_mismatch": (abs(prim["mt5_pip_size"] - prim["registry_pip_size"]) > 1e-12),
        })
        rows.append(prim)

    os.makedirs("reports", exist_ok=True)
    with open("reports/spread_calibration_measured.json", "w") as f:
        json.dump(rows, f, indent=2)

    hdr = (f"{'symbol':8} {'cls':9} {'regTyp':>7} {'regMax':>7} "
           f"{'med':>7} {'p95':>7} {'p99':>7} {'max':>8} "
           f"{'med/typ':>8} {'p95/max':>8} {'%>max':>7} {'pipMis':>6}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if "error" in r:
            print(f"{r['symbol']:8} ERROR {r['error']}")
            continue
        print(f"{r['symbol']:8} {r['asset_class']:9} "
              f"{r['registry_typical']:7.2f} {r['registry_max']:7.2f} "
              f"{r['median_pips_reg']:7.3f} {r['p95_pips_reg']:7.3f} "
              f"{r['p99_pips_reg']:7.3f} {r['max_pips_reg']:8.2f} "
              f"{r['ratio_median_over_typical']:8.2f} {r['ratio_p95_over_max']:8.2f} "
              f"{r['frac_bars_over_max']*100:6.2f}% "
              f"{'YES' if r['unit_mismatch'] else 'no':>6}")


if __name__ == "__main__":
    main()
