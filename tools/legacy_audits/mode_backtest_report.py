"""Aggregate the per-mode backtest JSONs into a comparable, honest report.

Reads ``reports/mode_backtest/<symbol>__<STYLE>.json`` and produces
``reports/mode_backtest_report.md`` plus a machine-readable JSON.

Two things this deliberately does NOT do:

  * it does not average per-symbol expectancies into a headline — a symbol with
    3 trades would otherwise carry the same weight as one with 60. Everything is
    pooled trade-by-trade;
  * it does not hide concentration. If one symbol supplies most of the positive
    R, that is stated next to the total, because a "profitable mode" that is one
    instrument wearing a costume is not a profitable mode.

Run:  python tools/mode_backtest_report.py
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SRC = os.path.join(REPO, "reports", "mode_backtest")
OUT_MD = os.path.join(REPO, "reports", "mode_backtest_report.md")
OUT_JSON = os.path.join(REPO, "reports", "mode_backtest_report.json")

STYLE_ORDER = ["SWING", "DAY_TRADING", "SCALP"]
STYLE_DESC = {
    "SWING": "D1 / H4 / H1 / H4 / M15  (primary H1)",
    "DAY_TRADING": "H4 / H1 / M15 / H1 / M5  (primary M15)",
    "SCALP": "H1 / M15 / M5 / M5 / M1  (primary M5)",
}


def _max_dd_r(pnl_r):
    """Max peak-to-trough on the cumulative-R curve."""
    arr = np.asarray(pnl_r, dtype=float)
    if arr.size == 0:
        return 0.0
    eq = np.cumsum(arr)
    peak = np.maximum.accumulate(eq)
    return float((peak - eq).max())


def _stats(rs):
    rs = np.asarray(rs, dtype=float)
    if len(rs) == 0:
        return {"trades": 0, "expectancy_r": 0.0, "total_r": 0.0, "win_rate_pct": 0.0,
                "profit_factor": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0,
                "max_dd_r": 0.0, "payoff_r": 0.0}
    wins, losses = rs[rs > 0], rs[rs < 0]
    gross_w, gross_l = float(wins.sum()), float(abs(losses.sum()))
    return {
        "trades": int(len(rs)),
        "expectancy_r": round(float(rs.mean()), 4),
        "total_r": round(float(rs.sum()), 2),
        "win_rate_pct": round(float(len(wins) / len(rs) * 100), 2),
        "profit_factor": round(gross_w / gross_l, 3) if gross_l > 0 else float("inf"),
        "avg_win_r": round(float(wins.mean()), 3) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.mean()), 3) if len(losses) else 0.0,
        "max_dd_r": round(_max_dd_r(rs), 2),
        "payoff_r": round(float(wins.mean() / abs(losses.mean())), 3)
        if len(wins) and len(losses) else 0.0,
    }


def load(src: str = SRC) -> dict:
    by_style = defaultdict(dict)
    for p in sorted(glob.glob(os.path.join(src, "*.json"))):
        with open(p, "r", encoding="utf-8") as fh:
            r = json.load(fh)
        if "error" in r:
            by_style[r["style"]]["_errors"] = by_style[r["style"]].get("_errors", [])
            by_style[r["style"]]["_errors"].append({"symbol": r["symbol"], "error": r["error"]})
            continue
        by_style[r["style"]][r["symbol"]] = r
    return by_style


def summarise(style: str, per_symbol: dict) -> dict:
    rows, all_r, all_times = [], [], []
    for sym, r in sorted(per_symbol.items()):
        if sym.startswith("_"):
            continue
        trades = r.get("trades", [])
        rs = [float(t.get("pnl_r", 0.0)) for t in trades]
        st = _stats(rs)
        st["symbol"] = sym
        st["window_days"] = r.get("window_days")
        st["primary_bars"] = r.get("primary_bars")
        st["total_r_from_pnl"] = round(sum(float(t.get("pnl", 0.0)) for t in trades), 2)
        st["net_profit"] = r.get("metrics", {}).get("net_profit", 0.0)
        st["max_dd_pct"] = r.get("metrics", {}).get("max_drawdown_pct", 0.0)
        rows.append(st)
        all_r.extend(rs)
        for t in trades:
            all_times.append((str(t.get("exit_time", "")), float(t.get("pnl_r", 0.0)), sym))

    pooled = _stats(all_r)
    # Portfolio curve: all symbols' trades interleaved by exit time, 1R risk each.
    all_times.sort(key=lambda x: x[0])
    pooled["max_dd_r"] = round(_max_dd_r([x[1] for x in all_times]), 2)

    # Concentration: how much of the gross positive R comes from the best symbol?
    pos_by_sym = {r["symbol"]: max(0.0, r["total_r"]) for r in rows}
    gross_pos = sum(pos_by_sym.values())
    top_sym = max(pos_by_sym, key=pos_by_sym.get) if pos_by_sym else None
    if gross_pos <= 0:
        concentration = 0.0
        top_sym = None
    else:
        concentration = round(pos_by_sym.get(top_sym, 0.0) / gross_pos * 100, 1)

    winners = [r for r in rows if r["total_r"] > 0]
    losers = [r for r in rows if r["total_r"] < 0]
    return {
        "style": style,
        "description": STYLE_DESC.get(style, ""),
        "symbols_run": len(rows),
        "errors": per_symbol.get("_errors", []),
        "pooled": pooled,
        "per_symbol": sorted(rows, key=lambda r: r["total_r"], reverse=True),
        "symbols_positive": len(winners),
        "symbols_negative": len(losers),
        "top_contributor": top_sym,
        "top_contributor_share_of_gross_positive_pct": concentration,
    }


def fmt_table(rows) -> str:
    head = ("| Symbol | Window | Bars | Trades | Exp R | Total R | WR % | PF | "
            "Avg win R | Avg loss R | MaxDD R | Net $ |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|"
    out = [head, sep]
    for r in rows:
        pf = r["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        out.append(
            f"| {r['symbol']} | {r['window_days']}d | {r['primary_bars']} | {r['trades']} | "
            f"{r['expectancy_r']:+.4f} | {r['total_r']:+.2f} | {r['win_rate_pct']:.1f} | "
            f"{pf_s} | {r['avg_win_r']:+.2f} | {r['avg_loss_r']:+.2f} | {r['max_dd_r']:.2f} | "
            f"{r['net_profit']:+.2f} |"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--out-md", default=OUT_MD)
    ap.add_argument("--out-json", default=OUT_JSON)
    args = ap.parse_args()

    by_style = load(args.src)
    if not by_style:
        print(f"no results under {args.src}")
        return 1

    summaries = [summarise(s, by_style[s]) for s in STYLE_ORDER if s in by_style]
    for s in by_style:
        if s not in STYLE_ORDER:
            summaries.append(summarise(s, by_style[s]))

    lines = ["# Six-Month Multi-Mode Backtest — Real MT5 Data", ""]
    lines.append("Every number below comes from the live broker feed (XMGlobal-MT5), "
                 "not a synthetic generator. Windows are the intersection over which "
                 "*every* timeframe the mode needs has real bars.")
    lines.append("")
    lines.append("| Mode | Timeframes | Symbols | Trades | Exp R | Total R | WR % | PF | MaxDD R | Positive symbols |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for s in summaries:
        p = s["pooled"]
        pf = p["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        lines.append(
            f"| **{s['style']}** | {s['description']} | {s['symbols_run']} | {p['trades']} | "
            f"{p['expectancy_r']:+.4f} | {p['total_r']:+.2f} | {p['win_rate_pct']:.1f} | {pf_s} | "
            f"{p['max_dd_r']:.2f} | {s['symbols_positive']}/{s['symbols_run']} |"
        )
    lines.append("")

    for s in summaries:
        p = s["pooled"]
        lines.append(f"## {s['style']}  ({s['description']})")
        lines.append("")
        lines.append(f"- Pooled trades: **{p['trades']}**  |  Expectancy: **{p['expectancy_r']:+.4f} R**  "
                     f"|  Total: **{p['total_r']:+.2f} R**")
        lines.append(f"- Win rate: **{p['win_rate_pct']:.1f}%**  |  Payoff: **{p['payoff_r']:.2f}**  "
                     f"|  Profit factor: **{p['profit_factor'] if p['profit_factor']==float('inf') else round(p['profit_factor'],2)}**  "
                     f"|  Max drawdown: **{p['max_dd_r']:.2f} R**")
        lines.append(f"- Symbols positive: **{s['symbols_positive']}/{s['symbols_run']}**")
        if s["top_contributor"]:
            lines.append(f"- Concentration: **{s['top_contributor']}** supplies "
                         f"**{s['top_contributor_share_of_gross_positive_pct']}%** of gross positive R")
        else:
            lines.append("- Concentration: **no symbol produced positive total R**")
        if s["errors"]:
            lines.append(f"- Errors: {len(s['errors'])}")
        lines.append("")
        lines.append(fmt_table(s["per_symbol"]))
        lines.append("")

    md = "\n".join(lines)
    with open(args.out_md, "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump({"modes": summaries}, fh, indent=2, default=str)

    for s in summaries:
        p = s["pooled"]
        print(f"{s['style']:12s} trades={p['trades']:5d} expR={p['expectancy_r']:+.4f} "
              f"totalR={p['total_r']:+8.2f} wr={p['win_rate_pct']:5.1f}% "
              f"pf={p['profit_factor'] if p['profit_factor']!=float('inf') else 'inf'} "
              f"pos={s['symbols_positive']}/{s['symbols_run']}")
    print(f"\n-> {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
