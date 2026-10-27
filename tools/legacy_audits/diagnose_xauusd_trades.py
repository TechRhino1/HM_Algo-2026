"""
Forensic autopsy of XAUUSD 3-month backtest trades to identify exact failure patterns and calibrate for 70%+ win rate.
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ["JARVIS_BACKTEST_MODE"] = "1"
os.environ["JARVIS_OFFLINE_MODE"] = "1"

import pandas as pd
import numpy as np

from run_3month_real_mt5_backtest import load_real_mt5_data, SYMBOL_CONFIGS
from jarvis.backtesting.engine import BacktestEngine
from jarvis.data.symbol_registry import resolve as resolve_symbol

def analyze_gold():
    cfg = [c for c in SYMBOL_CONFIGS if c["canonical"] == "XAUUSD"][0]
    df = load_real_mt5_data(cfg, days=90)
    spec = resolve_symbol("XAUUSD")
    
    engine = BacktestEngine(
        initial_balance=10000.0,
        risk_per_trade_pct=0.5,
        commission_per_lot=0.0,
        slippage_pips=0.5
    )
    
    res = engine.run_backtest(
        df_h1=df,
        symbol="XAUUSD",
        spread_pips=spec.typical_spread_pips,
        start_bar_idx=25
    )
    
    trades = res.get("trades", [])
    print(f"Total trades: {len(trades)}")
    
    df_trades = pd.DataFrame(trades)
    if df_trades.empty:
        print("No trades executed.")
        return
        
    wins = df_trades[df_trades["is_win"] == True]
    losses = df_trades[df_trades["is_win"] == False]
    
    print("\n--- PERFORMANCE BY REGIME ---")
    for reg, grp in df_trades.groupby("regime"):
        w = len(grp[grp["is_win"] == True])
        tot = len(grp)
        wr = (w / tot) * 100.0
        pnl = grp["pnl"].sum()
        print(f"Regime {reg:<20}: {tot:>2} trades | {w:>2} wins | WR: {wr:>5.1f}% | PnL: ${pnl:>+8.2f}")
        
    print("\n--- PERFORMANCE BY STRATEGY ---")
    for strat, grp in df_trades.groupby("strategy"):
        w = len(grp[grp["is_win"] == True])
        tot = len(grp)
        wr = (w / tot) * 100.0
        pnl = grp["pnl"].sum()
        print(f"Strategy {strat:<30}: {tot:>2} trades | {w:>2} wins | WR: {wr:>5.1f}% | PnL: ${pnl:>+8.2f}")
        
    print("\n--- PERFORMANCE BY EXIT REASON ---")
    for ex, grp in df_trades.groupby("exit_reason"):
        w = len(grp[grp["is_win"] == True])
        tot = len(grp)
        wr = (w / tot) * 100.0
        pnl = grp["pnl"].sum()
        print(f"Exit {ex:<30}: {tot:>2} trades | {w:>2} wins | WR: {wr:>5.1f}% | PnL: ${pnl:>+8.2f}")
        
    print("\n--- PERFORMANCE BY UTC HOUR ---")
    df_trades["hour"] = pd.to_datetime(df_trades["open_time"]).dt.hour
    for hr, grp in df_trades.groupby("hour"):
        w = len(grp[grp["is_win"] == True])
        tot = len(grp)
        wr = (w / tot) * 100.0
        pnl = grp["pnl"].sum()
        print(f"Hour {hr:>02} UTC: {tot:>2} trades | {w:>2} wins | WR: {wr:>5.1f}% | PnL: ${pnl:>+8.2f}")

    print("\n--- PERFORMANCE BY SCORE QUANTILES ---")
    df_trades["score_bin"] = pd.qcut(df_trades["score"], q=4, duplicates="drop")
    for b, grp in df_trades.groupby("score_bin"):
        w = len(grp[grp["is_win"] == True])
        tot = len(grp)
        wr = (w / tot) * 100.0
        pnl = grp["pnl"].sum()
        print(f"Score {str(b):<25}: {tot:>2} trades | {w:>2} wins | WR: {wr:>5.1f}% | PnL: ${pnl:>+8.2f}")

if __name__ == "__main__":
    analyze_gold()
