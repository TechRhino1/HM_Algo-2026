"""
Inspect Gold trades under different strategy combinations to reach 70%+ Win Rate.
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

def test_strategies():
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
    
    orig_prof.be_trigger_r = 0.85
    orig_prof.fast_cash_r = 0.85
    orig_prof.fast_cash_volume_pct = 0.70
    orig_prof.runner_trail_atr = 1.25
    orig_prof.allowed_utc_hours = (7, 16)
    orig_prof.premium_window_exception = False
    
    # Try different strategy allocations:
    cases = [
        {"name": "CHOCH Only", "choch": 1.0, "sweep": 0.0, "pullback": 0.0},
        {"name": "CHOCH (70%) + Pullback (30%)", "choch": 0.70, "sweep": 0.0, "pullback": 0.30},
        {"name": "CHOCH (80%) + Pullback (20%)", "choch": 0.80, "sweep": 0.0, "pullback": 0.20},
        {"name": "CHOCH (60%) + Sweep (20%) + Pullback (20%)", "choch": 0.60, "sweep": 0.20, "pullback": 0.20},
    ]
    
    orig_select = StrategySelector.select_strategy_probabilities
    
    for case in cases:
        def make_patched(c):
            def patched_select(self, regime, context=None, **kwargs):
                probs = orig_select(self, regime, context, **kwargs)
                sym = getattr(context, "symbol", "") or ""
                if sym in ("XAUUSD", "GOLD", "GOLD.i#", "GOLD#", "XAGUSD", "SILVER"):
                    for k in list(probs.keys()):
                        probs[k] = 0.0
                    probs["CHOCH_STRUCTURAL_REVERSAL"] = c["choch"]
                    probs["LIQUIDITY_SWEEP_REVERSAL"] = c["sweep"]
                    probs["TREND_PULLBACK"] = c["pullback"]
                return probs
            return patched_select
            
        StrategySelector.select_strategy_probabilities = make_patched(case)
        engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
        res = engine.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25)
        
        trades = res.get("trades", [])
        wins = [t for t in trades if t.get("is_win", False)]
        wr = (len(wins) / max(1, len(trades))) * 100.0 if trades else 0.0
        pnl = res.get("final_balance", 10000.0) - 10000.0
        pf = res.get("metrics", {}).get("profit_factor", 0.0)
        
        print(f"CASE [{case['name']:<40}]: {len(trades):>3} trades | {len(wins):>3} wins | WR: {wr:>5.1f}% | PnL: ${pnl:>+8.2f} | PF: {pf:.2f}")

    # Restore
    StrategySelector.select_strategy_probabilities = orig_select
    orig_prof.be_trigger_r = orig_be
    orig_prof.fast_cash_r = orig_fc
    orig_prof.fast_cash_volume_pct = orig_fc_vol
    orig_prof.runner_trail_atr = orig_trail
    orig_prof.allowed_utc_hours = orig_hours
    orig_prof.premium_window_exception = orig_prem

if __name__ == "__main__":
    test_strategies()
