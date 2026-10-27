#!/usr/bin/env python
"""Fair-Value-Gap as a STANDALONE entry trigger — does it have a measured edge?

WHY THIS EXISTS
---------------
An audit found the incumbent entry signal has no edge that survives the
multiple-testing bar (DSR > 0.95 met by 0/20 symbols once corrected for the
effective sample size; only 3/20 beat an always-long baseline). A fair-value-gap
/ displacement entry has been proposed as the replacement. This tool answers the
narrow, honest question the proposal has to survive before any code is swapped:

    if FVG is run standalone as the entry trigger, does it clear BOTH bars —

      1. DSR > 0.95 on the *effective* sample size (not the row count), and
      2. an always-long baseline on the same entries, same risk, same target?

Swapping one unproven model for another unproven model is not justified, so a
"no" here is a successful outcome.

WHAT IS MEASURED
----------------
* Signal timeframe: M15 183d. Structure context: H1 365d (optional filter).
* Trigger: a 3-candle FVG (the shipped detector in
  ``jarvis/market/fair_value_gap.py``) whose displacement candle (the middle
  bar, c2) has body/range >= 0.55 — the threshold the shipped entry engine
  already uses (``jarvis/intelligence/institutional_entry_engine.py:563``).
* Entry: at the gap's 50% consequent encroachment (CE) ON RETRACE. The trade is
  only taken if a later bar actually trades back into the CE. Entering at the
  gap edge would be lookahead — the price has already left it.
* Stop: beyond the far side of the gap plus a buffer (``--buffer-atr`` x ATR14).
* Target: 1.5R (the production tp_r).
* Costs: the entry reference is the CE *mid* level, so the ask is
  ``CE + spread`` and the bid ``CE - spread`` via
  ``jarvis.backtesting.fills.entry_fill``; the stop fill additionally pays
  ``--slip-pips`` of slippage, matching ``tools/audit_trade_quality.py``.

SPREAD COLUMN SEMANTICS (checked, because this repo has been burned before)
--------------------------------------------------------------------------
The parquet files carry a ``spread`` column and NO ``spread_pips`` column. The
manifest for each file documents ``point`` and ``pip_size``; e.g. EURUSD M15 has
``point=1e-05``, ``pip_size=1e-04`` and a mean raw ``spread`` of ~19.9. That is
19.9 **points** = 1.99 pips, consistent with the manifest's
``typical_spread_pips`` of 2.4-3.3. Multiplying raw by ``pip_size`` instead of
``point`` would give 0.002 price = 0.2 pips — a 10x under-charge. This tool
therefore converts with the same expression the shipped scanner uses
(``raw * point / pip_size``), never ``raw * pip_size``.

Usage
-----
    python tools/fvg_standalone_backtest.py
    python tools/fvg_standalone_backtest.py --symbols EURUSD,XAUUSD
    python tools/fvg_standalone_backtest.py --htf-filter      # H1-aligned only

Read-only with respect to ``jarvis/``; writes only its report under ``reports/``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from jarvis.backtesting.fills import entry_fill  # noqa: E402
from jarvis.backtesting.signal_scan import compute_atr  # noqa: E402
from jarvis.backtesting.trade_simulator import (  # noqa: E402
    BarArrays, Geometry, simulate_trade,
)
from jarvis.data.symbol_registry import (  # noqa: E402
    get_dollar_risk_per_price_unit, resolve,
)
from jarvis.learning.deflated_sharpe import (  # noqa: E402
    deflated_sharpe_ratio, expected_max_sharpe, probabilistic_sharpe_ratio,
    sharpe_stats,
)
from jarvis.learning.sample_weights import SampleUniquenessWeightEngine  # noqa: E402
from jarvis.market.fair_value_gap import FairValueGapEngine  # noqa: E402
from tools.deflated_sharpe_report import (  # noqa: E402
    ACCEPT_DSR, PRODUCTION_TP, TP_GRID_DEFAULT,
)

REAL_DIR = os.path.join(REPO, "data", "market", "real")
BARS_PER_YEAR_M15 = 96 * 252  # 24h x 4 M15 bars, 252 sessions

# The shipped displacement threshold, read off
# jarvis/intelligence/institutional_entry_engine.py:563.
DISPLACEMENT_MIN = 0.55


# ─────────────────────────────────────────────────────────────────────────────
# data + spread semantics
# ─────────────────────────────────────────────────────────────────────────────
def load_bars(symbol: str, tf: str, window: int) -> Optional[pd.DataFrame]:
    path = os.path.join(REAL_DIR, symbol, f"{symbol}_{tf}_{window}d.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path).sort_values("time").reset_index(drop=True)
    return df


def spread_pips_series(df: pd.DataFrame, spec: Any) -> np.ndarray:
    """Per-bar spread in pips.

    The stored ``spread`` column is in POINTS (integer). ``point`` and
    ``pip_size`` differ by 10x on 5-digit FX, so ``raw * point / pip_size`` is
    the conversion. This mirrors ``SignalScanner._spread_for_bar`` exactly; the
    raw value is never multiplied by ``pip_size`` (which would under-charge 10x
    on EURUSD).
    """
    pip = float(getattr(spec, "pip_size", 0.0001) or 0.0001)
    point = 10.0 ** (-int(getattr(spec, "digits", 5) or 5))
    if "spread" not in df.columns:
        fallback = float(getattr(spec, "typical_spread_pips", 2.0) or 2.0)
        return np.full(len(df), fallback, dtype=float)
    raw = df["spread"].to_numpy(dtype=float)
    raw = np.where(np.isfinite(raw) & (raw > 0), raw, np.nan)
    pips = raw * point / pip
    fallback = float(getattr(spec, "typical_spread_pips", 2.0) or 2.0)
    return np.where(np.isfinite(pips), pips, fallback)


def h1_trend_flags(df_h1: pd.DataFrame, times: np.ndarray) -> np.ndarray:
    """Causal H1 trend sign (+1 up / -1 down) as-of each M15 bar time.

    Uses the last H1 bar whose CLOSE time is <= the M15 bar time, so no future
    H1 information can leak into an M15 decision.
    """
    h1 = df_h1.copy()
    h1["time"] = pd.to_datetime(h1["time"])
    h1 = h1.sort_values("time").reset_index(drop=True)
    close = h1["close"].astype(float)
    ema_f = close.ewm(span=20, adjust=False).mean().to_numpy()
    ema_s = close.ewm(span=50, adjust=False).mean().to_numpy()
    trend = np.where(ema_f > ema_s, 1, -1)
    h1_t = h1["time"].to_numpy()
    idx = np.searchsorted(h1_t, pd.to_datetime(times), side="right") - 1
    out = np.zeros(len(times), dtype=int)
    ok = idx >= 0
    out[ok] = trend[idx[ok]]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# signal generation — reuse the SHIPPED FVG detector
# ─────────────────────────────────────────────────────────────────────────────
def fresh_fvgs(engine: FairValueGapEngine, df: pd.DataFrame, b: int,
               lookback: int) -> List[Dict[str, Any]]:
    """FVGs whose 3rd candle (c3) is bar ``b``.

    The detector is called on a window ending at ``b``. In that window an FVG
    whose c3 is the final bar has ``bar_idx == len(window) - 3`` (the engine
    reports ``bar_idx = i - 2`` where ``i`` is c3's index). Restricting to that
    index is what makes the detection causal: the gap has fully formed by the
    close of bar ``b`` and no later bar is visible.
    """
    lo = max(0, b - lookback + 1)
    win = df.iloc[lo:b + 1]
    if len(win) < 10:
        return []
    res = engine.analyze(win)
    target = len(win) - 3
    out: List[Dict[str, Any]] = []
    for f in res["bullish_fvgs"] + res["bearish_fvgs"]:
        if int(f["bar_idx"]) == target:
            out.append(f)
    return out


def build_entries(symbol: str, df: pd.DataFrame, df_h1: Optional[pd.DataFrame], *,
                  lookback: int, wait: int, buffer_atr: float,
                  disp_min: float, htf_filter: bool,
                  spread_mode: str = "data") -> Tuple[List[Dict[str, Any]], int]:
    """Every FVG entry candidate in the window, causally generated.

    ``spread_mode='data'`` charges each bar's own recorded spread (primary);
    ``'spec'`` charges the symbol's registry ``typical_spread_pips`` on every bar
    — a cost sensitivity, since the recorded feed spread on M15 FX is wide.

    Returns ``(entries, n_fvg_signals)`` where ``n_fvg_signals`` counts the
    displacement-qualified FVGs found, before the retrace requirement.
    """
    spec = resolve(symbol)
    pip = float(spec.pip_size or 0.0001)
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    atr = compute_atr(df, 14).to_numpy(float)
    sp = spread_pips_series(df, spec)
    if spread_mode == "spec":
        sp = np.full(len(df), float(getattr(spec, "typical_spread_pips", 2.0) or 2.0))
    times = df["time"].to_numpy()
    n = len(df)

    trend = h1_trend_flags(df_h1, times) if (htf_filter and df_h1 is not None) else None

    engine = FairValueGapEngine()
    entries: List[Dict[str, Any]] = []
    n_signals = 0

    for b in range(lookback, n - 1):
        fvgs = fresh_fvgs(engine, df, b, lookback)
        if not fvgs:
            continue
        # Displacement candle is the MIDDLE bar of the 3-candle pattern.
        rng = max(h[b - 1] - l[b - 1], 1e-12)
        body = abs(c[b - 1] - o[b - 1])
        if body / rng < disp_min:
            continue

        for f in fvgs:
            top = float(f["top"])
            bot = float(f["bottom"])
            direction = str(f["direction"])
            if not (top > bot):
                continue
            n_signals += 1

            if trend is not None:
                want = 1 if direction == "BULLISH" else -1
                if trend[b] != want:
                    continue

            ce = (top + bot) / 2.0
            side = "BUY" if direction == "BULLISH" else "SELL"

            # Retrace must actually happen: first later bar that trades into CE.
            j = None
            hi = min(n - 1, b + wait + 1)
            for k in range(b + 1, hi):
                if side == "BUY" and l[k] <= ce:
                    j = k
                    break
                if side == "SELL" and h[k] >= ce:
                    j = k
                    break
            if j is None:
                continue

            buffer = float(buffer_atr) * float(atr[b])
            sl = (bot - buffer) if side == "BUY" else (top + buffer)
            # CE is a mid-market level; entry_fill charges the correct side of
            # the spread (ask for a long, bid for a short).
            fill = entry_fill(ce, side, float(sp[j]), pip)
            risk = abs(fill - sl)
            if risk <= 0:
                continue

            entries.append({
                "symbol": symbol,
                "side": side,
                "signal_bar": int(b),
                "entry_bar": int(j),
                "time": str(times[j]),
                "ref": float(ce),
                "fill": float(fill),
                "sl": float(sl),
                "risk": float(risk),
                "gap_size": float(top - bot),
                "risk_atr": float(risk / max(atr[b], 1e-12)),
                "spread_pips": float(sp[j]),
            })

    return entries, n_signals


# ─────────────────────────────────────────────────────────────────────────────
# simulation
# ─────────────────────────────────────────────────────────────────────────────
def simulate_entries(symbol: str, df: pd.DataFrame, entries: Sequence[Dict[str, Any]],
                     tp_r: float, slip_price: float,
                     long_baseline: bool = False,
                     entry_bar_mode: str = "stop-only") -> Tuple[np.ndarray, List[Dict[str, Any]], List[int]]:
    """Replay entries at ``tp_r``; returns (R array, uniqueness spans, kept idx).

    ``long_baseline=True`` keeps the SAME entry bars, the SAME risk distance and
    the SAME target, but forces the direction LONG and re-prices the entry as
    the ask (``ref + spread``) rather than reusing the stored fill — a SELL
    candidate's fill is the bid, and reusing it as a long entry would hand the
    control a free half-spread.

    ``entry_bar_mode`` decides how the RETRACE bar (bar ``j``) is treated, since
    the entry fills intrabar on it:

    * ``stop-only`` (default, honest): test the stop on bar j, never the target
      — bar j's high may have printed before price retraced to the CE.
    * ``none``: ignore bar j entirely (optimistic on the stop side).
    * ``both``: let the simulator test stop *and* target on bar j — the
      lookahead variant, kept only to measure its size.
    """
    spec = resolve(symbol)
    pip = float(spec.pip_size or 0.0001)
    money = get_dollar_risk_per_price_unit(symbol, None)
    work = df.copy()
    if "atr" not in work.columns:
        work["atr"] = compute_atr(work, 14)
    bars = BarArrays.from_df(work)
    high = work["high"].to_numpy(float)
    low = work["low"].to_numpy(float)
    geom = Geometry(tp_r=float(tp_r))
    n = len(work)

    r_out: List[float] = []
    spans: List[Dict[str, Any]] = []
    kept: List[int] = []

    for k, e in enumerate(entries):
        if long_baseline:
            side = "BUY"
            fill = entry_fill(float(e["ref"]), "BUY", float(e["spread_pips"]), pip)
            sl = fill - float(e["risk"])
        else:
            side = str(e["side"])
            fill = float(e["fill"])
            sl = float(e["sl"])

        j = int(e["entry_bar"])
        direction = 1.0 if side.upper().startswith("BUY") else -1.0
        risk = abs(fill - sl)
        if risk <= 0 or j >= n - 1:
            continue

        # ── the retrace bar itself ──────────────────────────────────────────
        if entry_bar_mode == "stop-only":
            # The entry is intrabar (a limit at the CE), so the remainder of bar
            # j is live. Test ONLY the stop on that bar — never the target: bar
            # j's high may have printed *before* price retraced to the CE, so
            # booking a same-bar target would be lookahead. Booking a same-bar
            # stop is the conservative half of the same ambiguity.
            hit_sl = (low[j] <= sl) if direction > 0 else (high[j] >= sl)
            if hit_sl:
                exit_price = sl - direction * float(slip_price)
                pnl_r = (exit_price - fill) * direction / risk
                r_out.append(float(pnl_r))
                spans.append({"entry_bar": j, "duration_bars": 1, "pnl": float(pnl_r)})
                kept.append(k)
                continue
            entry_idx = j
        elif entry_bar_mode == "none":
            entry_idx = j
        else:  # "both" — lookahead variant
            entry_idx = j - 1

        # Live from bar entry_idx+1; the simulator tests stop before target and
        # pays slippage on stop fills, matching tools/audit_trade_quality.py.
        out = simulate_trade(
            symbol=symbol, side=side, entry_idx=entry_idx,
            fill=fill, sl=sl, geom=geom, money_per_unit=money, bars=bars,
            cost_price_equiv=0.0, slippage_price_equiv=float(slip_price), spec=spec,
        )
        if out is None:
            continue
        r_out.append(float(out.pnl_r))
        spans.append({
            "entry_bar": int(out.entry_idx),
            "duration_bars": max(1, int(out.exit_idx) - int(out.entry_idx) + 1),
            "pnl": float(out.pnl_r),
        })
        kept.append(k)

    return np.asarray(r_out, dtype=float), spans, kept


def summarise_r(r: np.ndarray) -> Dict[str, float]:
    if r.size == 0:
        return {"n": 0, "mean_r": float("nan"), "win_rate": float("nan"),
                "total_r": 0.0, "sharpe_per_obs": float("nan")}
    wins = r[r > 0]
    return {
        "n": int(r.size),
        "mean_r": float(r.mean()),
        "win_rate": float(len(wins) / r.size),
        "total_r": float(r.sum()),
        "sharpe_per_obs": float(r.mean() / r.std(ddof=1)) if r.std(ddof=1) > 1e-12 else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal-tf", default="M15")
    ap.add_argument("--signal-window", type=int, default=183)
    ap.add_argument("--structure-tf", default="H1")
    ap.add_argument("--structure-window", type=int, default=365)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--lookback", type=int, default=60,
                    help="bars of history handed to the FVG detector")
    ap.add_argument("--wait", type=int, default=30,
                    help="max bars after the gap to wait for the CE retrace")
    ap.add_argument("--buffer-atr", type=float, default=0.25,
                    help="stop buffer beyond the gap, in ATR14")
    ap.add_argument("--disp-min", type=float, default=DISPLACEMENT_MIN)
    ap.add_argument("--slip-pips", type=float, default=0.5)
    ap.add_argument("--tp-grid", default=",".join(f"{t:g}" for t in TP_GRID_DEFAULT))
    ap.add_argument("--htf-filter", action="store_true",
                    help="only take FVGs aligned with the causal H1 EMA trend")
    ap.add_argument("--spread-mode", default="data", choices=("data", "spec"),
                    help="'data' charges each bar's recorded spread; 'spec' the registry typical")
    ap.add_argument("--entry-bar-mode", default="stop-only",
                    choices=("stop-only", "none", "both"),
                    help="how the intrabar retrace bar is tested (see simulate_entries)")
    ap.add_argument("--out", default=os.path.join(REPO, "reports", "fvg_standalone_backtest.json"))
    args = ap.parse_args()

    tp_grid = [float(x) for x in args.tp_grid.split(",") if x.strip()]
    prod_tp = float(PRODUCTION_TP)
    prod_key = f"{prod_tp:.2f}"

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(d for d in os.listdir(REAL_DIR)
                         if os.path.isdir(os.path.join(REAL_DIR, d)))

    print(f"FVG standalone | signal {args.signal_tf} {args.signal_window}d | "
          f"structure {args.structure_tf} {args.structure_window}d | "
          f"disp>={args.disp_min} | CE retrace<={args.wait}b | "
          f"stop=gap+{args.buffer_atr}ATR | tp={prod_tp}R | "
          f"htf_filter={args.htf_filter}")
    print(f"{'sym':8s} {'signals':>8s} {'trades':>7s} {'n_eff':>7s} {'meanR':>8s} "
          f"{'win%':>6s} {'SR':>7s} {'PSR':>6s} {'DSR':>7s} {'DSReff':>7s} "
          f"{'longR':>8s} {'delta':>8s}  verdict")
    print("-" * 118)

    rows: List[Dict[str, Any]] = []
    pooled_prod: List[np.ndarray] = []
    pooled_spans: List[Dict[str, Any]] = []
    all_trial_sr: List[float] = []
    pooled_long: List[np.ndarray] = []

    for sym in symbols:
        t0 = time.time()
        df = load_bars(sym, args.signal_tf, args.signal_window)
        if df is None or len(df) < args.lookback + 10:
            print(f"{sym:8s}  -- no data")
            continue
        df_h1 = load_bars(sym, args.structure_tf, args.structure_window)
        spec = resolve(sym)
        pip = float(spec.pip_size or 0.0001)
        slip = args.slip_pips * pip

        entries, n_signals = build_entries(
            sym, df, df_h1, lookback=args.lookback, wait=args.wait,
            buffer_atr=args.buffer_atr, disp_min=args.disp_min,
            htf_filter=args.htf_filter, spread_mode=args.spread_mode,
        )
        if not entries:
            print(f"{sym:8s} {n_signals:8d}  -- no retrace entries")
            continue

        # ── trial grid (deflation input) ────────────────────────────────────
        series: Dict[str, np.ndarray] = {}
        spans_by_tp: Dict[str, List[Dict[str, Any]]] = {}
        for tp in tp_grid:
            r, sp, _ = simulate_entries(sym, df, entries, tp, slip,
                                            entry_bar_mode=args.entry_bar_mode)
            if r.size == 0:
                continue
            key = f"{tp:.2f}"
            series[key] = r
            spans_by_tp[key] = sp
        if prod_key not in series:
            print(f"{sym:8s} {n_signals:8d}  -- no production-tp trades")
            continue

        trial_sr = {k: sharpe_stats(v, periods_per_year=BARS_PER_YEAR_M15)["sr"]
                    for k, v in series.items()}
        all_trial_sr.extend(trial_sr.values())
        var_trial = float(np.var(list(trial_sr.values()), ddof=1)) if len(trial_sr) > 1 else 0.0
        n_trials_sym = len(trial_sr)
        best_key = max(trial_sr, key=lambda k: trial_sr[k])

        def _row(key: str) -> Dict[str, float]:
            r = series[key]
            st = sharpe_stats(r, periods_per_year=BARS_PER_YEAR_M15)
            psr = probabilistic_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"])
            dsr = deflated_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"],
                                        n_trials_sym, var_trial)
            n_eff = SampleUniquenessWeightEngine.effective_sample_size(spans_by_tp[key])
            dsr_eff = deflated_sharpe_ratio(st["sr"], int(round(max(n_eff, 2))),
                                            st["skew"], st["kurt"], n_trials_sym, var_trial)
            return {"n": st["n"], "n_eff": n_eff, "sr": st["sr"], "skew": st["skew"],
                    "kurt": st["kurt"], "psr": psr, "dsr": dsr, "dsr_eff": dsr_eff}

        prod = _row(prod_key)
        best = _row(best_key)

        # ── always-long baseline on the SAME entries / risk / target ────────
        r_long, _, kept_long = simulate_entries(sym, df, entries, prod_tp, slip,
                                                long_baseline=True,
                                                entry_bar_mode=args.entry_bar_mode)
        r_fvg, _, kept_fvg = simulate_entries(sym, df, entries, prod_tp, slip,
                                              entry_bar_mode=args.entry_bar_mode)
        # Align paired by original entry index (both should keep the same set).
        common = sorted(set(kept_fvg) & set(kept_long))
        idx_f = {k: i for i, k in enumerate(kept_fvg)}
        idx_l = {k: i for i, k in enumerate(kept_long)}
        a_f = np.array([r_fvg[idx_f[k]] for k in common], dtype=float)
        a_l = np.array([r_long[idx_l[k]] for k in common], dtype=float)
        long_mean = float(a_l.mean()) if a_l.size else float("nan")
        delta = float((a_f - a_l).mean()) if a_f.size else float("nan")

        # Paired t-stat on the per-entry difference (direction skill only).
        t_stat = float("nan")
        if a_f.size > 2 and np.std(a_f - a_l, ddof=1) > 1e-12:
            diff = a_f - a_l
            t_stat = float(diff.mean() / (diff.std(ddof=1) / math.sqrt(diff.size)))

        s = summarise_r(series[prod_key])
        pooled_prod.append(series[prod_key])
        pooled_spans.extend(spans_by_tp[prod_key])
        pooled_long.append(a_l)

        verdict = []
        verdict.append("DSR" if prod["dsr_eff"] > ACCEPT_DSR else "dsrX")
        verdict.append(">LONG" if delta > 0 else "<=LONG")

        rows.append({
            "symbol": sym,
            "n_signals": int(n_signals),
            "n_trades": s["n"],
            "n_effective": round(prod["n_eff"], 1),
            "uniqueness_ratio": round(prod["n_eff"] / prod["n"], 4) if prod["n"] else None,
            "mean_r": round(s["mean_r"], 5),
            "win_rate": round(s["win_rate"], 4),
            "total_r": round(s["total_r"], 3),
            "sr_per_obs": round(prod["sr"], 6),
            "sr_annual": round(prod["sr"] * math.sqrt(BARS_PER_YEAR_M15), 4),
            "skew": round(prod["skew"], 4),
            "kurt": round(prod["kurt"], 4),
            "psr": round(prod["psr"], 6),
            "dsr_selected": round(best["dsr"], 6),
            "dsr_selected_effective_n": round(best["dsr_eff"], 6),
            "best_tp_r": float(best_key),
            "dsr_production": round(prod["dsr"], 6),
            "dsr_production_effective_n": round(prod["dsr_eff"], 6),
            "passes_dsr_095_effective_n": bool(prod["dsr_eff"] > ACCEPT_DSR),
            "always_long_mean_r": round(long_mean, 5),
            "delta_over_always_long": round(delta, 5),
            "paired_t": round(t_stat, 4) if math.isfinite(t_stat) else None,
            "beats_always_long": bool(delta > 0),
            "n_trials_symbol": int(n_trials_sym),
            "var_of_trial_sharpes": round(var_trial, 8),
        })

        print(f"{sym:8s} {n_signals:8d} {s['n']:7d} {prod['n_eff']:7.1f} "
              f"{s['mean_r']:+8.4f} {s['win_rate'] * 100:6.1f} {prod['sr']:+7.4f} "
              f"{prod['psr']:6.3f} {prod['dsr']:7.3f} {prod['dsr_eff']:7.3f} "
              f"{long_mean:+8.4f} {delta:+8.4f}  {' '.join(verdict)}   "
              f"({time.time() - t0:.0f}s)")

    # ── portfolio ──────────────────────────────────────────────────────────
    book = np.concatenate(pooled_prod) if pooled_prod else np.array([])
    book_long = np.concatenate(pooled_long) if pooled_long else np.array([])
    n_trials_book = len(tp_grid) * max(len(rows), 1)
    var_book = float(np.var(all_trial_sr, ddof=1)) if len(all_trial_sr) > 1 else 0.0

    portfolio: Dict[str, Any] = {}
    if book.size:
        st = sharpe_stats(book, periods_per_year=BARS_PER_YEAR_M15)
        book_psr = probabilistic_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"])
        book_dsr = deflated_sharpe_ratio(st["sr"], st["n"], st["skew"], st["kurt"],
                                        n_trials_book, var_book)
        book_n_eff = SampleUniquenessWeightEngine.effective_sample_size(pooled_spans)
        book_dsr_eff = deflated_sharpe_ratio(st["sr"], int(round(max(book_n_eff, 2))),
                                             st["skew"], st["kurt"], n_trials_book, var_book)
        long_mean_book = float(book_long.mean()) if book_long.size else float("nan")
        portfolio = {
            "n": int(st["n"]),
            "n_effective": round(book_n_eff, 1),
            "uniqueness_ratio": round(book_n_eff / st["n"], 4) if st["n"] else None,
            "mean_r": round(float(book.mean()), 5),
            "win_rate": round(float((book > 0).mean()), 4),
            "sr_per_obs": round(st["sr"], 6),
            "sr_annual": round(st["sr"] * math.sqrt(BARS_PER_YEAR_M15), 4),
            "skew": round(st["skew"], 4),
            "kurt": round(st["kurt"], 4),
            "n_trials": n_trials_book,
            "var_of_trial_sharpes": round(var_book, 8),
            "expected_max_sharpe": round(expected_max_sharpe(var_book, n_trials_book), 6),
            "psr": round(book_psr, 6),
            "dsr": round(book_dsr, 6),
            "dsr_effective_n": round(book_dsr_eff, 6),
            "passes_dsr_095": bool(book_dsr > ACCEPT_DSR),
            "passes_dsr_095_effective_n": bool(book_dsr_eff > ACCEPT_DSR),
            "always_long_mean_r": round(long_mean_book, 5),
            "delta_over_always_long": round(float(book.mean() - long_mean_book), 5),
            "beats_always_long": bool(book.mean() > long_mean_book),
        }
        print("\n" + "-" * 118)
        print(f"{'BOOK':8s} {'':>8s} {st['n']:7d} {book_n_eff:7.1f} {book.mean():+8.4f} "
              f"{(book > 0).mean() * 100:6.1f} {st['sr']:+7.4f} {book_psr:6.3f} "
              f"{book_dsr:7.3f} {book_dsr_eff:7.3f} {long_mean_book:+8.4f} "
              f"{book.mean() - long_mean_book:+8.4f}")
        print(f"  portfolio deflation: n_trials={n_trials_book} "
              f"var(trial SR)={var_book:.6f} E[max SR]={expected_max_sharpe(var_book, n_trials_book):+.6f}")
        print(f"  uniqueness: {st['n']} trades are worth {book_n_eff:.0f} independent bets "
              f"({book_n_eff / st['n']:.1%})")

    n_pass_dsr = sum(1 for r in rows if r["passes_dsr_095_effective_n"])
    n_beat_long = sum(1 for r in rows if r["beats_always_long"])
    n_profitable = sum(1 for r in rows if r["mean_r"] > 0)

    # Verdict: BOTH bars must clear, per-symbol and at portfolio level.
    sym_both = sum(1 for r in rows
                   if r["passes_dsr_095_effective_n"] and r["beats_always_long"])
    portfolio_both = bool(portfolio.get("passes_dsr_095_effective_n")
                          and portfolio.get("beats_always_long"))
    if portfolio_both and sym_both >= max(3, len(rows) // 2):
        verdict = "PROCEED"
    elif portfolio_both:
        verdict = "DO NOT PROCEED (portfolio clears, per-symbol does not)"
    else:
        verdict = "DO NOT PROCEED"

    print("-" * 118)
    print(f"  profitable on               : {n_profitable}/{len(rows)}")
    print(f"  pass DSR>0.95 (effective n) : {n_pass_dsr}/{len(rows)}")
    print(f"  beat always-long            : {n_beat_long}/{len(rows)}")
    print(f"  clear BOTH bars per symbol  : {sym_both}/{len(rows)}")
    print(f"  PORTFOLIO both bars         : {portfolio_both}")
    print(f"  VERDICT                     : {verdict}")

    payload = {
        "generated": pd.Timestamp.now("UTC").isoformat(),
        "config": {
            "signal_tf": args.signal_tf, "signal_window_days": args.signal_window,
            "structure_tf": args.structure_tf, "structure_window_days": args.structure_window,
            "displacement_min": args.disp_min,
            "displacement_candle": "middle bar of the 3-candle FVG",
            "entry": "50% consequent encroachment (CE) on retrace",
            "wait_bars": args.wait, "lookback_bars": args.lookback,
            "stop": f"far gap edge +/- {args.buffer_atr} x ATR14",
            "target_tp_r": prod_tp, "tp_grid": tp_grid,
            "slippage_pips": args.slip_pips, "commission": 0.0,
            "htf_filter": bool(args.htf_filter),
            "entry_bar_mode": args.entry_bar_mode,
            "spread_mode": args.spread_mode,
            "spread_semantics": ("raw 'spread' column is in POINTS; converted with "
                                 "raw * point / pip_size (never raw * pip_size)"),
            "cost_model": ("entry_fill charges one spread at the CE mid "
                           "(ask=CE+spread, bid=CE-spread); stop fills pay "
                           f"{args.slip_pips} pip slippage"),
            "entry_bar_convention": ("simulator's first tested bar is the retrace bar "
                                     "itself (entry_idx = retrace_bar - 1)"),
        },
        "acceptance_dsr": ACCEPT_DSR,
        "n_symbols": len(rows),
        "n_profitable": n_profitable,
        "n_pass_dsr_effective_n": n_pass_dsr,
        "n_beat_always_long": n_beat_long,
        "n_clear_both_bars": sym_both,
        "portfolio_clears_both_bars": portfolio_both,
        "verdict": verdict,
        "portfolio": portfolio,
        "symbols": rows,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nwritten -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
