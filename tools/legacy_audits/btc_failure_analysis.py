"""
6-month BTCUSD# (Bitcoin vs USD) backtest and per-trade failure attribution.

Runs the PRODUCTION ``BacktestEngine`` on real MT5 H1 history for ``BTCUSD#``
(the broker's tradable name; canonical ``BTCUSD``), then explains — trade by
trade — why the losing trades lost.

The analysis deliberately separates *what happened* from *why*:

* **Execution facts** come from the engine (exit reason, MFE/MAE, bars held).
* **Attribution** applies explicit, falsifiable rules to those facts. Every rule
  is a measurable predicate, not an opinion, and each losing trade is shown with
  the numbers that triggered its label.
* **Counterfactuals** re-simulate the *same entries* under different target and
  stop distances using the same conservative path model, so "the target was too
  far" is a measured statement rather than an assertion.

Usage::

    python tools/btc_failure_analysis.py
    python tools/btc_failure_analysis.py --days 183 --balance 10000 --risk 0.5
    python tools/btc_failure_analysis.py --legacy     # uncalibrated baseline
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine  # noqa: E402
from jarvis.backtesting.signal_scan import compute_atr  # noqa: E402
from jarvis.backtesting.trade_simulator import (  # noqa: E402
    Geometry,
    simulate_trade,
)
from jarvis.config.paths import DATA_DIR  # noqa: E402
from jarvis.config.runtime import offline_mode  # noqa: E402
from jarvis.data.symbol_registry import resolve as resolve_symbol  # noqa: E402
from jarvis.intelligence.winrate_targeting import WRProfileStore  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("btc_failure_analysis")

REAL_DIR = Path(DATA_DIR) / "market" / "real"
PROFILE_PATH = REPO_ROOT / "config" / "winrate_profiles.json"
REPORT_DIR = REPO_ROOT / "reports"

SYMBOL = "BTCUSD"
DEFAULT_DAYS = 183


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def median_spread_pips(df: pd.DataFrame, symbol: str) -> float:
    spec = resolve_symbol(symbol)
    pip = float(spec.pip_size or 0.0001)
    pt = 10.0 ** (-int(spec.digits or 5))
    if "spread" in df.columns and pip > 0 and pt > 0:
        vals = pd.to_numeric(df["spread"], errors="coerce").dropna()
        if len(vals):
            return float(vals.median() * pt / pip)
    return float(getattr(spec, "typical_spread_pips", 2.0) or 2.0)


def r_multiple(trade: dict) -> float:
    """Realised R. Uses the immutable ``initial_sl`` — never the trailed ``sl``."""
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


def exit_family(result: str) -> str:
    """Collapse the engine's exit labels into a small, analysable set."""
    r = str(result or "").upper()
    if r.startswith("STAGNATION") or "TIME_STOP" in r:
        return "TIME_STOP"
    if r == "TP" or r == "LADDER_TP":
        return "TARGET"
    if r == "BE/TRAIL_SL":
        return "TRAIL_OR_BE"
    if r == "SL":
        return "STOP"
    if r == "CLOSE_AT_END":
        return "END_OF_TEST"
    return r or "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# Market context at entry
# ─────────────────────────────────────────────────────────────────────────────
def build_entry_context(df: pd.DataFrame) -> pd.DataFrame:
    """Per-bar context used to judge entries: volatility, trend, range position."""
    ctx = pd.DataFrame({"time": df["time"]})
    ctx["atr"] = compute_atr(df, 14).to_numpy()
    ctx["atr_pct"] = ctx["atr"] / df["close"].astype(float).to_numpy() * 100.0

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    # Directional context: 24-bar momentum and 24-bar range position.
    ctx["mom24"] = (close - close.shift(24)).to_numpy()
    roll_hi = high.rolling(24, min_periods=5).max()
    roll_lo = low.rolling(24, min_periods=5).min()
    span = (roll_hi - roll_lo).replace(0, np.nan)
    ctx["range_pos"] = ((close - roll_lo) / span).to_numpy()

    # Trend strength: |close - SMA50| in ATR units.
    sma50 = close.rolling(50, min_periods=10).mean()
    ctx["trend_atr"] = ((close - sma50).abs() / ctx["atr"].replace(0, np.nan)).to_numpy()

    # Realised forward volatility is not knowable at entry; ATR percentile is.
    ctx["atr_rank"] = ctx["atr"].rolling(200, min_periods=50).rank(pct=True).to_numpy()

    # ATR change over the prior 24 bars: is volatility expanding or contracting?
    ctx["atr_chg24"] = (ctx["atr"] / ctx["atr"].shift(24) - 1.0).to_numpy()
    return ctx


def enrich_trades(trades: List[dict], df: pd.DataFrame) -> List[dict]:
    """Attach entry-bar market context and excursion measures (in R) to each trade."""
    if not trades:
        return []
    ctx = build_entry_context(df)
    df_t = pd.to_datetime(df["time"], utc=True)
    ctx.index = df_t
    ctx_by_time = ctx

    enriched: List[dict] = []
    for t in trades:
        row = dict(t)
        r = r_multiple(t)
        risk = abs(float(t.get("entry", 0.0)) - float(t.get("initial_sl", t.get("sl", 0.0))))
        if risk <= 0:
            risk = abs(float(t.get("risk_dist", 0.0) or 0.0))
        mfe = float(t.get("mfe", 0.0) or 0.0)
        mae = float(t.get("mae", 0.0) or 0.0)

        row["r"] = round(r, 4)
        row["risk_dist"] = risk
        row["mfe_r"] = round(mfe / risk, 4) if risk > 0 else 0.0
        row["mae_r"] = round(mae / risk, 4) if risk > 0 else 0.0
        row["exit_family"] = exit_family(t.get("result", ""))
        row["gross_r"] = None  # filled below via the cost model

        # Context lookup on the entry bar.
        ts = pd.Timestamp(t.get("open_time"))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        try:
            c = ctx_by_time.loc[ts]
        except KeyError:
            c = None
        for key, out_key in (
            ("atr", "atr"),
            ("atr_pct", "atr_pct"),
            ("mom24", "mom24"),
            ("range_pos", "range_pos"),
            ("trend_atr", "trend_atr"),
            ("atr_rank", "atr_rank"),
            ("atr_chg24", "atr_chg24"),
        ):
            val = None
            if c is not None:
                try:
                    val = float(c[key])
                except Exception:
                    val = None
            row[out_key] = None if (val is None or not np.isfinite(val)) else round(val, 6)

        # Stop distance expressed in ATR — "was the stop inside the noise?"
        row["stop_atr"] = (
            round(risk / row["atr"], 3) if row.get("atr") else None
        )
        row["tp_r_planned"] = round(float(t.get("planned_rr", 0.0) or 0.0), 4)
        row["captured_frac"] = (
            round(r / row["mfe_r"], 3) if row["mfe_r"] > 0 else None
        )

        # Trend alignment is a property of the ENTRY, not of the outcome, so it is
        # computed for every trade here. Computing it inside the loss classifier
        # left the column missing on winners and broke the direction breakdown.
        side = str(t.get("type", "")).upper()
        mom = row.get("mom24")
        row["counter_trend"] = bool(
            mom is not None and ((side == "BUY" and mom < 0) or (side == "SELL" and mom > 0))
        )
        enriched.append(row)
    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# Failure attribution
# ─────────────────────────────────────────────────────────────────────────────
def classify(trade: dict, cost_r: float, max_bars: Optional[int]) -> Dict[str, Any]:
    """Attribute a losing trade to a primary cause, with supporting evidence.

    Every branch is a measurable predicate over the engine's own outputs, so the
    label can be checked rather than trusted. ``flags`` lists every condition that
    fired; ``primary`` is the highest-priority one.
    """
    r = float(trade.get("r", 0.0))
    mfe = float(trade.get("mfe_r", 0.0))
    mae = float(trade.get("mae_r", 0.0))
    fam = trade.get("exit_family", "UNKNOWN")
    bars = int(trade.get("bars_held", 0) or 0)
    stop_atr = trade.get("stop_atr")
    tp_r = float(trade.get("tp_r_planned", 0.0) or 0.0)

    flags: List[str] = []
    detail: Dict[str, Any] = {}

    # ── Costs ────────────────────────────────────────────────────────────────
    # The engine nets spread + commission. If the gross move was favourable but
    # the net result is a loss, the trade was decided by transaction costs.
    gross_r = r + cost_r
    detail["gross_r"] = round(gross_r, 4)
    detail["cost_r"] = round(cost_r, 4)
    if gross_r > 0 >= r:
        flags.append("COSTS_FLIPPED_SIGN")

    # ── Time stop ────────────────────────────────────────────────────────────
    if fam == "TIME_STOP":
        flags.append("TIME_STOP_NO_PROGRESS")
        detail["time_stop_bars"] = bars
    elif max_bars and bars >= max_bars:
        flags.append("TIME_STOP_NO_PROGRESS")
        detail["time_stop_bars"] = bars

    # ── Stop-outs: did it ever work? ─────────────────────────────────────────
    if fam == "STOP":
        if mfe < 0.25:
            flags.append("STOPPED_WITHOUT_PROGRESS")
        elif mfe >= 0.5:
            flags.append("GAVE_BACK_FAVOURABLE_MOVE")
        else:
            flags.append("STOPPED_AFTER_MINOR_PROGRESS")

    # ── Breakeven / trailing stop clipped a live trade ────────────────────────
    if fam == "TRAIL_OR_BE":
        if mfe >= 0.75:
            flags.append("TRAIL_TOO_TIGHT")
        else:
            flags.append("BREAKEVEN_STOP_TOO_EARLY")

    # ── Entry location ───────────────────────────────────────────────────────
    side = str(trade.get("type", "")).upper()
    rp = trade.get("range_pos")
    if rp is not None:
        # Bought in the top decile of the 24-bar range, or sold in the bottom.
        if side == "BUY" and rp >= 0.85:
            flags.append("ENTRY_AT_RANGE_EXTREME")
        if side == "SELL" and rp <= 0.15:
            flags.append("ENTRY_AT_RANGE_EXTREME")

    # ── Trend alignment ──────────────────────────────────────────────────────
    against = bool(trade.get("counter_trend"))
    detail["counter_trend"] = against
    if against and mae >= 0.75:
        flags.append("COUNTER_TREND_ENTRY")

    # ── Stop inside the noise ────────────────────────────────────────────────
    if stop_atr is not None:
        detail["stop_atr"] = stop_atr
        if stop_atr < 1.0:
            flags.append("STOP_TIGHTER_THAN_1_ATR")
        elif stop_atr > 3.0:
            flags.append("STOP_WIDER_THAN_3_ATR")

    # ── Volatility conditions ────────────────────────────────────────────────
    ar = trade.get("atr_rank")
    if ar is not None:
        detail["atr_rank"] = ar
        if ar >= 0.85:
            flags.append("HIGH_VOLATILITY_REGIME")
    ac = trade.get("atr_chg24")
    if ac is not None and ac >= 0.5:
        flags.append("VOLATILITY_EXPANDING_AT_ENTRY")

    # ── Target realism ───────────────────────────────────────────────────────
    if tp_r > 0 and mfe >= 0.7 * tp_r and fam != "TARGET":
        flags.append("TARGET_NEARLY_REACHED")
        detail["mfe_vs_target"] = round(mfe / tp_r, 3)

    # ── Immediate adverse excursion ──────────────────────────────────────────
    if mae >= 1.0 and mfe < 0.2:
        flags.append("IMMEDIATE_ADVERSE_MOVE")

    # ── Primary cause: first match in this priority order ────────────────────
    priority = [
        "COSTS_FLIPPED_SIGN",
        "TIME_STOP_NO_PROGRESS",
        "TRAIL_TOO_TIGHT",
        "BREAKEVEN_STOP_TOO_EARLY",
        "GAVE_BACK_FAVOURABLE_MOVE",
        "IMMEDIATE_ADVERSE_MOVE",
        "STOPPED_WITHOUT_PROGRESS",
        "STOPPED_AFTER_MINOR_PROGRESS",
    ]
    primary = next((f for f in priority if f in flags), "UNCLASSIFIED")
    return {"primary": primary, "flags": flags, "detail": detail}


CAUSE_TEXT = {
    "COSTS_FLIPPED_SIGN": (
        "Transaction costs decided the trade — the raw price move was favourable "
        "but spread + commission turned it into a loss."
    ),
    "TIME_STOP_NO_PROGRESS": (
        "Time stop: price never travelled far enough within the bar budget. The "
        "target was outside what the holding period could realistically deliver."
    ),
    "TRAIL_TOO_TIGHT": (
        "Trailing stop too tight — the trade moved well into profit and was then "
        "closed by the trail before reaching the target."
    ),
    "BREAKEVEN_STOP_TOO_EARLY": (
        "Breakeven stop triggered too early — the position was moved to cost and "
        "then stopped on a normal pullback."
    ),
    "GAVE_BACK_FAVOURABLE_MOVE": (
        "Gave back a favourable move: the trade ran into profit, then reversed "
        "all the way to the stop. Exit management, not entry selection."
    ),
    "IMMEDIATE_ADVERSE_MOVE": (
        "Entry timing: price moved straight against the position and never "
        "recovered. The entry was taken into immediate adverse flow."
    ),
    "STOPPED_WITHOUT_PROGRESS": (
        "Stopped out with essentially no favourable excursion — the entry had no "
        "immediate follow-through."
    ),
    "STOPPED_AFTER_MINOR_PROGRESS": (
        "Stopped out after a small favourable move that fell short of the target."
    ),
    "UNCLASSIFIED": "Did not match a specific failure pattern.",
}


# ─────────────────────────────────────────────────────────────────────────────
# Counterfactuals on the real entries
# ─────────────────────────────────────────────────────────────────────────────
def counterfactual_target_sweep(
    trades: List[dict],
    df: pd.DataFrame,
    cost_price_equiv: float,
    targets: List[float],
) -> List[dict]:
    """Re-simulate every real entry at several target distances.

    The entry price, stop and entry bar are the engine's own, so this isolates
    the target distance: everything else about the trade is held fixed. Uses the
    same conservative simulator as calibration (a bar spanning both the stop and
    the target is booked as a loss).
    """
    if not trades:
        return []
    df = df.reset_index(drop=True)
    time_to_idx = {pd.Timestamp(t): i for i, t in enumerate(pd.to_datetime(df["time"], utc=True))}
    spec = resolve_symbol(SYMBOL)
    money_per_unit = float(getattr(spec, "money_per_price_unit_per_lot", 0.0) or 1.0)

    rows: List[dict] = []
    for tp_r in targets:
        outs = []
        for t in trades:
            ts = pd.Timestamp(t.get("open_time"))
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            idx = time_to_idx.get(ts)
            if idx is None:
                continue
            entry = float(t["entry"])
            side = str(t["type"]).upper()
            geom = Geometry(
                tp_r=float(tp_r), be_trigger_r=None, fast_cash_r=None,
                trail_atr=None, max_bars=int(t.get("max_bars") or 48), min_score=0.0,
            )
            outcome = simulate_trade(
                symbol=SYMBOL, side=side, entry_idx=idx, fill=entry,
                sl=float(t.get("initial_sl", t.get("sl"))), geom=geom,
                money_per_unit=money_per_unit, df=df,
                cost_price_equiv=cost_price_equiv, spec=spec,
                regime=str(t.get("regime", "UNKNOWN")),
            )
            if outcome is not None:
                outs.append(outcome)
        if not outs:
            continue
        rs = [o.pnl_r for o in outs]
        wins = [x for x in rs if x > 0]
        losses = [x for x in rs if x <= 0]
        rows.append({
            "tp_r": float(tp_r),
            "trades": len(outs),
            "win_rate": round(len(wins) / len(outs) * 100, 2),
            "expectancy_r": round(float(np.mean(rs)), 4),
            "total_r": round(float(np.sum(rs)), 2),
            "avg_win_r": round(float(np.mean(wins)), 3) if wins else 0.0,
            "avg_loss_r": round(float(np.mean(losses)), 3) if losses else 0.0,
            "profit_factor": round(
                abs(sum(wins)) / abs(sum(losses)), 3
            ) if losses and sum(losses) != 0 else float("inf"),
        })
    return rows


def counterfactual_stop_sweep(
    trades: List[dict],
    df: pd.DataFrame,
    cost_price_equiv: float,
    stop_mults: List[float],
    hold_target_price: bool = False,
) -> List[dict]:
    """Widen/narrow the stop (as a multiple of the original) at a fixed target.

    ``hold_target_price=False`` keeps the *R:R* fixed, so the target widens with
    the stop — this scales the whole trade envelope to a different volatility band.

    ``hold_target_price=True`` keeps the target at its ORIGINAL absolute price, so
    only the stop moves. That isolates the stop from the target: if expectancy
    improves here, the stop really was too tight rather than the target too close.
    """
    if not trades:
        return []
    df = df.reset_index(drop=True)
    time_to_idx = {pd.Timestamp(t): i for i, t in enumerate(pd.to_datetime(df["time"], utc=True))}
    spec = resolve_symbol(SYMBOL)
    money_per_unit = float(getattr(spec, "money_per_price_unit_per_lot", 0.0) or 1.0)

    rows: List[dict] = []
    for mult in stop_mults:
        outs = []
        for t in trades:
            ts = pd.Timestamp(t.get("open_time"))
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            idx = time_to_idx.get(ts)
            if idx is None:
                continue
            entry = float(t["entry"])
            side = str(t["type"]).upper()
            orig_risk = abs(entry - float(t.get("initial_sl", t.get("sl"))))
            if orig_risk <= 0:
                continue
            new_risk = orig_risk * float(mult)
            new_sl = entry - new_risk if side == "BUY" else entry + new_risk

            if hold_target_price:
                orig_tp = float(t.get("tp", 0.0) or 0.0)
                if orig_tp <= 0:
                    continue
                tp_r = abs(orig_tp - entry) / new_risk
            else:
                tp_r = float(t.get("tp_r_planned", 0.0) or 0.0) or 1.0

            geom = Geometry(
                tp_r=tp_r, be_trigger_r=None, fast_cash_r=None,
                trail_atr=None, max_bars=int(t.get("max_bars") or 48), min_score=0.0,
            )
            outcome = simulate_trade(
                symbol=SYMBOL, side=side, entry_idx=idx, fill=entry,
                sl=new_sl, geom=geom, money_per_unit=money_per_unit, df=df,
                cost_price_equiv=cost_price_equiv, spec=spec,
                regime=str(t.get("regime", "UNKNOWN")),
            )
            if outcome is not None:
                outs.append(outcome)
        if not outs:
            continue
        rs = [o.pnl_r for o in outs]
        wins = [x for x in rs if x > 0]
        rows.append({
            "stop_mult": float(mult),
            "trades": len(outs),
            "win_rate": round(len(wins) / len(outs) * 100, 2),
            "expectancy_r": round(float(np.mean(rs)), 4),
            "total_r": round(float(np.sum(rs)), 2),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
def _fmt(x: Any, nd: int = 2) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)


def render(report: dict) -> str:
    L: List[str] = []
    A = L.append
    m = report["meta"]
    s = report["summary"]

    A(f"# {SYMBOL}# — 6-Month Backtest and Trade-Failure Analysis")
    A("")
    A(f"**Instrument:** {m['broker_symbol']} (canonical `{m['symbol']}`), "
      f"{m['asset_class']}, broker `{m['server']}`")
    A(f"**Window:** {m['start']} → {m['end']}  ({m['days']} days, {m['bars']} H1 bars)")
    A(f"**Data:** {m['provenance']} — real MT5 history, structural validation "
      f"{'passed' if m['quality_ok'] else 'FAILED'}")
    A(f"**Configuration:** {'uncalibrated baseline (legacy 29-gate stack)' if m['legacy'] else 'calibrated win-rate profile'}"
      f"  ")
    A(f"**Account:** ${m['initial_balance']:,.0f}, {m['risk_per_trade_pct']}% risk per trade, "
      f"median spread {m['spread_pips']:.1f} pips, "
      f"slippage {m['slippage_pips']} pips")
    A("")

    # ── 1. Headline ──────────────────────────────────────────────────────────
    A("## 1. Headline result")
    A("")
    A("| Metric | Value |")
    A("|---|---:|")
    A(f"| Trades | {s['trades']} |")
    A(f"| Wins / Losses | {s['wins']} / {s['losses']} |")
    A(f"| Win rate | {s['win_rate']:.2f}% |")
    A(f"| Expectancy | {s['expectancy_r']:+.4f} R per trade |")
    A(f"| Total R | {s['total_r']:+.2f} R |")
    A(f"| Average win / loss | {s['avg_win_r']:+.3f} R / {s['avg_loss_r']:+.3f} R |")
    A(f"| Payoff ratio | {_fmt(s['payoff_ratio'], 3)} |")
    A(f"| Profit factor | {_fmt(s['profit_factor'], 3)} |")
    A(f"| Net profit | ${s['net_profit']:+,.2f} |")
    A(f"| Max drawdown | {s['max_drawdown_pct']:.2f}% |")
    A(f"| Average bars held | {_fmt(s['avg_bars_held'], 1)} |")
    A("")
    A(f"**Verdict.** {s['verdict']}")
    A("")

    cv = report.get("calibrated_verdict")
    if cv:
        A("### The calibrated edge test on this window")
        A("")
        A("| Stage | Trades | Win rate | Expectancy (R) |")
        A("|---|---:|---:|---:|")
        A(f"| In-sample (training folds) | {cv['in_sample_trades']} | "
          f"{cv['in_sample_win_rate']*100:.1f}% | {cv['in_sample_expectancy_r']:+.4f} |")
        A(f"| Purged out-of-sample | {cv['oos_trades']} | {cv['oos_win_rate']*100:.1f}% | "
          f"{cv['oos_expectancy_r']:+.4f} |")
        A("")
        if cv["would_refuse"]:
            A(f"**The calibrated system refuses to trade this symbol.** Its out-of-sample "
              f"expectancy is non-positive ({cv['oos_expectancy_r']:+.4f} R over "
              f"{cv['oos_trades']} trades) despite a positive in-sample figure "
              f"({cv['in_sample_expectancy_r']:+.4f} R) — i.e. the edge did not survive "
              f"the walk-forward split. The entry policy declines the symbol rather than "
              f"trading it at a smaller size, so the engine takes **zero** trades when the "
              f"calibrated profile is active.")
            A("")
            A(f"> {cv['binding_constraint']}")
            A("")
            A("This is the single most important result on this page: the strategy *as "
              "configured by the legacy gates* does trade, and those trades are analysed "
              "below — but the calibrated configuration judges the symbol unprofitable and "
              "stands aside. The two are not in conflict; the second is what the evidence "
              "says about the first.")
        else:
            A("The calibrated profile passes the edge test on this window, so it would "
              "trade this symbol.")
        A("")

    # ── 2. Exit-reason breakdown ─────────────────────────────────────────────
    A("## 2. How trades ended")
    A("")
    A("| Exit | Count | Share | Avg R | Total R | Win rate |")
    A("|---|---:|---:|---:|---:|---:|")
    for row in report["exit_breakdown"]:
        A(f"| {row['exit_family']} | {row['count']} | {row['share']*100:.1f}% | "
          f"{row['avg_r']:+.3f} | {row['total_r']:+.2f} | {row['win_rate']:.1f}% |")
    A("")

    # ── 3. Failure taxonomy ──────────────────────────────────────────────────
    A("## 3. Why the losing trades lost")
    A("")
    A(f"Of {s['losses']} losing trades, every one is attributed to a primary cause "
      f"by explicit measurable rules (no discretion). A trade can trip several "
      f"conditions; the table counts *primary* cause and lists all contributing "
      f"flags separately.")
    A("")
    A("| Primary cause | Trades | Share of losses | Total R lost | Avg R |")
    A("|---|---:|---:|---:|---:|")
    for row in report["cause_summary"]:
        A(f"| {row['cause']} | {row['count']} | {row['share']*100:.1f}% | "
          f"{row['total_r']:+.2f} | {row['avg_r']:+.3f} |")
    A("")
    for row in report["cause_summary"]:
        if row["cause"] in CAUSE_TEXT:
            A(f"- **{row['cause']}** — {CAUSE_TEXT[row['cause']]}")
    A("")

    A("### Contributing conditions across all losses")
    A("")
    A("| Condition | Losses where it fired | Share |")
    A("|---|---:|---:|")
    for row in report["flag_summary"]:
        A(f"| {row['flag']} | {row['count']} | {row['share']*100:.1f}% |")
    A("")

    # ── 4. Every losing trade ────────────────────────────────────────────────
    A("## 4. Trade-by-trade failure ledger")
    A("")
    A("Every losing trade with the evidence that produced its label. `MFE` is the "
      "best unrealised excursion (how far the trade ever went in favour) and `MAE` "
      "the worst, both in R. `stop/ATR` below 1.0 means the stop sat inside one "
      "bar's normal range.")
    A("")
    A("| # | Entry time | Side | Entry | Stop | R | MFE (R) | MAE (R) | Bars | Exit | stop/ATR | range pos | counter-trend | Primary cause |")
    A("|---:|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|:--:|---|")
    for i, t in enumerate(report["losing_trades"], 1):
        A(f"| {i} | {str(t['open_time'])[:16]} | {t['type']} | {t['entry']:.1f} | "
          f"{t['initial_sl']:.1f} | {t['r']:+.2f} | {t['mfe_r']:.2f} | {t['mae_r']:.2f} | "
          f"{t['bars_held']} | {t['exit_family']} | {_fmt(t.get('stop_atr'), 2)} | "
          f"{_fmt(t.get('range_pos'), 2)} | "
          f"{'yes' if t.get('counter_trend') else 'no'} | {t['primary_cause']} |")
    A("")

    # ── 5. Market conditions ─────────────────────────────────────────────────
    A("## 5. Market conditions")
    A("")
    A("### By market regime")
    A("")
    A("| Regime | Trades | Win rate | Avg R | Total R |")
    A("|---|---:|---:|---:|---:|")
    for row in report["by_regime"]:
        A(f"| {row['regime']} | {row['trades']} | {row['win_rate']:.1f}% | "
          f"{row['avg_r']:+.3f} | {row['total_r']:+.2f} |")
    A("")
    A("### By volatility at entry (ATR percentile over the trailing 200 bars)")
    A("")
    A("| ATR percentile | Trades | Win rate | Avg R | Total R |")
    A("|---|---:|---:|---:|---:|")
    for row in report["by_volatility"]:
        A(f"| {row['bucket']} | {row['trades']} | {row['win_rate']:.1f}% | "
          f"{row['avg_r']:+.3f} | {row['total_r']:+.2f} |")
    A("")
    A("### By direction and trend alignment")
    A("")
    A("| Group | Trades | Win rate | Avg R | Total R |")
    A("|---|---:|---:|---:|---:|")
    for row in report["by_direction"]:
        A(f"| {row['group']} | {row['trades']} | {row['win_rate']:.1f}% | "
          f"{row['avg_r']:+.3f} | {row['total_r']:+.2f} |")
    A("")

    # ── 6. Counterfactuals ───────────────────────────────────────────────────
    A("## 6. Parameter counterfactuals (same entries, different parameters)")
    A("")
    A("Each row re-simulates the **identical entries** — same entry price, same "
      "entry bar, same original stop — changing only the parameter named. This "
      "separates 'the entries were bad' from 'the parameters were wrong'.")
    A("")
    A("### 6a. Target distance (`tp_r`, in multiples of initial risk)")
    A("")
    A("| tp_r | Trades | Win rate | Expectancy (R) | Total R | Avg win | Avg loss | PF |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in report["cf_targets"]:
        A(f"| {row['tp_r']:g} | {row['trades']} | {row['win_rate']:.1f}% | "
          f"{row['expectancy_r']:+.4f} | {row['total_r']:+.2f} | {row['avg_win_r']:+.3f} | "
          f"{row['avg_loss_r']:+.3f} | {_fmt(row['profit_factor'], 2)} |")
    A("")
    A("### 6b. Stop distance (multiple of the original stop, target held fixed in R:R)")
    A("")
    A("The target widens with the stop, so this scales the whole trade envelope to "
      "a different volatility band while keeping reward:risk unchanged.")
    A("")
    A("| Stop × | Trades | Win rate | Expectancy (R) | Total R |")
    A("|---|---:|---:|---:|---:|")
    for row in report["cf_stops"]:
        A(f"| {row['stop_mult']:g}× | {row['trades']} | {row['win_rate']:.1f}% | "
          f"{row['expectancy_r']:+.4f} | {row['total_r']:+.2f} |")
    A("")
    A("### 6c. Stop distance with the target held at its ORIGINAL price")
    A("")
    A("Here only the stop moves — the target stays exactly where the strategy put "
      "it. If expectancy improves, the stop was genuinely too tight rather than the "
      "target too close. This is the cleanest test of 'are we being stopped out by "
      "noise?'.")
    A("")
    A("| Stop × | Trades | Win rate | Expectancy (R) | Total R |")
    A("|---|---:|---:|---:|---:|")
    for row in report.get("cf_stops_iso", []):
        A(f"| {row['stop_mult']:g}× | {row['trades']} | {row['win_rate']:.1f}% | "
          f"{row['expectancy_r']:+.4f} | {row['total_r']:+.2f} |")
    A("")

    # ── 7. Conclusions ───────────────────────────────────────────────────────
    A("## 7. What is actually wrong")
    A("")
    for line in report["conclusions"]:
        A(line)
        A("")
    A("## 8. Method and limitations")
    A("")
    for line in report["method_notes"]:
        A(f"* {line}")
    A("")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--balance", type=float, default=10_000.0)
    ap.add_argument("--risk", type=float, default=0.5)
    ap.add_argument("--slippage-pips", type=float, default=0.5)
    ap.add_argument("--legacy", action="store_true",
                    help="run WITHOUT the calibrated profile (baseline)")
    ap.add_argument("--symbol", default=SYMBOL)
    args = ap.parse_args()

    sym = args.symbol.upper()
    dpath = REAL_DIR / sym / f"{sym}_H1_{args.days}d.parquet"
    if not dpath.exists():
        logger.error(f"No data at {dpath}. Fetch it with jarvis.data.mt5_history.")
        return 2
    df = pd.read_parquet(dpath).reset_index(drop=True)
    manifest_path = dpath.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    profile = None
    if not args.legacy:
        profiles = WRProfileStore(PROFILE_PATH).load()
        profile = profiles.get(sym)
        if profile is None:
            logger.error(f"No calibrated profile for {sym}; run tools/calibrate_winrate.py.")
            return 2

    spread = median_spread_pips(df, sym)
    spec = resolve_symbol(sym)

    # Always record what the calibrated system would do, even when the analysis
    # runs on the legacy strategy. The contrast between "what the gates trade" and
    # "what the calibrated edge test permits" is a core part of the answer.
    calibrated_verdict: Optional[Dict[str, Any]] = None
    try:
        cp = WRProfileStore(PROFILE_PATH).load().get(sym)
        if cp is not None:
            calibrated_verdict = {
                "in_sample_trades": cp.n_trades,
                "in_sample_win_rate": cp.win_rate,
                "in_sample_expectancy_r": cp.expectancy_r,
                "oos_trades": cp.oos_trades,
                "oos_win_rate": cp.oos_win_rate,
                "oos_expectancy_r": cp.oos_expectancy_r,
                "oos_profit_factor": cp.oos_profit_factor,
                "target_met_oos": cp.oos_target_met,
                "binding_constraint": cp.binding_constraint,
                "tp_r": cp.geometry.tp_r,
                "min_score": cp.geometry.min_score,
                "max_bars": cp.geometry.max_bars,
                "enabled_regimes": cp.enabled_regimes,
                "would_refuse": bool(cp.oos_trades >= 10 and cp.oos_expectancy_r <= 0.0),
            }
    except Exception as exc:  # pragma: no cover
        logger.warning(f"could not read calibrated profile: {exc}")

    # Cost of one round trip expressed in price units: spread + slippage.
    cost_price_equiv = spread * float(spec.pip_size) + args.slippage_pips * float(spec.pip_size)

    print(f"Running {sym} {args.days}d backtest "
          f"({'legacy' if args.legacy else 'calibrated'}) ...")
    t0 = time.time()
    with offline_mode():
        engine = BacktestEngine(
            initial_balance=args.balance, risk_per_trade_pct=args.risk
        )
        res = engine.run_backtest(
            df_h1=df, symbol=sym, spread_pips=spread,
            slippage_delta=args.slippage_pips, wr_profile=profile,
        )
    trades = res.get("trades", [])
    metrics = res.get("metrics", {})
    rejections = res.get("rejection_stats", {})
    print(f"  {len(trades)} trades in {time.time()-t0:.1f}s")

    if not trades:
        # A zero-trade run is a RESULT, not an error: the entry policy refuses a
        # symbol whose calibrated out-of-sample expectancy is non-positive. Say so
        # explicitly rather than dying — "we decline to trade this" is the most
        # important thing a backtest can report.
        refusal = next(
            (r for r in rejections if "refusing symbol" in r),
            None,
        )
        print()
        print("=" * 100)
        print(f"{sym} {args.days}d | NO TRADES TAKEN")
        if refusal:
            print(f"  {refusal}")
        print("=" * 100)
        top = sorted(rejections.items(), key=lambda kv: -kv[1])[:8]
        for k, v in top:
            print(f"  {v:>6}  {k[:110]}")
        payload = {
            "meta": {
                "symbol": manifest.get("symbol", sym),
                "broker_symbol": manifest.get("broker_symbol", sym),
                "days": args.days,
                "bars": len(df),
                "legacy": bool(args.legacy),
                "start": manifest.get("start", "?"),
                "end": manifest.get("end", "?"),
                "provenance": manifest.get("provenance", "unknown"),
            },
            "summary": {"trades": 0, "verdict": "No trades taken."},
            "refusal": refusal,
            "calibrated_verdict": calibrated_verdict,
            "rejection_stats": rejections,
        }
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        tag = "legacy" if args.legacy else "calibrated"
        (REPORT_DIR / f"btcusd_{args.days}d_failure_analysis_{tag}.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        return 0

    enriched = enrich_trades(trades, df)
    max_bars = int(profile.geometry.max_bars) if profile is not None else None

    # Cost in R per trade (used to detect cost-dominated losses).
    avg_risk = float(np.mean([t["risk_dist"] for t in enriched if t["risk_dist"] > 0]) or 1.0)
    cost_r = cost_price_equiv / avg_risk if avg_risk > 0 else 0.0

    losers = [t for t in enriched if t["r"] <= 0]
    for t in losers:
        cls = classify(t, cost_r, max_bars)
        t["primary_cause"] = cls["primary"]
        t["flags"] = cls["flags"]
        t["detail"] = cls["detail"]

    # ── Summary ─────────────────────────────────────────────────────────────
    rs = [t["r"] for t in enriched]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    summary = {
        "trades": len(enriched),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(enriched) * 100,
        "expectancy_r": float(np.mean(rs)),
        "total_r": float(np.sum(rs)),
        "avg_win_r": float(np.mean(wins)) if wins else 0.0,
        "avg_loss_r": float(np.mean(losses)) if losses else 0.0,
        "payoff_ratio": (float(np.mean(wins)) / abs(float(np.mean(losses)))) if wins and losses else 0.0,
        "profit_factor": metrics.get("profit_factor", 0.0),
        "net_profit": metrics.get("net_profit", 0.0),
        "max_drawdown_pct": metrics.get("max_drawdown_pct", 0.0),
        "avg_bars_held": float(np.mean([t.get("bars_held", 0) for t in enriched])),
    }
    exp = summary["expectancy_r"]
    if exp > 0.05 and summary["profit_factor"] > 1.2:
        summary["verdict"] = "Profitable and above cost. Trades are working."
    elif exp > 0:
        summary["verdict"] = (
            f"Marginally positive ({exp:+.4f} R/trade) but inside the noise: the edge "
            f"is not large enough to be distinguished from zero on {len(enriched)} trades."
        )
    else:
        summary["verdict"] = (
            f"Losing system: {exp:+.4f} R per trade over {len(enriched)} trades. "
            f"The losses are analysed below."
        )

    # ── Exit breakdown ──────────────────────────────────────────────────────
    exit_breakdown = []
    for fam, grp in pd.DataFrame(enriched).groupby("exit_family"):
        g_rs = grp["r"].tolist()
        exit_breakdown.append({
            "exit_family": fam,
            "count": len(grp),
            "share": len(grp) / len(enriched),
            "avg_r": float(np.mean(g_rs)),
            "total_r": float(np.sum(g_rs)),
            "win_rate": float(np.mean([1.0 if x > 0 else 0.0 for x in g_rs]) * 100),
        })
    exit_breakdown.sort(key=lambda r: -r["count"])

    # ── Cause summary ───────────────────────────────────────────────────────
    cause_summary = []
    for cause, grp in pd.DataFrame(losers).groupby("primary_cause"):
        g_rs = grp["r"].tolist()
        cause_summary.append({
            "cause": cause,
            "count": len(grp),
            "share": len(grp) / max(1, len(losers)),
            "total_r": float(np.sum(g_rs)),
            "avg_r": float(np.mean(g_rs)),
        })
    cause_summary.sort(key=lambda r: -r["count"])

    flag_counts: Dict[str, int] = {}
    for t in losers:
        for f in t.get("flags", []):
            flag_counts[f] = flag_counts.get(f, 0) + 1
    flag_summary = sorted(
        ({"flag": k, "count": v, "share": v / max(1, len(losers))} for k, v in flag_counts.items()),
        key=lambda r: -r["count"],
    )

    # ── Market-condition breakdowns ─────────────────────────────────────────
    def group_stats(frame: pd.DataFrame, key: str) -> List[dict]:
        """Group by ``key``; emit the label under ``group`` and under ``key``.

        Callers render different column headers (``regime``, ``bucket``, ``group``),
        so carrying both names avoids a KeyError per call site.
        """
        out = []
        for name, grp in frame.groupby(key):
            g_rs = grp["r"].tolist()
            out.append({
                "group": str(name),
                key: str(name),
                "trades": len(grp),
                "win_rate": float(np.mean([1.0 if x > 0 else 0.0 for x in g_rs]) * 100),
                "avg_r": float(np.mean(g_rs)),
                "total_r": float(np.sum(g_rs)),
            })
        out.sort(key=lambda r: -r["trades"])
        return out

    edf = pd.DataFrame(enriched)
    by_regime = group_stats(edf, "regime")

    vol = edf.copy()
    vol["bucket"] = pd.cut(
        vol["atr_rank"].astype(float),
        bins=[-0.01, 0.25, 0.5, 0.75, 1.01],
        labels=["Q1 lowest vol", "Q2", "Q3", "Q4 highest vol"],
    )
    by_volatility = []
    for name, grp in vol.dropna(subset=["bucket"]).groupby("bucket", observed=True):
        g_rs = grp["r"].tolist()
        by_volatility.append({
            "bucket": str(name),
            "trades": len(grp),
            "win_rate": float(np.mean([1.0 if x > 0 else 0.0 for x in g_rs]) * 100),
            "avg_r": float(np.mean(g_rs)),
            "total_r": float(np.sum(g_rs)),
        })

    edf["_dir"] = np.where(edf["type"].astype(str).str.upper() == "BUY", "BUY", "SELL")
    edf["_align"] = np.where(
        edf["counter_trend"].fillna(False).astype(bool), "counter-trend", "with-trend"
    )
    by_direction = group_stats(edf, "_dir") + group_stats(edf, "_align")

    # ── Counterfactuals ─────────────────────────────────────────────────────
    cf_targets = counterfactual_target_sweep(
        enriched, df, cost_price_equiv, [0.25, 0.3, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
    )
    cf_stops = counterfactual_stop_sweep(
        enriched, df, cost_price_equiv, [0.75, 1.0, 1.5, 2.0, 3.0]
    )
    cf_stops_iso = counterfactual_stop_sweep(
        enriched, df, cost_price_equiv, [0.75, 1.0, 1.5, 2.0, 3.0], hold_target_price=True
    )

    # ── Conclusions (derived, not asserted) ─────────────────────────────────
    conclusions: List[str] = []
    top_cause = cause_summary[0] if cause_summary else None
    if top_cause:
        conclusions.append(
            f"**Dominant failure mode: `{top_cause['cause']}`** — "
            f"{top_cause['count']} of {len(losers)} losses "
            f"({top_cause['share']*100:.0f}%), costing {top_cause['total_r']:+.2f} R. "
            f"{CAUSE_TEXT.get(top_cause['cause'], '')}"
        )

    best_tp = max(cf_targets, key=lambda r: r["expectancy_r"]) if cf_targets else None
    cur_tp = float(profile.geometry.tp_r) if profile is not None else None
    if best_tp and cur_tp is not None:
        if best_tp["tp_r"] != cur_tp and best_tp["expectancy_r"] > summary["expectancy_r"]:
            conclusions.append(
                f"**The target distance is mis-set.** Deployed `tp_r={cur_tp:g}`; the "
                f"same entries score best at `tp_r={best_tp['tp_r']:g}` "
                f"({best_tp['expectancy_r']:+.4f} R/trade vs {summary['expectancy_r']:+.4f}). "
                f"Win rate moves to {best_tp['win_rate']:.1f}%."
            )
        else:
            conclusions.append(
                f"**The target distance is not the problem.** `tp_r={cur_tp:g}` is already "
                f"the best of the swept range on these entries."
            )

    stop_losses = sum(1 for t in losers if t["exit_family"] == "STOP")
    time_losses = sum(1 for t in losers if t["exit_family"] == "TIME_STOP")
    if stop_losses:
        conclusions.append(
            f"**{stop_losses} of {len(losers)} losses are stop-outs** and "
            f"{time_losses} are time stops — so the loss mix is dominated by "
            f"{'premature stop-outs' if stop_losses > time_losses else 'failure to reach the target in time'}."
        )

    # The isolated stop sweep is the sharpest statement available about the stop.
    if cf_stops_iso:
        best_iso = max(cf_stops_iso, key=lambda r: r["expectancy_r"])
        base_iso = next((r for r in cf_stops_iso if abs(r["stop_mult"] - 1.0) < 1e-9), None)
        if base_iso and best_iso["stop_mult"] > 1.0 and best_iso["expectancy_r"] > base_iso["expectancy_r"]:
            conclusions.append(
                f"**The stop is too tight for this instrument.** Holding the target "
                f"at exactly the price the strategy chose and moving only the stop, "
                f"expectancy rises from {base_iso['expectancy_r']:+.4f} R at 1× to "
                f"{best_iso['expectancy_r']:+.4f} R at {best_iso['stop_mult']:g}× "
                f"(win rate {base_iso['win_rate']:.1f}% → {best_iso['win_rate']:.1f}%). "
                f"Because only the stop moved, this is noise stop-out, not a target "
                f"that was set too close."
            )

    # The exit mix: are winners being cut short relative to losers?
    trail_share = next(
        (r for r in exit_breakdown if r["exit_family"] == "TRAIL_OR_BE"), None
    )
    stop_share = next((r for r in exit_breakdown if r["exit_family"] == "STOP"), None)
    if trail_share and stop_share and trail_share["count"] > 0:
        conclusions.append(
            f"**The payoff is inverted.** {trail_share['count']} trades "
            f"({trail_share['share']*100:.0f}%) closed on the trail/breakeven stop for an "
            f"average of {trail_share['avg_r']:+.3f} R, while {stop_share['count']} "
            f"({stop_share['share']*100:.0f}%) took the full {stop_share['avg_r']:+.3f} R loss. "
            f"Only "
            f"{sum(1 for t in enriched if t['exit_family'] == 'TARGET')} trades reached the "
            f"target. Winners are being cut short while losers run to the stop — that "
            f"structure loses money at any win rate below roughly "
            f"{abs(stop_share['avg_r'])/(abs(stop_share['avg_r'])+trail_share['avg_r'])*100:.0f}%."
        )

    tight = flag_counts.get("STOP_TIGHTER_THAN_1_ATR", 0)
    if tight:
        conclusions.append(
            f"**{tight} losses had a stop tighter than 1×ATR at entry**, i.e. inside "
            f"one bar's ordinary range. Those are noise stop-outs, not wrong calls."
        )
    ct = flag_counts.get("COUNTER_TREND_ENTRY", 0)
    if ct:
        conclusions.append(
            f"**{ct} losses were taken against the 24-bar trend** and went at least "
            f"0.75R against immediately afterwards."
        )
    ext = flag_counts.get("ENTRY_AT_RANGE_EXTREME", 0)
    if ext:
        conclusions.append(
            f"**{ext} losses were entered at the extreme of the 24-bar range** "
            f"(bought in the top 15% / sold in the bottom 15%) — chasing."
        )
    hv = flag_counts.get("HIGH_VOLATILITY_REGIME", 0)
    if hv:
        conclusions.append(
            f"**{hv} losses occurred in the highest-volatility quartile** "
            f"(ATR ≥ 85th percentile of the trailing 200 bars)."
        )

    method_notes = [
        f"Real MT5 H1 bars for `{sym}` ({manifest.get('broker_symbol', 'n/a')}), "
        f"{manifest.get('rows', len(df))} bars, "
        f"{manifest.get('start', '?')} → {manifest.get('end', '?')}; "
        f"provenance `{manifest.get('provenance', 'unknown')}`.",
        "Execution is the production `BacktestEngine`: entry at the next bar's open, "
        "spread paid on the traded side, stop tested before target within a bar "
        "(conservative — a bar spanning both is booked as a loss).",
        f"Costs: median spread {spread:.2f} pips from the data plus "
        f"{args.slippage_pips} pips slippage per side. Swap/financing on crypto "
        f"positions is NOT modelled, so a real long-held BTC position would carry "
        f"additional financing cost.",
        "Failure attribution uses only the engine's own outputs (exit reason, MFE, "
        "MAE, bars held) plus market context computed from the same bars. Each rule "
        "is a numeric predicate; the ledger shows the numbers.",
        "MFE/MAE are bar-resolution excursions, so they understate true intra-bar "
        "extremes on a volatile instrument like BTC.",
        f"One instrument, one window. {len(enriched)} trades is a small sample: "
        f"per-bucket win rates below carry wide confidence intervals.",
    ]

    report = {
        "meta": {
            "symbol": manifest.get("symbol", sym),
            "broker_symbol": manifest.get("broker_symbol", sym),
            "asset_class": spec.asset_class,
            "server": "XMGlobal-MT5 5",
            "start": manifest.get("start", "?"),
            "end": manifest.get("end", "?"),
            "days": args.days,
            "bars": len(df),
            "provenance": manifest.get("provenance", "unknown"),
            "quality_ok": bool(manifest.get("quality", {}).get("ok", False)),
            "legacy": bool(args.legacy),
            "initial_balance": args.balance,
            "risk_per_trade_pct": args.risk,
            "spread_pips": round(spread, 3),
            "slippage_pips": args.slippage_pips,
        },
        "summary": summary,
        "exit_breakdown": exit_breakdown,
        "cause_summary": cause_summary,
        "flag_summary": flag_summary,
        "losing_trades": losers,
        "by_regime": by_regime,
        "by_volatility": by_volatility,
        "by_direction": by_direction,
        "cf_targets": cf_targets,
        "cf_stops": cf_stops,
        "cf_stops_iso": cf_stops_iso,
        "conclusions": conclusions,
        "method_notes": method_notes,
        "calibrated_verdict": calibrated_verdict,
        "rejection_stats": res.get("rejection_stats", {}),
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    tag = "legacy" if args.legacy else "calibrated"
    (REPORT_DIR / f"btcusd_{args.days}d_failure_analysis_{tag}.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (REPORT_DIR / f"btcusd_{args.days}d_failure_analysis_{tag}.md").write_text(
        render(report), encoding="utf-8"
    )

    print()
    print("=" * 100)
    print(f"{sym} {args.days}d | trades={summary['trades']} WR={summary['win_rate']:.1f}% "
          f"exp={summary['expectancy_r']:+.4f}R PF={_fmt(summary['profit_factor'],2)} "
          f"net=${summary['net_profit']:+,.2f} DD={summary['max_drawdown_pct']:.2f}%")
    print("-" * 100)
    for row in cause_summary:
        print(f"  {row['cause']:<32} {row['count']:>4} losses ({row['share']*100:>5.1f}%)  "
              f"{row['total_r']:+.2f} R")
    print("-" * 100)
    for row in cf_targets:
        print(f"  tp_r={row['tp_r']:<5g} WR={row['win_rate']:>5.1f}%  exp={row['expectancy_r']:+.4f}R  "
              f"total={row['total_r']:+.2f}R")
    print("=" * 100)
    print(f"Report: reports/btcusd_{args.days}d_failure_analysis_{tag}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
