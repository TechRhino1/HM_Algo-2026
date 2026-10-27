import os
import sys
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
"""
HM Algo 2.0 — 6-Month REAL MT5 Live Data Backtest (All 4 Primary Symbols)
"""
import os
import sys
import time
from datetime import datetime

import pandas as pd
import MetaTrader5 as mt5

BASE_DIR = str(Path(__file__).resolve().parent.parent.parent)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from jarvis.backtesting.engine import BacktestEngine
from jarvis.backtesting.walk_forward import WalkForwardEngine
from jarvis.data.symbol_registry import resolve as resolve_symbol

TF_MAP = {"H1": 16385}

def resolve_broker_symbol(mt5_instance, canonical_name):
    u_name = canonical_name.upper()
    alias_map = {
        "XAUUSD": ["GOLD.i#", "GOLD#", "GOLD", "XAUUSD", "XAUUSD#", "XAUUSD.i#", "XAUUSDm"],
        "BTCUSD": ["BTCUSD#", "BTCUSD", "BTCUSD.i#", "BTCUSD.i", "BTCUSDm", "BITCOIN"],
        "EURUSD": ["EURUSD", "EURUSD#", "EURUSD.i#", "EURUSD.i", "EURUSDm"],
        "GBPUSD": ["GBPUSD", "GBPUSD#", "GBPUSD.i#", "GBPUSD.i", "GBPUSDm"],
    }
    candidates = [canonical_name]
    if u_name in alias_map:
        for a in alias_map[u_name]:
            if a not in candidates:
                candidates.append(a)
    else:
        for suffix in ["#", ".i#", ".i", "m", ".m"]:
            c = f"{canonical_name}{suffix}"
            if c not in candidates:
                candidates.append(c)
    for cand in candidates:
        info = mt5_instance.symbol_info(cand)
        if info is not None and info.visible:
            return cand
    for cand in candidates:
        if mt5_instance.symbol_add(cand):
            return cand
    return canonical_name

def fetch_mt5_data(symbol, num_bars=4380):
    resolved = resolve_broker_symbol(mt5, symbol)
    tf = TF_MAP["H1"]
    rates = mt5.copy_rates_from_pos(resolved, tf, 1, num_bars)
    if rates is None or len(rates) == 0:
        rates = mt5.copy_rates_from_pos(resolved, tf, 0, num_bars)
    if rates is None or len(rates) == 0:
        print(f"  [ERROR] MT5 returned 0 rates for {symbol}")
        return None, resolved, 0, None, None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    df = df[["time", "open", "high", "low", "close", "volume"]].copy()
    first_date = df["time"].iloc[0]
    last_date = df["time"].iloc[-1]
    return df, resolved, len(df), first_date, last_date

def run_full_backtest(df, symbol, initial_balance=10000.0, risk_pct=0.75):
    engine = BacktestEngine(initial_balance=initial_balance, risk_per_trade_pct=risk_pct)
    result = engine.run_backtest(df, symbol=symbol)
    return result

def run_walk_forward(df, symbol, num_folds=6, initial_balance=10000.0, risk_pct=0.75):
    engine = WalkForwardEngine(num_folds=num_folds, in_sample_pct=0.70, initial_balance=initial_balance)
    result = engine.run_walk_forward_validation(df, symbol=symbol)
    return result

def print_trade_log(result, symbol, max_trades=30):
    trades = result.get("trades", [])
    if not trades:
        return
    print(f"\n  {'#':<4} {'Date':<12} {'Dir':<5} {'Entry':<12} {'SL':<12} {'TP':<12} {'Exit':<12} {'PnL':<10} {'R:R':<6} {'Strategy':<25} {'Regime':<15} {'Result':<6}")
    print("  " + "-" * 150)
    for i, t in enumerate(trades[:max_trades]):
        entry_date = str(t.get("entry_time", ""))[:10]
        direction = t.get("direction", "?")
        entry = t.get("entry_price", 0)
        sl = t.get("sl", 0)
        tp = t.get("tp", 0)
        exit_price = t.get("exit_price", 0)
        pnl = t.get("pnl", 0)
        rr = t.get("planned_rr", 0)
        strategy = t.get("strategy", "?")
        regime = t.get("regime", "?")
        result_str = "WIN" if t.get("is_win", False) else "LOSS"
        pnl_str = f"${pnl:+.2f}"
        print(f"  {i+1:<4} {entry_date:<12} {direction:<5} {entry:<12.2f} {sl:<12.2f} {tp:<12.2f} {exit_price:<12.2f} {pnl_str:<10} {rr:<6.2f} {strategy:<25} {regime:<15} {result_str:<6}")

if __name__ == "__main__":
    mt5.initialize()
    info = mt5.account_info()
    print("=" * 100)
    print("  HM Algo 2.0 — 6-MONTH REAL MT5 LIVE DATA BACKTEST (4 SYMBOLS)")
    print(f"  Server: {info.server} | Account: #{info.login} | Balance: ${info.balance:,.2f} | Leverage: 1:{info.leverage}")
    print("=" * 100)

    symbols = ["XAUUSD", "BTCUSD", "EURUSD", "GBPUSD"]
    all_results = {}

    for sym in symbols:
        df, resolved, num_bars, first_dt, last_dt = fetch_mt5_data(sym, num_bars=4380)
        if df is None:
            print(f"\n  Skipping {sym} — no data")
            continue

        print(f"\n  Loaded {num_bars} REAL MT5 H1 bars for {sym} [{resolved}] | {first_dt:%Y-%m-%d} to {last_dt:%Y-%m-%d}")

        print(f"\n  Running Full 6-Month Backtest for {sym}...")
        t0 = time.time()
        result = run_full_backtest(df, sym, initial_balance=10000.0, risk_pct=0.75)
        t1 = time.time()
        m = result.get("metrics", {})
        trades = result.get("trades", [])
        wins = [t for t in trades if t.get("is_win", False)]
        losses = [t for t in trades if not t.get("is_win", False)]
        net_pnl = sum(t.get("pnl", 0) for t in trades)
        gross_win = sum(t.get("pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("pnl", 0) for t in losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
        wr = (len(wins) / max(1, len(trades))) * 100.0
        final_bal = 10000.0 + net_pnl
        roi = (net_pnl / 10000.0) * 100.0

        print(f"\n  Full Backtest Results ({sym}):")
        print(f"    Trades: {len(trades)} | Wins: {len(wins)} | Losses: {len(losses)}")
        print(f"    Win Rate: {wr:.1f}% | PF: {pf:.2f} | Net PnL: ${net_pnl:+,.2f} | ROI: {roi:+.2f}%")
        print(f"    Final Balance: ${final_bal:,.2f} | Time: {t1-t0:.1f}s")

        print_trade_log(result, sym, max_trades=20)

        print(f"\n  Running 6-Fold Walk-Forward Validation for {sym}...")
        t0 = time.time()
        wf = run_walk_forward(df, sym, num_folds=6, initial_balance=10000.0, risk_pct=0.75)
        t1 = time.time()

        oos_metrics = wf.get("aggregate_oos_metrics", {})
        oos_wr = oos_metrics.get("win_rate_pct", 0)
        oos_pf = oos_metrics.get("profit_factor", 0)
        oos_trades = oos_metrics.get("total_trades", 0)
        wfe = wf.get("walk_forward_efficiency", 0)
        is_metrics = wf.get("aggregate_is_metrics", {})
        is_wr = is_metrics.get("win_rate_pct", 0)
        is_pf = is_metrics.get("profit_factor", 0)

        folds = wf.get("folds", [])
        print(f"\n  Walk-Forward Validation ({sym}):")
        print(f"    {'Fold':<6} {'IS Trades':<10} {'IS WR%':<10} {'IS PF':<10} {'OOS Trades':<12} {'OOS WR%':<12} {'OOS PF':<10} {'WFE':<8}")
        print("    " + "-" * 90)
        for fi, fold in enumerate(folds):
            is_m = fold.get("in_sample_metrics", {})
            oos_m = fold.get("out_of_sample_metrics", {})
            print(f"    {fi+1:<6} {is_m.get('total_trades',0):<10} {is_m.get('win_rate_pct',0):<10.1f} {is_m.get('profit_factor',0):<10.2f} {oos_m.get('total_trades',0):<12} {oos_m.get('win_rate_pct',0):<12.1f} {oos_m.get('profit_factor',0):<10.2f} {fold.get('wfe',0):<8.2f}")
        print("    " + "-" * 90)
        print(f"    {'AGG':<6} {'':10} {is_wr:<10.1f} {is_pf:<10.2f} {oos_trades:<12} {oos_wr:<12.1f} {oos_pf:<10.2f} {wfe:<8.2f}")

        all_results[sym] = {
            "full_trades": len(trades), "full_wr": wr, "full_pf": pf, "full_net": net_pnl, "full_roi": roi, "full_final": final_bal,
            "oos_trades": oos_trades, "oos_wr": oos_wr, "oos_pf": oos_pf, "wfe": wfe,
            "is_wr": is_wr, "is_pf": is_pf,
            "first_date": first_dt, "last_date": last_dt, "num_bars": num_bars
        }

    print("\n" + "=" * 100)
    print("  FINAL 6-MONTH PERFORMANCE SUMMARY (REAL MT5 DATA - 4 SYMBOLS)")
    print("=" * 100)
    print(f"  {'Metric':<30} | {'XAUUSD':<18} | {'BTCUSD':<18} | {'EURUSD':<18} | {'GBPUSD':<18}")
    print("  " + "-" * 95)
    r = {s: all_results.get(s, {}) for s in symbols}

    rows = [
        ("Data Period", lambda s: f"{r[s].get('first_date', 'N/A'):%Y-%m-%d} to {r[s].get('last_date', 'N/A'):%Y-%m-%d}" if r[s].get('first_date') else "N/A"),
        ("H1 Bars Simulated", lambda s: f"{r[s].get('num_bars', 0):,}"),
        ("Full Backtest Trades", lambda s: f"{r[s].get('full_trades', 0)}"),
        ("Full Backtest Win Rate", lambda s: f"{r[s].get('full_wr', 0):.1f}%"),
        ("Full Backtest Profit Factor", lambda s: f"{r[s].get('full_pf', 0):.2f}"),
        ("Full Backtest Net PnL", lambda s: f"${r[s].get('full_net', 0):+,.2f}"),
        ("Full Backtest ROI", lambda s: f"{r[s].get('full_roi', 0):+.2f}%"),
        ("Full Backtest Final Balance", lambda s: f"${r[s].get('full_final', 10000):,.2f}"),
        ("Walk-Forward OOS Trades", lambda s: f"{r[s].get('oos_trades', 0)}"),
        ("Walk-Forward IS Win Rate", lambda s: f"{r[s].get('is_wr', 0):.1f}%"),
        ("Walk-Forward OOS Win Rate", lambda s: f"{r[s].get('oos_wr', 0):.1f}%"),
        ("Walk-Forward IS Profit Factor", lambda s: f"{r[s].get('is_pf', 0):.2f}"),
        ("Walk-Forward OOS Profit Factor", lambda s: f"{r[s].get('oos_pf', 0):.2f}"),
        ("Walk-Forward Efficiency", lambda s: f"{r[s].get('wfe', 0):.2f}"),
    ]
    for label, fn in rows:
        vals = [fn(s) for s in symbols]
        print(f"  {label:<30} | {vals[0]:<18} | {vals[1]:<18} | {vals[2]:<18} | {vals[3]:<18}")

    # Portfolio totals
    total_net = sum(r[s].get('full_net', 0) for s in symbols)
    total_final = sum(r[s].get('full_final', 10000) for s in symbols)
    total_roi = (total_net / 40000.0) * 100.0
    print("  " + "-" * 95)
    print(f"  {'PORTFOLIO TOTAL':<30} | {'':18} | {'':18} | {'':18} | {'':18}")
    print(f"  {'Total Net PnL':<30} | ${total_net:+,.2f}")
    print(f"  {'Total ROI':<30} | {total_roi:+.2f}%")
    print(f"  {'Total Final Balance':<30} | ${total_final:,.2f}")

    print("  " + "=" * 95)
    print("  NOTE: ALL data sourced from LIVE MT5 historical H1 candles. No synthetic data.")
    print("=" * 100)

    mt5.shutdown()