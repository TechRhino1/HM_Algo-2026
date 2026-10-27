"""A/B: does correcting registry spread calibration change trade selection or P&L?

Read-only w.r.t. jarvis/. Two independent measurements:

1. BACKTEST selection/P&L: run the production SignalScanner twice per symbol --
   incumbent registry vs corrected -- and compare the candidate tables, the
   EXECUTE-decision subset (what actually trades, engine.py:684), and a
   fixed-geometry replay of that subset.

2. LIVE stop floor: the live path synthesises ask/bid from
   current_spread_pips = spec.typical_spread_pips (market_context.py:80), so
   dynamic_levels.py:139 collapses to registry_typical * pip_size and the
   max(3*spread, 0.10*ATR) floor at :280/:403 moves with the registry. Rebuild
   the live context on sampled bars under both registries and compare risk_dist.

Usage:
  python .scratch/spread_ab.py --tf H1 --window 183 --symbols EURUSD,...
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import math
import os
import sys
from typing import Any, Dict, List

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from jarvis.data import symbol_registry as reg  # noqa: E402
from jarvis.backtesting.signal_scan import SignalScanner  # noqa: E402
from jarvis.backtesting.trade_simulator import BarArrays, Geometry, simulate_trade  # noqa: E402

# corrected typical = measured M1 median; corrected max = ratio-preserving value
# that stays above the measured p95 (see report for derivation).
CORRECTED: Dict[str, Dict[str, float]] = {
    "EURUSD": {"typical_spread_pips": 1.9, "max_spread_pips": 5.5},
    "GBPUSD": {"typical_spread_pips": 2.2, "max_spread_pips": 6.0},
    "USDJPY": {"typical_spread_pips": 2.3, "max_spread_pips": 7.0},
    "AUDUSD": {"typical_spread_pips": 2.3, "max_spread_pips": 6.5},
    "GBPJPY": {"typical_spread_pips": 2.5, "max_spread_pips": 6.5},
    "EURJPY": {"typical_spread_pips": 2.0, "max_spread_pips": 5.0},
    "BTCUSD": {"typical_spread_pips": 2250.0},          # max 3000 already above p95
    "WTI": {"typical_spread_pips": 3.0},                # max 25 already above p95
}


#: The registry as it ships, captured at import BEFORE any override is applied.
#: This is what makes `apply_registry(None)` a genuine restore -- see the note on
#: `apply_registry` below.
_PRISTINE: Dict[str, Any] = dict(reg._REGISTRY)


def apply_registry(overrides: Dict[str, Dict[str, float]] | None) -> None:
    """Rebuild the registry from the PRISTINE snapshot, with `overrides` applied.

    The original built `new[key] = spec` from the CURRENT `_REGISTRY`:

        for key, spec in reg._REGISTRY.items():
            ov = (overrides or {}).get(key)
            new[key] = dataclasses.replace(spec, **ov) if ov else spec

    so `apply_registry(None)` was a no-op, not a restore. Once
    `apply_registry(CORRECTED)` had run, every later `apply_registry(None)` left
    the corrected specs in place -- and because both `main()` and the
    reconciliation driver call `apply_registry(None)` before the INCUMBENT scan
    of every symbol, **only the first symbol scanned ever had a genuine incumbent
    arm**. Every other symbol was measured corrected-vs-corrected.

    Measured directly (`.scratch/probe_j_harness.py`): pristine AUDUSD
    `typical_spread_pips` 0.9 -> after CORRECTED 2.3 -> after
    `apply_registry(None)` **2.3, not 0.9**.

    Always rebuilding from `_PRISTINE` makes the A arm genuinely incumbent, and
    makes the call idempotent, which is what the name always promised.
    """
    new = {}
    for key, spec in _PRISTINE.items():
        ov = (overrides or {}).get(key)
        new[key] = dataclasses.replace(spec, **ov) if ov else spec
    reg._REGISTRY.clear()
    reg._REGISTRY.update(new)


def load_bars(sym: str, tf: str, window: int):
    p = os.path.join(REPO, "data", "market", "real", sym, f"{sym}_{tf}_{window}d.parquet")
    return pd.read_parquet(p).reset_index(drop=True) if os.path.exists(p) else None


def scan(sym: str, df: pd.DataFrame):
    return SignalScanner(start_bar_idx=60).scan(df, sym)


@contextlib.contextmanager
def frozen_news():
    """Freeze the macro news calendar for the duration of a scan.

    WHY THIS EXISTS. `MacroAnalyst` calls `GLOBAL_NEWS_ENGINE.get_news_calendar()`
    on the decision path. That is a synchronous network fetch (5s and 6s socket
    timeouts in `jarvis/market/news.py`) sitting behind a 90s cache TTL, while
    `ParallelAnalystCluster` allows the analyst only 2.0s. On a cache miss the
    fetch was measured at 1.32s -- 66% of the analyst's entire budget, before any
    CPU work, competing with six other analysts for the GIL. So on a cache miss
    the analyst is replaced by a FABRICATED score-50 NEUTRAL report, and because
    the TTL expiry lands wherever it lands relative to the scan, the scan's own
    output changes between runs.

    Measured before this was added: AUDUSD under the pristine registry gave
    EXEC 51 in a clean process (2 reps, 0 fallbacks) and 53 / 56 in runs that
    logged timeouts. One measurement, three answers.

    Freezing removes the network from the measurement so the ONLY thing varying
    between the two arms is the spread registry -- which is what the A/B is
    supposed to isolate. It does not favour either arm: both are frozen from the
    same snapshot.

    Note this makes the scan MORE reproducible than production, deliberately. It
    is an instrument, not a model of production; the production defect it papers
    over (a budget smaller than its dependency's timeout) is tracked separately
    in `docs/AUDIT-3-TRACKS-2026-09-23.md` J.
    """
    import jarvis.market.news as news_mod

    engine = news_mod.GLOBAL_NEWS_ENGINE
    snapshot = engine.get_news_calendar()   # one real fetch, before freezing
    # Restore by REMOVING the instance attribute rather than assigning the bound
    # method back. `engine.get_news_calendar` is a class attribute, so assigning
    # the captured bound method as an instance attribute leaves a permanent
    # shadow: functionally the same call, but the object is no longer the one the
    # class defines, and `is` comparisons (and any future monkeypatch) break.
    had_instance_attr = "get_news_calendar" in engine.__dict__
    original = engine.get_news_calendar

    def _frozen(force_refresh: bool = False):  # signature-compatible
        return snapshot

    engine.get_news_calendar = _frozen
    try:
        yield snapshot
    finally:
        if had_instance_attr:
            engine.get_news_calendar = original
        else:
            try:
                del engine.get_news_calendar
            except AttributeError:
                pass


def replay(sym: str, df: pd.DataFrame, cands: pd.DataFrame, tp_r: float = 1.5):
    if cands is None or len(cands) == 0:
        return None
    spec = reg.resolve(sym)
    money = reg.get_dollar_risk_per_price_unit(sym, None)
    bars = BarArrays.from_df(df)
    n = len(df)
    rs: List[float] = []
    risks: List[float] = []
    for r in cands.itertuples(index=False):
        i = int(getattr(r, "bar_idx"))
        if i + 1 >= n:
            continue
        side = str(getattr(r, "side", "")).upper()
        if side not in ("BUY", "SELL"):
            continue
        fill, sl = float(getattr(r, "fill")), float(getattr(r, "sl"))
        if abs(fill - sl) <= 0:
            continue
        out = simulate_trade(
            symbol=sym, side=side, entry_idx=i, fill=fill, sl=sl,
            geom=Geometry(tp_r=float(tp_r)), money_per_unit=money, bars=bars,
            cost_price_equiv=0.0, slippage_price_equiv=0.5 * float(spec.pip_size), spec=spec,
        )
        if out is None:
            continue
        rs.append(float(out.pnl_r)); risks.append(float(out.risk_dist))
    if not rs:
        return None
    arr = np.asarray(rs, float)
    sd = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    return {
        "n": int(len(arr)), "mean_r": round(float(arr.mean()), 5),
        "total_r": round(float(arr.sum()), 3), "win_rate": round(float((arr > 0).mean()), 4),
        "t_stat": round(float(arr.mean() / (sd / math.sqrt(len(arr)))), 3) if sd > 1e-12 else 0.0,
        "median_risk_price": round(float(np.median(risks)), 8),
    }


def live_stop_ab(sym: str, df: pd.DataFrame, sample: int = 400) -> Dict[str, Any]:
    """Live-path stop comparison: ask/bid synthesised from registry typical."""
    from jarvis.market.market_context import MarketContextEngine
    from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine

    if len(df) < 220:
        return {}
    idxs = np.linspace(200, len(df) - 1, num=min(sample, len(df) - 200), dtype=int)
    ce = MarketContextEngine()
    dl = DynamicRiskAndLevelsEngine()

    def run_once() -> List[float]:
        spec = reg.resolve(sym)
        out: List[float] = []
        h4 = df.iloc[idxs[0] // 4:].copy()
        for i in idxs:
            hist = df.iloc[max(0, i - 300):i]
            mtf = {"primary": hist, "context": h4, "macro": h4}
            ctx = ce.build_context(sym, mtf,
                                   current_spread_pips=spec.typical_spread_pips,
                                   max_allowed_spread_pips=spec.max_spread_pips)
            lv = dl.calculate_levels(context=ctx, regime=_neutral_regime(), tentative_bias="BUY")
            if lv.get("risk_dist"):
                out.append(float(lv["risk_dist"]))
        return out

    apply_registry(None)
    a = run_once()
    apply_registry(CORRECTED)
    b = run_once()
    if not a or not b:
        return {}
    a, b = np.asarray(a), np.asarray(b)
    return {
        "n": int(len(a)),
        "median_risk_A": round(float(np.median(a)), 8),
        "median_risk_B": round(float(np.median(b)), 8),
        "median_ratio_B_over_A": round(float(np.median(b) / max(np.median(a), 1e-12)), 4),
        "frac_risk_changed": round(float((np.abs(b - a) > 1e-12).mean()), 4),
        "mean_ratio_B_over_A": round(float(np.mean(b / np.maximum(a, 1e-12))), 4),
    }


def _neutral_regime():
    from jarvis.data.schemas import RegimeOutput, MarketRegime
    return RegimeOutput(primary_regime=MarketRegime.RANGE, probabilities={}, confidence=0.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--window", type=int, default=183)
    ap.add_argument("--symbols", default=",".join(CORRECTED))
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-freeze-news", action="store_true",
                    help="do NOT freeze the macro news calendar during the scan.")
    args = ap.parse_args()

    tf = args.tf.upper()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    out = {"config": {"tf": tf, "window": args.window, "symbols": syms, "corrected": CORRECTED},
           "per_symbol": {}}

    with frozen_news() if not args.no_freeze_news else contextlib.nullcontext():
        print("[news] calendar frozen for the scan" if not args.no_freeze_news
              else "[news] NOT frozen -- reproducing production behaviour", flush=True)
        for sym in syms:
            df = load_bars(sym, tf, args.window)
            if df is None:
                print(f"[skip] {sym}: no bars", flush=True)
            continue

        apply_registry(None)
        base = scan(sym, df)
        apply_registry(CORRECTED)
        corr = scan(sym, df)
        bc, cc = base.candidates, corr.candidates

        def dec_counts(c: pd.DataFrame) -> Dict[str, int]:
            return {} if c is None or len(c) == 0 else \
                {str(k): int(v) for k, v in c["decision"].value_counts().items()}

        def executed_subset(c: pd.DataFrame) -> pd.DataFrame:
            if c is None or len(c) == 0:
                return c
            return c[c["decision"].astype(str).str.upper() == "EXECUTE"].copy()

        ex_a, ex_b = executed_subset(bc), executed_subset(cc)
        entry = {
            "bars": int(len(df)),
            "candidates_A": int(len(bc)), "candidates_B": int(len(cc)),
            "executed_A": int(base.executed), "executed_B": int(corr.executed),
            "gate_pass_A": int(base.gate_pass), "gate_pass_B": int(corr.gate_pass),
            "decisions_A": dec_counts(bc), "decisions_B": dec_counts(cc),
            "exec_replay_A": replay(sym, df, ex_a),
            "exec_replay_B": replay(sym, df, ex_b),
            "all_replay_A": replay(sym, df, bc),
            "all_replay_B": replay(sym, df, cc),
        }
        if len(bc) and len(cc):
            ka = set(zip(bc["bar_idx"], bc["side"].astype(str)))
            kb = set(zip(cc["bar_idx"], cc["side"].astype(str)))
            entry.update({"entry_overlap": len(ka & kb), "only_A": len(ka - kb), "only_B": len(kb - ka)})
        if not args.skip_live:
            entry["live_stop"] = live_stop_ab(sym, df)

        out["per_symbol"][sym] = entry
        ra, rb = entry["exec_replay_A"], entry["exec_replay_B"]
        la = entry.get("live_stop", {})
        print(f"{sym:8s} cand {entry['candidates_A']:5d}/{entry['candidates_B']:5d} "
              f"EXEC {entry['executed_A']:4d}/{entry['executed_B']:4d} | "
              f"exec-replay n={ra['n'] if ra else 0}/{rb['n'] if rb else 0} "
              f"E={ra['mean_r'] if ra else 0:+.4f}/{rb['mean_r'] if rb else 0:+.4f} | "
              f"live medRisk x{la.get('median_ratio_B_over_A', 0)} "
              f"changed={la.get('frac_risk_changed', 0)}", flush=True)

    apply_registry(None)
    path = args.out or os.path.join(REPO, "reports", f"spread_ab_{tf}_{args.window}d.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
