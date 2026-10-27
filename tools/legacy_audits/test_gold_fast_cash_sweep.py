"""
Sweep fast_cash_r and be_trigger_r from 0.60R to 0.90R on Gold to pinpoint 70%+ Win Rate.
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
from jarvis.intelligence.strategy_selector import StrategySelector

def sweep_fast_cash():
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
    
    orig_prof.allowed_utc_hours = (7, 16)
    orig_prof.premium_window_exception = False
    
    orig_select = StrategySelector.select_strategy_probabilities
    def patched_select(self, regime, context=None, **kwargs):
        probs = orig_select(self, regime, context, **kwargs)
        sym = getattr(context, "symbol", "") or ""
        if sym in ("XAUUSD", "GOLD", "GOLD.i#", "GOLD#", "XAGUSD", "SILVER"):
            probs["RANGE_MEAN_REVERSION"] = 0.0
            probs["TREND_FOLLOWING"] = 0.0
            probs["BREAKOUT_EXPANSION"] = 0.0
            probs["LIQUIDITY_SWEEP_REVERSAL"] = 0.0
            probs["CHOCH_STRUCTURAL_REVERSAL"] = 5.0
            probs["TREND_PULLBACK"] = 1.5
            tot = sum(probs.values())
            if tot > 0:
                probs = {k: v / tot for k, v in probs.items()}
        return probs
    StrategySelector.select_strategy_probabilities = patched_select
    
    r_targets = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
    
    print("=" * 100)
    print("FAST CASH & BREAKEVEN R-TRIGGER SWEEP ON REAL MT5 90-DAY GOLD DATA:")
    print("=" * 100)
    
    for r in r_targets:
        orig_prof.be_trigger_r = r
        orig_prof.fast_cash_r = r
        orig_prof.fast_cash_volume_pct = 0.70
        orig_prof.runner_trail_atr = 1.25
        
        engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
        res = engine.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25)
        
        trades = res.get("trades", [])
        wins = [t for t in trades if t.get("is_win", False)]
        losses = [t for t in trades if not t.get("is_win", False)]
        wr = (len(wins) / max(1, len(trades))) * 100.0 if trades else 0.0
        final_bal = res.get("final_balance", 10000.0)
        net_pnl = final_bal - 10000.0
        pf = res.get("metrics", {}).get("profit_factor", 0.0)
        
        print(f"Trigger {r:.2f}R | Trades: {len(trades):>3} | Wins: {len(wins):>3} | WR: {wr:>5.1f}% | Net PnL: ${net_pnl:>+8.2f} | PF: {pf:.2f}")

    # Restore
    StrategySelector.select_strategy_probabilities = orig_select
    orig_prof.be_trigger_r = orig_be
    orig_prof.fast_cash_r = orig_fc
    orig_prof.fast_cash_volume_pct = orig_fc_vol
    orig_prof.runner_trail_atr = orig_trail
    orig_prof.allowed_utc_hours = orig_hours
    orig_prof.premium_window_exception = orig_prem

if __name__ == "__main__":
    sweep_fast_cash()
