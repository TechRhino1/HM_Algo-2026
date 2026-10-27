import os
import sys
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
"""
HM Algo 2.0 — 3-Month Real MT5 Historical Backtest Runner (All Institutional Symbols).
Loads 3 months (90 days) of real H1 bar data directly from MetaTrader 5 live broker,
executes event-driven backtest across all 14 institutional symbols, and verifies profitability.
"""
import os
import sys

os.environ["JARVIS_BACKTEST_MODE"] = "1"
os.environ["JARVIS_OFFLINE_MODE"] = "1"

import json
import time
import struct
import logging
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
logger = logging.getLogger("3MonthMT5Backtest")

SYMBOL_CONFIGS = [
    {"canonical": "XAUUSD", "broker_dir": "GOLD.i#",    "asset_class": "Metal"},
    {"canonical": "BTCUSD", "broker_dir": "BTCUSD#",    "asset_class": "Crypto"},
    {"canonical": "ETHUSD", "broker_dir": "ETHUSD#",    "asset_class": "Crypto"},
    {"canonical": "SOLUSD", "broker_dir": "SOLUSD#",    "asset_class": "Crypto"},
    {"canonical": "EURUSD", "broker_dir": "EURUSD#",    "asset_class": "Forex", "fallback_dir": "EURUSD"},
    {"canonical": "GBPUSD", "broker_dir": "GBPUSD#",    "asset_class": "Forex", "fallback_dir": "GBPUSD"},
    {"canonical": "USDJPY", "broker_dir": "USDJPY#",    "asset_class": "Forex", "fallback_dir": "USDJPY"},
    {"canonical": "AUDUSD", "broker_dir": "AUDUSD#",    "asset_class": "Forex"},
    {"canonical": "USDCAD", "broker_dir": "USDCAD#",    "asset_class": "Forex"},
    {"canonical": "USDCHF", "broker_dir": "USDCHF#",    "asset_class": "Forex"},
    {"canonical": "NZDUSD", "broker_dir": "NZDUSD#",    "asset_class": "Forex"},
    {"canonical": "US30",   "broker_dir": "US30Cash#",  "asset_class": "Index"},
    {"canonical": "NAS100", "broker_dir": "US100Cash#", "asset_class": "Index"},
    {"canonical": "US500",  "broker_dir": "US500Cash#", "asset_class": "Index"},
    {"canonical": "WTI",    "broker_dir": "OILCash#",   "asset_class": "Commodity"},
]

def load_real_mt5_data(cfg: Dict[str, Any], days: int = 90) -> pd.DataFrame:
    """Loads 90 days of real MT5 data from live MT5 terminal or cache."""
    sym_broker = cfg["broker_dir"]
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            bars_to_fetch = max(1000, days * 24 + 100)
            rates = mt5.copy_rates_from_pos(sym_broker, mt5.TIMEFRAME_H1, 0, bars_to_fetch)
            if rates is not None and len(rates) > 50:
                df = pd.DataFrame(rates)
                df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
                # Filter for last N days
                latest_time = df["time"].max()
                cutoff_time = latest_time - timedelta(days=days)
                df_sub = df[df["time"] >= cutoff_time].copy().reset_index(drop=True)
                if not df_sub.empty and len(df_sub) >= 30:
                    return df_sub
    except Exception as e:
        logger.debug(f"Live MT5 fetch for {sym_broker} failed: {e}")

    # Fallback to binary HC cache file in AppData
    base_cache_dirs = [
        os.path.expanduser(r"~\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\bases\XMGlobal-MT5 5\history"),
        os.path.expanduser(r"~\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\bases\XMGlobal-MT5 2\history"),
    ]
    dirs_to_try = [cfg["broker_dir"]]
    if "fallback_dir" in cfg:
        dirs_to_try.append(cfg["fallback_dir"])

    for base in base_cache_dirs:
        for b_dir in dirs_to_try:
            hc_file = os.path.join(base, b_dir, "cache", "H1.hc")
            if os.path.exists(hc_file):
                try:
                    with open(hc_file, "rb") as f:
                        data = f.read()
                    if len(data) >= 432:
                        num_bars = struct.unpack_from("<I", data, 172)[0]
                        if num_bars > 0 and len(data) >= (432 + num_bars * 8):
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
                            }).sort_values("time").reset_index(drop=True)

                            latest_time = df["time"].max()
                            cutoff_time = latest_time - timedelta(days=days)
                            df_sub = df[df["time"] >= cutoff_time].copy().reset_index(drop=True)
                            if not df_sub.empty and len(df_sub) >= 30:
                                return df_sub
                except Exception:
                    pass

    return pd.DataFrame()


def _run_single_symbol_backtest(cfg: Dict[str, Any], initial_balance: float = 10000.0) -> Dict[str, Any]:
    """Worker function to execute backtest on a single symbol."""
    sym = cfg["canonical"]
    df = load_real_mt5_data(cfg, days=90)
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


def run_3month_backtest(initial_balance_per_symbol: float = 10000.0):
    print("=" * 140, flush=True)
    print("                 HM Algo 2.0 — 3-MONTH REAL MT5 HISTORICAL BACKTEST (ALL INSTITUTIONAL SYMBOLS)", flush=True)
    print("                 Source: Real MetaTrader 5 Live Historical Bars | Timeframe: H1 (90-Day Evaluation Window)", flush=True)
    print("=" * 140, flush=True)

    all_results = {}
    all_trades = []
    total_initial_balance = len(SYMBOL_CONFIGS) * initial_balance_per_symbol
    total_final_balance = 0.0

    for cfg in SYMBOL_CONFIGS:
        res = _run_single_symbol_backtest(cfg, initial_balance=initial_balance_per_symbol)
        sym = cfg["canonical"]
        all_results[sym] = res
        all_trades.extend(res.get("trades", []))
        total_final_balance += res.get("final_balance", initial_balance_per_symbol)

    # Portfolio Aggregations
    total_trades = len(all_trades)
    winning_trades = [t for t in all_trades if t.get("is_win", False)]
    losing_trades = [t for t in all_trades if not t.get("is_win", False)]
    total_net_pnl = total_final_balance - total_initial_balance
    portfolio_roi = (total_net_pnl / total_initial_balance) * 100.0
    overall_win_rate = (len(winning_trades) / max(1, total_trades)) * 100.0

    gross_profit = sum(t.get("pnl", 0.0) for t in winning_trades)
    gross_loss = abs(sum(t.get("pnl", 0.0) for t in losing_trades))
    profit_factor = round(gross_profit / max(1e-4, gross_loss), 2) if gross_loss > 0 else 999.0

    print("\n" + "=" * 140, flush=True)
    print("                                      PORTFOLIO PERFORMANCE SUMMARY (3-MONTH MT5 AUDIT)", flush=True)
    print("=" * 140, flush=True)
    print(f" Total Symbols Tested   : {len(SYMBOL_CONFIGS)}")
    print(f" Evaluation Window      : Last 90 Days (H1 Real MT5 Historical Feed)")
    print(f" Portfolio Initial Capital: ${total_initial_balance:,.2f}")
    print(f" Portfolio Final Capital  : ${total_final_balance:,.2f}")
    print(f" Total Net Profit       : ${total_net_pnl:+,.2f} ({portfolio_roi:+.2f}%)")
    print(f" Total Executed Trades  : {total_trades}")
    print(f" Win Rate               : {overall_win_rate:.1f}% ({len(winning_trades)} W / {len(losing_trades)} L)")
    print(f" Profit Factor          : {profit_factor:.2f}")
    print(f" Gross Profit           : ${gross_profit:,.2f}")
    print(f" Gross Loss             : ${gross_loss:,.2f}")
    print("=" * 140, flush=True)

    # Save summary json
    summary = {
        "evaluation_window_days": 90,
        "total_symbols": len(SYMBOL_CONFIGS),
        "total_initial_balance": total_initial_balance,
        "total_final_balance": total_final_balance,
        "total_net_pnl": total_net_pnl,
        "portfolio_roi_pct": portfolio_roi,
        "total_trades": total_trades,
        "win_rate_pct": overall_win_rate,
        "profit_factor": profit_factor,
        "symbols": {s: r["metrics"] for s, r in all_results.items()}
    }

    with open("3month_mt5_backtest_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print("Results saved to 3month_mt5_backtest_summary.json", flush=True)

if __name__ == "__main__":
    run_3month_backtest()
