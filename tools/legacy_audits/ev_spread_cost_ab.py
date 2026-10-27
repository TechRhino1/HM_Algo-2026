#!/usr/bin/env python
"""EV spread-cost A/B — should ``spread_cost`` use the real per-bar spread?

THE QUESTION
------------
`decision_engine.py:261` charges the modelled cost of a trade as

    spread_cost = context.volatility.current_spread_pips * pip_val_per_lot * est_lots

and on the live path `current_spread_pips` is the registry constant
`spec.typical_spread_pips`, not a measurement (every live `build_context` caller
passes the constant — `orchestrator.py:686`, `server.py:257`). For the tight FX
majors the registry understates the real quoted spread by ~2.9x (EURUSD registry
0.7 pips vs real median 1.9). This tool measures what happens to trade selection
and P&L if that cost term is pointed at the real per-bar spread.

ARMS  (identical bars, identical candidate universe)
----------------------------------------------------
A      BASELINE      registry constant everywhere — the live path today.
B      LITERAL       the instructed monkeypatch: swap
                     `current_spread_pips` -> real for the duration of
                     `_compute_blended_probability`. NOTE this also moves the ML
                     predictor's spread-friction feature (see below), so it is
                     not a pure `spread_cost` isolation.
Bpure  PURE COST     ONLY line 261 sees the real spread. The ML spread feature is
                     pinned back to the registry, so nothing but `spread_cost`
                     moves. This is the faithful model of the proposed change
                     (`spread_cost = context.live_spread_pips * ...`).
D      FULL COST     BOTH copies of the cost term see the real spread — line 261
                     AND the second, independent `_spread_cost` at line 954 that
                     actually feeds the strategy-EV (and therefore the `ev` the
                     quality gate reads). Gates, geometry and the ML feature stay
                     on the registry.
C      FULL REAL     `current_spread_pips` = real per-bar spread everywhere
                     (gates + stops + geometry + EV + ML feature). Reference arm /
                     harness sanity check.

WHY Bpure AND D EXIST (established by reading, then confirmed by the run)
------------------------------------------------------------------------
1. `_compute_blended_probability` is not the only reader of the spread inside the
   method. `self.ml_predictor.extract_feature_vector(...)` (called at line 211)
   reads `vol.current_spread_pips` at `online_ml_predictor.py:215` to build its
   "Spread Friction Ratio" feature. The AST walk that isolated "exactly one line,
   261" walked the method body only; it cannot see into the callees. So a raw
   method-level swap moves the ML win-probability feature as well. `Bpure` pins
   it; `B` does not.
2. The `ev` returned by `_compute_blended_probability` does NOT reach the quality
   gate. At line 1030 the local `ev` is OVERWRITTEN by the strategy EV:

       ev = selected_eval["ev"]        # built at line 997

   and that strategy EV is computed from a SECOND spread-cost copy at line 954
   (`_spread_cost = context.volatility.current_spread_pips * ...`). So the four
   gates the task named (364, 411, 413, 660) are fed by line 954's cost, not line
   261's. Line 261's EV reaches only the AI-dissection pillar (line 874) and the
   master-confluence threshold (line 904). Arm D is the version of the proposed
   change that also reaches those four gates.

HONESTY
-------
Read-only with respect to `jarvis/` and `tests/`. Every monkeypatch lives inside
this tool and is removed in `finally` blocks. Writes only under `reports/` and
`.scratch/`.

Run::
    python tools/ev_spread_cost_ab.py                       # all 20 symbols
    python tools/ev_spread_cost_ab.py --symbols EURUSD --limit-bars 400
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from jarvis.backtesting.fills import entry_fill  # noqa: E402
from jarvis.backtesting.signal_scan import SignalScanner, compute_atr  # noqa: E402
from jarvis.backtesting.trade_simulator import (  # noqa: E402
    BarArrays,
    Geometry,
    simulate_trade,
)
from jarvis.config.runtime import offline_mode  # noqa: E402
from jarvis.data.symbol_registry import (  # noqa: E402
    get_dollar_risk_per_price_unit,
    resolve,
)
from jarvis.intelligence.decision_engine import DecisionEngine, _is_forex  # noqa: E402
from jarvis.learning.online_ml_predictor import OnlineMLPredictor  # noqa: E402
from jarvis.risk.account_tier import get_effective_min_ev  # noqa: E402

REAL_DIR = os.path.join(REPO, "data", "market", "real")
ACCOUNT_BALANCE = 10_000.0
RISK_PCT = 0.5
SLIP_PIPS_DEFAULT = 0.5
ARMS = ("A", "B", "Bpure", "D", "C", "R")
OTHER_ARMS = ("B", "Bpure", "D", "C", "R")

# --------------------------------------------------------------------------
# Arm machinery — all monkeypatching is local to this module.
# --------------------------------------------------------------------------
_ORIG_BLENDED = DecisionEngine._compute_blended_probability
_ORIG_EVALUATE = DecisionEngine.evaluate
_ORIG_BIAS_LEVELS = DecisionEngine._compute_bias_and_levels
_ORIG_QUALITY_GATE = DecisionEngine._apply_quality_gate
_ORIG_EXTRACT = OnlineMLPredictor.extract_feature_vector

_BLENDED_LOG: List[Dict[str, Any]] = []
_D_STATE: Dict[str, Optional[float]] = {"reg": None}
_PIN_STATE: Dict[str, Optional[float]] = {"reg": None}


def _real_spread_of(context: Any) -> Optional[float]:
    """`context.live_spread_pips` if it is a usable positive number, else None."""
    raw = getattr(context, "live_spread_pips", None)
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) and val > 0.0 else None


def _recording_blended(self, context, *args, **kwargs):
    """Pass-through wrapper that logs the returned EV and the spread it saw."""
    out = _ORIG_BLENDED(self, context, *args, **kwargs)
    try:
        _BLENDED_LOG.append({
            "ev": float(out[2]),
            "win_p": float(out[0]),
            "spread_seen": float(getattr(context.volatility, "current_spread_pips", float("nan"))),
        })
    except Exception:  # observability must never break the measurement
        pass
    return out


def _swap_blended(self, context, *args, **kwargs):
    """`current_spread_pips` = real for the duration of this one call, then restored."""
    vol = context.volatility
    reg = float(getattr(vol, "current_spread_pips", 0.0) or 0.0)
    real = _real_spread_of(context)
    if real is None:
        return _recording_blended(self, context, *args, **kwargs)
    vol.current_spread_pips = real
    try:
        return _recording_blended(self, context, *args, **kwargs)
    finally:
        vol.current_spread_pips = reg


def _armB_blended(self, context, *args, **kwargs):
    """Arm B (literal): the swap is visible to the ML feature too."""
    return _swap_blended(self, context, *args, **kwargs)


def _armBpure_blended(self, context, *args, **kwargs):
    """Arm Bpure: only line 261 sees the real spread; the ML feature is pinned."""
    reg = float(getattr(context.volatility, "current_spread_pips", 0.0) or 0.0)
    _PIN_STATE["reg"] = reg
    try:
        return _swap_blended(self, context, *args, **kwargs)
    finally:
        _PIN_STATE["reg"] = None


def _pinned_extract(self, *args, **kwargs):
    """Keep the ML spread-friction feature on the registry spread while pinned."""
    reg = _PIN_STATE["reg"]
    if reg is None:
        return _ORIG_EXTRACT(self, *args, **kwargs)
    ctx = kwargs.get("context")
    if ctx is None and args:
        ctx = args[0]
    vol = getattr(ctx, "volatility", None) if ctx is not None else None
    if vol is None:
        return _ORIG_EXTRACT(self, *args, **kwargs)
    saved = vol.current_spread_pips
    vol.current_spread_pips = reg
    try:
        return _ORIG_EXTRACT(self, *args, **kwargs)
    finally:
        vol.current_spread_pips = saved


def _D_bias_levels(self, context, *args, **kwargs):
    """While Arm D is active, keep the geometry on the registry constant."""
    vol = context.volatility
    saved = vol.current_spread_pips
    reg = _D_STATE["reg"]
    if reg is not None:
        vol.current_spread_pips = reg
    try:
        return _ORIG_BIAS_LEVELS(self, context, *args, **kwargs)
    finally:
        vol.current_spread_pips = saved


def _D_quality_gate(self, *args, **kwargs):
    """While Arm D is active, keep the quality gate's `spread` on the registry."""
    reg = _D_STATE["reg"]
    if reg is not None and "spread" in kwargs:
        kwargs = dict(kwargs)
        kwargs["spread"] = reg
    return _ORIG_QUALITY_GATE(self, *args, **kwargs)


def _armD_evaluate(self, context, *args, **kwargs):
    """Arm D: real spread for the whole `evaluate`, except geometry/gate/ML feature."""
    vol = context.volatility
    reg = float(getattr(vol, "current_spread_pips", 0.0) or 0.0)
    real = _real_spread_of(context)
    if real is None:
        return _ORIG_EVALUATE(self, context, *args, **kwargs)
    _D_STATE["reg"] = reg
    _PIN_STATE["reg"] = reg
    vol.current_spread_pips = real
    try:
        return _ORIG_EVALUATE(self, context, *args, **kwargs)
    finally:
        vol.current_spread_pips = reg
        _D_STATE["reg"] = None
        _PIN_STATE["reg"] = None


@contextlib.contextmanager
def _arm_active(name: str):
    prev_b = DecisionEngine._compute_blended_probability
    prev_e = DecisionEngine.evaluate
    try:
        if name == "B":
            DecisionEngine._compute_blended_probability = _armB_blended
        elif name == "Bpure":
            DecisionEngine._compute_blended_probability = _armBpure_blended
        else:
            DecisionEngine._compute_blended_probability = _recording_blended
        DecisionEngine.evaluate = _armD_evaluate if name == "D" else _ORIG_EVALUATE
        yield
    finally:
        DecisionEngine._compute_blended_probability = prev_b
        DecisionEngine.evaluate = prev_e


@contextlib.contextmanager
def _all_arms_installed():
    """Install the always-on helpers (no-ops unless their arm is active)."""
    DecisionEngine._compute_bias_and_levels = _D_bias_levels
    DecisionEngine._apply_quality_gate = _D_quality_gate
    DecisionEngine._compute_blended_probability = _recording_blended
    OnlineMLPredictor.extract_feature_vector = _pinned_extract
    try:
        yield
    finally:
        DecisionEngine._compute_bias_and_levels = _ORIG_BIAS_LEVELS
        DecisionEngine._apply_quality_gate = _ORIG_QUALITY_GATE
        DecisionEngine._compute_blended_probability = _ORIG_BLENDED
        OnlineMLPredictor.extract_feature_vector = _ORIG_EXTRACT


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _eval_arm(engine, arm, context, regime, reports, devil, mtf):
    _BLENDED_LOG.clear()
    with _arm_active(arm):
        dec = engine.evaluate(
            context, regime, reports, devil,
            account_balance=ACCOUNT_BALANCE, risk_per_trade_pct=RISK_PCT,
            mtf_data=mtf,
        )
    blended = _BLENDED_LOG[-1] if _BLENDED_LOG else None
    return dec, blended


def _arm_fields(dec, blended, selected, pnl_r) -> Dict[str, Any]:
    qg = getattr(dec, "quality_gate", None)
    failing = list(getattr(qg, "failing_reasons", []) or [])
    return {
        "ev": round(float(getattr(dec, "expected_value", 0.0) or 0.0), 4),
        "ev_blended": round(float(blended["ev"]), 4) if blended else None,
        "spread_seen": round(float(blended["spread_seen"]), 6) if blended else None,
        "decision": str(getattr(dec, "decision", "NO_TRADE")),
        "gate_passed": bool(getattr(qg, "passed", False)),
        "rr": round(float(getattr(dec, "risk_reward_ratio", 0.0) or 0.0), 4),
        "n_failed": len(failing),
        "failing": "|".join(failing),
        "score": round(float(getattr(dec, "model_confidence", 0.0) or 0.0), 6),
        "dissection": round(float(getattr(dec, "dissection_score", 0.0) or 0.0), 4),
        "master": round(float(getattr(dec, "master_confluence_score", 0.0) or 0.0), 4),
        "selected": bool(selected),
        "r": (round(float(pnl_r), 6) if pnl_r is not None else None),
    }


def _replay_selected(symbol, side, bar_idx, fill, sl, rr, spec, money, bars, slip_price,
                     dec) -> Optional[float]:
    risk_dist = abs(fill - sl)
    if not (math.isfinite(risk_dist) and risk_dist > 0) or not (math.isfinite(rr) and rr > 0):
        return None
    geom = Geometry(tp_r=float(rr))
    regime_val = getattr(getattr(dec, "regime", None), "primary_regime", None)
    regime_str = getattr(regime_val, "value", str(regime_val or "UNKNOWN"))
    out = simulate_trade(
        symbol=symbol, side=side, entry_idx=int(bar_idx), fill=float(fill), sl=float(sl),
        geom=geom, money_per_unit=money, bars=bars, cost_price_equiv=0.0,
        slippage_price_equiv=slip_price,
        ai_score=float(getattr(dec, "model_confidence", 0.0) or 0.0),
        calibrated_win_p=float(getattr(dec, "model_confidence", 0.0) or 0.0),
        regime=str(regime_str), strategy=str(getattr(dec, "strategy", "UNKNOWN")),
        zone=str(getattr(getattr(getattr(dec, "context", None), "structure", None),
                         "discount_premium_zone", "UNKNOWN")),
        spec=spec,
    )
    return None if out is None else float(out.pnl_r)


# --------------------------------------------------------------------------
# Per-symbol scan
# --------------------------------------------------------------------------
def scan_symbol(symbol: str, tf: str, window: int, start_bar_idx: int,
                limit_bars: Optional[int], check_determinism: bool,
                analyst_timeout: float = 60.0
                ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    bars_path = os.path.join(REAL_DIR, symbol, f"{symbol}_{tf}_{window}d.parquet")
    if not os.path.exists(bars_path):
        return None, [], f"missing {bars_path}"

    df = pd.read_parquet(bars_path).sort_values("time").reset_index(drop=True)
    df["time"] = pd.to_datetime(df["time"])
    df["atr"] = compute_atr(df, 14)
    spec = resolve(symbol)
    pip = float(getattr(spec, "pip_size", 0.0001) or 0.0001)
    reg = float(getattr(spec, "typical_spread_pips", 2.0) or 2.0)
    slip_price = SLIP_PIPS_DEFAULT * pip
    money = get_dollar_risk_per_price_unit(symbol, None)
    bars = BarArrays.from_df(df)
    total = len(df)

    scanner = SignalScanner(start_bar_idx=start_bar_idx)
    # The cluster's 2.0s default makes slow analysts fall back to a NEUTRAL
    # score-50 report non-deterministically (documented in AUDIT-3-TRACKS §J3).
    # A measurement must be reproducible, so raise it well past any real latency.
    # `--analyst-timeout 2.0` reproduces the historical (timeout-degraded) scan.
    scanner.analyst_cluster.timeout_sec = float(analyst_timeout)
    h4, d1 = scanner._resample(df)
    ce, rc, cluster, engine = (
        scanner.context_engine, scanner.regime_classifier,
        scanner.analyst_cluster, scanner.decision_engine,
    )

    eff_min_ev = get_effective_min_ev(ACCOUNT_BALANCE, max(0.50, ACCOUNT_BALANCE * RISK_PCT / 100.0))
    is_fx = _is_forex(symbol) and ("JPY" not in symbol.upper())
    point = 10.0 ** (-int(getattr(spec, "digits", 5) or 5))

    # Arm R — "correct the registry to the observed spread": a CONSTANT = the
    # series median, fed everywhere the registry constant is fed today. This is
    # the change AUDIT-3-TRACKS §J2 measured (EXECUTE 2440→2839, total R
    # −115→−240); it is NOT the same change as arm C (real per-bar spread).
    _raw = pd.to_numeric(df.get("spread"), errors="coerce") if "spread" in df.columns else None
    if _raw is not None and _raw.notna().any():
        reg_obs = float(np.median(_raw.dropna().to_numpy(dtype=float))) * point / pip
    else:
        reg_obs = reg
    if not (math.isfinite(reg_obs) and reg_obs > 0):
        reg_obs = reg

    start = max(start_bar_idx, 60)
    stop = total - 1 if limit_bars is None else min(total - 1, start + limit_bars)

    records: List[Dict[str, Any]] = []
    n_bars = 0
    det_mismatch = 0

    with _all_arms_installed():
        for i in range(start, stop):
            history = df.iloc[max(0, i - 300):i]
            current = df.iloc[i]
            nxt = df.iloc[i + 1]
            bar_time = current["time"]
            h4_slice = h4[h4["time"] <= bar_time].iloc[-100:]
            d1_slice = d1[d1["time"] <= bar_time].iloc[-50:]
            mtf = {"primary": history, "context": h4_slice, "macro": d1_slice}

            raw = current.get("spread", None)
            real = reg
            try:
                if raw is not None and np.isfinite(float(raw)):
                    real = float(raw) * point / pip
            except (TypeError, ValueError):
                real = reg
            if not (math.isfinite(real) and real > 0):
                real = reg
            n_bars += 1

            # ── registry context (shared by arms A / B / Bpure / D) ────────
            try:
                ctx_reg = ce.build_context(
                    symbol, mtf, current_spread_pips=reg,
                    max_allowed_spread_pips=spec.max_spread_pips, live_spread_pips=real,
                )
                regime = rc.classify_regime(ctx_reg)
                tentative = (
                    "BUY" if ctx_reg.structure.bias == "BULLISH"
                    else ("SELL" if ctx_reg.structure.bias == "BEARISH"
                          else ("SELL" if getattr(ctx_reg.momentum, "trend_score", 0.0) < 0 else "BUY"))
                )
                reports, devil = cluster.run_all_parallel(ctx_reg, regime, tentative)
                dec_A, bl_A = _eval_arm(engine, "A", ctx_reg, regime, reports, devil, mtf)
            except Exception as exc:
                print(f"  [{symbol}] bar {i} pipeline error: {exc}", flush=True)
                continue

            if str(getattr(dec_A, "bias", "HOLD")).upper() not in ("BUY", "SELL"):
                continue

            if check_determinism:
                dec_A2, _ = _eval_arm(engine, "A", ctx_reg, regime, reports, devil, mtf)
                if (abs(float(dec_A.expected_value) - float(dec_A2.expected_value)) > 1e-9
                        or str(dec_A.decision) != str(dec_A2.decision)):
                    det_mismatch += 1

            dec_B, bl_B = _eval_arm(engine, "B", ctx_reg, regime, reports, devil, mtf)
            dec_Bp, bl_Bp = _eval_arm(engine, "Bpure", ctx_reg, regime, reports, devil, mtf)
            dec_D, bl_D = _eval_arm(engine, "D", ctx_reg, regime, reports, devil, mtf)

            # ── real context (arm C) ───────────────────────────────────────
            try:
                ctx_real = ce.build_context(
                    symbol, mtf, current_spread_pips=real,
                    max_allowed_spread_pips=spec.max_spread_pips, live_spread_pips=real,
                )
                regime_c = rc.classify_regime(ctx_real)
                reports_c, devil_c = cluster.run_all_parallel(ctx_real, regime_c, tentative)
                dec_C, bl_C = _eval_arm(engine, "C", ctx_real, regime_c, reports_c, devil_c, mtf)
            except Exception as exc:
                print(f"  [{symbol}] bar {i} arm-C error: {exc}", flush=True)
                continue

            # ── corrected-registry-constant context (arm R, = §J2) ─────────
            try:
                ctx_R = ce.build_context(
                    symbol, mtf, current_spread_pips=reg_obs,
                    max_allowed_spread_pips=spec.max_spread_pips, live_spread_pips=real,
                )
                regime_r = rc.classify_regime(ctx_R)
                reports_r, devil_r = cluster.run_all_parallel(ctx_R, regime_r, tentative)
                dec_R, bl_R = _eval_arm(engine, "R", ctx_R, regime_r, reports_r, devil_r, mtf)
            except Exception as exc:
                print(f"  [{symbol}] bar {i} arm-R error: {exc}", flush=True)
                continue

            side = str(dec_A.bias).upper()
            rec: Dict[str, Any] = {
                "symbol": symbol, "bar_idx": int(i), "time": str(bar_time), "side": side,
                "reg": round(reg, 6), "real": round(real, 6), "is_fx": bool(is_fx),
            }
            for arm, dec, bl, sp in (
                ("A", dec_A, bl_A, reg),
                ("B", dec_B, bl_B, reg),
                ("Bpure", dec_Bp, bl_Bp, reg),
                ("D", dec_D, bl_D, reg),
                ("C", dec_C, bl_C, real),
                ("R", dec_R, bl_R, reg_obs),
            ):
                selected = (str(getattr(dec, "decision", "")) == "EXECUTE"
                            and str(getattr(dec, "bias", "")).upper() in ("BUY", "SELL"))
                pnl_r = None
                if selected:
                    fill = entry_fill(float(nxt["open"]), side, sp, spec.pip_size)
                    shift = fill - float(dec.entry_price)
                    sl = float(dec.stop_loss) + shift
                    pnl_r = _replay_selected(
                        symbol, side, i, fill, sl,
                        float(getattr(dec, "risk_reward_ratio", 0.0) or 0.0),
                        spec, money, bars, slip_price, dec,
                    )
                rec[arm] = _arm_fields(dec, bl, selected, pnl_r)
            records.append(rec)

    reals = np.array([r["real"] for r in records], dtype=float) if records else np.array([])
    summary = {
        "symbol": symbol,
        "bars_scanned": n_bars,
        "candidates": len(records),
        "det_mismatch": det_mismatch,
        "reg_spread": round(reg, 6),
        "reg_obs_spread": round(reg_obs, 6),
        "real_spread_median": round(float(np.median(reals)), 4) if len(reals) else None,
        "real_spread_mean": round(float(np.mean(reals)), 4) if len(reals) else None,
        "ratio_median": round(float(np.median(reals)) / reg, 4) if (len(reals) and reg > 0) else None,
        "is_fx_major": bool(is_fx),
        "eff_min_ev": round(float(eff_min_ev), 4),
    }
    return summary, records, None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def _thresholds(rec: Dict[str, Any], arm: str) -> Dict[str, bool]:
    ev = float(rec[arm]["ev"] or 0.0)
    evb = rec[arm]["ev_blended"]
    evb = float(evb) if evb is not None else 0.0
    rr = float(rec[arm]["rr"] or 0.0)
    is_fx = bool(rec.get("is_fx"))
    eff = float(rec.get("eff_min_ev", 1.0) or 0.0)
    return {
        "t364": (not is_fx) and ev >= 1.5 and rr >= 2.0,
        "t411": rr >= 3.0 and ev > 0,
        "t413": rr >= 2.0 and ev > 0,
        "t660": ev > 0 and ev >= eff,
        "t904": rr >= 2.0 and evb > 0,
    }


def _metrics(r: np.ndarray) -> Dict[str, Any]:
    r = np.asarray(r, dtype=float)
    n = len(r)
    if n == 0:
        return {"trades": 0, "total_r": 0.0, "expectancy_r": 0.0, "t_stat": 0.0,
                "win_rate": 0.0, "max_dd_r": 0.0}
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    curve = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))
    return {
        "trades": int(n),
        "total_r": round(float(r.sum()), 3),
        "expectancy_r": round(float(r.mean()), 5),
        "win_rate": round(float((r > 0).mean()), 4),
        "t_stat": round(float(r.mean() / (sd / math.sqrt(n))), 3) if sd > 1e-12 else 0.0,
        "max_dd_r": round(float((peak[1:] - curve).max()), 3),
    }


def _sel_mask(df: pd.DataFrame, arm: str) -> pd.Series:
    return df[arm].apply(lambda d: bool(d.get("selected", False)))


def _r_col(df: pd.DataFrame, arm: str) -> pd.Series:
    return df[arm].apply(lambda d: d.get("r"))


def _paired(df: pd.DataFrame, arm_x: str, arm_y: str) -> Dict[str, Any]:
    """Paired stats on the trades BOTH arms selected (same bar)."""
    mask = _sel_mask(df, arm_x) & _sel_mask(df, arm_y)
    sel = df[mask]
    if len(sel) == 0:
        return {"n": 0}
    dx = _r_col(sel, arm_x).to_numpy(dtype=float)
    dy = _r_col(sel, arm_y).to_numpy(dtype=float)
    diff = dy - dx
    sd = float(diff.std(ddof=1)) if len(diff) > 1 else 0.0
    return {
        "n": int(len(diff)),
        "mean_diff_r": round(float(diff.mean()), 6),
        "sd_diff_r": round(sd, 5),
        "t_stat": round(float(diff.mean() / (sd / math.sqrt(len(diff)))), 3) if sd > 1e-12 else 0.0,
        "pct_improved": round(float((diff > 1e-12).mean()), 4),
        "pct_worsened": round(float((diff < -1e-12).mean()), 4),
    }


def _set_delta(df: pd.DataFrame, arm_x: str, arm_y: str) -> Dict[str, Any]:
    mx = _sel_mask(df, arm_x)
    my = _sel_mask(df, arm_y)
    sx, sy = df[mx], df[my]
    kx = set(zip(sx["symbol"].astype(str), sx["bar_idx"].astype(int)))
    ky = set(zip(sy["symbol"].astype(str), sy["bar_idx"].astype(int)))
    common, added, dropped = kx & ky, ky - kx, kx - ky

    def _sum_r(sub: pd.DataFrame, arm: str, keys: set) -> float:
        if not keys or len(sub) == 0:
            return 0.0
        mask = [k in keys for k in zip(sub["symbol"].astype(str), sub["bar_idx"].astype(int))]
        if not any(mask):
            return 0.0
        return float(_r_col(sub.loc[mask], arm).sum())

    r_added = _sum_r(sy, arm_y, added)
    r_dropped = _sum_r(sx, arm_x, dropped)
    return {
        "n_x": int(len(sx)), "n_y": int(len(sy)),
        "n_common": int(len(common)), "n_added": int(len(added)), "n_dropped": int(len(dropped)),
        "r_added": round(r_added, 3), "r_dropped": round(r_dropped, 3),
        "d_total_r_from_selection": round(r_added - r_dropped, 3),
    }


def _arm_r(df: pd.DataFrame, arm: str) -> np.ndarray:
    sub = df[_sel_mask(df, arm)]
    return _r_col(sub, arm).to_numpy(dtype=float)


def _summarise_arm(df: pd.DataFrame, arm: str) -> Dict[str, Any]:
    m = _metrics(_arm_r(df, arm))
    ev_a = df["A"].apply(lambda d: d["ev"])
    ev_x = df[arm].apply(lambda d: d["ev"])
    evb_a = df["A"].apply(lambda d: d["ev_blended"])
    evb_x = df[arm].apply(lambda d: d["ev_blended"])
    gate_a = df["A"].apply(lambda d: d["gate_passed"])
    gate_x = df[arm].apply(lambda d: d["gate_passed"])
    dec_a = df["A"].apply(lambda d: d["decision"])
    dec_x = df[arm].apply(lambda d: d["decision"])
    n = len(df)
    return {
        **m,
        "n_candidates": int(n),
        "n_final_ev_changed": int((ev_x != ev_a).sum()),
        "n_blended_ev_changed": int((evb_x != evb_a).sum()),
        "n_gate_flipped": int((gate_x != gate_a).sum()),
        "n_decision_flipped": int((dec_x != dec_a).sum()),
        "n_selected": int(_sel_mask(df, arm).sum()),
    }


def _threshold_table(df: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for t in ("t364", "t411", "t413", "t660", "t904"):
        row: Dict[str, Any] = {}
        for arm in ARMS:
            row[arm] = int(df.apply(lambda r: _thresholds(r, arm)[t], axis=1).sum())
        for other in OTHER_ARMS:
            row[f"flips_A_{other}"] = int(
                df.apply(lambda r: _thresholds(r, other)[t] != _thresholds(r, "A")[t], axis=1).sum()
            )
        out[t] = row
    return out


def _block(df: pd.DataFrame) -> Dict[str, Any]:
    return {
        "arms": {arm: _summarise_arm(df, arm) for arm in ARMS},
        "thresholds": _threshold_table(df),
        "paired": {o: _paired(df, "A", o) for o in OTHER_ARMS},
        "set_delta": {o: _set_delta(df, "A", o) for o in OTHER_ARMS},
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def _symbol_task(symbol: str, tf: str, window: int, start_bar_idx: int,
                 limit_bars: Optional[int], check_determinism: bool,
                 analyst_timeout: float):
    with offline_mode():
        summary, records, err = scan_symbol(symbol, tf, window, start_bar_idx,
                                            limit_bars, check_determinism, analyst_timeout)
    return symbol, summary, records, err


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--window", type=int, default=183)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--start-bar", type=int, default=60)
    ap.add_argument("--limit-bars", type=int, default=None)
    ap.add_argument("--check-determinism", action="store_true")
    ap.add_argument("--analyst-timeout", type=float, default=60.0,
                    help="analyst cluster timeout; 2.0 reproduces the historical "
                         "timeout-degraded scan")
    ap.add_argument("--out", default=os.path.join(REPO, "reports", "ev_spread_cost_ab.json"))
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(
            d for d in os.listdir(REAL_DIR)
            if os.path.isdir(os.path.join(REAL_DIR, d))
            and os.path.exists(os.path.join(REAL_DIR, d, f"{d}_{args.tf}_{args.window}d.parquet"))
        )

    print(f"EV SPREAD-COST A/B  tf={args.tf} window={args.window}d "
          f"symbols={len(symbols)} workers={args.workers}")
    t0 = time.time()
    summaries: List[Dict[str, Any]] = []
    all_records: List[Dict[str, Any]] = []
    skipped: List[str] = []

    def _collect(sym, summary, records, err):
        if err or summary is None:
            skipped.append(f"{sym}: {err}")
            print(f"  {sym:8s} SKIP {err}", flush=True)
            return
        summaries.append(summary)
        all_records.extend(records)
        print(f"  {sym:8s} bars={summary['bars_scanned']:5d} cand={summary['candidates']:5d} "
              f"reg={summary['reg_spread']:.2f} real_med={summary['real_spread_median']} "
              f"ratio={summary['ratio_median']} [{time.time()-t0:.0f}s]", flush=True)

    if args.workers <= 1:
        for s in symbols:
            _collect(*_symbol_task(s, args.tf, args.window, args.start_bar,
                                   args.limit_bars, args.check_determinism,
                                   args.analyst_timeout))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(_symbol_task, s, args.tf, args.window, args.start_bar,
                                args.limit_bars, args.check_determinism,
                                args.analyst_timeout): s for s in symbols}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    _collect(*fut.result())
                except Exception as exc:
                    skipped.append(f"{s}: {exc}")
                    print(f"  {s:8s} FAIL {exc}", flush=True)

    if not all_records:
        print("no candidates recorded")
        return 1

    df = pd.DataFrame(all_records)
    df["eff_min_ev"] = df["symbol"].map({s["symbol"]: s["eff_min_ev"] for s in summaries})

    pooled = {"candidates": int(len(df)), **_block(df)}
    per_symbol = {}
    for s in summaries:
        sub = df[df["symbol"] == s["symbol"]]
        if len(sub) == 0:
            continue
        per_symbol[s["symbol"]] = {**s, **_block(sub)}

    payload = {
        "generated": pd.Timestamp.now("UTC").isoformat(),
        "config": {
            "tf": args.tf, "window_days": args.window,
            "symbols": [s["symbol"] for s in summaries], "skipped": skipped,
            "slip_pips": SLIP_PIPS_DEFAULT, "account_balance": ACCOUNT_BALANCE,
            "risk_pct": RISK_PCT, "start_bar_idx": args.start_bar,
            "limit_bars": args.limit_bars,
            "arms": {
                "A": "baseline: registry typical_spread_pips everywhere (live path)",
                "B": "literal method-level swap inside _compute_blended_probability "
                     "(line 261 + the ML spread-friction feature)",
                "Bpure": "pure: ONLY line 261 spread_cost sees the real spread "
                         "(ML feature pinned to the registry)",
                "D": "pure: BOTH cost copies (line 261 + line 954) see the real spread; "
                     "gates, geometry and ML feature stay on the registry",
                "C": "real per-bar spread everywhere (gates + geometry + EV + ML feature)",
                "R": "registry corrected to a CONSTANT = observed series median, fed "
                     "everywhere the registry constant is fed (reproduces AUDIT §J2)",
            },
        },
        "pooled": pooled,
        "per_symbol": per_symbol,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    scratch = os.path.join(REPO, ".scratch")
    os.makedirs(scratch, exist_ok=True)
    df.to_parquet(os.path.join(scratch, "ev_spread_cost_ab_candidates.parquet"), index=False)

    print("\nPOOLED:")
    for arm in ARMS:
        m = pooled["arms"][arm]
        print(f"  {arm:6s} cand={m['n_candidates']} dEV={m['n_final_ev_changed']} "
              f"dEVb={m['n_blended_ev_changed']} gateFlip={m['n_gate_flipped']} "
              f"decFlip={m['n_decision_flipped']} sel={m['n_selected']} "
              f"totR={m['total_r']:+.1f} E[R]={m['expectancy_r']:+.5f}")
    for o in OTHER_ARMS:
        p = pooled["paired"][o]
        d = pooled["set_delta"][o]
        print(f"  A vs {o:6s}: paired n={p.get('n',0)} dE={p.get('mean_diff_r',0):+.5f} "
              f"t={p.get('t_stat',0):+.2f} | set +{d['n_added']}/-{d['n_dropped']} "
              f"common={d['n_common']} dR_sel={d['d_total_r_from_selection']:+.1f}")
    print(f"\nwrote {args.out}")
    print(f"wrote {os.path.join(scratch, 'ev_spread_cost_ab_candidates.parquet')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
