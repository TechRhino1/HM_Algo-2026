import os
import sys
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
"""
HM Algo 2.0 — 2-Month Real MT5 Historical Backtest Runner (All Symbols).
Loads 2 months of real H1 bar data directly from MetaTrader 5 institutional history cache,
executes event-driven backtests across all 13 symbols in parallel, and generates full performance metrics.
"""
import os
import sys

os.environ["JARVIS_BACKTEST_MODE"] = "1"
os.environ["JARVIS_OFFLINE_MODE"] = "1"

import json
import time
import struct
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

import pandas as pd
import numpy as np

BASE_DIR = str(Path(__file__).resolve().parent.parent.parent)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from jarvis.backtesting.engine import BacktestEngine
from jarvis.backtesting.metrics import PerformanceMetricsCalculator
from jarvis.data.symbol_registry import resolve as resolve_symbol

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("2MonthMT5Backtest")

# Master Symbol Mapping: Canonical Name -> MT5 Broker Directory Name & Asset Class
SYMBOL_CONFIGS = [
    {"canonical": "XAUUSD", "broker_dir": "GOLD.i#",    "asset_class": "Metal"},
    {"canonical": "BTCUSD", "broker_dir": "BTCUSD#",    "asset_class": "Crypto"},
    {"canonical": "ETHUSD", "broker_dir": "ETHUSD#",    "asset_class": "Crypto"},
    {"canonical": "SOLUSD", "broker_dir": "SOLUSD#",    "asset_class": "Crypto"},
    {"canonical": "EURUSD", "broker_dir": "EURUSD#",    "asset_class": "Forex", "fallback_dir": "EURUSD"},
    {"canonical": "GBPUSD", "broker_dir": "GBPUSD#",    "asset_class": "Forex", "fallback_dir": "GBPUSD"},
    {"canonical": "USDJPY", "broker_dir": "USDJPY#",    "asset_class": "Forex", "fallback_dir": "USDJPY"},
    {"canonical": "AUDUSD", "broker_dir": "AUDUSD#",    "asset_class": "Forex"},
    {"canonical": "USDCHF", "broker_dir": "USDCHF#",    "asset_class": "Forex"},
    {"canonical": "US30",   "broker_dir": "US30Cash#",  "asset_class": "Index"},
    {"canonical": "NAS100", "broker_dir": "US100Cash#", "asset_class": "Index"},
    {"canonical": "US500",  "broker_dir": "US500Cash#", "asset_class": "Index"},
    {"canonical": "WTI",    "broker_dir": "OILCash#",   "asset_class": "Commodity"},
]

MT5_HISTORY_BASE = os.path.expanduser(
    r"~\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\bases\XMGlobal-MT5 2\history"
)


def parse_mt5_hc(filepath: str) -> pd.DataFrame:
    """Fast, lossless parser for MetaTrader 5 binary H1.hc history cache files."""
    if not os.path.exists(filepath):
        return pd.DataFrame()

    try:
        with open(filepath, "rb") as f:
            data = f.read()

        if len(data) < 432:
            return pd.DataFrame()

        num_bars = struct.unpack_from("<I", data, 172)[0]
        if num_bars == 0 or len(data) < (432 + num_bars * 8):
            return pd.DataFrame()

        # Unpack contiguous arrays
        times = struct.unpack_from(f"<{num_bars}q", data, 432)
        
        off_open = 432 + num_bars * 8 + 4
        opens = struct.unpack_from(f"<{num_bars}d", data, off_open)
        
        off_high = off_open + num_bars * 8 + 4
        highs = struct.unpack_from(f"<{num_bars}d", data, off_high)
        
        off_low = off_high + num_bars * 8 + 4
        lows = struct.unpack_from(f"<{num_bars}d", data, off_low)
        
        off_close = off_low + num_bars * 8 + 4
        closes = struct.unpack_from(f"<{num_bars}d", data, off_close)
        
        off_vol = off_close + num_bars * 8 + 4
        vols = struct.unpack_from(f"<{num_bars}q", data, off_vol)

        df = pd.DataFrame({
            "time": pd.to_datetime(times, unit="s", utc=True),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols
        })
        return df.sort_values("time").reset_index(drop=True)
    except Exception as e:
        logger.error(f"Error parsing MT5 HC file {filepath}: {e}")
        return pd.DataFrame()


def load_2month_data(cfg: Dict[str, Any], days: int = 60) -> pd.DataFrame:
    """Loads and filters the last 2 months (60 days) of real MT5 historical data."""
    dirs_to_try = [cfg["broker_dir"]]
    if "fallback_dir" in cfg:
        dirs_to_try.append(cfg["fallback_dir"])

    df = pd.DataFrame()
    for b_dir in dirs_to_try:
        hc_file = os.path.join(MT5_HISTORY_BASE, b_dir, "cache", "H1.hc")
        df_cand = parse_mt5_hc(hc_file)
        if not df_cand.empty and (df.empty or df_cand["time"].max() > df["time"].max()):
            df = df_cand

    if df.empty:
        return pd.DataFrame()

    # Filter for last 60 days relative to latest bar in dataset
    latest_time = df["time"].max()
    cutoff_time = latest_time - timedelta(days=days)
    df_2m = df[df["time"] >= cutoff_time].copy().reset_index(drop=True)
    return df_2m


def _run_single_symbol_backtest(cfg: Dict[str, Any], initial_balance: float = 10000.0) -> Dict[str, Any]:
    """Worker function to execute backtest on a single symbol."""
    sym = cfg["canonical"]
    df = load_2month_data(cfg, days=60)
    if df.empty or len(df) < 30:
        return {"symbol": sym, "error": "Insufficient data", "trades": [], "metrics": {}}

    t0 = time.time()
    spec = resolve_symbol(sym)
    engine = BacktestEngine(
        initial_balance=initial_balance,
        risk_per_trade_pct=0.5,
        commission_per_lot=0.0,
        slippage_pips=0.5
    )

    res = engine.run_backtest(
        df_h1=df,
        symbol=sym,
        spread_pips=spec.typical_spread_pips,
        start_bar_idx=25
    )

    trades = res.get("trades", [])
    m = res.get("metrics", {})
    final_bal = res.get("final_balance", initial_balance)
    net_profit = final_bal - initial_balance
    roi_pct = (net_profit / initial_balance) * 100.0

    m["symbol"] = sym
    m["asset_class"] = cfg["asset_class"]
    m["bars_tested"] = len(df)
    m["start_date"] = df["time"].iloc[0].strftime("%Y-%m-%d %H:%M")
    m["end_date"] = df["time"].iloc[-1].strftime("%Y-%m-%d %H:%M")
    m["initial_balance"] = initial_balance
    m["final_balance"] = final_bal
    m["net_profit"] = net_profit
    m["roi_pct"] = roi_pct
    m["total_trades"] = len(trades)
    
    wins = [t for t in trades if t.get("is_win", False)]
    losses = [t for t in trades if not t.get("is_win", False)]
    m["win_count"] = len(wins)
    m["loss_count"] = len(losses)
    m["win_rate_pct"] = (len(wins) / max(1, len(trades))) * 100.0 if trades else 0.0

    longs = [t for t in trades if t.get("direction", "").upper() == "BUY" or t.get("type", "").upper() == "BUY"]
    shorts = [t for t in trades if t.get("direction", "").upper() == "SELL" or t.get("type", "").upper() == "SELL"]
    m["long_trades"] = len(longs)
    m["short_trades"] = len(shorts)
    m["long_win_rate"] = (len([t for t in longs if t.get("is_win")]) / max(1, len(longs))) * 100.0 if longs else 0.0
    m["short_win_rate"] = (len([t for t in shorts if t.get("is_win")]) / max(1, len(shorts))) * 100.0 if shorts else 0.0

    for tr in trades:
        tr["symbol"] = sym

    elapsed = time.time() - t0
    m["elapsed_sec"] = round(elapsed, 2)
    print(f" -> [{sym:<8}] {len(trades):>2} trades | WinRate: {m['win_rate_pct']:>5.1f}% | Net: ${net_profit:>+8.2f} | PF: {m.get('profit_factor', 0.0):>4.2f} | MaxDD: {m.get('max_drawdown_pct', 0.0):>4.1f}% | ({elapsed:.1f}s)", flush=True)

    return {"symbol": sym, "cfg": cfg, "metrics": m, "trades": trades, "final_balance": final_bal}


def run_2month_backtest(initial_balance_per_symbol: float = 10000.0):
    print("=" * 135, flush=True)
    print("                 HM Algo 2.0 — 2-MONTH REAL MT5 HISTORICAL BACKTEST (ALL 13 INSTITUTIONAL SYMBOLS)", flush=True)
    print("                 Source: Real MetaTrader 5 History Cache | Timeframe: H1 (60-Day Evaluation Window)", flush=True)
    print("=" * 135, flush=True)

    all_results = {}
    all_trades = []
    total_initial_balance = len(SYMBOL_CONFIGS) * initial_balance_per_symbol
    total_final_balance = 0.0

    print(f"\n[PHASE 1] Ingesting Real MT5 History Cache (Last 2 Months)...", flush=True)
    print(f"{'Symbol':<10} | {'Class':<11} | {'Bars':<6} | {'Date Range':<32} | {'First Close':<12} | {'Last Close':<12}", flush=True)
    print("-" * 135, flush=True)

    for cfg in SYMBOL_CONFIGS:
        sym = cfg["canonical"]
        df_2m = load_2month_data(cfg, days=60)
        if df_2m.empty:
            print(f"{sym:<10} | {cfg['asset_class']:<11} | {'0':<6} | {'NO DATA FOUND':<32} | {'-':<12} | {'-':<12}", flush=True)
            continue
        dt_start = df_2m["time"].iloc[0].strftime("%Y-%m-%d %H:%M")
        dt_end = df_2m["time"].iloc[-1].strftime("%Y-%m-%d %H:%M")
        first_c = f"{df_2m['close'].iloc[0]:,.2f}"
        last_c = f"{df_2m['close'].iloc[-1]:,.2f}"
        print(f"{sym:<10} | {cfg['asset_class']:<11} | {len(df_2m):<6} | {dt_start} -> {dt_end} | {first_c:<12} | {last_c:<12}", flush=True)

    t_all_start = time.time()
    for cfg in SYMBOL_CONFIGS:
        res = _run_single_symbol_backtest(cfg, initial_balance_per_symbol)
        sym = res["symbol"]
        if "metrics" in res and res["metrics"]:
            all_results[sym] = res["metrics"]
            all_trades.extend(res["trades"])
            total_final_balance += res.get("final_balance", initial_balance_per_symbol)

    total_time = time.time() - t_all_start
    print(f"\n[PHASE 2 COMPLETE] All 13 symbols finished simulation in {total_time:.1f}s!\n", flush=True)

    # Phase 3: Display Detailed Summary Table
    print("=" * 135, flush=True)
    print("                             HM Algo 2.0 — 2-MONTH BACKTEST MULTI-ASSET SUMMARY TABLE", flush=True)
    print("=" * 135, flush=True)
    print(f"{'Symbol':<8} | {'Class':<10} | {'Bars':<5} | {'Trades':<7} | {'Win %':<7} | {'PF':<6} | {'Net Profit':<12} | {'ROI %':<8} | {'Max DD %':<8} | {'Sharpe':<6} | {'Long WR':<8} | {'Short WR'}", flush=True)
    print("-" * 135, flush=True)

    for cfg in SYMBOL_CONFIGS:
        sym = cfg["canonical"]
        if sym not in all_results:
            continue
        r = all_results[sym]
        print(
            f"{sym:<8} | "
            f"{r['asset_class']:<10} | "
            f"{r['bars_tested']:<5} | "
            f"{r['total_trades']:<7} | "
            f"{r['win_rate_pct']:<6.1f}% | "
            f"{r.get('profit_factor', 0.0):<6.2f} | "
            f"${r['net_profit']:<+11.2f} | "
            f"{r['roi_pct']:<+7.2f}% | "
            f"{r.get('max_drawdown_pct', 0.0):<7.2f}% | "
            f"{r.get('sharpe_ratio', 0.0):<6.2f} | "
            f"{r['long_win_rate']:<7.1f}% | "
            f"{r['short_win_rate']:<6.1f}%",
            flush=True
        )

    print("=" * 135, flush=True)

    # Phase 4: Portfolio Level Performance
    portfolio_metrics = PerformanceMetricsCalculator.calculate_metrics(all_trades, total_initial_balance)
    total_net_pnl = total_final_balance - total_initial_balance
    portfolio_roi = (total_net_pnl / total_initial_balance) * 100.0

    print("\n" + "=" * 85, flush=True)
    print("                   COMBINED 13-ASSET 2-MONTH PORTFOLIO METRICS", flush=True)
    print("=" * 85, flush=True)
    print(f"Total Capital Allocated         : ${total_initial_balance:,.2f}", flush=True)
    print(f"Final Portfolio Equity          : ${total_final_balance:,.2f}", flush=True)
    print(f"Combined Net Profit ($)         : ${total_net_pnl:+,.2f}", flush=True)
    print(f"Combined Portfolio Growth (ROI) : {portfolio_roi:+.2f}%", flush=True)
    print(f"Total Combined Trades Executed  : {len(all_trades)}", flush=True)
    print(f"Overall Portfolio Win Rate      : {portfolio_metrics.get('win_rate_pct', 0.0):.2f}%", flush=True)
    print(f"Combined Profit Factor          : {portfolio_metrics.get('profit_factor', 0.0):.2f}", flush=True)
    print(f"Portfolio Max Drawdown          : {portfolio_metrics.get('max_drawdown_pct', 0.0):.2f}%", flush=True)
    print(f"Portfolio Sharpe Ratio          : {portfolio_metrics.get('sharpe_ratio', 0.0):.2f}", flush=True)
    print(f"Portfolio Sortino Ratio         : {portfolio_metrics.get('sortino_ratio', 0.0):.2f}", flush=True)
    print(f"Winning Trades / Losing Trades  : {portfolio_metrics.get('winning_trades', 0)} / {portfolio_metrics.get('losing_trades', 0)}", flush=True)
    print("=" * 85 + "\n", flush=True)

    # Save to JSON Report
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period_days": 60,
        "symbols_count": len(all_results),
        "total_initial_balance": total_initial_balance,
        "total_final_balance": total_final_balance,
        "total_net_profit": total_net_pnl,
        "portfolio_roi_pct": portfolio_roi,
        "portfolio_metrics": portfolio_metrics,
        "symbols": all_results,
        "trades": all_trades
    }

    out_file = os.path.join(BASE_DIR, "backtest_2month_all_symbols_report.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"[SUCCESS] Complete 2-Month Real MT5 Backtest Report saved to: {out_file}\n", flush=True)

    return report


if __name__ == "__main__":
    run_2month_backtest()
