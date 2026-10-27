"""
Run the complete 3-month backtest on real MT5 data with calibrated profiles.

Runs the PRODUCTION ``BacktestEngine`` per symbol, wired to that symbol's
calibrated win-rate profile, and reports:

  * per-symbol: trades, win rate, expectancy (R), profit factor, net profit,
    max drawdown, target met / not met, the binding constraint, the calibrated
    geometry and the enabled regimes;
  * overall: aggregate trades, win rate, expectancy, profit factor, portfolio
    equity curve and drawdown, plus HRP risk weights and a
    sample-uniqueness-weighted expectancy across symbols.

Outputs ``reports/backtest_3month_report.json`` and
``reports/backtest_3month_report.md``.

Usage::

    python tools/run_3month_backtest.py
    python tools/run_3month_backtest.py --symbols EURUSD,XAUUSD
    python tools/run_3month_backtest.py --legacy   # uncalibrated baseline
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine  # noqa: E402
from jarvis.backtesting.metrics import PerformanceMetricsCalculator  # noqa: E402
from jarvis.config.paths import DATA_DIR  # noqa: E402
from jarvis.config.runtime import offline_mode  # noqa: E402
from jarvis.data.symbol_registry import resolve as resolve_symbol  # noqa: E402
from jarvis.intelligence.winrate_targeting import WRProfileStore  # noqa: E402
from jarvis.learning.sample_weights import SampleUniquenessWeightEngine  # noqa: E402
from jarvis.risk.hrp_allocator import HierarchicalRiskParityAllocator  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backtest_3month")

REAL_DIR = Path(DATA_DIR) / "market" / "real"
SIGNAL_DIR = Path(DATA_DIR) / "signals"
PROFILE_PATH = REPO_ROOT / "config" / "winrate_profiles.json"
REPORT_DIR = REPO_ROOT / "reports"

INITIAL_BALANCE = 10_000.0
RISK_PER_TRADE_PCT = 0.5


def median_spread_pips(df: pd.DataFrame, symbol: str) -> float:
    """Median spread in pips from the real data (falls back to the registry)."""
    spec = resolve_symbol(symbol)
    pip = float(spec.pip_size or 0.0001)
    pt = 10.0 ** (-int(spec.digits or 5))
    if "spread" in df.columns and pip > 0 and pt > 0:
        vals = pd.to_numeric(df["spread"], errors="coerce").dropna()
        if len(vals):
            return float(vals.median() * pt / pip)
    return float(getattr(spec, "typical_spread_pips", 2.0) or 2.0)


def discover() -> list[str]:
    if not REAL_DIR.exists():
        return []
    return sorted(
        d.name for d in REAL_DIR.iterdir()
        if d.is_dir() and any(d.glob("*_H1_95d.parquet"))
    )


def r_multiple(trade: dict) -> float:
    """Realised R for a closed trade.

    MUST use the immutable ``initial_sl``, not ``sl``. ``sl`` is the stop as it
    stands at close — i.e. after any trailing/breakeven ratchet — so using it as
    the risk denominator shrinks 1R towards zero and reports absurd R values
    (a trailed stop sitting just behind entry turns a small win into "+12R").
    """
    entry = float(trade.get("entry", 0.0))
    initial_sl = trade.get("initial_sl", trade.get("sl", 0.0))
    risk = abs(entry - float(initial_sl))
    if risk <= 0:
        risk = abs(float(trade.get("risk_dist", 0.0) or 0.0))
    if risk <= 0:
        return 0.0
    move = float(trade.get("exit", 0.0)) - entry
    if str(trade.get("type", "")).upper() == "SELL":
        move = -move
    return move / risk


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=None, help="comma-separated subset")
    ap.add_argument("--legacy", action="store_true",
                    help="run WITHOUT calibrated profiles (uncalibrated baseline)")
    args = ap.parse_args()

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else discover()
    )
    if not symbols:
        logger.error(f"No real data found under {REAL_DIR}")
        return 2

    store = WRProfileStore(PROFILE_PATH)
    profiles = {} if args.legacy else store.load()
    if not args.legacy and not profiles:
        logger.error(f"No calibrated profiles at {PROFILE_PATH}. Run tools/calibrate_winrate.py first.")
        return 2

    print("=" * 108)
    print(f"HM Algo 2.0 — 3-MONTH BACKTEST ON REAL MT5 DATA   "
          f"({'UNCALIBRATED BASELINE' if args.legacy else 'CALIBRATED PROFILES'})")
    print("=" * 108)

    all_trades: list[dict] = []
    per_symbol: dict[str, dict] = {}
    daily_r: dict[str, pd.Series] = {}
    t_start = time.time()

    with offline_mode():
        for sym in symbols:
            dpath = REAL_DIR / sym / f"{sym}_H1_95d.parquet"
            if not dpath.exists():
                print(f"  {sym:8s} SKIP (no data)")
                continue
            df = pd.read_parquet(dpath).reset_index(drop=True)
            spread = median_spread_pips(df, sym)
            profile = None if args.legacy else profiles.get(sym)

            t0 = time.time()
            engine = BacktestEngine(
                initial_balance=INITIAL_BALANCE,
                risk_per_trade_pct=RISK_PER_TRADE_PCT,
            )
            res = engine.run_backtest(
                df_h1=df, symbol=sym, spread_pips=spread, wr_profile=profile
            )
            trades = res.get("trades", [])
            m = res.get("metrics", {})

            rs = [r_multiple(t) for t in trades]
            entry = {
                "symbol": sym,
                "bars": len(df),
                "spread_pips_median": round(spread, 3),
                "trades": len(trades),
                "wins": m.get("wins", 0),
                "losses": m.get("losses", 0),
                "win_rate": m.get("win_rate_pct", 0.0),
                "expectancy_r": round(float(np.mean(rs)), 4) if rs else 0.0,
                "total_r": round(float(np.sum(rs)), 3) if rs else 0.0,
                "profit_factor": m.get("profit_factor", 0.0),
                "net_profit": m.get("net_profit", 0.0),
                "max_drawdown_pct": m.get("max_drawdown_pct", 0.0),
                "sharpe": m.get("sharpe_ratio", 0.0),
                "seconds": round(time.time() - t0, 1),
                "rejection_stats": res.get("rejection_stats", {}),
            }
            if profile is not None:
                entry.update({
                    "calibrated_geometry": profile.geometry.to_dict(),
                    "calibrated_min_score": profile.geometry.min_score,
                    "calibration_oos_trades": profile.oos_trades,
                    "calibration_oos_win_rate": profile.oos_win_rate,
                    "calibration_oos_expectancy_r": profile.oos_expectancy_r,
                    "calibration_oos_profit_factor": profile.oos_profit_factor,
                    "calibration_oos_pre_policy_trades": profile.oos_pre_policy_trades,
                    "calibration_oos_pre_policy_win_rate": profile.oos_pre_policy_win_rate,
                    "calibration_oos_pre_policy_expectancy_r": profile.oos_pre_policy_expectancy_r,
                    "policy_fitted_on": profile.policy_fitted_on,
                    "target_wr": profile.target_wr,
                    "target_met_in_sample": profile.target_met,
                    "target_met_oos": profile.oos_target_met,
                    "binding_constraint": profile.binding_constraint,
                    "enabled_regimes": profile.enabled_regimes,
                    "regime_geometry": {k: v.tp_r for k, v in profile.regime_geometry.items()},
                })
            per_symbol[sym] = entry
            all_trades.extend(trades)

            if trades and "exit_time" in trades[0]:
                s = pd.Series(
                    rs,
                    index=pd.to_datetime([t.get("exit_time") for t in trades], errors="coerce", utc=True),
                ).dropna()
                if len(s):
                    daily_r[sym] = s.resample("1D").sum()

            print(f"  {sym:8s} trades={len(trades):4d} WR={entry['win_rate']:5.1f}% "
                  f"exp={entry['expectancy_r']:+.3f}R PF={entry['profit_factor']:5.2f} "
                  f"net=${entry['net_profit']:+9.2f} DD={entry['max_drawdown_pct']:5.2f}% "
                  f"[{entry['seconds']}s]")

    # ── Portfolio aggregation ───────────────────────────────────────────────
    total_trades = len(all_trades)
    all_r = [r_multiple(t) for t in all_trades]
    agg_metrics = PerformanceMetricsCalculator.calculate_metrics(all_trades, INITIAL_BALANCE)
    wins = [r for r in all_r if r > 0]
    losses = [r for r in all_r if r < 0]

    portfolio = {
        "symbols_traded": len(per_symbol),
        "total_trades": total_trades,
        "win_rate": round(len(wins) / total_trades * 100, 2) if total_trades else 0.0,
        "expectancy_r": round(float(np.mean(all_r)), 4) if all_r else 0.0,
        "avg_win_r": round(float(np.mean(wins)), 4) if wins else 0.0,
        "avg_loss_r": round(float(np.mean(losses)), 4) if losses else 0.0,
        "payoff_ratio": round(abs(np.mean(wins) / np.mean(losses)), 3) if wins and losses else 0.0,
        "total_r": round(float(np.sum(all_r)), 3) if all_r else 0.0,
        "net_profit": agg_metrics.get("net_profit", 0.0),
        "profit_factor": agg_metrics.get("profit_factor", 0.0),
        "max_drawdown_pct": agg_metrics.get("max_drawdown_pct", 0.0),
        "sharpe": agg_metrics.get("sharpe_ratio", 0.0),
        "sortino": agg_metrics.get("sortino_ratio", 0.0),
        "calmar": agg_metrics.get("calmar_ratio", 0.0),
    }

    # Sample-uniqueness weighted expectancy: overlapping positions across
    # symbols carry duplicated information, so an unweighted mean overstates
    # the evidence.
    if all_trades:
        enriched = []
        for t in all_trades:
            ot, xt = t.get("open_time"), t.get("exit_time")
            dur = 1
            try:
                dur = max(1, int((pd.Timestamp(xt) - pd.Timestamp(ot)).total_seconds() // 3600))
            except Exception:
                pass
            enriched.append({"pnl": t.get("pnl", 0.0), "duration_bars": dur})
        w = SampleUniquenessWeightEngine.get_sample_weights(enriched)
        r_arr = np.array(all_r, dtype=float)
        if len(w) == len(r_arr) and w.sum() > 0:
            portfolio["expectancy_r_uniqueness_weighted"] = round(float(np.sum(w * r_arr)), 4)

    # HRP risk allocation across symbols (inverse-variance clustering input).
    hrp_weights: dict[str, float] = {}
    if len(daily_r) >= 2:
        matrix = pd.DataFrame(daily_r).fillna(0.0)
        if matrix.shape[1] >= 2 and len(matrix) >= 5:
            hrp_weights = HierarchicalRiskParityAllocator.allocate_weights(matrix)
    portfolio["hrp_risk_weights"] = hrp_weights

    # ── Report assembly ─────────────────────────────────────────────────────
    meta = {
        "mode": "uncalibrated_baseline" if args.legacy else "calibrated",
        "data_source": "MT5 terminal, real H1 bars (validated non-synthetic)",
        "initial_balance": INITIAL_BALANCE,
        "risk_per_trade_pct": RISK_PER_TRADE_PCT,
        "elapsed_seconds": round(time.time() - t_start, 1),
        "generated_utc": pd.Timestamp.now("UTC").isoformat(),
    }
    if not args.legacy and profiles:
        first = next(iter(profiles.values()))
        meta.update({
            "target_wr": first.target_wr,
            "profiles_loaded": len(profiles),
            "symbols_target_met_oos": sum(1 for p in profiles.values() if p.oos_target_met),
        })

    report = {"meta": meta, "portfolio": portfolio, "per_symbol": per_symbol}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "backtest_3month_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )

    md = render_markdown(report, profiles, args.legacy)
    (REPORT_DIR / "backtest_3month_report.md").write_text(md, encoding="utf-8")

    print()
    print("-" * 108)
    print(f"PORTFOLIO: {portfolio['total_trades']} trades over {portfolio['symbols_traded']} symbols | "
          f"WR {portfolio['win_rate']:.1f}% | expectancy {portfolio['expectancy_r']:+.3f}R | "
          f"PF {portfolio['profit_factor']:.2f} | net ${portfolio['net_profit']:+.2f} | "
          f"maxDD {portfolio['max_drawdown_pct']:.2f}%")
    if "expectancy_r_uniqueness_weighted" in portfolio:
        print(f"           uniqueness-weighted expectancy: "
              f"{portfolio['expectancy_r_uniqueness_weighted']:+.4f}R")
    print(f"Report: {(REPORT_DIR / 'backtest_3month_report.md').relative_to(REPO_ROOT)}")
    print(f"Elapsed {meta['elapsed_seconds']}s")
    return 0


def render_markdown(report: dict, profiles: dict, legacy: bool) -> str:
    meta, port, per = report["meta"], report["portfolio"], report["per_symbol"]
    L: list[str] = []
    A = L.append

    A("# HM Algo 2.0 — 3-Month Backtest on Real MT5 Data")
    A("")
    A(f"**Mode:** {'uncalibrated baseline (legacy 29-gate stack)' if legacy else 'calibrated per-symbol win-rate profiles'}  ")
    A(f"**Data:** {meta['data_source']}  ")
    A(f"**Initial balance:** ${meta['initial_balance']:,.2f} at {meta['risk_per_trade_pct']}% risk per trade  ")
    A(f"**Generated:** {meta['generated_utc']}")
    A("")

    A("## 1. Headline")
    A("")
    A("| Metric | Value |")
    A("|---|---|")
    A(f"| Symbols traded | {port['symbols_traded']} |")
    A(f"| Total trades | {port['total_trades']} |")
    A(f"| Win rate | {port['win_rate']:.2f}% |")
    A(f"| Expectancy | {port['expectancy_r']:+.4f} R per trade |")
    A(f"| Average win / loss | {port['avg_win_r']:+.3f} R / {port['avg_loss_r']:+.3f} R |")
    A(f"| Payoff ratio | {port['payoff_ratio']:.3f} |")
    A(f"| Total R | {port['total_r']:+.2f} R |")
    A(f"| Profit factor | {port['profit_factor']:.2f} |")
    A(f"| Net profit | ${port['net_profit']:+,.2f} |")
    A(f"| Max drawdown | {port['max_drawdown_pct']:.2f}% |")
    A(f"| Sharpe / Sortino / Calmar | {port['sharpe']} / {port['sortino']} / {port['calmar']} |")
    if "expectancy_r_uniqueness_weighted" in port:
        A(f"| Expectancy (sample-uniqueness weighted) | {port['expectancy_r_uniqueness_weighted']:+.4f} R |")
    A("")

    if not legacy and profiles:
        met = sum(1 for p in profiles.values() if p.oos_target_met)
        ins = sum(1 for p in profiles.values() if p.target_met and not p.oos_target_met)
        A(f"**Win-rate target outcome:** {met}/{len(profiles)} symbols met the target "
          f"out-of-sample. {ins} met it in-sample but failed out-of-sample "
          f"(i.e. the target was reachable only by overfitting).")
        A("")

    A("## 2. Per-symbol results")
    A("")
    A("| Symbol | Trades | WR % | Exp (R) | PF | Net $ | MaxDD % | OOS WR % | OOS Exp (R) | Target | Binding constraint |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|---|")
    for sym, e in sorted(per.items(), key=lambda kv: -kv[1]["trades"]):
        tgt = "—"
        if "target_met_oos" in e:
            tgt = "OOS ✓" if e["target_met_oos"] else ("in-sample" if e["target_met_in_sample"] else "✗")
        A(f"| {sym} | {e['trades']} | {e['win_rate']:.1f} | {e['expectancy_r']:+.3f} | "
          f"{e['profit_factor']:.2f} | {e['net_profit']:+,.2f} | {e['max_drawdown_pct']:.2f} | "
          f"{(e.get('calibration_oos_win_rate') or 0)*100:.1f} | "
          f"{e.get('calibration_oos_expectancy_r') or 0:+.3f} | {tgt} | "
          f"{(e.get('binding_constraint') or '—')[:70]} |")
    A("")

    if not legacy and profiles:
        A("### 2a. What the learned regime policy contributes")
        A("")
        A("Calibration reports the same out-of-sample sample twice: once with every "
          "regime enabled, and once with the regimes the learned policy has "
          "switched off removed. Both are purged out-of-sample w.r.t. the geometry "
          "and threshold; the policy itself is fitted on **training folds only**, "
          "so it has never seen the out-of-sample bars it filters. The gap between "
          "the two columns is the policy's entire contribution — read it as the "
          "honest size of the effect, not as free money.")
        A("")
        A("| Symbol | OOS n (all regimes) | OOS WR | OOS Exp (R) | OOS n (policy applied) | OOS WR | OOS Exp (R) | Policy fitted on |")
        A("|---|---:|---:|---:|---:|---:|---:|---|")
        for sym, e in sorted(per.items(), key=lambda kv: -kv[1]["trades"]):
            if "calibration_oos_pre_policy_trades" not in e:
                continue
            A(f"| {sym} | {e['calibration_oos_pre_policy_trades']} | "
              f"{e['calibration_oos_pre_policy_win_rate']*100:.1f} | "
              f"{e['calibration_oos_pre_policy_expectancy_r']:+.3f} | "
              f"{e['calibration_oos_trades']} | {e['calibration_oos_win_rate']*100:.1f} | "
              f"{e['calibration_oos_expectancy_r']:+.3f} | {e.get('policy_fitted_on') or '—'} |")
        A("")

    if not legacy and profiles:
        A("## 3. Win-rate / expectancy frontier")
        A("")
        A("For each target size (`tp_r`, as a multiple of initial risk) the table "
          "gives the highest win rate found at all, and the highest win rate that "
          "still carries positive expectancy. Where those two diverge, the gap is "
          "win rate that can only be bought by running a losing system. This is "
          "the direct evidence for whether the 75% target is reachable.")
        A("")
        for sym, p in sorted(profiles.items()):
            if not p.frontier:
                continue
            A(f"**{sym}**")
            A("")
            A("| tp_r | best WR | exp at best WR | trades | best WR with exp > 0 | exp | trades |")
            A("|---:|---:|---:|---:|---:|---:|---:|")
            for f in p.frontier:
                pos = f"{f.wr_at_positive_expectancy*100:.1f}%" if f.trades_at_positive_wr else "—"
                pexp = f"{f.expectancy_at_positive_wr:+.3f}" if f.trades_at_positive_wr else "—"
                pn = f.trades_at_positive_wr if f.trades_at_positive_wr else "—"
                A(f"| {f.tp_r:g} | {f.best_wr*100:.1f}% | {f.best_wr_expectancy_r:+.3f} | "
                  f"{f.best_wr_trades} | {pos} | {pexp} | {pn} |")
            A("")

        A("## 4. Calibrated geometry per symbol")
        A("")
        A("`tp_r` is the target distance in multiples of initial risk; `be` is the "
          "breakeven trigger (off = never lock breakeven); `pc` the partial bank; "
          "`mb` the time stop in bars; `thr` the entry score threshold.")
        A("")
        A("The last two columns come from the isotonic score calibration: the "
          "realised win probability the fitted map assigns to a score sitting "
          "exactly at the deployed threshold, and the Brier score of that map "
          "(lower is better; 0.25 is the skill-free baseline for a balanced "
          "sample). The calibration is monotone, so it cannot change which "
          "candidates pass — it exists to make the score interpretable and to "
          "give the AI layer a probability rather than an arbitrary index.")
        A("")
        A("| Symbol | tp_r | be | pc | mb | threshold | P(win) @ thr | Brier | Enabled regimes |")
        A("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for sym, p in sorted(profiles.items()):
            g = p.geometry
            be = "off" if g.be_trigger_r is None else f"{g.be_trigger_r:g}"
            pc = "off" if g.fast_cash_r is None else f"{g.fast_cash_r:g}"
            cal = p.score_calibration
            if cal is not None:
                try:
                    p_win = f"{cal.predict(g.min_score)*100:.1f}%"
                except Exception:
                    p_win = "—"
                brier = f"{cal.brier:.3f}"
            else:
                p_win, brier = "—", "—"
            A(f"| {sym} | {g.tp_r:g} | {be} | {pc} | {g.max_bars} | {g.min_score:.3f} | "
              f"{p_win} | {brier} | {', '.join(p.enabled_regimes) or '—'} |")
        A("")

    A("## 5. Portfolio risk allocation (HRP)")
    A("")
    if port.get("hrp_risk_weights"):
        A("Inverse-variance weights from the HRP allocator over per-symbol daily R:")
        A("")
        A("| Symbol | Risk weight |")
        A("|---|---:|")
        for k, v in sorted(port["hrp_risk_weights"].items(), key=lambda kv: -kv[1]):
            A(f"| {k} | {v:.2%} |")
    else:
        A("Not enough aligned per-symbol return history to compute HRP weights.")
    A("")

    A("## 6. Entry rejection analysis")
    A("")
    A("Why candidates were not traded under the calibrated profile. In calibrated "
      "mode these are the capital-protection gates plus the calibrated edge "
      "filter; the legacy 29-check stack no longer decides.")
    A("")
    A("Counts are per **bar evaluated**, not per candidate: a bar on which the "
      "pipeline found no setup at all (`no directional bias`) is counted here "
      "too, because the engine has to consider and decline it. A large "
      "`no directional bias` count therefore means the pipeline rarely formed a "
      "view, not that a good trade was vetoed.")
    A("")
    A("| Symbol | Top rejection reasons |")
    A("|---|---|")
    for sym, e in sorted(per.items(), key=lambda kv: -kv[1]["trades"]):
        stats = e.get("rejection_stats") or {}
        top = sorted(stats.items(), key=lambda kv: -kv[1])[:3]
        rendered = "; ".join(f"{k} ({v})" for k, v in top) or "—"
        A(f"| {sym} | {rendered[:150]} |")
    A("")

    A("## 7. Method and honesty notes")
    A("")
    A("**Calibration is walk-forward.** Each symbol's geometry and entry threshold "
      "were chosen only on training folds and reported on purged out-of-sample "
      "folds (`PurgedKFold`, with the embargo excluded from both train and test). "
      "The deployed configuration is the one the folds agree on most often, not "
      "the best in-sample point.")
    A("")
    A("**Win rate is a constrained objective, not a bare target.** The calibrator "
      "maximises expectancy subject to `win_rate >= target`, `trades >= min_trades` "
      "and `expectancy > 0`. A high win rate is trivially obtainable by shrinking "
      "the target relative to the stop, so a profile that reached 75% with "
      "negative expectancy would be rejected.")
    A("")
    A("**The regime policy is fitted out-of-sample.** Which regimes are switched "
      "off is learned from trades the fold-selected configuration would have "
      "taken *inside its training windows*, never from the out-of-sample bars it "
      "then filters. Fitting the policy on the same sample it filters is a "
      "filter tuned to the test set: measured on this data it moved the "
      "aggregate out-of-sample result from roughly break-even to about +52R, "
      "which is the size of the artefact. Section 2a publishes both figures so "
      "the contribution can be judged rather than assumed.")
    A("")
    A("**Intrabar ordering is conservative.** A bar whose range spans both the "
      "stop and the target is booked as a loss, because OHLC data does not reveal "
      "which came first. The previous engine resolved this optimistically, which "
      "inflated win rate exactly where the target lives.")
    A("")
    A("**The backtest is hermetic.** All persistent-state reads and writes on the "
      "decision path are disabled during simulation (`jarvis.config.runtime`), so "
      "results are reproducible and cannot be influenced by live trade history.")
    A("")
    A("### Known limitations")
    A("")
    A("* Three months of H1 data bounds the achievable sample: with "
      "one-position-at-a-time and a time stop, a symbol yields roughly "
      "`bars / max_bars` trades. Per-symbol win rates on 20-40 trades carry wide "
      "confidence intervals.")
    A("* Costs are modelled as the realised spread from the data plus the symbol "
      "profile's commission (zero on XM Ultra Low). Swap/financing is not modelled.")
    A("* Regime labels and the candidate set come from the same pipeline under "
      "test; a regime that the pipeline cannot detect will not appear here.")
    A("")
    return "\n".join(L)


if __name__ == "__main__":
    raise SystemExit(main())
