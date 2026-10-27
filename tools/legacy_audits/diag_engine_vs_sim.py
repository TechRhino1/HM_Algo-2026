"""
Diagnostic: head-to-head of the PRODUCTION BacktestEngine (what live will do)
vs the CALIBRATOR's simulator walk (trade_simulator), on the SAME
H1_95d data and the SAME calibrated profile.

Goal: find WHY calibration OOS expectancy is systematically more optimistic
than the engine backtest (the negative-portfolio root cause).

For one symbol we print:
  * engine  : full-sample WR / expectancy(R) / trade count / exit-reason mix
  * sim     : full-sample walk WR / expectancy(R) / trade count / exit-reason mix
  * overlap : how many traded bars the two agree on (divergence in the WALK)
  * per-regime geometry difference between profile.geometry and geometry_for()

Usage:
    python tools/diag_engine_vs_sim.py NAS100
    python tools/diag_engine_vs_sim.py XAUUSD USDCAD
"""
from __future__ import annotations

import sys
import types
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

# Headless: stub MetaTrader5 if it is not installed (never connects in backtest).
try:
    import MetaTrader5  # noqa: F401
except Exception:  # pragma: no cover
    sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.config.runtime import offline_mode  # noqa: E402
from jarvis.config.paths import DATA_DIR  # noqa: E402
from jarvis.data.symbol_registry import (  # noqa: E402
    resolve as resolve_symbol,
    get_dollar_risk_per_price_unit,
)
from jarvis.backtesting.engine import BacktestEngine  # noqa: E402
from jarvis.backtesting.trade_simulator import (  # noqa: E402
    simulate_all_candidates,
    select_sequential,
    summarise,
)
from jarvis.intelligence.winrate_targeting import (  # noqa: E402
    WRTargetCalibrator,
    WRProfileStore,
)

REAL_DIR = Path(DATA_DIR) / "market" / "real"
SIGNAL_DIR = Path(DATA_DIR) / "signals"
PROFILE_PATH = REPO_ROOT / "config" / "winrate_profiles.json"


def median_spread_pips(df: pd.DataFrame, symbol: str) -> float:
    spec = resolve_symbol(symbol)
    pip = float(spec.pip_size or 0.0001)
    pt = 10.0 ** (-int(spec.digits or 5))
    if "spread" in df.columns and pip > 0 and pt > 0:
        vals = pd.to_numeric(df["spread"], errors="coerce").dropna()
        if len(vals):
            return float(vals.median() * pt / pip)
    return float(getattr(spec, "typical_spread_pips", 2.0) or 2.0)


def r_multiple(t: dict) -> float:
    entry = float(t.get("entry", 0.0))
    initial_sl = t.get("initial_sl", t.get("sl", 0.0))
    risk = abs(entry - float(initial_sl))
    if risk <= 0:
        risk = abs(float(t.get("risk_dist", 0.0) or 0.0))
    if risk <= 0:
        return 0.0
    move = float(t.get("exit", 0.0)) - entry
    if str(t.get("type", "")).upper() == "SELL":
        move = -move
    return move / risk


def _norm_ts(v):
    try:
        ts = pd.Timestamp(v)
        return ts.tz_localize(None) if ts.tzinfo else ts
    except Exception:
        return None


def main() -> int:
    raw_argv = [a for a in sys.argv[1:]]
    probe = "--probe" in raw_argv
    symbols = [s.strip().upper() for s in raw_argv if not s.startswith("--")] or ["NAS100"]
    store = WRProfileStore(PROFILE_PATH)
    profiles = store.load()
    if not profiles:
        print("No calibrated profiles found.")
        return 2

    cal = WRTargetCalibrator(target_wr=0.75, slippage_pips=0.5)

    for sym in symbols:
        dpath = REAL_DIR / sym / f"{sym}_H1_95d.parquet"
        cpath = SIGNAL_DIR / f"{sym}_candidates.parquet"
        if not dpath.exists() or not cpath.exists():
            print(f"\n### {sym}: SKIP (missing data/candidates)")
            continue
        df = pd.read_parquet(dpath).reset_index(drop=True)
        cands = pd.read_parquet(cpath)
        spread = median_spread_pips(df, sym)
        profile = profiles.get(sym)
        if profile is None:
            print(f"\n### {sym}: SKIP (no profile)")
            continue

        spec = resolve_symbol(sym)
        money_per_unit = get_dollar_risk_per_price_unit(sym)
        cost = cal._cost_price_equiv(sym, money_per_unit)
        slip = 0.5 * float(getattr(spec, "pip_size", 0.0001) or 0.0001)

        print("\n" + "=" * 92)
        print(f"### {sym}  | bars={len(df)} spread_median={spread:.3f} "
              f"candidates={len(cands)} profile_geom={profile.geometry.key()}")
        print("=" * 92)

        # ---- ENGINE (ground truth for live) ----
        if probe:
            profile.regime_geometry = {}
            print("  [PROBE] engine forced to validated BASE geometry "
                  "(per-regime override cleared)")
        with offline_mode():
            eng = BacktestEngine(initial_balance=10_000.0, risk_per_trade_pct=0.5)
            res = eng.run_backtest(df_h1=df, symbol=sym, spread_pips=spread, wr_profile=profile)
        et = res.get("trades", [])
        er = [r_multiple(t) for t in et]
        eng_wr = 100.0 * sum(1 for r in er if r > 0) / len(er) if er else 0.0
        eng_exp = float(np.mean(er)) if er else 0.0
        eng_reasons = Counter(t.get("result", "?") for t in et)
        eng_open_times = {_norm_ts(t.get("open_time")) for t in et}

        # ---- SIMULATOR full-sample walk (same geometry / threshold / regimes) ----
        geom = profile.geometry
        outcomes = simulate_all_candidates(
            df=df, candidates=cands, geom=geom, money_per_unit=money_per_unit,
            cost_price_equiv=cost, slippage_price_equiv=slip, spec=spec, symbol=sym,
        )
        enabled = None
        if getattr(profile, "regime_edge", None):
            enabled = {r for r, e in profile.regime_edge.items()
                       if getattr(e, "enabled", True)}
        chosen = select_sequential(outcomes, min_score=geom.min_score, regimes=enabled)
        sim_sum = summarise(chosen)
        sim_reasons = Counter(o.result for o in chosen)
        sim_open_times = {_norm_ts(o.entry_time) for o in chosen}

        # ---- OVERLAP of traded bars ----
        if eng_open_times and sim_open_times:
            inter = eng_open_times & sim_open_times
            union = eng_open_times | sim_open_times
            jacc = len(inter) / len(union) if union else 0.0
            only_eng = len(eng_open_times - sim_open_times)
            only_sim = len(sim_open_times - eng_open_times)
        else:
            jacc = only_eng = only_sim = 0

        # ---- per-regime geometry diff ----
        rg = getattr(profile, "regime_geometry", None) or {}
        geom_diffs = {}
        for rname, rg_geom in rg.items():
            if (rg_geom.tp_r != geom.tp_r or rg_geom.be_trigger_r != geom.be_trigger_r
                    or rg_geom.max_bars != geom.max_bars or rg_geom.fast_cash_r != geom.fast_cash_r):
                geom_diffs[rname] = (rg_geom.tp_r, rg_geom.be_trigger_r, rg_geom.max_bars)

        # ---- report ----
        print(f"\n  TRADE COUNT      engine={len(et):4d}   simulator={len(chosen):4d}")
        print(f"  WIN RATE %        engine={eng_wr:6.2f}   simulator={sim_sum['win_rate']*100:6.2f}")
        print(f"  EXPECTANCY (R)    engine={eng_exp:+.4f}   simulator={sim_sum['expectancy_r']:+.4f}")
        aw = np.mean([r for r in er if r > 0]) if er else 0.0
        al = np.mean([r for r in er if r < 0]) if er else 0.0
        print(f"  AVG WIN / LOSS R  engine={aw:.3f} / {al:.3f}"
              f"   sim={sim_sum['avg_win_r']:.3f} / {sim_sum['avg_loss_r']:.3f}")
        print(f"  WALK OVERLAP      jaccard={jacc:.3f}  only_engine={only_eng}  only_simulator={only_sim}")
        print(f"  PROFILE OOS (stored)  exp={profile.oos_expectancy_r:+.4f}R  "
              f"WR={profile.oos_win_rate*100:.2f}%  n={profile.oos_trades}")
        print(f"\n  ENGINE exit reasons:   {dict(eng_reasons)}")
        print(f"  SIM    exit reasons:   {dict(sim_reasons)}")
        if geom_diffs:
            print(f"  PER-REGIME GEOMETRY DIFFERS from base (tp_r, be, max_bars): {geom_diffs}")
        else:
            print("  PER-REGIME GEOMETRY: identical to base")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
