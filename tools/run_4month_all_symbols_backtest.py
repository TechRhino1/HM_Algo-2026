"""
HM Algo 2.0 — Complete 4-Month Multi-Symbol Historical Backtest.
Executes historical backtest on real MT5 data (June 17, 2026 - October 17, 2026, 122 days)
across all 18 institutional instruments:
Metals, Energy, Crypto, Forex Majors, and Global Indices.
"""
import os
import sys
import time
import json
import logging
from pathlib import Path
from typing import Dict, List, Any
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.backtesting.metrics import PerformanceMetricsCalculator
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.config.runtime import offline_mode

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backtest_4month")

REAL_DIR = REPO_ROOT / "data" / "market" / "real"
REPORT_DIR = REPO_ROOT / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

SYMBOLS = [
    # Commodities / Metals / Energy
    "XAUUSD", "XAGUSD", "WTI",
    # Crypto Assets
    "BTCUSD", "ETHUSD", "SOLUSD",
    # Forex Majors
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    # Global Indices
    "US30", "NAS100", "US500", "GER40", "UK100"
]

ASSET_CLASS_MAP = {
    "XAUUSD": "Metal", "XAGUSD": "Metal", "WTI": "Energy",
    "BTCUSD": "Crypto", "ETHUSD": "Crypto", "SOLUSD": "Crypto",
    "EURUSD": "Forex", "GBPUSD": "Forex", "USDJPY": "Forex",
    "AUDUSD": "Forex", "USDCAD": "Forex", "USDCHF": "Forex", "NZDUSD": "Forex",
    "US30": "Index", "NAS100": "Index", "US500": "Index",
    "GER40": "Index", "UK100": "Index"
}

INITIAL_BALANCE = 10_000.0
RISK_PER_TRADE_PCT = 0.5


def r_multiple(trade: dict) -> float:
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


def run_4month_backtest():
    print("=" * 125)
    print("                HM Algo 2.0 — COMPLETE 4-MONTH REAL MT5 DATA BACKTEST (ALL 18 SYMBOLS)")
    print("               Window: June 17, 2026 - October 17, 2026 (~122 Days) | Initial Balance: $10,000")
    print("=" * 125)

    all_trades: List[Dict[str, Any]] = []
    symbol_results: Dict[str, Dict[str, Any]] = {}
    exit_reasons_breakdown: Dict[str, int] = {}
    t_global_start = time.time()

    print(f"\n{'Symbol':<9} | {'Class':<7} | {'Bars':<5} | {'Trades':<6} | {'Win %':<7} | {'PF':<6} | {'Net P&L ($)':<12} | {'Max DD%':<7} | {'Avg Win':<8} | {'Avg Loss':<8} | {'Total R':<8} | {'Time'}")
    print("-" * 125)

    with offline_mode():
        for sym in SYMBOLS:
            # Look for 122d parquet first, then fallback to 183d/95d sliced to 122d
            p122 = REAL_DIR / sym / f"{sym}_H1_122d.parquet"
            p183 = REAL_DIR / sym / f"{sym}_H1_183d.parquet"
            p95 = REAL_DIR / sym / f"{sym}_H1_95d.parquet"

            if p122.exists():
                df = pd.read_parquet(p122).reset_index(drop=True)
            elif p183.exists():
                df = pd.read_parquet(p183).reset_index(drop=True)
                df = df.iloc[-2928:].reset_index(drop=True)
            elif p95.exists():
                df = pd.read_parquet(p95).reset_index(drop=True)
            else:
                print(f"{sym:<9} | {ASSET_CLASS_MAP.get(sym, 'Unknown'):<7} | SKIP (no parquet data)")
                continue

            spec = resolve_symbol(sym)
            spread = getattr(spec, "typical_spread_pips", 1.0) or 1.0

            t0 = time.time()
            engine = BacktestEngine(
                initial_balance=INITIAL_BALANCE,
                risk_per_trade_pct=RISK_PER_TRADE_PCT,
            )
            res = engine.run_backtest(df_h1=df, symbol=sym, spread_pips=spread)
            dur = time.time() - t0

            trades = res.get("trades", [])
            m = res.get("metrics", {})

            for t in trades:
                t["symbol"] = sym
                reason = t.get("exit_reason") or t.get("result") or "UNKNOWN"
                exit_reasons_breakdown[reason] = exit_reasons_breakdown.get(reason, 0) + 1

            all_trades.extend(trades)

            rs = [r_multiple(t) for t in trades]
            wins = [t for t in trades if t.get("pnl", 0.0) > 0]
            losses = [t for t in trades if t.get("pnl", 0.0) < 0]
            avg_win = float(np.mean([t["pnl"] for t in wins])) if wins else 0.0
            avg_loss = float(np.mean([t["pnl"] for t in losses])) if losses else 0.0
            total_r = float(np.sum(rs)) if rs else 0.0

            sym_entry = {
                "symbol": sym,
                "asset_class": ASSET_CLASS_MAP.get(sym, "Unknown"),
                "bars": len(df),
                "total_trades": len(trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate_pct": round(m.get("win_rate_pct", 0.0), 2),
                "profit_factor": round(m.get("profit_factor", 0.0), 2),
                "net_profit": round(m.get("net_profit", 0.0), 2),
                "roi_pct": round((m.get("net_profit", 0.0) / INITIAL_BALANCE) * 100.0, 2),
                "max_drawdown_pct": round(m.get("max_drawdown_pct", 0.0), 2),
                "sharpe_ratio": round(m.get("sharpe_ratio", 0.0), 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "total_r": round(total_r, 2),
                "expectancy_r": round(float(np.mean(rs)), 3) if rs else 0.0,
                "seconds": round(dur, 2)
            }
            symbol_results[sym] = sym_entry

            print(
                f"{sym:<9} | "
                f"{sym_entry['asset_class']:<7} | "
                f"{sym_entry['bars']:<5} | "
                f"{sym_entry['total_trades']:<6} | "
                f"{sym_entry['win_rate_pct']:<6.1f}% | "
                f"{sym_entry['profit_factor']:<6.2f} | "
                f"${sym_entry['net_profit']:<+11.2f} | "
                f"{sym_entry['max_drawdown_pct']:<6.2f}% | "
                f"${sym_entry['avg_win']:<7.2f} | "
                f"${sym_entry['avg_loss']:<7.2f} | "
                f"{sym_entry['total_r']:<+7.2f}R | "
                f"{dur:<4.1f}s"
            )

    print("=" * 125)

    # Aggregate Portfolio Metrics
    port_metrics = PerformanceMetricsCalculator.calculate_metrics(all_trades, INITIAL_BALANCE)
    port_trades = len(all_trades)
    port_wins = len([t for t in all_trades if t.get("pnl", 0.0) > 0])
    port_losses = len([t for t in all_trades if t.get("pnl", 0.0) < 0])
    port_win_rate = (port_wins / max(port_trades, 1)) * 100.0
    port_net_profit = sum(t.get("pnl", 0.0) for t in all_trades)
    port_roi = (port_net_profit / INITIAL_BALANCE) * 100.0
    gross_profit = sum(t.get("pnl", 0.0) for t in all_trades if t.get("pnl", 0.0) > 0)
    gross_loss = abs(sum(t.get("pnl", 0.0) for t in all_trades if t.get("pnl", 0.0) < 0))
    port_pf = gross_profit / max(gross_loss, 1e-9)

    port_rs = [r_multiple(t) for t in all_trades]
    port_total_r = sum(port_rs)
    port_exp_r = np.mean(port_rs) if port_rs else 0.0

    print("\n" + "=" * 85)
    print("                COMBINED 18-SYMBOL PORTFOLIO PERFORMANCE (4 MONTHS)")
    print("=" * 85)
    print(f"Total Combined Trades Executed  : {port_trades} trades")
    print(f"Winning Trades / Losing Trades  : {port_wins} W / {port_losses} L")
    print(f"Overall Portfolio Win Rate      : {port_win_rate:.2f}%")
    print(f"Portfolio Profit Factor         : {port_pf:.2f}")
    print(f"Gross Profit / Gross Loss       : ${gross_profit:,.2f} / ${gross_loss:,.2f}")
    print(f"Combined Net Realized Profit    : ${port_net_profit:+,.2f} ({port_roi:+.2f}%)")
    print(f"Total Realized R-Multiple       : {port_total_r:+.2f}R (Avg {port_exp_r:+.2f}R/trade)")
    print(f"Portfolio Max Drawdown          : {port_metrics.get('max_drawdown_pct', 0.0):.2f}%")
    print(f"Portfolio Sharpe Ratio          : {port_metrics.get('sharpe_ratio', 0.0):.2f}")
    print(f"Portfolio Sortino Ratio         : {port_metrics.get('sortino_ratio', 0.0):.2f}")
    print(f"Total Backtest Execution Time   : {time.time() - t_global_start:.1f} seconds")
    print("=" * 85)

    print("\n--- Exit Reasons Breakdown ---")
    for r, cnt in sorted(exit_reasons_breakdown.items(), key=lambda x: x[1], reverse=True):
        print(f"  {r:<35}: {cnt:4d} ({cnt/max(port_trades, 1)*100:.1f}%)")

    # Save to JSON
    json_path = REPORT_DIR / "backtest_4month_all_symbols.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": pd.Timestamp.now(tz="UTC").isoformat(),
            "window": "122 days (June 17, 2026 - October 17, 2026)",
            "symbols_count": len(symbol_results),
            "symbol_results": symbol_results,
            "portfolio": {
                "trades": port_trades,
                "wins": port_wins,
                "losses": port_losses,
                "win_rate_pct": round(port_win_rate, 2),
                "profit_factor": round(port_pf, 2),
                "gross_profit": round(gross_profit, 2),
                "gross_loss": round(gross_loss, 2),
                "net_profit": round(port_net_profit, 2),
                "roi_pct": round(port_roi, 2),
                "total_r": round(port_total_r, 2),
                "expectancy_r": round(float(port_exp_r), 3),
                "max_drawdown_pct": round(port_metrics.get("max_drawdown_pct", 0.0), 2),
                "sharpe_ratio": round(port_metrics.get("sharpe_ratio", 0.0), 2),
                "sortino_ratio": round(port_metrics.get("sortino_ratio", 0.0), 2),
            },
            "exit_reasons": exit_reasons_breakdown
        }, f, indent=2, default=str)
    print(f"\n[OK] Full 4-month backtest saved to: {json_path}")
    return symbol_results, port_metrics


if __name__ == "__main__":
    run_4month_backtest()
