"""
Verify XAUUSD Win Rate and Profitability on real MT5 data using 100% pure production code.
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

def run_verification():
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
    wins = [t for t in trades if t.get("is_win", False)]
    losses = [t for t in trades if not t.get("is_win", False)]
    wr = (len(wins) / max(1, len(trades))) * 100.0 if trades else 0.0
    final_bal = res.get("final_balance", 10000.0)
    net_pnl = final_bal - 10000.0
    pf = res.get("metrics", {}).get("profit_factor", 0.0)
    max_dd = res.get("metrics", {}).get("max_drawdown_pct", 0.0)
    
    print("=" * 100)
    print("PRODUCTION CODE VERIFICATION FOR XAUUSD (GOLD):")
    print(f"Total Trades : {len(trades)}")
    print(f"Win Rate     : {wr:.1f}% ({len(wins)} W / {len(losses)} L)")
    print(f"Net Profit   : ${net_pnl:+,.2f}")
    print(f"Profit Factor: {pf:.2f}")
    print(f"Max Drawdown : {max_dd:.1f}%")
    print("=" * 100)
    
    df_trades = pd.DataFrame(trades)
    if not df_trades.empty:
        print("\nBreakdown by strategy:")
        for strat, grp in df_trades.groupby("strategy"):
            w = len(grp[grp["is_win"] == True])
            print(f"  {strat:<30}: {len(grp):>2} trades | {w:>2} wins | WR: {(w/len(grp))*100:>5.1f}% | PnL: ${grp['pnl'].sum():>+8.2f}")
            
        print("\nBreakdown by exit reason:")
        for ex, grp in df_trades.groupby("exit_reason"):
            w = len(grp[grp["is_win"] == True])
            print(f"  {ex:<30}: {len(grp):>2} trades | {w:>2} wins | WR: {(w/len(grp))*100:>5.1f}% | PnL: ${grp['pnl'].sum():>+8.2f}")

if __name__ == "__main__":
    run_verification()
