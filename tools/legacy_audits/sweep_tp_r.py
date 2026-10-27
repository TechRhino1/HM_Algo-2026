"""
Per-symbol tp_r frontier sweep (ROOT-CAUSE ANALYSIS).

For every symbol, replay the cached candidates under a range of tp_r values and
report, at each point:

  * win rate, expectancy (R), profit factor
  * REALISED payoff (avg_win_r / |avg_loss_r|)
  * BREAKEVEN win rate  = 1 / (1 + payoff)
  * MARGIN              = win_rate - breakeven_win_rate

The margin is the whole story. A configuration whose margin is <= 0 is
structurally unprofitable; one whose margin is thin (1-3 points) flips negative
under any out-of-sample -> engine slippage. The old calibrator ranked by WIN
RATE whenever the 75% target was missed, which systematically selected the
tightest tp_r and therefore the thinnest margin. This sweep shows, per symbol,
what that cost.

Usage:
    python tools/sweep_tp_r.py
    python tools/sweep_tp_r.py NAS100 USDJPY
"""
from __future__ import annotations

import sys
import types
from collections import Counter
from pathlib import Path

import pandas as pd

try:
    import MetaTrader5  # noqa: F401
except Exception:  # pragma: no cover
    sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.config.paths import DATA_DIR  # noqa: E402
from jarvis.data.symbol_registry import (  # noqa: E402
    resolve as resolve_symbol,
    get_dollar_risk_per_price_unit,
)
from jarvis.backtesting.trade_simulator import (  # noqa: E402
    simulate_all_candidates,
    select_sequential,
    summarise,
)
from jarvis.intelligence.winrate_targeting import (  # noqa: E402
    WRTargetCalibrator,
    WRProfileStore,
    Geometry,
)

REAL_DIR = Path(DATA_DIR) / "market" / "real"
SIGNAL_DIR = Path(DATA_DIR) / "signals"
PROFILE_PATH = REPO_ROOT / "config" / "winrate_profiles.json"

TP_GRID = [0.25, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.5, 2.0]


def main() -> int:
    syms = [s.strip().upper() for s in sys.argv[1:] if not s.startswith("--")]
    store = WRProfileStore(PROFILE_PATH)
    profiles = store.load()
    if not profiles:
        print("No profiles.")
        return 2
    syms = syms or sorted(profiles.keys())
    cal = WRTargetCalibrator(target_wr=0.75, slippage_pips=0.5)

    for sym in syms:
        dpath = REAL_DIR / sym / f"{sym}_H1_95d.parquet"
        cpath = SIGNAL_DIR / f"{sym}_candidates.parquet"
        if not dpath.exists() or not cpath.exists():
            print(f"\n### {sym}: SKIP (no data)")
            continue
        profile = profiles.get(sym)
        if profile is None:
            continue
        df = pd.read_parquet(dpath).reset_index(drop=True)
        cands = pd.read_parquet(cpath)
        spec = resolve_symbol(sym)
        mpu = get_dollar_risk_per_price_unit(sym)
        cost = cal._cost_price_equiv(sym, mpu)
        slip = 0.5 * float(getattr(spec, "pip_size", 0.0001) or 0.0001)
        base = profile.geometry
        thr = float(base.min_score)
        enabled = None
        if getattr(profile, "regime_edge", None):
            enabled = {r for r, e in profile.regime_edge.items()
                       if getattr(e, "enabled", True)}

        print("\n" + "=" * 118)
        print(f"### {sym}   base tp_r={base.tp_r}  min_score={thr:.4f}  "
              f"enabled_regimes={sorted(enabled) if enabled else 'ALL'}")
        print("=" * 118)
        print(f"{'tp_r':>5} {'trd':>4} {'WR%':>6} {'awR':>6} {'alR':>7} "
              f"{'payoff':>6} {'BE_WR%':>7} {'MARGIN':>7} {'exp_R':>8} {'PF':>5}  exits")

        best_wr = {"score": -9e9}
        best_exp = {"score": -9e9}
        best_robust = {"score": (-1, -9e9)}
        MIN_MARGIN = 0.05

        for tp in TP_GRID:
            geom = Geometry(
                tp_r=float(tp),
                be_trigger_r=base.be_trigger_r,
                fast_cash_r=base.fast_cash_r,
                fast_cash_pct=base.fast_cash_pct,
                trail_atr=base.trail_atr,
                trail_activation_r=base.trail_activation_r,
                max_bars=base.max_bars,
                min_score=thr,
            )
            outs = simulate_all_candidates(
                df=df, candidates=cands, geom=geom, money_per_unit=mpu,
                cost_price_equiv=cost, slippage_price_equiv=slip,
                spec=spec, symbol=sym,
            )
            chosen = select_sequential(outs, min_score=thr, regimes=enabled)
            if not chosen:
                print(f"{tp:5.2f} {'-- no trades --'}")
                continue
            s = summarise(chosen)
            wr = float(s["win_rate"])
            aw = abs(float(s["avg_win_r"]))
            al = abs(float(s["avg_loss_r"]))
            exp = float(s["expectancy_r"])
            pf = float(s["profit_factor"])
            payoff = (aw / al) if al > 0 else 0.0
            be_wr = (1.0 / (1.0 + payoff)) if payoff > 0 else 1.0
            margin = wr - be_wr
            reasons = dict(Counter(o.result for o in chosen))

            print(f"{tp:5.2f} {s['trades']:4d} {wr*100:6.2f} {aw:6.3f} {al:7.3f} "
                  f"{payoff:6.3f} {be_wr*100:7.2f} {margin*100:+7.2f} {exp:+8.4f} {pf:5.2f}  {reasons}")

            # What each objective would pick:
            if wr > best_wr["score"]:
                best_wr = {"score": wr, "tp": tp, "exp": exp, "margin": margin, "wr": wr}
            if exp > best_exp["score"]:
                best_exp = {"score": exp, "tp": tp, "exp": exp, "margin": margin, "wr": wr}
            robust_key = (1 if margin >= MIN_MARGIN else 0, exp)
            if robust_key > best_robust["score"]:
                best_robust = {"score": robust_key, "tp": tp, "exp": exp,
                               "margin": margin, "wr": wr}

        f = lambda d: (f"tp={d.get('tp')} WR={d.get('wr',0)*100:.2f}% "
                       f"margin={d.get('margin',0)*100:+.2f} exp={d.get('exp',0):+.4f}R")
        print(f"\n  pick-by-WIN-RATE  (old Tier1/0 behaviour): {f(best_wr)}")
        print(f"  pick-by-EXPECTANCY                        : {f(best_exp)}")
        print(f"  pick-by-EXPECTANCY+margin>={MIN_MARGIN:.0%} (proposed): {f(best_robust)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
