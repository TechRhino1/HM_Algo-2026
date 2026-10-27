"""
Targeted backtest evaluating Precision Rules on XAUUSD to verify 70%+ Win Rate.
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
from jarvis.intelligence.symbol_profile_config import SYMBOL_PROFILES

def run_test():
    cfg = [c for c in SYMBOL_CONFIGS if c["canonical"] == "XAUUSD"][0]
    df = load_real_mt5_data(cfg, days=90)
    spec = resolve_symbol("XAUUSD")
    
    orig_prof = SYMBOL_PROFILES["XAUUSD"]
    orig_be = orig_prof.be_trigger_r
    orig_fc = orig_prof.fast_cash_r
    orig_fc_vol = orig_prof.fast_cash_volume_pct
    orig_trail = orig_prof.runner_trail_atr
    orig_hours = orig_prof.allowed_utc_hours
    orig_prem = orig_prof.premium_window_exception
    orig_strat = dict(orig_prof.strategy_weights)
    orig_banned = list(orig_prof.banned_strategies)
    
    experiments = [
        {
            "name": "1. Baseline (Current)",
            "be_r": 1.80, "fc_r": 1.50, "fc_vol": 0.33, "trail": 2.00,
            "hours": (12, 16), "prem_exc": True,
        },
        {
            "name": "2. Fast Cash 0.9R + BE 0.95R + Bank 65% + Trail 1.4 ATR",
            "be_r": 0.95, "fc_r": 0.90, "fc_vol": 0.65, "trail": 1.40,
            "hours": (12, 16), "prem_exc": True,
        },
        {
            "name": "3. Fast Cash 0.85R + BE 0.90R + Bank 70% + Trail 1.3 ATR",
            "be_r": 0.90, "fc_r": 0.85, "fc_vol": 0.70, "trail": 1.30,
            "hours": (12, 16), "prem_exc": True,
        },
        {
            "name": "4. Fast Cash 0.80R + BE 0.80R + Bank 70% + Trail 1.2 ATR",
            "be_r": 0.80, "fc_r": 0.80, "fc_vol": 0.70, "trail": 1.20,
            "hours": (12, 16), "prem_exc": True,
        },
        {
            "name": "5. Fast Cash 0.75R + BE 0.75R + Bank 75% + Trail 1.2 ATR",
            "be_r": 0.75, "fc_r": 0.75, "fc_vol": 0.75, "trail": 1.20,
            "hours": (12, 16), "prem_exc": True,
        },
    ]
    
    print("=" * 130)
    print("                      XAUUSD PRECISION EXIT GEOMETRY SWEEP")
    print("=" * 130)
    
    for exp in experiments:
        orig_prof.be_trigger_r = exp["be_r"]
        orig_prof.fast_cash_r = exp["fc_r"]
        orig_prof.fast_cash_volume_pct = exp["fc_vol"]
        orig_prof.runner_trail_atr = exp["trail"]
        orig_prof.allowed_utc_hours = exp["hours"]
        orig_prof.premium_window_exception = exp["prem_exc"]
        
        engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
        res = engine.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25)
        
        trades = res.get("trades", [])
        wins = [t for t in trades if t.get("is_win", False)]
        losses = [t for t in trades if not t.get("is_win", False)]
        wr = (len(wins) / max(1, len(trades))) * 100.0 if trades else 0.0
        final_bal = res.get("final_balance", 10000.0)
        net_pnl = final_bal - 10000.0
        pf = res.get("metrics", {}).get("profit_factor", 0.0)
        max_dd = res.get("metrics", {}).get("max_drawdown_pct", 0.0)
        
        print(f"[{exp['name']:<70}] Trades: {len(trades):>2} | WR: {wr:>5.1f}% | Net: ${net_pnl:>+8.2f} | PF: {pf:>4.2f} | MaxDD: {max_dd:>4.1f}%", flush=True)

    # Restore
    orig_prof.be_trigger_r = orig_be
    orig_prof.fast_cash_r = orig_fc
    orig_prof.fast_cash_volume_pct = orig_fc_vol
    orig_prof.runner_trail_atr = orig_trail
    orig_prof.allowed_utc_hours = orig_hours
    orig_prof.premium_window_exception = orig_prem
    print("=" * 130)

if __name__ == "__main__":
    run_test()
